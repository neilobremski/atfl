# Phase 3 — no-cost image generation: HuggingFace inference + Colab automation

Written 2026-09-29 (session #42). Answers Neil's Sep-29 directive:
*"No costs up front. The AI Studio quickly begins costing (in my
experience). Let's explore HuggingFace, running models there (we will use
very little for the daily art generation) and automating Google Colab
(https://colab.research.google.com/) via w2-browser or some such; it
doesn't need to be fast but this way you can run local models in the
cloud and there is a small daily quota so we won't need to worry about
cost."*

Survey method: three targeted web searches (quota-light), Sept 2026.
The AI Studio / OpenAI key direction (open question #6 as phrased) is
retired by this directive — no paid image API, no cost up front, ever.

## Option A — HuggingFace Inference (RECOMMENDED primary)

Free serverless inference, now routed through HF Inference Providers.
What the survey established:

- **Cost:** $0 forever, no card. Needs a free HuggingFace account token
  with the `inference` scope (token from huggingface.co/settings/tokens —
  a *new* HF account, but accounts are free and self-serve; Neil just
  creates one and hands over the token, same pattern as the game's
  Gmail token question).
- **REST:** `POST https://router.huggingface.co/hf-inference/models/<model-id>`
  with `Authorization: Bearer <HF_TOKEN>` and JSON body
  `{"inputs": "<prompt>"}` — image bytes come back in the response body.
  Fallback base `https://api-inference.huggingface.co` if the router is
  unreachable from a host (documented in community integrations).
- **Limits:** ~300 requests/hour, best-effort, rate-limited free tier.
  Our load: 2 generated panels (scene + selfie) per turn, ~1 turn/day/
  player = a rounding error against that ceiling.
- **Scene panel model:** `black-forest-labs/FLUX.1-schnell` — fast,
  free, high quality, 1024px native. The community-standard free pick.
- **Selfie panel (character reference):** `black-forest-labs/FLUX.1-Kontext-dev`
  — open-weight 12B instruction-based editing model: takes a reference
  image + edit instruction and keeps subject identity consistent across
  edits, no finetuning. This is the make-or-break feature for the selfie
  panel (phase3-image-pipeline.md).
- **Cold starts:** serverless models load on demand — a first call may
  return 503 `{"error": "Model <id> is currently loading"}`. Our
  existing failure policy absorbs this: one retry, then degrade to
  text-only (never fails the turn; images are additive per §2.6).

Caveats, all honest:
1. **Model roster churn:** same caveat as session #17 — model IDs in this
   space die fast. The adapter pins IDs as constructor defaults so a swap
   is a config-layer change; the demo suite pins no model behavior.
2. **License gating:** FLUX.1-dev weights are under the FLUX
   Non-Commercial License (fine for this hobby project, noted for the
   record); Kontext-dev's HF repo requires accepting its license
   conditions on the HF account (contact-info share) — a one-time action
   on whoever's account holds the token. If that blocks, the fallback is
   generic image-to-image via FLUX.1-schnell/dev or Option B.
3. **Selfie endpoint shape:** the exact provider-side JSON shape for
   Kontext-dev on the free inference tier is the one live-check item.
   The adapter pins our chosen shape (`{"inputs": prompt, "image":
   <base64 ref>}`) hermetically in code + demo; the first live call
   against the real endpoint (once the token exists) verifies or corrects
   it — same posture as the Gemini adapter's pre-live contract.

## Option B — Google Colab automation via w2-browser (documented fallback)

Neil's suggested path: run local open models in Colab's cloud, no spend.
- **Free tier reality:** free GPU/TPU, 12h max sessions, Google login
  required, no card; daily quota exists but is unpublished. Colab
  disconnects idle sessions and runtimes reset — no persistence, so
  outputs must be saved to Drive or downloaded each run.
- **Design:** a notebook (diffusers: FLUX.1-schnell for scene,
  FLUX.1-Kontext-dev via FluxKontextPipeline for the selfie ref pass)
  that takes prompt + optional ref JPEG and writes the two panels;
  w2-browser (the live-browser seat) automates: open notebook → run all
  → download the panel PNGs → hand to the game's asset pipeline.
- **Fit:** for our ~2 images/day the free quota is ample and "it doesn't
  need to be fast" matches Colab's best-effort GPUs. Useful as the
  fallback when a model isn't on the free inference roster (e.g.
  Kontext-dev gating) and as an independent check on the HF renders.
- **Not the primary:** browser automation is brittle vs. a REST call,
  and it needs Neil's Google sign-in in w2-browser (he owns sign-ins —
  same boundary as the Oracle console). The HF REST path runs unattended
  from free-micro-1; Colab stays the operator-run fallback.

## Decision (2026-09-29)

1. **Primary: HuggingFace Inference REST adapter** — stdlib-only
   (urllib, no new deploy deps), bearer header not URL, 60s timeout +
   one retry on transport/503-loading, 429 → immediate degrade (the
   established failure policy). `ATFL_IMAGES=hf`, token via
   `ATFL_HF_TOKEN` (startup refuses half-wired, same as the game-address
   gate). Implemented this session as `HFImageProvider` with the same
   hermetic `request_fn` injection convention as the Gemini adapter.
2. **Colab via w2-browser: documented fallback**, notebook design above;
   explored live only if the HF roster blocks us.
3. The Gemini `real` mode stays in code but is deprioritized — the paid
   direction is off the table per Neil's directive.

## What this needs from Neil (one-time, zero cost)

- A **free HuggingFace account** + a token with the `inference` scope
  (ATFL_HF_TOKEN), handed to the same place as the game token.
- Accept **FLUX.1-Kontext-dev's license conditions** on that account if
  we want the reference-based selfie path on the free tier (one click;
  otherwise we fall back to plain image-to-image or the Colab notebook).
- w2-browser sign-in is NOT needed yet — only if the Colab fallback is
  ever exercised.

## Addendum 2026-10-01 — the `hf-inference` free route is dead for image models; provider routing is the path

**What changed:** `POST https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell`
now returns **410 "deprecated and no longer supported by provider hf-inference"** (first observed
2026-09-30; FLUX.1-dev, SDXL-base-1.0 identical; Qwen-Image returns 400 "not supported by provider
hf-inference"). The design above assumed the `hf-inference` route serves these models free forever —
that assumption no longer holds. Model IDs pinned in `server/images.py` (FLUX.1-schnell,
FLUX.1-Kontext-dev) are still the right models; only the ROUTE changed.

**Where image generation lives now (verified 2026-10-01 via the Hub API, not just docs):**
`GET https://huggingface.co/api/models/black-forest-labs/FLUX.1-schnell?expand=inferenceProviderMapping`
returns live mappings: **nscale: live, fal-ai: live, wavespeed: live** (together/deepinfra: error).
The router serves providers at `https://router.huggingface.co/<provider>/models/<model>` and nscale
additionally exposes an OpenAI-compatible shape at
`https://router.huggingface.co/nscale/v1/images/generations`
(`{"model": "...", "prompt": "...", "response_format": "b64_json"}`).

**Blocker found in the sandbox:** from this VM, ALL nscale routes hang — `/nscale/v1/images/generations`
(120s, even for deliberately invalid requests) and `/nscale/models/...` (60s). Meanwhile
`hf-inference/...` (fast 410s) and `huggingface.co/api/...` (fast 200s) answer normally, so this is
nscale-specific, not a general egress failure. fal-ai/wavespeed provider-prefix probes were attempted
2026-10-01 (see progress log session #65 for the outcome).

**Cost note (unverified, from a third-party integration doc 2026-10-01):** Inference Providers bill
against the HF account; free tier reportedly includes $0.10/month in inference credits. At ~60
scene+selfie images/month this may not stay at $0 — verify the actual credit policy before wiring
this into the daily turn loop. Neil's "no costs up front" directive stands; this needs his awareness
in a digest before the image path goes live, not just a code change.

**Next step — DONE 2026-10-01 11:07 session:** retested from free-micro-1
(the actual `ATFL_IMAGES=hf` deploy target) over the Tailscale SSH path:
`POST https://router.huggingface.co/nscale/v1/images/generations`
`{"model": "black-forest-labs/FLUX.1-schnell", "prompt": "...",
"response_format": "b64_json"}` → **200** with
`{"created": 1790878126, "data": [{"b64_json": "<1.15MB base64 PNG>"}]}` —
real 1024x1024 PNG bytes (goal hidden_files/hf_first_test_image.png, the
first real test image). `HFImageProvider`'s REST contract rewritten against
this verified response in server/images.py (commit this session); the demo's
hermetic section 13 now pins the OpenAI-compatible shape and passes.
Note: this egress quirk is now two-sided — the sandbox egress hangs nscale
routes while free-micro-1 answers normally, and the
`/hf-inference/models/<dummy>` probe from free-micro-1 returned 401 instead
of the 410 the sandbox sees (route churn; irrelevant, the working route is
the nscale one).
