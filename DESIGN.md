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

## 2. Turn structure (stub — next session)
Per-turn flow from the design notes (gather → yes/no mutations → narrative
→ advance time → update), mapped to schema operations in
`research/phase0-world-state-schema.md`. Needs: fixed game-clock turn
length, daily-nudge wording policy, what "done" means per turn.

## 3. World model (stub)
Schema locked in Phase 0 (`research/phase0-world-state-schema.md`).
Needs: the fixed mystery scenario's initial state — places, actors,
objects seeded for turn 1.

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

## Open questions for Neil
None new — protocol questions are answered by the design notes; the
standing ones (game email account, GM model) stand.
