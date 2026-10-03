# Phase 2 acceptance-checklist pass (2026-09-27)

One read of every §6.2 box against what the build actually proves today.
Legend:
- **PROVEN** — exercised end-to-end in a green demo (MockGM; the pipeline,
  not the prose, is what the demos prove)
- **CODE** — implemented, no demo coverage yet
- **BLOCKED** — waits on a Neil decision (open question #1 game address /
  #2 GM model / #3 plot pick defaulted / #4 token.json path)
- **LIVE** — provable only on a real email run, even with everything built

## §6.1 Playtest entry criteria — status

1. Live game address — **BLOCKED (#1)**. Poller + adapter + systemd unit
   are all ready and point at the stub; nothing touches a live mailbox.
2. R4T GM behind the pipeline — **BLOCKED (#2)**. GameMaster interface
   is frozen; MockGM proves plumbing only.
3. Plot pick — **BLOCKED (#3, defaulted to "earth changing")**. Turn 1
   records the pick as a game-level mutation, verified in demos.
4. Server up on free-micro-1 — **CODE**. Poll entry + systemd unit +
   deploy doc exist; deployment itself needs #1 (and Neil's OAuth for
   token.json, OQ#5). Unit verified 2026-09-27: `systemd-analyze verify`
   reports zero parser errors (only the expected not-executable line for
   the target-VM venv path); structural invariants (paths vs README,
   atfl:atfl, Restart=always, hardening) pinned in deploy_demo.py
   (25/25 green) so future unit edits are gated.

## §6.2 Acceptance checklist — box by box

**Start**
- Signup creates game (UUID4 GUID, seeded `<GUID>.db`, turn-1 email):
  **PROVEN** (dispatch_demo, mailer_demo) except "within minutes" on a
  live poll loop — **LIVE**.
- Turn-1 email content (four §3.3.3 facts, two trail directions, open
  question, secrecy check): pipeline **PROVEN** (secrecy enforced,
  render layout fixed); the prose itself is Neil's checkpoint (turn-1
  intro draft, "Waiting on Neil's eye") and the real GM's fact-writing
  is **LIVE (#2)**.

**Turn flow**
- One reply = exactly one turn, five steps, §2.5 done criteria:
  **PROVEN** — verify_turn asserts all 7 (outbound email =
  turn_stats.email_sent_at handoff record since #89; was a skip until
  open question #1 closed with the relay mailer).
- Reply content honored; denied claims get a `no` row with rationale +
  partial effect: **PROVEN** (MockGM drink-yes / fell-tree-no cases in
  world_state_demo). Natural-language intent parsing quality: **LIVE (#2)**.
- Serial turns; mid-turn reply queues into the next turn: **CODE** —
  per-game locks + batch folding proven; a true race under load is
  **LIVE**.
- Late reply folded with `late reply to turn N` audit mark: **PROVEN**
  (dispatch_demo).

**GUID threading and identity**
- `[ATFL <8hex>]` tag + `Game code:` footer + one Gmail thread per game:
  **PROVEN** (mailer_demo threading assertions on RFC Message-IDs).
- Ambiguous inbound → clarification, wrong game never mutated:
  **PROVEN** (mailer_demo).
- Attachments logged, never acted on: **PROVEN** (mailer_demo).

**State persistence**
- Survives restarts (same `<GUID>.db`), mutations ledger with causes,
  elapsed-time reconciliation: **PROVEN** (world_state_demo; the
  bottle-evaporates / drop-dries cases fire).
- Failed turns retry once, send nothing invented, logged + surfaced in
  digest: **PROVEN** at dispatch/mailer level (mailer_demo: failed
  outcome sends nothing); digest surfacing is the §2.6 contract and
  **LIVE**.

**Idle and death**
- ~24h idle turn (conservative default, catch-up lead, open prompt):
  **PROVEN** (dispatch_demo idle sweep; catch-up lead in
  world_state_demo turn 3).
- Death → status=dead, ended_at, nothing further ever: **PROVEN** as of
  today (world_state_demo death test: dead game refuses run_turn and
  gains no new turns).

**No fiction outside turns**
- Standalone nudges ≤1/24h, ≤120 words, mutate nothing, reveal nothing:
  **PROVEN** (mailer_demo). Known copy edge (logged 2026-09-27):
  the nudge said "Nothing has changed" but the firing window can
  include a prior idle turn that *did* change things
  (conservative-default mutations) — **RESOLVED 2026-09-27**: the nudge
  no longer asserts anything about world state; it points at the last
  turn email (§2.5 makes it the latest confirmed view). Structural rule
  recorded in render.render_nudge's docstring + 2 new mailer_demo
  checks (state-claim denylist, last-turn-email pointer). Nudge prose
  stays placeholder per §2.4, still Neil's eye at copy lock.
- No system email in-character: **CODE** (clarification + nudge
  templates are plain system voice; asserted ≤120 words for the nudge).

## §6.3 The Neil playtest — status

- **BLOCKED (#1, #2)**: hourly/daylight tempo, parsed intent, denied
  claim, idle turn with catch-up, full §5.2 layout in his inbox.
- Dogfooding stats rows: **PROVEN as of today** — `turn_stats` ships
  (mutations count, adjudicate_ms, narrative_ms, secrecy_pass, send_ms,
  email_sent_at). Pipeline stats recorded by run_turn, send latency
  filled by the mailer on send. Dashboards still later.

## §6.4 Exit criteria — status

- All §6.2 boxes on a real end-to-end run: pending (**LIVE**).
- Neil's playtest + reads: pending (**BLOCKED**).
- Archive = `<GUID>.db` + ledger + emails: by construction (the games
  dir is the backup unit).
- Nothing from Phase 3/4/5 sneaked in: holds.

## Score

- PROVEN: 12 boxes · CODE: 4 · BLOCKED: 6 (all six are Neil's calls) ·
  LIVE: everything remaining is one real playtest away.
- Phase 2 is as far along as it can go without the game address and
  the R4T GM. Next Phase 2 work that does NOT need Neil: none left that
  matters — the wrap is done. Phase 3 research (image pipeline) is the
  honest next use of session time.
