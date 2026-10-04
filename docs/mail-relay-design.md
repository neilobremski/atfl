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
   TRANSPORT HALF DONE 2026-10-02 (#78): `server/murph_relay.py`
   (22/22 selftest green) — normalize_inbound (the pinned #74 contract),
   build_outbound_envelope + base64-ascii wire form, poll_inbound over
   the node's a8s inbox dir, consume_inbound -> trash/, send_outbound
   via `a8s tell` with TELL_OUTBOX_DIR + retry-once-then-loud.
   DONE 2026-10-02 (#79): mailer.py relay edition — GmailClient/build_raw/
   extract_text_body deleted, poll_inbox polls the engine's A8S inbox via
   the relay contract, send_outcome hands atfl_outbound envelopes to
   send_outbound (turn_email with composite, clarify with
   turn_no="clarify-<inkbox id>" + fresh_thread advisory, nudge with
   turn_no="nudge-<UTC date>"), maybe_nudge hands off too, thread_message_id/
   thread_refs bookkeeping removed (last_email_at keeps the nudge gate),
   send_ms now measures the handoff, FakeGmail reshaped as the Murph-side
   test double (murph_relay.py's 22/22 selftest still green against it),
   new mailer selftest 20/20 green. gmail_adapter.py deleted.
2. Rewrite `server/config.py` per the decisions above; update the systemd
   EnvironmentFile template in `deploy/`.
   DONE 2026-10-02 (#79): ATFL_GAME_ADDRESS + ATFL_TOKEN_PATH deleted;
   ATFL_MURPH_NODE added (default "murph", blank refuses); ATFL_A8S_NODE_ROOT
   now required unconditionally (the send path needs it); deploy/atfl.service
   comment, deploy/README.md env table + atfl.env template, and
   deploy/bootstrap-free-micro-1.sh updated to the relay env.
3. Rewrite DESIGN.md §1 + §5.3 to describe the relay.
   DONE 2026-10-02 (#79): §1.1 identity = no engine email identity (players
   email murph@inkboxmail.com), A8S envelope transport, Murph-owned
   threading; §1.2 poll cadence = A8S inbox; §5.3 all three system email
   types as handoff envelopes (nudge/clarify/death), clarify fresh_thread
   advisory noted, threading bullet retired.
4. Murph-side: implement the inbox filter + forwarding + thread-state file
   (my operational half; lives outside this repo).
   DONE 2026-10-02 (#75/#76): inbound `hidden_files/atfl_relay_forwarder.py`
   (14/14 selftest green, dry-run default, own seen-set), outbound
   `hidden_files/atfl_relay_sender.py` (13/13 selftest green, thread pinned
   per game_guid in `hidden_files/murph_relay_threads.json`, Idempotency-Key
   per guid:turn + sent-ledger replay guard). Wire note: envelope attachment
   "bytes" travels base64-ascii over A8S (sender accepts both forms).
   DONE 2026-10-02 (#81): outbound consumer
   `hidden_files/atfl_outbound_consumer.py` (24/24 selftest green) — scans
   ~/filedrops/murph/.inbox/*.json (+ the agents/murph/inbox detached path)
   for tells whose content is a wire-form atfl_outbound envelope (positive
   kind check only; all other mail untouched), runs each through the sender
   (dry-run default, --send to post), moves handled blobs to
   .trash-atfl-outbound/, malformed envelopes to .quarantine-atfl-outbound/,
   leaves the blob in place and aborts loudly on API failure. Verified
   against a real engine build_outbound_envelope -> envelope_for_wire blob
   (jpeg bytes intact, blob trashed) and a live dry-run over 189 real inbox
   tells (all ignored, nothing moved). Next: give the consumer a runner
   (cron or message-poller hook) before the first real playtest turn.
   DONE 2026-10-02 (#83): runner `hidden_files/atfl_outbound_runner.sh`
   (flock-guarded, mode from `atfl_outbound_runner.mode` = dryrun|send,
   per-run line in `atfl_outbound_consumer.log`; no cron daemon exists on
   the sandbox VM, so the runner is designed to be called from the
   message-poller's step loop — wiring that hook is a main-agent ask).
   BUG FOUND BY THE DRILL: the sender POSTed to `/api/v1/messages/send`
   (HTTP 404) — the consumer's 24/24 selftest had enshrined that wrong
   endpoint. Fixed to the proven mailbox endpoint
   `POST /api/v1/mail/mailboxes/murph@inkboxmail.com/messages` (same one
   the working digest sender uses; payload shape identical). Verified by
   two drills: (1) drain drill, real engine builders -> wire -> tell blob
   -> consumer --send with fake api (14/14: byte-intact attachment,
   Idempotency-Key, ledger, thread pin, trash, replay-skip); (2) real-send
   drill, real sender.send to murph@inkboxmail.com (HTTP 200, new thread
   d1462ab2-..., confirmed newest outbound message in the mailbox). Mode
   flipped to `send` after the drills passed.
5. VM-side deploy wiring (free-micro-1, atfl user) — DONE 2026-10-03 (#86):
   S3 remote configured in /srv/atfl/.config/a8s/network.json (same
   ar3-temporary bucket/prefix/profile as the dev VM; AWS credential copied
   from the dev VM's a8s-s3-storage profile to /srv/atfl/.aws/credentials,
   0600 — per Neil's standing full-VM-control grant); `python3-pip`
   installed from baseos+appstream only (limited repos, 1 GiB box) so the
   a8s-s3 dep group (boto3) could install; `a8s health`: remote s3 OK,
   atfl-server OK. Node daemon as new unit a8s-atfl-server.service
   (User=atfl, HOME=/srv/atfl; ExecStart wrapped in /usr/bin/bash because
   SELinux Enforcing labels /srv/atfl/.ar3 var_t, which the service domain
   cannot exec directly — 203/EXEC "Permission denied").
   INBOUND BRIDGE (the gap this closed): the daemon delivers to an attached
   node by waking its definition's invoke command, never by leaving files in
   the agents inbox dir that poll_inbound() scans — so the inbound leg had a
   dead last hop. New deploy/atfl-server-definition.json
   (invoke: $PYTHON inbox_append.py $MESSAGE, argv-substituted so JSON
   bodies arrive intact) + deploy/atfl-server-inbox_append.py: writes
   Murph's raw atfl_inbound envelopes verbatim to
   <ATFL_A8S_AGENTS_DIR>/atfl-server/inbox/ (new env var in atfl.env =
   /srv/atfl/a8s/engine-inbox); non-envelope bodies go to dropped/ for
   inspection; deterministic filenames from inkbox_message_id make
   duplicate wakes idempotent. Verified live both directions: VM->dev
   (envelope in ~/filedrops/murph/.inbox/) and dev->VM (bridge file landed,
   engine's poll_inbound + normalize_inbound read it green via the deployed
   venv). atfl.service's poll loop now reads the (empty) engine inbox
   instead of logging the loud "cannot read a8s inbox" error.

## For Neil's eye (batched to the digest)

- Player onboarding copy: drafted 2026-10-01 in
  `docs/relay-onboarding-copy-DRAFT.md` — how a player learns the address
  (murph@inkboxmail.com) and what their first email should say. Wording
  and tone are subjective — needs his read before anything player-facing
  ships. The draft lists the five specific reads wanted.

## End-to-end drill (2026-10-03, session #87)

First complete turn over the real transport, synthetic player
"write start" (from murph@inkboxmail.com, drill only): `a8s tell
atfl-server` → S3 bucket → VM daemon wake → inbox_append →
engine-inbox (deterministic filename) → atfl.service poll cycle →
dispatch signup (game 2680626c) → MockGM turn 1 (images off) →
build_outbound_envelope → `a8s tell murph` → S3 → this VM's murph
daemon → ~/filedrops/murph/.inbox/ blob → atfl_outbound_runner.sh
(mode=send) → consumer → Inkbox POST → turn email delivered
("[ATFL 2680626c] Above the Fog Line", turn 1, seeded trailhead
narrative, `[[TURN_COMPOSITE]]` placeholder as designed with images
off). Every hop verified by its own record (daemon wake → bridge
file → poll log "1 inbound processed, 1 handed off" → blob →
consumer log "sent=1" → mailbox). Drill game DB removed from the VM
afterward so nudge sweeps never touch it.

LOOPBACK NOTE: a send to the game's own address (murph@inkboxmail.com)
produces TWO mailbox records — the outbound copy (direction=outbound,
status=delivered) and, ~1s later, the SES-delivered inbound copy
(direction=inbound, status=received, new thread). This is NOT an
Idempotency-Key failure; the client POSTed exactly once (one audit +
ledger entry). The forwarder's anti-loop (from == own address) already
excludes the inbound copy from forwarding, so drill/loopback mail can
never self-trigger a game. Real players will never see this (their
address != the game's).

## Turn-2 continuity drill (2026-10-03, session #90)

Proves the continuation path, not just signup: after the turn-1 drill
above (new game fde5378c, sender murph@inkboxmail.com), a second
`a8s tell atfl-server` envelope carried the player's action text plus
the `Game code: <GUID>` footer. The poll cycle matched it by
(GUID, sender) — `extract_guid` prefers the footer, UUID fallback —
ran turn 2 serially under the per-game lock, advanced the game clock
07:00 → 08:00 (visible in the delivered body), and handed off via the
same `a8s tell murph` → runner → Inkbox path. DB evidence: `turns`
holds turn 1 ("write start") and turn 2 (the action text); `turn_stats`
shows `email_sent_at` set for BOTH turns, so the #89 verify_turn
outbound-email criterion is green on continuations, not just signups.
Drill game DB removed from the VM afterward (games dir back to
mailer.db); drill loopback copies marked read so the input channel
stays clean. MockGM's placeholder narrative is unchanged — the drill
tests the machinery (match → adjudicate → advance → handoff), not
prose quality.

## Standalone-nudge drill (2026-10-03, session #92)

Proves the idle path over real transport — the one turn-loop leg never
drilled (signup #87, continuation #90, verify 7/7 #89): after a fresh
signup drill (new game 7d883792, sender murph@inkboxmail.com, turn 1
handed off and delivered as usual), `last_email_at` in the drill game
DB was backdated 25h via `sudo -u atfl python3` on free-micro-1. The
next poll cycle (22:17:48 UTC) logged "1 nudged": `maybe_nudge` fired,
the envelope went `a8s tell murph` → filedrops `.inbox` → runner
(mode=send, sent=1) → Inkbox. Envelope checks: kind=atfl_outbound,
turn_no=`nudge-2026-10-03` (stable per-day replay key), subject
`[ATFL 7d883792] Above the Fog Line`, to murph@inkboxmail.com, body the
placeholder `render_nudge` prose. Nudge email verified landed in the
mailbox (22:21:08Z). DB evidence: `last_email_at` advanced to the
handoff time; `turns` holds ONLY turn 1 — the nudge mutated nothing
(no phantom turn row), per §2.3. The following poll cycle (22:22:48)
logged "0 nudged" — the 24h gate holds, no double-nudge. Drill game DB
removed from the VM afterward (games dir back to mailer.db); drill
loopback copies marked read so the input channel stays clean. Note:
nudge prose itself remains a review checkpoint (Neil's eye) — the drill
tests the machinery (gate → envelope → handoff → delivery), not copy.

## Death-path drill (2026-10-03, session #93)

The last undrilled turn-loop leg over real transport (signup #87,
continuation #90, nudge #92, verify 7/7 #89): the full death path.

Fresh signup drill (sender murph@inkboxmail.com, own mailbox,
`a8s tell atfl-server` with TELL_OUTBOX_DIR=~/filedrops/murph/.outbox)
→ poll cycle 00:12:49 UTC "1 inbound processed, 1 handed off" → game
12cf2e7d → MockGM turn 1 → runner sent=1 → turn-1 email delivered.
Then player hp was forced to 0 in the drill game DB
(`sudo -u atfl python3` on free-micro-1 — MockGM never writes
`physical_state.hp`, so the forced value survives adjudication, and
this is the honest instrument for §2.5.7 without a killing GM).

Turn-2 tell (action text + `Game code: <GUID>` footer) → cycle
00:17:49 UTC "1 inbound processed, 1 handed off, action=turn_email
turn=2 handoff=True". DB evidence: `games.status='dead'`,
`ended_at` set, `turns` holds exactly turns 1+2 (no phantom rows),
`turn_stats.email_sent_at` set for BOTH turns (criterion 4 green on the
death turn), player hp still 0. The ending email went runner sent=1 →
delivered; the raw body contains the §5.3 death closer verbatim
("This was your last email. The game is over.").

"Dead games are over forever" checks, same drill game:
- Post-death player input (another action + Game code footer) →
  cycle 00:22:49 UTC `action=clarify guid=None turn=None handoff=True`
  "No active game for that Game code and this address
  (murph@inkboxmail.com)." — the clarify email delivered, and the
  `turns` table still holds only turns 1-2 (no phantom turn).
- `last_email_at` backdated 25h on the DEAD game → cycle 00:27:49 UTC
  "0 inbound processed, 0 handed off, 0 nudged" — the §2.4.5 sweep
  excludes dead games even when their last_email_at is stale.

Drill game DB removed from the VM afterward (games dir back to
mailer.db); all drill loopback copies marked read, watermark advanced.
