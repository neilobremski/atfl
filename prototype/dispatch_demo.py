#!/usr/bin/env python3
"""Above the Fog Line — dispatch driver (Phase 2, session #10).

Exercises server.dispatch: inbound matching (§1.1), signup, turn
dispatch, batch late-reply folding (§1.3.2), clarification on ambiguity,
and the idle-turn sweep (§1.3/§2.3) — with rendered §5.2 turn emails.

Run from the repo root: python3 prototype/dispatch_demo.py
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.dispatch import (dispatch_message, dispatch_batch, sweep_idle,
                             extract_guid, match_game)
from server.gm import MockGM

GAMES = tempfile.mkdtemp(prefix="atfl-games-")
gm = MockGM()
guid = None


def show(out, label):
    print(f"--- {label}: action={out.action} guid={str(out.guid)[:8] if out.guid else None} "
          f"turn={out.turn_no} note={out.note}")
    if out.subject:
        print(f"    subject: {out.subject}")
    if out.body:
        for line in out.body.splitlines()[:4]:
            print(f"    | {line[:70]}")
    return out


def footer(g):
    return f"\n\nGame code: {g}"


print("== 1. signup (first email, no GUID) -> new game, turn 1 ==")
o = show(dispatch_message(GAMES, "neil@example.com", "start",
                          "I want to play.", gm), "signup")
assert o.action == "turn_email" and o.turn_no == 1
guid = o.guid
assert "While you were quiet" not in o.body  # no catch-up on turn 1
assert o.body.strip().endswith(f"Turn 1 · Day 1, 07:00")

print("\n== 2. GUID+sender reply -> turn 2 ==")
o = show(dispatch_message(GAMES, "neil@example.com", f"Re: [ATFL {guid[:8]}]",
                          "I pick up the bottle and drink." + footer(guid), gm), "turn 2")
assert o.action == "turn_email" and o.turn_no == 2

print("\n== 3. unknown GUID -> clarification, no guess ==")
o = show(dispatch_message(GAMES, "neil@example.com", "Re: game",
                          "go down" + footer("00000000-0000-4000-8000-000000000000"), gm),
         "bad guid")
assert o.action == "clarify" and o.turn_no is None
assert "Game code" in o.body

print("\n== 4. no GUID, sender has exactly one game -> matched by sender ==")
o = show(dispatch_message(GAMES, "neil@example.com", "lost my code",
                          "I look around.", gm), "sender match")
assert o.action == "turn_email" and o.turn_no == 3

print("\n== 5. batch: two same-game messages -> one folded late turn ==")
o1 = show(dispatch_message(GAMES, "neil@example.com", "a",
                           "I sit down." + footer(guid), gm), "batch base")
turn_before = o1.turn_no
batch = [
    {"sender": "neil@example.com", "subject": "b1", "body": "I stand up." + footer(guid)},
    {"sender": "neil@example.com", "subject": "b2", "body": "I check my cheek." + footer(guid)},
    {"sender": "neil@example.com", "subject": "b3", "body": "Is the fog moving?" + footer(guid)},
]
outs = dispatch_batch(GAMES, batch, gm)
for i, out in enumerate(outs):
    show(out, f"batch[{i}]")
turns = [x.turn_no for x in outs if x.action == "turn_email"]
assert len(turns) == 2, f"expected 2 turns (first + one folded), got {turns}"
db = sqlite3.connect(os.path.join(GAMES, f"{guid}.db"))
db.row_factory = sqlite3.Row
late = db.execute("SELECT cause FROM mutations WHERE cause LIKE 'late reply to turn %'").fetchall()
assert len(late) == 2, f"expected 2 late-reply audit rows, got {len(late)}"
assert all(f"late reply to turn {turn_before + 1}" in r["cause"] for r in late)
narr = db.execute("SELECT narrative FROM turns ORDER BY turn_no DESC LIMIT 1").fetchone()[0]
db.close()
print(f"    late audit rows: {[r['cause'] for r in late]}")

print("\n== 6. idle sweep: stale game gets one GM-driven idle turn ==")
db = sqlite3.connect(os.path.join(GAMES, f"{guid}.db"))
db.execute("UPDATE turns SET created_at='2026-09-20T00:00:00+00:00'")
db.commit()
db.close()
outs = sweep_idle(GAMES, gm, interval_h=24)
assert len(outs) == 1, f"expected 1 idle turn, got {len(outs)}"
o = show(outs[0], "idle")
assert o.action == "turn_email"
db = sqlite3.connect(os.path.join(GAMES, f"{guid}.db"))
idle_input = db.execute(
    "SELECT player_input FROM turns ORDER BY turn_no DESC LIMIT 1").fetchone()[0]
db.close()
assert idle_input == "idle default", f"idle turn should carry the GM-default sentinel, got {idle_input!r}"
# no catch-up lead here is CORRECT: turn 6 was real player input, so zero
# missed frames; the catch-up path itself was proved positive in session #9.

print("\n== 7. extract_guid / match_game unit checks ==")
assert extract_guid("Game code: " + guid) == guid
assert extract_guid("no code here") is None
k, g, _ = match_game(GAMES, "stranger@example.com")
assert k == "signup"
k, g, _ = match_game(GAMES, "neil@example.com")
assert k == "matched" and g == guid
k, _, reason = match_game(GAMES, "neil@example.com", "ffffffff-ffff-4fff-bfff-ffffffffffff")
assert k == "clarify" and "No active game" in reason

shutil.rmtree(GAMES, ignore_errors=True)
print("\nOK — dispatch green: matching, signup, batch folding, clarify, idle sweep.")
