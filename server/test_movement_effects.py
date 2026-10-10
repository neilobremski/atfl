"""Regression test for the 2026-10-09 movement-effects fix.

Before the fix, the roster's effect language was physical_state.*-only, so
no turn could record movement: actors.player.location_slug stayed
'trailhead' and every place but trailhead stayed undiscovered forever —
the map froze and the scene prompt never changed (player-reported bug).

Run: python3 server/test_movement_effects.py   (exit 0 = pass)
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.schema import SCHEMA
from server.seed import seed
from server.gm import _validate_questions, GameMaster
from server.turn_loop import run_turn, apply_effect, filtered_view, TurnFailed
from server.images import scene_prompt, time_of_day_word


class MoveGM(GameMaster):
    """Adjudicates 'walk down the trail' as a yes with a movement effect."""

    def pick_plot(self, roster, context=None):
        return "earth-changing"

    def adjudicate(self, player_input, filtered, context=None):
        return [{
            "q": "Does the player walk down the trail to the descent?",
            "answer": "yes",
            "rationale": "the test input says walk down",
            "effect": {"actor:player": {"location_slug": "trail-down"}},
        }]

    def compose_narrative(self, player_input, questions, filtered, catchup,
                          context=None):
        return "You walk down the trail to the descent. The fog waits below."


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    seed(db, "test-guid-movement", "player@example.com")
    return db


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.failed = True
check.failed = False


def movement_question(effect):
    return [{"q": "q?", "answer": "yes", "rationale": "r", "effect": effect}]


# 1. validator: the new key is accepted, the old abuses still rejected
check("validator accepts actor location_slug",
      _validate_questions(movement_question(
          {"actor:player": {"location_slug": "trail-down"}})))

for bad_effect, name in [
    ({"place:trail-down": {"location_slug": "trailhead"}},
     "validator rejects location_slug on place"),
    ({"place:trail-down": {"discovered": 1}},
     "validator rejects roster-written discovered"),
    ({"place:trail-down": {"last_visited_turn": 9}},
     "validator rejects roster-written last_visited_turn"),
    ({"actor:player": {"hp": 0.5}},
     "validator rejects other scalar keys on actor"),
]:
    try:
        _validate_questions(movement_question(bad_effect))
        check(name, False)
    except TurnFailed:
        check(name, True)

check("validator still accepts physical_state.*",
      _validate_questions(movement_question(
          {"place:trailhead": {"physical_state.ground_wetness": "dry"}})))

# 2. full turn: movement lands in the DB and unfreezes derived state
db = fresh_db()
before = scene_prompt(filtered_view(db, "test-guid-movement"),
                      time_of_day_word(0))
result = run_turn(db, "walk down the trail", MoveGM())
loc = db.execute(
    "SELECT location_slug FROM actors WHERE slug='player'").fetchone()[0]
check("run_turn moved player to trail-down", loc == "trail-down")
disc, lvt = db.execute(
    "SELECT discovered, last_visited_turn FROM places "
    "WHERE slug='trail-down'").fetchone()
check("destination discovered=1", disc == 1)
check("destination last_visited_turn=turn_no", lvt == result.turn_no)
after = scene_prompt(filtered_view(db, "test-guid-movement"),
                     time_of_day_word(60))
check("scene prompt changes after move", before != after)
check("scene prompt names the new place", "Descent" in after)
db.close()

# 3. referential guard: moving nowhere fails loudly, not silently
db = fresh_db()
turn_id = db.execute(
    "INSERT INTO turns (game_guid,turn_no,game_time_start_min,"
    "game_time_len_min) VALUES ('test-guid-movement',1,0,60)"
).lastrowid
try:
    apply_effect(db, turn_id, "actor", "player", "location_slug",
                 "no-such-place", "test")
    check("apply_effect rejects unknown destination", False)
except TurnFailed:
    check("apply_effect rejects unknown destination", True)
db.close()

print("ALL PASS" if not check.failed else "FAILURES PRESENT")
sys.exit(1 if check.failed else 0)
