# Mail relay design — Murph as the exchange layer

**Decision record (Neil, 2026-09-30 ~15:53 PDT):** the game engine sends NO
email directly. Murph is the exchange layer between the engine and players.
Game mail identity is **murph@inkboxmail.com** (Inkbox mailbox, send-capability
API-verified 2026-09-30). This closes open question #1 (game email identity).

This doc sketches the relay path both directions and what happens to
`ATFL_GAME_ADDRESS`. It is the decision record; the code rework
(`server/mailer.py`, `server/gmail_adapter.py`, `server/config.py`,
`deploy/atfl.service`) is queued as follow-up work, and DESIGN.md §1
(email protocol) / §5.3 (renderer/threading) will be rewritten to match.

## Current architecture (pre-relay, for contrast)

- The engine owns a Gmail identity (`ATFL_GAME_ADDRESS`; stub
  `abovethefogline@example.invalid`, open question #1).
- Inbound: `mailer.poll_inbox(gmail, game_address)` — Gmail query
  `to:{game_address}`, normalized inbound dicts, dedupe by Gmail message id
  in a persistent seen-set (`mailer.db`).
- Dispatch: signup on first email from an unknown sender; `extract_guid`
  from subject/body; `match_game(sender, guid)`; per the design, game files
  live under GUID + sender address.
- Outbound: `send_outcome` builds RFC822 with `From: <game_address>` and
  threads on RFC Message-IDs (`thread_message_id` / `thread_refs` in the
  games table). Anti-loop: inbound FROM the game address itself is ignored.
- Startup refuses to run without `ATFL_GAME_ADDRESS` + `ATFL_TOKEN_PATH`
  (Gmail OAuth) — fail loudly, never silently poll a stub.

## New architecture

Transport between engine and Murph is **A8S tell** (structured JSON
envelopes), both directions. The engine's only mail-adjacent surface is the
normalized inbound-dict contract that `GmailClient` already defines; the
relay adds an A8S-side implementation of the same contract.

### Outbound: engine → Murph → player

1. Engine renders the turn email exactly as today
   (`render_turn_email` → subject/body/html; composite JPEG attachments for
   turn emails per Phase 3, text-only for clarify/nudge).
2. Instead of `gmail.send`, the engine hands a structured payload to Murph:
   `a8s tell murph` with envelope
   `{kind: "atfl_outbound", game_guid, turn_no, to: <player_email>,
   subject, body_text, body_html, attachments: [...]}`. Composite JPEGs ride
   as tell attachments (A8S supports attachments on tells) and are
   referenced by Content-ID, preserving the inline-image behavior.
3. Murph sends from **murph@inkboxmail.com** via the Inkbox send API.
   Murph owns the player-facing thread: one Inkbox thread per game,
   In-Reply-To/References managed on Murph's side.
4. `failed` / `ignored` outcomes still send NOTHING (§2.6). The standalone
   nudge also goes through Murph — there is no engine-side send path at all.

### Inbound: player → Murph → engine

1. The only address players ever see is **murph@inkboxmail.com**. To start a
   game you email that address (the old "email an address to start" rule,
   §6.2, with the address changed).
2. Murph polls the Inkbox inbox and forwards game mail to the engine:
   `a8s tell atfl-server` with envelope
   `{kind: "atfl_inbound", from: <player_email>, subject, body_text,
   inkbox_message_id}`. `inkbox_message_id` is the dedupe key (replaces the
   Gmail message id in the seen-set).
3. **Murph-side filter** (decision 2026-09-30): forward only mail whose
   subject contains `[ATFL` (the prefix every engine-rendered subject
   carries, e.g. `[ATFL {guid8}] Above the Fog Line`) or whose subject/body
   mentions "above the fog line" (first-contact/signup path). Everything
   else in Murph's inbox is not game mail and is never forwarded.
4. Engine-side routing is UNCHANGED: `extract_guid(subject/body)` +
   `match_game(sender, guid)`; unknown sender → signup. Murph preserves
   subjects verbatim, so the `[ATFL {guid8}]` prefix survives the relay and
   GUID routing works without modification.
5. Anti-loop moves to Murph's side: mail From murph@inkboxmail.com that is
   not an engine handoff is never forwarded to the engine.

### Murph-side state (operational, not engine code)

- A small routing/state file (e.g.
  `~/workspace/goals/above-the-fog-line-game-project/hidden_files/murph-relay-state.json`):
  game GUID → Inkbox thread id, so Murph keeps one thread per game and can
  thread replies correctly.
- Murph-side failure rule: if a handoff tell fails, retry once, then the
  turn is `failed` (loud, §2.6 semantics preserved — nothing half-sent).

## What ATFL_GAME_ADDRESS means now

**Retired.** The engine never constructs SMTP, never needs a From address,
never polls a mailbox. Decisions (2026-09-30):

- `ATFL_GAME_ADDRESS` is deleted from `server/config.py`. The player-facing
  address (murph@inkboxmail.com) is Murph's operational detail, not engine
  config — the engine has no use for it (render subjects don't reference it).
- `ATFL_TOKEN_PATH` is deleted with it (no Gmail OAuth anywhere in the
  engine).
- New: `ATFL_MURPH_NODE` (default `"murph"`) — the A8S node the engine hands
  outbound payloads to and polls for inbound forwards. Missing/unreachable
  at call time fails loudly, in the same spirit as the old startup refusal
  (RosterGM already sets this precedent: fail at call time, never silently).
- The old startup refusal ("refusing to start against the stub") is
  re-expressed: the poll loop refuses to start without a working Murph node
  target.
- Engine DB columns `thread_message_id` / `thread_refs` become vestigial
  (threading moved to Murph's side); they stay unread until the mailer
  rewrite removes them.

## What stays unchanged

- Dispatch, GUID routing, signup semantics, turn loop, GM backends
  (mock/roster), render layer, nudge policy (≤1/24h, never in-character,
  mutates nothing), the `[ATFL {guid8}]` subject convention, attachments
  logged-never-acted-on, per-turn stats bookkeeping (send_ms now measures
  the handoff to Murph, and the doc notes the semantic change).
- `dry_run.py` is unaffected (it never touches mailer).

## Follow-up work (queued, not this session)

1. Rewrite `server/mailer.py`: replace `GmailClient` sends with
   Murph-handoff; replace `poll_inbox` Gmail query with an A8S-inbox poll
   adapter implementing the same normalized-dict contract; `maybe_nudge`
   hands off too. Keep `FakeGmail` for tests (it now simulates the
   Murph side: `queue_inbound` ≈ a forwarded player mail).
2. Rewrite `server/config.py` per the decisions above; update the systemd
   EnvironmentFile template in `deploy/`.
3. Rewrite DESIGN.md §1 + §5.3 to describe the relay.
4. Murph-side: implement the inbox filter + forwarding + thread-state file
   (my operational half; lives outside this repo).

## For Neil's eye (batched to the digest)

- Player onboarding copy: drafted 2026-10-01 in
  `docs/relay-onboarding-copy-DRAFT.md` — how a player learns the address
  (murph@inkboxmail.com) and what their first email should say. Wording
  and tone are subjective — needs his read before anything player-facing
  ships. The draft lists the five specific reads wanted.
