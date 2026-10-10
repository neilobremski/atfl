"""Deployment config — Above the Fog Line server (Phase 2 MVP).

Relay edition (2026-10-02): the engine sends no email directly. It hands
atfl_outbound envelopes to Murph's A8S node and polls its own A8S inbox
for Murph's atfl_inbound forwards (docs/mail-relay-design.md; Neil's
2026-09-30 decision — the engine is transport-independent, Murph is the
exchange layer).

Everything is an environment variable so the same code runs on this dev
VM and on free-micro-1 (via the systemd EnvironmentFile). Defaults are
the free-micro-1 layout; override with env vars locally.

    ATFL_GAMES_DIR      where game SQLite files + mailer.db live
                        (default /var/lib/atfl/games)
    ATFL_MURPH_NODE     the A8S node the engine hands outbound envelopes
                        to (default "murph"). Blank refuses to start — a
                        tell to nowhere must never be silent.
    ATFL_A8S_NODE       the engine's own a8s node (default "atfl-server").
                        Murph forwards atfl_inbound envelopes to its
                        inbox, which the mailer polls.
    ATFL_A8S_NODE_ROOT  root directory of the engine's a8s node on the
                        host (the cwd `a8s tell` runs from, and the base
                        of TELL_OUTBOX_DIR). REQUIRED — startup refuses
                        without it so a half-wired send path fails at
                        boot, not mid-turn.
    ATFL_POLL_MIN       inbox poll cadence, minutes (default 5)
    ATFL_TURN_LEN_MIN   game-turn length, minutes (default 60 — DESIGN.md §2.1)
    ATFL_GM             'mock' (default) or 'roster'. 'roster' wires the
                        R4T fogline-gm roster through server/gm.py RosterGM;
                        needs the roster live on the host (phase 4). Values
                        other than 'mock'/'roster' raise.
    ATFL_IMAGES         'off' (default), 'stub', 'real', 'hf' or
                        'pollinations'. 'stub' wires the deterministic
                        placeholder provider into turn emails; 'real' raises
                        until the image API key exists; 'hf' raises until
                        ATFL_HF_TOKEN exists — the no-cost HuggingFace
                        Inference path (research/phase3-hf-colab-art.md),
                        whose free $0.10/mo credit pool hit zero on the game
                        token (402, 2026-10-07); 'pollinations' is the keyless
                        Pollinations.ai backend (no account, no key, no
                        spend — the flip candidate now awaiting Neil's call).
    ATFL_COMPOSITE       'v1' (default), 'v2', or 'maponly'. 'v1' =
                        three-panel composite (scene, code-drawn map,
                        selfie). 'v2' = single image: scene with the SVG map
                        rasterized into the bottom-left corner, no selfie
                        (Neil's 2026-10-02 one-image-per-turn verdict). The
                        flip is Neil's call — he approves the overlay proof
                        (goal hidden_files/
                        scene_map_overlay_proof_20261002.jpg); until then v2
                        stays off and v1 ships. 'maponly' = full-size SVG
                        map as JPEG, no scene generation (2026-10-10: scene
                        images paused per Neil's verdict that the broad
                        landscapes are a concept mismatch, not a tuning
                        problem).
    ATFL_IMAGE_API_KEY  key for the real (Gemini) image provider. Deprioritized
                        2026-09-29: Neil ruled out paid image APIs.
    ATFL_HF_TOKEN       free HuggingFace token with the 'inference' scope.
                        New one-time ask for the no-cost image path.

Retired 2026-10-02 (relay): ATFL_GAME_ADDRESS (open question #1 closed
2026-09-30 — the player-facing address murph@inkboxmail.com is Murph's
operational detail, never engine config) and ATFL_TOKEN_PATH (no Gmail
OAuth anywhere in the engine).
"""
import os

DEFAULT_GAMES_DIR = "/var/lib/atfl/games"


def load(env=os.environ):
    """Read config from env; raise ConfigError on anything missing or
    nonsensical. Failing loudly at startup beats silently handing
    envelopes to nowhere."""
    gm = env.get("ATFL_GM", "mock").strip().lower()
    if gm not in ("mock", "roster"):
        raise ConfigError(f"ATFL_GM must be 'mock' or 'roster', got {gm!r}")
    # 'roster' needs the fogline-gm roster on the host (phase4 doc);
    # the RosterGM adapter fails loudly at call time if it is missing.
    node_root = env.get("ATFL_A8S_NODE_ROOT", "").strip() or None
    if not node_root:
        raise ConfigError(
            "ATFL_A8S_NODE_ROOT is not set — the engine's a8s node root "
            "(cwd + TELL_OUTBOX_DIR for `a8s tell`). Refusing to start "
            "with a half-wired send path.")

    murph_node = env.get("ATFL_MURPH_NODE", "murph").strip()
    if not murph_node:
        raise ConfigError(
            "ATFL_MURPH_NODE is blank — the A8S node the engine hands "
            "outbound envelopes to. Refusing to start against nowhere.")

    try:
        poll_min = int(env.get("ATFL_POLL_MIN", "5"))
        if poll_min < 1:
            raise ValueError
    except ValueError:
        raise ConfigError("ATFL_POLL_MIN must be a positive integer (minutes).")

    try:
        turn_len_min = int(env.get("ATFL_TURN_LEN_MIN", "60"))
        if turn_len_min < 1:
            raise ValueError
    except ValueError:
        raise ConfigError("ATFL_TURN_LEN_MIN must be a positive integer (minutes).")

    images_mode = env.get("ATFL_IMAGES", "off").strip().lower()
    if images_mode not in ("off", "stub", "real", "hf", "pollinations"):
        raise ConfigError(
            f"ATFL_IMAGES must be 'off', 'stub', 'real', 'hf' or "
            f"'pollinations', got {images_mode!r}")

    composite = env.get("ATFL_COMPOSITE", "v1").strip().lower()
    if composite not in ("v1", "v2", "maponly"):
        raise ConfigError(
            f"ATFL_COMPOSITE must be 'v1', 'v2' or 'maponly', got {composite!r}")
    image_api_key = env.get("ATFL_IMAGE_API_KEY", "").strip() or None
    if images_mode == "real" and not image_api_key:
        raise ConfigError(
            "ATFL_IMAGES=real needs ATFL_IMAGE_API_KEY set. Refusing to "
            "start half-wired.")
    hf_token = env.get("ATFL_HF_TOKEN", "").strip() or None
    if images_mode == "hf" and not hf_token:
        raise ConfigError(
            "ATFL_IMAGES=hf needs ATFL_HF_TOKEN set — the free HuggingFace "
            "token with the 'inference' scope (research/phase3-hf-colab-"
            "art.md). Refusing to start half-wired.")

    return {
        "games_dir": env.get("ATFL_GAMES_DIR", DEFAULT_GAMES_DIR),
        "murph_node": murph_node,
        "a8s_node": env.get("ATFL_A8S_NODE", "atfl-server").strip()
        or "atfl-server",
        "a8s_node_root": node_root,
        "poll_min": poll_min,
        "turn_len_min": turn_len_min,
        "gm": gm,
        "images_mode": images_mode,
        "image_api_key": image_api_key,
        "hf_token": hf_token,
        "composite": composite,
    }


class ConfigError(Exception):
    """Startup config is missing or invalid."""
