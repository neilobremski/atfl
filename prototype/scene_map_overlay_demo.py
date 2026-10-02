"""Prototype: Neil's single-image turn render (2026-10-02 decision).

Recipe, verbatim from his digest reply: one image that is the scene, with the
map drawn as SVG, rendered into the raster image in the bottom-left corner, and
the whole thing compressed to a JPEG. Selfie cut until scene+map look right.
One render per player per day.

This demo proves the recipe end to end against REAL seeded game state:
  DB (seeded) -> filtered_view -> discovered/edges/player/tod
     -> render_map_svg()            (SVG, mirrors server/map_panel style:
                                     double frame, sketch strokes, nodes,
                                     labels with parchment halo, YOU pin,
                                     time-of-day caption)
     -> cairosvg rasterization      (SVG -> raster PNG, bottom-left overlay)
     -> PIL composite + shadow      (onto the real 1024x1024 test scene)
     -> JPEG q=80                   (single file for the email)

Geometry contract is unchanged: layout_map() and the palette come from
server/map_panel, so the SVG and the old PIL panel agree byte-for-byte on
positions; only the drawing backend changes (PIL -> SVG -> cairo).

Run: /tmp/svgvenv/bin/python3 prototype/scene_map_overlay_demo.py
     (cairosvg lives in that throwaway venv; the deploy target will need the
     same library installed for the images.py rewrite, e.g. pip in its venv)
Exit 0 = all checks green. Artifacts land in the goal hidden_files/ dir so
Neil can eyeball them: scene_map_overlay_proof_20261002.jpg + .svg.
"""

from __future__ import annotations

import io
import math
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.map_panel import _TOD_PALETTE, _seed, layout_map  # noqa: E402
from server.schema import SCHEMA  # noqa: E402
from server.seed import seed  # noqa: E402
from server.images import time_of_day_word, SCENARIO_EDGES  # noqa: E402
from server.turn_loop import filtered_view  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HIDDEN = os.path.expanduser(
    "~/workspace/goals/above-the-fog-line-game-project/hidden_files")
SCENE_SRC = os.path.join(HIDDEN, "hf_e2e_scene.png")  # real test scene, 1024x1024
OUT_JPG = os.path.join(HIDDEN, "scene_map_overlay_proof_20261002.jpg")
OUT_SVG = os.path.join(HIDDEN, "scene_map_overlay_proof_20261002.svg")

OVERLAY_PX = 340   # map overlay square, px
MARGIN_PX = 28     # from left and bottom edges
JPEG_QUALITY = 80

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name,
          ("— " + detail) if detail and not cond else "")
    if not cond:
        FAILURES.append(name + (" — " + detail if detail else ""))


# ---------------------------------------------------------------- sketch paths
def _sketch_path(p0, p1, seed_text, size, passes=2, jitter=9):
    """Midpoint-displaced polyline, 2 passes — mirrors _sketch_line's seeds."""
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


# ---------------------------------------------------------------- SVG renderer
def render_map_svg(places, edges, player_slug, time_of_day="morning", size=1024):
    """Map as an SVG string. Same inputs/contract as render_map.

    Only discovered places are ever drawn (the caller passes the filtered
    set). Deterministic: seeded randomness only.
    """
    if player_slug not in places:
        raise ValueError(f"player place {player_slug!r} not in places")
    base, accent, ink = _TOD_PALETTE.get(time_of_day, _TOD_PALETTE["morning"])
    positions = layout_map(places, edges, size)

    s = []
    A = s.append
    A(f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
      f'viewBox="0 0 {size} {size}">')
    A(f'<rect x="0" y="0" width="{size}" height="{size}" rx="{size * 0.05:.0f}" '
      f'fill="{_rgb(base)}"/>')

    # double hand-drawn frame
    m = size * 0.03
    for d in _sketch_path((m, m), (size - m, m), "frame:n", size, jitter=size * 0.009):
        A(f'<path d="{d}" stroke="{_rgb(ink)}" stroke-width="4" fill="none" '
          f'stroke-linecap="round"/>')
    for d in _sketch_path((size - m, m), (size - m, size - m), "frame:e", size, jitter=size * 0.009):
        A(f'<path d="{d}" stroke="{_rgb(ink)}" stroke-width="4" fill="none" '
          f'stroke-linecap="round"/>')
    for d in _sketch_path((size - m, size - m), (m, size - m), "frame:s", size, jitter=size * 0.009):
        A(f'<path d="{d}" stroke="{_rgb(ink)}" stroke-width="4" fill="none" '
          f'stroke-linecap="round"/>')
    for d in _sketch_path((m, size - m), (m, m), "frame:w", size, jitter=size * 0.009):
        A(f'<path d="{d}" stroke="{_rgb(ink)}" stroke-width="4" fill="none" '
          f'stroke-linecap="round"/>')
    m2 = size * 0.045
    for (p0, p1, sd) in (((m2, m2), (size - m2, m2), "frame2:n"),
                         ((size - m2, m2), (size - m2, size - m2), "frame2:e"),
                         ((size - m2, size - m2), (m2, size - m2), "frame2:s"),
                         ((m2, size - m2), (m2, m2), "frame2:w")):
        for d in _sketch_path(p0, p1, sd, size, jitter=size * 0.009):
            A(f'<path d="{d}" stroke="{_rgb(accent)}" stroke-width="2" fill="none" '
              f'stroke-linecap="round"/>')

    # edges under nodes
    for a, b in edges:
        key = f"edge:{min(a, b)}:{max(a, b)}"
        for d in _sketch_path(positions[a], positions[b], key, size, jitter=size * 0.009):
            A(f'<path d="{d}" stroke="{_rgb(ink)}" stroke-width="3" fill="none" '
              f'stroke-linecap="round"/>')

    # nodes + labels. Labels get an opaque parchment plate behind them
    # (cairosvg ignores paint-order, so the PIL halo-stroke trick does not
    # survive rasterization — the plate keeps edges from striking through).
    node_r = size * 0.035
    label_px = max(20, size // 34)
    for slug, name in places.items():
        x, y = positions[slug]
        d = _sketch_circle_path((x, y), node_r, f"node:{slug}")
        A(f'<path d="{d}" stroke="{_rgb(ink)}" stroke-width="3" fill="none" '
          f'stroke-linecap="round"/>')
        lines = _wrap(name, width=max(10, size // 22))
        est_w = max(len(ln) for ln in lines) * label_px * 0.60
        est_h = len(lines) * label_px * 1.18
        plate_y = y + node_r + 6
        A(f'<rect x="{x - est_w / 2 - 6:.1f}" y="{plate_y - label_px * 0.55:.1f}" '
          f'width="{est_w + 12:.1f}" height="{est_h + 4:.1f}" rx="6" '
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
    A(f'<text x="{px:.1f}" y="{py - node_r - 6:.1f}" text-anchor="middle" '
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


# ---------------------------------------------------------------- pipeline
def rasterize(svg_text, px):
    import cairosvg
    return cairosvg.svg2png(bytestring=svg_text.encode("utf-8"),
                            output_width=px, output_height=px)


def composite_scene_map(scene_jpeg_or_png, map_png, overlay_px, margin, quality):
    from PIL import Image, ImageDraw, ImageFilter
    scene = Image.open(io.BytesIO(scene_jpeg_or_png)).convert("RGB")
    W, H = scene.size
    map_img = Image.open(io.BytesIO(map_png)).convert("RGBA")
    if map_img.size != (overlay_px, overlay_px):
        map_img = map_img.resize((overlay_px, overlay_px), Image.LANCZOS)

    # soft drop shadow
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


def main():
    # ---- real seeded game state (same turn-1 reality as turn_composite_e2e)
    games_dir = tempfile.mkdtemp(prefix="atfl-svgmap-")
    guid = "svgmap-proof-guid"
    db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    seed(db, guid, "player@example.com")
    c = db.cursor()
    c.execute("UPDATE games SET turn_no=1, game_clock_min=60 WHERE guid=?", (guid,))
    c.execute("UPDATE places SET discovered=1, last_visited_turn=1 WHERE slug='trail-down'")
    c.execute("UPDATE actors SET location_slug='trail-down' WHERE is_player=1")
    db.commit()
    view = filtered_view(db, guid)
    tod = time_of_day_word(60)
    player_loc = view["actors"]["player"]["location_slug"]
    discovered = {s: p["name"] for s, p in view["places"].items() if p["discovered"]}
    edges = [(a, b) for a, b in SCENARIO_EDGES if a in discovered and b in discovered]
    db.close()

    # ---- draw the map at the overlay's NATIVE size so type is designed
    # for 340px, not downscaled from 1024
    svg1 = render_map_svg(discovered, edges, player_loc, tod, size=OVERLAY_PX)
    svg2 = render_map_svg(discovered, edges, player_loc, tod, size=OVERLAY_PX)
    check("svg deterministic (same bytes twice)", svg1 == svg2)
    root = ET.fromstring(svg1)
    check("svg is well-formed xml", root.tag.endswith("svg"))
    svg_texts = " ".join((t.text or "") for t in root.iter()
                        if t.tag.endswith("text"))
    for name in discovered.values():
        check(f"discovered place named in svg: {name!r}", name in svg_texts)
    check("player pin labelled", "YOU" in svg_texts)
    check("caption shows time of day", tod.upper() in svg_texts)
    # no hidden geography: every text line is part of a discovered name,
    # the YOU pin, or the caption (labels wrap, so compare per-line)
    allowed = set()
    for name in discovered.values():
        allowed.update(_wrap(name, width=max(10, OVERLAY_PX // 22)))
    allowed |= {"YOU", tod.upper()}
    unknowns = [(t.text or "") for t in root.iter() if t.tag.endswith("text")
                and (t.text or "") not in allowed]
    check("svg names nothing outside filtered state", not unknowns, str(unknowns[:3]))
    open(OUT_SVG, "w").write(svg1)

    # ---- rasterize + composite
    map_png = rasterize(svg1, OVERLAY_PX)
    from PIL import Image
    over = Image.open(io.BytesIO(map_png))
    check("map raster is overlay size", over.size == (OVERLAY_PX, OVERLAY_PX),
          str(over.size))

    with open(SCENE_SRC, "rb") as f:
        scene_bytes = f.read()
    jpeg, box = composite_scene_map(scene_bytes, map_png, OVERLAY_PX, MARGIN_PX,
                                    JPEG_QUALITY)
    W = H = 1024
    check("overlay sits bottom-left (x within left half)",
          box[0] + (box[2] - box[0]) <= W // 2, str(box))
    check("overlay sits bottom-left (y within bottom half)", box[1] >= H // 2,
          str(box))
    check("overlay clears the margin from edges",
          box[0] >= MARGIN_PX and box[3] <= H - MARGIN_PX, str(box))

    final = Image.open(io.BytesIO(jpeg))
    check("output is a single 1024x1024 jpeg", final.size == (1024, 1024) and
          final.mode == "RGB", f"{final.size} {final.mode}")
    check("output jpeg under 600KB", len(jpeg) < 600 * 1024,
          f"{len(jpeg) // 1024}KB")
    with open(OUT_JPG, "wb") as f:
        f.write(jpeg)

    shutil.rmtree(games_dir, ignore_errors=True)

    print(f"\nartifacts: {OUT_JPG} ({len(jpeg) // 1024}KB), {OUT_SVG}")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURES")
        sys.exit(1)
    print("\nALL GREEN")


if __name__ == "__main__":
    main()
