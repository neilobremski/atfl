#!/usr/bin/env python3
"""Roster GM adapter demo — server/gm.py's RosterGM behind GameMaster.

Everything is hermetic: a scripted FakeRoster stands in for the a8s
send+poll transport (send_fn records the envelope, poll_fn answers from
it — one script fn serves both halves), so no roster, no subprocess, no
network. All green = the adapter honors the phase4 contract: exact
envelope schema with no hidden-state leak, strict adjudication
validation, narrative word cap, pick validation, async send+poll with a
bounded reply wait plus ONE re-prompt before the loud failure, the
start-send-stop publish discipline enforced in code (§12), and every
roster failure surfacing as a TurnFailed
so dispatch.py's §2.6 retry-once path covers it.

Run from the repo root: python3 prototype/roster_demo.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.config import ConfigError, load as config_load
from server.dispatch import dispatch_message
from server.gm import (ENVELOPE_KEYS, MAX_NARRATIVE_WORDS, RosterGM,
                       build_envelope, game_clock_label)
from server.turn_loop import TurnFailed
import server.gm as gm_mod

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
cfg = config_load({**BASE, "ATFL_GM": "roster",
                   "ATFL_A8S_NODE_ROOT": "/srv/atfl/a8s/atfl-server"})
check("ATFL_GM=roster loads", cfg["gm"] == "roster")
check("roster config carries node name + root",
      cfg["a8s_node"] == "atfl-server"
      and cfg["a8s_node_root"] == "/srv/atfl/a8s/atfl-server")
try:
    config_load({**BASE, "ATFL_GM": "roster"})  # no node root
    refused = False
except ConfigError:
    refused = True
check("ATFL_GM=roster without node root refused at startup", refused)
for bad in ("real", "bogus", "ROSTER "):
    try:
        config_load({**BASE, "ATFL_GM": bad,
                     "ATFL_A8S_NODE_ROOT": "/x"})
        refused = False
    except ConfigError:
        refused = True
    check(f"ATFL_GM={bad!r} refused", refused if bad != "ROSTER " else not refused)

print("\n== 10. async transport: bounded reply wait, adjudication on narrative ==")
# The poll loop never answers -> the bounded wait (+ one bounded
# re-prompt) fires. Small reprompt_wait_s keeps the hermetic run fast.
silent = RosterGM(send_fn=lambda j: None,
                  poll_fn=lambda t, s: None,
                  reply_wait_s=0.05, poll_interval_s=0.05,
                  reprompt_wait_s=0.05)
err = raises_turn_failed(
    lambda: silent.adjudicate("x", {}, context={"game_guid": "g"}))
check("no keeper reply within the waits -> TurnFailed", err is not None
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

print("\n== 11. re-prompt: one bounded nudge before the loud failure ==")
# A silent roster exhausts the first wait -> exactly ONE reprompt goes
# out -> the re-prompt wait also exhausts -> TurnFailed.
silent2 = RosterGM(send_fn=lambda j: None,
                   poll_fn=lambda t, s: None,
                   reply_wait_s=0.05, poll_interval_s=0.05,
                   reprompt_wait_s=0.05)
def reprompt_script(seen):
    def script(payload):
        seen.append(json.loads(payload))
        return None
    return script
seen = []
err = raises_turn_failed(lambda: RosterGM(
    send_fn=reprompt_script(seen), poll_fn=lambda t, s: None,
    reply_wait_s=0.05, poll_interval_s=0.05, reprompt_wait_s=0.05
    ).adjudicate("x", {}, context={"game_guid": "g"}))
check("double timeout -> TurnFailed naming both budgets",
      err is not None and "no roster reply" in err and "re-prompt" in err)
check("exactly two envelopes sent: original then reprompt",
      [e["call"] for e in seen] == ["adjudicate", "reprompt"])
rep = seen[1]
check("reprompt carries exactly the REPROMPT_KEYS",
      set(rep) == set(("call", "game_guid", "turn_no", "game_clock",
                       "original_call", "original_envelope")))
check("reprompt names the original call",
      rep["original_call"] == "adjudicate")
check("reprompt embeds the original envelope verbatim",
      rep["original_envelope"] == seen[0])
check("reprompt leaks no hidden state",
      "hidden_traits" not in json.dumps(rep)
      and "earth-changing" not in json.dumps(rep["original_envelope"]["filtered"]))
# A reply to the re-prompt is accepted and validated like any reply.
def recovery_script(seen):
    def script(payload):
        env = json.loads(payload)
        seen.append(env)
        if env["call"] == "reprompt":
            return json.dumps(GOOD_Q)
        return None  # silent on the original call
    return script
seen2 = []
gm, fake2 = gm_script(recovery_script(seen2), reply_wait_s=0.05,
                      poll_interval_s=0.05, reprompt_wait_s=1)
check("starved call recovered by the re-prompt",
      gm.adjudicate("x", {}, context={}) == GOOD_Q)
check("recovery sent original + reprompt only",
      [e["call"] for e in fake2.sent] == ["adjudicate", "reprompt"])
# reprompt_wait_s=0/None disables the re-prompt: fail loud after one send.
for off in (0, None):
    seen3 = []
    err = raises_turn_failed(lambda s=seen3, o=off: RosterGM(
        send_fn=reprompt_script(s), poll_fn=lambda t, si: None,
        reply_wait_s=0.05, poll_interval_s=0.05,
        reprompt_wait_s=o).pick_plot(["aliens"], context={}))
    check(f"reprompt disabled ({off!r}) -> one send, then TurnFailed",
          err is not None and "no roster reply" in err
          and "re-prompt" not in err and len(seen3) == 1
          and seen3[0]["call"] == "pick_plot")
# Late replies to the ORIGINAL call during the re-prompt wait still count.
def late_reply_script():
    state = {"n": 0}
    def script(payload):
        state["n"] += 1
        return "The fog holds." if state["n"] > 1 else None
    return script
gm, _ = gm_script(late_reply_script(), reply_wait_s=0.05,
                  poll_interval_s=0.05, reprompt_wait_s=1)
check("late original-call reply during re-prompt wait accepted",
      gm.compose_narrative("x", [], {}, "", context={}) == "The fog holds.")

print("\n== 12. start-send-stop: the publish discipline, enforced in code ==")
# _send_real shells out; hermetically we stub subprocess.run and script
# each verb's result. node_root must be a real dir (tempfile).


class FakeRun:
    """Stand-in for subprocess.run: records (argv, kwargs); the script
    returns (rc, stdout, stderr) per call, or raises."""
    def __init__(self, script):
        self.calls = []
        self.script = script

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        outcome = self.script(argv, kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        rc, out, err = outcome
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def send_with_stub(gm, script):
    """Run gm._send_real with a stubbed subprocess; returns (calls, err)
    where calls is the recorded argv/kwargs list and err is the
    TurnFailed message or None."""
    fake = FakeRun(script)
    stub = types.SimpleNamespace(run=fake,
                                 TimeoutExpired=subprocess.TimeoutExpired)
    real = gm_mod.subprocess
    gm_mod.subprocess = stub
    try:
        gm._send_real('{"call":"adjudicate"}')
        err = None
    except TurnFailed as e:
        err = str(e)
    finally:
        gm_mod.subprocess = real
    return fake.calls, err


def ok_script(argv, kwargs):
    return (0, "", "")


def fail_verb(verb):
    def script(argv, kwargs):
        return (1, "", "boom") if argv[1] == verb else (0, "", "")
    return script


ROOT = tempfile.mkdtemp(prefix="atfl-node-")
BIN = gm_mod._a8s_bin()
gm12 = RosterGM(node_root=ROOT)
calls, err = send_with_stub(gm12, ok_script)
check("happy path: start, tell, stop in order",
      [c[0][1] for c in calls] == ["start", "tell", "stop"])
check("all three verbs use the resolved a8s binary",
      all(c[0][0] == BIN for c in calls) and BIN)
check("start/stop name the node, tell names the roster",
      calls[0][0] == [BIN, "start", "atfl-server"]
      and calls[1][0] == [BIN, "tell", "fogline-gm", '{"call":"adjudicate"}']
      and calls[2][0] == [BIN, "stop", "atfl-server"])
check("tell runs with cwd=node_root", calls[1][1].get("cwd") == ROOT)
check("happy path sends with no TurnFailed", err is None)
gm_named = RosterGM(node_name="game-mailbox", node_root=ROOT)
calls, _ = send_with_stub(gm_named, ok_script)
check("node_name override respected by start/stop",
      calls[0][0][2] == "game-mailbox" and calls[2][0][2] == "game-mailbox")
check("node_name defaults to atfl-server", RosterGM().node_name == "atfl-server")
calls, err = send_with_stub(gm12, fail_verb("tell"))
check("tell failure -> TurnFailed naming the tell",
      err is not None and "a8s tell" in err)
check("stop still runs when tell fails (finally)",
      [c[0][1] for c in calls] == ["start", "tell", "stop"])
calls, err = send_with_stub(gm12, fail_verb("start"))
check("start failure -> TurnFailed naming the start, no tell, no stop",
      err is not None and "a8s start" in err
      and [c[0][1] for c in calls] == ["start"])
calls, err = send_with_stub(gm12, fail_verb("stop"))
check("stop failure -> TurnFailed naming the stop (poll would race a daemon)",
      err is not None and "a8s stop" in err and "daemon" in err
      and [c[0][1] for c in calls] == ["start", "tell", "stop"])
calls, err = send_with_stub(
    gm12, lambda argv, kw: subprocess.TimeoutExpired(argv, 60)
    if argv[1] == "tell" else (0, "", ""))
check("tell timeout -> TurnFailed naming the 60s send budget, stop still ran",
      err is not None and "60s" in err
      and [c[0][1] for c in calls] == ["start", "tell", "stop"])
calls, err = send_with_stub(RosterGM(), ok_script)  # node_root unset
check("missing node_root -> TurnFailed before any subprocess runs",
      err is not None and "node_root" in err and calls == [])
calls, err = send_with_stub(
    RosterGM(node_root=os.path.join(ROOT, "no-such-dir")), ok_script)
check("nonexistent node_root dir -> TurnFailed before any subprocess runs",
      err is not None and "node_root" in err and calls == [])
err = raises_turn_failed(
    lambda: RosterGM()._poll_real(1, "2026-01-01T00:00:00Z"))
check("_poll_real without node_root -> TurnFailed (no subprocess)",
      err is not None and "node_root" in err)


def poll_with_stub(gm, script):
    """Run gm._poll_real with a stubbed subprocess; returns (calls, body)."""
    fake = FakeRun(script)
    stub = types.SimpleNamespace(run=fake,
                                 TimeoutExpired=subprocess.TimeoutExpired)
    real = gm_mod.subprocess
    gm_mod.subprocess = stub
    try:
        body = gm._poll_real(5, "2026-09-30T00:00:00Z")
    finally:
        gm_mod.subprocess = real
    return fake.calls, body


def keeper_row(utc, body, frm="fogline-gm:keeper"):
    return json.dumps({"ulid": "01TEST", "seq": 1, "from": frm,
                       "to": "atfl-server", "utc": utc, "content": body})


gm_poll = RosterGM(node_name="atfl-server", node_root=ROOT)
calls, body = poll_with_stub(
    gm_poll, lambda argv, kw: (0, keeper_row("2026-09-30T01:00:50Z",
                                            "earth-changing"), ""))
check("poll reads the NODE mailbox, not the roster thread "
      "(2026-09-29 live bug: convo fogline-gm showed zero rows forever)",
      calls[0][0] == [BIN, "convo", "atfl-server",
                      "--from", "fogline-gm:keeper", "--json", "--limit", "25"]
      and body == "earth-changing")
gm_poll_named = RosterGM(node_name="game-mailbox", node_root=ROOT)
calls, _ = poll_with_stub(
    gm_poll_named, lambda argv, kw: (0, "", ""))
check("poll respects node_name override", calls[0][0][2] == "game-mailbox")
calls, body = poll_with_stub(
    gm_poll, lambda argv, kw: (0, keeper_row("2026-09-29T23:00:00Z",
                                            "earth-changing"), ""))
check("poll ignores keeper rows older than since_iso", body is None)
calls, body = poll_with_stub(
    gm_poll, lambda argv, kw: (0, keeper_row("2026-09-30T01:00:50Z",
                                            "earth-changing",
                                            frm="fogline-gm:critic"), ""))
check("poll ignores rows from other roster members", body is None)
calls, body = poll_with_stub(
    gm_poll, lambda argv, kw: (0, keeper_row("2026-09-30T01:00:50Z", "   "), ""))
check("poll ignores empty keeper rows", body is None)
old_bin = os.environ.get("ATFL_A8S_BIN")
os.environ["ATFL_A8S_BIN"] = "/bin/true"
try:
    check("ATFL_A8S_BIN override respected", gm_mod._a8s_bin() == "/bin/true")
finally:
    if old_bin is None:
        del os.environ["ATFL_A8S_BIN"]
    else:
        os.environ["ATFL_A8S_BIN"] = old_bin

print(f"\nroster demo green — {len(checks)} checks, 0 FAILs.")
