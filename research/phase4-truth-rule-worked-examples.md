# Phase 4 — the DB-wins truth rule: worked conflict scenarios

*2026-09-28 — code-free design pass, companion to
`research/phase4-gm-integration.md` ("K7e Knowledge store vs. the game's
SQLite truth").*

**The rule, one line:** when the R4T roster's memory and the game's SQLite
disagree, the SQLite wins, always. The roster's memory (k7e craft facts +
month-long conversation continuation) is a storytelling aid; the ledger is
the world.

These three worked cases pin down what the rule means in practice — what
breaks, which existing defense catches it, and what the roster's standing
instructions must say. Case 2 surfaced a real gap in the MVP loop and was
fixed in the same session (see §4).

## Setup (all three cases)

Game `fog-line-mystery-v1`, turn 7, game-clock "day 1, ~13:00". Filtered view
handed to the roster (physical state only):

- `object:gate` at `place:trailhead`: `physical_state = {"locked": false, "latched": true}`
- `object:water-bottle`: `{"water_ml": 320}`
- `actor:mara` (a fellow hiker NPC): `{"location": "trailhead", "mood": "wary"}`
- `actor:player`: `{"hp": 1.0, "fatigue": 0.2}`
- Plot pick (turn 1, pinned): `earth-changing`

The roster runs with conversation continuation on, so it "remembers" its own
prior turns' prose and decisions in addition to the k7e craft store.

## Case 1 — the narrative hallucinates a change the adjudication denied

**Turn 7 input:** "I slam the trailhead gate shut and lock it."

**Adjudication (correct):** one question —
`{"q": "Can the player lock the gate?", "answer": "no",
"rationale": "no key in inventory; latch only", "effect": null}`.
No mutation. DB unchanged: `gate.locked` stays `false`.

**The slip:** the roster's `compose_narrative` reply nonetheless writes,
"The gate clicks shut behind you. Locked." A model slip — prose running
ahead of adjudication. The narrative passes `secrecy_check` (no plot leak)
and is emailed. The player now believes the gate is locked.

**What the rule does, turn 8:** the turn-8 envelope's `filtered` view is
built by `turn_loop.filtered_view` from the DB, not from the roster's
prose. It still shows `gate.locked == false`. The roster (reading its own
turn-7 prose via continuation) faces a direct conflict: its memory says
locked, the handed-in world says open.

**Resolution:** the world wins. The runbook line for `~/ar3/fogline-gm/r4t.md`
must be explicit (draft, §5):

> The `filtered` world in each envelope is the truth. If your memory of a
> prior turn contradicts it, the world wins — narrate the world, not your
> memory. If you need a character to have done something they haven't,
> propose it as an adjudication question *this* turn; never narrate it as
> already done.

Turn 8's narrative must describe the gate as it is — a graceful repair is
fine in-fiction ("in the morning light you see the latch never caught —
the gate swings free"), but the state the player acts on is the DB's.

**Which defense caught it:** none needed to — the slip is confined to prose
and self-corrects at the next envelope. The DB was never touched because the
adjudication answered "no" and `apply_effect` only runs on `answer ==
"yes"` (with the partial-effect path for `no` also going through the same
commit). Prose cannot mutate state; only the commit can.

## Case 2 — adjudication targets an entity that doesn't exist

**Turn 7 input:** "Mara hands me her lantern."

**The slip:** the roster's conversation memory holds that Mara carries a
lantern (it wrote that three turns ago, offhand). But `object:lantern` was
never created — it's not in the filtered view. Adjudication replies:

```json
[{"q": "Does Mara give the player her lantern?", "answer": "yes",
  "rationale": "she's grateful for the water you shared",
  "effect": {"object:mara-lantern": {"physical_state.owner": "player"}}}]
```

The `_validate_questions` shape check passes (`object:mara-lantern` is a
legal `etype:slug` target with `physical_state.*` keys) — the adapter
validates *shape*, not *existence*.

**What the rule does:** `apply_effect` → `_row(db, "objects",
"mara-lantern")` → no such row. Before this session's fix, `fetchone()`
returned `None` and `dict(None)` raised a raw `TypeError` — *not* a
`TurnFailed`, so the §2.6 retry-once path in `dispatch._turn_outcome` (which
catches only `TurnFailed`) never engaged. The turn died with an opaque
crash, no clean "failed, nothing sent" outcome, no audit row. The roster's
hallucination bypassed every model-independent defense precisely because it
wasn't shaped like a model failure.

**Fixed (2026-09-28, this session):** `_row` raises `TurnFailed` on a
missing slug (`server/turn_loop.py`). Now the walkthrough completes as
designed:

1. Adjudication answer "yes" with effect on a nonexistent slug → `apply_effect`
   raises `TurnFailed("no objects row for slug 'mara-lantern'")`.
2. `dispatch` §2.6: retry once with the identical envelope (same input —
   the roster gets one chance to produce a sane answer, not coaching).
3. Second failure → `DispatchOutcome("failed", …, "turn failed twice,
   nothing sent")`; the turn's transaction rolls back — no partial state,
   no email, the game waits for the next player input.
4. The DB still shows no lantern; `mara` still at the trailhead. The
   hallucination committed exactly nothing.

**Pinned in `prototype/roster_demo.py` (hermetic, scripted `tell_fn`):**
hallucinated-slug adjudication → `RosterTurnFailed` (a `TurnFailed`,
retry-eligible); dispatch-level check: outcome `"failed"`, nothing rendered,
DB unchanged (no new `turns` row committed, no ledger rows).

**Residual risk, noted not fixed:** `compose_narrative` can still *describe*
the phantom lantern in prose ("Mara's lantern swings as she walks"). That's
Case 1 territory — a prose-only slip, self-corrected by the next envelope's
filtered view. Prose cannot create entities; only adjudication + commit can,
and both are now guarded.

## Case 3 — a world fact sneaks into the k7e craft store

**Setup:** k7e (`- **Knowledge:** on`, per-roster store) holds *craft*
facts: the plot pick, pacing notes ("player likes slow mornings"),
Neil's style corrections ("less purple prose"), per-player tendencies.
It must never hold *world* facts.

**The slip:** after turn 5 the roster's post-turn capture writes to k7e:
"the old well behind the trailhead is poisoned" — a beat it invented in
narrative, stored as durable memory. By turn 9 the roster adjudicates a
"drink from the well" question as if poisoning were established, citing
its own k7e note in the rationale.

**What the rule does:** the DB wins twice over.

1. The k7e note is not world state. `filtered_view` never reads k7e; the
   adjudication envelope's `filtered` has no well-poisoning field because
   no committed mutation ever set one. A rationale citing k7e is not
   evidence the commit machinery can see.
2. The legitimate path for a world fact is a *mutation through
   adjudication*: the roster proposes `{"q": "Is the well poisoned?",
   "answer": "yes", "effect": {"object:well":
   {"physical_state.poisoned": true}}, "rationale": "..."}`, the loop
   commits it, the ledger records it, and *then* it's true. Craft memory
   proposes; the commit disposes.

**The Neil-reply corollary** (why this matters operationally): Neil's
digest replies sometimes correct world facts ("actually the lantern should
be broken from the start"). Those corrections arrive as *email*, not as
roster instructions. They must be converted into DB mutations by the
operator (Murph) — a seed/DB change logged in the progress log — never
applied by the roster itself. The roster has no write path to the world,
by construction.

**Standing instruction (draft for `~/ar3/fogline-gm/r4t.md`, §5):**

> Your k7e notes hold craft: style, pacing, the plot pick, player
> tendencies. Never store world facts there, and never treat a k7e note as
> established truth — if it matters, it must appear in the `filtered`
> world handed to you. If you believe the world is missing a fact, ask for
> it as an adjudication question this turn.

## §4 What these cases teach the adapter design

1. **Shape validation is not existence validation.** The roster can only
   address the world through `etype:slug` targets, but "well-formed target"
   and "target exists" are different checks. The commit-side check
   (`_row` → `TurnFailed` on missing slug) is the one that matters, and it
   must speak the failure contract the dispatch loop already honors
   (`TurnFailed`, retry-eligible), not a raw `TypeError`.
2. **Prose is memory; JSON is the only write path.** Cases 1 and 3 both
   reduce to: anything the roster says in prose or stores in k7e is
   advisory until an adjudication question answers "yes" and the commit
   runs. The ledger (`mutations`, all with causes) is the audit trail of
   what actually became true.
3. **The envelope is the whole world.** Case 1's self-correction works
   because every envelope is self-contained: the roster must never *need*
   its conversation history to adjudicate. The `filtered` view is complete
   physical state; continuation is a storytelling convenience, demoted
   below the envelope on any conflict.

## Open items carried

- The `~/ar3/fogline-gm/r4t.md` runbook lines above are drafts — they land
  when the roster is created (after Neil's one-time OpenCode sign-in).
  Nothing here is a creative fork and nothing is irreversible.
- Neil's plot pick and turn-1 read remain his standing checkpoints; this
  doc defaults to `earth-changing` like the demos.
