"""Real Gmail API adapter — DESIGN.md §1.1 + research/phase0-email-identity.md.

GoogleApiGmail implements server.mailer.GmailClient over the Gmail REST
API (users.messages.list/get/send/modify). The `messages` resource is
injected — shaped like googleapiclient's users().messages() — so tests
run against a stub and no network call happens without an explicit,
configured service.

NOT wired to any account yet: build_service() raises until the game's
dedicated Google account exists (open question #1). Nothing in this
module touches Neil's mailbox; demos exercise it through a stub.

Quota-light posture:
  - list is cheap; get() runs only for ids the mailer hasn't seen
    (mailer.poll_inbox(skip_ids=...) skips them before the get call)
  - get uses format=full: headers + body in one call; attachments are
    metadata-only (filename + size) — content is never downloaded
  - send is one call plus one metadata get for the sent RFC Message-ID
    (what In-Reply-To/References need for correct threading)
  - modify (mark read) is best-effort; the poller retries implicitly
"""
import base64
import html as _html
import os
import re

from .mailer import GmailClient

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _headers(payload):
    return {h["name"].lower(): h["value"]
            for h in payload.get("headers", [])}


def _parse_addr(value):
    """'Name <a@b>' -> 'a@b' (lowercased)."""
    v = (value or "").strip()
    if "<" in v and v.endswith(">"):
        v = v[v.index("<") + 1:-1]
    return v.strip().lower()


def _b64decode(data):
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", errors="replace")


def _walk_parts(part):
    yield part
    for sub in part.get("parts", []) or []:
        yield from _walk_parts(sub)


def _strip_html(s):
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", s))).strip()


def _extract_body(payload):
    """Best plain-text body: prefer the first text/plain part; fall back
    to any other text part (HTML tag-stripped). Attachment parts (any
    part with a filename) are skipped — metadata only, never downloaded."""
    plain, other = None, None
    for part in _walk_parts(payload):
        if part.get("filename"):
            continue
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        text = _b64decode(data).strip()
        if mime == "text/plain" and plain is None:
            plain = text
        elif mime.startswith("text/") and other is None:
            other = _strip_html(text) if mime == "text/html" else text
    return plain if plain is not None else (other or "")


def _extract_attachments(payload):
    """[{filename, size_bytes}] — metadata only, content never fetched."""
    out = []
    for part in _walk_parts(payload):
        name = part.get("filename")
        if not name:
            continue
        out.append({"filename": name,
                    "size_bytes": (part.get("body") or {}).get("size", 0)})
    return out


def normalize_message(message):
    """A Gmail API format=full message -> the normalized dict that
    mailer's GmailClient.get promises (id, thread_id, header_message_id,
    from, sender, to, subject, date, body, attachments, label_ids)."""
    payload = message.get("payload", {})
    heads = _headers(payload)
    return {
        "id": message["id"],
        "thread_id": message.get("threadId"),
        "header_message_id": heads.get("message-id"),  # RFC Message-ID
        "from": heads.get("from"),
        "sender": _parse_addr(heads.get("from")),
        "to": heads.get("to"),
        "subject": heads.get("subject"),
        "date": heads.get("date"),
        "body": _extract_body(payload),
        "attachments": _extract_attachments(payload),
        "label_ids": message.get("labelIds", []),
    }


class GoogleApiGmail(GmailClient):
    """GmailClient over an injected users().messages()-shaped resource."""

    def __init__(self, messages_resource, user_id="me"):
        self._m = messages_resource
        self._user = user_id

    def list(self, query, max_results=50, page_token=None):
        kw = {"userId": self._user, "q": query, "maxResults": max_results}
        if page_token:
            kw["pageToken"] = page_token
        resp = self._m.list(**kw).execute()
        out = {"messages": resp.get("messages", [])}
        if resp.get("nextPageToken"):
            out["nextPageToken"] = resp["nextPageToken"]
        return out

    def get(self, message_id):
        msg = self._m.get(userId=self._user, id=message_id,
                          format="full").execute()
        return normalize_message(msg)

    def send(self, raw_b64):
        sent = self._m.send(userId=self._user,
                            body={"raw": raw_b64}).execute()
        # The send response carries only Gmail ids. The RFC Message-ID —
        # what In-Reply-To/References need — comes from one metadata get.
        meta = self._m.get(userId=self._user, id=sent["id"],
                           format="metadata",
                           metadataHeaders=["Message-ID"]).execute()
        rfc = _headers(meta.get("payload", {})).get("message-id")
        return {"id": sent["id"], "threadId": sent.get("threadId"),
                "message_id": rfc or sent["id"]}

    def mark_read(self, message_id):
        self._m.modify(userId=self._user, id=message_id,
                       body={"removeLabelIds": ["UNREAD"]}).execute()


def build_service(token_path=None):
    """Build the real Gmail API service once the game account exists.

    token_path: authorized_user JSON for the game's dedicated Google
    account (gmail.modify scope). Raises until open question #1 (the
    game email identity) is closed — the adapter stays stub-tested
    until then, and no live mailbox is ever touched before that."""
    if not token_path or not os.path.exists(token_path):
        raise RuntimeError(
            "game Gmail account not configured yet (open question #1: "
            "the game's email identity). Real wiring happens after Neil "
            "provides the dedicated account.")
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    return build("gmail", "v1", credentials=creds)


def messages_resource(service):
    """service.users().messages() — the object GoogleApiGmail consumes."""
    return service.users().messages()
