# Phase 3 — image pipeline research (2026-09-27)

DESIGN.md §5.2 fixes the turn-email contract: text/plain always carries the
*complete* turn; the three-part composite (scene, map, selfie) is additive and
attaches as plain MIME images, one outbound email per turn. Time-of-day is a
first-class input ("time of day informs the images", design notes). This memo
covers the provider survey, the recommended architecture, and what got
prototyped this session.

## What each panel is

- **Scene**: generated image. The current location as it looks *right now*
  (game-clock time-of-day, weather, visible state changes from this turn's
  mutations). Prompt built from filtered world state (never `hidden_traits`).
- **Map**: deterministic code-drawn panel. Places, edges, player position —
  facts the game DB holds exactly. A generator would invent geography; code
  can't. Rendered SVG→raster (or PIL directly), rough sketch style.
- **Selfie**: generated image of the player's actor as they'd look *right
  now* (injuries, carried items, time-of-day light). Needs **character
  consistency** across turns — this is the hard requirement, and it decides
  the provider.

Composite: the three panels stitched into one image (Pillow), attached to
the turn email. Sizes: 1024px panels, final JPEG ~200–400KB total — far
under Gmail's 25MB/message ceiling. Map-as-text block (§5.4) stays in the
body as the client-proof fallback.

## Provider survey (Sept 2026)

Pace is ~1–2 turns/player/day, 2 generated images per turn. Cost is a
non-issue at any provider — the differentiators are **reference-image
consistency** (selfie continuity) and **account friction** (who holds keys).

| Provider / model | Cost / 1024px image | Reference images | Notes |
|---|---|---|---|
| OpenAI gpt-image-2.5-flare | ~$0.013 (medium) | yes (image input + multi-turn edit) | Great instruction-following, 4K, C2PA+SynthID. Separate OpenAI account + billing — new credential to provision. |
| OpenAI gpt-image-2.5-sunburst | ~$0.053 (high) | yes | Precision sibling of flare. Same account friction. |
| Google gemini-3.1-flash-image ("Nano Banana 2") | ~$0.067 (1K) | up to 14, incl. character refs | GA, 4K, strongest character-consistency story. Google AI Studio key — can ride the same Google account as the game's Gmail identity (one provisioning conversation). |
| Google gemini-3-pro-image ("Nano Banana Pro") | ~$0.134 (1K) | up to 14 | Premium quality; overkill for daily turn panels. |
| Google Imagen 4 Fast/Standard | $0.02 / $0.04 | no | Best text legibility, cheapest — but no reference support, so selfies drift. Good *scene*-only engine if we ever split providers. |
| Replicate (FLUX/SDXL + IP-adapter) | ~$0.01–0.05 | via consistency models | Flexible but model-churny; another account + billing. |

Sources: OpenAI gpt-image-2.5 pricing breakdowns (tech-insider.org, Sep 2026);
Gemini image pricing + model-ID table (blog.laozhang.ai; github.com/petarkalinovski/agent_gm
sprite_generation_models.md; github.com/hoodini/ai-agents-skills
14-ai-image-models-2026.md). Caveat: model IDs die fast in this space
(gemini-2.5-flash-image shuts down Oct 2, 2026) — pin and revisit per release.

## Recommendation

**Primary: gemini-3.1-flash-image** (or current GA equivalent) for both
generated panels.
- Character-reference support is the feature the selfie panel lives or dies
  on: keep a canonical reference image per game (first selfie, or a
  player-approved portrait) in the games dir and pass it as reference every
  turn. Scene panel needs no reference but gets the same API.
- One Google account covers both the game's Gmail identity (OQ#1) and the
  image API key — a single provisioning ask to Neil instead of two.
- ~$0.13–0.20/day/player: well inside "API keys as phases demand" without a
  second thought.

**Fallback: OpenAI gpt-image-2.5-flare** if Neil already has OpenAI billing
he'd rather use (multi-turn editing is a nice second way to keep the selfie
consistent — edit the *previous* selfie instead of regenerating from
reference).

**Map: never generated.** Deterministic PIL renderer (see below) — stdlib +
Pillow, no key, no latency, no drift.

Explicitly NOT: self-hosted generation on free-micro-1 (1 OCPU / 1 GB —
a diffusion model cannot run there; the workstation pipeline is Phase 5's
domain).

## Architecture (image provider layer)

Mirror the Phase 2 pattern: a small `ImageProvider` protocol the turn loop
calls, with a stub for offline dev.

```python
class ImageProvider(Protocol):
    def generate_scene(self, prompt: str, *, reference: bytes | None,
                       size: int = 1024) -> bytes: ...
    def generate_selfie(self, prompt: str, *, character_ref: bytes,
                        size: int = 1024) -> bytes: ...
```

- `server/image_providers/stub.py` — returns fixed placeholder bytes; tests
  and offline runs never touch a key. Loud refusal if real mode lacks a key,
  same as config.py's ATFL_GAME_ADDRESS rule.
- `server/image_providers/gemini.py` — REST call with `GOOGLE_API_KEY`
  (or `ATFL_IMAGE_API_KEY`), timeout + one retry, failure → turn still sends
  (text is the complete turn; missing images are logged, not fatal — DESIGN
  §2.6 "never invented fiction on failure" extends to images).
- Prompt builder gets filtered world state + time-of-day word; prompts are
  stored in the turn's audit trail so a weird image is reproducible.

Time-of-day handling: the game-clock word (morning/midday/dusk/night from
`time_of_day(t)`) goes into the prompt *and* tints the map panel palette —
cheap, deterministic, and visibly correct.

## What this session built

- `server/map_panel.py` — the deterministic map renderer. Contract:
  `render_map(places, edges, player_slug, time_of_day, size=1024) -> PIL.Image`.
  Places: `{slug: name}` (only *discovered* places are ever passed — the
  renderer, like the text renderer, only receives filtered state, so the map
  can't leak hidden geography). Edges: `[(a, b)]` slug pairs; coordinates
  come from a stable per-slug hash (no layout state to persist). Style: hand-
  drawn sketch — jittered lines (fixed seed → deterministic bytes), parchment
  base with a time-of-day tint band, player position marked with a red "YOU"
  pin. Font: tries DejaVu Serif, falls back to PIL's built-in default (the
  fallback path is what the unit tests exercise, so free-micro-1 needs no
  fonts package).
- `prototype/map_panel_demo.py` — green: renders, labels present,
  deterministic bytes across runs, player pin marked, time-of-day changes
  tint, undiscovered place never rendered (secrecy shape), two-component
  graph lays out without overlap.

## Open items (for later sessions, none Neil-blocking)

- Wire the stub `ImageProvider` into the turn loop behind a config flag
  (default off); real provider once Neil provisions the key.
- Composite stitching (3×1024 panels → one JPEG) — trivial Pillow, do it
  when the first generated panel exists.
- Reference-portrait policy: first selfie becomes the canonical ref, or
  Neil approves a portrait up front? (candidate for "Waiting on Neil's eye"
  — it's a taste call about the player character's look).
- The SVG/"Picasso" experiment (design notes) stays deferred — generator
  quality + reference refs made it unnecessary for v1; revisit in Phase 5.
