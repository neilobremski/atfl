"""Gmail adapter demo — server/gmail_adapter.py against a stubbed
users().messages() resource. No network, no real mailbox: proves the
list/get/send/modify mapping, RFC Message-ID threading, skip_ids
quota-light polling, and the persistent seen-set across restarts.
"""
import base64
import sqlite3
import sys
import tempfile
from email import policy
from email.parser import BytesParser

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from server.gmail_adapter import GoogleApiGmail
from server.mailer import run_poll_cycle, poll_inbox, GAME_ADDRESS
from server.gm import MockGM

_MSG = BytesParser(policy=policy.default)
PLAYER = "player@example.com"
GAME = GAME_ADDRESS


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"demo failed: {name}")


class _Exec:
    def __init__(self, thunk):
        self._thunk = thunk

    def execute(self):
        return self._thunk()


def _b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def text_part(text, mime="text/plain"):
    return {"mimeType": mime, "filename": "",
            "body": {"data": _b64(text), "size": len(text)}}


def full_msg(mid, thread, headers, parts, labels=("INBOX", "UNREAD")):
    return {"id": mid, "threadId": thread, "labelIds": list(labels),
            "payload": {"mimeType": "multipart/mixed",
                        "headers": [{"name": k, "value": v}
                                    for k, v in headers.items()],
                        "parts": parts}}


class StubMessages:
    """users().messages()-shaped stub: chained .execute() calls, canned
    pages and format=full payloads, full call log for assertions."""

    def __init__(self):
        self.pages = []
        self.by_id = {}
        self.meta_by_id = {}
        self.sent = []
        self.calls = {"list": [], "get": [], "send": [], "modify": []}
        self._n = 0

    # -- chained resource surface --
    def list(self, **kw):
        self.calls["list"].append(kw)
        idx = int(kw["pageToken"]) if kw.get("pageToken") else 0
        page = self.pages[idx] if idx < len(self.pages) else {"messages": []}
        out = {"messages": page["messages"]}
        if idx + 1 < len(self.pages):
            out["nextPageToken"] = str(idx + 1)
        return _Exec(lambda: out)

    def get(self, **kw):
        self.calls["get"].append(kw)
        mid = kw["id"]
        if kw.get("format") == "metadata":
            rfc = self.meta_by_id.get(mid, f"<{mid}@stub>")
            return _Exec(lambda: {
                "id": mid,
                "payload": {"headers": [{"name": "Message-ID",
                                         "value": rfc}]}})
        return _Exec(lambda: self.by_id[mid])

    def send(self, **kw):
        self.calls["send"].append(kw)
        self._n += 1
        sid = f"gmail-sent-{self._n}"
        self.sent.append({"raw": base64.urlsafe_b64decode(kw["body"]["raw"]),
                          "id": sid})
        self.meta_by_id[sid] = f"<{sid}@stub>"
        return _Exec(lambda: {"id": sid, "threadId": f"thread-{sid}"})

    def modify(self, **kw):
        self.calls["modify"].append(kw)
        return _Exec(lambda: {})


def parsed(raw):
    return _MSG.parsebytes(raw)

def game_db_path(games_dir):
    """The <GUID>.db game file (not mailer.db)."""
    names = [n for n in __import__("os").listdir(games_dir)
             if n.endswith(".db") and len(n) == 39]
    assert len(names) == 1, names
    return f"{games_dir}/{names[0]}"




# --- 1-7. list/get mapping, pagination, normalization ---
stub = StubMessages()
gmail = GoogleApiGmail(stub)

stub.by_id["m1"] = full_msg(
    "m1", "th-1",
    {"From": "Some Player <Player@Example.COM>", "To": GAME,
     "Subject": "start", "Date": "Sat, 26 Sep 2026 21:00:00 -0700",
     "Message-ID": "<signup-1@stub>"},
    [text_part("hello plain"),
     text_part("<b>hi</b> html", "text/html")])
stub.by_id["m2"] = full_msg(
    "m2", "th-1",
    {"From": f"Above the Fog Line <{GAME}>", "To": GAME,
     "Subject": "echo", "Message-ID": "<echo-2@stub>"},
    [text_part("our own send")])
stub.by_id["m3"] = full_msg(
    "m3", "th-3",
    {"From": "friend@example.com", "To": GAME, "Subject": "pic",
     "Message-ID": "<pic-3@stub>"},
    [text_part("see attached"),
     {"mimeType": "image/png", "filename": "pic.png",
      "body": {"attachmentId": "att1", "size": 1234}}])
stub.pages = [
    {"messages": [{"id": "m1", "threadId": "th-1"},
                  {"id": "m2", "threadId": "th-1"}]},
    {"messages": [{"id": "m3", "threadId": "th-3"}]},
]

res = poll_inbox(gmail)
check("pagination merged two pages, own-address dropped (anti-loop)",
      [m["id"] for m in res] == ["m1", "m3"])
check("poll query carries is:unread",
      "is:unread" in stub.calls["list"][0]["q"])
m1 = res[0]
check("text/plain preferred over text/html", m1["body"] == "hello plain")
check("RFC Message-ID parsed", m1["header_message_id"] == "<signup-1@stub>")
check("sender parsed and lowercased", m1["sender"] == "player@example.com")
check("attachment is metadata only (filename + size, no content)",
      res[1]["attachments"] == [{"filename": "pic.png", "size_bytes": 1234}])
check("attachment not leaked into body", res[1]["body"] == "see attached")

n_gets = len(stub.calls["get"])
res2 = poll_inbox(gmail, skip_ids={"m1"})
check("skip_ids avoids the get call entirely",
      all(c["id"] != "m1" for c in stub.calls["get"][n_gets:]))
check("skip_ids result omits the seen message",
      [m["id"] for m in res2] == ["m3"])

# --- 8-9. send returns the RFC Message-ID via one metadata get ---
from server.mailer import build_raw
raw = build_raw(GAME, PLAYER, "subj", "body")
r = gmail.send(base64.urlsafe_b64encode(raw).decode())
check("send returns gmail id + thread + RFC message id",
      r["id"] == "gmail-sent-1" and r["threadId"] == "thread-gmail-sent-1"
      and r["message_id"] == "<gmail-sent-1@stub>")
check("send did exactly one metadata follow-up get",
      sum(1 for c in stub.calls["get"]
          if c.get("format") == "metadata"
          and "Message-ID" in c.get("metadataHeaders", [])) == 1)
check("sent raw carries an explicit Message-ID",
      parsed(stub.sent[0]["raw"])["Message-ID"] is not None)

gmail.mark_read("m1")
check("mark_read removes UNREAD",
      stub.calls["modify"][-1]["body"] == {"removeLabelIds": ["UNREAD"]})

# --- 10-12. end to end: signup -> threaded turns on RFC Message-IDs ---
games_dir = tempfile.mkdtemp(prefix="atfl-adapter-")
stub2 = StubMessages()
g2 = GoogleApiGmail(stub2)
gm = MockGM()

stub2.by_id["s1"] = full_msg(
    "s1", "th-s1",
    {"From": PLAYER, "To": GAME, "Subject": "start",
     "Date": "Sat, 26 Sep 2026 21:05:00 -0700",
     "Message-ID": "<signup-1@stub>"},
    [text_part("start")])
stub2.pages = [{"messages": [{"id": "s1", "threadId": "th-s1"}]}]
seen = set()
out = run_poll_cycle(games_dir, g2, gm, seen_ids=seen)
check("signup ran a turn", out["sent"][0]["action"] == "turn_email")
t1 = parsed(stub2.sent[0]["raw"])
check("turn 1 In-Reply-To is the signup's RFC Message-ID",
      t1["In-Reply-To"] == "<signup-1@stub>")

db = sqlite3.connect(game_db_path(games_dir))
db.row_factory = sqlite3.Row
grow = db.execute("SELECT guid, thread_message_id FROM games").fetchone()
guid = grow["guid"]
check("thread bookkeeping stores the RFC Message-ID, not the Gmail id",
      grow["thread_message_id"] == "<gmail-sent-1@stub>"
      and grow["thread_message_id"] != "gmail-sent-1")
turns_after_signup = db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
db.close()

stub2.by_id["s2"] = full_msg(
    "s2", "th-s1",
    {"From": PLAYER, "To": GAME,
     "Subject": "Re: Above the Fog Line",
     "Date": "Sat, 26 Sep 2026 21:10:00 -0700",
     "Message-ID": "<reply-2@stub>"},
    [text_part(f"drink\n\nGame code: {guid}")])
stub2.pages = [{"messages": [{"id": "s2", "threadId": "th-s1"}]}]
out = run_poll_cycle(games_dir, g2, gm, seen_ids=seen)
t2 = parsed(stub2.sent[1]["raw"])
check("turn 2 In-Reply-To is turn 1's RFC Message-ID",
      t2["In-Reply-To"] == "<gmail-sent-1@stub>")
check("References accumulates the full chain",
      "<signup-1@stub>" in (t2["References"] or "")
      and "<gmail-sent-1@stub>" in (t2["References"] or ""))

# --- 13. restart with a fresh in-memory seen-set: the persisted
# seen-set (mailer.db) stops the still-unread s2 from re-dispatching ---
db = sqlite3.connect(game_db_path(games_dir))
turns_before = db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
db.close()
sends_before = len(stub2.sent)
gets_before = len([c for c in stub2.calls["get"] if c["id"] == "s2"])
stub2.pages = [{"messages": [{"id": "s2", "threadId": "th-s1"}]}]  # still unread
out = run_poll_cycle(games_dir, g2, gm, seen_ids=set())  # new process
db = sqlite3.connect(game_db_path(games_dir))
turns_after = db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
db.close()
check("restart: no duplicate turn ran",
      turns_after == turns_before and len(stub2.sent) == sends_before)
check("restart: no get call for the already-seen message",
      len([c for c in stub2.calls["get"] if c["id"] == "s2"]) == gets_before)

# --- 14. build_service refuses until the game account exists ---
from server.gmail_adapter import build_service
try:
    build_service()
    check("build_service raises without a token path", False)
except RuntimeError as e:
    check("build_service raises without a token path",
          "open question #1" in str(e))

print("\ngmail_adapter demo: all green — no network touched, no live mailbox.")
