"""Image pipeline — Phase 3 (research/phase3-image-pipeline.md).

DESIGN.md §5.2: text/plain always carries the complete turn; the
three-part composite (scene, map, selfie) is additive and attaches as
one image to the turn email. Failure policy (§2.6 extended): a failed
image generation never fails the turn — text sends anyway.

Mirrors the Phase 2 stub-then-real pattern:
  - `StubImageProvider` — deterministic placeholder panels, offline dev
    and tests; no key, no network.
  - `GeminiImageProvider` — Gemini generateContent REST adapter, stdlib
    only (urllib), AI Studio key in the `x-goog-api-key` header (never
    in the URL, so it can't leak through logs/proxies), one retry on
    transport/5xx, reference-image support for the selfie.
  - `build_provider(mode, api_key)` — mode "real" raises until the key
    exists (loud refusal, same as config.py's ATFL_GAME_ADDRESS rule).

Secrecy shape: prompt builders take ONLY the filtered world view
(turn_loop.filtered_view — physical state, never hidden_traits, never
the plot concept). The map panel is code-drawn from DB truth
(map_panel.py) — a generator would invent geography. Prompts and the
composite bytes are recorded in the `assets` table, so a weird image is
reproducible from the game archive.
"""
from __future__ import annotations  # py3.9 compat: `X | None` annotations stay lazy on the deploy target (OL9 stock python3.9)
import hashlib
import io
import json
import logging
import os
import sqlite3
import base64
import socket
import urllib.error
import urllib.request
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


def build_provider(mode: str, api_key: str | None = None,
                   hf_token: str | None = None) -> ImageProvider:
    """'off'/'stub'/'real'/'hf' from config. 'real'/'hf' refuse loudly
    until their key exists — no silent stub generation against a live
    account. 'real' is the paid Gemini direction (deprioritized per
    2026-09-29 — Neil ruled out paid image APIs); 'hf' is the no-cost
    HuggingFace Inference path (research/phase3-hf-colab-art.md)."""
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
        return GeminiImageProvider(api_key)
    if mode == "hf":
        if not hf_token:
            raise ImageError(
                "ATFL_IMAGES=hf needs ATFL_HF_TOKEN set — a free "
                "HuggingFace token with the 'inference' scope "
                "(research/phase3-hf-colab-art.md); refusing to run "
                "without it.")
        return HFImageProvider(hf_token)
    raise ImageError(f"ATFL_IMAGES must be 'off', 'stub', 'real' or 'hf', "
                     f"got {mode!r}")


# -- Gemini REST provider --------------------------------------------------------

# Model ID pinned at the research decision (phase3-image-pipeline.md).
# These IDs die fast in this space (gemini-2.5-flash-image shut down
# 2026-10-02); revisit per release — and note the turn loop pins no
# model behavior in any demo, so a swap is a config-layer change.
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
GEMINI_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
                   "models/{model}:generateContent")


def _text_part(text):
    return {"text": text}


def _inline_part(mime_type, data_bytes):
    return {"inlineData": {"mimeType": mime_type,
                           "data": base64.b64encode(data_bytes).decode()}}


def _first_inline_data(response_json):
    """Pull the first returned image part from a generateContent
    response. Returns base64 string or None."""
    for cand in response_json.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return inline["data"]
    return None


def _to_jpeg(raw_bytes):
    """Provider may return PNG — normalize to JPEG bytes so the archive
    and the character-ref store have one format."""
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class GeminiImageProvider:
    """Real backend: Gemini generateContent image generation.

    - stdlib-only (urllib) — no new deploy dependency; deploy on
      free-micro-1 keeps requirements.txt as-is.
    - The AI Studio key travels in the `x-goog-api-key` header, never in
      the URL, so it can't leak through access logs or the sandbox
      proxy's request line.
    - Timeout 60s + one retry on transport failures and 5xx (one spare
      beat, not a loop — a dead provider must degrade to text-only,
      not hang the poll cycle). 429 is NOT retried: images never fail
      a turn, so rate-limiting degrades to text-only immediately.
    - `character_ref` (the stored first selfie JPEG) is sent as a
      reference image with a continuity instruction for generate_selfie.
    - `request_fn(url, headers, body_bytes) -> (status, body_bytes)`
      is injectable so the demo pins the REST contract without network.
    """

    def __init__(self, api_key, *, model=GEMINI_IMAGE_MODEL,
                 timeout=60, request_fn=None):
        if not api_key:
            raise ImageError(
                "GeminiImageProvider needs an API key (open question #6); "
                "refusing to construct without one.")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._request_fn = request_fn or self._urllib_post

    # -- ImageProvider protocol --

    def generate_scene(self, prompt: str, *, size: int = 1024) -> bytes:
        return self._generate([_text_part(prompt)], size=size)

    def generate_selfie(self, prompt: str, *, character_ref: bytes,
                        size: int = 1024) -> bytes:
        parts = []
        if character_ref:
            parts.append(_inline_part("image/jpeg", character_ref))
            prompt = ("Keep the SAME person as in the reference photo "
                      "(same face, same hiker, same look); "
                      + prompt)
        parts.append(_text_part(prompt))
        return self._generate(parts, size=size)

    # -- REST plumbing --

    @staticmethod
    def _image_size_for(panel_px: int) -> str:
        """1K covers our 1024 panels; anything larger goes 2K."""
        return "2K" if panel_px > 1024 else "1K"

    def _request_body(self, parts, *, size: int) -> bytes:
        return json.dumps({
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": "1:1",
                                "imageSize": self._image_size_for(size)},
            },
        }).encode()

    def _urllib_post(self, url, headers, body):
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _generate(self, parts, *, size: int) -> bytes:
        url = GEMINI_ENDPOINT.format(model=self._model)
        headers = {"Content-Type": "application/json",
                   "x-goog-api-key": self._api_key}
        body = self._request_body(parts, size=size)

        last = None
        for attempt in (1, 2):
            try:
                status, raw = self._request_fn(url, headers, body)
            except (urllib.error.URLError, socket.timeout, TimeoutError,
                    OSError) as e:
                last = ImageError(
                    f"gemini transport failed (attempt {attempt}/2): {e}")
                continue  # one retry on transport failure
            if status == 429:
                # Quota/rate limit: do NOT retry — degrade to text-only
                # immediately (failure policy, images.py docstring).
                raise ImageError(
                    "gemini rate-limited (429); no retry this turn — "
                    "text-only turn goes out.")
            if 500 <= status < 600:
                last = ImageError(
                    f"gemini server error {status} (attempt {attempt}/2)")
                continue  # one retry on 5xx
            if status != 200:
                raise ImageError(
                    f"gemini rejected the request ({status}): "
                    f"{raw[:200]!r}")
            try:
                b64 = _first_inline_data(json.loads(raw))
            except (ValueError, KeyError, TypeError) as e:
                raise ImageError(
                    f"gemini response unparseable: {e}") from e
            if not b64:
                raise ImageError(
                    "gemini returned no image part in this response")
            try:
                return _to_jpeg(base64.b64decode(b64))
            except Exception as e:
                raise ImageError(
                    f"gemini image payload not decodable: {e}") from e
        raise last


# -- HuggingFace Inference provider ---------------------------------------------

# Model IDs pinned at the research decision (phase3-hf-colab-art.md).
# Same churn caveat as the Gemini IDs: these move fast; a swap is a
# constructor-arg / config-layer change, and no demo pins model behavior.
# 2026-10-01: the `hf-inference` route is DEAD for these models (410
# "deprecated and no longer supported by provider hf-inference"). The Hub API
# maps FLUX.1-schnell live to nscale/fal-ai/wavespeed. Provider route
# VERIFIED LIVE 2026-10-01 from free-micro-1 (the actual deploy target):
# POST https://router.huggingface.co/nscale/v1/images/generations with
# {"model": ..., "prompt": ..., "response_format": "b64_json"} returned 200
# with {"created": int, "data": [{"b64_json": "<base64 png>"}]} — real
# 1024x1024 PNG bytes (see goal hidden_files/hf_first_test_image.png).
# NOTE: the nscale routes hang from the sandbox egress (verified 2026-10-01),
# so re-verify if the deploy egress ever changes.
HF_SCENE_MODEL = "black-forest-labs/FLUX.1-schnell"
HF_SELFIE_MODEL = "black-forest-labs/FLUX.1-Kontext-dev"
HF_PROVIDER_BASE = ("https://router.huggingface.co/nscale/v1/"
                    "images/generations")

# Kontext-dev repo access on HF requires accepting its license conditions
# (one-time account action on whoever holds the token). If that blocks,
# fall back to generic image-to-image via the scene model or the Colab
# notebook path — see phase3-hf-colab-art.md.


class HFImageProvider:
    """Real backend: HuggingFace Inference, no-cost tier.

    - stdlib-only (urllib) — no new deploy dependency, same as the
      Gemini adapter.
    - The HF token travels in the `Authorization: Bearer` header, never
      in the URL (can't leak through access logs / the sandbox proxy's
      request line).
    - Failure policy (established contract): 60s timeout + one retry on
      transport failures and on 503 (serverless models load on demand —
      the classic "Model is currently loading" 503). 429 is NOT retried:
      images never fail a turn, rate-limiting degrades to text-only
      immediately. Any other non-200 surfaces loudly as ImageError.
    - Scene = text-to-image via HF_SCENE_MODEL. Selfie = the selfie
      model with an instruction-prefixed prompt; plain text-to-image for
      now (Kontext-dev accepts raw prompts). The reference-photo
      instruction-editing shape for subject continuity is NOT yet
      verified on this route, so character_ref is accepted for API
      compatibility but unused — selfie continuity via image-editing
      waits on a verified edits contract.
    - `request_fn(url, headers, body_bytes) -> (status, raw_bytes)` is
      injectable so the demo pins the REST contract without network.
    - REST contract VERIFIED LIVE 2026-10-01 from free-micro-1 (see
      module constants): OpenAI-compatible POST to HF_PROVIDER_BASE,
      {"model", "prompt", "response_format": "b64_json"}; 200 returns
      {"created": int, "data": [{"b64_json": "<png>"}]}. A response that
      doesn't match raises ImageError loudly — no shape guessing.
      Do NOT call it without the token.
    """

    def __init__(self, hf_token, *, scene_model=HF_SCENE_MODEL,
                 selfie_model=HF_SELFIE_MODEL, timeout=60,
                 request_fn=None):
        if not hf_token:
            raise ImageError(
                "HFImageProvider needs a HuggingFace token with the "
                "'inference' scope (ATFL_HF_TOKEN); refusing to construct "
                "without one.")
        self._hf_token = hf_token
        self._scene_model = scene_model
        self._selfie_model = selfie_model
        self._timeout = timeout
        self._request_fn = request_fn or self._urllib_post

    # -- ImageProvider protocol --

    def generate_scene(self, prompt: str, *, size: int = 1024) -> bytes:
        return self._generate(self._scene_model,
                              {"model": self._scene_model,
                               "prompt": prompt,
                               "response_format": "b64_json"})

    def generate_selfie(self, prompt: str, *, character_ref: bytes,
                        size: int = 1024) -> bytes:
        instruction = ("Keep the SAME person as in the reference photo "
                       "(same face, same hiker, same look); " + prompt)
        # character_ref is reserved (see class docstring): the image-editing
        # shape is unverified on this route, so we do plain text-to-image
        # with the instruction prompt only.
        return self._generate(self._selfie_model,
                              {"model": self._selfie_model,
                               "prompt": instruction,
                               "response_format": "b64_json"})

    # -- REST plumbing --

    def _urllib_post(self, url, headers, body):
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _generate(self, model: str, payload: dict) -> bytes:
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self._hf_token}"}
        body = json.dumps(payload).encode()

        last = None
        for attempt in (1, 2):
            try:
                status, raw = self._request_fn(HF_PROVIDER_BASE, headers,
                                               body)
            except (urllib.error.URLError, socket.timeout, TimeoutError,
                    OSError) as e:
                last = ImageError(
                    f"hf transport failed (attempt {attempt}/2): {e}")
                continue  # one retry on transport failure
            if status == 429:
                # Rate limit: do NOT retry — degrade to text-only
                # immediately (failure policy, images.py docstring).
                raise ImageError(
                    "hf rate-limited (429); no retry this turn — "
                    "text-only turn goes out.")
            if status == 503:
                # Serverless cold start ("Model is currently loading"):
                # one retry, then degrade. Treated like a 5xx.
                last = ImageError(
                    f"hf model loading (503, attempt {attempt}/2)")
                continue
            if 500 <= status < 600:
                last = ImageError(
                    f"hf server error {status} (attempt {attempt}/2)")
                continue  # one retry on 5xx
            if status != 200:
                raise ImageError(
                    f"hf rejected the request ({status}): {raw[:200]!r}")
            try:
                # OpenAI-compatible envelope, verified live 2026-10-01:
                # {"created": int, "data": [{"b64_json": "<base64 png>"}]}.
                # Anything else is a loud failure, never a guess.
                envelope = json.loads(raw)
                b64 = envelope["data"][0]["b64_json"]
                return _to_jpeg(base64.b64decode(b64))
            except (KeyError, IndexError, ValueError,
                    base64.binascii.Error) as e:
                raise ImageError(
                    f"hf image payload not decodable: {e}; "
                    f"first bytes: {raw[:120]!r}") from e
            except Exception as e:
                raise ImageError(
                    f"hf image payload not decodable: {e}") from e
        raise last


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

# Composite tuning, decided 2026-09-27 (research/composite-size-matrix.md):
# 1024px panels keep the archived turn composite crisp; email clients
# downscale to ~600px anyway, so the archive copy is the real beneficiary.
# JPEG quality 80 is visually indistinguishable from 85 at these sizes on
# photographic panels and ~7% smaller; the map panel's line-art text stays
# legible at q80 in 1024px. Typical outbound composite lands ~100-150KB
# on stub/synthetic panels, ~0.3-0.8MB on real AI-generated photos —
# comfortably inside Gmail's 25MB cap.
COMPOSITE_PANEL_SIZE = 1024
COMPOSITE_JPEG_QUALITY = 80


def stitch_composite(scene: Image.Image, map_panel: Image.Image,
                     selfie: Image.Image, size: int = COMPOSITE_PANEL_SIZE,
                     quality: int = COMPOSITE_JPEG_QUALITY) -> bytes:
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
    canvas.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def build_turn_composite(games_dir, guid, turn_no, provider,
                         size: int = COMPOSITE_PANEL_SIZE,
                         quality: int = COMPOSITE_JPEG_QUALITY) -> dict:
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
            size=size, quality=quality)
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
