"""A8S relay transport — engine <-> Murph (docs/mail-relay-design.md).

Replaces the engine's Gmail surface (server/gmail_adapter.py) with A8S
tells. The engine NEVER sends email directly (Neil's 2026-09-30
decision): it hands atfl_outbound envelopes to Murph's A8S node, and
reads atfl_inbound envelopes Murph forwards from players.

Contract (pinned hermetically by
prototype/murph_relay_adapter_demo.py, 2026-10-02):

  inbound:  {kind:"atfl_inbound", from, subject, body_text,
             inkbox_message_id} -> normalized inbound dict whose KEYS are
             identical to GmailClient.get's, so dispatch/extract_guid/
             match_game run unmodified. inkbox_message_id is the seen-set
             key; thread_id/header_message_id are None (threading moved
             to Murph's side per the design doc); forwards carry no
             attachments; date is stamped at forward time.
  outbound: {kind:"atfl_outbound", game_guid, turn_no, to, subject,
             body_text, body_html, attachments:[{content_id, filename,
             bytes}]}. On the A8S wire the envelope is a JSON text body
             and attachment bytes travel base64-ascii inside it
             (hidden_files/atfl_relay_sender.py accepts both forms).

How the engine talks to A8S (verified live 2026-10-02):

  inbound read: the a8s subscriber delivers tell JSON files into
      <agents_dir>/<node>/inbox/*.json for registered nodes even when
      the node is not running (observed: 5 undelivered files sitting in
      atfl-server's inbox from Oct 1). poll_inbound() scans that
      directory, skips inbox.tmp (subscriber in-progress writes) and
      anything that doesn't parse as an atfl_inbound envelope.
      Processed files are moved to ../trash/ — the same convention
      `a8s drain` uses for consumed messages.
  outbound send: `a8s tell <murph_node> -` with the wire JSON on stdin,
      cwd = the engine node's root, TELL_OUTBOX_DIR=<root>/.outbox
      exported (bare-shell `a8s tell` fails with "cannot send from this
      directory" when cwd matches multiple filedrops — AGENTS.md
      2026-09-29). Retry once, then raise (the mailer rewrite maps this
      to a loud `failed` outcome, §2.6 semantics).
"""
import base64
import json
import logging
import os
import shutil
import subprocess

log = logging.getLogger("atfl.relay")

RELAY_ADDRESS = "murph@inkboxmail.com"  # player-facing address; Murph's side
INBOUND_KIND = "atfl_inbound"
OUTBOUND_KIND = "atfl_outbound"
BUG_REPORT_KIND = "atfl_bug_report"  # engine -> Murph: a filed bug report

# Inbound envelope fields the engine requires (kind checked separately).
_INBOUND_FIELDS = ("from", "subject", "body_text", "inkbox_message_id")

# Outbound envelope fields hidden_files/atfl_relay_sender.py requires.
OUTBOUND_FIELDS = ("kind", "game_guid", "turn_no", "to", "subject",
                   "body_text", "body_html", "attachments")

# Bug-report envelope fields (docs/playtest-bug-handling.md: the relay
# routes a BUG:-flagged message to Murph, never to the engine as a move).
BUG_REPORT_FIELDS = ("kind", "game_guid", "bug_id", "from", "subject",
                     "body_text", "reported_at")


class A8STransportError(Exception):
    """The A8S handoff failed (send retry exhausted, or the inbound
    directory is unreadable). Loud by design — never silently dropped."""


# ---------------------------------------------------------------------------
# Inbound: atfl_inbound envelope -> normalized inbound dict
# ---------------------------------------------------------------------------
def normalize_inbound(envelope):
    """Adapter implementing the GmailClient.get contract for a Murph
    forward. Decisions (2026-10-02, from the pinned demo):

    - "id" is the inkbox_message_id (opaque dedupe key, replaces the
      Gmail message id in the seen-set).
    - thread_id=None: the engine has no reader of thread_id; threading
      is Murph's now.
    - header_message_id=None: In-Reply-To ownership moved to Murph;
      send_outcome's `state.get(...) or inbound.get(...)` chain tolerates
      the missing key.
    - attachments=[]: forwards carry no attachment metadata; the
      "logged-never-acted-on" rule holds trivially.
    - date stamped at forward time: nothing in the server reads the
      inbound date back.
    """
    if not isinstance(envelope, dict) or envelope.get("kind") != INBOUND_KIND:
        raise A8STransportError(
            f"not an {INBOUND_KIND} envelope: {type(envelope)}")
    missing = [f for f in _INBOUND_FIELDS if f not in envelope]
    if missing:
        raise A8STransportError(
            f"{INBOUND_KIND} envelope missing fields: {missing}")
    from datetime import datetime, timezone
    sender = str(envelope["from"]).strip().lower()
    return {
        "id": str(envelope["inkbox_message_id"]),
        "thread_id": None,
        "header_message_id": None,
        "from": envelope["from"],
        "to": RELAY_ADDRESS,
        "subject": envelope["subject"],
        "date": datetime.now(timezone.utc).isoformat(),
        "body": envelope["body_text"],
        "sender": sender,
        "attachments": [],
        "label_ids": ["INBOX", "UNREAD"],
    }


# ---------------------------------------------------------------------------
# Outbound: render outputs -> atfl_outbound envelope (+ A8S wire form)
# ---------------------------------------------------------------------------
def build_outbound_envelope(game_guid, turn_no, to_addr, subject,
                            body_text, body_html, composite_jpeg=None):
    """Engine -> Murph handoff (design doc §Outbound.2). The composite
    JPEG rides as one envelope attachment with a stable content id;
    the html body references cid:composite (the COMPOSITE_IMG_MARKER
    convention). Clarify/nudge are text-only -> attachments []."""
    attachments = []
    if composite_jpeg is not None:
        attachments.append({"content_id": "composite",
                            "filename": f"turn-{turn_no}-composite.jpg",
                            "bytes": composite_jpeg})
    return {"kind": OUTBOUND_KIND, "game_guid": game_guid,
            "turn_no": turn_no, "to": to_addr, "subject": subject,
            "body_text": body_text, "body_html": body_html,
            "attachments": attachments}


def envelope_for_wire(envelope):
    """Serialize an atfl_outbound envelope for `a8s tell`: JSON text
    with attachment bytes as base64-ascii (a8s tell bodies are text).
    The Murph-side sender accepts base64-ascii or raw bytes."""
    wire = dict(envelope)
    atts = []
    for att in envelope.get("attachments", []):
        a = dict(att)
        data = a.get("bytes", b"")
        if isinstance(data, str):  # already base64-ascii
            data.encode("ascii")  # raises if not ascii — fail loudly
        else:
            a["bytes"] = base64.b64encode(data).decode("ascii")
        atts.append(a)
    wire["attachments"] = atts
    return json.dumps(wire)


def envelope_from_wire(text):
    """Inverse of envelope_for_wire: JSON text -> envelope with raw
    bytes restored. Used by tests and by any Murph-side consumer."""
    envelope = json.loads(text)
    atts = []
    for att in envelope.get("attachments", []):
        a = dict(att)
        data = a.get("bytes", b"")
        if isinstance(data, str):
            a["bytes"] = base64.b64decode(data.encode("ascii"))
        atts.append(a)
    envelope["attachments"] = atts
    return envelope


# ---------------------------------------------------------------------------
# A8S plumbing: read this node's inbox dir, tell Murph
# ---------------------------------------------------------------------------
def _agents_dir():
    """Where a8s keeps per-node mailboxes. Overridable for tests."""
    return os.environ.get("ATFL_A8S_AGENTS_DIR",
                          os.path.expanduser("~/.config/a8s/agents"))


def _inbox_dir(node_name, agents_dir=None):
    return os.path.join(agents_dir or _agents_dir(), node_name, "inbox")


def poll_inbound(node_name, agents_dir=None):
    """Read atfl_inbound envelopes addressed to this engine node.

    Returns [(envelope, source_path)]. A file that doesn't parse as
    JSON, or isn't an atfl_inbound envelope, is skipped (logged) — it
    may belong to another consumer (e.g. roster calls like the
    "earth-changing" tells sitting in atfl-server's inbox). The caller
    moves processed files aside with consume_inbound(); files are never
    deleted here.

    Raises A8STransportError when the inbox directory itself can't be
    read (fail loudly, never silently report "no mail").
    """
    inbox = _inbox_dir(node_name, agents_dir)
    try:
        names = sorted(os.listdir(inbox))
    except OSError as e:
        raise A8STransportError(f"cannot read a8s inbox {inbox}: {e}")
    out = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(inbox, name)
        try:
            with open(path, "rb") as f:
                raw = f.read()
            envelope = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as e:
            # inbox.tmp / half-written subscriber files: leave for the
            # next cycle, never crash the poll loop on them.
            log.warning("relay: skipping unreadable inbox file %s: %s",
                        path, e)
            continue
        if not isinstance(envelope, dict) \
                or envelope.get("kind") != INBOUND_KIND:
            log.debug("relay: inbox file %s is not %s (kind=%r) — skipping",
                      path, INBOUND_KIND,
                      envelope.get("kind") if isinstance(envelope, dict)
                      else None)
            continue
        try:
            normalize_inbound(envelope)  # validate before handing off
        except A8STransportError as e:
            log.warning("relay: malformed %s envelope in %s: %s",
                        INBOUND_KIND, path, e)
            continue
        out.append((envelope, path))
    return out


def consume_inbound(source_path, node_name, agents_dir=None):
    """Move a processed inbox file to the node's trash/ (the `a8s drain`
    convention) so the next poll doesn't re-read it."""
    trash = os.path.join(agents_dir or _agents_dir(), node_name, "trash")
    os.makedirs(trash, exist_ok=True)
    shutil.move(source_path,
                os.path.join(trash, os.path.basename(source_path)))


def send_outbound(envelope, murph_node, node_root, a8s_bin=None,
                  timeout_s=60):
    """Hand one atfl_outbound envelope to Murph: `a8s tell <murph_node>`
    with the wire JSON on stdin. Retry once on failure, then raise
    A8STransportError (the mailer rewrite maps this to a loud `failed`
    outcome — §2.6: nothing half-sent).

    node_root is the engine node's root dir (the sender identity for
    the tell); TELL_OUTBOX_DIR is exported so `a8s tell` works from a
    bare shell (AGENTS.md 2026-09-29).
    """
    missing = [f for f in OUTBOUND_FIELDS if f not in envelope]
    if missing:
        raise A8STransportError(
            f"{OUTBOUND_KIND} envelope missing fields: {missing}")
    if envelope.get("kind") != OUTBOUND_KIND:
        raise A8STransportError(f"not an {OUTBOUND_KIND} envelope")
    wire = envelope_for_wire(envelope)
    cmd = [a8s_bin or os.path.expanduser("~/.ar3/a8s"),
           "tell", murph_node, "-"]
    env = dict(os.environ)
    env["TELL_OUTBOX_DIR"] = os.path.join(node_root, ".outbox")
    last_err = None
    for attempt in (1, 2):
        try:
            proc = subprocess.run(
                cmd, input=wire.encode("utf-8"), capture_output=True,
                cwd=node_root, env=env, timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as e:
            last_err = f"{type(e).__name__}: {e}"
        else:
            if proc.returncode == 0:
                return True
            last_err = (proc.stderr.decode(errors="replace").strip()
                        or proc.stdout.decode(errors="replace").strip()
                        or f"exit {proc.returncode}")
        log.warning("relay: tell to %s failed (attempt %d/2): %s",
                    murph_node, attempt, last_err)
    raise A8STransportError(
        f"a8s tell {murph_node} failed twice: {last_err}")


# ---------------------------------------------------------------------------
# Bug reports: engine -> Murph handoff (docs/playtest-bug-handling.md)
# ---------------------------------------------------------------------------
def build_bug_report_envelope(game_guid, bug_id, from_addr, subject,
                              body_text):
    """One filed bug report, handed to Murph over A8S. The wire form is
    plain JSON (no attachments). Routing note: the Murph-side consumer
    classifies POSITIVELY on kind == "atfl_outbound", so a bug report
    is NOT picked up by atfl_outbound_consumer — the tell blob stays in
    Murph's inbox, where the regular message checker surfaces it for
    triage. Nothing is ever mailed to the player by the engine for a
    bug (the resolution email goes out through Murph)."""
    from datetime import datetime, timezone
    return {"kind": BUG_REPORT_KIND, "game_guid": game_guid, "bug_id": bug_id,
            "from": from_addr, "subject": subject, "body_text": body_text,
            "reported_at": datetime.now(timezone.utc).isoformat()}


def send_bug_report(envelope, murph_node, node_root, a8s_bin=None,
                    timeout_s=60):
    """Hand one atfl_bug_report envelope to Murph: `a8s tell
    <murph_node>` with the wire JSON on stdin. Retry once, then raise
    A8STransportError (the mailer maps this to a loud `failed` outcome —
    §2.6: nothing half-sent; the inbox file is not consumed, so the
    next cycle retries)."""
    missing = [f for f in BUG_REPORT_FIELDS if f not in envelope]
    if missing:
        raise A8STransportError(
            f"{BUG_REPORT_KIND} envelope missing fields: {missing}")
    if envelope.get("kind") != BUG_REPORT_KIND:
        raise A8STransportError(f"not a {BUG_REPORT_KIND} envelope")
    wire = json.dumps(envelope)
    cmd = [a8s_bin or os.path.expanduser("~/.ar3/a8s"),
           "tell", murph_node, "-"]
    env = dict(os.environ)
    env["TELL_OUTBOX_DIR"] = os.path.join(node_root, ".outbox")
    last_err = None
    for attempt in (1, 2):
        try:
            proc = subprocess.run(
                cmd, input=wire.encode("utf-8"), capture_output=True,
                cwd=node_root, env=env, timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as e:
            last_err = f"{type(e).__name__}: {e}"
        else:
            if proc.returncode == 0:
                return True
            last_err = (proc.stderr.decode(errors="replace").strip()
                        or proc.stdout.decode(errors="replace").strip()
                        or f"exit {proc.returncode}")
        log.warning("relay: bug-report tell to %s failed (attempt %d/2): %s",
                    murph_node, attempt, last_err)
    raise A8STransportError(
        f"a8s tell {murph_node} failed twice (bug report): {last_err}")


# ---------------------------------------------------------------------------
# Selftest — the pinned contract as an executable spec
# ---------------------------------------------------------------------------
def _check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"selftest failed: {name}")


def selftest():
    import stat
    import tempfile

    # -- inbound contract (promoted from the pinned demo) --
    env = {"kind": INBOUND_KIND, "from": "Player@Example.com",
           "subject": "[ATFL a1b2c3d4] Above the Fog Line",
           "body_text": "I pick up the bottle.",
           "inkbox_message_id": "ink-9f31"}
    got = normalize_inbound(env)
    from .mailer import FakeGmail
    fake = FakeGmail()
    fake.queue_inbound("x@y.z", "s", "b")
    _check("inbound: key set == GmailClient normalized dict key set",
           set(got.keys()) == set(fake.inbox[0].keys()))
    _check("inbound: id is the inkbox dedupe key",
           got["id"] == "ink-9f31" and not got["id"].startswith("in-"))
    _check("inbound: sender lowercased, from preserved raw",
           got["sender"] == "player@example.com"
           and got["from"] == "Player@Example.com")
    _check("inbound: subject/body verbatim (GUID prefix survives)",
           got["subject"].startswith("[ATFL")
           and got["body"] == "I pick up the bottle.")
    _check("inbound: thread_id/header_message_id None",
           got["thread_id"] is None and got["header_message_id"] is None)
    _check("inbound: no attachments; presented as new mail",
           got["attachments"] == []
           and got["label_ids"] == ["INBOX", "UNREAD"])
    try:
        normalize_inbound({"kind": INBOUND_KIND, "from": "a@b.c"})
        _check("inbound: missing fields raise", False)
    except A8STransportError:
        _check("inbound: missing fields raise", True)
    try:
        normalize_inbound({"kind": "nope"})
        _check("inbound: wrong kind raises", False)
    except A8STransportError:
        _check("inbound: wrong kind raises", True)

    # -- outbound builder (promoted from the pinned demo) --
    out = build_outbound_envelope(
        "guid-1", 3, "player@example.com",
        "[ATFL a1b2c3d4] Above the Fog Line — turn 3",
        "Day 1 · 10:00 · morning\n\nnarrative here",
        '<html>narrative here <img src="cid:composite"></html>',
        composite_jpeg=b"\xff\xd8fake-jpeg\xff\xd9")
    _check("outbound: envelope keys exactly the pinned set",
           set(out.keys()) == set(OUTBOUND_FIELDS))
    _check("outbound: composite rides as one cid attachment, bytes intact",
           len(out["attachments"]) == 1
           and out["attachments"][0]["content_id"] == "composite"
           and out["attachments"][0]["filename"] == "turn-3-composite.jpg"
           and out["attachments"][0]["bytes"] == b"\xff\xd8fake-jpeg\xff\xd9")
    _check("outbound: text-only -> attachments []",
           build_outbound_envelope("g", 1, "t", "s", "b", "<h>")
           ["attachments"] == [])

    # -- wire round trip --
    wire = envelope_for_wire(out)
    _check("wire: serialized form is plain text", isinstance(wire, str))
    back = envelope_from_wire(wire)
    _check("wire: round trip restores envelope + bytes",
           back["kind"] == OUTBOUND_KIND and back["game_guid"] == "guid-1"
           and back["attachments"][0]["bytes"] == b"\xff\xd8fake-jpeg\xff\xd9")
    _check("wire: base64-ascii in the serialized text",
           base64.b64encode(b"\xff\xd8fake-jpeg\xff\xd9").decode("ascii")
           in wire)

    # -- bug-report envelope (docs/playtest-bug-handling.md routing) --
    br = build_bug_report_envelope("guid-9", 7, "masta@gibdon.com",
                                   "BUG: map label wrong",
                                   "the map showed a place I've never been")
    _check("bugreport: envelope keys exactly the pinned set",
           set(br.keys()) == set(BUG_REPORT_FIELDS))
    _check("bugreport: kind is distinct from atfl_outbound",
           br["kind"] == BUG_REPORT_KIND
           and br["kind"] != OUTBOUND_KIND)
    _check("bugreport: carries game, bug id, reporter, body",
           br["game_guid"] == "guid-9" and br["bug_id"] == 7
           and br["from"] == "masta@gibdon.com"
           and br["body_text"].startswith("the map showed"))
    _check("bugreport: wire form is plain JSON text",
           json.loads(json.dumps(br))["kind"] == BUG_REPORT_KIND)
    try:
        bugtmp = tempfile.mkdtemp(prefix="relay-bugreport-")
        send_bug_report({"kind": BUG_REPORT_KIND, "game_guid": "g"},
                        "murph", bugtmp, a8s_bin="/bin/false", timeout_s=5)
        _check("bugreport: missing fields raise", False)
    except A8STransportError:
        _check("bugreport: missing fields raise", True)
    try:
        bad = dict(br)
        bad["kind"] = OUTBOUND_KIND
        send_bug_report(bad, "murph", bugtmp, a8s_bin="/bin/false", timeout_s=5)
        _check("bugreport: wrong kind raises", False)
    except A8STransportError:
        _check("bugreport: wrong kind raises", True)
    try:
        send_bug_report(br, "murph", bugtmp, a8s_bin="/bin/false", timeout_s=5)
        _check("bugreport: a8s failure raises loudly (no half-send)", False)
    except A8STransportError as e:
        _check("bugreport: a8s failure raises loudly (no half-send)",
               "failed twice" in str(e))

    # -- poll_inbound against a fake agents dir --
    tmp = tempfile.mkdtemp(prefix="relay-inbox-")
    inbox = os.path.join(tmp, "atfl-server", "inbox")
    os.makedirs(inbox)
    good = {"kind": INBOUND_KIND, "from": "p@e.c", "subject": "[ATFL x] t",
            "body_text": "hi", "inkbox_message_id": "ink-a"}
    with open(os.path.join(inbox, "m1.json"), "w") as f:
        json.dump(good, f)
    with open(os.path.join(inbox, "roster.json"), "w") as f:  # other consumer
        json.dump({"kind": "pick_plot", "from": "fogline-gm:keeper",
                   "to": "atfl-server", "content": "x"}, f)
    with open(os.path.join(inbox, "half.tmp"), "w") as f:  # subscriber mid-write
        f.write('{"kind": "atfl_inb')
    with open(os.path.join(inbox, "note.txt"), "w") as f:
        f.write("not json")
    found = poll_inbound("atfl-server", agents_dir=tmp)
    _check("poll: finds exactly the atfl_inbound envelope",
           len(found) == 1 and found[0][0]["inkbox_message_id"] == "ink-a")
    consume_inbound(found[0][1], "atfl-server", agents_dir=tmp)
    _check("poll: consumed file moved to trash, inbox empty of it",
           not os.path.exists(found[0][1])
           and os.path.exists(os.path.join(tmp, "atfl-server", "trash",
                                           "m1.json"))
           and poll_inbound("atfl-server", agents_dir=tmp) == [])
    try:
        poll_inbound("no-such-node", agents_dir=tmp)
        _check("poll: missing inbox dir raises loudly", False)
    except A8STransportError:
        _check("poll: missing inbox dir raises loudly", True)

    # -- send_outbound against a fake `a8s` (hermetic; no network) --
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    fake_a8s = os.path.join(bindir, "a8s")
    rec = os.path.join(tmp, "tell-records.txt")
    with open(fake_a8s, "w") as f:
        f.write("#!/bin/bash\n"
                f"echo \"cwd=$PWD tell_outbox_dir=$TELL_OUTBOX_DIR args=$*\" >> {rec}\n"
                f"cat >> {rec}.stdin\n"
                "echo 'tell -> ok'\n"
                "exit 0\n")
    os.chmod(fake_a8s, os.stat(fake_a8s).st_mode | stat.S_IEXEC)
    root = os.path.join(tmp, "node-root")
    os.makedirs(os.path.join(root, ".outbox"))
    send_outbound(out, "murph", root, a8s_bin=fake_a8s)
    with open(rec) as f:
        rec_line = f.readline().strip()
    with open(rec + ".stdin") as f:
        stdin_text = f.read()
    _check("send: tell invoked with node + stdin mode from node root",
           "args=tell murph -" in rec_line and f"cwd={root}" in rec_line)
    _check("send: TELL_OUTBOX_DIR exported (bare-shell tell needs it)",
           f"tell_outbox_dir={root}/.outbox" in rec_line)
    _check("send: wire JSON delivered on stdin, bytes intact",
           envelope_from_wire(stdin_text)["attachments"][0]["bytes"]
           == b"\xff\xd8fake-jpeg\xff\xd9")
    with open(fake_a8s, "w") as f:  # now always fail -> retry exhausted
        f.write("#!/bin/bash\necho 'tell: boom' >&2\nexit 1\n")
    try:
        send_outbound(out, "murph", root, a8s_bin=fake_a8s)
        _check("send: double failure raises loudly", False)
    except A8STransportError as e:
        _check("send: double failure raises loudly (retry once)",
               "failed twice" in str(e))
    try:
        send_outbound(dict(out, kind="nope"), "murph", root, a8s_bin=fake_a8s)
        _check("send: wrong kind rejected before any tell", False)
    except A8STransportError:
        _check("send: wrong kind rejected before any tell", True)

    print("\nmurph_relay selftest: all checks green.")


if __name__ == "__main__":
    selftest()
