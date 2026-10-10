# Turn-image art direction rethink (2026-10-10)

## The verdict that forced this

Neil, 2026-10-10: "the pictures seem to show very broad landscapes and the
most recent one didn't reflect the narrative text nor the first-person feel.
The image generartor may not be sufficient."

He is right about the mismatch, and the mismatch is structural, not a tuning
problem. This note records the mechanism, the new direction, and the test.

Standing rule from the same conversation: art stays OUT of the digest's
"needs your eye" batch until the images actually serve the narrative. Nothing
here goes to Neil until the mechanical gate (§5) passes.

## Why v1 produces broad landscapes (mechanism, not taste)

`server/images.py::scene_prompt` (v1):

> "Cinematic still, quiet eerie calm, photographic: {place name}, {time} light.
> Visible now: {place description + visible objects}. No people other than
> none; no text, no captions, no watermark."

Three structural defects:

1. **"Cinematic still" is an establishing-shot instruction.** The generator's
   strongest prior for that phrase is a wide landscape. First-person feel was
   never requested, so it never appears.
2. **The prompt describes the *place*, not the *turn*.** The narrative is the
   player's moment-to-moment experience (boots on gravel, the slope dropping,
   fog where the light ends); the image is the location's postcard. The two
   are built from different sources, so they diverge by construction. The
   narrative is available at prompt-build time and was never used.
3. **The first-person element was built but never shipped.** `selfie_prompt`
   ("First-person selfie...") exists in `images.py` but the live composite is
   scene+map only. The one POV panel sat unused while the landscape panel
   carried the whole visual load.

## The rethink: what the image is for in a first-person fog game

- The scene panel becomes a **POV shot: exactly what the player's eyes see
  right now** — tight, embodied, a few meters of visibility at most. The fog
  is a compositional gift: in-fiction it forbids broad landscapes, so the
  prompt should use it as a depth limiter rather than fighting it.
- The prompt is keyed to **this turn's narrative beats, not the place
  description**. The narrative is the source of concrete visual nouns. (The
  narrative is already player-visible text, so feeding it to the prompt
  builder leaks nothing; hidden_traits still never leaves the DB layer.)
- The **selfie panel gets wired in** as the third panel (the original
  scene/map/selfie design). It is the explicit first-person anchor. Risk:
  cross-turn character consistency on Pollinations is unverified (their
  `kontext` image-to-image model's contract is untested here); test cheaply
  before promising it.
- The **map panel stays as-is**: text-keyed SVG, working, movement verified
  in play (turn 5). It is the spatial anchor; the scene panel is the
  experiential one. They do different jobs now instead of both gesturing at
  "the place."

## v2 prompt spec (scene panel)

- Framing prefix: "First-person point of view, exactly what the hiker's eyes
  see right now, photographic, quiet eerie calm: ..."
- Body: 2–4 concrete visual beats taken from THIS turn's narrative text
  (nouns the prose actually names: boots, gravel, the slope, the fog wall —
  never invented elements, never hidden state).
- Fog as depth cue: "visibility only a few meters, fog swallowing distance"
  — matches the fiction and blocks the landscape prior.
- Explicit negative: "tight embodied framing, not a landscape, not a wide
  establishing shot, not a postcard view."
- Kept from v1: time-of-day light, no text/captions/watermark.
- The hiker's own body at the frame edge (boots/legs/hands) is the standard
  POV cue; "no people" in v1 actively fought first-person feel and is dropped
  in favor of "no other people; the hiker's own limbs may appear at the frame
  edge."

## The generator-sufficiency test (Neil's hypothesis)

One v2 image for a real past turn (turn 5, "Descent", 2026-10-10 — narrative
on file in the game DB). Mechanical gate, no taste judgment:

1. **Narrative coverage**: does the image contain the concrete elements the
   turn's narrative names (boots/gravel, descending slope, fog wall where the
   light ends)?
2. **POV read**: does it read as a first-person view rather than a landscape?
3. **No invention**: nothing in the frame the narrative doesn't support.

If the v2 image fails the gate, the generator likely can't do the job and the
honest options are text-only turns (Option E, already the designed degrade
path) or a funded/quota-serious provider (his call — genuine fork, batched).
If it passes, the next step is wiring v2 prompts + the selfie panel into the
live pipeline and re-running the gate on 2–3 more past turns before anything
reaches a turn email.

## What this session did

- Wrote this note (the concept + spec).
- Generated one v2 test image for turn 5's narrative (`docs/` test only,
  never attached to a turn email). Verdict recorded below.

## Test record

- v1 prompt used for turn 5 (reconstructed): "Cinematic still, quiet eerie
  calm, photographic: trail-down, morning light. Visible now: ..." →
  broad-landscape establishing shot (the failure Neil named).
- v2 prompt (this session): first-person POV, boots on wet gravel lower
  frame, steep straight descent with cut steps dropping ahead, wall of white
  fog standing in the trail where the morning light ends, visibility a few
  meters, pale diffused morning light; tight embodied framing, not a
  landscape; hiker's own boots/legs may appear at frame edge; no text, no
  captions, no watermark.
- Image: `docs/art-v2-test-turn5-20261010.jpg` (test only).
- v2b prompt (second formulation — concrete camera instead of abstract POV:
  "POV photograph taken by a hiker holding the camera at chest height and
  looking down: the hiker's own hiking boots large in the bottom third of the
  frame... No other people."): `docs/art-v2b-test-turn5-20261010.jpg`.
- Mechanical gate verdict: **FAIL, twice, in instructive ways.**
  - v2: "first-person point of view" + "boots at the bottom edge" + "not a
    landscape" → the generator returned a landscape with no hiker at all.
    The POV and boot instructions were dropped; the landscape prior won.
  - v2b: "POV photograph... boots large in the bottom third... No other
    people" → the generator rendered a THIRD-PERSON shot of a hiker from
    behind walking down a stepped forest trail. It did the opposite of the
    POV instruction: the hiker became the subject instead of the camera.
  - Both images also carry a `pollinations.ai` watermark bottom-right
    despite `nologo=true` and "no watermark" in the prompt (research caveat
    confirmed live).
  - Two distinct prompt formulations, same class of failure: the provider
    (Pollinations, flux) does not follow POV/embodiment instructions. This
    supports — not refutes — Neil's "the image generator may not be
    sufficient" hypothesis. A third formulation would be a tuning spiral,
    which he explicitly said this isn't.

## Decision (2026-10-10, this session)

- **The generated scene panel is paused until a provider/prompt exists that
  serves the narrative.** Shipping v1-style broad landscapes hours after
  Neil named them as the problem would be the "are you reading my replies"
  failure mode, live. The v2 test proves prompting can't fix it on this
  provider.
- **The map panel stays.** It is text-keyed SVG from DB truth, accurate
  (movement verified in play, turn 5), and Neil never objected to it. The
  text email already carries the map-as-text (§5.4); the visual map remains
  the turn email's image anchor.
- Next step (15:07 session): add a map-only composite path
  (`build_turn_composite_maponly`: render_map_svg at larger size → JPEG, no
  scene generation), behind a config flag, with selftest, then deploy to
  /srv/atfl/atfl before the 18:52 PDT idle-turn gate. Deployed env lives at
  /etc/atfl/atfl.env (ATFL_IMAGES/ATFL_COMPOSITE).
- Honest alternatives if a scene image is ever wanted again: Cloudflare
  Workers AI (needs his account) or funded HF (spending) — genuine forks,
  batched for a future digest when art re-enters the conversation. NOT
  tonight: art is out of the batch per his instruction.
