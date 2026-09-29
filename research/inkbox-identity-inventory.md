# Inkbox identity inventory (@murph) — 2026-09-29

Neil asked (digest reply, Sep 29 06:28 PDT): "You have an Inkbox API key for
yourself (@murph) — what does that get you?" Answer from live API probing
(`whoami`, `identity-list`, `channel-status`, OpenAPI spec at
https://inkbox.ai/api/openapi.json), all 2026-09-29 ~07:10 PDT:

## What the key is
- Agent-scoped API key, label "Murph", created 2026-09-23, **no expiry**.
- Scope: `agent_identity:063042fc-be4f-4477-bbac-0cc9bfe6b60c` (murph only).

## What @murph gets
1. **Email identity + mailbox: murph@inkboxmail.com** — active, on the
   verified platform sending domain `inkboxmail.com`. Full mail REST API:
   send (with **`body_html`**, `body_text`, attachments, threading fields,
   `track_opens`), list/search/threads/drafts (with attachments), reply,
   reply-all, forward, read/unread flags, contact rules
   (allow/blacklist, inbound + outbound). Mailbox currently empty
   (`/api/v1/mail/messages` → `{"items":[]}`).
2. **iMessage identity** — enabled (already the channel the iMessage poller
   uses); no dedicated iMessage number assigned (`imessage_number: null`).
3. **Public tunnel: murph.inkboxwire.com** (TLS edge) — webhook delivery
   target. `message.received` webhook exists: fire-and-forget POST to
   `/mail-event` when inbound mail is accepted. Push-based inbound is
   possible — no polling needed.
4. **Calling available** (voice); SMS not available; no phone number assigned.

## Implications for the game identity question
- Inkbox-with-email is real and ready today: the game could send rich-HTML
  turn emails and receive player replies at murph@inkboxmail.com with no
  new account, no cost, no OAuth walkthrough.
- `body_html` on send directly answers Neil's rich-email ask (Sep 29):
  HTML turn emails are a first-class field, not MIME assembly.
- Open question for Neil: the identity is **murph**, not the game. Options:
  (a) use murph@inkboxmail.com as the game sender, (b) create a
  game-specific Inkbox identity/mailbox (identity creation is not exposed
  in the agent API — likely needs Neil or Inkbox-side setup), (c) keep the
  game on Neil's Gmail for now.
- Local CLI (`~/workspace/skills/inkbox/bin/inkbox`) currently wraps only
  iMessage endpoints; mail endpoints need either CLI additions or direct
  REST calls with the surrogate credential helper.
