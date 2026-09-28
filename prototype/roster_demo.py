#!/usr/bin/env python3
"""Roster GM adapter demo — server/gm.py's RosterGM behind GameMaster.

Everything is hermetic: a scripted tell_fn stands in for `r4t tell
fogline-gm`, so no roster, no subprocess, no network. All green = the
adapter honors the phase4 contract: exact envelope schema with no
hidden-state leak, strict adjudication validation, narrative word cap,
pick validation, and every roster failure surfacing as a TurnFailed so
dispatch.py's §2.6 retry-once path covers it.

Run from the repo root: python3 prototype/roster_demo.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.config import ConfigError, load as config_load
from server.dispatch import dispatch_message
from server.gm import (ENVELOPE_KEYS, MAX_NARRATIVE_WORDS, RosterGM,
                       build_envelope, game_clock_label)
from server.turn_loop import TurnFailed

BASE = {"ATFL_GAME_ADDRESS": "fogline@game.example"}

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"demo failed: {name}")


def raises_turn_failed(fn):
    try:
        fn()
    except TurnFailed as e:
        return str(e)
    return None


print("== 1. envelope schema: exact keys, no hidden-state leak ==")
env = build_envelope("adjudicate", "g-1", 7, "day 1, ~13:00", "I drink",
                     {"objects": {"water-bottle": {"physical_state": "{\"water_ml\": 500}"}}},
                     ["aliens", "earth-changing"], "While you were quiet: …")
check("envelope has exactly the ENVELOPE_KEYS", set(env) == set(ENVELOPE_KEYS))
check("plot pick not embedded in envelope values",
      "earth-changing" not in json.dumps(env["filtered"]))
check("no hidden_traits anywhere in envelope", "hidden_traits" not in json.dumps(env))
check("player input verbatim", env["player_input"] == "I drink")
check("catchup carried", env["catchup"].startswith("While you were quiet"))
check("call/guid/turn/clock carried",
      (env["call"], env["game_guid"], env["turn_no"], env["game_clock"])
      == ("adjudicate", "g-1", 7, "day 1, ~13:00"))
check("clock label day 1 midnight", game_clock_label(0) == "day 1, ~00:00")
check("clock label day 1 01:00", game_clock_label(60) == "day 1, ~01:00")
check("clock label day 2 midnight", game_clock_label(1440) == "day 2, ~00:00")

print("\n== 2. adjudication validation (scripted roster) ==")
GOOD_Q = [{"q": "Does anything change?", "answer": "no",
           "rationale": "the player only looks around", "effect": None}]
GOOD_FX = [{"q": "Can the player drink?", "answer": "yes",
            "rationale": "bottle holds water",
            "effect": {"object:water-bottle": {"physical_state.water_ml": 350}}}]

gm = RosterGM(tell_fn=lambda j: json.dumps(GOOD_Q))
qs = gm.adjudicate("look", {}, context={"game_guid": "g", "turn_no": 2,
                                        "game_clock": "day 1, ~08:00"})
check("valid adjudication parses", qs == GOOD_Q)
gm = RosterGM(tell_fn=lambda j: json.dumps(GOOD_FX))
check("yes+effect shape parses",
      gm.adjudicate("x", {}, context={})[0]["effect"]["object:water-bottle"]
      ["physical_state.water_ml"] == 350)

for label, reply in [
        ("malformed JSON", "sure, the player can drink!"),
        ("JSON but not a list", json.dumps({"q": "x"})),
        ("empty list", "[]"),
        ("answer not yes/no", json.dumps([{"q": "x", "answer": "maybe",
                                           "rationale": "r", "effect": None}])),
        ("item not a dict", json.dumps(["no"])),
        ("missing q/rationale", json.dumps([{"answer": "no", "effect": None}])),
        ("bad effect target", json.dumps([{"q": "x", "answer": "yes",
                                           "rationale": "r",
                                           "effect": {"moon:crater": {}}}])),
        ("effect key outside physical_state",
         json.dumps([{"q": "x", "answer": "yes", "rationale": "r",
                      "effect": {"object:water-bottle": {"hp": 0}}}]))]:
    err = raises_turn_failed(lambda r=reply: RosterGM(
        tell_fn=lambda j, r=r: r).adjudicate("x", {}, context={}))
    check(f"invalid adjudication -> TurnFailed ({label})", err is not None)

print("\n== 3. pick_plot validation ==")
check("roster pick accepted",
      RosterGM(tell_fn=lambda j: "earth-changing\n").pick_plot(
          ["aliens", "earth-changing"], context={}) == "earth-changing")
err = raises_turn_failed(lambda: RosterGM(
    tell_fn=lambda j: "vampires").pick_plot(["aliens"], context={}))
check("off-roster pick -> TurnFailed", err is not None)

print("\n== 4. narrative word cap ==")
gm = RosterGM(tell_fn=lambda j: "The fog holds.", max_narrative_words=10)
check("short narrative passes",
      gm.compose_narrative("x", [], {}, "", context={}) == "The fog holds.")
err = raises_turn_failed(lambda: RosterGM(
    tell_fn=lambda j: "word " * 11, max_narrative_words=10
    ).compose_narrative("x", [], {}, "", context={}))
check("over-cap narrative -> TurnFailed", err is not None)
check("production default cap is 2000 words", MAX_NARRATIVE_WORDS == 2000)

print("\n== 5. tell failures -> TurnFailed (dispatch retry path covers) ==")
def boom(j):
    raise RuntimeError("roster exploded")
err = raises_turn_failed(lambda: RosterGM(tell_fn=boom).adjudicate("x", {}, context={}))
check("tell_fn exception -> TurnFailed", err is not None)
gm = RosterGM(tell_fn=boom)
try:
    gm.adjudicate("x", {}, context={})
    failed = None
except TurnFailed as e:
    failed = e
check("roster failure IS a TurnFailed (retry-once eligible)",
      isinstance(failed, TurnFailed))

print("\n== 6. end-to-end: scripted roster drives real turns via dispatch ==")
GAMES = tempfile.mkdtemp(prefix="atfl-roster-")
seen_envelopes = []


def script(j):
    env = json.loads(j)
    seen_envelopes.append(env)
    call = env["call"]
    if call == "pick_plot":
        return "earth-changing"
    if call == "adjudicate":
        if "drink" in env["player_input"].lower():
            return json.dumps([{
                "q": "Can the player drink from the bottle?",
                "answer": "yes",
                "rationale": "bottle holds water",
                "effect": {"object:water-bottle": {"physical_state.water_ml": 350},
                           "actor:player": {"physical_state.hunger": 0.1}}}])
        return json.dumps([{"q": "Does anything change?", "answer": "no",
                            "rationale": "the player only looks around",
                            "effect": None}])
    return "Below, the fog does not move."


roster_gm = RosterGM(tell_fn=script)
o = dispatch_message(GAMES, "neil@example.com", "start", "I want to play.", roster_gm)
check("turn 1 via roster pick -> turn_email", o.action == "turn_email" and o.turn_no == 1)
guid = o.guid
db = sqlite3.connect(os.path.join(GAMES, f"{guid}.db"))
db.row_factory = sqlite3.Row
check("roster plot pick landed in DB",
      dict(db.execute("SELECT * FROM games").fetchone())["plot_concept"] == "earth-changing")
db.close()
o = dispatch_message(GAMES, "neil@example.com", f"Re: [ATFL {guid[:8]}]",
                     "I drink from the bottle." + f"\n\nGame code: {guid}", roster_gm)
check("turn 2 adjudication+effect via roster", o.action == "turn_email" and o.turn_no == 2)
check("turn 2 email carries roster narrative",
      "Below, the fog does not move." in o.body)
db = sqlite3.connect(os.path.join(GAMES, f"{guid}.db"))
db.row_factory = sqlite3.Row
check("roster-proposed mutation committed with cause",
      [dict(r) for r in db.execute(
          "SELECT * FROM mutations WHERE cause LIKE 'mutation Q:%'")] != [])
check("turn 1 envelope call was pick_plot",
      any(e["call"] == "pick_plot" and e["turn_no"] == 1 for e in seen_envelopes))
check("turn 2 envelopes were adjudicate+narrative with turn_no=2",
      {e["call"] for e in seen_envelopes if e["turn_no"] == 2}
      == {"adjudicate", "compose_narrative"})
db.close()

print("\n== 7. roster failure -> failed turn, nothing sent, no partial state ==")
bad_gm = RosterGM(tell_fn=lambda j: "not json at all")
o = dispatch_message(GAMES, "neil@example.com", f"Re: [ATFL {guid[:8]}]",
                     "I look around." + f"\n\nGame code: {guid}", bad_gm)
check("malformed roster output -> failed outcome", o.action == "failed")
check("failure note names the retry", "twice" in o.note)
db = sqlite3.connect(os.path.join(GAMES, f"{guid}.db"))
db.row_factory = sqlite3.Row
check("game still on turn 2 after failed turn 3",
      dict(db.execute("SELECT * FROM games").fetchone())["turn_no"] == 2)
db.close()

print("\n== 7b. hallucinated entity slug -> TurnFailed, retry, failed turn ==")
# Case 2 of research/phase4-truth-rule-worked-examples.md: the roster
# answers "yes" with an effect on a slug that doesn't exist. Shape
# validation passes; the commit-side existence check (apply_effect)
# must raise TurnFailed so the §2.6 path honors it.
HALLUCINATED = [{
    "q": "Does Mara give the player her lantern?", "answer": "yes",
    "rationale": "she's grateful for the water you shared",
    "effect": {"object:mara-lantern": {"physical_state.owner": "player"}}}]
phantom_gm = RosterGM(tell_fn=lambda j: json.dumps(HALLUCINATED)
                      if json.loads(j)["call"] == "adjudicate"
                      else "Mara hands you the lantern.")
db = sqlite3.connect(os.path.join(GAMES, f"{guid}.db"))
db.row_factory = sqlite3.Row
n_mut_before = db.execute("SELECT COUNT(*) FROM mutations").fetchone()[0]
n_turns_before = db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
db.close()
o = dispatch_message(GAMES, "neil@example.com", f"Re: [ATFL {guid[:8]}]",
                     "Mara hands me her lantern." + f"\n\nGame code: {guid}",
                     phantom_gm)
check("hallucinated slug -> failed outcome", o.action == "failed")
check("failure note names the retry", "twice" in o.note)
db = sqlite3.connect(os.path.join(GAMES, f"{guid}.db"))
db.row_factory = sqlite3.Row
check("game still on turn 2 after phantom-slug turn",
      dict(db.execute("SELECT * FROM games").fetchone())["turn_no"] == 2)
check("no new turn row committed",
      db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == n_turns_before)
check("no ledger rows from the phantom turn",
      db.execute("SELECT COUNT(*) FROM mutations").fetchone()[0] == n_mut_before)
check("bottle water_ml untouched",
      json.loads(dict(db.execute(
          "SELECT * FROM objects WHERE slug='water-bottle'").fetchone())
                 ["physical_state"])["water_ml"] == 350)
db.close()

print("\n== 8. roster narrative still passes the secrecy check ==")
leak_gm = RosterGM(tell_fn=lambda j:
                   "You feel the earth changing all around you.")
o = dispatch_message(GAMES, "neil@example.com", f"Re: [ATFL {guid[:8]}]",
                     "I look around." + f"\n\nGame code: {guid}", leak_gm)
check("leaked plot concept -> failed outcome, nothing sent", o.action == "failed")

print("\n== 9. config: roster backend selectable, mock stays default ==")
cfg = config_load(dict(BASE))
check("default GM backend is mock", cfg["gm"] == "mock")
cfg = config_load({**BASE, "ATFL_GM": "roster"})
check("ATFL_GM=roster loads", cfg["gm"] == "roster")
for bad in ("real", "bogus", "ROSTER "):
    try:
        config_load({**BASE, "ATFL_GM": bad})
        refused = False
    except ConfigError:
        refused = True
    check(f"ATFL_GM={bad!r} refused", refused if bad != "ROSTER " else not refused)

print(f"\nroster demo green — {len(checks)} checks, 0 FAILs.")
