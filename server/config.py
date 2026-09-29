"""Deployment config — Above the Fog Line server (Phase 2 MVP).

Everything is an environment variable so the same code runs on this dev
VM and on free-micro-1 (via the systemd EnvironmentFile). Defaults are
the free-micro-1 layout; override with env vars locally.

    ATFL_GAMES_DIR      where game SQLite files + mailer.db live
                        (default /var/lib/atfl/games)
    ATFL_TOKEN_PATH     authorized_user JSON for the game's Gmail account
                        (default /etc/atfl/token.json). Not set until
                        open question #1 (game email identity) closes.
    ATFL_GAME_ADDRESS   the game's email address. EMPTY by default —
                        startup refuses to poll without it so we never
                        silently run against the stub.
    ATFL_POLL_MIN       inbox poll cadence, minutes (default 5)
    ATFL_TURN_LEN_MIN   game-turn length, minutes (default 60 — DESIGN.md §2.1)
    ATFL_GM             'mock' (default) or 'roster'. 'roster' wires the
                        R4T fogline-gm roster through server/gm.py RosterGM;
                        needs the roster live on the host (phase 4). Values
                        other than 'mock'/'roster' raise.
    ATFL_A8S_NODE       a8s node name the game server sends from (default
                        atfl-server). Unused for 'mock'.
    ATFL_A8S_NODE_ROOT  root directory of that a8s node on the host
                        (where `a8s tell` must run from — keeper replies to
                        the inbound sender). REQUIRED when ATFL_GM=roster:
                        startup refuses without it so a miswired roster
                        backend fails at boot, not mid-turn. Unused for
                        'mock'.
    ATFL_IMAGES         'off' (default), 'stub', 'real' or 'hf'. 'stub' wires the
                        deterministic placeholder provider into turn emails;
                        'real' raises until the image API key exists (open
                        question #6); 'hf' raises until ATFL_HF_TOKEN exists —
                        the no-cost HuggingFace Inference path (recommended,
                        research/phase3-hf-colab-art.md).
    ATFL_IMAGE_API_KEY  key for the real (Gemini) image provider. Not set until
                        open question #6 closes. Deprioritized 2026-09-29:
                        Neil ruled out paid image APIs.
    ATFL_HF_TOKEN       free HuggingFace token with the 'inference' scope.
                        New one-time ask for the no-cost image path.
"""
import os

DEFAULT_GAMES_DIR = "/var/lib/atfl/games"
DEFAULT_TOKEN_PATH = "/etc/atfl/token.json"


def load(env=os.environ):
    """Read config from env; raise ConfigError on anything missing or
    nonsensical. Failing loudly at startup beats silently polling a stub."""
    gm = env.get("ATFL_GM", "mock").strip().lower()
    if gm not in ("mock", "roster"):
        raise ConfigError(f"ATFL_GM must be 'mock' or 'roster', got {gm!r}")
    # 'roster' needs the fogline-gm roster on the host (phase4 doc);
    # the RosterGM adapter fails loudly at call time if it is missing.
    node_root = env.get("ATFL_A8S_NODE_ROOT", "").strip() or None
    if gm == "roster" and not node_root:
        raise ConfigError(
            "ATFL_GM=roster needs ATFL_A8S_NODE_ROOT set — the game "
            "server's mailbox-only a8s node root (e.g. the directory "
            "from `a8s add atfl-server <root>`). Refusing to start "
            "half-wired.")

    address = env.get("ATFL_GAME_ADDRESS", "").strip()
    if not address:
        raise ConfigError(
            "ATFL_GAME_ADDRESS is not set — the game's email identity is "
            "still open question #1. Refusing to start against the stub.")

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
    if images_mode not in ("off", "stub", "real", "hf"):
        raise ConfigError(
            f"ATFL_IMAGES must be 'off', 'stub', 'real' or 'hf', "
            f"got {images_mode!r}")
    image_api_key = env.get("ATFL_IMAGE_API_KEY", "").strip() or None
    if images_mode == "real" and not image_api_key:
        raise ConfigError(
            "ATFL_IMAGES=real needs ATFL_IMAGE_API_KEY set — the image key "
            "is still open question #6. Refusing to start half-wired.")
    hf_token = env.get("ATFL_HF_TOKEN", "").strip() or None
    if images_mode == "hf" and not hf_token:
        raise ConfigError(
            "ATFL_IMAGES=hf needs ATFL_HF_TOKEN set — the free HuggingFace "
            "token with the 'inference' scope (research/phase3-hf-colab-"
            "art.md). Refusing to start half-wired.")

    return {
        "games_dir": env.get("ATFL_GAMES_DIR", DEFAULT_GAMES_DIR),
        "token_path": env.get("ATFL_TOKEN_PATH", DEFAULT_TOKEN_PATH),
        "game_address": address,
        "poll_min": poll_min,
        "turn_len_min": turn_len_min,
        "gm": gm,
        "a8s_node": env.get("ATFL_A8S_NODE", "atfl-server").strip()
        or "atfl-server",
        "a8s_node_root": node_root,
        "images_mode": images_mode,
        "image_api_key": image_api_key,
        "hf_token": hf_token,
    }


class ConfigError(Exception):
    """Startup config is missing or invalid."""
