# Phase 4 — actor claims: the protocol between actors and the turn loop

*2026-10-07 — code-free design pass. Companions:
`research/phase4-gm-integration.md` (roster behind the GameMaster interface),
`research/phase4-truth-rule-worked-examples.md` (DB-wins conflicts),
`docs/mail-relay-design.md` (Murph as exchange layer). Grounds in
`server/schema.py` (actors/turns/mutations), DESIGN.md §1.3 (expired-turn
rules, reused verbatim), PROJECT_PLAN.md Phase 4.*

## The gap this closes

The MVP loop is one player → one GM adjudication → mutations. Phase 4 adds
three things that all need the same mechanism:

1. **Roster-driven actors** — R4T agents playing animals/bosses that act
   between turns and submit *claims* ("the crow takes the bottle").
2. **Multiplayer** — several email players in one game, each bound to an
   actor, sharing one serial turn pipeline.
3. **Idle-player AI driving** — already specced in DESIGN §1.3; this memo
   pins how it reuses the claim path instead of being a special case.

The one mechanism: **a claim is a proposed world effect by any actor that
is not the player whose input is driving this turn.** Claims are staged,
then arbitrated inside the existing adjudicate step as additional
mutation-question candidates. The GM honors or denies; the ledger records
both.

## 1. Actor taxonomy (schema delta)

Current `actors` rows: `player` (is_player=1, ai_driver=0), NPCs
(ai_driver=1). Phase 4 formalizes *who drives each actor*:

```sql
ALTER TABLE actors ADD COLUMN controller TEXT NOT NULL DEFAULT 'ai';
-- 'email'  : a human player, reached by email (player_input drives them)
-- 'roster' : an R4T agent on the fogline-gm roster (animals, bosses)
-- 'ai'     : GM-default-driven NPC (minor actors, background)
ALTER TABLE actors ADD COLUMN email_address TEXT;  -- set iff controller='email'
```

New table for staging + audit:

```sql
CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    game_guid TEXT NOT NULL,
    turn_no INTEGER NOT NULL,        -- the turn this claim was submitted for
    actor_slug TEXT NOT NULL,        -- who claims
    effect_json TEXT NOT NULL,       -- {"entity_type","entity_id","field","old","new"}
    rationale TEXT,                  -- the claimant's reason, for the GM
    submitted_at TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT 'pending',  -- pending|honored|denied
    verdict_turn_no INTEGER          -- turn in which arbitrated (may be later)
);
```

And for multiplayer routing (guid is now shared per game, not per player):

```sql
CREATE TABLE players (
    game_guid TEXT NOT NULL,
    email TEXT NOT NULL,
    actor_slug TEXT NOT NULL,        -- the actor this human drives
    last_seen_turn INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_guid, email)
);
```

**Decisions:**
- A game keeps **one serial turn pipeline and one game-clock**, never one
  per player. Multiplayer turns are not simultaneous-move: the gather step
  merges every player's input since the last turn, and one turn email goes
  to each player (see §4). Seriality is what makes the ledger auditable.
- `ai_driver` stays as the MVP-era flag; `controller` is the Phase 4
  refinement. Migration: `controller = CASE WHEN is_player THEN 'email'
  WHEN ai_driver THEN 'ai' ELSE 'ai' END` — no behavior change on load.
- Denied claims stay in the ledger (`verdict='denied'`). The roster's
  memory will remember scheming; the DB is the audit of what happened.
  (Truth-rule memo applies: roster memory vs. claims table → table wins.)

## 2. Claim protocol

### 2.1 Shape

A claim mirrors the mutation-question dict the GM already emits
(§2.4 contract), plus provenance:

```json
{
  "actor": "crow",
  "effect": {"entity_type": "object", "entity_id": 1,
             "field": "holder", "old": "place:trailhead", "new": "actor:crow:beak"},
  "rationale": "shiny; the crow has been circling since turn 4",
  "for_turn": 12
}
```

### 2.2 Submission channels

| controller | channel | cadence |
|---|---|---|
| `roster` | A8S tell to the atfl node, `call: "claim"`, one leg per claim | between turns; may also arrive mid-turn (staged for next) |
| `email` | — | **players do not submit claims.** Their email is `player_input` for their own actor, adjudicated as today. A player can *attempt* anything in prose; the GM turns attempts into mutation questions, exactly as the MVP does. |
| `ai` | — | the GM generates conservative defaults inside adjudicate (no staging; these are candidates, not claims) |

Claims are idempotent by content: a duplicate leg (roster re-issue loops —
track open legs by `game_guid` + `turn_no` + `call`, never by thread id)
with identical `(actor_slug, effect_json, for_turn)` collapses to one row.

Claims submitted *after* a turn's gather step are staged for the next turn
(`verdict_turn_no` records where they landed). Nothing is lost; nothing
jumps the queue.

### 2.3 Arbitration (inside adjudicate, not beside it)

The turn's `adjudicate` call receives, in addition to today's inputs:

- pending claims for this turn (actor, effect, rationale),
- the filtered view (physical state only — claims never carry hidden
  state, and the claimant never saw any).

Each claim becomes a mutation-question candidate with
`cause: "claim:actor:<slug>"`. The GM honors or denies each in the same
pass as the player's questions, so **cross-claim conflicts resolve in one
place** (crow steals the bottle the player is reaching for → one
adjudication, one winner, one ledger trail).

Denial rules, structural:
- **Senses rule:** a claim is denied if the actor's senses cannot produce
  it (the camera discriminator: "can his senses produce this claim right
  now"). The crow cannot claim the bottle's *owner* — `hidden.owner` is
  not in its filtered view.
- **Physics rule:** denied if the effect contradicts `physical_state`
  (DB wins over any claimant's memory).
- **Plot rule:** denied if the effect would decide a `hidden_traits`
  fact — those belong to the plot, adjudicated only through player-facing
  questions, never through NPC claims.

Denied claims are **not narrated as attempts** unless the acting player's
actor could have observed them. An NPC's failed scheming must not leak
into prose — the secrecy denylist already guards words, but the
*knowledge* must not appear either. (This is a narrative instruction for
the roster's standing prompt, not a code gate.)

### 2.4 Idle players reuse the path (DESIGN §1.3, unchanged philosophy)

A player who misses the nudge cadence does not become a claim submitter —
their actor is driven by GM default, exactly as §1.3.4 says, and that
default enters adjudication as an `ai`-style candidate with
`cause: "late/default for actor:<slug>"`. No new mechanism; the claim
table is not involved. Controller stays `email` (the human can return any
time; `last_acted_turn` + the nudge timer decide when defaulting kicks
in). A late reply still folds into the next turn's gather step (§1.3.2).

## 3. Join protocol (players inhabiting existing actors)

The game GUID is printed in every turn email's footer. Sharing it is
sharing the game. A new sender emails:

```
join <game-guid>
```

1. Dispatch sees an unknown sender + `join <guid>` → looks up the game by
   guid (guid is now the game key; the old GUID+sender join becomes
   guid+sender via the `players` table).
2. A `players` row is created binding the sender to an actor. Actor
   assignment, in order of preference: (a) an existing `ai`-controller
   actor the GM flags as inhabitable (the fellow-hiker pattern — this is
   the plan's "players inhabiting existing actors"); (b) a fresh actor
   spawned at the newcomer's described entry. The GM proposes the binding
   in the next adjudication; the binding itself is a mutation
   (`entity_type='actor'`, field `controller`, old `'ai'`, new
   `'email'`), so it's audited.
3. The new player gets a catch-up email (missed frames summarized,
   §1.3.3) and joins the next turn's gather.

Anti-grief, structural: one email address binds to one actor per game;
`join` with a guid that doesn't exist is a normal signup (new game).
Joining mid-game never rewinds the clock — the newcomer arrives at the
current turn, with the catch-up doing the narrative work.

## 4. Turn email fan-out

One turn email **per player per turn**. Content:

- Shared core: the narrative, the composite image, map — identical for
  all players (the world is one).
- Per-player head: a one-line catch-up of frames since *their*
  `last_seen_turn` (§1.3.3), then any beats that concern their actor
  specifically.
- Footer: the shared game GUID + the per-player "reply to act" framing.
  (The GUID footer is what makes `join` forwardable.)

Cost note: N players = N Inkbox sends per turn. At the playtest scale
(2–4 players) this is fine; the relay already hands one envelope per
player to Murph, who sends. No protocol change, just a loop.

## 5. Worked example (turn 12, game fog-line-mystery-v1)

State: player Neil (`actor:neil`, controller `email`) at trailhead;
`actor:crow` (controller `roster`); `actor:mara` (controller `ai`,
fellow hiker); second human joins mid-turn.

1. Between turns: the roster submits a claim —
   `crow` takes `water-bottle` (holder → `actor:crow:beak`),
   rationale "drawn to the loose cap". Staged: claims row, pending.
2. Second human emails `join <guid>` → dispatch creates
   `players(guid, friend@x, actor:mara)`, controller `ai`→`email`
   staged as a mutation candidate for turn 12.
3. Neil emails "I grab the bottle and head up-trail."
4. Turn 12 gather: Neil's input + crow's claim + mara's controller flip
   + GM defaults for any silent `ai` actors.
5. Adjudicate (one call): the crow's claim and Neil's grab conflict on
   `object:water-bottle.holder` — one winner. Say the crow is faster
   (senses rule passes; physics passes): claim honored, Neil's grab
   becomes "the bottle is gone — a crow lifts off with it" in the
   narrative. Mara's flip honored (no conflict). Mutations ledger shows
   all three with their causes.
6. Fan-out: Neil's email (catch-up: none missed; narrative: crow took
   the bottle) and the newcomer's email (catch-up: turns 1–11 summary;
   "you are Mara, the fellow hiker").

## 6. Explicit non-goals / deferred

- Roster leg mechanics for claims (timeouts, retries, staging-dir
  draining) — reuse the `phase4-gm-integration.md` patterns and the
  turn_loop send/staging lessons; not re-derived here.
- Which R4T agents play animals/bosses, and the narrative voice — the
  roster's standing prompt is Neil-adjacent taste territory; the *contract*
  here is taste-free.
- Encryption at rest (standing open question #4) — claims carry no new
  secret class, so nothing changes.
- Claim rate limits / anti-spam from roster actors (a crow submitting 40
  claims a turn) — operational, deferred to first roster-actor trials;
  the idempotency rule + one-claim-per-actor-per-turn cap is the starting
  position.
- Prose/style of any of this (Neil's eye).

## 7. Build order (when Phase 4 starts)

1. Schema migration: `controller`, `email_address`, `claims`, `players`
   (migration script + demo pinning the migration, the repo's usual
   contract-first pattern).
2. Dispatch: `join <guid>` handling + `players` routing (guid+sender).
3. `turn_loop` gather: merge player inputs + pending claims +
   `ai` defaults; fan-out N emails.
4. Roster claim leg: `call: "claim"` on the atfl node, idempotent
   staging, re-issue discipline (guid+turn+call identity).
5. First roster actor trial (the crow) — expect the denial rules to get
   their first real workout; worked examples become regression cases.
