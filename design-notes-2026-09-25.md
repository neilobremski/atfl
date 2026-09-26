# Above the Fog Line — design notes (from Neil's voice memos, Sat 2026-09-19)

Transcribed by Gropple, relayed via A8S on Fri 2026-09-25 ~20:49 PDT.
Recorded while hiking out of the Alpine Lakes Wilderness (2:34 PM and 4:25 PM PT).
Raw timestamped transcripts available from Gropple on request.

## Origin
- Name came from standing above an ocean of fog, eerie Silent Hill feel.
- First story thought: you're trying to get above the fog line because something is wrong with everything below it.
- A red drop on his cheek + a water bottle on an empty trail = "the perfect start to a mystery."

## Core format
- Email-based, non-realtime exploration game. Email an address to start.
- Each reply carries a GUID code in the body; game files live under GUID + sender address (semi-secure).
- Email sidesteps slow LLM real-time play: the game responds on a cadence.
- Fixed-length turns (e.g. one hour of game time). Real play maybe one email a day, a game lasting about a month.
- Playtest by speeding tempo: hourly emails, daylight hours, zombies at night.
- Open: decide email frequency and what it means for the game.
- Games are ephemeral: death or game end stops the emails.
- Narrative-agnostic: mystery on a mountain, but outer space would work too.

## Game master
- GM AI follows hard rules, D&D dungeon-master style. Runs as an R4T roster.
- Theme is mystery. GM picks one plot concept at start (aliens, a government project, you're dead, a spell, the earth changing) and nudges the player along the plot in slow beats.
- Players can change settings in natural language, but hard physics apply (no flying).
- GM keeps hidden player stats.
- GM is the proxy and filters raw player input. Players can't tell whether another actor is human or AI (same as Savage Lands).

## World model
- Spatial + temporal mechanics in SQLite that travel with the player.
- Places, actors, objects are directories: files for physical state, GM-only hidden traits, generated assets (images, sounds) so things stay consistent across turns.
- Minimal attribute list per thing: GM-secret attributes + obvious ones (hunger, fatigue, bathroom).
- Actors act every turn. Objects are static but reconcile elapsed time when revisited (the bottle has dried up, tipped over, or is leaking).
- Fixed-slot inventory; hands and backpack shown in the image.

## Per-turn flow (agentic)
1. Gather the place, actors, objects involved.
2. Decide mutations through yes/no questions (a player too weak to fell a tree only chips it).
3. Combine into one narrative.
4. Set the time length of the turn; advance time.
5. Update the place and everything touched.
- Every turn captured server-side, so it can be dogfooded as a Pay-i/Ascerta instance with players as users and per-turn stats.

## Multiplayer
- Everyone (NPC and PC) is an "actor." Players can inhabit existing actors or creatures.
- Idle players' actors get AI-driven; a daily email still goes out with the frames they missed. Replying to an expired turn gets an update, and the reply is still taken into account.
- Later: different actors on different models as R4T roster agents (animals, bosses). Actors submit claims; GM honors or denies them, resolves interactions, moves pieces. R4T would need to be turn-based — good R4T test.

## Output / rendering
- Two parts: a renderer and a turn-based game server.
- Images over text (Qwen prose is banal).
- v1 output: three-part composite image — the scene, a rough map showing time and space, maybe a selfie showing the player's state. Time of day informs the images.
- Renderer could start as text. Experiment: emit SVG and animate with Playwright + CSS ("Picasso").
- Later: turn image + narration into a short scene. Looping 5–8 s LTX-Video clip, AudioLDM sound effects crossing the loop boundary, Kokoro narrator (EGA-animation vibe), ~60 s clip stitched with FFmpeg. Player replies in text, comes back for more video. Stay on LTX-Video 1 so it runs overnight on his work workstation, in stages (LTX and Qwen can't run together).

## Lineage (why structure matters)
- Allegro-era "Infinite Worlds" tile-map generator he almost helped with: "a quarter century later, an infinite world system based on pretty much all human knowledge... it's got to be structured though."
- Chris Cowherd's 3.5-turbo-era LLM story game: semi-real-time, fun briefly, but bad memory and massive hallucination. Hard rules + on-disk state are the answer.

---

## Other creative ideas from the same hike

### Daily chained AI short film (memo 1, ~45:30–50:40)
- Background: Neil made a short called "You're Absolutely Correct" with Gemini Web + Veo — about running out of AI quota and dropping to lesser models (sports car → bike → tricycle → space hopper); he cries "I can't code," robots pat him and say "You are absolutely correct."
- Noticed: Gemini conversation let him extend the video segment by segment (10 s, 20, 30, 40; 40 s cap). He gets ~3 generations/day and doesn't use them daily — quota goes to waste.
- Idea: write a story with destination + beats, divide into ~30 s chunks then 10 s sub-beats. Every night, drive Gemini Web to make the next segment, feeding a screen capture of the last video + what comes next. Chain and upload to YouTube. A week ≈ 3.5 minutes.
- If quota out or service busy, skip that day. If a segment over/undershoots, re-plan the timeline so every beat lands ("relies on the video generator's personality").
- Generator-agnostic: works with LTX-Video too (different driver — Veo's 10 s→40 s chunking is its own shape). Subtitles/title cards later.
- Tone: "let it hallucinate as much as it wants. It's supposed to be interesting and not supposed to be cohesive." First target: his nuclear zombies film (started last year).

### Barney as editor-in-chief (memo 1, ~12:50–14:40)
- Barney runs the newsletter on Claude. Concern: Opus output increasingly guardrailed and watermarked (EU pressure over AI-generated content).
- Idea: Barney becomes editor-in-chief. A writer on a local model (Qwen) does the actual writing, tone tuned through the writer's prompts. Barney picks out errors, restructures, suggests, criticizes.
- General pattern: heavy-hitter model = reviewer/editor ("the chief," "the lieutenant"); cheaper/local model produces.
