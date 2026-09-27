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
    ATFL_GM             'mock' (default) or 'real'. 'real' raises until
                        open question #2 (R4T GM model) is closed.
    ATFL_IMAGES         'off' (default), 'stub' or 'real'. 'stub' wires the
                        deterministic placeholder provider into turn emails;
                        'real' raises until the image API key exists (open
                        question #6).
    ATFL_IMAGE_API_KEY  key for the real image provider. Not set until
                        open question #6 closes.
"""
import os

DEFAULT_GAMES_DIR = "/var/lib/atfl/games"
DEFAULT_TOKEN_PATH = "/etc/atfl/token.json"


def load(env=os.environ):
    """Read config from env; raise ConfigError on anything missing or
    nonsensical. Failing loudly at startup beats silently polling a stub."""
    gm = env.get("ATFL_GM", "mock").strip().lower()
    if gm not in ("mock", "real"):
        raise ConfigError(f"ATFL_GM must be 'mock' or 'real', got {gm!r}")
    if gm == "real":
        raise ConfigError(
            "ATFL_GM=real needs the R4T roster model wired (open question #2); "
            "the GameMaster interface is ready for it (server/gm.py).")

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
    if images_mode not in ("off", "stub", "real"):
        raise ConfigError(
            f"ATFL_IMAGES must be 'off', 'stub' or 'real', got {images_mode!r}")
    image_api_key = env.get("ATFL_IMAGE_API_KEY", "").strip() or None
    if images_mode == "real" and not image_api_key:
        raise ConfigError(
            "ATFL_IMAGES=real needs ATFL_IMAGE_API_KEY set — the image key "
            "is still open question #6. Refusing to start half-wired.")

    return {
        "games_dir": env.get("ATFL_GAMES_DIR", DEFAULT_GAMES_DIR),
        "token_path": env.get("ATFL_TOKEN_PATH", DEFAULT_TOKEN_PATH),
        "game_address": address,
        "poll_min": poll_min,
        "turn_len_min": turn_len_min,
        "gm": gm,
        "images_mode": images_mode,
        "image_api_key": image_api_key,
    }


class ConfigError(Exception):
    """Startup config is missing or invalid."""
