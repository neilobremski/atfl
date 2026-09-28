"""Mailer demo — DESIGN.md §1.1, §5.3 via server/mailer.py's FakeGmail.

End to end with no network: signup -> threaded turn emails ->
clarification (fresh thread) -> failed turn sends nothing ->
attachment logged, never acted on -> standalone-nudge fallback after
24h of silence. All green = the mailer contract holds.
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from server.mailer import (FakeGmail, run_poll_cycle, GAME_ADDRESS,
                           maybe_nudge)
from server.gm import MockGM
from server.turn_loop import TurnFailed

PLAYER = "player@example.com"
games_dir = tempfile.mkdtemp(prefix="atfl-mailer-")
seen = set()
gm = MockGM()


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"demo failed: {name}")


def outbox():
    return fake.outbox


def last_sent():
    return fake.outbox[-1]["parsed"]


def plain(msg):
    """The text/plain part (always the complete message)."""
    return msg.get_body(preferencelist=("plain",)).get_content()


def html(msg):
    """The text/html twin (rich reading layer)."""
    return msg.get_body(preferencelist=("html",)).get_content()


# --- 1. signup: first email starts a game; turn 1 threads to it ---
fake = FakeGmail()
fake.queue_inbound(PLAYER, "start", "start",
                   header_message_id="<signup-1@fake>")
res = run_poll_cycle(games_dir, fake, gm, seen_ids=seen)
check("signup produced one turn_email send",
      len(res["sent"]) == 1 and res["sent"][0]["action"] == "turn_email")
m1 = last_sent()
check("turn 1 threads to the signup email",
      m1["In-Reply-To"] == "<signup-1@fake>")
check("turn 1 subject carries the tag",
      str(m1["Subject"]).startswith("[ATFL "))
body1 = plain(m1)
guid = [l for l in body1.splitlines() if l.startswith("Game code:")][0].split(": ")[1]
check("turn 1 body has the Game code footer", len(guid) == 36)
check("turn 1 has a rich-HTML twin (Neil's 2026-09-27 directive)",
      html(m1) is not None and "<html" in html(m1))
check("HTML twin carries the same facts (narrative + Game code)",
      "Known places" in html(m1) and guid in html(m1))
check("2026-09-27: the open prompt is gone from the plain body",
      "What do you do?" not in body1)
check("2026-09-27: the open prompt is gone from the HTML twin",
      "What do you do?" not in html(m1))
check("no leftover composite marker in the HTML (no image attached)",
      "TURN_COMPOSITE" not in html(m1))
check("signup marked read", fake.inbox[0]["id"] in fake.read_ids)
guid8 = guid.replace("-", "")[:8]

# --- 2. reply with GUID: turn 2 continues the same thread ---
fake.queue_inbound(PLAYER, f"Re: [ATFL {guid8}] Above the Fog Line",
                   f"drink\n\nGame code: {guid}",
                   header_message_id="<reply-2@fake>")
res = run_poll_cycle(games_dir, fake, gm, seen_ids=seen)
check("reply produced a turn email", res["sent"][-1]["action"] == "turn_email")
m2 = last_sent()
check("turn 2 In-Reply-To is turn 1's RFC Message-ID (not the Gmail id)",
      m2["In-Reply-To"] == m1["Message-ID"])
check("References chain carries the thread history",
      "<signup-1@fake>" in (m2["References"] or "")
      and str(m1["Message-ID"]) in (m2["References"] or ""))
check("subject stays constant for threading",
      str(m2["Subject"]) == str(m1["Subject"]))

# --- 2b. §6.3 turn stats: pipeline stats recorded by run_turn, send
# latency filled in by the mailer once each turn email went out ---
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
check("send latency recorded after the turn emails went out",
      all(s["send_ms"] is not None and s["email_sent_at"] for s in stats))
db.close()

# --- 3. unknown GUID: clarification on a FRESH thread ---
n_before = len(fake.outbox)
fake.queue_inbound("stranger@example.com", "hello?",
                   "Game code: 11111111-2222-3333-4444-555555555555",
                   header_message_id="<ambig-3@fake>")
res = run_poll_cycle(games_dir, fake, gm, seen_ids=seen)
check("ambiguous inbound -> clarification", res["sent"][-1]["action"] == "clarify")
mc = last_sent()
check("clarification starts a fresh thread (no In-Reply-To)",
      mc["In-Reply-To"] is None and mc["References"] is None)
check("clarification subject", str(mc["Subject"]) == "[ATFL] Couldn't match your game")
check("clarification has an HTML twin too",
      html(mc) is not None and "match it to a game" in html(mc))
check("only the clarification was added", len(fake.outbox) == n_before + 1)

# --- 4. failed turn: nothing leaves (§2.6) ---
class ExplodingGM(MockGM):
    def compose_narrative(self, *a, **k):
        raise TurnFailed("boom")


n_before = len(fake.outbox)
fake.queue_inbound(PLAYER, f"Re: [ATFL {guid8}] Above the Fog Line",
                   f"look around\n\nGame code: {guid}",
                   header_message_id="<fail-4@fake>")
res = run_poll_cycle(games_dir, fake, ExplodingGM(), seen_ids=seen)
check("failed turn outcome", res["sent"][-1]["action"] == "failed")
check("failed turn sends NOTHING", len(fake.outbox) == n_before)

# --- 5. attachment: logged, never acted on ---
turns_before = sqlite3.connect(f"{games_dir}/{guid}.db").execute(
    "SELECT COUNT(*) FROM turns").fetchone()[0]
fake.queue_inbound(PLAYER, f"Re: [ATFL {guid8}] Above the Fog Line",
                   f"nice view\n\nGame code: {guid}",
                   header_message_id="<att-5@fake>",
                   attachments=[{"filename": "photo.jpg", "size_bytes": 1234}])
res = run_poll_cycle(games_dir, fake, gm, seen_ids=seen)
check("turn still ran with the attachment", res["sent"][-1]["action"] == "turn_email")
db = sqlite3.connect(f"{games_dir}/{guid}.db")
logged = db.execute(
    "SELECT new_value, cause FROM mutations WHERE field='attachment_received'"
).fetchall()
db.close()
check("attachment logged in mutations", len(logged) == 1 and "photo.jpg" in logged[0][0])
check("attachment cause says never acted on", "never acted on" in logged[0][1])

# --- 6. nudge fallback: 25h of silence -> one system nudge, thread reply ---
turns_n = sqlite3.connect(f"{games_dir}/{guid}.db").execute(
    "SELECT COUNT(*) FROM turns").fetchone()[0]
old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
db = sqlite3.connect(f"{games_dir}/{guid}.db")
db.execute("UPDATE games SET last_email_at=? WHERE guid=?", (old, guid))
db.commit()
db.close()
n_before = len(fake.outbox)
nudged = maybe_nudge(games_dir, fake)
check("nudge fired after 24h of silence",
      len(nudged) == 1 and nudged[0]["guid"] == guid)
mn = last_sent()
check("nudge is a thread reply, not a fresh thread",
      mn["In-Reply-To"] is not None)
check("nudge is short (<=120 words per §2.4)",
      len(plain(mn).split()) <= 120)
# 2026-09-27: the nudge fires when the pipeline is unverified, so it
# must never assert anything about current world state (the old copy
# claimed "nothing has changed" — false after an idle turn's default
# mutations). Denylist pins the structural rule, not the prose.
check("nudge asserts nothing about world state",
      not any(p in plain(mn).lower() for p in
              ["nothing has changed", "exactly where you left", "unchanged",
               "untouched", "the fog is"]))
check("nudge points at the last turn email (latest confirmed view)",
      "last turn email" in plain(mn))
check("nudge has an HTML twin", html(mn) is not None
      and "your game is waiting" in html(mn))
turns_after = sqlite3.connect(f"{games_dir}/{guid}.db").execute(
    "SELECT COUNT(*) FROM turns").fetchone()[0]
check("nudge mutated nothing (turn count unchanged)",
      turns_after == turns_n)
nudged2 = maybe_nudge(games_dir, fake)
check("no second nudge within 24h", nudged2 == [])
check("exactly one nudge was sent", len(fake.outbox) == n_before + 1)

print(f"\nmailer demo green: {len(fake.outbox)} outbound, game {guid8}, db in {games_dir}")
