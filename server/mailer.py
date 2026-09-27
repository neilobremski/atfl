"""Mailer layer — DESIGN.md §1.1, §5.3 + research/phase0-email-identity.md.

Maps dispatch outcomes to Gmail sends and polls the game mailbox. Built
against an abstract GmailClient (the game's real address is still open
question #1, so no live mailbox is touched here); the real API adapter
lives in server/gmail_adapter.py and FakeGmail (tests) both implement it.

Threading (§1.1, §5.3): every game's turn/nudge/death emails form one
Gmail thread. The mailer keeps per-game threading state in the games
table (`thread_message_id`, `thread_refs` — see schema.py): turn emails
go out as thread replies (In-Reply-To/References), clarification emails
always start a fresh thread.

Rules enforced here, not elsewhere:
  - `failed` / `ignored` outcomes send NOTHING (§2.6).
  - Inbound mail FROM the game address itself is ignored (anti-loop).
  - Attachments are logged in `mutations` and never acted on (§2.2).
  - The standalone nudge (§2.3/§2.4/§5.3) is mailer-level: at most one
    per 24h per active game, only when no turn email went out in that
    window, never in-character, mutates nothing in the world tables.
"""
import base64
import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid

_MSG = BytesParser(policy=policy.default)

from .render import render_nudge
from . import schema as _schema

GAME_ADDRESS = "abovethefogline@example.invalid"  # STUB until OQ#1 closes

NUDGE_MAX_AGE_H = 24  # §5.3: ≤1 standalone nudge per 24h per game


class GmailClient(ABC):
    """Minimal Gmail surface the mailer needs. A real adapter maps these
    onto users.messages.list/get/send/modify; tests use FakeGmail."""

    @abstractmethod
    def list(self, query, max_results=50, page_token=None):
        """-> {"messages": [{"id","threadId"}], "nextPageToken"?}"""

    @abstractmethod
    def get(self, message_id):
        """-> normalized dict: id, thread_id, header_message_id, from,
        sender (parsed lowercase address), to, subject, date, body
        (plain text), attachments ([{filename, size_bytes}]), label_ids."""

    @abstractmethod
    def send(self, raw_b64):
        """Send a base64url RFC822 message.
        -> {"id", "threadId", "message_id"?} — message_id is the RFC
        Message-ID of the sent mail when the adapter can learn it; the
        mailer prefers it for In-Reply-To/References bookkeeping."""

    @abstractmethod
    def mark_read(self, message_id):
        """Remove UNREAD from the message."""


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _open_game_db(games_dir, guid):
    db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
    db.row_factory = sqlite3.Row
    _schema.ensure_mailer_columns(db)  # migrate pre-mailer DB files
    return db


def extract_text_body(raw_bytes):
    """Best plain-text body from an RFC822 message: prefer the first
    text/plain part, fall back to any text part."""
    msg = _MSG.parsebytes(raw_bytes)
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8",
                              errors="replace")
    plain, other = None, None
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        text = (part.get_payload(decode=True) or b"").decode(
            part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain" and plain is None:
            plain = text
        elif other is None:
            other = text
    return (plain or other or "").strip()


def build_raw(from_addr, to_addr, subject, body,
              in_reply_to=None, references=None):
    """RFC822 bytes for a text/plain send, with optional threading.

    Sets an explicit Message-ID (Gmail preserves a supplied one): the
    mailer threads on RFC Message-IDs, never on Gmail API ids."""
    m = EmailMessage()
    m["From"] = from_addr
    m["To"] = to_addr
    m["Subject"] = subject
    m["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    m.set_content(body)
    return m.as_bytes()


def _from_addr(value):
    """'Name <a@b>' -> 'a@b'."""
    v = (value or "").strip()
    if "<" in v and v.endswith(">"):
        v = v[v.index("<") + 1:-1]
    return v.strip().lower()


def poll_inbox(gmail, game_address=GAME_ADDRESS, since_days=2,
               max_results=50, skip_ids=None):
    """List candidate inbound messages and normalize them. Caller dedupes
    by id against its seen-set and feeds new ones to dispatch.

    skip_ids: ids the mailer has already processed — they are skipped
    BEFORE the get call, so re-polls cost one cheap list only
    (quota-light). The query carries is:unread so processed-and-marked
    messages never re-list; the seen-set covers the rest (mark_read is
    best-effort and the process may restart).

    Returns [inbound], each: id, thread_id, header_message_id, sender,
    subject, date, body, attachments."""
    query = f"to:{game_address} newer_than:{since_days}d -in:sent is:unread"
    skip = set(skip_ids or ())
    page_token = None
    out = []
    while True:
        page = gmail.list(query, max_results=max_results,
                          page_token=page_token)
        for ref in page.get("messages", []):
            if ref["id"] in skip:
                continue
            full = gmail.get(ref["id"])
            if _from_addr(full.get("from")) == game_address.lower():
                continue  # our own sends — anti-loop guard
            out.append(full)
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return out


def _mail_state(games_dir, guid):
    db = _open_game_db(games_dir, guid)
    try:
        row = db.execute(
            "SELECT thread_message_id, thread_refs, last_email_at, status"
            " FROM games WHERE guid=?", (guid,)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def _chain(*parts):
    """Dedupe-preserve a References chain: existing refs, then the
    message being replied to, then the new sent id."""
    out = []
    for p in parts:
        for tok in (p or "").split():
            if tok not in out:
                out.append(tok)
    return " ".join(out)


def _record_send(games_dir, guid, rfc_message_id, full_refs):
    """Thread-state bookkeeping after a successful send.

    Stores the RFC Message-ID (what In-Reply-To/References need), not
    the Gmail API id — a Gmail id in In-Reply-To threads by luck only."""
    db = _open_game_db(games_dir, guid)
    try:
        db.execute(
            "UPDATE games SET thread_message_id=?, thread_refs=?,"
            " last_email_at=? WHERE guid=?",
            (rfc_message_id, full_refs, _utcnow_iso(), guid))
        db.commit()
    finally:
        db.close()


def _sent_rfc_id(sent):
    """The RFC Message-ID to file for threading: prefer the adapter's
    message_id, fall back to the Gmail id (self-heals on the next send)."""
    return sent.get("message_id") or sent["id"]


def send_outcome(games_dir, gmail, outcome, inbound, game_address=GAME_ADDRESS):
    """Map one DispatchOutcome to Gmail (or to nothing).

    inbound is the normalized poll dict for the triggering message —
    its header_message_id becomes In-Reply-To for turn 1 (no thread
    state yet). Returns the sent message id, or None when nothing sent.
    """
    if outcome.action in ("failed", "ignored"):
        return None  # §2.6: nothing leaves on failure
    to_addr = outcome.sender

    if outcome.action == "turn_email":
        state = _mail_state(games_dir, outcome.guid) or {}
        in_reply_to = (state.get("thread_message_id")
                       or inbound.get("header_message_id"))
        refs = _chain(state.get("thread_refs"), in_reply_to)
        raw = build_raw(game_address, to_addr, outcome.subject,
                        outcome.body, in_reply_to, refs or None)
        sent = gmail.send(base64.urlsafe_b64encode(raw).decode())
        rfc_id = _sent_rfc_id(sent)
        _record_send(games_dir, outcome.guid, rfc_id,
                     _chain(refs, in_reply_to, rfc_id))
        return sent["id"]

    if outcome.action == "clarify":
        # §5.3: clarification is always a fresh thread — no threading
        # headers, so a confused player never lands mid-game-thread.
        raw = build_raw(game_address, to_addr, outcome.subject,
                        outcome.body)
        return gmail.send(base64.urlsafe_b64encode(raw).decode())["id"]

    return None


def log_attachments(games_dir, guid, inbound):
    """§2.2: attachments are logged (mutations audit) and never acted on."""
    if not inbound.get("attachments"):
        return
    db = _open_game_db(games_dir, guid)
    try:
        latest = db.execute("SELECT MAX(id) FROM turns").fetchone()[0] or 0
        for att in inbound["attachments"]:
            new = f"{att.get('filename') or '(unnamed)'} ({att.get('size_bytes', 0)} bytes)"
            db.execute(
                "INSERT INTO mutations (turn_id,entity_type,entity_id,field,"
                " old_value,new_value,cause) VALUES (?,?,?,?,?,?,?)",
                (latest, "game", 0, "attachment_received", None, new,
                 "logged; never acted on (§2.2)"))
        db.commit()
    finally:
        db.close()


def maybe_nudge(games_dir, gmail, gm_unused=None, game_address=GAME_ADDRESS,
                max_age_h=NUDGE_MAX_AGE_H):
    """§2.3/§5.3 standalone-nudge fallback (mailer-level): for each active
    game with no outbound email in the last max_age_h, send one short
    system nudge as a thread reply. Advances nothing, mutates nothing in
    the world tables (only the mailer bookkeeping columns)."""
    sent = []
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=max_age_h)).isoformat()
    if not os.path.isdir(games_dir):
        return sent
    for name in sorted(os.listdir(games_dir)):
        if not (name.endswith(".db") and len(name) == 36 + 3):
            continue
        guid = name[:-3]
        db = _open_game_db(games_dir, guid)
        try:
            row = db.execute(
                "SELECT status, player_email, thread_message_id, thread_refs,"
                " last_email_at FROM games WHERE guid=?", (guid,)).fetchone()
        finally:
            db.close()
        if row is None or row["status"] != "active":
            continue  # §2.4.5: dead or ended games get no nudges, ever
        if row["last_email_at"] and row["last_email_at"] >= cutoff:
            continue
        subject, body = render_nudge(guid)
        raw = build_raw(game_address, row["player_email"], subject, body,
                        row["thread_message_id"], row["thread_refs"] or None)
        sent_msg = gmail.send(base64.urlsafe_b64encode(raw).decode())
        rfc_id = _sent_rfc_id(sent_msg)
        _record_send(games_dir, guid, rfc_id,
                     _chain(row["thread_refs"], row["thread_message_id"],
                            rfc_id))
        sent.append({"guid": guid, "message_id": sent_msg["id"]})
    return sent


def _seen_db_path(games_dir):
    return os.path.join(games_dir, "mailer.db")


def _load_seen(games_dir):
    """Ids already processed in a previous process lifetime. Empty set
    when the mailer state DB doesn't exist yet."""
    path = _seen_db_path(games_dir)
    if not os.path.exists(path):
        return set()
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT message_id FROM seen_messages")}
    finally:
        db.close()


def _store_seen(games_dir, message_ids):
    """Record processed ids AFTER full processing (send + mark-read
    attempted): at-least-once on crash, never silent loss."""
    if not message_ids:
        return
    os.makedirs(games_dir, exist_ok=True)
    db = sqlite3.connect(_seen_db_path(games_dir))
    try:
        db.execute("CREATE TABLE IF NOT EXISTS seen_messages"
                   " (message_id TEXT PRIMARY KEY, first_seen_at TEXT)")
        now = _utcnow_iso()
        db.executemany(
            "INSERT OR IGNORE INTO seen_messages (message_id, first_seen_at)"
            " VALUES (?, ?)",
            [(mid, now) for mid in message_ids])
        db.commit()
    finally:
        db.close()


def run_poll_cycle(games_dir, gmail, gm, game_address=GAME_ADDRESS,
                   seen_ids=None, turn_len_min=60):
    """One full mailer cycle: poll -> dispatch -> send -> mark read.

    seen_ids persists across cycles within a process; the on-disk
    seen-set (mailer.db in games_dir) persists across restarts, so a
    message that was processed but never marked read never runs a
    duplicate turn. Crash between processing and the seen-record means
    at-least-once re-dispatch — the audit log shows the duplicate.
    Returns {"outcomes": [...], "sent": [...], "nudged": [...]}."""
    from .dispatch import dispatch_batch  # local import: mailer is dispatch's client
    seen = set() if seen_ids is None else seen_ids
    seen |= _load_seen(games_dir)
    inbound = poll_inbox(gmail, game_address, skip_ids=seen)
    fresh = [m for m in inbound if m["id"] not in seen]
    for m in fresh:
        seen.add(m["id"])

    outcomes = dispatch_batch(games_dir,
                              [{"sender": m["sender"], "subject": m["subject"],
                                "body": m["body"]} for m in fresh],
                              gm, turn_len_min)
    sent = []
    by_sender = {}
    for m, out in zip(fresh, outcomes):
        by_sender.setdefault(m["sender"], []).append((m, out))
    for m, out in zip(fresh, outcomes):
        if out.guid and m.get("attachments"):
            log_attachments(games_dir, out.guid, m)
        sent_id = send_outcome(games_dir, gmail, out, m, game_address)
        sent.append({"sender": out.sender, "action": out.action,
                     "guid": out.guid, "turn_no": out.turn_no,
                     "message_id": sent_id, "note": out.note})
        try:
            gmail.mark_read(m["id"])
        except Exception:
            pass  # read-marking is best-effort; the turn already ran
    _store_seen(games_dir, [m["id"] for m in fresh])
    return {"outcomes": outcomes, "sent": sent,
            "nudged": maybe_nudge(games_dir, gmail, game_address=game_address)}


class FakeGmail(GmailClient):
    """In-memory Gmail stand-in for tests. Inbound mail is queued with
    queue_inbound(); sent mail lands in `outbox` as decoded RFC822."""

    def __init__(self, game_address=GAME_ADDRESS):
        self.game_address = game_address
        self.inbox = []   # normalized inbound dicts
        self.outbox = []  # {"raw": bytes, "parsed": EmailMessage, "id": str}
        self.read_ids = set()
        self._next = 1000

    def queue_inbound(self, sender, subject, body,
                      header_message_id=None, attachments=(),
                      date=None):
        mid = f"in-{self._next}"
        self._next += 1
        self.inbox.append({
            "id": mid, "thread_id": f"th-{mid}",
            "header_message_id": header_message_id or f"<{mid}@fake>",
            "from": sender, "to": self.game_address, "subject": subject,
            "date": date or _utcnow_iso(), "body": body,
            "sender": sender, "attachments": list(attachments),
            "label_ids": ["INBOX", "UNREAD"]})
        return mid

    # -- GmailClient interface --
    def list(self, query, max_results=50, page_token=None):
        msgs = [{"id": m["id"], "threadId": m["thread_id"]}
                for m in self.inbox
                if "UNREAD" in m.get("label_ids", [])][:max_results]
        return {"messages": msgs}

    def get(self, message_id):
        for m in self.inbox:
            if m["id"] == message_id:
                return dict(m)
        raise KeyError(message_id)

    def send(self, raw_b64):
        raw = base64.urlsafe_b64decode(raw_b64.encode())
        parsed = _MSG.parsebytes(raw)
        sid = f"out-{self._next}"
        self._next += 1
        self.outbox.append({"raw": raw, "parsed": parsed, "id": sid})
        # Faithful to the real adapter: message_id is the RFC Message-ID
        # of what was actually sent (build_raw sets it explicitly).
        return {"id": sid, "threadId": parsed.get("Thread-Index", sid),
                "message_id": parsed["Message-ID"]}

    def mark_read(self, message_id):
        self.read_ids.add(message_id)
        for m in self.inbox:
            if m["id"] == message_id and "UNREAD" in m["label_ids"]:
                m["label_ids"].remove("UNREAD")
