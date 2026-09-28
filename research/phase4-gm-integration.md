# Phase 4 — GM integration design: plugging the R4T roster behind the `GameMaster` interface

*2026-09-28 — code-free design pass. Companion to `research/r4t-opencode-gm-plan.md`
(the roster setup) and `server/gm.py` (the interface it must satisfy).*

## The one-line contract

The turn loop keeps **state authority**; the GM is **advisory**. `turn_loop.py`
doesn't change: the R4T `fogline-gm` roster is wrapped by a `RosterGM(GameMaster)`
adapter that turns the two interface methods — `adjudicate` and
`compose_narrative` (plus `pick_plot` once per game) — into subprocess calls.
Everything the loop already enforces (serial turns, yes/no-only mutations,
auditable ledger, §2.5.5 secrecy check before send, retry-once §2.6) stays in
Python, never in the GM's hands.

This division is deliberate: an LLM that owns the database is a liability an
LLM that *proposes* mutations is not. The ledger, the secrecy denylist, and
the commit are the last line of defense, and they don't depend on model
behavior.

## What the GM may and may not touch

**Sees:** the player's email text for this turn, the `filtered_view`
(physical state only — places, actors, objects, no `hidden_traits`), the
game's turn number and game-clock, the catch-up lead already computed by
`build_catchup`, and the §3 scenario seed rules. Nothing else.

**Never sees:** `hidden_traits` (all three tables), `plot_concept`, the
mutation-ledger rows, other games' DBs, the raw Gmail payload. The filter
is enforced by the adapter, not by asking nicely — the subprocess gets a
JSON envelope built from `filtered_view` and nothing else.

**Never does:** write the DB (no SQLite tools needed for the job), send
email (the mailer sends; the GM only writes prose), advance the clock,
decide game over. It *proposes* `{"q", "answer", "rationale", "effect"}`
structures; `apply_effect` commits them.

## Calling convention (two prompts per turn, or one)

Option A — two tells per turn (matches the interface exactly):
1. `adjudicate` tell → roster returns a JSON array of question dicts.
2. `compose_narrative` tell → roster returns the narrative string.

Option B — one tell with a two-part answer (adjudication JSON, then
narrative). Half the round trips; slightly harder to validate.

**Decision (2026-09-28): start with Option A.** It mirrors the mock exactly,
keeps the failure attribution clear (a malformed adjudication vs. a leaked
narrative are different bugs), and the cost is two free-model calls, not
one — on Zen free models the token cost is $0 either way. Revisit B only if
turn latency ever matters; at ~1 email/day it won't.

Turn-1 `pick_plot` is a separate third tell, run once per game at creation,
and its answer is pinned as a game-level mutation (§3.3.2) — the roster is
never re-asked.

## The envelope (adapter → roster → adapter)

The adapter spawns the roster synchronously from the poll cycle:

```text
r4t tell fogline-gm "<JSON envelope>"   (or: r4t engine opencode run --agent fogline-gm ...)
```

Envelope in (JSON on stdin / message body):
```json
{
  "call": "adjudicate" | "compose_narrative" | "pick_plot",
  "game_guid": "...",
  "turn_no": 7,
  "game_clock": "day 1, ~13:00",
  "player_input": "<the email text, verbatim>",
  "filtered": { "places": {...}, "actors": {...}, "objects": {...} },
  "plot_roster": ["aliens", "government-project", "you-are-dead", "a-spell", "earth-changing"],
  "catchup": "While you were quiet: …" | ""
}
```

For `adjudicate`, the runbook instructs the roster to reply with **only** the
JSON array (no prose wrapper — the adapter parses `json.loads`; anything
else is a malformed-output failure, retried once per §2.6, then the turn
fails and nothing is sent). The roster's standing instructions (in
`~/ar3/fogline-gm/r4t.md`) carry the hard rules: answer yes/no, never invent
entities outside the filtered view, effects only as `{"<etype>:<slug>":
{"physical_state.<key>": value}}`, no mechanics talk in rationales (§5.3).

For `compose_narrative`, the reply is prose; the adapter validates it with
the same `secrecy_check` the mock path uses — leaked denylist string means
TurnFailed, exactly as today. Length guidance lives in the runbook
(§5: renderer spec — text-first, fixed block order); the adapter also
enforces a hard cap (e.g. 2000 words) so a runaway model can't produce a
novella.

## Failure semantics (unchanged from §2.6)

- Roster subprocess timeout (decision: 300s per call — generous for a free
  model, bounded for the poll cycle) → `TurnFailed` → one retry with the
  same envelope → on second failure, the turn is marked failed and nothing
  is sent. The player sees silence, not a half-game.
- Non-JSON / schema-invalid adjudication → same retry path. The roster
  must not "helpfully" fix its output on retry; it gets the identical
  envelope.
- The mock stays the dev default (`server/gm.py MockGM`); the roster
  adapter is selected by config (`config.py` gets a `GM_BACKEND` =
  `"mock" | "roster"`), so the whole suite of `prototype/*_demo.py` checks
  keeps running against the mock even after the roster goes live.

## K7e Knowledge store vs. the game's SQLite truth (do not confuse)

Two memory systems, two jobs:

- **`Knowledge: on` (k7e store, per-roster):** the GM's durable *craft*
  facts — the plot concept it picked, pacing notes, style corrections Neil
  makes in email ("less purple prose"), per-player tendencies. This is
  where Neil's reply corrections land (post-turn capture, per the
  r4t-engine-memory design).
- **Game SQLite (`~/…/<guid>.db`):** the *world truth* — physical states,
  the ledger, hp. The roster never writes here and never needs to read
  here; it gets the filtered view handed to it.

If these two ever disagree (roster "remembers" a door that's closed, DB
says open), **the DB wins**, always — the roster's memory is a storytelling
aid, the ledger is the world. The secrecy check and the hard rules in
§4.1 exist partly to enforce this: a GM that hallucinates a fact can only
propose it as a question, and "no" kills it.

## Continuation across the month

The roster runs `- **Continue:** on` (per-directory conversation store),
so the GM keeps its conversation across the ~30 turns. Risk: context
bloat/stalls over a month of play. Mitigations, in order:

1. Keep each envelope self-contained — the GM must never *need* the old
   conversation to adjudicate; the filtered view is the whole world.
2. If continuation gets weird (repetition, stale facts), the guide's
   15m-idle-retire pattern (ch. 2 §11) — a fresh conversation that re-reads
   the k7e store, which holds the durable facts anyway.
3. Per-turn latency budget lives in the adapter timeout, not in roster
   internals.

## Budgets and quota

- r4t's budget bucket caps the roster per hour — the GM gets a small
  bucket because a game turn needs at most 2–3 calls/day.
- Free Zen models = $0/token; the $20 top-up question (if Zen demands it
  at key issue) is Neil's spending call and stays an open question.
- This design adds no new accounts, no subscriptions, no paid APIs.

## Ordering (what lands when)

**2026-09-28 (session #27) — item 3 DONE:** `RosterGM` adapter lives in
`server/gm.py` (behind the `GameMaster` interface, `context` kwarg for
envelope metadata), `prototype/roster_demo.py` pins the envelope schema
and the malformed-output path (42 checks, hermetic — scripted
`tell_fn`, no roster needed). `config.py` takes `ATFL_GM=mock|roster`
(mock default; 'real' retired). Waiting on item 2 (the roster itself)
before any live flip.

1. **Now (code-free):** this doc. Nothing to build until the roster exists.
2. **~~After Neil's one-time OpenCode sign-in~~ (retired 2026-09-28 — opencode
   needs no login on the free path; done in session #32):** `~/ar3/fogline-gm/r4t.md`
   created and registered: three members (keeper/leader, arbiter, critic), each
   with its own role and separate `Knowledge: small` k7e store, opencode rig
   `gm-worker` (8/hour, max 24), single-turn contract. First smoke tell answered
   by keeper in 10.3s. Machine quirk found and fixed: opencode must be on the
   r4t worker PATH (symlinked into ~/.local/bin/; first turn failed exit 127).
3. **Then:** `RosterGM` adapter in `server/gm.py` behind the `GameMaster`
   interface, config-gated, mock remains default; new `prototype/roster_demo.py`
   pins the envelope schema and the malformed-output retry path (hermetic —
   no roster needed, like `deploy_demo.py`'s guarded verify).
4. **Then (Phase 4):** flip one test game to the roster backend; the mock
   suite keeps guarding the loop.

## Open questions for Neil

Carried, unchanged: (2) OpenCode sign-in vs. Zen key (his credentials; $20
top-up caveat is his spending call); (3) Cursor/Muse/Devon keys later.
Nothing new added — this pass introduced no creative forks and no
irreversible choices.

---

## Live two-tell exercise — corrections (2026-09-28, session #33)

Ran the real thing against the registered `fogline-gm` roster on this VM:
`pick_plot` → `adjudicate` → `compose_narrative` tells sent as `murph`
via `a8s tell fogline-gm`, envelopes built by the real
`server.gm.build_envelope` from the seeded scenario world. Two results
were clean, one is still in flight, and the exercise corrected four
design assumptions in this doc:

**Verified.**
- `pick_plot`: keeper answered `earth-changing` (bare slug, nothing else),
  recorded the pick in its k7e craft notes with correct secrecy framing,
  and replied by `tell murph` — the reply landed in the sender's a8s
  mailbox as `from fogline-gm:keeper`.
- `adjudicate`: keeper → arbiter → critic chain ran exactly as chartered
  (keeper routed with bare `tell arbiter`; critic returned `CLEAR` twice).
  The reply to murph was a **bare JSON array, no fences** — 7 questions,
  yes/no-only, effects on `object:water-bottle` / `actor:player` with
  `physical_state.*` keys only. The server's `_validate_questions` passes
  on it verbatim, and the secrecy denylist finds no hits. Arbiter's
  rationales show the hard rules landed (it refused to write `holder`
  because `holder` sits outside `physical_state`).
- `compose_narrative`: sent and received by the node; the keeper-drafts →
  critic-reviews → keeper-replies chain had not yet run when the session
  ended (see wake-slot note below). Prose discipline unverified — next
  session reads the reply from murph's mailbox.

**Corrections (design changes).**
1. **The transport is async, not synchronous.** `r4t tell` is the owner's
   impersonation verb — it queues a message and returns; nothing comes
   back on stdout. `RosterGM._tell_real`'s "shell out and read stdout as
   the reply" assumption is wrong. Real contract: server sends
   `a8s tell fogline-gm '<envelope>'`, then **waits on its own a8s
   mailbox** for keeper's reply (attributed `from fogline-gm:keeper`,
   correlated by game_guid/turn_no/call carried in the reply thread).
   The adapter needs a rework: send + poll-mailbox-with-timeout instead
   of subprocess-stdout. Item 3's "spawns the roster synchronously"
   language above is superseded.
2. **The fogline-gm a8s node must be running.** Tells sent while the node
   was down sat in the S3 mailbox unprocessed; `a8s start fogline-gm`
   drained them. Deploy consequence: free-micro-1 must run the roster
   node (start on boot) alongside the game server.
3. **Sender identity = the game server's own a8s node.** Keeper replies
   to the inbound sender (`tell murph` in the exercise). Production must
   not reuse murph's personal mailbox: register a dedicated mailbox-only
   a8s node (e.g. `atfl-server`, never started — the server reads its
   inbox files directly) and send tells from inside its root so the
   sender stamps correctly.
4. **The compose_narrative envelope must carry the approved mutations.**
   External tells open fresh a8s threads, so keeper's narrative turn
   cannot see the adjudication answers from the earlier tell — but the
   narrative has to be built from them. Decision: add the validated
   question array to the envelope on `compose_narrative` calls (new key
   next to the fixed set; charter updated to name it). Not yet
   implemented — next session's code task, along with the adapter
   rework in (1).
5. **Wake latency is minutes-scale and the idle pass holds the single
   wake slot.** Observed: 4.5 min from receipt to wake once; the second
   time, a `r4t idle` dreaming/distillation pass (k7e distill of keeper's
   turn) occupied the wake slot for 15+ min with the compose tell queued
   behind it. The server's reply-wait timeout must be generous (tens of
   minutes, not the 300s subprocess timeout), or the poll cycle must
   send-and-return and pick the reply up on a later cycle.
