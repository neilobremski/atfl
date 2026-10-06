"""SVG map panel + scene compositing — the single-image turn render.

Promoted 2026-10-02 from prototype/scene_map_overlay_demo.py into server
code (work session #82). Neil's 2026-10-02 verdict: one image per turn —
the scene, with the map drawn as SVG and rendered into the raster image
in the bottom-left corner, whole thing compressed to a JPEG. Selfie is
cut until scene+map look right.

Geometry contract: layout_map() + _TOD_PALETTE come from server/map_panel,
so the SVG and the old PIL panel agree byte-for-byte on node positions;
only the drawing backend changes (PIL -> SVG -> cairo).

Determinism: seeded randomness only, exactly the same seeds as the PIL
panel, so the SVG byte-stream is stable for a given game state.

Composite spec (Neil's 2026-10-04 direction, supersedes the 2026-10-03
half-size verdict): the scene stays 1024x1024; only the map shrinks 50%
(340 -> 170), fonts scaling down with it. The map is rendered at its FULL
340 design size and LANCZOS-downscaled to 170 (rasterize_scaled) —
rendering small directly made the cairosvg text a jumble. This supersedes
the 2026-10-02 "never rendered big and downscaled" rule.
"""
from __future__ import annotations

import io
import math
import os
import random
import shutil

from .map_panel import _TOD_PALETTE, _seed, layout_map

# Overlay geometry. OVERLAY_PX = the map's full design size. Neil's
# 2026-10-04 direction: the scene stays 1024; only the map shrinks 50%
# (340 -> 170). The map is rendered at full size and downscaled with
# LANCZOS (rasterize_scaled); fonts scale down with the map by
# construction. This supersedes the 2026-10-02 "never downscale" rule
# and the 2026-10-03 512px composite.
OVERLAY_PX = 340
OVERLAY_MARGIN_PX = 28
OVERLAY_V2_PX = 170
OVERLAY_V2_MARGIN_PX = 14


def _sketch_path(p0, p1, seed_text, size, passes=2, jitter=9):
    """Midpoint-displaced polyline, 2 passes — mirrors map_panel's
    _sketch_line seeds, so frame/edges match the PIL panel."""
    out = []
    for p in range(passes):
        rng = random.Random(_seed(seed_text) + p * 7919)
        x0, y0 = p0
        x1, y1 = p1
        mid = [((x0 + x1) / 2, (y0 + y1) / 2)]
        for _ in range(2):
            new_mid = []
            prev = p0
            for mx, my in mid + [p1]:
                nx, ny = (prev[0] + mx) / 2, (prev[1] + my) / 2
                j = jitter if p == 0 else jitter * 0.55
                new_mid.append((nx + rng.uniform(-j, j), ny + rng.uniform(-j, j)))
                prev = (mx, my)
            mid = new_mid
        pts = [p0] + mid + [p1]
        d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        out.append(d)
    return out


def _sketch_circle_path(center, radius, seed_text):
    rng = random.Random(_seed(seed_text))
    pts = []
    for i in range(24):
        a = 2 * math.pi * i / 24
        r = radius + rng.uniform(-radius * 0.12, radius * 0.12)
        pts.append((center[0] + r * math.cos(a), center[1] + r * math.sin(a)))
    d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"
    return d


def _wrap(name, width=16):
    words, lines, cur = name.split(), [], ""
    for w in words:
        if len(cur) + 1 + len(w) > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def _rgb(t):
    return "#%02x%02x%02x" % t


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Map presentation labels (Neil's 2026-10-04 direction: the map's labels
# are short — "YOU" + interesting points only. The map orients the player;
# it does not describe. The seed's sentence-length names stay the places'
# prose identity; this table is the map's label layer, not a rename.)
SHORT_LABELS = {
    "trailhead": "Trailhead",
    "trail-down": "Descent",
    "trail-up": "Switchbacks",
    "fog-below": "Fog",
}


# Map house palette (Neil's 2026-10-06 verdict: the vB sketch/dusk look).
# The palette is the map's aesthetic identity; the time-of-day caption on
# the panel keeps reporting the game clock truthfully (see render_map_svg's
# palette parameter). Neil picked the dusk *look*, not dusk the clock hour.
MAP_PALETTE = "dusk"


def render_map_svg(places, edges, player_slug, time_of_day="morning",
                   size=1024, labels=None, style="sketch", palette=None):
    """Map as an SVG string. Same inputs/contract as map_panel.render_map.

    Only discovered places are ever drawn (the caller passes the filtered
    set). labels maps slug -> short label; when None, SHORT_LABELS is used
    (falling back to the full name for unknown slugs). style is "sketch"
    (hand-drawn double frame, parchment label plates), "minimal" (single
    frame, filled node dots, small plates) or "plain" (no frame, thin
    lines, bare labels beside nodes). palette selects the _TOD_PALETTE
    entry the artwork uses; when None it follows time_of_day. The panel's
    time-of-day caption always uses time_of_day, so a fixed house palette
    (MAP_PALETTE) never makes the caption lie about the game clock.

    Labels get opaque parchment plates in the sketch/minimal styles —
    cairosvg ignores paint-order on text, so a stroke halo would not
    survive rasterization.
    """
    if style not in ("sketch", "minimal", "plain"):
        raise ValueError(f"unknown map style: {style!r}")
    if player_slug not in places:
        raise ValueError(f"player place {player_slug!r} not in places")
    pal = palette if palette is not None else time_of_day
    base, accent, ink = _TOD_PALETTE.get(pal, _TOD_PALETTE["morning"])
    positions = layout_map(places, edges, size)
    lab = (labels if labels is not None
           else {s: SHORT_LABELS.get(s, n) for s, n in places.items()})

    s = []
    A = s.append
    A(f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
      f'viewBox="0 0 {size} {size}">')
    A(f'<rect x="0" y="0" width="{size}" height="{size}" rx="{size * 0.05:.0f}" '
      f'fill="{_rgb(base)}"/>')

    jw = size * 0.009
    if style in ("sketch", "minimal"):
        # hand-drawn frame (double for sketch, single for minimal)
        fw = 4 if style == "sketch" else 3
        m = size * 0.03
        for (p0, p1, sd) in (((m, m), (size - m, m), "frame:n"),
                             ((size - m, m), (size - m, size - m), "frame:e"),
                             ((size - m, size - m), (m, size - m), "frame:s"),
                             ((m, size - m), (m, m), "frame:w")):
            for d in _sketch_path(p0, p1, sd, size, jitter=jw):
                A(f'<path d="{d}" stroke="{_rgb(ink)}" stroke-width="{fw}" '
                  f'fill="none" stroke-linecap="round"/>')
        if style == "sketch":
            m2 = size * 0.045
            for (p0, p1, sd) in (((m2, m2), (size - m2, m2), "frame2:n"),
                                 ((size - m2, m2), (size - m2, size - m2), "frame2:e"),
                                 ((size - m2, size - m2), (m2, size - m2), "frame2:s"),
                                 ((m2, size - m2), (m2, m2), "frame2:w")):
                for d in _sketch_path(p0, p1, sd, size, jitter=jw):
                    A(f'<path d="{d}" stroke="{_rgb(accent)}" stroke-width="2" '
                      f'fill="none" stroke-linecap="round"/>')
    # plain: no frame at all

    # edges under nodes
    for a, b in edges:
        key = f"edge:{min(a, b)}:{max(a, b)}"
        if style == "plain":
            x0, y0 = positions[a]
            x1, y1 = positions[b]
            A(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
              f'stroke="{_rgb(ink)}" stroke-width="2"/>')
        else:
            passes = 2 if style == "sketch" else 1
            ew = 3 if style == "sketch" else 2
            for d in _sketch_path(positions[a], positions[b], key, size,
                                  passes=passes, jitter=jw):
                A(f'<path d="{d}" stroke="{_rgb(ink)}" stroke-width="{ew}" '
                  f'fill="none" stroke-linecap="round"/>')

    # nodes + labels (short labels; parchment plates in sketch/minimal,
    # bare labels beside nodes in plain)
    node_r = size * 0.035
    dr = node_r if style == "sketch" else node_r * (0.5 if style == "minimal" else 0.32)
    label_px = max(20, size // 34)
    for slug, name in places.items():
        x, y = positions[slug]
        text = lab[slug]
        if style == "sketch":
            d = _sketch_circle_path((x, y), node_r, f"node:{slug}")
            A(f'<path d="{d}" stroke="{_rgb(ink)}" stroke-width="3" fill="none" '
              f'stroke-linecap="round"/>')
        else:
            A(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{dr:.1f}" '
              f'fill="{_rgb(ink)}"/>')
        if style == "plain":
            ly = y + label_px * 0.35
            A(f'<text x="{x + dr + 8:.1f}" y="{ly:.1f}" '
              f'text-anchor="start" font-family="\'DejaVu Serif\',serif" '
              f'font-size="{label_px}" fill="{_rgb(ink)}">{_esc(text)}</text>')
        else:
            lines = _wrap(text, width=max(10, size // 22))
            pad = 6 if style == "sketch" else 4
            est_w = max(len(ln) for ln in lines) * label_px * 0.60
            est_h = len(lines) * label_px * 1.18
            plate_y = y + dr + 6
            A(f'<rect x="{x - est_w / 2 - pad:.1f}" y="{plate_y - label_px * 0.55:.1f}" '
              f'width="{est_w + pad * 2:.1f}" height="{est_h + 4:.1f}" rx="6" '
              f'fill="{_rgb(base)}" fill-opacity="0.94"/>')
            for i, ln in enumerate(lines):
                ly = plate_y + label_px * 0.35 + i * label_px * 1.15
                A(f'<text x="{x:.1f}" y="{ly:.1f}" '
                  f'text-anchor="middle" font-family="\'DejaVu Serif\',serif" '
                  f'font-size="{label_px}" fill="{_rgb(ink)}">{_esc(ln)}</text>')

    # player pin: filled red dot + YOU
    px, py = positions[player_slug]
    pin_r = node_r * 0.55
    pin_px = max(16, size // 46)
    A(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{pin_r:.1f}" fill="#b22222" '
      f'stroke="#5a1010" stroke-width="2"/>')
    A(f'<text x="{px:.1f}" y="{py - dr - 6:.1f}" text-anchor="middle" '
      f'font-family="\'DejaVu Serif\',serif" font-size="{pin_px}" '
      f'font-weight="bold" fill="#b22222">YOU</text>')

    # time-of-day caption
    cap_px = max(18, size // 40)
    A(f'<text x="{size * 0.07:.1f}" y="{size * 0.055 + cap_px:.1f}" '
      f'text-anchor="start" font-family="\'DejaVu Serif\',serif" '
      f'font-size="{cap_px}" letter-spacing="2" fill="{_rgb(accent)}">'
      f'{time_of_day.upper()}</text>')
    A('</svg>')
    return "\n".join(s)


def rasterize(svg_text, px):
    """SVG -> PNG bytes at the overlay's native size. cairosvg is
    imported lazily so the server boots without it (deploy note: needs
    cairosvg + system cairo on free-micro-1)."""
    try:
        import cairosvg
    except ImportError as e:
        raise ImageError(f"cairosvg is not installed: {e}") from e
    return cairosvg.svg2png(bytestring=svg_text.encode("utf-8"),
                            output_width=px, output_height=px)


def rasterize_scaled(svg_text, render_px, out_px):
    """Render the SVG at its full design size, then LANCZOS-downscale to
    the shipped overlay size. Neil's 2026-10-04 direction: the downscale
    is what keeps the cairosvg text crisp — rendering at the small size
    directly makes the text a jumble (supersedes the 2026-10-02
    never-downscale rule)."""
    from PIL import Image
    png = rasterize(svg_text, render_px)
    img = Image.open(io.BytesIO(png))
    if img.size == (out_px, out_px):
        return png
    img = img.resize((out_px, out_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def composite_scene_map(scene_jpeg_or_png, map_png, overlay_px=OVERLAY_PX,
                        margin=OVERLAY_MARGIN_PX, quality=80):
    """Paste the rasterized map onto the scene, bottom-left, with a soft
    drop shadow. Returns (jpeg_bytes, overlay_box)."""
    from PIL import Image, ImageDraw, ImageFilter
    scene = Image.open(io.BytesIO(scene_jpeg_or_png)).convert("RGB")
    W, H = scene.size
    map_img = Image.open(io.BytesIO(map_png)).convert("RGBA")
    if map_img.size != (overlay_px, overlay_px):
        map_img = map_img.resize((overlay_px, overlay_px), Image.LANCZOS)

    shadow = Image.new("RGBA", (overlay_px + 40, overlay_px + 40), (0, 0, 0, 0))
    dr = ImageDraw.Draw(shadow)
    dr.rounded_rectangle([20, 20, 20 + overlay_px, 20 + overlay_px],
                         radius=int(overlay_px * 0.05), fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))

    x, y = margin, H - margin - overlay_px
    scene.paste(shadow, (x - 20 + 7, y - 20 + 10), shadow)
    scene.paste(map_img, (x, y), map_img)

    buf = io.BytesIO()
    scene.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), (x, y, x + overlay_px, y + overlay_px)


class ImageError(Exception):
    """Raised when map SVG rasterization is unavailable. Kept local so
    map_svg imports without images.py; images.py catches and rewrites it
    into its own ImageError (same message) for the caller contract."""


# ---------------------------------------------------------------------------
# Selftest — promotion parity: the server build must reproduce the proof
# ---------------------------------------------------------------------------
def _check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name,
          ("— " + detail) if detail and not cond else "")
    if not cond:
        raise SystemExit(f"selftest failed: {name}")


def _proof_state(games_dir, guid):
    """Seeded game state identical to prototype/scene_map_overlay_demo's
    main(): turn 1, game clock 07:00 (morning), trail-down discovered,
    player at trail-down."""
    import sqlite3 as _sqlite3
    from .schema import SCHEMA
    from .seed import seed
    from .turn_loop import filtered_view
    from .images import time_of_day_word, SCENARIO_EDGES

    db = _sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
    db.row_factory = _sqlite3.Row
    db.executescript(SCHEMA)
    seed(db, guid, "player@example.com")
    db.execute("UPDATE games SET turn_no=1, game_clock_min=60 WHERE guid=?",
               (guid,))
    db.execute("UPDATE places SET discovered=1, last_visited_turn=1 "
               "WHERE slug='trail-down'")
    db.execute("UPDATE actors SET location_slug='trail-down' WHERE is_player=1")
    db.commit()
    view = filtered_view(db, guid)
    db.close()
    tod = time_of_day_word(60)
    player_loc = view["actors"]["player"]["location_slug"]
    discovered = {s: p["name"] for s, p in view["places"].items()
                  if p["discovered"]}
    edges = [(a, b) for a, b in SCENARIO_EDGES
             if a in discovered and b in discovered]
    return view, tod, player_loc, discovered, edges


def selftest():
    import sqlite3 as _sqlite3
    import tempfile
    import xml.etree.ElementTree as ET
    from PIL import Image

    HIDDEN = os.path.expanduser(
        "~/workspace/goals/above-the-fog-line-game-project/hidden_files")
    # Full-size spec (Neil's 2026-10-04 direction, supersedes the 2026-10-03
    # half-size verdict): 1024 scene, map rendered at its 340 design size
    # and downscaled to 170. Written by hidden_files/gen_map_proofs_20261004.py.
    PROOF_SVG = os.path.join(HIDDEN, "map_svg_proof_20261004.svg")
    PROOF_JPG = os.path.join(HIDDEN, "composite_proof_20261004.jpg")
    SCENE_SRC = os.path.join(HIDDEN, "composite_proof_scene_20261004.png")

    games_dir = tempfile.mkdtemp(prefix="atfl-mapsvg-")
    guid = "mapsvg-selftest"
    view, tod, player_loc, discovered, edges = _proof_state(games_dir, guid)

    # -- svg renderer parity with the shipped proof --
    svg1 = render_map_svg(discovered, edges, player_loc, tod, size=OVERLAY_PX)
    svg2 = render_map_svg(discovered, edges, player_loc, tod, size=OVERLAY_PX)
    _check("svg deterministic (same bytes twice)", svg1 == svg2)
    if os.path.exists(PROOF_SVG):
        with open(PROOF_SVG) as f:
            proof_svg = f.read()
        _check("server svg byte-identical to the shipped proof svg",
               svg1 == proof_svg,
               f"server {len(svg1)}B vs proof {len(proof_svg)}B")
    else:
        # Proof fixtures live on the operator workstation only
        # (hidden_files, not the repo, and the path is keyed to that
        # host's HOME). On any other host this check skips instead of
        # dying — byte-parity is a workstation control, not a server
        # invariant (caught 2026-10-04: died on free-micro-1 where
        # HOME=/srv/atfl).
        _check("server svg byte-identical to the shipped proof svg "
               "(SKIP: no proof fixture on this host)", True)

    # -- style matrix: all three styles render, differ, and are deterministic --
    styled = {}
    for st in ("sketch", "minimal", "plain"):
        a = render_map_svg(discovered, edges, player_loc, tod,
                           size=OVERLAY_PX, style=st)
        b = render_map_svg(discovered, edges, player_loc, tod,
                           size=OVERLAY_PX, style=st)
        _check(f"style {st} deterministic", a == b)
        _check(f"style {st} parses as xml", ET.fromstring(a) is not None)
        styled[st] = a
    _check("the three styles differ pairwise",
           len(set(styled.values())) == 3)
    _check("plain uses straight lines, sketch does not",
           "<line" in styled["plain"] and "<line" not in styled["sketch"])
    try:
        render_map_svg(discovered, edges, player_loc, tod, style="nope")
        _check("unknown style raises", False)
    except ValueError:
        _check("unknown style raises", True)

    # -- secrecy: the svg names nothing outside the filtered state --
    root = ET.fromstring(svg1)
    texts = [(t.text or "") for t in root.iter() if t.tag.endswith("text")]
    allowed = set()
    for slug, name in discovered.items():
        allowed.update(_wrap(SHORT_LABELS.get(slug, name),
                             width=max(10, OVERLAY_PX // 22)))
    allowed |= {"YOU", tod.upper()}
    unknowns = [t for t in texts if t not in allowed]
    _check("svg names nothing outside filtered state", not unknowns,
           str(unknowns[:3]))
    # -- and the long seed names must NOT appear on the map --
    long_names = [n for s, n in discovered.items()
                  if SHORT_LABELS.get(s, n) != n]
    _check("long seed names are not used as map labels",
           not any(n in svg1 for n in long_names),
           str(long_names[:2]))

    # -- rasterize_scaled: full-size render -> downscaled overlay --
    map_png = rasterize_scaled(svg1, OVERLAY_PX, OVERLAY_V2_PX)
    img = Image.open(io.BytesIO(map_png))
    _check("downscaled map is 170x170", img.size == (OVERLAY_V2_PX, OVERLAY_V2_PX),
           str(img.size))
    full_png = rasterize(svg1, OVERLAY_PX)
    _check("full-size raster is 340x340",
           Image.open(io.BytesIO(full_png)).size == (OVERLAY_PX, OVERLAY_PX))

    # -- composite geometry: 1024 scene, 170 overlay, 14 margin --
    scene = Image.new("RGB", (1024, 1024), (120, 140, 120))
    sbuf = io.BytesIO()
    scene.save(sbuf, "PNG")
    jpeg, box = composite_scene_map(sbuf.getvalue(), map_png,
                                    OVERLAY_V2_PX, OVERLAY_V2_MARGIN_PX)
    out = Image.open(io.BytesIO(jpeg))
    _check("composite jpeg is 1024x1024 (Neil 2026-10-04)",
           out.size == (1024, 1024), str(out.size))
    _check("overlay box bottom-left at margin",
           box == (OVERLAY_V2_MARGIN_PX, 1024 - OVERLAY_V2_MARGIN_PX - OVERLAY_V2_PX,
                   OVERLAY_V2_MARGIN_PX + OVERLAY_V2_PX,
                   1024 - OVERLAY_V2_MARGIN_PX),
           str(box))

    # -- full v2 builder end to end with a stub provider --
    from .images import build_turn_composite_v2

    class Stub:
        def generate_scene(self, prompt, *, size=1024):
            _check("scene prompt mentions morning", "morning" in prompt.lower())
            _check("scene generated at 1024", size == 1024)
            scene = Image.new("RGB", (size, size), (120, 140, 120))
            b = io.BytesIO()
            scene.save(b, "PNG")
            return b.getvalue()

    res = build_turn_composite_v2(games_dir, guid, 1, Stub())
    # Full size since Neil's 2026-10-04 verdict (scene stays 1024).
    _check("v2 jpeg is 1024x1024 (full size, Neil 2026-10-04)",
           Image.open(io.BytesIO(res["jpeg"])).size == (1024, 1024))
    _check("v2 overlay_px is 170", res["overlay_px"] == OVERLAY_V2_PX,
           str(res["overlay_px"]))
    _check("v2 returns the builder contract keys",
           set(res) >= {"jpeg", "scene_prompt", "time_of_day", "overlay_px",
                        "overlay_box", "prompt_hash", "sent_at"})
    _check("v2 time_of_day from game clock", res["time_of_day"] == "morning")
    for kind, ext in (("scene_v2", "jpg"), ("map_svg", "svg"),
                      ("composite_v2", "jpg")):
        p = os.path.join(games_dir, "assets", guid, f"turn-1-{kind}.{ext}")
        _check(f"v2 asset file persisted: turn-1-{kind}.{ext}",
               os.path.exists(p))
    db = _sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
    rows = db.execute(
        "SELECT kind FROM assets WHERE turn_created=1").fetchall()
    db.close()
    _check("v2 provenance rows in assets table",
           {r[0] for r in rows} == {"scene_v2", "map_svg", "composite_v2"},
           str([r[0] for r in rows]))

    # -- proof reproduction: same scene + same state through the server
    #    pipeline must byte-match the shipped proof jpeg --
    if os.path.exists(SCENE_SRC) and os.path.exists(PROOF_JPG):
        with open(SCENE_SRC, "rb") as f:
            proof_scene = f.read()

        class ProofScene:
            def generate_scene(self, prompt, *, size=1024):
                return proof_scene

        res2 = build_turn_composite_v2(games_dir, guid, 2, ProofScene())
        with open(PROOF_JPG, "rb") as f:
            proof_jpeg = f.read()
        _check("v2 reproduces the shipped proof jpeg byte-for-byte",
               res2["jpeg"] == proof_jpeg,
               f"server {len(res2['jpeg'])}B vs proof {len(proof_jpeg)}B")
    else:
        # Same workstation-only coupling as the svg parity check above.
        _check("v2 reproduces the shipped proof jpeg byte-for-byte "
               "(SKIP: no proof fixture on this host)", True)
    shutil.rmtree(games_dir, ignore_errors=True)
    print("\nmap_svg selftest: all checks green.")


if __name__ == "__main__":
    selftest()
