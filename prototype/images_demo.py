"""Phase 3 images demo — server/images.py wired through the mailer.

Stub provider determinism, prompt secrecy (filtered state only), the
three-panel composite stitch, character-ref continuity, the outbound
attachment path, and the failure policy: images never fail a turn.
All green = the image pipeline contract holds.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from PIL import Image

from server import config as _config
from server.gm import MockGM
from server.images import (ImageError, GeminiImageProvider, HFImageProvider,
                           HF_SCENE_MODEL, HF_SELFIE_MODEL,
                           POLLINATIONS_BASE, POLLINATIONS_MODEL,
                           PollinationsImageProvider,
                           StubImageProvider, build_provider,
                           build_turn_composite, get_character_ref,
                           scene_prompt, selfie_prompt, stitch_composite,
                           time_of_day_word)
from server.mailer import (FakeGmail, run_poll_cycle,
                           _resolve_composite_marker)
from server.murph_relay import build_outbound_envelope
from server.render import COMPOSITE_CID, COMPOSITE_IMG_MARKER
from server.schema import create_db
from server import seed as _seed
from server.turn_loop import filtered_view

PLAYER = "player@example.com"


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"demo failed: {name}")


def fresh_game_db():
    db = create_db()
    guid = "00000000-0000-4000-8000-000000000001"
    _seed.seed(db, guid, PLAYER)
    return db, guid


# --- 1. time-of-day words from the game clock (starts 07:00) ---
check("t0 -> morning", time_of_day_word(0) == "morning")
check("t+240m (11:00) -> midday", time_of_day_word(240) == "midday")
check("t+600m (17:00) -> dusk", time_of_day_word(600) == "dusk")
check("t+840m (21:00) -> night", time_of_day_word(840) == "night")
check("t+1140m (02:00) -> night", time_of_day_word(1140) == "night")

# --- 2. provider modes: loud refusal, stub available ---
check("off -> None", build_provider("off") is None)
check("stub -> provider", isinstance(build_provider("stub"), StubImageProvider))
for bad in ("real", "hf", "bogus"):
    try:
        build_provider(bad)
        check(f"mode {bad!r} raises", False)
    except ImageError:
        check(f"mode {bad!r} raises (no key / unknown)", True)
try:
    real_provider = build_provider("real", api_key="k")
    check("real+key -> GeminiImageProvider", isinstance(real_provider, GeminiImageProvider))
except ImageError:
    check("real+key -> GeminiImageProvider (wired)", False)
try:
    hf_provider = build_provider("hf", hf_token="hf_test")
    check("hf+token -> HFImageProvider", isinstance(hf_provider, HFImageProvider))
except ImageError:
    check("hf+token -> HFImageProvider (wired)", False)
try:
    GeminiImageProvider("")
    check("empty API key raises at construction", False)
except ImageError:
    check("empty API key raises at construction", True)
try:
    HFImageProvider("")
    check("empty HF token raises at construction", False)
except ImageError:
    check("empty HF token raises at construction", True)

# --- 3. stub determinism ---
stub = StubImageProvider()
a1 = stub.generate_scene("the fog ocean at dusk", size=256)
a2 = stub.generate_scene("the fog ocean at dusk", size=256)
b1 = stub.generate_scene("a different prompt", size=256)
check("stub deterministic per prompt", a1 == a2)
check("stub varies with prompt", a1 != b1)
check("stub scene is a valid JPEG",
      Image.open(io.BytesIO(a1)).size == (256, 256))
check("stub selfie honors the provider interface",
      Image.open(io.BytesIO(stub.generate_selfie("x", character_ref=b"ref",
                                                 size=256))).size == (256, 256))

# --- 4. prompts use filtered state only: hidden traits never leak ---
db, guid = fresh_game_db()
db.execute("UPDATE games SET plot_concept='the earth changing' WHERE guid=?",
           (guid,))
view = filtered_view(db, guid)
sp, fp = scene_prompt(view, "dusk"), selfie_prompt(view, "dusk")
check("scene prompt names the current place",
      "Trailhead" in sp)
check("scene prompt has the time-of-day word", "dusk" in sp)
check("scene prompt excludes hidden_traits values",
      "unexplained" not in sp.lower())
check("scene prompt excludes the plot concept",
      "earth changing" not in sp.lower())
check("selfie prompt excludes hidden_traits values",
      "unexplained" not in fp.lower())
check("selfie prompt has pose + time of day",
      "standing" in fp and "dusk" in fp)
db.close()

# --- 5. composite build on a real game dir: stitch, assets, ref ---
games_dir = tempfile.mkdtemp(prefix="atfl-images-")
db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
db.row_factory = sqlite3.Row
db.executescript(__import__("server.schema", fromlist=["SCHEMA"]).SCHEMA)
_seed.seed(db, guid, PLAYER)
db.close()

comp1 = build_turn_composite(games_dir, guid, 1, stub, size=256)
img = Image.open(io.BytesIO(comp1["jpeg"]))
check("composite is 3 stacked panels", img.size == (256, 256 * 3 + 12))
check("composite provenance has prompts + time of day",
      comp1["time_of_day"] == "morning" and comp1["scene_prompt"]
      and comp1["selfie_prompt"])
check("first turn: no ref yet", comp1["character_ref_used"] is False)
check("character ref stored after first selfie",
      get_character_ref(games_dir, guid) is not None)

db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
kinds = sorted(r[0] for r in db.execute("SELECT kind FROM assets WHERE turn_created=1"))
db.close()
check("assets rows: scene, selfie, composite, character_ref",
      kinds == ["character_ref", "composite", "scene", "selfie"])

comp2 = build_turn_composite(games_dir, guid, 2, stub, size=256)
check("second turn reuses the canonical ref",
      comp2["character_ref_used"] is True)
db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
ref_rows = db.execute(
    "SELECT COUNT(*) FROM assets WHERE kind='character_ref'").fetchone()[0]
db.close()
check("character_ref created once, never overwritten", ref_rows == 1)
check("prompts reproducible via hash",
      len(comp2["prompt_hash"]) == 16)

# --- 6. stitch_composite unit ---
from server.map_panel import render_map
map_img = render_map({"trailhead": "Trailhead"}, [], "trailhead",
                     "dusk", size=256)
stitched = stitch_composite(Image.open(io.BytesIO(a1)), map_img,
                            Image.open(io.BytesIO(b1)), size=256)
check("stitch yields a valid JPEG of the stacked size",
      Image.open(io.BytesIO(stitched)).size == (256, 256 * 3 + 12))

# --- 6a. composite size/quality tuning constants (2026-09-27 decision) ---
from server.images import (COMPOSITE_PANEL_SIZE, COMPOSITE_JPEG_QUALITY,
                           build_turn_composite)
check("production composite is 1024px panels (archive-crisp; clients downscale)",
      COMPOSITE_PANEL_SIZE == 1024)
check("production composite JPEG quality is 80 (sweet spot vs 85)",
      COMPOSITE_JPEG_QUALITY == 80)
hi_q = stitch_composite(Image.open(io.BytesIO(a1)), map_img,
                        Image.open(io.BytesIO(b1)), size=256, quality=95)
lo_q = stitch_composite(Image.open(io.BytesIO(a1)), map_img,
                        Image.open(io.BytesIO(b1)), size=256, quality=60)
check("quality param flows through: lower quality -> fewer bytes",
      len(lo_q) < len(hi_q))
def_q = stitch_composite(Image.open(io.BytesIO(a1)), map_img,
                         Image.open(io.BytesIO(b1)), size=256)
check("default quality matches the constant",
      len(def_q) == len(stitch_composite(Image.open(io.BytesIO(a1)), map_img,
                                         Image.open(io.BytesIO(b1)), size=256,
                                         quality=COMPOSITE_JPEG_QUALITY)))

# --- 7. outbound attachment path: the atfl_outbound envelope contract ---
# (relay edition, 2026-10-02: the engine no longer builds MIME — build_raw
# is gone. The composite rides as an envelope attachment with the pinned
# content_id, and the Murph-side sender renders inline images as
# cid:<content_id> per digest_send_inkbox.py.)
import re as _re
env = build_outbound_envelope(
    guid, 1, PLAYER, "subject", "body",
    "<p>body</p>" + COMPOSITE_IMG_MARKER, composite_jpeg=comp1["jpeg"])
atts = env["attachments"]
check("one composite attachment rides in the envelope", len(atts) == 1)
check("attachment content_id is the pinned cid convention",
      atts[0]["content_id"] == COMPOSITE_CID == "composite")
check("attachment filename names the turn",
      atts[0]["filename"] == "turn-1-composite.jpg")
check("attachment bytes are the composite JPEG",
      atts[0]["bytes"] == comp1["jpeg"] and atts[0]["bytes"][:2] == b"\xff\xd8")
resolved = _resolve_composite_marker(env["body_html"], True)
check("marker resolves to exactly one inline img tag",
      resolved.count("<img") == 1 and COMPOSITE_IMG_MARKER not in resolved)
cid = _re.search(r'cid:([a-z0-9-]+)', resolved).group(1)
check("html cid names the attachment's content_id (else a broken image)",
      cid == atts[0]["content_id"])
check("body text still intact", env["body_text"] == "body")
# text-only turn: no attachments, marker dropped, never a broken image
env2 = build_outbound_envelope(
    guid, 2, PLAYER, "subject", "body",
    "<p>body</p>" + COMPOSITE_IMG_MARKER, composite_jpeg=None)
check("no composite: no attachments", env2["attachments"] == [])
no_img = _resolve_composite_marker(env2["body_html"], False)
check("no composite: marker dropped, no img tag",
      COMPOSITE_IMG_MARKER not in no_img and "<img" not in no_img)

# --- 8. end to end: poll cycle with stub images attaches the composite ---
fake = FakeGmail()
fake.queue_inbound(PLAYER, "start", "start")
gm = MockGM()
res = run_poll_cycle(games_dir, gm, "atfl-server", "murph", "/node/root",
                     images={"mode": "stub"}, relay=fake)
check("turn email handed off with images on",
      len(res["sent"]) == 1 and res["sent"][0]["action"] == "turn_email"
      and res["sent"][0]["handoff"] is True)
check("handoff note records the composite",
      res["sent"][0]["note"] and "composite attached" in res["sent"][0]["note"])
env8 = fake.outbox[-1]["envelope"]
check("envelope is atfl_outbound to the player",
      env8["kind"] == "atfl_outbound" and env8["to"] == PLAYER)
sent_atts = env8["attachments"]
check("outbound envelope carries the composite JPEG",
      len(sent_atts) == 1
      and sent_atts[0]["content_id"] == "composite"
      and sent_atts[0]["filename"].endswith("-composite.jpg")
      and sent_atts[0]["bytes"][:2] == b"\xff\xd8")
check("HTML twin renders the composite inline (cid reference)",
      'src="cid:composite"' in env8["body_html"]
      and COMPOSITE_IMG_MARKER not in env8["body_html"])
check("turn body still complete text with image on",
      "Game code:" in env8["body_text"] and len(env8["body_text"]) > 100)

# --- 9. failure policy: a broken provider never fails the turn ---
class FailingProvider:
    def generate_scene(self, prompt, *, size=1024):
        raise RuntimeError("model exploded")
    def generate_selfie(self, prompt, *, character_ref, size=1024):
        raise RuntimeError("model exploded")

fake2 = FakeGmail()
fake2.queue_inbound(PLAYER, "start", "start")
games2 = tempfile.mkdtemp(prefix="atfl-images-fail-")
# _turn_composite resolves build_provider from server.images at call
# time, so patching the module attribute steers it at the source.
import server.images as _images
_real_build = _images.build_provider
_images.build_provider = lambda mode, **kw: FailingProvider()
try:
    res2 = run_poll_cycle(games2, gm, "atfl-server", "murph", "/node/root",
                          images={"mode": "stub"}, relay=fake2)
finally:
    _images.build_provider = _real_build
check("broken provider: turn still handed off",
      len(res2["sent"]) == 1 and res2["sent"][0]["handoff"] is True)
check("broken provider: note says images skipped",
      res2["sent"][0]["note"] and "images skipped" in res2["sent"][0]["note"])
check("broken provider: no attachment on the text-only handoff",
      fake2.outbox[-1]["envelope"]["attachments"] == [])

# --- 10. images off: cycle identical to the old text-only path ---
fake3 = FakeGmail()
fake3.queue_inbound(PLAYER, "start", "start")
games3 = tempfile.mkdtemp(prefix="atfl-images-off-")
res3 = run_poll_cycle(games3, gm, "atfl-server", "murph", "/node/root",
                      images={"mode": "off"}, relay=fake3)
check("images off: turn handed off, no attachment",
      res3["sent"][0]["handoff"] is True
      and fake3.outbox[-1]["envelope"]["attachments"] == [])
check("images off: no assets dir created",
      not os.path.exists(os.path.join(games3, "assets")))

# --- 11. config: images flag validation (relay edition, 2026-10-02) ---
# ATFL_GAME_ADDRESS is retired (the player-facing address is Murph's
# operational detail); ATFL_A8S_NODE_ROOT is required — startup refuses
# a half-wired send path.
BASE = {"ATFL_A8S_NODE_ROOT": "/node/root"}
check("default ATFL_IMAGES is off",
      _config.load(dict(BASE))["images_mode"] == "off")
try:
    _config.load(dict(BASE, ATFL_IMAGES="bogus"))
    check("bogus ATFL_IMAGES raises", False)
except _config.ConfigError:
    check("bogus ATFL_IMAGES raises", True)
try:
    _config.load(dict(BASE, ATFL_IMAGES="real"))
    check("real without key raises", False)
except _config.ConfigError:
    check("real without key raises", True)
cfg = _config.load(dict(BASE, ATFL_IMAGES="stub"))
check("stub config passes", cfg["images_mode"] == "stub")
cfg_p = _config.load(dict(BASE, ATFL_IMAGES="pollinations"))
check("pollinations config passes (keyless, no token required)",
      cfg_p["images_mode"] == "pollinations")

# --- 12. real provider REST contract: hermetic, no network ---
import base64 as _b64
import urllib.error as _uerr

def _fake_png(size=256):
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (120, 90, 60)).save(buf, "PNG")
    return buf.getvalue()

_FAKE_PNG = _fake_png()

def _fake_generate_response():
    return json.dumps({
        "candidates": [{
            "content": {"parts": [
                {"inlineData": {"mimeType": "image/png",
                                "data": _b64.b64encode(_FAKE_PNG).decode()}}]}}]
    }).encode()

def _ok_transport(seen):
    def _fn(url, headers, body):
        seen.append((url, dict(headers), body))
        return 200, _fake_generate_response()
    return _fn

seen = []
gp = GeminiImageProvider("TEST-KEY", request_fn=_ok_transport(seen))
scene = gp.generate_scene("mist on the ridge", size=1024)
check("real provider returns image bytes", len(scene) > 1000)
check("real provider normalizes to JPEG", scene[:2] == b"\xff\xd8")
img = Image.open(io.BytesIO(scene))
check("real provider JPEG opens", img.size[0] > 0)

url, headers, body = seen[0]
check("request hits generateContent endpoint",
      url.endswith("gemini-3.1-flash-image:generateContent"))
check("API key in header, not URL",
      headers.get("x-goog-api-key") == "TEST-KEY" and "key=" not in url)
payload = json.loads(body)
check("generationConfig requests IMAGE only",
      payload["generationConfig"]["responseModalities"] == ["IMAGE"])
check("1K imageSize for 1024 panels",
      payload["generationConfig"]["imageConfig"] == {"aspectRatio": "1:1",
                                                     "imageSize": "1K"})
check("scene parts are text-only",
      [p.keys() for p in payload["contents"][0]["parts"]] == [{"text"}])
check("prompt passes through verbatim",
      payload["contents"][0]["parts"][0]["text"] == "mist on the ridge")

seen2 = []
gp2 = GeminiImageProvider("TEST-KEY", request_fn=_ok_transport(seen2))
ref = _fake_png(128)
selfie = gp2.generate_selfie("damp hiker selfie", character_ref=ref, size=1024)
sparts = json.loads(seen2[0][2])["contents"][0]["parts"]
check("selfie parts: ref image first, text second",
      set(sparts[0]["inlineData"].keys()) == {"mimeType", "data"}
      and "text" in sparts[1])
check("ref sent as base64 inlineData",
      _b64.b64decode(sparts[0]["inlineData"]["data"]) == ref)
check("continuity instruction prepended",
      sparts[1]["text"].startswith("Keep the SAME person as in the reference photo"))

seen3 = []
gp3 = GeminiImageProvider("TEST-KEY", request_fn=_ok_transport(seen3))
gp3.generate_selfie("no ref available", character_ref=b"", size=1024)
sparts3 = json.loads(seen3[0][2])["contents"][0]["parts"]
check("no ref: text-only parts", [p.keys() for p in sparts3] == [{"text"}])

calls = {"n": 0}
def _flaky_then_ok(url, headers, body):
    calls["n"] += 1
    if calls["n"] == 1:
        raise _uerr.URLError("connection reset")
    return 200, _fake_generate_response()
gp4 = GeminiImageProvider("TEST-KEY", request_fn=_flaky_then_ok)
check("one transport retry then success", len(gp4.generate_scene("x")) > 1000
      and calls["n"] == 2)

def _always_fail(url, headers, body):
    raise _uerr.URLError("down")
try:
    GeminiImageProvider("TEST-KEY", request_fn=_always_fail).generate_scene("x")
    check("double transport failure raises ImageError", False)
except ImageError:
    check("double transport failure raises ImageError", True)

def _limited(url, headers, body):
    return 429, b'{"error": {"message": "quota"}}'
try:
    GeminiImageProvider("TEST-KEY", request_fn=_limited).generate_scene("x")
    check("429 raises without retry", False)
except ImageError as e:
    check("429 raises without retry", "429" in str(e))

def _badkey(url, headers, body):
    return 400, b'{"error": {"message": "API key not valid"}}'
try:
    GeminiImageProvider("TEST-KEY", request_fn=_badkey).generate_scene("x")
    check("400 surfaces the status", False)
except ImageError as e:
    check("400 surfaces the status", "400" in str(e))

def _server_err(url, headers, body):
    return 503, b'{"error": {"message": "backend"}}'
calls5 = {"n": 0}
def _count_server_err(url, headers, body):
    calls5["n"] += 1
    return _server_err(url, headers, body)
try:
    GeminiImageProvider("TEST-KEY", request_fn=_count_server_err).generate_scene("x")
    check("5xx retries once then raises", False)
except ImageError:
    check("5xx retries once then raises", calls5["n"] == 2)

def _no_image(url, headers, body):
    return 200, b'{"candidates": [{"content": {"parts": [{"text": "no image for you"}]}}]}'
try:
    GeminiImageProvider("TEST-KEY", request_fn=_no_image).generate_scene("x")
    check("response without image part raises", False)
except ImageError:
    check("response without image part raises", True)

# --- 13. HF Inference provider REST contract: hermetic, no network ---
# Contract verified LIVE 2026-10-01 from free-micro-1 (nscale provider route):
# POST https://router.huggingface.co/nscale/v1/images/generations,
# {"model": ..., "prompt": ..., "response_format": "b64_json"} -> 200
# {"created": int, "data": [{"b64_json": "<base64 png>"}]}. Real 1024x1024
# PNG confirmed (goal hidden_files/hf_first_test_image.png).
def _hf_ok_transport(seen):
    def _fn(url, headers, body):
        seen.append((url, dict(headers), body))
        env = {"created": 1700000000,
               "data": [{"b64_json": _b64.b64encode(_fake_png(256)).decode()}]}
        return 200, json.dumps(env).encode()
    return _fn

seen_hf = []
hp = HFImageProvider("hf_test", request_fn=_hf_ok_transport(seen_hf))
hscene = hp.generate_scene("mist on the ridge")
check("hf scene returns image bytes", len(hscene) > 1000)
check("hf normalizes to JPEG", hscene[:2] == b"\xff\xd8")

hurl, hheaders, hbody = seen_hf[0]
check("hf request hits the nscale OpenAI-compatible endpoint",
      hurl == "https://router.huggingface.co/nscale/v1/images/generations")
check("hf token in Bearer header, not URL",
      hheaders.get("Authorization") == "Bearer hf_test"
      and "hf_test" not in hurl)
hpayload = json.loads(hbody)
check("hf scene body is the OpenAI-compatible shape",
      hpayload == {"model": HF_SCENE_MODEL, "prompt": "mist on the ridge",
                   "response_format": "b64_json"})
check("scene model is FLUX.1-schnell", HF_SCENE_MODEL == "black-forest-labs/FLUX.1-schnell")
check("selfie model is FLUX.1-schnell (Kontext-dev 400s on all provider routes; verified 2026-10-01)", HF_SELFIE_MODEL == "black-forest-labs/FLUX.1-schnell")

seen_hf2 = []
hp2 = HFImageProvider("hf_test", request_fn=_hf_ok_transport(seen_hf2))
href = _fake_png(128)
hselfie = hp2.generate_selfie("damp hiker selfie", character_ref=href)
check("hf selfie returns image bytes", len(hselfie) > 1000)
hurl2, _, hbody2 = seen_hf2[0]
check("hf selfie hits the same provider endpoint",
      hurl2 == "https://router.huggingface.co/nscale/v1/images/generations")
hpayload2 = json.loads(hbody2)
check("hf selfie body is model + instruction prompt",
      hpayload2["model"] == HF_SELFIE_MODEL
      and hpayload2["prompt"].startswith(
          "Keep the SAME person as in the reference photo")
      and hpayload2["response_format"] == "b64_json")

seen_hf3 = []
hp3 = HFImageProvider("hf_test", request_fn=_hf_ok_transport(seen_hf3))
hp3.generate_selfie("no ref available", character_ref=b"")
hpayload3 = json.loads(seen_hf3[0][2])
check("hf no-ref selfie is the same OpenAI shape",
      hpayload3["model"] == HF_SELFIE_MODEL
      and hpayload3["prompt"].startswith(
          "Keep the SAME person as in the reference photo"))

hcalls = {"n": 0}
def _hf_loading_then_ok(url, headers, body):
    hcalls["n"] += 1
    if hcalls["n"] == 1:
        return 503, b'{"error": "Model black-forest-labs/FLUX.1-schnell is currently loading"}'
    env = {"created": 1,
           "data": [{"b64_json": _b64.b64encode(_fake_png(256)).decode()}]}
    return 200, json.dumps(env).encode()
hp4 = HFImageProvider("hf_test", request_fn=_hf_loading_then_ok)
check("hf 503-loading retries once then succeeds",
      len(hp4.generate_scene("x")) > 1000 and hcalls["n"] == 2)

hcalls5 = {"n": 0}
def _hf_loading_forever(url, headers, body):
    hcalls5["n"] += 1
    return 503, b'{"error": "Model is currently loading"}'
try:
    HFImageProvider("hf_test", request_fn=_hf_loading_forever).generate_scene("x")
    check("hf 503 twice raises ImageError", False)
except ImageError as e:
    check("hf 503 twice raises ImageError", hcalls5["n"] == 2 and "503" in str(e))

def _hf_limited(url, headers, body):
    return 429, b'{"error": "Rate limit reached"}'
try:
    HFImageProvider("hf_test", request_fn=_hf_limited).generate_scene("x")
    check("hf 429 raises without retry", False)
except ImageError as e:
    check("hf 429 raises without retry", "429" in str(e))

def _hf_flaky(url, headers, body):
    raise _uerr.URLError("reset")
try:
    HFImageProvider("hf_test", request_fn=_hf_flaky).generate_scene("x")
    check("hf double transport failure raises", False)
except ImageError:
    check("hf double transport failure raises", True)

def _hf_not_image(url, headers, body):
    return 200, b'not-an-image-at-all'
try:
    HFImageProvider("hf_test", request_fn=_hf_not_image).generate_scene("x")
    check("hf non-image payload raises", False)
except ImageError:
    check("hf non-image payload raises", True)

# -- Pollinations (keyless) provider -----------------------------------------
# Contract: GET https://image.pollinations.ai/prompt/{urlencoded-prompt}?
# width=&height=&model=flux&seed=<deterministic-on-prompt>&nologo=true&private=true
# -> 200 image/jpeg directly. Verified live 2026-10-07 from the sandbox
# (256px smoke: 200, JPEG, ~5.5s).
def _pollinations_ok(seen):
    def _fn(url, headers):
        seen.append((url, dict(headers)))
        return 200, _fake_png(256)  # endpoint serves JPEG; _to_jpeg normalizes
    return _fn

seen_p = []
pp = PollinationsImageProvider(request_fn=_pollinations_ok(seen_p))
pscene = pp.generate_scene("mist on the ridge")
check("pollinations scene returns image bytes", len(pscene) > 1000)
check("pollinations normalizes to JPEG", pscene[:2] == b"\xff\xd8")

purl, pheaders = seen_p[0]
import hashlib as _hashlib
want_seed = int(_hashlib.sha1(b"mist on the ridge").hexdigest(), 16) % 2**31
check("pollinations request hits the keyless GET endpoint",
      purl == (f"{POLLINATIONS_BASE}mist%20on%20the%20ridge"
               f"?width=1024&height=1024&model={POLLINATIONS_MODEL}"
               f"&seed={want_seed}&nologo=true&private=true"))
check("pollinations sends no secret in any header",
      all(v != "Bearer hf_test" for v in pheaders.values())
      and "Authorization" not in pheaders)
check("pollinations requests private generation", "private=true" in purl)

seen_p2 = []
pp2 = PollinationsImageProvider(request_fn=_pollinations_ok(seen_p2))
href_p = _fake_png(128)
pp2.generate_selfie("damp hiker selfie", character_ref=href_p)
purl2 = seen_p2[0][0]
check("pollinations selfie is instruction-prefixed t2i",
      "Keep%20the%20SAME%20person%20as%20in%20the%20reference%20photo" in purl2)

# determinism: same prompt -> same seed -> same URL
seen_p3 = []
pp3 = PollinationsImageProvider(request_fn=_pollinations_ok(seen_p3))
pp3.generate_scene("mist on the ridge")
check("pollinations seed is deterministic on the prompt",
      seen_p3[0][0] == seen_p[0][0])

pcalls = {"n": 0}
def _pollinations_flaky(url, headers):
    pcalls["n"] += 1
    if pcalls["n"] == 1:
        raise _uerr.URLError("reset")
    return 200, _fake_png(256)
pp4 = PollinationsImageProvider(request_fn=_pollinations_flaky)
check("pollinations transport failure retries once then succeeds",
      len(pp4.generate_scene("x")) > 1000 and pcalls["n"] == 2)

def _pollinations_limited(url, headers):
    return 429, b'{"error": "rate limited"}'
try:
    PollinationsImageProvider(request_fn=_pollinations_limited).generate_scene("x")
    check("pollinations 429 raises without retry", False)
except ImageError as e:
    check("pollinations 429 raises without retry", "429" in str(e))

def _pollinations_err(url, headers):
    return 200, b'{"error": "something broke"}'
try:
    PollinationsImageProvider(request_fn=_pollinations_err).generate_scene("x")
    check("pollinations JSON-body 200 raises", False)
except ImageError:
    check("pollinations JSON-body 200 raises", True)

# build_provider wiring
check("build_provider('pollinations') -> PollinationsImageProvider",
      isinstance(build_provider("pollinations"), PollinationsImageProvider))
try:
    build_provider("nope")
    check("build_provider rejects unknown mode", False)
except ImageError as e:
    check("build_provider rejects unknown mode",
          "pollinations" in str(e))

print("\nimages demo: all green")
