"""Startup config demo — server/config.py's fail-loud contract.

Every secret-gated mode refuses to start until its open question closes;
every refusal is a ConfigError with a message naming the blocker. The
game address gate means the poller can never silently run against the
stub. All green = the startup safety contract holds.
"""
import sys

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from server.config import ConfigError, load

BASE = {"ATFL_GAME_ADDRESS": "fogline@game.example"}


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"demo failed: {name}")


def raises(env):
    try:
        load(env)
    except ConfigError as e:
        return str(e)
    return None


# --- 1. valid minimal config: defaults fill in, all keys present ---
cfg = load(dict(BASE))
check("minimal valid config loads", cfg["game_address"] == "fogline@game.example")
check("defaults: poll 5 / turn 60 / mock GM / images off",
      cfg["poll_min"] == 5 and cfg["turn_len_min"] == 60
      and cfg["gm"] == "mock" and cfg["images_mode"] == "off")
check("defaults: token path + games dir pinned",
      cfg["token_path"] == "/etc/atfl/token.json"
      and cfg["games_dir"] == "/var/lib/atfl/games")
check("images off -> api_key None", cfg["image_api_key"] is None)

# --- 2. game address gate (open question #1): never run against the stub ---
err = raises({})
check("missing ATFL_GAME_ADDRESS refused", err is not None)
check("refusal names the game-address blocker", "ATFL_GAME_ADDRESS" in err)
check("blank ATFL_GAME_ADDRESS also refused", raises({"ATFL_GAME_ADDRESS": "  "}) is not None)

# --- 3. GM gate (open question #2): 'real' raises until the roster is wired ---
err = raises({**BASE, "ATFL_GM": "real"})
check("ATFL_GM=real refused", err is not None)
check("refusal names the roster blocker", "roster" in err.lower() or "question #2" in err)
check("ATFL_GM=bogus refused", raises({**BASE, "ATFL_GM": "bogus"}) is not None)

# --- 4. image-mode gate (open question #6) ---
err = raises({**BASE, "ATFL_IMAGES": "real"})
check("ATFL_IMAGES=real without key refused", err is not None)
check("refusal names the image-key blocker", "ATFL_IMAGE_API_KEY" in err)
cfg = load({**BASE, "ATFL_IMAGES": "real", "ATFL_IMAGE_API_KEY": "k123"})
check("ATFL_IMAGES=real with key loads", cfg["images_mode"] == "real"
      and cfg["image_api_key"] == "k123")
cfg = load({**BASE, "ATFL_IMAGES": "stub"})
check("ATFL_IMAGES=stub loads, key None", cfg["images_mode"] == "stub"
      and cfg["image_api_key"] is None)
check("ATFL_IMAGES=bogus refused", raises({**BASE, "ATFL_IMAGES": "bogus"}) is not None)
check("ATFL_IMAGES case/space tolerant", load({**BASE, "ATFL_IMAGES": " Stub "})["images_mode"] == "stub")

# --- 5. numeric sanity gates ---
for bad in ("0", "-3", "abc"):
    check(f"ATFL_POLL_MIN={bad!r} refused", raises({**BASE, "ATFL_POLL_MIN": bad}) is not None)
for bad in ("0", "abc"):
    check(f"ATFL_TURN_LEN_MIN={bad!r} refused", raises({**BASE, "ATFL_TURN_LEN_MIN": bad}) is not None)
cfg = load({**BASE, "ATFL_POLL_MIN": "10", "ATFL_TURN_LEN_MIN": "90"})
check("valid numerics load", cfg["poll_min"] == 10 and cfg["turn_len_min"] == 90)

# --- 6. overrides still honored ---
cfg = load({**BASE, "ATFL_TOKEN_PATH": "/run/secrets/token.json",
            "ATFL_GAMES_DIR": "/tmp/atfl-test"})
check("token/games-dir overrides honored",
      cfg["token_path"] == "/run/secrets/token.json"
      and cfg["games_dir"] == "/tmp/atfl-test")

print("\nconfig demo green — startup refuses every half-wired shape.")
