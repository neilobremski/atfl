"""Inbound matching + turn dispatch — DESIGN.md §1.1, §1.2, §1.3, §2.2.

The poller (Phase 2 detail) feeds raw messages in; dispatch figures out
which game each one belongs to and runs turns serially. Email send/receive
is still stubbed (game address = open question #1): dispatch returns
DispatchOutcome objects carrying fully rendered subject/body pairs ready
for the mailer to send.

Matching (§1.1):
  - GUID + sender address is the join key. The short form (first 8 hex)
    is display-only, NEVER a join key.
  - GUID present and joined to this sender  -> the turn runs.
  - GUID present but no such (GUID, sender)  -> clarification, not a guess.
  - No GUID: exactly one active game for the sender -> matched by sender.
    Zero active games -> signup (first email starts a game, §6.2).
    Multiple active games -> clarification.

Turn dispatch (§2.2): turns are serial per game — one threading.Lock per
GUID, held for the whole run_turn. A batch with several messages for the
same game folds the extras into ONE next turn (§1.3.2): the player's
intents are adjudicated fresh as the combined player_input, and the
mutations audit marks each folded message `cause: "late reply to turn N"`.

§2.6: on TurnFailed the turn is retried once with the same inputs, then
the outcome is `failed` and NOTHING is sent — no invented fiction.

Idle sweep (§1.3, §2.3): games with no turn in ~interval_h get one idle
turn ("idle default" — the GM's conservative default, never suicidal).
The idle-turn email doubles as the daily touch; the standalone-nudge
fallback (≥24h with no turn email, mutates nothing) is mailer-level.
"""
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import schema as _schema
from . import seed as _seed
from .render import render_turn_email, render_clarification
from .turn_loop import run_turn, filtered_view, TurnFailed

UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE)

GAME_CODE_RE = re.compile(r"game code\s*:\s*([0-9a-f-]{36})", re.IGNORECASE)

IDLE_INPUT = "idle default"  # the sentinel turn_loop keys catch-up off

# per-game serial-turn locks (§2.2: "turns serial per game")
_locks_guard = threading.Lock()
_game_locks = {}


def get_lock(guid):
    with _locks_guard:
        return _game_locks.setdefault(guid, threading.Lock())


class DispatchOutcome:
    """One inbound message's resolution. action is one of:
      turn_email — a turn ran; send subject/body as the game's turn email
      clarify    — no turn ran; send the fresh-thread clarification email
      failed     — the turn failed twice; send NOTHING (§2.6)
      ignored    — message was a duplicate/empty; send nothing"""
    def __init__(self, action, sender, subject=None, body=None,
                 guid=None, turn_no=None, note=None):
        self.action, self.sender = action, sender
        self.subject, self.body = subject, body
        self.guid, self.turn_no, self.note = guid, turn_no, note


def extract_guid(text):
    """First UUID4 found: prefer the `Game code:` footer, fall back to
    any UUID-shaped string in the body."""
    if not text:
        return None
    m = GAME_CODE_RE.search(text)
    if m:
        return m.group(1).lower()
    m = UUID_RE.search(text)
    return m.group(0).lower() if m else None


def iter_games(games_dir):
    """Yield (guid, games-row) for every <GUID>.db in games_dir."""
    if not os.path.isdir(games_dir):
        return
    for name in sorted(os.listdir(games_dir)):
        if not (name.endswith(".db") and len(name) == 36 + 3):
            continue
        guid = name[:-3]
        db = sqlite3.connect(os.path.join(games_dir, name))
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT * FROM games WHERE guid=?", (guid,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        db.close()
        if row is not None:
            yield guid, dict(row)


def _open_db(games_dir, guid):
    db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
    db.row_factory = sqlite3.Row
    return db


def match_game(games_dir, sender_email, guid=None):
    """-> (kind, guid, reason). kind in {"signup","matched","clarify"}.
    Only status='active' games match; dead games are over forever."""
    sender = sender_email.strip().lower()
    active = [(g, r) for g, r in iter_games(games_dir) if r["status"] == "active"]
    if guid:
        hits = [g for g, r in active
                if g == guid and r["player_email"].strip().lower() == sender]
        if hits:
            return "matched", hits[0], ""
        return ("clarify", None,
                f"No active game for that Game code and this address "
                f"({sender}).")
    mine = [g for g, r in active if r["player_email"].strip().lower() == sender]
    if len(mine) == 1:
        return "matched", mine[0], ""
    if not mine:
        return "signup", None, ""
    return ("clarify", None,
            f"You have {len(mine)} active games on this address. "
            f"Reply with the Game code from the game you mean.")


def _turn_outcome(db, sender, gm, player_input, guid, turn_len_min, late_for=None):
    """Run one turn (with the §2.6 single retry) and render the email."""
    game = dict(db.execute("SELECT * FROM games WHERE guid=?", (guid,)).fetchone())
    for attempt in (1, 2):
        try:
            result = run_turn(db, player_input, gm, turn_len_min)
            break
        except TurnFailed as e:
            if attempt == 2:
                db.rollback()
                return DispatchOutcome("failed", sender, guid=guid,
                                       note=f"turn failed twice, nothing sent: {e}")
    # §1.3.2: each message folded into this turn gets a `late reply` audit row
    if late_for:
        folded = json.dumps([m for m in late_for["inputs"]])[:2000]
        for _m in late_for["inputs"]:
            db.execute(
                "INSERT INTO mutations (turn_id,entity_type,entity_id,field,old_value,new_value,cause)"
                " VALUES (?,?,?,?,?,?,?)",
                (result.turn_id, "game", 0, "folded_player_input", None,
                 folded, f"late reply to turn {late_for['turn_no']}"))
        db.commit()
    view = filtered_view(db, guid)
    subject, body = render_turn_email(guid, view, result)
    return DispatchOutcome("turn_email", sender, subject, body,
                           guid=guid, turn_no=result.turn_no,
                           note=f"late reply folded for {len(late_for['inputs'])} message(s)"
                           if late_for else None)


def new_game(games_dir, sender_email, signup_body, gm, turn_len_min=60):
    """First email starts a game (§6.2): GUID + seeded <GUID>.db, and the
    signup email IS turn 1's player_input (§3.2.6)."""
    guid = str(uuid.uuid4())
    os.makedirs(games_dir, exist_ok=True)
    db = _open_db(games_dir, guid)
    db.executescript(_schema.SCHEMA)
    _seed.seed(db, guid, sender_email.strip().lower())
    with get_lock(guid):
        outcome = _turn_outcome(db, sender_email, gm, signup_body or "hello",
                                guid, turn_len_min)
    db.close()
    outcome.note = (outcome.note + "; " if outcome.note else "") + "new game signup"
    return outcome


def dispatch_message(games_dir, sender_email, subject, body, gm, turn_len_min=60):
    """Route one inbound message. Serial per game; the caller may feed a
    whole poll batch through dispatch_batch for late-reply folding."""
    guid = extract_guid(body) or extract_guid(subject)
    kind, matched_guid, reason = match_game(games_dir, sender_email, guid)
    if kind == "signup":
        return new_game(games_dir, sender_email, body, gm, turn_len_min)
    if kind == "clarify":
        csubj, cbody = render_clarification(reason)
        return DispatchOutcome("clarify", sender_email, csubj, cbody,
                               note=f"ambiguous inbound: {reason}")
    db = _open_db(games_dir, matched_guid)
    try:
        with get_lock(matched_guid):
            outcome = _turn_outcome(db, sender_email, gm, body or "(empty)",
                                    matched_guid, turn_len_min)
    finally:
        db.close()
    return outcome


def dispatch_batch(games_dir, messages, gm, turn_len_min=60):
    """Process one poll batch in arrival order. The first message per game
    runs a normal turn; further same-game messages in the same batch are
    late replies (§1.3.2) — folded into ONE next turn, adjudicated fresh,
    audit-marked `late reply to turn N`."""
    outcomes = []
    seen = {}  # game-guid -> {"turn_no": int, "inputs": [str]}
    for msg in messages:
        guid = extract_guid(msg.get("body")) or extract_guid(msg.get("subject"))
        kind, matched_guid, _ = match_game(games_dir, msg["sender"], guid)
        if kind != "matched":
            outcomes.append(dispatch_message(games_dir, msg["sender"],
                                             msg.get("subject", ""), msg.get("body", ""),
                                             gm, turn_len_min))
            continue
        if matched_guid not in seen:
            out = dispatch_message(games_dir, msg["sender"], msg.get("subject", ""),
                                   msg.get("body", ""), gm, turn_len_min)
            outcomes.append(out)
            seen[matched_guid] = {"turn_no": out.turn_no or 0,
                                  "inputs": [], "first_outcome": out}
        else:
            seen[matched_guid]["inputs"].append(msg.get("body", "") or "(empty)")
    # fold each game's late messages into one next turn
    for guid, st in seen.items():
        if not st["inputs"]:
            continue
        first = st["first_outcome"]
        if first.action != "turn_email":
            outcomes.append(DispatchOutcome(
                "failed", "", guid=guid,
                note="first turn failed; late messages dropped (nothing invented)"))
            continue
        db = _open_db(games_dir, guid)
        try:
            with get_lock(guid):
                folded_input = "\n---\n".join(st["inputs"])
                outcome = _turn_outcome(db, first.sender, gm, folded_input, guid,
                                        turn_len_min,
                                        late_for={"turn_no": st["turn_no"],
                                                  "inputs": st["inputs"]})
        finally:
            db.close()
        outcomes.append(outcome)
    return outcomes


def sweep_idle(games_dir, gm, interval_h=24, turn_len_min=60):
    """Run one idle turn per game with no turn in the last interval_h
    (§1.3, §2.3). The idle-turn email is the daily touch; the standalone
    nudge fallback (mutates nothing) is mailer-level."""
    outcomes = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=interval_h)).isoformat()
    for guid, game in iter_games(games_dir):
        if game["status"] != "active":
            continue
        db = _open_db(games_dir, guid)
        try:
            latest = db.execute("SELECT MAX(created_at) FROM turns").fetchone()[0]
            if latest and latest >= cutoff:
                continue
            with get_lock(guid):
                outcomes.append(_turn_outcome(db, game["player_email"], gm,
                                              IDLE_INPUT, guid, turn_len_min))
        finally:
            db.close()
    return outcomes
