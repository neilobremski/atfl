"""Phase 3 images demo — server/images.py wired through the mailer.

Stub provider determinism, prompt secrecy (filtered state only), the
three-panel composite stitch, character-ref continuity, the outbound
attachment path, and the failure policy: images never fail a turn.
All green = the image pipeline contract holds.
"""
import io
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
from server.images import (ImageError, StubImageProvider, build_provider,
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
    build_provider("real", api_key="k")
    check("real+key raises (not wired yet)", False)
except ImageError as e:
    check("real+key raises (not wired yet)", "next session" in str(e))

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

# --- 7. outbound attachment path: build_raw with attachments ---
raw = build_raw("game@example.com", PLAYER, "subject", "body",
                attachments=[("turn-1-composite.jpg", comp1["jpeg"],
                              "image/jpeg")])
parsed = BytesParser(policy=policy.default).parsebytes(raw)
atts = list(parsed.iter_attachments())
check("one image/jpeg attachment rides along",
      len(atts) == 1 and atts[0].get_content_type() == "image/jpeg")
check("attachment filename set",
      atts[0].get_filename() == "turn-1-composite.jpg")
check("body text still intact", parsed.get_body(preferencelist=("plain",))
      .get_content().strip() == "body")

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

print("\nimages demo: all green")
