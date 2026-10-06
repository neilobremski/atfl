# Playtest bug handling

How bugs get reported, triaged, and fixed while Neil (and later other
players) are playing Above the Fog Line. Written 2026-10-06 to answer
Neil's question: "How are we going to handle bugs as I play the game?"

## The player protocol (what Neil does)

1. **Report by replying to any game email.** Put `BUG:` at the start of
   the subject or the first line of the body, then describe what looked
   wrong in plain English — what you saw, what you expected. Include the
   turn number if it's handy (it's in the email's dateline). You don't
   need to diagnose anything; "the map showed a place I've never been"
   is a complete report.
2. **While a bug is open, the game pauses for you.** No new turns are
   generated, the game clock does not advance against you, and you get
   no expiry nudges. A bug report is never counted as a turn and never
   costs you game time. This is the fairness guarantee — reporting a bug
   can only help your game, never hurt it.
3. **You get a resolution email.** When the bug is closed you hear: what
   it was, what changed, and — if any game state was repaired — exactly
   what was repaired and how many turns (if any) were replayed. Then the
   game resumes from the corrected state.

## The operator protocol (what Murph does)

**Routing.** A `BUG:`-flagged message is a Murph handoff, not player
input: the relay routes it to Murph and never feeds it to the engine as
a move. (TO-BUILD: the relay needs a `BUG:` keyword rule; currently any
reply would reach the engine as player_input. Until it lands, Murph
screens inbound manually during playtest — acceptable for one player.)

**Triage (within a day — this is a slow game, the digest cycle is the
SLA).** Three buckets:
- *Game-breaking:* wrong world state, lost progress, stuck with no
  legal move, turn email never arrived. Fix first, repair state, resume.
- *Cosmetic:* typo, wrong image, map label off, formatting glitch.
  Logged, fixed in the next session batch, game continues.
- *Engine question:* "is this a bug or the game being mysterious?" —
  see the mystery rule below.

**Reproduction.** Every turn's sent body is archived by the relay and
the game DB is snapshotted per turn. Bugs are reproduced from the
snapshot, never by poking the live game. The live game is touched only
by the fix itself.

**Fix paths.**
- Narrative/content fix (no state change): fix, note it in the
  resolution email, resume.
- State repair: the fix is a logged compensation entry in the game DB
  (what was wrong, what was set, which turns were affected) — never a
  silent edit. The resolution email states the repair verbatim.
- Engine bug: hotfix through the normal deploy path (commit, push,
  deploy-parity check, playtest_watch pre-flight), regression test
  added where one fits, then resume.

**The mystery rule (most important).** This is a mystery game; some
things the player sees are *supposed* to be weird. A bug report that
touches hidden machinery is never answered by explaining the machinery
— the resolution email says what was fixed or "working as intended,
carry on" without leaking game secrets. When unsure whether an oddity
is a bug or intentional, the default is: ask the player for one more
detail, check the DB snapshot, and only then decide. Never silently
"fix" a mystery.

**What the player never has to do.** No logs, no screenshots required
(though they're welcome), no reproducing on demand, no waiting on a
broken game — the pause rule means a reported bug freezes his game in a
safe state until it's resolved.

## Standing notes

- One player (Neil) for the playtest: manual screening of inbound until
  the `BUG:` relay rule ships. Multiplayer later gets the same protocol
  per game (a bug pauses only the reporter's game, not everyone's).
- Bug reports and their resolutions are logged in the goal progress log
  under a `## Playtest bugs` section — the running record of what broke
  and what it taught us.
