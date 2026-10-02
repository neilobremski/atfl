"""E2E check: full turn-image path against REAL seeded game state.

Covers what the earlier checks did not: build_turn_composite (the exact
function mailer.py calls) reading filtered_view + game clock from a real
seeded DB — prompts from physical state only, map from DB-discovered
places, character-ref persistence across turns, provenance rows in the
assets table. Uses the stub provider (local placeholder panels) so this
burns no HF inference; the real HF provider route was verified separately
(session #67).

Run: python3 prototype/turn_composite_e2e.py
Exit 0 = all checks green.
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from server.schema import SCHEMA  # noqa: E402
from server.seed import seed  # noqa: E402
from server.images import build_provider, build_turn_composite  # noqa: E402
from PIL import Image

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, ("— " + detail) if detail and not cond else "")
    if not cond:
        FAILURES.append(name + (" — " + detail if detail else ""))


def main():
    games_dir = tempfile.mkdtemp(prefix="atfl-turnimg-")
    guid = "e2e-turn-check-guid"
    db_path = os.path.join(games_dir, f"{guid}.db")

    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    seed(db, guid, "player@example.com")
    # Turn-1 reality: player walked trailhead -> trail-down (discovered it),
    # game clock advanced one hour (07:00 -> 08:00).
    c = db.cursor()
    c.execute("UPDATE games SET turn_no=1, game_clock_min=60 WHERE guid=?", (guid,))
    c.execute("UPDATE places SET discovered=1, last_visited_turn=1 WHERE slug='trail-down'")
    c.execute("UPDATE actors SET location_slug='trail-down' WHERE is_player=1")
    db.commit()
    db.close()

    provider = build_provider("stub")

    # ---- Turn 1: no character ref yet
    r1 = build_turn_composite(games_dir, guid, 1, provider, size=256)
    check("t1 composite returned jpeg bytes", isinstance(r1["jpeg"], bytes) and len(r1["jpeg"]) > 1000)
    img1 = Image.open(io.BytesIO(r1["jpeg"]))
    check("t1 composite has 3 stacked panels", abs(img1.size[1] - (3 * 256 + 2 * 6)) <= 2,
          f"size={img1.size}")
    check("t1 no character ref used yet", r1["character_ref_used"] is False)
    check("t1 time of day morning", r1["time_of_day"] == "morning", str(r1["time_of_day"]))
    check("t1 scene prompt non-empty", bool(r1["scene_prompt"]))
    check("t1 selfie prompt non-empty", bool(r1["selfie_prompt"]))

    db = sqlite3.connect(db_path)
    kinds1 = [row[0] for row in db.execute(
        "SELECT kind FROM assets WHERE turn_created=1")]
    db.close()
    for kind in ("scene", "selfie", "composite", "character_ref"):
        check(f"t1 assets row stored: {kind}", kind in kinds1, f"kinds={kinds1}")

    # ---- Turn 2: character ref must be reused
    db = sqlite3.connect(db_path)
    db.execute("UPDATE games SET turn_no=2, game_clock_min=120 WHERE guid=?", (guid,))
    db.commit()
    db.close()
    r2 = build_turn_composite(games_dir, guid, 2, provider, size=256)
    check("t2 character ref reused", r2["character_ref_used"] is True)
    img2 = Image.open(io.BytesIO(r2["jpeg"]))
    check("t2 composite valid jpeg", img2.size[0] == 256)

    db = sqlite3.connect(db_path)
    n_refs = db.execute(
        "SELECT COUNT(*) FROM assets WHERE kind='character_ref'"
    ).fetchone()[0]
    db.close()
    check("t2 character ref stored exactly once", n_refs == 1, f"refs={n_refs}")

    shutil.rmtree(games_dir, ignore_errors=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURES")
        sys.exit(1)
    print("\nALL GREEN")


if __name__ == "__main__":
    main()
