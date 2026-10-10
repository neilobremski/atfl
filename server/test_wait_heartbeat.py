"""Regression test for the 2026-10-09 keeper-wait heartbeat.

Finding (2026-10-08): the engine sat inside one RosterGM._wait with no
poll-cycle logs for ~2h while the roster was merely slow — silence was
indistinguishable from a dead roster. The per-call timeout budget
already existed (REPLY_WAIT_S + REPROMPT_WAIT_S, then a loud
TurnFailed); what was missing was observability.

Fix: _wait prints a "still waiting" heartbeat every WAIT_HEARTBEAT_S of
waiting, and _call prints loud lines at the budget-exhaustion boundary
(re-prompt send, final failure).

Run: python3 server/test_wait_heartbeat.py   (exit 0 = pass)
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.gm import RosterGM, WAIT_HEARTBEAT_S
from server.turn_loop import TurnFailed

FAILS = []


def check(name, cond, note=""):
    print(f"{'PASS' if cond else 'FAIL'} {name}" + (f" — {note}" if note else ""))
    if not cond:
        FAILS.append(name)


def empty_poll(timeout_s, since_iso):
    """A roster that never answers."""
    return []


def one_reply_poll(timeout_s, since_iso):
    """A roster that answers immediately with a good body."""
    return [("2026-10-10T06:30:00Z", '{"q": "q1", "answer": "yes"}')]


def make_gm(**kw):
    return RosterGM(send_fn=lambda env: None, poll_fn=empty_poll,
                    poll_interval_s=0.05, **kw)


# 1. Heartbeat fires during a slow wait, carries the label + numbers.
gm = make_gm()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = gm._wait("2026-10-10T06:00:00Z", 0.6, None, "adjudicate",
                   label="adjudicate game f1fbd54c turn 6", heartbeat_s=0.1)
out = buf.getvalue()
check("slow wait returns None on budget exhaustion", got is None)
check("heartbeat lines printed", "still waiting" in out, f"{out.count('still waiting')} beats")
check("heartbeat carries the label",
      "adjudicate game f1fbd54c turn 6" in out)
check("heartbeat shows elapsed and remaining",
      "elapsed" in out and "left on this" in out)
check("heartbeat names this budget", "0s budget" in out or "left on this" in out)

# 2. No heartbeat when the reply arrives promptly.
gm2 = RosterGM(send_fn=lambda env: None, poll_fn=one_reply_poll,
               poll_interval_s=0.05)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got2 = gm2._wait("2026-10-10T06:00:00Z", 5, None, "adjudicate",
                     label="adjudicate game f1fbd54c turn 6",
                     heartbeat_s=0.01)
out2 = buf.getvalue()
check("prompt reply returned", got2 == '{"q": "q1", "answer": "yes"}')
check("no heartbeat for a fast wait", "still waiting" not in out2)

# 3. heartbeat_s=0 disables the heartbeat.
gm3 = make_gm()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got3 = gm3._wait("2026-10-10T06:00:00Z", 0.3, None, "adjudicate",
                     label="adjudicate game f1fbd54c turn 6", heartbeat_s=0)
check("disabled heartbeat still times out", got3 is None)
check("no beats when disabled", "still waiting" not in buf.getvalue())

# 4. Full _call timeout: loud exhaustion lines, one re-prompt, TurnFailed.
sends = []
gm4 = RosterGM(send_fn=sends.append, poll_fn=empty_poll,
               poll_interval_s=0.05, reply_wait_s=0.3, reprompt_wait_s=0.2)
buf = io.StringIO()
ctx = {"game_guid": "f1fbd54c-9667-4e6f-b053-775a013a13c3", "turn_no": 6,
       "game_clock": "day 1, ~06:00"}
raised = False
with contextlib.redirect_stdout(buf):
    try:
        gm4._call("adjudicate", ctx, player_input="wait", filtered={})
    except TurnFailed:
        raised = True
out4 = buf.getvalue()
check("full timeout raises TurnFailed", raised)
check("re-prompt boundary is loud",
      "WAIT BUDGET EXHAUSTED: adjudicate game f1fbd54c turn 6" in out4, out4[:200])
check("final failure is loud",
      "CALL FAILED: adjudicate game f1fbd54c turn 6" in out4)
check("original send + one re-prompt", len(sends) == 2,
      f"{len(sends)} sends")

# 5. Sanity: the module default is 600s.
check("WAIT_HEARTBEAT_S == 600", WAIT_HEARTBEAT_S == 600)

if FAILS:
    print(f"\n{len(FAILS)} FAILURE(S)")
    sys.exit(1)
print("\nall checks passed")
