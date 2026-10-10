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

# --- BUG: reports (docs/playtest-bug-handling.md) ---------------------------
# A BUG:-flagged message is a Murph handoff, never player input. The
# keyword sits at the start of the subject or the first line of the body.
BUG_RE = re.compile(r"^\s*bug\s*:", re.IGNORECASE)
# Turn emails arrive as replies, so the player's subject carries Re:
# prefixes the spec's "start of the subject" would otherwise miss.
_RE_PREFIX_RE = re.compile(r"^\s*(re\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


def is_bug_report(subject, body):
    """True when the message carries the BUG: flag (player protocol).

    Forgiving by design: case-insensitive, leading whitespace allowed,
    Re:/Re[n]: prefixes stripped from the subject, blank body lines
    before the first line skipped. A "bug:" later in the subject or on a
    later body line is NOT a flag — the marker must lead."""
    subj = _RE_PREFIX_RE.sub("", subject or "").strip()
    if BUG_RE.match(subj):
        return True
    for line in (body or "").splitlines():
        if line.strip():
            return bool(BUG_RE.match(line))
    return False


BUGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bugs (
    id INTEGER PRIMARY KEY,
    game_guid TEXT,                 -- NULL when no game matched the report
    reporter TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    reported_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | closed
    resolution_note TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bugs_game ON bugs(game_guid);
"""


def ensure_bugs_table(db):
    """Idempotent: fresh DBs get it from schema.py, pre-existing DBs get
    it here on first touch (no migration step; 0-game fleets are fine)."""
    db.executescript(BUGS_SCHEMA)


def report_bug(db, game_guid, reporter, subject, body):
    """File one open bug report; returns the bug id. The game is paused
    the moment a row is open (see game_paused) — no state change is
    needed beyond the row itself."""
    cur = db.execute(
        "INSERT INTO bugs (game_guid, reporter, subject, body, reported_at, status)"
        " VALUES (?,?,?,?,?,?)",
        (game_guid, reporter.strip().lower(), subject or "", body or "",
         datetime.now(timezone.utc).isoformat(), "open"))
    return cur.lastrowid


def open_bug_count(db, game_guid):
    """Number of still-open bugs for one game (pause predicate)."""
    return db.execute(
        "SELECT COUNT(*) FROM bugs WHERE game_guid=? AND status='open'",
        (game_guid,)).fetchone()[0]


def game_paused(games_dir, guid):
    """True when the game has an open bug: no turns, no idle turns, no
    nudges (§pause — docs/playtest-bug-handling.md player protocol #2)."""
    if guid is None:
        return False
    db = _open_db(games_dir, guid)
    try:
        ensure_bugs_table(db)
        return open_bug_count(db, guid) > 0
    finally:
        db.close()


def close_bug(db, bug_id, resolution_note):
    """Close one bug (the resolution email goes out through Murph, not
    the engine). Returns True when a row was actually closed. When the
    last open bug for a game closes, the game unpauses automatically —
    the pause is derived from open rows, never a flag to remember to
    clear."""
    cur = db.execute(
        "UPDATE bugs SET status='closed', resolution_note=?, closed_at=?"
        " WHERE id=? AND status='open'",
        (resolution_note or "",
         datetime.now(timezone.utc).isoformat(), bug_id))
    return cur.rowcount > 0


def _bug_outcome(games_dir, sender_email, subject, body, guid):
    """Route a BUG:-flagged message: file it, pause the game, hand off
    to Murph. No turn runs, nothing is sent to the player — the engine
    never feeds a bug report to the GM as a move."""
    kind, matched_guid, _ = match_game(games_dir, sender_email, guid)
    target = matched_guid if kind == "matched" else None
    if target is not None:
        db = _open_db(games_dir, target)
        try:
            with get_lock(target):
                ensure_bugs_table(db)
                bug_id = report_bug(db, target, sender_email,
                                    subject or "", body or "")
                db.commit()
        finally:
            db.close()
        note = f"bug #{bug_id} filed; game paused"
    else:
        bug_id = None  # no game to file against; still a Murph handoff
        note = "bug report with no matching game; handed to Murph unfiled"
    return DispatchOutcome("bug", sender_email, subject=subject, body=body,
                           guid=target, note=note, bug_id=bug_id)

# per-game serial-turn locks (§2.2: "turns serial per game")
_locks_guard = threading.Lock()
_game_locks = {}


def get_lock(guid):
    with _locks_guard:
        return _game_locks.setdefault(guid, threading.Lock())


class DispatchOutcome:
    """One inbound message's resolution. action is one of:
      turn_email — a turn ran; send subject/body[/html] as the game's turn email
      clarify    — no turn ran; send the fresh-thread clarification email
      failed     — the turn failed twice; send NOTHING (§2.6)
      ignored    — message was a duplicate/empty; send nothing
      bug        — BUG:-flagged report: no turn ran; hand the report to
                   Murph (never the player) via send_outcome's bug branch
      paused     — game has open bug(s); the move was held, no turn ran,
                   nothing sent

    html is the rich-HTML twin of body (2026-09-27: Neil wants HTML on
    every game email; plain text always carries the complete message).
    bug_id is the row id filed in the game's bugs table (bug action only).
    """
    def __init__(self, action, sender, subject=None, body=None,
                 guid=None, turn_no=None, note=None, html=None, bug_id=None):
        self.action, self.sender = action, sender
        self.subject, self.body = subject, body
        self.guid, self.turn_no, self.note = guid, turn_no, note
        self.html = html
        self.bug_id = bug_id


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
    subject, body, html = render_turn_email(guid, view, result)
    return DispatchOutcome("turn_email", sender, subject, body,
                           guid=guid, turn_no=result.turn_no, html=html,
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
    whole poll batch through dispatch_batch for late-reply folding.

    A BUG:-flagged message short-circuits everything: it is filed and
    handed to Murph, and no turn ever runs for it."""
    guid = extract_guid(body) or extract_guid(subject)
    if is_bug_report(subject, body):
        return _bug_outcome(games_dir, sender_email, subject, body, guid)
    kind, matched_guid, reason = match_game(games_dir, sender_email, guid)
    if kind == "signup":
        return new_game(games_dir, sender_email, body, gm, turn_len_min)
    if kind == "clarify":
        csubj, cbody, chtml = render_clarification(reason)
        return DispatchOutcome("clarify", sender_email, csubj, cbody,
                               note=f"ambiguous inbound: {reason}", html=chtml)
    if game_paused(games_dir, matched_guid):
        # Bug pause (docs/playtest-bug-handling.md #2): the move is held
        # — never counted as a turn, never advances the clock — and
        # nothing is sent. The fairness guarantee is "report a bug, lose
        # nothing": a move during the pause must not cost game time
        # either, so it is held, not run.
        return DispatchOutcome(
            "paused", sender_email, guid=matched_guid,
            note="game paused by open bug(s); move held, nothing sent")
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
        # A BUG: report never folds into a turn and never becomes the
        # "first" message a batch folds onto — it is routed per-message,
        # directly, before any game matching.
        if is_bug_report(msg.get("subject", ""), msg.get("body", "")):
            outcomes.append(dispatch_message(games_dir, msg["sender"],
                                             msg.get("subject", ""),
                                             msg.get("body", ""), gm,
                                             turn_len_min))
            continue
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
        if game_paused(games_dir, guid):
            continue  # bug pause: no idle turns, the clock never advances
        db = _open_db(games_dir, guid)
        try:
            latest = db.execute("SELECT MAX(created_at) FROM turns").fetchone()[0]
            if latest is None:
                continue  # no turns yet: a failed signup, not an idle game —
                          # the nudge path owns that UX (§2.3); the sweep
                          # never starts a game
            if latest >= cutoff:
                continue
            with get_lock(guid):
                outcomes.append(_turn_outcome(db, game["player_email"], gm,
                                              IDLE_INPUT, guid, turn_len_min))
        finally:
            db.close()
    return outcomes


# ---------------------------------------------------------------------------
# Selftest — the BUG: keyword rule as an executable spec
# ---------------------------------------------------------------------------
def _check(name, cond, rows):
    rows.append({"name": name, "ok": bool(cond)})
    return bool(cond)


def selftest():
    """Focused controls for the bug-report keyword rule. The positive
    cases matter here: a synthetic BUG:-shaped message MUST file and
    pause (a rule that can only refuse is not a rule)."""
    import sys
    import tempfile
    from . import schema as _schema2
    rows = []
    tmp = tempfile.mkdtemp(prefix="atfl-bugrule-")

    # -- is_bug_report: the flag predicate ---------------------------------
    cases = [
        ("BUG: map showed a place", "the map showed a place", True),
        ("Re: [ATFL ab12cd34] Above the Fog Line",
         "BUG: the map showed a place", True),     # reply shape: subject flag buried, body leads
        ("Re: [ATFL ab12cd34] Above the Fog Line", "the map showed a place", False),
        ("RE[2]: re: BUG: x", "", True),            # stacked Re: prefixes
        ("", "  \n  bug : typo in turn 3", True),   # blank lines + whitespace
        ("Bug report", "please help", False),       # "bug report" is not "bug:"
        ("", "I found a bug: in the story", False), # mid-line colon, no flag
        ("", "first line is normal\nBUG: second line", False),  # must be FIRST line
        ("", "", False),
        (None, None, False),
    ]
    for i, (subj, body, want) in enumerate(cases):
        _check(f"is_bug_report case {i}: {subj!r}", is_bug_report(subj, body) is want, rows)

    # -- game fixture --------------------------------------------------------
    guid = "12345678-1234-1234-1234-1234567890ab"
    db = _schema2.create_db(os.path.join(tmp, guid + ".db"))
    db.execute(
        "INSERT INTO games (guid, player_email, scenario_id, status, turn_no)"
        " VALUES (?,?,?,?,?)",
        (guid, "masta@gibdon.com", "fog-line-mystery-v1", "active", 0))
    db.commit()
    db.close()

    # -- pause lifecycle: pause derives from open rows, never a flag -------
    db = _open_db(tmp, guid)
    ensure_bugs_table(db)
    _check("no pause before any bug", open_bug_count(db, guid) == 0, rows)
    bid = report_bug(db, guid, "Neil@GibDon.com", "BUG: map wrong", "the map...")
    db.commit()
    _check("report_bug returns int id", isinstance(bid, int) and bid >= 1, rows)
    _check("reporter normalized lowercase",
           db.execute("SELECT reporter FROM bugs WHERE id=?", (bid,)).fetchone()[0]
           == "neil@gibdon.com", rows)
    _check("open bug pauses the game", open_bug_count(db, guid) == 1, rows)
    db.close()
    _check("game_paused reads through the helper", game_paused(tmp, guid), rows)

    # -- a normal move while paused: held, never a turn ---------------------
    out = dispatch_message(tmp, "masta@gibdon.com", "Re: turn 2", "I walk north",
                           gm=None)
    _check("paused move -> 'paused' action", out.action == "paused", rows)
    db = _open_db(tmp, guid)
    _check("paused move runs no turn",
           db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0, rows)
    _check("paused move does not advance game_clock_min",
           db.execute("SELECT game_clock_min FROM games").fetchone()[0] == 0, rows)
    # -- close reopens automatically -----------------------------------------
    _check("close_bug closes the open row", close_bug(db, bid, "map label fixed"), rows)
    _check("close_bug on closed row is a no-op", not close_bug(db, bid, "x"), rows)
    db.commit()
    _check("game unpauses when last bug closes", open_bug_count(db, guid) == 0, rows)
    db.close()
    _check("game_paused False after close", not game_paused(tmp, guid), rows)

    # -- bug report dispatch: files, pauses, never runs a turn --------------
    out = dispatch_message(tmp, "masta@gibdon.com",
                           "Re: [ATFL] turn 3",
                           f"BUG: the map label is wrong\nGame code: {guid}\nextra",
                           gm=None)
    _check("bug report -> 'bug' action", out.action == "bug", rows)
    _check("bug outcome carries the bug id",
           out.bug_id is not None and isinstance(out.bug_id, int), rows)
    _check("bug outcome names the game", out.guid == guid, rows)
    _check("bug report runs no turn",
           _open_db(tmp, guid).execute("SELECT COUNT(*) FROM turns")
           .fetchone()[0] == 0, rows)
    _check("filing the bug pauses the game", game_paused(tmp, guid), rows)

    # -- a second bug report while paused files, not holds ------------------
    out2 = dispatch_message(tmp, "masta@gibdon.com", "BUG: another thing",
                            f"still broken\nGame code: {guid}", gm=None)
    _check("second bug report also files", out2.action == "bug"
           and out2.bug_id not in (None, out.bug_id), rows)

    # -- bug report with no matching game: still a Murph handoff ------------
    out3 = dispatch_message(tmp, "stranger@example.com", "BUG: help",
                            "I have no game", gm=None)
    _check("unmatched bug report -> 'bug', unfiled",
           out3.action == "bug" and out3.bug_id is None
           and out3.guid is None, rows)

    # -- sweep_idle skips paused games ---------------------------------------
    idle_out = sweep_idle(tmp, gm=None, interval_h=0)
    _check("sweep_idle emits nothing for a paused game", idle_out == [], rows)

    # -- batch: bug report never folds, never becomes a fold target ----------
    batch_out = dispatch_batch(
        tmp,
        [{"sender": "masta@gibdon.com", "subject": "move",
          "body": "I walk north"},
         {"sender": "masta@gibdon.com", "subject": "Re: turn",
          "body": f"BUG: batch one\nGame code: {guid}"}],
        gm=None)
    actions = sorted(o.action for o in batch_out)
    _check("batch with bug+move -> bug and paused, nothing else",
           actions == ["bug", "paused"], rows)

    n_fail = sum(1 for r in rows if not r["ok"])
    print(f"dispatch bugrule selftest: {len(rows) - n_fail}/{len(rows)} PASS")
    for r in rows:
        if not r["ok"]:
            print("  FAIL:", r["name"], file=sys.stderr)
    return 1 if n_fail else 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(selftest())
