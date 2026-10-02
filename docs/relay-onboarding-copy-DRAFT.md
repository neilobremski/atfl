# Relay onboarding copy — DRAFT (2026-10-01)

**Status:** draft for Neil's eye. Nothing here ships to a player until he
reads it. This is the todo from `docs/mail-relay-design.md` ("For Neil's
eye": how a player learns the address, and what their first email should
say — wording and tone are subjective).

## Aim (what this draft is trying to do)

Two small pieces of player-facing prose for the relay world, where the only
address a player ever sees is **murph@inkboxmail.com** and the signup email
is already turn 1's input (DESIGN.md §1.2/§2: "the signup email IS turn 1's
player_input" — e.g. "start", or the player's first words).

- **Piece 1, the invitation:** the text Neil (or whoever invites) forwards
  to a prospective player. This is how the address travels — friend to
  friend, not a public signup page.
- **Piece 2, the first-email guidance:** what the player's first email
  should say, and the minimum mechanics they need so their first turn
  doesn't bounce off confusion.

Tone target: a found invitation — quiet, curious, slightly uncanny. Not a
software EULA, not a gamer manual. Spoiler-free: it names nothing about
the plot, the cheek mark, or what's under the fog.

Assumptions baked in (flag if wrong): invites go to friends of Neil for a
personal beta; the game is presented honestly as a slow email game, not a
surprise ARG.

---

## Piece 1 — The invitation (forward-able)

> **Above the Fog Line**
>
> A slow mystery, played entirely by email. You email an address; the
> mountain emails you back. About one email a day, for about a month.
> Each turn is an hour of game time. There are no accounts, no apps, no
> dice — you just write what you do, the way you'd tell a friend.
>
> The world keeps moving whether you reply or not. If you go quiet,
> you'll get a catch-up. If you die, the story ends.
>
> To begin, email **murph@inkboxmail.com** with "Above the Fog Line"
> in the subject line, and write "start" — or just write your first
> move. Morning, trailhead, fog below you. You'll know what to do.

## Piece 2 — First-email guidance (for the player who opens a blank email)

> Subject: **Above the Fog Line** (this is how your email finds the game)
>
> Body: anything. "start" is enough. Or skip the preamble and write your
> first action in plain words — *I look around*, *I check my pack*, *I
> head down the trail*. The game reads plain English, not commands.
>
> After that, just hit reply. Your game has a code (you'll see it at the
> bottom of every email) — keep the thread and the game keeps you.

---

## Mechanics this encodes (so Neil can check it against the design)

- Subject must contain "above the fog line" — matches the Murph-side
  relay filter (first-contact path), so the forward to the engine works.
- Body freeform — matches "signup email IS turn 1's player_input".
- ~1 email/day, ~a month, 1 turn = 1 game-hour — DESIGN.md §1.2 verbatim.
- "The world keeps moving / catch-up / death ends the story" — §1.3
  expired-turn rules, compressed to three sentences.
- "Just hit reply / keep the thread / game code" — §1.1 GUID + threading,
  without teaching anyone what a GUID is.

## Specific read wanted from Neil

1. **Tone** — too coy? too plain? Does "the mountain emails you back"
   land, or is it trying too hard?
2. **Death up front** — "If you die, the story ends" is in the invite.
   State it, or let them discover it?
3. **Pacing promise** — "about one email a day, for about a month." Right
   promise, or does it undersell/oversell the commitment?
4. **Address framing** — the invite says "email Murph at
   murph@inkboxmail.com". Murph is the exchange layer; is naming him as
   the mailbox fine, or should the address travel unexplained?
5. **First-email bar** — "just write 'start'" vs. nudging an in-character
   first move. Right level of hand-holding, or too much?
