# Phase 0 — stack/decision memo (wrap, 2026-09-26)

The output the plan promised for Phase 0: the stack decisions the MVP will be
built on. Two sessions produced two building-block decisions (email identity,
world-state schema); this memo consolidates them into one stack and records
what Phase 0 deliberately leaves to later phases.

## Decisions

### 1. Email identity — dedicated free Google account, Gmail API (2026-09-25)
Full research in `phase0-email-identity.md`. The game's email lives in its own
free Google account (no domain purchase, no paid tier, no deliverability
work). Send via Gmail API `messages.send` (500 recipients/day free tier —
a dozen players at one turn a day is a rounding error). Receive by polling
`messages.list` from the Oracle VM every few minutes. Push via Pub/Sub is a
later optimization if polling ever stops being fine.

**Open:** Neil creates the account (or authorizes a spare). Not a blocker —
the turn loop can be prototyped against a throwaway address meanwhile.

### 2. World state — one SQLite file per game (2026-09-26)
Full sketch in `phase0-world-state-schema.md`, proven by a runnable prototype
(`../prototype/world_state_demo.py`, mock GM, two turns end to end, clean).
- One `<GUID>.db` per game; tables: games / places / actors / objects /
  turns / mutations / assets.
- Physical state and GM-hidden traits as separate JSON columns — hidden state
  is explicit in the schema, never mixed into rendered state.
- Every yes/no adjudication lands in the `mutations` audit table (what
  changed, when, why): the answer to "the 3.5-turbo game had bad memory."
- Elapsed-time reconciliation (open bottle dries, drop evaporates) is a
  first-class step inside "gather", before adjudication.
- Fixed-slot inventory (hands ×2 + backpack ×8) — what the composite image
  shows is what the DB holds.
- Games are ephemeral: the DB file is the archive.

### 3. The stack, one line
Host: Oracle Always Free VM `free-micro-1` (us-sanjose-1) — Neil decided
2026-09-25. Transport: Gmail API on a dedicated free account. State: SQLite,
one file per game. GM: R4T-roster agent with hard rules (Neil's domain;
per-turn flow prototyped with a mock GM). Images: deferred to Phase 3.
Video: deferred to Phase 5.

## What Phase 0 deliberately did not survey
- **Play-by-email precedents:** light pass done. The classic pattern we keep —
  a missed turn is run by the GM with a conservative default and the player
  gets a catch-up summary (old Wushu PBEM rule: non-posters roll a weak
  default and bleed Chi). This informs the expired-turn rules in DESIGN.md.
- **LLM GM patterns:** the design notes already fix the pattern (hard rules +
  hidden state, gather → yes/no mutations → narrative → advance time →
  update); the schema encodes it. Model selection is Neil's call via R4T —
  carried to the open-questions list, not decided here.
- **Image pipeline options** (three-part composite): moved to Phase 3, where
  it belongs. Phase 2 MVP is text-first by plan.

## Phase 0 status: COMPLETE
Outputs: `phase0-email-identity.md`, `phase0-world-state-schema.md`,
`prototype/world_state_demo.py`, this memo. The remaining research items
fold into their natural phases (image → 3, GM model → 2/4). Next: Phase 1
DESIGN.md — email protocol first (GUID handling, turn cadence, expired-turn
rules), then turn structure, GM rules, plot roster, renderer spec, MVP
"done" criteria.

## Open questions carried forward
1. (Standing, Neil) Create the game's free Google account / authorize a
   spare so the MVP turn loop can send as the game.
2. (Neil) Which model/agent serves the GM in the MVP turn loop — the R4T
   roster choice. The mock GM in the prototype only proves the plumbing.
3. (Later, Murph's call) Whether `hidden_traits` should be encrypted at rest —
   the GUID+sender-address model treats the file itself as the secret.
