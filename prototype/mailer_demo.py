"""Mailer demo — DESIGN.md §1.1, §5.3 via server/mailer.py's FakeGmail.

End to end with no network: signup -> threaded turn emails ->
clarification (fresh thread) -> failed turn sends nothing ->
attachment logged, never acted on -> standalone-nudge fallback after
24h of silence. All green = the mailer contract holds.
"""
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
body1 = m1.get_content()
guid = [l for l in body1.splitlines() if l.startswith("Game code:")][0].split(": ")[1]
check("turn 1 body has the Game code footer", len(guid) == 36)
check("signup marked read", fake.inbox[0]["id"] in fake.read_ids)
guid8 = guid.replace("-", "")[:8]

# --- 2. reply with GUID: turn 2 continues the same thread ---
turn1_id = fake.outbox[-1]["id"]
fake.queue_inbound(PLAYER, f"Re: [ATFL {guid8}] Above the Fog Line",
                   f"drink\n\nGame code: {guid}",
                   header_message_id="<reply-2@fake>")
res = run_poll_cycle(games_dir, fake, gm, seen_ids=seen)
check("reply produced a turn email", res["sent"][-1]["action"] == "turn_email")
m2 = last_sent()
check("turn 2 In-Reply-To is turn 1's sent id",
      m2["In-Reply-To"] == turn1_id)
check("References chain carries the thread history",
      "<signup-1@fake>" in (m2["References"] or "") and turn1_id in (m2["References"] or ""))
check("subject stays constant for threading",
      str(m2["Subject"]) == str(m1["Subject"]))

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
      len(mn.get_content().split()) <= 120)
turns_after = sqlite3.connect(f"{games_dir}/{guid}.db").execute(
    "SELECT COUNT(*) FROM turns").fetchone()[0]
check("nudge mutated nothing (turn count unchanged)",
      turns_after == turns_n)
nudged2 = maybe_nudge(games_dir, fake)
check("no second nudge within 24h", nudged2 == [])
check("exactly one nudge was sent", len(fake.outbox) == n_before + 1)

print(f"\nmailer demo green: {len(fake.outbox)} outbound, game {guid8}, db in {games_dir}")
