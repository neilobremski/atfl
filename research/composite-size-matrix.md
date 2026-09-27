# Composite size/format tuning (2026-09-27)

**Decision:** panels stay at **1024px**, composite JPEG quality **80**.
Constants: `server.images.COMPOSITE_PANEL_SIZE = 1024`,
`server.images.COMPOSITE_JPEG_QUALITY = 80`. Both flow through
`stitch_composite(..., size, quality)` and `build_turn_composite(...)`;
demos pin the constants plus quality monotonicity.

## Measurements

Deterministic experiment (multi-octave value-noise panels standing in for
AI-generated photography — real photos land higher, stub flat panels lower;
code-drawn map panel is real). Composite = 3 stacked panels + 6px bands.

| panels | q | composite | scene | map | selfie |
|---|---|---|---|---|---|
| 768 | 70 | 66K | 14K | 28K | 14K |
| 768 | 80 | 80K | 16K | 31K | 17K |
| 768 | 85 | 86K | 20K | 34K | 21K |
| 768 | 90 | 99K | 29K | 38K | 31K |
| 1024 | 70 | 101K | 20K | 45K | 20K |
| 1024 | 80 | 117K | 22K | 50K | 23K |
| 1024 | 85 | 125K | 25K | 53K | 26K |
| 1024 | 90 | 142K | 33K | 59K | 35K |

With the real stub + code-drawn map at production settings
(1024px, q80): 112KB (q75: 108K, q85: 118K).

## Rationale

- **1024 over 768:** email clients downscale inline images to ~600px wide
  anyway, so the visible email difference is nil; 1024 keeps the archived
  asset crisp for future use (game recap, artifacts). Size cost is modest
  (~+35-45% bytes for ~78% more pixels; synthetic 117K vs 80K at q80).
- **q80 over q85:** visually indistinguishable at these sizes on both
  synthetic photography and the real map panel; ~7% smaller than q85, ~18%
  smaller than q90. No cliff below q85 in this content mix.
- **Map-panel legibility check (eyeballed):** place-name text at 1024/q80
  renders crisply (`research/` samples below); 768 is acceptable but
  slightly softer.
- **Budget:** expect real composites ≈ 0.3–0.8MB per turn on real
  AI photos at 1024/q80 — comfortably inside Gmail's 25MB cap, one image
  per email, ~1MB/day worst case.
- **JPEG everywhere:** PNG is wrong for photographic panels (megabytes);
  JPEG artifacts on the map's line art at q80 are invisible in practice.

## Verdict on the SVG/"Picasso" alternative

Deferring. The code-drawn map already is a vector-precise (PIL-drawn)
panel whose text legibility is verified above. Generating map art via
LLM-drawn SVG would add a rendering chain (svg→raster) with no legibility
gain and a secrecy surface (the LLM would need discovered-place names —
same as now, but with an extra output stage to validate). Revisit only if
the map ever needs photographic styling.

## Files

- samples: `/tmp/compmatrix/comp_768_q80.jpg`, `comp_1024_q80.jpg`,
  `mapcrop_768.jpg`, `mapcrop_1024.jpg` (ephemeral, not committed)
- existing committed proof: `research/composite-sample-stub-dusk.jpg`
