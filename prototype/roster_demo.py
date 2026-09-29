#!/usr/bin/env python3
"""Roster GM adapter demo — server/gm.py's RosterGM behind GameMaster.

Everything is hermetic: a scripted FakeRoster stands in for the a8s
send+poll transport (send_fn records the envelope, poll_fn answers from
it — one script fn serves both halves), so no roster, no subprocess, no
network. All green = the adapter honors the phase4 contract: exact
envelope schema with no hidden-state leak, strict adjudication
validation, narrative word cap, pick validation, async send+poll with a
bounded reply wait, and every roster failure surfacing as a TurnFailed
so dispatch.py's §2.6 retry-once path covers it.

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


class FakeRoster:
    """Hermetic stand-in for the async a8s transport: send_fn records
    each envelope, poll_fn answers from the last one via script, so one
    script fn serves both halves of the send+poll contract."""

    def __init__(self, script):
        self.script = script  # script(payload_json_str) -> reply str
        self.sent = []        # decoded envelopes, in send order
        self._last = None

    def send_fn(self, payload):
        self._last = payload
        self.sent.append(json.loads(payload))

    def poll_fn(self, timeout_s, since_iso):
        return self.script(self._last) if self._last is not None else None


def gm_script(script, **kw):
    """RosterGM wired to a scripted FakeRoster; returns (gm, fake)."""
    fake = FakeRoster(script)
    return RosterGM(send_fn=fake.send_fn, poll_fn=fake.poll_fn, **kw), fake


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

gm, _ = gm_script(lambda j: json.dumps(GOOD_Q))
qs = gm.adjudicate("look", {}, context={"game_guid": "g", "turn_no": 2,
                                        "game_clock": "day 1, ~08:00"})
check("valid adjudication parses", qs == GOOD_Q)
gm, _ = gm_script(lambda j: json.dumps(GOOD_FX))
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
    err = raises_turn_failed(lambda r=reply: gm_script(
        lambda j, r=r: r)[0].adjudicate("x", {}, context={}))
    check(f"invalid adjudication -> TurnFailed ({label})", err is not None)

print("\n== 3. pick_plot validation ==")
check("roster pick accepted",
      gm_script(lambda j: "earth-changing\n")[0].pick_plot(
          ["aliens", "earth-changing"], context={}) == "earth-changing")
err = raises_turn_failed(lambda: gm_script(
    lambda j: "vampires")[0].pick_plot(["aliens"], context={}))
check("off-roster pick -> TurnFailed", err is not None)

print("\n== 4. narrative word cap ==")
gm, _ = gm_script(lambda j: "The fog holds.", max_narrative_words=10)
check("short narrative passes",
      gm.compose_narrative("x", [], {}, "", context={}) == "The fog holds.")
err = raises_turn_failed(lambda: gm_script(
    lambda j: "word " * 11, max_narrative_words=10
    )[0].compose_narrative("x", [], {}, "", context={}))
check("over-cap narrative -> TurnFailed", err is not None)
check("production default cap is 2000 words", MAX_NARRATIVE_WORDS == 2000)

print("\n== 5. send/poll failures -> TurnFailed (dispatch retry path covers) ==")
def boom(j):
    raise RuntimeError("roster exploded")
err = raises_turn_failed(lambda: gm_script(boom)[0].adjudicate("x", {}, context={}))
check("send_fn exception -> TurnFailed", err is not None)
gm, _ = gm_script(boom)
try:
    gm.adjudicate("x", {}, context={})
    failed = None
except TurnFailed as e:
    failed = e
check("roster failure IS a TurnFailed (retry-once eligible)",
      isinstance(failed, TurnFailed))

print("\n== 6. end-to-end: scripted roster drives real turns via dispatch ==")
GAMES = tempfile.mkdtemp(prefix="atfl-roster-")


def script(j):
    env = json.loads(j)
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


roster_gm, roster_fake = gm_script(script)
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
      any(e["call"] == "pick_plot" and e["turn_no"] == 1 for e in roster_fake.sent))
check("turn 2 envelopes were adjudicate+narrative with turn_no=2",
      {e["call"] for e in roster_fake.sent if e["turn_no"] == 2}
      == {"adjudicate", "compose_narrative"})
db.close()

print("\n== 7. roster failure -> failed turn, nothing sent, no partial state ==")
bad_gm, _ = gm_script(lambda j: "not json at all")
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
phantom_gm, _ = gm_script(lambda j: json.dumps(HALLUCINATED)
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
leak_gm, _ = gm_script(lambda j:
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

print("\n== 10. async transport: bounded reply wait, adjudication on narrative ==")
# The poll loop never answers -> the bounded wait fires.
silent = RosterGM(send_fn=lambda j: None,
                  poll_fn=lambda t, s: None,
                  reply_wait_s=0.05, poll_interval_s=0.05)
err = raises_turn_failed(
    lambda: silent.adjudicate("x", {}, context={"game_guid": "g"}))
check("no keeper reply within the wait -> TurnFailed", err is not None
      and "no roster reply" in err)
# A poll-side blowup (a8s tells failing) surfaces as TurnFailed too.
def poll_boom(timeout_s, since_iso):
    raise OSError("mailbox unreachable")
err = raises_turn_failed(
    lambda: RosterGM(send_fn=lambda j: None, poll_fn=poll_boom,
                     reply_wait_s=1).pick_plot(["aliens"], context={}))
check("poll_fn exception -> TurnFailed", err is not None)
# compose_narrative carries the validated adjudication array so keeper
# can build prose from it on a fresh a8s thread (correction 4).
QUESTIONS = [{"q": "Does anything change?", "answer": "no",
              "rationale": "the player only looks around", "effect": None}]
gm, fake = gm_script(lambda j: "The fog holds.")
check("narrative round-trips",
      gm.compose_narrative("look", QUESTIONS, {}, "",
                           context={"game_guid": "g"}) == "The fog holds.")
narr_env = fake.sent[-1]
check("narrative envelope carries the adjudication array",
      narr_env["call"] == "compose_narrative"
      and narr_env["adjudication"] == QUESTIONS)
gm, fake = gm_script(lambda j: json.dumps(GOOD_Q))
gm.adjudicate("look", {}, context={})
check("adjudicate/pick_plot envelopes carry an empty adjudication list",
      all(e["adjudication"] == [] for e in fake.sent))

print(f"\nroster demo green — {len(checks)} checks, 0 FAILs.")
