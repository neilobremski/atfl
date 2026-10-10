"""Mailer layer — relay edition (DESIGN.md §1.1, §5.3; rewritten 2026-10-02).

The engine sends NO email directly (Neil's 2026-09-30 decision): outbound
goes to Murph over A8S as atfl_outbound envelopes, and inbound arrives as
atfl_inbound forwards that Murph polls from murph@inkboxmail.com
(docs/mail-relay-design.md). This module maps dispatch outcomes to
handoffs and polls the engine's A8S inbox for Murph's forwards.

Transport contract (what tests inject): a "relay" object implementing

    poll_inbound(engine_node, agents_dir=None) -> [(envelope, source_path)]
    normalize_inbound(envelope) -> normalized inbound dict
    build_outbound_envelope(game_guid, turn_no, to_addr, subject,
                            body_text, body_html, composite_jpeg=None)
    consume_inbound(source_path, engine_node, agents_dir=None)
    send_outbound(envelope, murph_node, node_root) -> True (raises loudly
        on failure — §2.6: a handoff that fails is never half-sent)

The live implementation is server/murph_relay.py (pass relay=None to use
it). Tests use FakeGmail below, which simulates the Murph side
(queue_inbound ≈ a forwarded player mail).

Rules enforced here, not elsewhere:
  - `failed` / `ignored` outcomes hand off NOTHING (§2.6).
  - The §2.2 attachments rule holds trivially: atfl_inbound forwards
    carry no attachment metadata (normalize_inbound always yields []),
    so there is nothing to log and nothing to act on.
  - The standalone nudge (§2.3/§2.4/§5.3) is mailer-level: at most one
    per 24h per active game, only when no handoff went out in that
    window, never in-character, mutates nothing in the world tables.

Threading moved to Murph's side (one Inkbox thread per game): the old
thread_message_id/thread_refs bookkeeping is gone from this module and
from schema.py; last_email_at survives because it drives the nudge gate.
Per-turn stats still record send_ms — it now measures the A8S handoff to
Murph, not an SMTP send (design doc notes the semantic change).
"""
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from .render import (render_nudge, COMPOSITE_IMG_MARKER,
                     COMPOSITE_IMG_TAG)
from . import schema as _schema
from . import murph_relay as _relay

NUDGE_MAX_AGE_H = 24  # §5.3: ≤1 standalone nudge per 24h per game


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _open_game_db(games_dir, guid):
    db = sqlite3.connect(os.path.join(games_dir, f"{guid}.db"))
    db.row_factory = sqlite3.Row
    _schema.ensure_mailer_columns(db)  # migrate pre-mailer DB files
    return db


def _resolve_composite_marker(html, has_composite):
    """Resolve the composite marker in a turn email's HTML twin: with
    the composite attached -> inline <img cid:...>; without -> the
    marker is dropped, never a broken image. (Was _html_for_send; the
    attachment rides in the atfl_outbound envelope now, not in MIME.)"""
    if not html or COMPOSITE_IMG_MARKER not in html:
        return html
    if has_composite:
        return html.replace(COMPOSITE_IMG_MARKER, COMPOSITE_IMG_TAG)
    return html.replace(COMPOSITE_IMG_MARKER, "")


def poll_inbox(engine_node, skip_ids=None, agents_dir=None, relay=None):
    """Poll the engine's A8S inbox for Murph's atfl_inbound forwards.

    Returns [(inbound, source_path)]: the normalized dicts carry exactly
    the keys the old GmailClient.get dicts had, so dispatch/
    extract_guid/match_game run unmodified. skip_ids are
    inkbox_message_ids already processed (the seen-set covers restarts).
    Files are NOT consumed here — run_poll_cycle consumes each after its
    handoff, or leaves it in place on failure so the next cycle retries.
    """
    relay = _relay if relay is None else relay
    skip = set(skip_ids or ())
    out = []
    for envelope, path in relay.poll_inbound(engine_node,
                                             agents_dir=agents_dir):
        inbound = relay.normalize_inbound(envelope)
        if inbound["id"] in skip:
            continue
        out.append((inbound, path))
    return out


def _record_handoff(games_dir, guid):
    """Bookkeeping after a successful handoff: when we last handed
    anything to Murph for this game (drives the nudge gate)."""
    db = _open_game_db(games_dir, guid)
    try:
        db.execute("UPDATE games SET last_email_at=? WHERE guid=?",
                   (_utcnow_iso(), guid))
        db.commit()
    finally:
        db.close()


def _record_send_stats(games_dir, guid, turn_no, handoff_ms):
    """§6.3: fill the send side of the turn's stats row once the handoff
    actually leaves. send_ms now measures the A8S handoff to Murph, not
    an SMTP send. Only turn emails have stats rows; clarification and
    nudge emails (no turn) are not part of the dogfooding set."""
    db = _open_game_db(games_dir, guid)
    try:
        row = db.execute(
            "SELECT id FROM turns WHERE game_guid=? AND turn_no=?",
            (guid, turn_no)).fetchone()
        if row is None:
            return
        db.execute(
            "UPDATE turn_stats SET send_ms=?, email_sent_at=? WHERE turn_id=?",
            (handoff_ms, _utcnow_iso(), row["id"]))
        db.commit()
    finally:
        db.close()


def send_outcome(games_dir, outcome, inbound, murph_node, node_root,
                 images=None, relay=None):
    """Map one DispatchOutcome to a Murph handoff (or to nothing).

    inbound is the normalized poll dict for the triggering message.
    Returns (handed_off, note): note carries the image outcome for turn
    emails ("composite attached (...)" / "images skipped (...)").

    turn_no on the envelope: turn emails carry the turn number (the
    Murph-side replay guard dedupes per guid:turn). Clarify and nudge
    have no turn, and the guard would treat every (guid, None) pair as
    the same send — so clarify carries "clarify-<inkbox id>" (stable
    across retries of the same clarify, distinct across clarifies) and
    nudge carries "nudge-<UTC date>" (stable across retries of the same
    day's nudge, distinct across days).

    A handoff failure raises (A8STransportError) — loud §2.6, never
    half-sent; the caller leaves the inbox file unconsumed so the next
    cycle retries.
    """
    relay = _relay if relay is None else relay
    if outcome.action in ("failed", "ignored", "paused"):
        return False, None  # §2.6: nothing leaves on failure; a paused
                            # move is held, not sent
    to_addr = outcome.sender

    if outcome.action == "bug":
        # A filed bug report is a Murph handoff, never a player email:
        # the envelope rides `a8s tell` with kind atfl_bug_report, which
        # the Murph-side consumer does NOT pick up (positive
        # classification on atfl_outbound) — the blob stays in Murph's
        # inbox, where the regular message checker surfaces it for
        # triage. The player hears about it through Murph, not the
        # engine.
        envelope = relay.build_bug_report_envelope(
            outcome.guid, outcome.bug_id, outcome.sender,
            outcome.subject or "", outcome.body or "")
        relay.send_bug_report(envelope, murph_node, node_root)
        return True, "bug report handed to Murph"

    if outcome.action == "turn_email":
        jpeg, img_note = _turn_composite(games_dir, outcome, images)
        html = _resolve_composite_marker(outcome.html, jpeg is not None)
        envelope = relay.build_outbound_envelope(
            outcome.guid, outcome.turn_no, to_addr, outcome.subject,
            outcome.body, html, composite_jpeg=jpeg)
        t0 = time.perf_counter()
        relay.send_outbound(envelope, murph_node, node_root)
        handoff_ms = (time.perf_counter() - t0) * 1000.0
        _record_handoff(games_dir, outcome.guid)
        _record_send_stats(games_dir, outcome.guid, outcome.turn_no,
                           handoff_ms)
        return True, img_note

    if outcome.action == "clarify":
        # §5.3: clarification was always a fresh thread. Murph's sender
        # currently keeps one thread per game, so fresh_thread rides as
        # an advisory key (the sender tolerates extra keys) until the
        # Murph side implements it.
        envelope = relay.build_outbound_envelope(
            outcome.guid, f"clarify-{inbound['id']}", to_addr,
            outcome.subject, outcome.body, outcome.html)
        envelope["fresh_thread"] = True
        relay.send_outbound(envelope, murph_node, node_root)
        return True, None

    return False, None


def _turn_composite(games_dir, outcome, images_cfg):
    """Phase 3: build the turn's composite JPEG for a turn_email
    outcome. Returns (jpeg_bytes_or_None, note_or_None).

    Any failure → (None, note); the text-only turn still sends (§2.6:
    images never fail a turn). Only turn emails get composites;
    clarify/nudge stay text-only.

    images_cfg["composite"]: 'v1' (three-panel composite) or 'v2'
    (single scene image + SVG map overlay, build_turn_composite_v2).
    Defaults to 'v1'. Neil approved v2 on 2026-10-03 (single image) with
    the 2026-10-04 geometry (1024 scene, 170px overlay) — the flip rides
    on ATFL_COMPOSITE in the env.
    """
    if (not images_cfg) or images_cfg.get("mode") in (None, "off") \
            or outcome.action != "turn_email":
        return None, None
    composite_mode = (images_cfg.get("composite") or "v1").strip().lower()
    if composite_mode not in ("v1", "v2"):
        return None, (f"images skipped (ATFL_COMPOSITE={composite_mode!r} "
                       "not 'v1'/'v2')")
    try:
        from .images import (build_provider, build_turn_composite,
                             build_turn_composite_v2, ImageError)
        provider = build_provider(images_cfg.get("mode"),
                                  api_key=images_cfg.get("api_key"),
                                  hf_token=images_cfg.get("hf_token"))
        if provider is None:
            return None, None
        if composite_mode == "v2":
            comp = build_turn_composite_v2(games_dir, outcome.guid,
                                           outcome.turn_no, provider)
            return (comp["jpeg"],
                    f"single image attached ({comp['time_of_day']}, "
                    f"map overlay {comp['overlay_px']}px)")
        comp = build_turn_composite(games_dir, outcome.guid,
                                    outcome.turn_no, provider)
        return (comp["jpeg"],
                f"composite attached ({comp['time_of_day']}, "
                f"ref={'kept' if comp['character_ref_used'] else 'new'})")
    except Exception as e:
        # log, never raise: text carries the complete turn
        logging.getLogger("atfl.mailer").warning(
            "images skipped for guid=%s turn=%s: %s",
            outcome.guid, outcome.turn_no, e)
        return None, f"images skipped ({type(e).__name__}: {e})"


def maybe_nudge(games_dir, murph_node, node_root, relay=None,
                max_age_h=NUDGE_MAX_AGE_H):
    """§2.3/§5.3 standalone-nudge fallback (mailer-level): for each active
    game with no handoff in the last max_age_h, hand one short system
    nudge envelope to Murph. Advances nothing, mutates nothing in the
    world tables (only the last_email_at bookkeeping column). Games with
    an open bug are paused: no nudges (docs/playtest-bug-handling.md)."""
    from .dispatch import game_paused  # local import: mailer is dispatch's client
    relay = _relay if relay is None else relay
    sent = []
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=max_age_h)).isoformat()
    if not os.path.isdir(games_dir):
        return sent
    for name in sorted(os.listdir(games_dir)):
        if not (name.endswith(".db") and len(name) == 36 + 3):
            continue
        guid = name[:-3]
        if game_paused(games_dir, guid):
            continue  # bug pause: the player gets no expiry nudges
        db = _open_game_db(games_dir, guid)
        try:
            row = db.execute(
                "SELECT status, player_email, last_email_at FROM games"
                " WHERE guid=?", (guid,)).fetchone()
        finally:
            db.close()
        if row is None or row["status"] != "active":
            continue  # §2.4.5: dead or ended games get no nudges, ever
        if row["last_email_at"] and row["last_email_at"] >= cutoff:
            continue
        subject, body, html = render_nudge(guid)
        envelope = relay.build_outbound_envelope(
            guid,
            "nudge-" + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            row["player_email"], subject, body, html)
        relay.send_outbound(envelope, murph_node, node_root)
        _record_handoff(games_dir, guid)
        sent.append({"guid": guid, "handoff": True})
    return sent


def _seen_db_path(games_dir):
    return os.path.join(games_dir, "mailer.db")


def _load_seen(games_dir):
    """inkbox_message_ids already processed in a previous process
    lifetime. Empty set when the mailer state DB doesn't exist yet."""
    path = _seen_db_path(games_dir)
    if not os.path.exists(path):
        return set()
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT message_id FROM seen_messages")}
    finally:
        db.close()


def _store_seen(games_dir, message_ids):
    """Record processed ids AFTER full processing (handoff + consume
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


def run_poll_cycle(games_dir, gm, engine_node, murph_node, node_root,
                   seen_ids=None, turn_len_min=60, images=None,
                   relay=None, agents_dir=None):
    """One full relay cycle: poll -> dispatch -> hand off -> consume.

    images: {"mode": "off"/"stub"/"real"/"hf", "api_key": ...,
             "hf_token": ...} or None.
    The composite attaches to turn emails only; any image failure is
    noted, never raised (the text-only turn still sends).

    seen_ids persists across cycles within a process; the on-disk
    seen-set (mailer.db in games_dir) persists across restarts, so a
    forward that was processed but never consumed never runs a
    duplicate turn. Crash between processing and the seen-record means
    at-least-once re-dispatch — the audit log shows the duplicate. A
    handoff failure raises: the seen-set is NOT stored and the inbox
    file is NOT consumed, so the next cycle retries the same inbound
    (Murph's replay guard dedupes the turn if the first tell landed).
    Returns {"outcomes": [...], "sent": [...], "nudged": [...]}."""
    from .dispatch import dispatch_batch, sweep_idle  # local import: mailer is dispatch's client
    relay = _relay if relay is None else relay
    seen = set() if seen_ids is None else seen_ids
    seen |= _load_seen(games_dir)
    pairs = poll_inbox(engine_node, skip_ids=seen, agents_dir=agents_dir,
                       relay=relay)
    fresh = [(m, p) for (m, p) in pairs if m["id"] not in seen]
    for m, _ in fresh:
        seen.add(m["id"])

    outcomes = dispatch_batch(games_dir,
                              [{"sender": m["sender"], "subject": m["subject"],
                                "body": m["body"]} for m, _ in fresh],
                              gm, turn_len_min)
    sent = []
    for (m, path), out in zip(fresh, outcomes):
        handed_off, img_note = send_outcome(
            games_dir, out, m, murph_node, node_root, images=images,
            relay=relay)
        note = "; ".join(n for n in (out.note, img_note) if n) or None
        sent.append({"sender": out.sender, "action": out.action,
                     "guid": out.guid, "turn_no": out.turn_no,
                     "handoff": handed_off, "note": note})
        try:
            relay.consume_inbound(path, engine_node, agents_dir=agents_dir)
        except Exception:
            pass  # seen-set covers it; the file just re-polls once
    _store_seen(games_dir, [m["id"] for m, _ in fresh])

    # Idle sweep (§2.3): the daily touch. Runs AFTER inbound turns so a
    # same-cycle player reply suppresses it (sweep_idle's gate keys off
    # MAX(turns.created_at), which the reply's turn just refreshed), and
    # BEFORE maybe_nudge: a handed-off idle turn refreshes last_email_at
    # via _record_handoff, keeping the standalone nudge a true fallback
    # (it fires only when no turn email went out in 24h — e.g. the idle
    # turn failed twice and nothing was handed off).
    for out in sweep_idle(games_dir, gm, turn_len_min=turn_len_min):
        handed_off, img_note = send_outcome(
            games_dir, out, None, murph_node, node_root, images=images,
            relay=relay)
        note = "; ".join(n for n in (out.note, img_note) if n) or None
        sent.append({"sender": out.sender, "action": out.action,
                     "guid": out.guid, "turn_no": out.turn_no,
                     "handoff": handed_off, "note": note})
    return {"outcomes": outcomes, "sent": sent,
            "nudged": maybe_nudge(games_dir, murph_node, node_root,
                                  relay=relay)}


class FakeGmail:
    """In-memory Murph-side stand-in for tests. (The name is historical —
    it used to fake Gmail; now it fakes the relay's Murph side.)

    Inbound: queue_inbound() ≈ Murph forwarding a player mail. The stored
    dicts are exactly normalize_inbound-shaped (thread_id and
    header_message_id None, attachments [], ink-style id), so the key
    set matches server/murph_relay.py's pinned inbound contract.
    Outbound: send_outbound() records envelopes in `outbox` instead of
    shelling `a8s tell`; consume_inbound() drops files from the inbox.
    """

    def __init__(self, murph_node="murph", engine_node="atfl-server"):
        self.murph_node = murph_node
        self.engine_node = engine_node
        self.inbox = []   # normalized inbound dicts, unconsumed
        self.trash = []   # consumed inbound dicts
        self.outbox = []  # {"envelope", "murph_node", "node_root"}
        self._next = 1000

    def queue_inbound(self, sender, subject, body):
        mid = f"ink-{self._next}"
        self._next += 1
        self.inbox.append({
            "id": mid, "thread_id": None, "header_message_id": None,
            "from": sender, "to": "murph@inkboxmail.com",
            "subject": subject, "date": _utcnow_iso(), "body": body,
            "sender": sender.strip().lower(), "attachments": [],
            "label_ids": ["INBOX", "UNREAD"]})
        return mid

    # -- relay transport contract --
    @staticmethod
    def normalize_inbound(envelope):
        return envelope  # queue_inbound already yields normalized dicts

    build_outbound_envelope = staticmethod(
        _relay.build_outbound_envelope)  # the pinned builder, verbatim
    build_bug_report_envelope = staticmethod(
        _relay.build_bug_report_envelope)  # bug-report builder, verbatim

    def poll_inbound(self, engine_node, agents_dir=None):
        return [(m, m["id"]) for m in self.inbox]

    def consume_inbound(self, source_id, engine_node, agents_dir=None):
        kept = [m for m in self.inbox if m["id"] != source_id]
        self.trash.extend(m for m in self.inbox if m["id"] == source_id)
        self.inbox = kept

    def send_outbound(self, envelope, murph_node, node_root):
        missing = [f for f in _relay.OUTBOUND_FIELDS if f not in envelope]
        if missing:
            raise _relay.A8STransportError(
                f"atfl_outbound envelope missing fields: {missing}")
        if envelope.get("kind") != _relay.OUTBOUND_KIND:
            raise _relay.A8STransportError("not an atfl_outbound envelope")
        self.outbox.append({"envelope": dict(envelope),
                            "murph_node": murph_node,
                            "node_root": node_root})
        return True

    def send_bug_report(self, envelope, murph_node, node_root):
        missing = [f for f in _relay.BUG_REPORT_FIELDS if f not in envelope]
        if missing:
            raise _relay.A8STransportError(
                f"atfl_bug_report envelope missing fields: {missing}")
        if envelope.get("kind") != _relay.BUG_REPORT_KIND:
            raise _relay.A8STransportError("not an atfl_bug_report envelope")
        self.outbox.append({"envelope": dict(envelope),
                            "murph_node": murph_node,
                            "node_root": node_root})
        return True


# ---------------------------------------------------------------------------
# Selftest — the relay-edition mailer contract as an executable spec
# ---------------------------------------------------------------------------
def _check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"selftest failed: {name}")


def selftest():
    import tempfile
    from . import schema as _schema
    from .dispatch import DispatchOutcome
    from .gm import MockGM

    tmp = tempfile.mkdtemp(prefix="mailer-relay-")
    relay = FakeGmail()

    # A game row for the direct send_outcome checks (in real flows the
    # dispatch signup creates the DB; here we seed it by hand).
    gdb = _schema.create_db(os.path.join(tmp, "guid-1.db"))
    gdb.execute(
        "INSERT INTO games (guid, player_email, scenario_id, status)"
        " VALUES (?,?,?,?)",
        ("guid-1", "player@example.com", "fog-line-mystery-v1", "active"))
    gdb.commit()
    gdb.close()

    # -- poll_inbox: skip + shape --
    mid1 = relay.queue_inbound("Player@Example.com", "[ATFL ab12cd34] hi",
                               "start")
    mid2 = relay.queue_inbound("other@example.com", "above the fog line",
                               "start too")
    pairs = poll_inbox("atfl-server", relay=relay)
    _check("poll: two forwards polled with source tokens",
           len(pairs) == 2 and all(p[1] for p in pairs))
    m1 = pairs[0][0]
    _check("poll: normalized keys == murph_relay pinned key set",
           set(m1.keys()) == {"id", "thread_id", "header_message_id",
                               "from", "to", "subject", "date", "body",
                               "sender", "attachments", "label_ids"})
    _check("poll: sender lowercased, subject verbatim",
           m1["sender"] == "player@example.com"
           and m1["subject"].startswith("[ATFL"))
    _check("poll: skip_ids honored",
           poll_inbox("atfl-server", skip_ids={mid1}, relay=relay)[0][0]["id"]
           == mid2)

    # -- send_outcome: turn_email hands off, records, stats --
    out = DispatchOutcome("turn_email", "player@example.com",
                          subject="[ATFL ab12cd34] Above the Fog Line",
                          body="narrative", guid="guid-1", turn_no=3,
                          html="<p>narrative</p> [[TURN_COMPOSITE]]")
    handed, note = send_outcome(tmp, out, m1, "murph", "/node/root",
                                images={"mode": "off"}, relay=relay)
    _check("send: turn_email hands off", handed is True)
    env = relay.outbox[-1]["envelope"]
    _check("send: envelope is the pinned atfl_outbound set",
           set(env.keys()) == set(_relay.OUTBOUND_FIELDS)
           and env["game_guid"] == "guid-1" and env["turn_no"] == 3
           and env["to"] == "player@example.com")
    _check("send: images off -> no composite, marker dropped from html",
           env["attachments"] == []
           and "[[TURN_COMPOSITE]]" not in env["body_html"])
    _check("send: told the configured murph node from node root",
           relay.outbox[-1]["murph_node"] == "murph"
           and relay.outbox[-1]["node_root"] == "/node/root")

    # -- send_outcome: clarify gets a distinct replay key + fresh_thread --
    out_c = DispatchOutcome("clarify", "player@example.com",
                            subject="[ATFL] Couldn't match your game",
                            body="clarify body", guid="guid-1",
                            html="<p>clarify</p>")
    handed_c, _ = send_outcome(tmp, out_c, m1, "murph", "/node/root",
                               relay=relay)
    env_c = relay.outbox[-1]["envelope"]
    _check("send: clarify hands off with stable distinct replay key",
           handed_c is True
           and env_c["turn_no"] == f"clarify-{mid1}")
    _check("send: clarify carries fresh_thread advisory",
           env_c.get("fresh_thread") is True
           and env_c["attachments"] == [])

    # -- send_outcome: failed/ignored hand off nothing --
    for action in ("failed", "ignored"):
        n0 = len(relay.outbox)
        handed_f, _ = send_outcome(
            tmp, DispatchOutcome(action, "p@e.c", guid="guid-1"), m1,
            "murph", "/node/root", relay=relay)
        _check(f"send: {action} hands off nothing",
               handed_f is False and len(relay.outbox) == n0)

    # -- run_poll_cycle: end to end with MockGM, then idempotent re-run --
    relay2 = FakeGmail()
    gm = MockGM()
    relay2.queue_inbound("newplayer@example.com", "above the fog line",
                         "I want to play")
    r1 = run_poll_cycle(tmp, gm, "atfl-server", "murph", "/node/root",
                        images={"mode": "off"}, relay=relay2)
    _check("cycle: one inbound -> one signup turn handed off",
           len(r1["sent"]) == 1 and r1["sent"][0]["handoff"] is True
           and r1["sent"][0]["action"] == "turn_email")
    _check("cycle: inbox consumed",
           relay2.poll_inbound("atfl-server") == [])
    r2 = run_poll_cycle(tmp, gm, "atfl-server", "murph", "/node/root",
                        images={"mode": "off"}, relay=relay2)
    _check("cycle: re-run sends nothing new (seen-set + consumed)",
           r2["sent"] == [] and relay2.outbox and True)

    # -- verify_turn (§2.5): criterion 4 reads the mailer's handoff --
    # The signup cycle above ran a real turn AND handed its email off
    # via relay2, so turn_stats.email_sent_at exists for turn 1 — the
    # new outbound-email check must pass against its own writer.
    from .turn_loop import verify_turn as _verify_turn
    nguid = r1["sent"][0]["guid"]
    vdb = _open_game_db(tmp, nguid)
    (tid1,) = vdb.execute(
        "SELECT id FROM turns WHERE game_guid=? AND turn_no=1",
        (nguid,)).fetchone()
    vchecks = _verify_turn(vdb, tid1)
    vdb.close()
    _check("verify: outbound email check reads the handoff record",
           ("outbound email", True) in
           [(n, ok) for n, ok, _note in vchecks])
    _check("verify: all 7 criteria asserted, none skip",
           all(state != "skip" for _n, state, _nt in vchecks))
    seen = _load_seen(tmp)
    _check("cycle: seen-set persisted the inkbox id",
           len(seen) == 1 and next(iter(seen)).startswith("ink-"))

    # -- maybe_nudge: 24h gate, nudge replay key shape --
    guid = r1["sent"][0]["guid"]
    n0 = len(relay2.outbox)
    _check("nudge: fresh game gets no nudge (handoff < 24h ago)",
           maybe_nudge(tmp, "murph", "/node/root", relay=relay2) == []
           and len(relay2.outbox) == n0)
    db = _open_game_db(tmp, guid)
    old = (datetime.now(timezone.utc)
           - timedelta(hours=25)).isoformat()
    db.execute("UPDATE games SET last_email_at=? WHERE guid=?",
               (old, guid))
    db.commit()
    db.close()
    nudged = maybe_nudge(tmp, "murph", "/node/root", relay=relay2)
    nenv = relay2.outbox[-1]["envelope"]
    _check("nudge: stale game nudged once, replay key is nudge-<date>",
           len(nudged) == 1 and nudged[0]["guid"] == guid
           and nenv["turn_no"].startswith("nudge-")
           and nenv["attachments"] == [])
    _check("nudge: second sweep quiet (last_email_at advanced)",
           maybe_nudge(tmp, "murph", "/node/root", relay=relay2) == [])

    # -- maybe_nudge: NULL last_email_at = failed signup (dispatch "failed"
    # rolls back without a handoff, so last_email_at stays NULL and the
    # sweep nudges the very next cycle, NOT 24h later). Pins the real
    # first-game failure UX: the player is kept warm immediately, then
    # the gate closes again for 24h.
    import uuid as _uuid
    from . import seed as _seed
    fail_guid = str(_uuid.uuid4())
    fdb = _schema.create_db(os.path.join(tmp, fail_guid + ".db"))
    _seed.seed(fdb, fail_guid, "failed-signup@example.com")
    fdb.close()
    n1 = len(relay2.outbox)
    nudged2 = maybe_nudge(tmp, "murph", "/node/root", relay=relay2)
    fdb2 = _open_game_db(tmp, fail_guid)
    try:
        lea = fdb2.execute("SELECT last_email_at FROM games WHERE guid=?",
                           (fail_guid,)).fetchone()["last_email_at"]
    finally:
        fdb2.close()
    _check("nudge: failed-signup game (NULL last_email_at) nudged now, "
           "not after 24h",
           len(nudged2) == 1 and nudged2[0]["guid"] == fail_guid
           and len(relay2.outbox) == n1 + 1 and lea is not None)
    _check("nudge: second sweep quiet after the immediate nudge",
           maybe_nudge(tmp, "murph", "/node/root", relay=relay2) == [])

    # -- bug path: BUG: inbound files, pauses, hands off, never turns ---
    import uuid as _uuid2
    from . import dispatch as _dispatch
    bguid = str(_uuid2.uuid4())
    bdb = _schema.create_db(os.path.join(tmp, bguid + ".db"))
    bdb.execute("INSERT INTO games (guid, player_email, scenario_id, status,"
                " turn_no) VALUES (?,?,?,?,?)",
                (bguid, "bugplayer@example.com", "fog-line-mystery-v1",
                 "active", 0))
    bdb.commit()
    bdb.close()
    relay3 = FakeGmail()
    relay3._next = 5000  # the selftest's shared games_dir seen-set already
    # holds ink-1000.. from earlier blocks; a fresh range keeps these
    # inbounds actually fresh
    relay3.queue_inbound("bugplayer@example.com", "Re: [ATFL] turn 2",
                         f"BUG: the map label is wrong\nGame code: {bguid}")
    res3 = run_poll_cycle(tmp, MockGM(), "atfl-server", "murph", "/node/root",
                          turn_len_min=60, images={"mode": "off"},
                          relay=relay3)
    _check("bug: full cycle -> one 'bug' outcome, handed off",
           len(res3["sent"]) == 1 and res3["sent"][0]["action"] == "bug"
           and res3["sent"][0]["handoff"] is True)
    kinds = [e["envelope"]["kind"] for e in relay3.outbox]
    _check("bug: the handoff is exactly one atfl_bug_report (never a"
           " player email)",
           len(relay3.outbox) == 1 and kinds == ["atfl_bug_report"])
    benv = relay3.outbox[0]["envelope"]
    _check("bug: report envelope carries game + bug id + reporter + body",
           benv["game_guid"] == bguid and isinstance(benv["bug_id"], int)
           and benv["bug_id"] >= 1 and benv["from"] == "bugplayer@example.com"
           and "map label" in benv["body_text"])
    bdb2 = _open_game_db(tmp, bguid)
    try:
        turns_n = bdb2.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    finally:
        bdb2.close()
    _check("bug: no turn ran for the report", turns_n == 0)
    _check("bug: game is paused after filing", _dispatch.game_paused(tmp, bguid))
    # a normal move while paused: held, nothing to the player
    relay3.queue_inbound("bugplayer@example.com", "Re: [ATFL] turn 2",
                         f"I walk north\nGame code: {bguid}")
    res4 = run_poll_cycle(tmp, MockGM(), "atfl-server", "murph", "/node/root",
                          turn_len_min=60, images={"mode": "off"},
                          relay=relay3)
    _check("bug: move during pause -> 'paused' action, not a turn",
           [s["action"] for s in res4["sent"]] == ["paused"])
    _check("bug: move during pause sends nothing to anyone",
           len(relay3.outbox) == 1)
    # ...and the nudge sweep stays quiet on the paused game too
    _check("bug: paused game gets no nudge",
           maybe_nudge(tmp, "murph", "/node/root", relay=FakeGmail()) == [])

    # -- _turn_composite: v1/v2 wiring behind ATFL_COMPOSITE (#91) --
    # Seeded game (discovered trailhead → map has something to draw),
    # stub provider so no network/key anywhere.
    from .seed import seed as _seed
    vguid = "composite-drill-guid"
    vdb = _schema.create_db(os.path.join(tmp, vguid + ".db"))
    _seed(vdb, vguid, "player@example.com")
    vdb.commit()
    vdb.close()
    out_t = DispatchOutcome("turn_email", "player@example.com",
                            subject="[ATFL] x", body="narrative",
                            guid=vguid, turn_no=1)
    jpg2, note2 = _turn_composite(tmp, out_t,
                                  {"mode": "stub", "composite": "v2"})
    _check("composite: v2 returns JPEG bytes + single-image note",
           jpg2 is not None and jpg2[:3] == b"\xff\xd8\xff"
           and note2.startswith("single image attached"))
    import io as _io
    from PIL import Image as _Img
    _check("composite: v2 full-size 1024x1024, overlay 170px (Neil 2026-10-04)",
           _Img.open(_io.BytesIO(jpg2)).size == (1024, 1024)
           and "170px" in note2)
    adb = _open_game_db(tmp, vguid)
    v2_kinds = {r[0] for r in adb.execute(
        "SELECT kind FROM assets WHERE turn_created=1")}
    adb.close()
    _check("composite: v2 provenance rows are the _v2 kinds",
           {"scene_v2", "map_svg", "composite_v2"} <= v2_kinds
           and "composite" not in v2_kinds)
    jpg1, note1 = _turn_composite(tmp, out_t,
                                  {"mode": "stub", "composite": "v1"})
    _check("composite: v1 three-panel still ships",
           jpg1 is not None and jpg1[:3] == b"\xff\xd8\xff"
           and note1.startswith("composite attached"))
    jpg_d, note_d = _turn_composite(tmp, out_t, {"mode": "stub"})
    _check("composite: missing 'composite' key defaults to v1",
           jpg_d is not None and note_d.startswith("composite attached"))
    jpg_b, note_b = _turn_composite(tmp, out_t,
                                    {"mode": "stub", "composite": "v3"})
    _check("composite: bad mode -> (None, note), never raises",
           jpg_b is None and "ATFL_COMPOSITE" in note_b)

    # -- sweep_idle wiring (§2.3): the idle turn is the daily touch, the
    # standalone nudge is fallback-only. Found 2026-10-10: sweep_idle was
    # defined and tested but never called by run_poll_cycle — only the
    # nudge fallback ran, so a silent player got a system "are you there?"
    # instead of the designed idle turn with catch-up lead.
    import uuid as _uuid3
    from .dispatch import sweep_idle as _sweep_idle
    from .turn_loop import TurnFailed as _TurnFailed
    iguid = str(_uuid3.uuid4())
    idb = _schema.create_db(os.path.join(tmp, iguid + ".db"))
    _seed(idb, iguid, "idleplayer@example.com")
    idb.close()
    irelay = FakeGmail()
    irelay._next = 6000  # shared games_dir seen-set already holds ink-1000..
    irelay.queue_inbound("idleplayer@example.com", "above the fog line",
                         "I want to play")
    ir1 = run_poll_cycle(tmp, MockGM(), "atfl-server", "murph", "/node/root",
                         images={"mode": "off"}, relay=irelay)
    _check("idle: signup turn hands off normally",
           len(ir1["sent"]) == 1 and ir1["sent"][0]["action"] == "turn_email")
    _check("idle: fresh game gets no idle turn",
           _sweep_idle(tmp, MockGM(), interval_h=24) == [])
    idb2 = _open_game_db(tmp, iguid)
    stale = (datetime.now(timezone.utc)
             - timedelta(hours=25)).isoformat()
    idb2.execute("UPDATE turns SET created_at=? WHERE game_guid=?",
                 (stale, iguid))
    idb2.execute("UPDATE games SET last_email_at=? WHERE guid=?",
                 (stale, iguid))
    idb2.commit()
    idb2.close()
    ir2 = run_poll_cycle(tmp, MockGM(), "atfl-server", "murph", "/node/root",
                         images={"mode": "off"}, relay=irelay)
    idle_sent = [s for s in ir2["sent"] if s["action"] == "turn_email"]
    idb3 = _open_game_db(tmp, iguid)
    try:
        last_input = idb3.execute(
            "SELECT player_input FROM turns WHERE game_guid=? "
            "ORDER BY turn_no DESC LIMIT 1", (iguid,)).fetchone()[0]
    finally:
        idb3.close()
    _check("idle: stale game gets exactly one idle turn handed off",
           len(idle_sent) == 1 and idle_sent[0]["handoff"] is True
           and idle_sent[0]["turn_no"] == 2)
    _check("idle: the idle turn's player_input is the sentinel",
           last_input == "idle default")
    _check("idle: nudge stays quiet after the idle handoff (fallback-only)",
           ir2["nudged"] == [])

    # fallback: when the idle turn fails twice, nothing is handed off and
    # the standalone nudge fires — it is the fallback, not the default.
    class _FailGM(MockGM):
        def adjudicate(self, player_input, filtered, context=None):
            raise _TurnFailed("drill failure")
    fgid = str(_uuid3.uuid4())
    fdb = _schema.create_db(os.path.join(tmp, fgid + ".db"))
    _seed(fdb, fgid, "failplayer@example.com")
    fdb.execute(
        "INSERT INTO turns (game_guid, turn_no, game_time_start_min,"
        " game_time_len_min, player_input, created_at)"
        " VALUES (?,?,0,60,'hello',?)",
        (fgid, 1, stale))
    fdb.execute("UPDATE games SET last_email_at=?, status='active'"
                " WHERE guid=?", (stale, fgid))
    fdb.commit()
    fdb.close()
    fr = run_poll_cycle(tmp, _FailGM(), "atfl-server", "murph", "/node/root",
                        images={"mode": "off"}, relay=FakeGmail())
    _check("idle: failed idle turn -> 'failed' outcome, nothing handed off",
           [s["action"] for s in fr["sent"]] == ["failed"]
           and all(s["handoff"] is False for s in fr["sent"]))
    _check("idle: nudge fires as the fallback when no turn email went out",
           len(fr["nudged"]) == 1 and fr["nudged"][0]["guid"] == fgid)

    # zero-turn games are failed signups, not idle games: the sweep never
    # starts a game (the nudge path owns failed-signup UX, §2.3)
    import tempfile as _tf
    ztmp = _tf.mkdtemp(prefix="atfl-idle-zero-")
    zguid = str(_uuid3.uuid4())
    zdb = _schema.create_db(os.path.join(ztmp, zguid + ".db"))
    _seed(zdb, zguid, "zeroplayer@example.com")
    zdb.close()
    _check("idle: zero-turn game gets no idle turn (interval_h=0)",
           _sweep_idle(ztmp, MockGM(), interval_h=0) == [])

    print("\nmailer relay selftest: all checks green.")


if __name__ == "__main__":
    selftest()
