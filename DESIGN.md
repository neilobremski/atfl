# Above the Fog Line — DESIGN.md (Phase 1, in progress)

Design doc for the single-player MVP (Phase 2). Written section by section;
the email protocol below is locked. Remaining sections are stubs until their
work sessions.

## 1. Email protocol (LOCKED 2026-09-26)

### 1.1 Identity and the GUID
- The game lives at one dedicated address (the game's own free Google
  account; see `research/phase0-email-identity.md`). Players email that
  address to start.
- Every outbound turn email carries the game's GUID in a body footer line:

  `Game code: <GUID>`

  and in the subject tag `[ATFL <first-8-hex>]` so players can find their
  game thread in a crowded inbox.
- Inbound matching: **GUID + sender address** is the join key into the
  world-state DB (`games.guid`, `games.player_email` — "game files live
  under GUID + sender address, semi-secure", per the design notes). A reply
  without a GUID in the body is matched by sender address alone if that
  sender has exactly one active game; zero or multiple active games gets a
  clarification email, not a guess.
- GUID format: UUID4, lowercase hex with dashes (36 chars). Short form
  (first 8 hex) is display-only, never a join key.
- Gmail threading: replies keep `In-Reply-To` / `References` so each game's
  turns form one thread in the player's inbox.

### 1.2 Turn cadence
- One game-time turn ≈ **1 hour of game clock** (fixed per scenario; the
  design notes' default).
- Real-world cadence for play: roughly **one turn email per player per day**.
- The server polls the game mailbox every few minutes; a player email
  starts a turn when it arrives — there is no fixed daily deadline in the
  single-player MVP. Playtest tempo (hourly emails, daylight hours) is a
  scenario config, not a protocol change.
- The GM sends a turn reply only when a turn ran. No spam: at most one
  outbound email per inbound player email, plus at most one daily
  "still your move"-style nudge for stalled games (Phase 2 detail).

### 1.3 Expired-turn rules
The design notes already fix the philosophy: idle players' actors get
AI-driven, the player gets a daily email with the frames they missed, and
a reply to an expired turn still gets taken into account. Concrete rules:

1. **A turn never expires for lack of reply.** The world keeps moving on the
   daily nudge cadence; the GM runs the player's actor with a conservative
   default (the classic PBEM pattern: missed turn = GM drives, no free
   lunch, no free death either — defaults are cautious, never suicidal).
2. **A reply that arrives after its turn ran is not lost.** It is folded
   into the *next* turn's gather step: the player's intent is adjudicated
   fresh, and the mutation audit marks it `cause: "late reply to turn N"`.
3. **Missed frames are summarized.** Every turn email opens with a one-line
   catch-up of anything the player missed since their last input
   ("While you were quiet: the fog thickened; your bottle is empty.").
4. **Death ends the game.** If a defaulted turn kills the player, the game
   ends — same as an answered turn. Ephemeral games cut both ways; the
   daily nudge exists to keep this fair.
5. Multiplayer/idle-player AI driving (Phase 4) reuses these rules verbatim.

## 2. Turn structure (LOCKED 2026-09-26)

### 2.1 Turn length and cadence
- One turn = a fixed `turn_len_min` game-clock minutes, set per scenario.
  Default: **60** (the design notes' ~1h game time).
- Reconciles the design-notes flow step "set the time length of the turn":
  the length is fixed by scenario config, not chosen per turn; that step is
  now "advance the clock by the fixed length".
- Real-world pacing is player-driven: every inbound player email runs one
  turn. If no player input and no turn has run in the last ~24h of real
  time, the server runs an idle turn (GM conservative default, §1.3). Net
  effect: ~1 email/player/day for active games, never exceeding the
  protocol's 1:1 inbound cap plus the daily idle turn.
- Playtest tempo (hourly emails, daylight hours) is a scenario config
  (`idle_turn_interval_h`), not a protocol change.
- `game_clock_min` advances only when turns run. ~30 daily turns ≈ ~30
  game-hours ≈ the design notes' "about a month" game.

### 2.2 The five-step pipeline (contract)
The design-notes agentic flow, with a per-step contract (schema operations
in `research/phase0-world-state-schema.md`; embodied in
`prototype/world_state_demo.py`, to be factored into a shared `run_turn()`
in Phase 2):

1. **Gather** — SELECT the current place, the actors there, and the objects
   in those places and inventories. Elapsed-time reconciliation runs first
   (turns since `last_touched_turn` → passive mutations), so the GM reasons
   from fresh state.
2. **Yes/no mutations** — the GM emits `mutation_questions`; every proposed
   state change is a yes/no question with rationale. Approved effects land
   as `mutations` rows. Denied actions keep their `no` row (auditable) with
   rationale and an optional partial effect ("chips the bark instead").
3. **Narrative** — one `narrative` string on the `turns` row, composed from
   the approved mutations plus the catch-up line. Rendered per §5; must
   pass the secrecy check (§2.5.5).
4. **Advance time** — `games.game_clock_min += turn_len_min`;
   `games.turn_no += 1`.
5. **Update** — UPDATE touched rows' JSON state, stamp their
   `last_*_turn` to the new turn, INSERT new assets, INSERT the `turns`
   row.

Serial per game: at most one turn runs at a time. Inbound that arrives
while a turn is running is queued and folded into the *next* turn's gather
step (same rule as late replies: never dropped, never raced).

Player input extraction: `turns.player_input` is the email body minus the
`Game code: <GUID>` footer line. Attachments are ignored in the MVP
(logged, not acted on).

### 2.3 Idle turns and the daily touch
- An idle turn is a normal turn whose `player_input` reads "idle default";
  the GM drives the player's actor with a conservative default (§1.3).
- The idle-turn email leads with the catch-up line and ends with an open
  prompt — it *is* the day's "still your move" touch. No separate "are you
  there?" email while idle turns are running.
- Standalone nudge (fallback only): fires if an active game went ≥24h of
  real time with **no turn email at all** (idle-turn pipeline down, or a
  scenario that disables idle turns). It advances nothing and mutates
  nothing; at most one per 24h per game.

### 2.4 Nudge wording policy (structural — prose style is Neil's eye)
Nudges and catch-ups carry constraints, not style (the prose itself is a
review checkpoint under §5):
1. Never reveal anything from `hidden_traits` or `plot_concept` — only
   established, player-visible facts.
2. The catch-up line is mandatory whenever the player missed frames since
   their last input. Format: `While you were quiet: <1–2 concrete events>.`
   Each event must be auditable against `mutations` rows since the player's
   last input turn.
3. End with an open question or concrete choice ("What do you do?",
   "Follow the trail or go back for the bottle?") — never a demand, never
   invented urgency the world state doesn't support.
4. ≤120 words for a standalone nudge. No guilt-tripping, no manufactured
   FOMO.
5. Dead or ended game: no nudges, ever.

### 2.5 Per-turn done criteria
A turn is done only when ALL of these hold:
1. `turns` row written: turn_no, game-clock window
   `[start, start+turn_len_min)`, player_input (or "idle default"),
   mutation_questions JSON, narrative, created_at.
2. **No silent mutations**: every state change the turn made has a
   `mutations` row with old/new values and a cause (an adjudication
   question, "elapsed-time reconciliation", or "late reply to turn N").
3. Clock advanced: `games.game_clock_min` and `games.turn_no`
   incremented; all touched rows' `last_*_turn` stamped to the new turn.
4. Outbound email: exactly one sent (the turn email; or the standalone
   nudge if the fallback fired). It carries the `Game code: <GUID>` footer,
   the `[ATFL <8hex>]` subject tag, and Gmail threading headers.
5. **Secrecy check**: the rendered narrative contains nothing from
   `hidden_traits`/`plot_concept` (MVP: string-level check against a
   GM-side denylist built from the gathered entities' hidden JSON).
6. Catch-up lead: if the player missed frames since their last input, the
   email opens with the §2.4 catch-up line.
7. Death/ending: if the turn killed the player, `status='dead'`,
   `ended_at` set, and the scheduler never runs another turn or nudge for
   that game.

### 2.6 Turn failure handling
- A turn that can't complete (GM produced no usable output, send failed):
  retry once with the same inputs.
- After two failures: no invented fiction goes to the player. The turn is
  marked failed (logged; surfaced in the daily digest for Neil), and the
  next inbound or idle-turn slot starts a fresh turn.
- Partial state is never emailed: a turn email goes out only when every
  §2.5 criterion holds.

## 3. Scenario seed: "fog-line-mystery-v1" (LOCKED 2026-09-26)

The fixed mystery for the Phase 2 single-player MVP. Seed = turn 0 state:
the trailhead above the fog, the player, the water bottle, the red drop —
the design notes' opening ("the perfect start to a mystery"). All
mechanics below are locked; the plot concept and the intro prose are not
(see §3.4).

### 3.1 Scenario config
- `scenario_id`: `fog-line-mystery-v1`
- `turn_len_min`: 60 (one game-hour per turn; §2.1)
- Game clock: `game_clock_min = 0` ↔ **07:00 local**. Config
  `day_start_min_of_day: 420`; time-of-day for any game time `t` is
  `(420 + t) % 1440`. The renderer (§5) and later the video/sound
  pipeline (Phase 5) are time-of-day aware off this function.
- **Morning, not dusk.** The prototype demo seeded dusk; morning
  supersedes it — the fog is a morning phenomenon, and the design notes'
  origin is a bright morning over an ocean of fog.
- `plot_concept`: **NULL at seed** — the GM picks one from the §4 roster
  at game start, before composing the turn-1 narrative. The prototype's
  demo value ("a spell") was a placeholder, never canon.

### 3.2 Seed entities (turn 0 — before any turn runs)

`games` row: scenario_id `fog-line-mystery-v1`, plot_concept NULL,
status `active`, turn_no 0, game_clock_min 0. (GUID and player_email are
assigned per signup; §1.1.)

**places**

1. `trailhead` — "Trail above the fog line" — *discovered=1,
   last_visited_turn=0*
   - physical_state: `{fog_density_local: 0.0, fog_below: true,
     light: "morning", temp_c: 8, wind: "light", ground: "damp gravel",
     trail_empty: true}` — locally clear above the line; the ocean of
     fog is *below*, visible but not present.
   - hidden_traits: `{}` — the GM seeds place secrets at plot pick (§4).

2. `trail-down` — "The trail descends toward the fog." — *discovered=0*
   - physical: `{fog_density: 0.4, light: "morning"}`; hidden: `{}`.

3. `trail-up` — "Switchbacks climb the ridge, away from the fog." —
   *discovered=0* — physical: `{fog_density: 0.0, light: "morning"}`;
   hidden: `{}`.

4. `fog-below` — "The ocean of fog below the ridge." — *discovered=0*
   (visible from the trailhead, never entered) —
   physical: `{fog_density: 1.0}`; hidden: `{}`.

**actors**

- `player` — name "You", kind `player`, is_player=1,
  location `trailhead`, last_acted_turn=0
  - physical_state: `{hp: 1.0, hunger: 0.2, fatigue: 0.3, wetness: 0.0,
    cold: 0.2, pose: "standing", facing: "down-trail"}`
  - hidden_traits: `{}` — GM assigns hidden player stats at plot pick
    (conventions in §4).
  - inventory: `{"hands": [null, null], "backpack": [null × 8]}`
    (fixed slots; the composite image shows hands + backpack, §5/Phase 3).

**objects**

- `water-bottle` — "Water bottle" — holder `place:trailhead`
  - description: "A half-full bottle sitting on an otherwise empty trail."
  - physical_state: `{water_ml: 400, cap_on: false, tipped: false}`
  - hidden_traits: `{unexplained: true, owner: null}` — facts the GM
    knows are unknown; the answers belong to the plot, not the seed.

- `red-drop` — "A red drop" — holder **`actor:player:cheek`**
  - description: "A single red drop on the player's cheek. It isn't
    raining."
  - physical_state: `{volume_ml: 0.05, color: "red", wet: true,
    dried: false}`
  - hidden_traits: `{unexplained: true}` — the origin is the plot's
    business (§4); the seed records only that it has none yet.
  - Holder-extension (locked): `holder` allows `actor:<slug>:<body-spot>`
    for on-body positions outside inventory slots (`cheek`, `shoulder`,
    …). Hands/backpack slots stay strictly for the fixed-slot inventory.

Both objects are covered by elapsed-time reconciliation (§2.2): an open
bottle evaporates, a wet drop dries — already demo'd in
`prototype/world_state_demo.py`.

### 3.3 Turn-1 rules (structural — prose is Neil's eye)

1. **The signup email IS turn 1's player_input** (e.g. "start", or the
   player's first words). All §2.5 done criteria apply: the turn-1
   outbound email is the intro, carrying the GUID footer and subject tag
   from turn 1 — the 1:1 inbound/outbound invariant starts immediately.
2. The GM picks `plot_concept` from the §4 roster *before* composing the
   turn-1 narrative and records the pick in turn 1's update step
   (a `mutations` row with entity_type `game`, field `plot_concept`,
   cause "plot pick at game start").
3. Turn-1 narrative duties (structural, not style): establish the four
   player-verifiable facts — (a) above the fog, fog ocean below;
   (b) the trail is empty; (c) half-full bottle, cap off, at your feet;
   (d) red drop on your cheek, not raining — name the two trail
   directions and the fog below; end with an open question. The email
   must pass the secrecy check (§2.5.5): no hint of hidden_traits or
   plot_concept.

### 3.4 Deferred (not in the seed, not this session)

- The **plot-concept roster + beat pacing** — §4.
- **The intro email's prose** — a draft sits in the progress log under
  "Waiting on Neil's eye" (2026-09-26); Neil's read drives the renderer
  spec (§5).
- The red drop's origin and the bottle's owner — these are answers, and
  answers live in the GM's plot, never in the seed.

## 4. GM rules and plot-concept roster (stub)
GM: hard rules, D&D dungeon-master style, R4T roster agent. Picks one plot
concept at start (aliens / government project / you're dead / a spell / the
earth changing) and nudges the player in slow beats. Needs: the roster list,
beat pacing, hidden-stat conventions.

## 5. Renderer spec (stub)
Phase 2 MVP is text-first by plan. Needs: what a turn email looks like
(text layout, footer, map-as-text placeholder) before Phase 3's
three-part image composite.

## 6. MVP "done" criteria (stub)
A playable game over email: start by email → turns flow → GUID-threaded
thread → world state persists → death/ending stops the emails. Playtest
with Neil by email. Needs: the acceptance checklist.

## Decisions (2026-09-26)
- GUID+sender-address join; clarification email instead of guessing on
  ambiguous inbound (never mutate the wrong game).
- No hard turn deadline in single-player MVP; AI-driven conservative
  defaults for idle turns; late replies folded into the next turn, never
  dropped; catch-up line leads every turn email.
- UUID4 lowercase hex GUIDs; `[ATFL <8-hex>]` subject tag; footer
  `Game code: <GUID>` line; Gmail threading headers preserved.
- Turn length fixed per scenario, default 60 game-min (`turn_len_min`
  config) — reconciles the design-notes "set the time length" step, which
  is now "advance the clock by the fixed length". Real-world pacing is
  player-driven + one idle turn per ~24h; ~1 email/day for active games.
- Idle-turn email doubles as the daily touch (catch-up lead + open prompt);
  standalone nudges are fallback-only, ≤1 per 24h per game, mutate nothing,
  and follow a structural wording policy (no spoilers, catch-up auditable
  against mutations, ≤120 words, no guilt).
- Turns are serial per game; mid-turn inbound is queued into the next
  turn's gather. Per-turn done criteria: turns row + no silent mutations +
  clock advanced + exactly one outbound email with GUID/threading +
  secrecy check + catch-up lead + death handling. Failed turns retry
  once, then are logged and surfaced in the digest — never send invented
  fiction.
- Narrative prose style is deliberately unspecified here — it's a review
  checkpoint for Neil's eye under the renderer spec (§5).

## Open questions for Neil
None new — the turn-structure questions are answered by the design notes +
the decisions above; the standing ones (game email account, GM model)
stand.
