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
from email import policy
from email.parser import BytesParser

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from PIL import Image

from server import config as _config
from server.gm import MockGM
from server.images import (ImageError, GeminiImageProvider, StubImageProvider, build_provider,
                           build_turn_composite, get_character_ref,
                           scene_prompt, selfie_prompt, stitch_composite,
                           time_of_day_word)
from server.mailer import FakeGmail, build_raw, run_poll_cycle, GAME_ADDRESS
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
for bad in ("real", "bogus"):
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
    GeminiImageProvider("")
    check("empty API key raises at construction", False)
except ImageError:
    check("empty API key raises at construction", True)

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
      "Trail above the fog line" in sp)
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
map_img = render_map({"trailhead": "Trail above the fog line"}, [], "trailhead",
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

# --- 7. outbound attachment path: build_raw with inline composite ---
raw = build_raw("game@example.com", PLAYER, "subject", "body",
                html_body="<p>body</p><img src=\"cid:turn-composite\">",
                attachments=[("turn-1-composite.jpg", comp1["jpeg"],
                              "image/jpeg", "turn-composite")])
parsed = BytesParser(policy=policy.default).parsebytes(raw)
atts = list(parsed.iter_attachments())
check("one image/jpeg attachment rides along",
      len(atts) == 1 and atts[0].get_content_type() == "image/jpeg")
check("attachment filename set",
      atts[0].get_filename() == "turn-1-composite.jpg")
check("composite marked inline with a Content-ID (Neil's inline-images req)",
      atts[0]["Content-ID"] == "<turn-composite>"
      and atts[0].get_content_disposition() == "inline")
check("body text still intact", parsed.get_body(preferencelist=("plain",))
      .get_content().strip() == "body")
check("HTML twin rides along with the cid reference",
      parsed.get_body(preferencelist=("html",)).get_content())
# 3-tuple attachments (no cid) stay plain downloadable attachments
raw2 = build_raw("game@example.com", PLAYER, "subject", "body",
                 attachments=[("notes.txt", b"hi", "text/plain")])
att2 = list(BytesParser(policy=policy.default).parsebytes(raw2)
            .iter_attachments())[0]
check("no-cid attachment has no Content-ID",
      att2["Content-ID"] is None
      and att2.get_content_disposition() == "attachment")

# --- 8. end to end: poll cycle with stub images attaches the composite ---
fake = FakeGmail()
fake.queue_inbound(PLAYER, "start", "start",
                   header_message_id="<signup-img@fake>")
gm = MockGM()
res = run_poll_cycle(games_dir, fake, gm, game_address=GAME_ADDRESS,
                     images={"mode": "stub", "api_key": None})
check("turn email sent with images on",
      len(res["sent"]) == 1 and res["sent"][0]["action"] == "turn_email")
check("sent note records the composite",
      res["sent"][0]["note"] and "composite attached" in res["sent"][0]["note"])
sent = fake.outbox[-1]["parsed"]
sent_atts = list(sent.iter_attachments())
check("outbound turn email carries the composite JPEG",
      len(sent_atts) == 1
      and sent_atts[0].get_content_type() == "image/jpeg"
      and sent_atts[0].get_filename().endswith("-composite.jpg"))
check("composite part is inline via Content-ID",
      sent_atts[0]["Content-ID"] == "<turn-composite>"
      and sent_atts[0].get_content_disposition() == "inline")
sent_html = sent.get_body(preferencelist=("html",)).get_content()
check("HTML twin renders the composite inline (cid reference)",
      'src="cid:turn-composite"' in sent_html
      and "TURN_COMPOSITE" not in sent_html)
body = sent.get_body(preferencelist=("plain",)).get_content()
check("turn body still complete text with image on",
      "Game code:" in body and len(body) > 100)

# --- 9. failure policy: a broken provider never fails the turn ---
class FailingProvider:
    def generate_scene(self, prompt, *, size=1024):
        raise RuntimeError("model exploded")
    def generate_selfie(self, prompt, *, character_ref, size=1024):
        raise RuntimeError("model exploded")

fake2 = FakeGmail()
fake2.queue_inbound(PLAYER, "start", "start",
                    header_message_id="<signup-fail@fake>")
games2 = tempfile.mkdtemp(prefix="atfl-images-fail-")
# _turn_attachments resolves build_provider from server.images at call
# time, so patching the module attribute steers it at the source.
import server.images as _images
_real_build = _images.build_provider
_images.build_provider = lambda mode, key=None: FailingProvider()
try:
    res2 = run_poll_cycle(games2, fake2, gm, game_address=GAME_ADDRESS,
                          images={"mode": "stub", "api_key": None})
finally:
    _images.build_provider = _real_build
check("broken provider: turn still sent",
      len(res2["sent"]) == 1 and res2["sent"][0]["message_id"] is not None)
check("broken provider: note says images skipped",
      res2["sent"][0]["note"] and "images skipped" in res2["sent"][0]["note"])
check("broken provider: no attachment on the text-only send",
      len(list(fake2.outbox[-1]["parsed"].iter_attachments())) == 0)

# --- 10. images off: cycle identical to the old text-only path ---
fake3 = FakeGmail()
fake3.queue_inbound(PLAYER, "start", "start",
                    header_message_id="<signup-off@fake>")
games3 = tempfile.mkdtemp(prefix="atfl-images-off-")
res3 = run_poll_cycle(games3, fake3, gm, game_address=GAME_ADDRESS,
                      images={"mode": "off", "api_key": None})
check("images off: turn sent, no attachment",
      res3["sent"][0]["message_id"] is not None
      and len(list(fake3.outbox[-1]["parsed"].iter_attachments())) == 0)
check("images off: no assets dir created",
      not os.path.exists(os.path.join(games3, "assets")))

# --- 11. config: images flag validation ---
os.environ.update({"ATFL_GAME_ADDRESS": "game@example.com"})
check("default ATFL_IMAGES is off",
      _config.load({"ATFL_GAME_ADDRESS": "x@y.z"})["images_mode"] == "off")
try:
    _config.load({"ATFL_GAME_ADDRESS": "x@y.z", "ATFL_IMAGES": "bogus"})
    check("bogus ATFL_IMAGES raises", False)
except _config.ConfigError:
    check("bogus ATFL_IMAGES raises", True)
try:
    _config.load({"ATFL_GAME_ADDRESS": "x@y.z", "ATFL_IMAGES": "real"})
    check("real without key raises", False)
except _config.ConfigError:
    check("real without key raises", True)
cfg = _config.load({"ATFL_GAME_ADDRESS": "x@y.z", "ATFL_IMAGES": "stub"})
check("stub config passes", cfg["images_mode"] == "stub")

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

print("\nimages demo: all green")
