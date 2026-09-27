# Phase 0 — game email identity: research & decision (2026-09-25)

## The question
Where should the game's email live — the address players write to, and the
address game turns come from? Constraints: free/cheap (side project), no
deliverability wars, works from the Oracle free VM (`free-micro-1`).

## Volume model
One turn ≈ one email/player/day. A month of play ≈ ~30 turns/player.
Even with a dozen players, that's far under 100 sends/day.

## Options compared

### A. Gmail API with a dedicated game address (recommended)
- A free Google account just for the game (e.g. `abovethefogline@gmail.com`).
- Send via Gmail API `messages.send`: free accounts get **500 recipients/day**
  (researched 2026-09-25; per Google support + current guides). Our volume is
  a rounding error next to that.
- Receive by polling `messages.list` from the Oracle VM every few minutes.
  List costs ~1 API quota unit — trivial. Gmail→Gmail deliverability is
  excellent, and there's no MX/DNS work at all.
- Cost: $0. New accounts: none paid — just a second Google account.
- The polling pattern is the same one already proven in the project's
  message-checking routine, and multi-account support exists if needed.
- Push via Pub/Sub exists as a later optimization; polling is fine at this scale.

### B. Transactional service (Resend / Brevo / SES)
- Resend free tier: 3,000/mo, capped 100/day — volume-adequate, but it
  **only sends from a domain you verify via DNS**, which means buying and
  managing a domain (real cost) plus setting up SPF/DKIM/DMARC.
- SendGrid killed its free plan (2025); Mailgun free is paywalled;
  Brevo's 300/day free tier also assumes a verified domain for real use.
- Inbound adds real complexity: Resend Inbound (webhook + second API call
  per message), SendGrid Inbound Parse (fragile multipart parsing, no
  retry on 4xx), Mailgun routes (paywalled).
- This path needs a new paid-ish account + a domain. Wrong fit for Phase 2 MVP;
  revisit if the game ever needs branding on its own domain.

### C. Self-hosted mail on the Oracle VM
- OCI blocks outbound port 25, so the VM cannot send mail directly.
- Even for inbound only, IP reputation and deliverability would be our
  permanent problem. Rejected.

## Decision
**Option A: a dedicated free Google account for the game, send + receive via
the Gmail API, polled from the Oracle VM.** Zero cost, zero new paid accounts,
zero deliverability work, and the integration pattern already exists.

## What this needs from Neil (open question, not a blocker)
Create a free Google account for the game and connect it (or authorize an
existing spare). Nothing else — no domain purchase, no DNS, no paid tier.
Murph can prototype the turn loop against a throwaway address in the meantime.

## Architecture notes for Phase 2
- One mailbox per game identity; player identity = GUID + sender address, so a
  dedicated `From:` keeps game mail out of personal inboxes entirely.
- Poll query: `to:<game address> newer_than:1h`-style windows with dedup by
  message id; reply by `messages.send` with threading headers preserved.
- Alias shortcut (weak): sending as `eweplay9+fogline@gmail.com` works today
  but the identity is ugly and it still shares Neil's mailbox. Not preferred.

## Update 2026-09-27 — Neil's directive (supersedes the Option A ask)

New Google accounts are too hard to get now (Google's restrictions), so
**no new account**: the game uses Neil's existing email capability for MVP
testing — Murph's Gmail integration (the `hatch_gws_cli gmail` connection)
sends/receives as the game while the adapter layer (`GmailClient`
abstraction) keeps the mailbox swappable. Longer-term candidate: a
dedicated **Inkbox with email capability** whose whole job is running the
game. Any candidate service must be verified to support **inline images**
and **HTML rich-text bodies** — both are now hard requirements (Neil:
"that's going to be important"). Gmail is verified for both as of
session #18 (cid-referenced inline parts, multipart/alternative).
