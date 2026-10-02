"""Murph-relay adapter contract demo — docs/mail-relay-design.md, pinned
hermetically BEFORE the queued mailer.py/config.py rewrite.

The relay moves the engine's mail surface to A8S tells handled by Murph
(engine -> Murph -> player, player -> Murph -> engine; Neil's 2026-09-30
decision). This demo pins the exact contract the rewrite must implement:

  inbound:  atfl_inbound envelope -> normalized inbound dict identical in
            KEYS to what FakeGmail.queue_inbound produces today (the
            "same normalized-dict contract" the design doc promises), so
            dispatch/extract_guid/match_game work unmodified;
  filter:   Murph-side forward/anti-loop rules (decision 2026-09-30);
  dedupe:   inkbox_message_id as the seen-set key, through mailer's real
            _store_seen/_load_seen;
  outbound: atfl_outbound envelope builder from render outputs.

No network, no A8S, no mailbox. Decisions made here (2026-10-02) are the
rewrite's spec; the demo fails loudly if any of them regress.
"""
import sys
import tempfile

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from server.mailer import FakeGmail, _store_seen, _load_seen  # noqa: E402

RELAY_ADDRESS = "murph@inkboxmail.com"
TAG = "[ATFL"
SIGNUP_PHRASE = "above the fog line"

_n = 0


def check(name, cond):
    global _n
    _n += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"demo failed: {name}")


# ---------------------------------------------------------------------------
# Inbound: atfl_inbound envelope -> normalized inbound dict
# ---------------------------------------------------------------------------
def relay_inbound_get(envelope):
    """Adapter: {kind, from, subject, body_text, inkbox_message_id}
    -> the normalized dict GmailClient.get yields.

    Decisions (2026-10-02):
    - thread_id=None: the engine has no reader of thread_id (verified:
      only FakeGmail.list maps it; routing is sender+subject/body, and
      threading moved to Murph's side per the design doc).
    - header_message_id=None: In-Reply-To ownership moved to Murph;
      send_outcome's `state.get(...) or inbound.get(...)` chain tolerates
      the missing key.
    - attachments=[]: forwards carry no attachment metadata; the
      "logged-never-acted-on" rule holds trivially.
    - date stamped at forward time: nothing in the server reads the
      inbound date back (only gmail_adapter sets it).
    """
    assert envelope["kind"] == "atfl_inbound", envelope.get("kind")
    sender = envelope["from"].strip().lower()
    from datetime import datetime, timezone
    return {
        "id": envelope["inkbox_message_id"],
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


def test_inbound_contract():
    env = {"kind": "atfl_inbound", "from": "Player@Example.com",
           "subject": "[ATFL a1b2c3d4] Above the Fog Line",
           "body_text": "I pick up the bottle.",
           "inkbox_message_id": "ink-9f31"}
    got = relay_inbound_get(env)

    fake = FakeGmail()
    fake_mid = fake.queue_inbound("x@y.z", "s", "b")
    fake_keys = set(fake.inbox[0].keys())

    check("inbound: key set == FakeGmail normalized dict key set",
          set(got.keys()) == fake_keys)
    check("inbound: id is the inkbox dedupe key, not a gmail id",
          got["id"] == "ink-9f31" and not got["id"].startswith("in-"))
    check("inbound: sender lowercased, from preserved raw",
          got["sender"] == "player@example.com"
          and got["from"] == "Player@Example.com")
    check("inbound: subject/body verbatim (GUID prefix survives)",
          got["subject"].startswith(TAG) and got["body"] == "I pick up the bottle.")
    check("inbound: thread_id None (engine has no reader; Murph threads)",
          got["thread_id"] is None)
    check("inbound: header_message_id None (In-Reply-To is Murph's)",
          got["header_message_id"] is None)
    check("inbound: no attachments on forwards", got["attachments"] == [])
    check("inbound: presented as new mail", got["label_ids"] == ["INBOX", "UNREAD"])


# ---------------------------------------------------------------------------
# Murph-side filter (decision 2026-09-30, design doc §Inbound.3)
# ---------------------------------------------------------------------------
def relay_should_forward(mail):
    """mail: {from, subject, body}. Anti-loop FIRST, then the forward rule:
    subject contains '[ATFL' (subject only), or subject/body mentions
    'above the fog line' (the signup path)."""
    if mail["from"].strip().lower() == RELAY_ADDRESS:
        return False  # anti-loop: Murph's own mail is never forwarded
    if TAG in (mail.get("subject") or ""):
        return True
    hay = ((mail.get("subject") or "") + "\n"
           + (mail.get("body") or "")).lower()
    return SIGNUP_PHRASE in hay


def test_filter():
    me = "player@example.com"
    check("filter: tagged turn mail forwards",
          relay_should_forward({"from": me, "subject": "[ATFL a1b2c3d4] Re: turn",
                                "body": "I walk down."}))
    check("filter: signup phrase in body forwards (no tag)",
          relay_should_forward({"from": me, "subject": "hello?",
                                "body": "I want to play above the fog line"}))
    check("filter: signup phrase in subject forwards",
          relay_should_forward({"from": me, "subject": "Above the Fog Line?",
                                "body": "how do I start"}))
    check("filter: tag in body alone does NOT forward (subject-only rule)",
          not relay_should_forward({"from": me, "subject": "hi",
                                    "body": "saw [ATFL somewhere"}))
    check("filter: unrelated mail dropped",
          not relay_should_forward({"from": me, "subject": "lunch?",
                                    "body": "tacos at noon"}))
    check("filter: anti-loop beats the tag (Murph's own mail)",
          not relay_should_forward({"from": RELAY_ADDRESS,
                                    "subject": "[ATFL a1b2c3d4] Above the Fog Line",
                                    "body": "Day 1..."}))
    check("filter: anti-loop is case-insensitive on From",
          not relay_should_forward({"from": "Murph@InkboxMail.com",
                                    "subject": "x", "body": "y"}))
    check("filter: signup phrase match is case-insensitive",
          relay_should_forward({"from": me, "subject": "ABOVE THE FOG LINE",
                                "body": ""}))


# ---------------------------------------------------------------------------
# Dedupe: inkbox_message_id through mailer's real seen-set
# ---------------------------------------------------------------------------
def test_dedupe():
    games_dir = tempfile.mkdtemp(prefix="relay-dedupe-")
    env1 = {"kind": "atfl_inbound", "from": "a@b.c", "subject": "s",
            "body_text": "b", "inkbox_message_id": "ink-aaa"}
    env2 = dict(env1, inkbox_message_id="ink-aaa")  # redelivered forward
    env3 = dict(env1, inkbox_message_id="ink-bbb")

    def fresh_ids(seen, envelopes):
        out = []
        for e in envelopes:
            m = relay_inbound_get(e)
            if m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
        return out

    seen = set(_load_seen(games_dir))
    got1 = fresh_ids(seen, [env1, env2])
    check("dedupe: redelivered forward collapses to one inbound", len(got1) == 1)
    _store_seen(games_dir, [m["id"] for m in got1])

    seen2 = set(_load_seen(games_dir))  # across "restarts"
    got2 = fresh_ids(seen2, [env2, env3])
    check("dedupe: seen-set persists; only the new id passes",
          [m["id"] for m in got2] == ["ink-bbb"])
    check("dedupe: seen-set holds opaque inkbox ids (no gmail shape)",
          "ink-aaa" in _load_seen(games_dir))


# ---------------------------------------------------------------------------
# Outbound: render outputs -> atfl_outbound envelope
# ---------------------------------------------------------------------------
def build_outbound_envelope(game_guid, turn_no, to_addr, subject,
                            body_text, body_html, composite_jpeg=None):
    """Engine -> Murph handoff (design doc §Outbound.2). Composite JPEGs
    ride as envelope attachments with a stable content id; the html body
    references cid:composite (the COMPOSITE_IMG_MARKER convention)."""
    attachments = []
    if composite_jpeg is not None:
        attachments.append({"content_id": "composite",
                            "filename": f"turn-{turn_no}-composite.jpg",
                            "bytes": composite_jpeg})
    return {"kind": "atfl_outbound", "game_guid": game_guid,
            "turn_no": turn_no, "to": to_addr, "subject": subject,
            "body_text": body_text, "body_html": body_html,
            "attachments": attachments}


def test_outbound():
    env = build_outbound_envelope(
        "guid-1", 3, "player@example.com",
        "[ATFL a1b2c3d4] Above the Fog Line — turn 3",
        "Day 1 · 10:00 · morning\n\nnarrative here",
        "<html>narrative here <img src=\"cid:composite\"></html>",
        composite_jpeg=b"\xff\xd8fake-jpeg\xff\xd9")
    check("outbound: envelope keys exactly the doc's set",
          set(env.keys()) == {"kind", "game_guid", "turn_no", "to",
                              "subject", "body_text", "body_html",
                              "attachments"})
    check("outbound: kind + routing fields pass through",
          env["kind"] == "atfl_outbound" and env["game_guid"] == "guid-1"
          and env["turn_no"] == 3 and env["to"] == "player@example.com")
    check("outbound: subject keeps the [ATFL guid8] tag for the filter",
          env["subject"].startswith(TAG))
    att = env["attachments"]
    check("outbound: composite rides as one cid attachment, bytes intact",
          len(att) == 1 and att[0]["content_id"] == "composite"
          and att[0]["filename"] == "turn-3-composite.jpg"
          and att[0]["bytes"] == b"\xff\xd8fake-jpeg\xff\xd9")
    check("outbound: html references cid:composite",
          "cid:composite" in env["body_html"])

    text_only = build_outbound_envelope("guid-1", 4, "p@e.c", "s", "t", "<h>t</h>")
    check("outbound: text-only (clarify/nudge) -> empty attachments",
          text_only["attachments"] == [])


if __name__ == "__main__":
    test_inbound_contract()
    test_filter()
    test_dedupe()
    test_outbound()
    print(f"\n{_n} checks green — relay adapter contract pinned.")
