# Above the Fog Line — DESIGN.md (Phase 1, in progress)

Design doc for the single-player MVP (Phase 2). Written section by section;
the email protocol below is locked. Remaining sections are stubs until their
work sessions.

## 1. Email protocol (LOCKED 2026-09-26; relay edition 2026-10-02)

### 1.1 Identity and the GUID
- The game has no email identity of its own. Players email
  **murph@inkboxmail.com**; Murph (the operator's agent) is the exchange
  layer between the engine and players (decision 2026-09-30). The engine
  never sends email directly and never polls a mailbox.
- Transport between engine and Murph is A8S tell with structured JSON
  envelopes (docs/mail-relay-design.md): `atfl_outbound` (engine →
  Murph: game_guid, turn_no, to, subject, body_text, body_html,
  attachments) and `atfl_inbound` (Murph → engine: from, subject,
  body_text, inkbox_message_id). The engine's mail-adjacent surface is
  the normalized inbound-dict contract the old GmailClient defined.
- Every outbound turn email carries the game's GUID in a body footer line:

  `Game code: <GUID>`

  and in the subject tag `[ATFL <first-8-hex>]` so players can find their
  game thread in a crowded inbox. Murph preserves subjects verbatim, so
  GUID routing survives the relay unmodified.
- Inbound matching: **GUID + sender address** is the join key into the
  world-state DB (`games.guid`, `games.player_email` — "game files live
  under GUID + sender address, semi-secure", per the design notes). A reply
  without a GUID in the body is matched by sender address alone if that
  sender has exactly one active game; zero or multiple active games gets a
  clarification email, not a guess.
- GUID format: UUID4, lowercase hex with dashes (36 chars). Short form
  (first 8 hex) is display-only, never a join key.
- Threading: Murph owns the player-facing thread — one Inkbox thread per
  game, In-Reply-To/References managed on Murph's side. The engine keeps
  no thread state (the old `thread_message_id` / `thread_refs` columns
  are retired).

### 1.2 Turn cadence
- One game-time turn ≈ **1 hour of game clock** (fixed per scenario; the
  design notes' default).
- Real-world cadence for play: roughly **one turn email per player per day**.
- The server polls its A8S inbox every few minutes; a forwarded player
  email starts a turn when it arrives — there is no fixed daily deadline in the
  single-player MVP. Playtest tempo (hourly emails, daylight hours) is a
  scenario config, not a protocol change.
- The GM sends a turn reply only when a turn ran. No spam: at most one
  outbound handoff per inbound player email, plus at most one daily
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
   Effect language: `physical_state.<key>` changes on any target, plus
   `location_slug` on `actor:` targets for movement (2026-10-09 — the
   roster could not record movement before this, freezing the map and
   scene prompt). Place `discovered`/`last_visited_turn` are
   engine-derived from the player's post-effects location, never
   roster-written.
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

Roster calls are send-then-wait (async A8S transport): each call waits up
to 2h for the keeper's reply, then sends ONE re-prompt and waits 30min
more before failing the turn loudly (TurnFailed). The wait is no longer
silent — a "still waiting" heartbeat line fires every 10min naming the
call/game/turn, elapsed, and remaining budget, and the re-prompt send and
final failure print loud boundary lines (2026-10-09: a 2h silent wait
looked identical to a dead roster in the journal).

Player input extraction: `turns.player_input` is the email body minus the
`Game code: <GUID>` footer line. Attachments are ignored in the MVP
(logged, not acted on).

### 2.3 Idle turns and the daily touch
- An idle turn is a normal turn whose `player_input` reads "idle default";
  the GM drives the player's actor with a conservative default (§1.3).
- The idle-turn email leads with the catch-up line and ends with the
  world blocks — the open prompt is gone (2026-09-27, Neil); the idle
  turn *is* the day's "still your move" touch. No separate "are you
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
3. (Superseded 2026-09-27 for turn emails: Neil removed the open
   prompt — turns end without a question, and silence folds into the
   next catch-up. Applies now only to standalone-nudge wording, and
   the nudge prose is placeholder per §5 anyway.) Formerly: end with
   an open question or concrete choice ("What do you do?",
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
   nudge if the fallback fired). It carries the `Game code: <GUID>` footer
   and the `[ATFL <8hex>]` subject tag; threading rides on the relay's
   per-game thread (the Gmail threading headers of the pre-relay design
   were superseded by the 2026-09-30 relay edition).
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

1. `trailhead` — "Trailhead" — *discovered=1,
   last_visited_turn=0*
   - physical_state: `{fog_density_local: 0.0, fog_below: true,
     light: "morning", temp_c: 8, wind: "light", ground: "damp gravel",
     trail_empty: true}` — locally clear above the line; the ocean of
     fog is *below*, visible but not present.
   - hidden_traits: `{}` — the GM seeds place secrets at plot pick (§4).

2. `trail-down` — "Descent" — *discovered=0*
   - physical: `{fog_density: 0.4, light: "morning"}`; hidden: `{}`.

3. `trail-up` — "Switchbacks" —
   *discovered=0* — physical: `{fog_density: 0.0, light: "morning"}`;
   hidden: `{}`.

4. `fog-below` — "Fog" — *discovered=0*
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

## 4. GM rules and plot-concept roster (LOCKED 2026-09-26)

The GM is a D&D dungeon-master-style agent on the R4T roster (model TBD —
standing open question #2). These rules are its contract: they bind what the
GM may do, whatever model ends up behind it. §4 is structural; the narrative
prose voice is a review checkpoint for Neil's eye (§5).

### 4.1 GM hard rules

1. **World-model supremacy.** The SQLite game database is canon. The LLM's
   text is the presentation layer, never the source of truth: a fact in the
   narrative must be checkable against `entities`/`mutations` state, or
   against a declared hidden trait. If state and an old narrative line
   conflict, state wins and the narrative adjusts.
2. **Mutations only through the pipeline.** Every world change happens in
   the §2.2 five-step turn flow — gather → yes/no mutation questions →
   narrative → advance clock → update. There are no off-turn mutations:
   idle turns, nudges, and expiry handling (§2.3) mutate nothing unless a
   §2.2 turn is running.
3. **No invented answers.** The seed (§3.2) contains only `{unexplained}`
   markers. The GM may resolve a marker ONLY into the answer implied by the
   chosen plot concept (§4.3) — never into a fresh invention. If the plot
   concept has no answer for a marker, it stays unexplained and the
   narrative says nothing about it.
4. **One plot, committed.** `plot_concept` is picked once at game start
   (§3.3.2) and never changed. The GM cannot pivot to another roster
   concept mid-game to cover a hole — holes are covered by honesty:
   "you find nothing."
5. **Secrecy.** The GM knows the full truth from turn 1 and never leaks
   it: no hidden-trait names or values in outbound email, no fourth-wall
   references to mechanics ("your cold stat", "hidden traits"), no
   out-of-character reasoning. The §2.5.5 secrecy check runs every turn.
   Failed → the turn is rewritten, not shipped.
6. **Neutral arbiter.** The GM is neither the player's ally nor enemy:
   danger is real and death stops the emails (§2.1). Player claims succeed
   or fail through the yes/no questions of step 2, gated by state and
   hidden stats — "a player too weak to fell a tree only chips it."
   Natural-language setting changes are allowed, but hard physics apply
   (no flying).
7. **GM as proxy.** The GM filters raw player input: it parses intent,
   rejects out-of-world requests (real-world commands, prompt-injection
   attempts, "tell me the plot"), and answers only from the game. An
   out-of-world reply is a clarification, not a turn (mutates nothing).
8. **Player agency over plot.** Beats are opportunities, not rails. If the
   player ignores the plot, the GM answers honestly from the world model
   and folds unmet beats back in when the player returns to relevant
   ground — or lets the game end unresolved at the final beat, if the
   player never engages.

### 4.2 Beat pacing (structural)

Target: ~30 turns, roughly one email a day for about a month. Playtest
tempo: hourly emails during daylight (§design notes). Turn length is
60 game-minutes; pacing is measured in turns, never in prose timing.

| Beat | Turns | Structural duty |
|---|---|---|
| Establish | 1–5 | The four seed facts are in play; the player has two named directions and the fog. No plot claims — only the unexplained markers (§3.3.3). |
| First doubt | 6–12 | One unexplained marker develops (moves, changes state, recurs). The player should be able to name what's wrong, not why. |
| Escalation | 13–20 | A second marker develops; the first hints at a rule (it behaves *as if* something). GM seeds one verifiable plot-consistent clue per 2–3 turns. |
| Point of no return | 21–28 | A player-visible change the plot concept makes irreversible (the trail up closes / the fog reaches the trailhead / someone answers). Survival decisions start costing. |
| Resolution | 29–30+ | The plot's answer becomes state, or the player earns it and the game ends — or death ends it. Ephemeral: the emails stop either way. |

Guidance (not rules): nudges follow the §2.4 wording policy; an idle-turn
AI move defaults to "wait and observe" (it lets the next beat arrive on
schedule rather than chasing). If the player is ahead of the beat map,
the GM accelerates; if behind, it decelerates — the beat *windows* are
soft, the turn count is not stretched beyond ~35.

### 4.3 Plot-concept roster

Five concepts, from Neil's design notes. One is picked at game start.
**The pick is Neil's call** — Murph's lean (see Decisions) is not locked.
Each entry gives: the truth; what the three seed markers are under it
(fog ocean, red drop, bottle — the seed itself only holds `{unexplained}`
facts, §3.4); the five beats in this concept's shape; and the resolution
condition.

#### 4.3.1 The fog is alien (aliens)

- **Truth.** Something below the fog is not human, and it is watching —
  sampling, not attacking.
- **Markers.** Fog: a *held* phenomenon, unnaturally still, like a lid.
  Red drop: not blood — ichor from whatever touched the player's cheek
  while they slept or climbed (it is not raining, so it fell *from*
  something, not the sky). Bottle: half-full, cap off, left by the last
  visitor — they did not finish the water, and they are gone.
- **Beats.** Establish: the stillness is total — no birds, no insects
  (a verifiable absence). First doubt: the drop returns — same cheek,
  same spot — overnight. Escalation: patterns in the fog surface that
  behave as if *attending* to the player (it re-forms where they look).
  Point of no return: the trail up is blocked by the fog itself, which
  has climbed. Resolution: the player sees what is below — contact,
  refusal, or death.
- **Resolution.** First contact — speak, flee, or hide — or death below
  the fog line.

#### 4.3.2 The fog is a project (government project)

- **Truth.** The fog is a test plume from a classified program; the
  trailhead sits on the edge of a monitored zone.
- **Markers.** Fog: too uniform, edges too sharp for weather — a
  *dispersed* phenomenon with a boundary. Red drop: tracer dye from the
  plume's edge, sticky, slightly chemical. Bottle: a field tech's,
  dropped when they were extracted in a hurry.
- **Beats.** Establish: the fog's edge is a clean line you can walk
  along. First doubt: low aircraft — or something like it — at regular
  intervals. Escalation: unmarked equipment in the brush, turned *away*
  from the trail (watching the fog, not the hiker). Point of no return:
  the trail up is closed — official-looking barriers, fresh. Resolution:
  the player finds the observation post, is intercepted, or gets below
  the fog and sees the array.
- **Resolution.** The player reaches the array / is picked up / turns
  back with proof.

#### 4.3.3 You are already dead (you're dead)

- **Truth.** The player died on the ridge — the red drop is their own,
  and everything since is the time between.
- **Markers.** Fog: not a weather event — it's where the trail, the
  mountain, and the world simply stop being knowable. Red drop: the
  player's own blood; the cheek injury matches a fall they cannot
  remember. Bottle: half-full because the person who owned it no longer
  needs the other half.
- **Beats.** Establish: small wrongnesses — no wind, no animals, the
  same birdsong twice. First doubt: the player cannot recall how they
  got above the fog line, or when. Escalation: evidence of a search —
  gear, voices — that never quite arrives. Point of no return: the
  player finds their own body, or the fog shows them the moment.
  Resolution: acceptance or denial; the emails end with one or the other.
- **Resolution.** The player accepts it and the game closes — or refuses,
  and the loop tightens (death-in-place, emails stop).

#### 4.3.4 The fog is a spell (a spell)

- **Truth.** Someone cast something weather-scale on this mountain; the
  fog is a working, with a caster and a purpose.
- **Markers.** Fog: moves with *intention* — it thickens against the
  wind, thins where the player walks, like it is being held. Red drop:
  a reagent-mark — deliberately placed, too precise for accident.
  Bottle: an offering or a mistake — cap off because it was *used* in
  the casting.
- **Beats.** Establish: the fog responds — it parts where you step, too
  cleanly. First doubt: glyphs or arranged stones where fog thins, fresh.
  Escalation: a presence on the trail — footprints that start and stop,
  a voice in the fog that knows the player's name. Point of no return:
  the caster is close, and the fog is being *re-cast* around the player.
  Resolution: confrontation — break it, join it, or be worked into it.
- **Resolution.** The spell breaks, is renewed by the player's hand, or
  consumes them.

#### 4.3.5 The fog is wrong itself (the earth changing)

- **Truth.** No agency: the world has changed in a way no one ordered —
  the fog is a new natural phenomenon and the rules below it are
  different. (Murph's lean — closest to Neil's Silent Hill / ocean-of-fog
  origin.)
- **Markers.** Fog: geological — it behaves like an ocean because it
  *is* one now, a new layer of the world. Red drop: condensation of
  something the air carries — the fog sheds, faintly, on everything
  above it. Bottle: left by the last person who came up — they stopped
  needing it, one way or another.
- **Beats.** Establish: the fog has tides — it breathes on a slow
  cycle, rising and falling like water. First doubt: familiar landmarks
  *below* are wrong — the wrong shape, the wrong trees, the wrong
  silence. Escalation: things come up out of it — fog-fauna, sounds —
  that obey the new rules. Point of no return: the fog reaches the
  trailhead; above and below trade places. Resolution: the player learns
  the new rules and lives by them, climbs beyond them, or drowns in
  air below the line.
- **Resolution.** Adaptation (live in the new world), escape (climb
  past it — and find what the mountain has become), or death below the
  fog line.

**Roster discipline.** New plot concepts can be added by Neil at any time
(he owns the mystery), but a game in progress never changes its pick.
Concepts not on this roster do not exist as answers — the GM cannot
improvise a sixth concept to resolve a stuck game.

### 4.4 Hidden-stat conventions

- **Obvious vs hidden.** Per the design notes, each entity carries both:
  obvious state (shown in the renderer — hunger, fatigue, bottle level)
  and GM-secret state in `hidden_traits` (plot-specific meters, fear,
  exposure, the truth-values behind markers). The renderer receives a
  filtered view: obvious state only.
- **Seed discipline.** At seed, `hidden_traits` hold `{unexplained}`
  markers only — no answers (§3.2, §3.4). The GM's chosen concept gives
  each marker its meaning at turn 1 (recorded in the mutation ledger,
  internal only), and beats *convert* markers into facts over the game's
  life — each conversion is a mutation with a cause, auditable in the
  ledger.
- **Hidden changes are still mutations.** The GM's internal stat moves
  (exposure rising, a marker developing) go through the same `mutations`
  table with entity_type `game` or the relevant entity — visible to
  Murph/Neil in audit, never to the player in email.
- **Hidden stats gate claims.** Step-2 yes/no questions consult hidden
  stats the player can't see: "am I too cold to continue the descent?"
  resolves from `player.cold` + descent exposure, and the narrative
  reports the *felt* outcome, not the numbers.
- **Encryption at rest:** deferred (standing open question #4).

### 4.5 Deferred (not §4's business)

- Which R4T model runs the GM (standing open question #2).
- The narrative voice/prose style (Neil's eye, §5).
- Multiplayer actor claims (Phase 4) — the GM's arbiter role scales
  there; the MVP contract here is single-player.

## 5. Renderer spec (LOCKED 2026-09-26)

Text-first renderer for the Phase 2 MVP; Phase 3's three-part image composite
(scene / map / selfie) layers on top without changing this contract. Neil's
design notes say "images over text (Qwen prose is banal)" and "renderer could
start as text" — so: the text email always carries the *complete* turn (all
game facts, never image-only), images are additive. Prose *style* is not
specified here — it is Neil's eye (review checkpoint: turn-1 intro draft,
session #5, still open).

### 5.1 Body format
- **multipart/alternative: text/plain + text/html** (Neil's 2026-09-27
  directive, overriding the earlier text/plain-only rule). The plain
  part always carries the complete message; the HTML twin is the rich
  reading layer and must never add facts. One outbound email per turn
  (§2.5.4), images included.
- **2026-09-27 (Neil): no open prompt.** Turn emails no longer end with
  a question ("What do you do?" is gone): the email ends after the
  world blocks, and a silent player is folded into the next turn's
  catch-up lead. Death/game-end still ends with the explicit closer
  (§5.3).
- Monospace blocks (indented or fenced) render fine in plain text clients;
  the map block (§5.4) assumes monospace.

### 5.2 Turn email layout (fixed block order)
```
[subject] [ATFL <8hex>] Above the Fog Line      (set once at game start; later
                                                 turns reply in-thread so the
                                                 subject stays constant)

Day {N} · {HH:MM} · {time-of-day word}            (canonical game-clock line,
                                                 from time_of_day(t); e.g.
                                                 "Day 1 · 07:00 · morning")

<catch-up lead, one line, mandatory>             (§1.3.3 — auditable against
                                                 the mutations ledger; omitted
                                                 only on turn 1, nothing to
                                                 catch up on)

<narrative: the GM's prose for this turn>        (target ≤400 words; the four
                                                 turn-1 facts, beat work,
                                                 parsed player intent, etc.)

--- Known places ---                              (map-as-text block, §5.4;
<monospace map>                                   present from Phase 3's
                                                  image composite onward as the
                                                  client-proof fallback;
                                                  MVP may ship the simple list
                                                  variant)

Carrying: <compact inventory line>                (e.g. "hands empty ·
                                                  backpack empty"; §3.2 slots)

[no open prompt — removed 2026-09-27 (Neil): the email ends here for
 alive games; death/game-end appends the closer line below]

---
Game code: <GUID>                                 (§1.1 — footer, always last)
Turn {N} · Day {d}, {HH:MM}
```
- A literal template with a filled turn-1 example lives at
  `prototype/turn_email_layout.txt`; the Python renderer in Phase 2 fills it
  verbatim. Example prose there is illustration only — not locked.
- **What the renderer receives:** the *filtered* world state (§4.4 — no
  `hidden_traits` key at all), the turn's narrative, and the catch-up
  line. It cannot leak what it never sees.
- **Secrecy gate (§2.5.5):** after rendering, substring-check the body against
  every value in the game's `hidden_traits` JSON (denylist); a hit fails the
  turn — the email is NOT sent (§2.6: retry once, then surface in the digest).

### 5.3 The other email types (system voice, never in-character)
GM fiction goes only in turn emails. Everything else is plain, honest system
text — no narration, no spoilers, no mechanics talk. All of these leave the
engine as atfl_outbound handoff envelopes to Murph (relay edition
2026-10-02); the engine sends nothing directly:
- **Standalone nudge** (fallback-only, ≤1/24h, §2.3): ≤120 words, structural
  policy per §2.4. Handed to Murph as a nudge envelope; Murph threads it
  into the game's thread. Mutates nothing.
- **Clarification** (ambiguous inbound, §1.1): the envelope carries
  `fresh_thread: true` (advisory — Murph's sender currently keeps one
  thread per game; restoring the fresh-thread behavior is a Murph-side
  extension). Subject `[ATFL] Couldn't match your game`. Body: "I got your
  message but couldn't match it to a game. Reply with the Game code from a
  previous email, or email murph@inkboxmail.com to start a new game."
  No fiction, no retry of the player's intent.
- **Death / game-end**: the final turn email follows the §5.2 layout (it is a
  turn), but the prompt is replaced by an explicit closer: "This was your
  last email. The game is over." No nudges follow (§2.4 — no dead-game
  nudges); a reply starts a new game.
- **Threading**: Murph owns the player-facing thread — one Inkbox thread per
  game (§1.1). The engine keeps no threading state; the old
  `thread_message_id` / `thread_refs` columns are retired.

### 5.4 Map-as-text block (placeholder → Phase 3)
- MVP variant: a simple discovered-places list under `--- Known places ---`,
  one line per place: `trailhead (where you are)`.
- Rule: only **discovered** places render in full. Visible-but-undiscovered
  places (§3.2) render as one-line hints with no detail
  (`fog-below — visible below, not yet visited`). Undiscovered + invisible
  places never render — the renderer never sees them (§5.2).
- Phase 3 replaces this block with the three-part image composite (scene,
  rough time/space map, selfie — time-of-day aware per §3.1); the text map
  stays as the fallback for clients that block images and as the audit
  surface. Same discovery rule applies to the image map.

### 5.5 Turn-length target
- Turn narrative target ≤400 words (a constraint for the GM, not a hard
  gate — the done criteria in §2.5 are the hard gates). Standalone nudges
  are hard-capped at 120 words (§2.4). Rationale: one email a day has to be
  *read*; short beats long when the image composite lands in Phase 3.

## 6. MVP "done" criteria (LOCKED 2026-09-26)

The Phase 2 MVP is *done* when all three hold: the build passes the
acceptance checklist (§6.2), Neil has played it over email (§6.3), and
the exit criteria are met (§6.4). Completing §6 completes Phase 1:
DESIGN.md is the Phase 1 output and is now locked (the intro prose and
plot pick remain Neil's open checkpoints, not design stubs).

### 6.1 Playtest entry criteria

Phase 2 must not call itself playable until it can actually run. Before
the Neil playtest starts, the build must have:

1. **A live game address.** The game's dedicated free Google account
   exists and is connected (standing open question #1), and the poller
   on the Oracle VM reads it every few minutes.
2. **A GM behind the pipeline.** Some R4T-roster model answers the
   step-2 yes/no adjudications and step-3 narratives (standing open
   question #2). A mock GM may prove the plumbing, but the playtest runs
   against the real one.
3. **A plot pick.** One §4.3 concept chosen by Neil (open question #3;
   defaults to "earth changing" after the 2026-09-26 digest if he says
   nothing). The scenario seed (§3.2) is loaded verbatim; `plot_concept`
   goes from NULL to the pick on turn 1.
4. **Server up.** The turn loop (§2.2) + poller + sender all run on the
   Oracle VM (`free-micro-1`) or a declared fallback, with the daily
   digest surfacing turn failures (§2.6).

### 6.2 Acceptance checklist

Each item below is verifiable by an end-to-end email run, not by code
review. The build passes when every box holds.

**Start**
- [ ] A plain email to the game address (e.g. "start") creates a new
      game: a UUID4 GUID, one `<GUID>.db` file seeded exactly per §3.2,
      and a turn-1 email sent back within minutes.
- [ ] The turn-1 email establishes the four §3.3.3 player-verifiable
      facts (fog ocean below / empty trail / half-full bottle cap off /
      red drop on cheek, not raining), names the two trail directions
      and the fog, ends with an open question, and passes the secrecy
      check (§2.5.5).

**Turn flow**
- [ ] A player reply runs exactly one turn: gather → yes/no mutations →
      narrative → advance clock → update, with all §2.5 done criteria
      (turns row, no silent mutations, clock advanced, one outbound
      email, secrecy check, catch-up lead when frames were missed).
- [ ] The reply content is honored: intent parsed from natural language
      (§4.1.7); denied claims get a `no` row with rationale + partial
      effect (§2.2.2).
- [ ] Two turns at the same game are serial; a reply arriving mid-turn
      queues into the next turn's gather — never dropped, never raced.
- [ ] A late reply (after its turn ran) is folded into the next turn
      with a `late reply to turn N` audit mark (§1.3.2).

**GUID threading and identity**
- [ ] Every outbound email carries `Game code: <GUID>` in the body
      footer and the `[ATFL <8hex>]` subject tag; replies thread as one
      Gmail conversation per game (§1.1, §5.3).
- [ ] An ambiguous inbound (no GUID, multiple active games for the
      sender) gets the clarification email (§5.3) — the wrong game is
      never mutated.
- [ ] Attachments are logged, never acted on (§2.2).

**State persistence**
- [ ] World state survives between turns and server restarts: the same
      `<GUID>.db` file, the mutations ledger showing every change with
      cause (§2.5.2), elapsed-time reconciliation firing on idle objects
      (the demo'd bottle-evaporates/drop-dries case).
- [ ] Failed turns (§2.6) retry once, then send nothing invented — the
      failure is logged and surfaced in the digest.

**Idle and death**
- [ ] ~24h with no player input runs an idle turn: conservative default,
      catch-up lead, open prompt (§2.3) — and it counts as the day's
      "still your move" touch.
- [ ] Player death sets `status='dead'` / `ended_at`; no further turns
      or nudges go out, ever (§2.4.5, §2.5.7). The final email carries
      the §5.3 closer.

**No fiction outside turns**
- [ ] Standalone nudges (§2.3) fire only as the fallback, ≤1/24h/game,
      ≤120 words, mutate nothing, reveal nothing hidden (§2.4).
- [ ] No system email is ever in-character (§5.3).

### 6.3 The Neil playtest

- Neil plays the MVP by email at playtest tempo (hourly emails, daylight
  hours per the design notes) for a day or more — enough turns to see
  the pipeline work: at least one parsed intent, one denied claim with a
  `no` row, one idle turn with catch-up lead, and the full §5.2 email
  layout landing in his inbox.
- Playtest goals are subjective, so they are Neil's reads, recorded
  after: does the turn pacing feel right? Is the narrative voice close
  to the turn-1 draft checkpoint (session #5)? Are beats arriving or is
  it wandering? These reads steer Phase 2 iteration, not new features.
- Every turn is captured server-side (per-turn stats: mutations count,
  GM latency, secrecy-check passes/fails, send latency) — the design
  notes' dogfooding hook. The MVP ships the stats rows; dashboards are
  later.

### 6.4 Exit criteria (MVP → Phase 3)

- [ ] All §6.2 boxes checked on a real end-to-end run.
- [ ] Neil's playtest done; his reads logged as checkpoints (not as
      blocking new features unless the mechanics are broken).
- [ ] The `<GUID>.db` + mutations ledger + turn emails are the archive
      of the playtest — inspectable, exportable, no special tooling.
- [ ] Known deferred items are still deferred: Phase 3 image pipeline
      (composite), Phase 4 actors/multiplayer, Phase 5 video — none
      sneaked into the MVP. (2026-10-10 note: the per-turn scene/map
      composite already ships via Pollinations — treated as playtest
      enrichment, not a Phase 3 pre-emption; Phase 3's scope,
      time-of-day-aware composite + selfie + text-map fallback polish,
      is unchanged. Neil's art feedback judges it, not this gate.)

### 6.5 Deferred (not §6's business)

- The plot pick and turn-1 intro prose (Neil's checkpoints; open
  questions #3 and the "Waiting on Neil's eye" item).
- The game's email identity (#1) and the GM model (#2) — entries in
  §6.1, decisions in Neil's hands.

## Decisions (2026-09-26)
- GUID+sender-address join; clarification email instead of guessing on
  ambiguous inbound (never mutate the wrong game).
- No hard turn deadline in single-player MVP; AI-driven conservative
  defaults for idle turns; late replies folded into the next turn, never
  dropped; catch-up line leads every turn email.
- UUID4 lowercase hex GUIDs; `[ATFL <8-hex>]` subject tag; footer
  `Game code: <GUID>` line; threading handled by the relay's per-game
  thread (the "Gmail threading headers" of the pre-relay design were
  superseded by the 2026-09-30 relay edition).
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
- GM hard rules: world-model supremacy (SQLite canon, narrative never the
  source of truth), mutations only through the turn pipeline, no invented
  answers (markers resolve only into the chosen plot concept's answers),
  one plot per game (never switched mid-game), secrecy every turn, neutral
  arbiter (danger is real), GM as input proxy, player agency over beats.
- Beat pacing: ~30 turns / ~1 month (playtest: hourly/daylight); beats
  Establish / First doubt / Escalation / Point of no return / Resolution
  with soft windows; nudge/idle defaults don't mutate.
- Plot-concept roster (Neil's five from the design notes) is LOCKED as
  the concept list — but the PICK is Neil's call, still open. Murph's
  lean: "the fog is wrong itself" (earth changing) — closest to Neil's
  Silent Hill ocean-of-fog origin. Hidden stats gate step-2 yes/no
  claims; hidden changes go through the same mutation ledger.
- Renderer (multipart/alternative since 2026-09-27: text/plain body +
  rich-HTML twin, per Neil): fixed §5.2 block order
  (clock line → catch-up lead → narrative → composite slot → map block →
  inventory line → GUID footer; no open prompt since 2026-09-27); the text
  email always carries the complete turn, Phase 3 images render inline in
  the HTML and as viewable parts. System emails
  (nudge/clarification/death) are never in-character. Text map block
  doubles as the Phase 3 image fallback and the audit surface.
- MVP "done" = acceptance checklist (start → turn flow → GUID threading
  → state persistence → idle/death rules → no fiction outside turns) +
  a Neil playtest at hourly/daylight tempo + playtest entry criteria
  (live game address, real GM, plot pick, server up). Completing §6
  completes Phase 1: DESIGN.md is locked. The plot pick and the turn-1
  intro prose are Neil's open checkpoints, not design stubs — the MVP
  build proceeds on the "earth changing" default if he says nothing.

## Open questions for Neil
- (standing) Game's free Google account / spare address for the MVP turn
  loop — blocks the playtest entry criteria (§6.1), not the Phase 2
  build itself.
- (standing) Which R4T model runs the GM — same: needed for the playtest,
  not for prototyping the loop.
- **(NEW) Which plot concept for the fixed MVP scenario?** Roster:
  aliens / government project / you're dead / a spell / the earth
  changing. Murph's lean is "earth changing" but this is Neil's mystery
  to call — the MVP defaults to the lean after the 2026-09-26 digest if
  he says nothing.
- (standing) Whether `hidden_traits` should be encrypted at rest (later).
