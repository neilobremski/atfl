# Failure-isolation audit — poll loop & turn pipeline (2026-10-04)

Audited ahead of Neil's first real game ("write start"), which will exercise
the one path never run end to end: the §6.2 start chain against the REAL roster
GM (dispatch → run_turn → RosterGM → render → handoff). Question: what breaks
if the roster leg raises, and what state is left behind?

## Transaction shape (verified in code, not just docstrings)

- `turn_loop.run_turn` has exactly ONE commit point (`turn_loop.py:319`, at the
  very end). Turn row, mutations, narrative, turn_no/clock advance, death
  status — all atomic. Nothing persists before the commit.
- Crash-resume is built into that shape: a killed/failed process leaves the
  turn row uncommitted (or, for an in-DB crash resume, a row with NULL
  narrative that the next `run_turn` reuses, skipping completed steps).
  RosterGM re-attaches to in-flight calls via its pending file rather than
  re-sending (see `gm.RosterGM`).

## Exception taxonomy

| Exception | Where caught | Result |
|---|---|---|
| `TurnFailed` (semantic GM failure: no adjudication, secrecy denylist hit, dead game) | `_turn_outcome` (`dispatch.py`) — §2.6 single in-process retry; second failure → `db.rollback()` → `DispatchOutcome("failed")` | Nothing sent (§5.3: `send_outcome` no-ops on `"failed"`); batch CONTINUES; message marked seen/consumed. The double-fail is visible in the service journal (`action=failed`) and `turn_stats.success=0`. The nudge sweep is the safety net — but the timing is NOT 24h: a failed signup leaves `last_email_at` NULL (no handoff ever happened), and `maybe_nudge` runs at the end of the same poll cycle, so the player gets ONE nudge within minutes, not after a day of silence. The nudge advances `last_email_at`, so the 24h gate applies from there. (Verified in code + pinned by mailer selftest "nudge: failed-signup game (NULL last_email_at) nudged now, not after 24h", 2026-10-05.) |
| Anything else from `run_turn` (A8S transport failure, roster node down, timeout, bug) | NOT caught by `_turn_outcome`, `dispatch_batch`, or `run_poll_cycle` — propagates to `poll.py`, which logs "cycle failed; loop continues" and the loop survives | Nothing was sent, nothing was committed, nothing was marked seen (fresh ids only persist via `_store_seen` at the end of `run_poll_cycle`, which the raise skips). Next cycle re-polls and re-dispatches the whole batch — at-least-once, exactly as `run_poll_cycle`'s docstring designs. No duplicate emails (send_outcome never ran), no duplicate games (uncommitted signup rolls back; an empty `<GUID>.db` file may linger on disk — cosmetic, `iter_games` skips it). |
| Handoff failure (`a8s tell` raises) | `send_outcome` raises `A8STransportError`; caller leaves the inbox file unconsumed and does NOT store the seen-set | Next cycle retries; Murph's per-(guid, turn) replay guard dedupes if the first tell actually landed (§2.6). Loud, never half-sent. |

## Deliberate non-fix: per-game isolation in `dispatch_batch`

`dispatch_batch` has no per-message try/except: one game's non-TurnFailed
exception aborts the rest of the batch for that cycle. This was considered
and deliberately left alone. A per-message catch would have to choose between
(1) marking the failed message seen — no retry, player gets nothing, no loud
signal — or (2) reimplementing batch-level retry semantics around a per-game
hole. For the single-player MVP the current shape is correct: the whole batch
retries in 5 minutes, loudly, until the roster recovers. Multi-game starvation
is a real theoretical gap; it becomes a fix when a second player exists.

## Bottom line for the first real game

If keeper's leg raises on turn 1: no email goes out, no partial game state
commits, the poll loop logs it and retries in 5 minutes — the failure is loud
(journal) and self-healing when the roster recovers. If the roster produces
semantic garbage twice: the turn is dropped, recorded as failed in the DB, and
the player gets ONE nudge within minutes (the failed signup leaves
`last_email_at` NULL, so `maybe_nudge` fires at the end of the same cycle —
NOT after 24h, as an earlier draft of this doc claimed); the 24h gate applies
from that nudge onward. Both outcomes are survivable without human
intervention; neither corrupts the game DB.
