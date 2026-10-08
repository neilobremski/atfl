"""Mailer demo — the relay-edition turn pipeline (server/mailer.py).

Relay edition (2026-10-02): the engine builds no MIME and touches no
mailbox. Each inbound (a Murph forward the poller read from the
engine's A8S inbox) runs the dispatch -> turn pipeline, and the
outcome is handed to Murph as an atfl_outbound envelope via the relay
stand-in (FakeGmail — the name is historical). DESIGN.md §1.1, §5.3
hold end to end with no network: signup -> threaded turn envelopes ->
clarification (fresh-thread advisory) -> failed turn sends nothing ->
standalone-nudge fallback after 24h of silence. All green = the
relay mailer contract holds.

Run from the repo root: python3 prototype/mailer_demo.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from server.mailer import FakeGmail, run_poll_cycle, maybe_nudge
from server.murph_relay import (OUTBOUND_FIELDS, envelope_for_wire,
                                envelope_from_wire)
from server.gm import MockGM
from server.turn_loop import TurnFailed

ENGINE_NODE = "atfl-server"
MURPH_NODE = "murph"
NODE_ROOT = tempfile.mkdtemp(prefix="atfl-node-")

PLAYER = "player@example.com"
games_dir = tempfile.mkdtemp(prefix="atfl-mailer-")
seen = set()
gm = MockGM()


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"demo failed: {name}")


def cycle(**kw):
    """One relay cycle with the Murph-side stand-in."""
    return run_poll_cycle(games_dir, gm, ENGINE_NODE, MURPH_NODE,
                          NODE_ROOT, seen_ids=seen, relay=fake, **kw)


def envelope():
    """The last outbound envelope the relay handed to Murph."""
    return fake.outbox[-1]["envelope"]


# --- 1. signup: first forward starts a game; turn 1 envelopes to Murph ---
fake = FakeGmail(murph_node=MURPH_NODE, engine_node=ENGINE_NODE)
mid1 = fake.queue_inbound(PLAYER, "start", "start")
res = cycle()
check("signup produced one turn_email send",
      len(res["sent"]) == 1 and res["sent"][0]["action"] == "turn_email")
check("signup handed the outcome to the murph node",
      res["sent"][0]["handoff"] is True)
e1 = envelope()
check("envelope carries exactly the pinned outbound fields",
      set(e1) == set(OUTBOUND_FIELDS))
check("envelope: atfl_outbound kind, turn 1, to the player",
      e1["kind"] == "atfl_outbound" and e1["turn_no"] == 1
      and e1["to"] == PLAYER)
guid = e1["game_guid"]
check("game guid is a 36-char uuid", len(guid) == 36)
check("turn 1 subject carries the tag", e1["subject"].startswith("[ATFL "))
check("turn 1 body has the Game code footer", f"Game code: {guid}" in e1["body_text"])
check("turn 1 has a rich-HTML twin (Neil's 2026-09-27 directive)",
      e1["body_html"] is not None and "<html" in e1["body_html"])
check("HTML twin carries the same facts (narrative + Game code)",
      "Known places" in e1["body_html"] and guid in e1["body_html"])
check("2026-09-27: the open prompt is gone from the plain body",
      "What do you do?" not in e1["body_text"])
check("2026-09-27: the open prompt is gone from the HTML twin",
      "What do you do?" not in e1["body_html"])
check("no composite: images off -> no attachments, no marker",
      e1["attachments"] == [] and "cid:composite" not in e1["body_html"])
wire = envelope_for_wire(e1)
back = envelope_from_wire(wire)
check("envelope wire-round-trips (JSON text for `a8s tell`)",
      back["game_guid"] == guid and back["turn_no"] == 1
      and back["attachments"] == [])
check("signup forward consumed from the inbox",
      fake.inbox == [] and any(m["id"] == mid1 for m in fake.trash))
guid8 = guid.replace("-", "")[:8]

# --- 2. reply with GUID: turn 2 envelopes to the same game ---
fake.queue_inbound(PLAYER, f"Re: [ATFL {guid8}] Above the Fog Line",
                   f"drink\n\nGame code: {guid}")
res = cycle()
check("reply produced a turn email", res["sent"][-1]["action"] == "turn_email")
e2 = envelope()
check("turn 2 same game, turn_no 2", e2["game_guid"] == guid and e2["turn_no"] == 2)
check("subject stays constant across turns", e2["subject"] == e1["subject"])

# --- 2b. §6.3 turn stats: pipeline stats recorded by run_turn, send
# latency filled in by the mailer once each outcome was handed off ---
db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
db.row_factory = sqlite3.Row
stats = [dict(r) for r in db.execute("SELECT * FROM turn_stats ORDER BY turn_id")]
check("turn_stats rows exist for both turns", len(stats) == 2)
check("secrecy check recorded as pass", all(s["secrecy_pass"] == 1 for s in stats))
check("mutations count matches the ledger",
      all(s["mutations_count"] == db.execute(
          "SELECT COUNT(*) FROM mutations WHERE turn_id=?",
          (s["turn_id"],)).fetchone()[0] for s in stats))
check("GM latencies recorded (adjudicate + narrative)",
      all((s["adjudicate_ms"] or 0) >= 0 and (s["narrative_ms"] or 0) >= 0
          for s in stats))
check("send latency recorded after the turn outcomes went out",
      all(s["send_ms"] is not None and s["email_sent_at"] for s in stats))
db.close()

# --- 3. unknown GUID: clarification (fresh thread advisory) ---
n_before = len(fake.outbox)
mid3 = fake.queue_inbound("stranger@example.com", "hello?",
                          "Game code: 11111111-2222-3333-4444-555555555555")
res = cycle()
check("ambiguous inbound -> clarification", res["sent"][-1]["action"] == "clarify")
mc = envelope()
check("clarification rides the murph node", res["sent"][-1]["handoff"] is True)
check("clarification asks for a fresh thread",
      mc.get("fresh_thread") is True)
check("clarification turn_no is a stable per-forward id",
      mc["turn_no"] == f"clarify-{mid3}")
check("clarification subject", mc["subject"] == "[ATFL] Couldn't match your game")
check("clarification has an HTML twin too",
      mc["body_html"] is not None and "match it to a game" in mc["body_html"])
check("only the clarification was added", len(fake.outbox) == n_before + 1)

# --- 4. failed turn: nothing leaves (§2.6) ---
class ExplodingGM(MockGM):
    def compose_narrative(self, *a, **k):
        raise TurnFailed("boom")


n_before = len(fake.outbox)
fake.queue_inbound(PLAYER, f"Re: [ATFL {guid8}] Above the Fog Line",
                   f"look around\n\nGame code: {guid}")
res2 = run_poll_cycle(games_dir, ExplodingGM(), ENGINE_NODE, MURPH_NODE,
                      NODE_ROOT, seen_ids=seen, relay=fake)
check("failed turn outcome", res2["sent"][-1]["action"] == "failed")
check("failed turn sends NOTHING", len(fake.outbox) == n_before)

# --- 5. standalone nudge: 24h of silence -> one system nudge ---
# (relay era: Murph forwards the nudge on the game thread; threading is
# Murph's job, so the demo pins the envelope shape, not MIME headers.)
turns_n = sqlite3.connect(f"{games_dir}/{guid}.db").execute(
    "SELECT COUNT(*) FROM turns").fetchone()[0]
old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
db = sqlite3.connect(f"{games_dir}/{guid}.db")
db.execute("UPDATE games SET last_email_at=? WHERE guid=?", (old, guid))
db.commit()
db.close()
n_before = len(fake.outbox)
nudged = maybe_nudge(games_dir, MURPH_NODE, NODE_ROOT, relay=fake)
check("nudge fired after 24h of silence",
      len(nudged) == 1 and nudged[0]["guid"] == guid
      and nudged[0]["handoff"] is True)
mn = envelope()
check("nudge envelopes to the player, no turn number",
      mn["to"] == PLAYER
      and mn["turn_no"] == "nudge-" + datetime.now(timezone.utc).strftime("%Y-%m-%d"))
check("nudge is short (<=120 words per §2.4)",
      len(mn["body_text"].split()) <= 120)
# 2026-09-27: the nudge fires when the pipeline is unverified, so it
# must never assert anything about current world state (the old copy
# claimed "nothing has changed" — false after an idle turn's default
# mutations). Denylist pins the structural rule, not the prose.
check("nudge asserts nothing about world state",
      not any(p in mn["body_text"].lower() for p in
              ["nothing has changed", "exactly where you left", "unchanged",
               "untouched", "the fog is"]))
check("nudge points at the last turn email (latest confirmed view)",
      "last turn email" in mn["body_text"])
check("nudge has an HTML twin", mn["body_html"] is not None
      and "your game is waiting" in mn["body_html"])
turns_after = sqlite3.connect(f"{games_dir}/{guid}.db").execute(
    "SELECT COUNT(*) FROM turns").fetchone()[0]
check("nudge mutated nothing (turn count unchanged)",
      turns_after == turns_n)
nudged2 = maybe_nudge(games_dir, MURPH_NODE, NODE_ROOT, relay=fake)
check("no second nudge within 24h", nudged2 == [])
check("exactly one nudge was handed off", len(fake.outbox) == n_before + 1)

print(f"\nmailer demo green: {len(fake.outbox)} envelopes handed off, "
      f"game {guid8}, db in {games_dir}")
