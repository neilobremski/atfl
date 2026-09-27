"""Image pipeline — Phase 3 (research/phase3-image-pipeline.md).

DESIGN.md §5.2: text/plain always carries the complete turn; the
three-part composite (scene, map, selfie) is additive and attaches as
one image to the turn email. Failure policy (§2.6 extended): a failed
image generation never fails the turn — text sends anyway.

Mirrors the Phase 2 stub-then-real pattern:
  - `StubImageProvider` — deterministic placeholder panels, offline dev
    and tests; no key, no network.
  - `build_provider(mode, api_key)` — mode "real" raises until the key
    exists (loud refusal, same as config.py's ATFL_GAME_ADDRESS rule);
    the Gemini REST implementation lands when OQ#6 closes.

Secrecy shape: prompt builders take ONLY the filtered world view
(turn_loop.filtered_view — physical state, never hidden_traits, never
the plot concept). The map panel is code-drawn from DB truth
(map_panel.py) — a generator would invent geography. Prompts and the
composite bytes are recorded in the `assets` table, so a weird image is
reproducible from the game archive.
"""
import hashlib
import io
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont

from .map_panel import render_map
from .seed import EDGES as SCENARIO_EDGES
from .turn_loop import filtered_view

log = logging.getLogger("atfl.images")

ASSETS_DIRNAME = "assets"  # under games_dir: assets/<guid>/...
COMPOSITE_KIND = "composite"
CHARACTER_REF_KIND = "character_ref"


class ImageError(Exception):
    """Image generation failed. Callers (mailer) log it and send the
    text-only turn — never raised into the turn pipeline."""


class ImageProvider(Protocol):
    """What the turn loop needs from an image backend. Panel size is the
    square edge in px; returns JPEG bytes. character_ref is the
    canonical portrait bytes (selfie continuity), opaque to the caller."""

    def generate_scene(self, prompt: str, *, size: int = 1024) -> bytes: ...
    def generate_selfie(self, prompt: str, *, character_ref: bytes,
                        size: int = 1024) -> bytes: ...


# -- time of day -----------------------------------------------------------

# Game clock starts at 07:00 local (morning); one turn = 60 game-minutes
# (DESIGN §3). The word drives both the generator prompt and the map
# panel's palette tint — cheap, deterministic, visibly correct.
def time_of_day_word(game_clock_min: int) -> str:
    """morning/midday/dusk/night from the game clock (minutes since 07:00)."""
    hour = (7 + game_clock_min // 60) % 24
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 16:
        return "midday"
    if 16 <= hour < 20:
        return "dusk"
    return "night"


# -- prompt builders (filtered state only) ---------------------------------

def _visibles(view, player_loc):
    """Description fragments for what the player can see where they
    stand: place description, nearby objects, own physical state.
    Everything here is physical/obvious state — hidden_traits never
    leaves the DB layer, so it cannot reach a prompt."""
    loc = view["places"].get(player_loc, {})
    bits = [loc.get("description") or ""]
    for o in view["objects"].values():
        if o["holder"] == f"place:{player_loc}" or o["holder"].startswith("actor:player"):
            bits.append(f"{o['name']}: {o['description']}")
    return "; ".join(b for b in bits if b)


def scene_prompt(view, time_of_day: str) -> str:
    """Scene panel prompt: the current location as it looks right now."""
    player = view["actors"].get("player", {})
    loc = player.get("location_slug", "trailhead")
    place = view["places"].get(loc, {})
    body = _visibles(view, loc)
    return (
        "Cinematic still, quiet eerie calm, photographic: "
        f"{place.get('name', 'somewhere')}, {time_of_day} light. "
        f"Visible now: {body}. "
        "No people other than none; no text, no captions, no watermark."
    )


def selfie_prompt(view, time_of_day: str) -> str:
    """Selfie panel prompt: the player's actor as they look right now
    (pose, carried items, condition)."""
    player = view["actors"].get("player", {})
    phys = json.loads(player.get("physical_state") or "{}")
    inv = json.loads(player.get("inventory") or "{}")
    carried = [i for hand in inv.get("hands", []) for i in [hand] if i]
    condition = []
    if phys.get("wetness", 0) > 0.3:
        condition.append("damp")
    if phys.get("hp", 1.0) < 1.0:
        condition.append("worn, a little hurt")
    return (
        "First-person selfie, quiet eerie calm, photographic: a lone hiker "
        f"on a mountain trail, {time_of_day} light, pose: "
        f"{phys.get('pose', 'standing')}, "
        f"{'carrying ' + ', '.join(carried) if carried else 'hands empty'}, "
        f"{'looking ' + ', '.join(condition) if condition else 'steady'}. "
        "Face partly in shadow; no text, no captions, no watermark."
    )


# -- stub provider ----------------------------------------------------------

def _placeholder_panel(label: str, prompt: str, size: int,
                       bg=(38, 44, 58), fg=(196, 198, 210)) -> bytes:
    """Deterministic placeholder: dark panel with the label and a short
    prompt hash so different prompts give different (but stable) panels.
    Identical bytes for identical input — tests can pin them."""
    img = Image.new("RGB", (size, size), bg)
    d = ImageDraw.Draw(img)
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    # deterministic grain from the prompt hash
    rnd_seed = int(digest, 16)
    for i in range(size // 8):
        x = (rnd_seed * (i + 7)) % size
        y = (rnd_seed * (i + 13) * 31) % size
        d.ellipse([x, y, x + 3, y + 3], fill=(bg[0] + 14, bg[1] + 14, bg[2] + 18))
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size // 22)
    except OSError:
        font = ImageFont.load_default()
    d.text((size // 12, size // 3), label, fill=fg, font=font)
    d.text((size // 12, size // 3 + size // 14), f"stub · {digest}",
           fill=(fg[0] // 2, fg[1] // 2, fg[2] // 2), font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


class StubImageProvider:
    """Deterministic offline provider: fixed placeholder panels. Never
    touches a key or the network; identical prompt -> identical bytes."""

    def generate_scene(self, prompt: str, *, size: int = 1024) -> bytes:
        return _placeholder_panel("SCENE", prompt, size,
                                  bg=(34, 40, 54))

    def generate_selfie(self, prompt: str, *, character_ref: bytes,
                        size: int = 1024) -> bytes:
        # Stub ignores the ref (no model to honor it) but stays
        # deterministic on the prompt; the real provider passes it.
        return _placeholder_panel("SELFIE", prompt, size,
                                  bg=(56, 44, 38))


def build_provider(mode: str, api_key: str | None = None) -> ImageProvider:
    """'off'/'stub'/'real' from config. 'real' refuses loudly until the
    API key exists — no silent stub generation against a live account."""
    mode = (mode or "off").strip().lower()
    if mode == "off":
        return None
    if mode == "stub":
        return StubImageProvider()
    if mode == "real":
        if not api_key:
            raise ImageError(
                "ATFL_IMAGES=real needs the image API key provisioned "
                "(open question #6); refusing to run without it.")
        raise ImageError(
            "real provider not wired yet — the key exists but the Gemini "
            "REST adapter lands in the next session; stub mode keeps dev "
            "moving.")
    raise ImageError(f"ATFL_IMAGES must be 'off', 'stub' or 'real', got {mode!r}")


# -- character reference store ----------------------------------------------

def _assets_dir(games_dir, guid):
    return os.path.join(games_dir, ASSETS_DIRNAME, guid)


def get_character_ref(games_dir, guid) -> bytes | None:
    """The canonical portrait bytes for this game (the first selfie ever
    generated), or None when no selfie exists yet. Passed as
    character_ref every turn — the selfie's continuity anchor."""
    path = os.path.join(_assets_dir(games_dir, guid), "character-ref.jpg")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _store_asset(games_dir, guid, turn_no, kind, data, prompt):
    """Persist panel bytes under assets/<guid>/ and file the provenance
    row in the game's assets table (the file IS the archive — the games
    dir is the whole backup unit)."""
    os.makedirs(_assets_dir(games_dir, guid), exist_ok=True)
    fname = "character-ref.jpg" if kind == CHARACTER_REF_KIND \
        else f"turn-{turn_no}-{kind}.jpg"
    path = os.path.join(_assets_dir(games_dir, guid), fname)
    with open(path, "wb") as f:
        f.write(data)
    rel = os.path.relpath(path, games_dir)
    phash = hashlib.sha256(data).hexdigest()[:16]
    db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
    try:
        db.execute(
            "INSERT INTO assets (entity_type,entity_id,kind,path,turn_created,prompt,prompt_hash)"
            " VALUES (?,?,?,?,?,?,?)",
            ("game", 0, kind, rel, turn_no, prompt, phash))
        db.commit()
    finally:
        db.close()
    return rel


# -- composite ----------------------------------------------------------------

PANEL_ORDER = ("scene", "map", "selfie")  # DESIGN §5.2 block order


def stitch_composite(scene: Image.Image, map_panel: Image.Image,
                     selfie: Image.Image, size: int = 1024) -> bytes:
    """Stack the three panels vertically into one JPEG: scene, map,
    selfie — the turn email's image-led order (§5.2). Thin dark bands
    separate the panels."""
    band = 6
    total_h = size * 3 + band * 2
    canvas = Image.new("RGB", (size, total_h), (24, 24, 28))
    y = 0
    for panel in (scene, map_panel, selfie):
        if panel.size != (size, size):
            panel = panel.resize((size, size), Image.LANCZOS)
        canvas.paste(panel, (0, y))
        y += size + band
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def build_turn_composite(games_dir, guid, turn_no, provider,
                         size: int = 1024) -> dict:
    """Generate the turn's three-panel composite.

    Steps: prompt from filtered state (+ game-clock time of day) ->
    scene + selfie from the provider, map code-drawn from DB truth ->
    stitch -> persist bytes + provenance rows. First-ever selfie also
    becomes the game's character reference.

    Returns {"jpeg": bytes, "scene_prompt": str, "selfie_prompt": str,
             "time_of_day": str, "character_ref_used": bool,
             "prompt_hash": str}. Raises ImageError on any failure —
    the caller logs it and sends the text-only turn.
    """
    try:
        db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
        db.row_factory = sqlite3.Row
        view = filtered_view(db, guid)
        g = dict(db.execute("SELECT * FROM games WHERE guid=?", (guid,)).fetchone())
        db.close()
    except Exception as e:
        raise ImageError(f"cannot read game state for images: {e}") from e
    if not g:
        raise ImageError(f"no game row for {guid}")

    tod = time_of_day_word(g["game_clock_min"])
    s_prompt, f_prompt = scene_prompt(view, tod), selfie_prompt(view, tod)

    try:
        player_loc = view["actors"]["player"]["location_slug"]
        discovered = {s: p["name"] for s, p in view["places"].items()
                      if p["discovered"]}
        if not discovered:
            raise ImageError("no discovered places — nothing to draw")
        edges = [(a, b) for a, b in SCENARIO_EDGES
                 if a in discovered and b in discovered]
        map_img = render_map(discovered, edges, player_loc, tod, size=size)

        ref = get_character_ref(games_dir, guid)
        scene_jpg = provider.generate_scene(s_prompt, size=size)
        selfie_jpg = provider.generate_selfie(f_prompt, character_ref=ref or b"",
                                              size=size)
    except ImageError:
        raise
    except Exception as e:
        raise ImageError(f"panel generation failed: {e}") from e

    try:
        composite = stitch_composite(
            Image.open(io.BytesIO(scene_jpg)),
            map_img,
            Image.open(io.BytesIO(selfie_jpg)),
            size=size)
    except Exception as e:
        raise ImageError(f"composite stitch failed: {e}") from e

    # Provenance: panels + prompts land in the assets table; the first
    # selfie becomes the canonical character reference.
    prompt_text = f"SCENE: {s_prompt}\nSELFIE: {f_prompt}"
    _store_asset(games_dir, guid, turn_no, "scene", scene_jpg, s_prompt)
    _store_asset(games_dir, guid, turn_no, "selfie", selfie_jpg, f_prompt)
    _store_asset(games_dir, guid, turn_no, COMPOSITE_KIND, composite, prompt_text)
    if ref is None:
        _store_asset(games_dir, guid, turn_no, CHARACTER_REF_KIND,
                     selfie_jpg, f_prompt)

    return {
        "jpeg": composite,
        "scene_prompt": s_prompt,
        "selfie_prompt": f_prompt,
        "time_of_day": tod,
        "character_ref_used": ref is not None,
        "prompt_hash": hashlib.sha256(prompt_text.encode()).hexdigest()[:16],
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
