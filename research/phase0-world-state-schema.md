# Above the Fog Line — world-state schema sketch (Phase 0)

2026-09-26. One SQLite file per game, named `<GUID>.db`, stored under the
player's game directory (`GUID + sender address`). It travels with the player,
is queryable for the GM, and holds the full audit trail of every turn — the
answer to "Cowherd's 3.5-turbo game had bad memory": hard rules + on-disk state.

Neil's note says places/actors/objects are *directories* with files for physical
state, GM-only hidden traits, and generated assets. This sketch maps that onto
SQLite: one row per thing, JSON columns for the mutable state (small, fast to
read/write whole), a separate `assets` table for generated media (files on disk,
rows as the registry), and a `mutations` audit table so the yes/no adjudication
is inspectable instead of buried in prose.

## Tables

### games — one row per game
- `guid` TEXT PK — the code carried in email bodies
- `player_email` TEXT — sender address the game is bound to
- `scenario_id` TEXT — the fixed mystery scenario for this game (Phase 2: one scenario)
- `plot_concept` TEXT — GM-only: the plot picked at start (aliens / govt project / dead / spell / earth changing…). Never rendered to the player.
- `status` TEXT — `active | dead | ended`
- `turn_no` INTEGER — current turn counter
- `game_clock_min` INTEGER — minutes of game time elapsed since start
- `started_at` TEXT, `ended_at` TEXT — real-world timestamps (ISO)

### places
- `id` INTEGER PK, `slug` TEXT UNIQUE (`trailhead`, `switchback-3`…), `name`, `description` TEXT
- `physical_state` JSON — what any observer would see: weather, light level, fog density, ground wetness…
- `hidden_traits` JSON — GM-only: what's really here (e.g. `{"buried_cache": true}`), the thing the mystery beats uncover
- `discovered` INTEGER 0/1, `last_visited_turn` INTEGER
- Assets (scene renders of this place) live in `assets`, keyed by place id.

### actors — players, NPCs, creatures are all actors
- `id` INTEGER PK, `slug`, `name`, `kind` (`player | npc | creature`), `is_player` 0/1
- `location_slug` TEXT — where they are; FK-ish to places.slug (kept loose so "missing" is a state, not a constraint violation)
- `physical_state` JSON — obvious attributes: `hp`, `hunger`, `fatigue`, `wetness`, `encumbrance`… plus `pose`, `facing`
- `hidden_traits` JSON — GM-only: `{"afraid_of_dark": 0.8}`, secret stats, the plot's grip on this actor
- `inventory` JSON — **fixed slots**: `{"hands": [slot, slot], "backpack": [×8]}`. Each slot holds an object slug or null. What the hands/backpack hold is what the image composite shows.
- `ai_driver` 0/1 — for idle players (Phase 4)
- `last_acted_turn` INTEGER

### objects
- `id` INTEGER PK, `slug`, `name`, `description` TEXT
- `physical_state` JSON — e.g. water bottle: `{"water_ml": 750, "cap_on": true, "tipped": false}`
- `hidden_traits` JSON — GM-only (the bottle isn't just a bottle, maybe)
- `holder` TEXT — `place:<slug>` or `actor:<slug>:<slot>` (`hands[0]`, `backpack[3]`…) — one location, no ambiguity
- `last_touched_turn` INTEGER — for elapsed-time reconciliation on revisit
- Objects are static (they don't act), but **reconcile elapsed time when revisited**: turns since `last_touched_turn` → passive effects (water evaporates, leaks spread, the red drop dries). The GM applies these as mutations before the turn's main adjudication.

### turns — the per-turn record
- `id` INTEGER PK, `game_guid`, `turn_no` INTEGER
- `game_time_start_min`, `game_time_len_min` — this turn covered game-clock [start, start+len); default len ≈ 60
- `player_input` TEXT — the raw email text
- `mutation_questions` JSON — the yes/no adjudication list, e.g.
  `[{"q": "Is the player strong enough to fell the tree?", "answer": "no",
     "rationale": "fatigue 0.7, no axe", "effect": "chips the bark instead"}]`
- `narrative` TEXT — the combined story text for the turn
- `created_at` TEXT — real timestamp

### mutations — audit trail of every state change
- `id` INTEGER PK, `turn_id` FK, `entity_type` (`place|actor|object`), `entity_id`
- `field` TEXT — JSON path changed, e.g. `physical_state.water_ml`
- `old_value` TEXT, `new_value` TEXT — JSON-encoded
- `cause` TEXT — which mutation question (or "elapsed-time reconciliation") produced it

This is the "hard rules" ledger: the GM proposes changes as yes/no questions,
the approved ones land here, and nothing mutates state without a row.

### assets — registry for generated media
- `id` INTEGER PK, `entity_type`, `entity_id`, `kind`
  (`scene | map | selfie | sound | video | other`)
- `path` TEXT — file on disk under the game directory
- `turn_created` INTEGER, `prompt` TEXT, `prompt_hash` TEXT
- Consistency across turns: the renderer looks up the latest `scene` asset for
  the current place as the reference/seed for the next one.

### (Phase 1, not schema'd here) emails
GUID handling, inbound/outbound message ids, and turn cadence belong to the
email protocol in DESIGN.md; the schema only needs `games.guid` + `player_email`
as the join key.

## Per-turn flow → schema operations

Neil's agentic flow, mapped 1:1:

1. **Gather** — `SELECT` the current place, actors there, objects in those
   places/inventories. For each object, compute `turn_no - last_touched_turn`
   and apply elapsed-time reconciliation as mutations first.
2. **Yes/no mutations** — the GM emits `mutation_questions`; each answered
   question with an effect becomes one or more `mutations` rows. Denied actions
   get `answer: "no"` with rationale and no mutation rows (or a partial-effect
   row, e.g. "chips the bark").
3. **Narrative** — one `narrative` string on the `turns` row, composed from the
   approved mutations.
4. **Advance time** — `game_clock_min += game_time_len_min`; `turn_no += 1`.
5. **Update** — `UPDATE` the touched rows' JSON state, set their
   `last_*_turn` to the new turn, `INSERT` any new assets.

Concurrency is trivial: one writer (the turn loop), one game per file.

## Design decisions (2026-09-26)
- JSON columns over strict relational attributes: the attribute list is
  intentionally minimal and scenario-dependent; JSON keeps the schema stable
  while scenarios vary. The `mutations` table provides the structure queries
  need (what changed, when, why).
- Elapsed-time reconciliation is a first-class step (step 1), not GM whimsy:
  it runs before adjudication so the GM reasons from fresh state.
- Fixed inventory slots in JSON (not rows): slots are positional and few;
  rows would add joins for no benefit.
- `plot_concept` on `games`, `hidden_traits` on everything: the GM's secret
  layer is explicit in the schema, never mixed into rendered state.
- One DB file per game (not per player): games are ephemeral; death/ending
  stops the emails and the file is the archive.

## Open (for Phase 1 DESIGN.md, not blocking)
- Exact game-clock turn length and real-world cadence (design note says ~1h
  game time / ~1 email per day; playtest tempo TBD).
- Whether `hidden_traits` should be encrypted at rest (semi-secure GUID model
  says the file itself is the secret; note for later).
