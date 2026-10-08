"""Startup config demo — server/config.py's fail-loud contract.

Relay edition (2026-10-02): ATFL_GAME_ADDRESS is retired (the
player-facing address murph@inkboxmail.com is Murph's operational
detail, never engine config) and ATFL_A8S_NODE_ROOT is REQUIRED —
every config refuses to start without it, so a half-wired send path
fails at boot, not mid-turn. Every secret-gated image mode refuses to
start until its blocker closes; every refusal is a ConfigError naming
the blocker. All green = the startup safety contract holds.
"""
import sys

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from server.config import ConfigError, load

BASE = {"ATFL_A8S_NODE_ROOT": "/srv/atfl/a8s/atfl-server"}


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
check("minimal valid config loads (node root only)",
      cfg["a8s_node_root"] == "/srv/atfl/a8s/atfl-server")
check("defaults: poll 5 / turn 60 / mock GM / images off",
      cfg["poll_min"] == 5 and cfg["turn_len_min"] == 60
      and cfg["gm"] == "mock" and cfg["images_mode"] == "off")
check("defaults: murph node + engine node pinned",
      cfg["murph_node"] == "murph" and cfg["a8s_node"] == "atfl-server")
check("defaults: games dir pinned",
      cfg["games_dir"] == "/var/lib/atfl/games")
check("images off -> api_key None", cfg["image_api_key"] is None)
check("images off -> hf_token None", cfg["hf_token"] is None)
check("composite default v1", cfg["composite"] == "v1")

# --- 2. node-root gate (relay): never boot with a half-wired send path ---
err = raises({})
check("missing ATFL_A8S_NODE_ROOT refused", err is not None)
check("refusal names the node-root blocker", "ATFL_A8S_NODE_ROOT" in err)
check("blank ATFL_A8S_NODE_ROOT also refused",
      raises({"ATFL_A8S_NODE_ROOT": "  "}) is not None)

# --- 2b. murph-node gate: a tell to nowhere must never be silent ---
err = raises({**BASE, "ATFL_MURPH_NODE": "  "})
check("blank ATFL_MURPH_NODE refused", err is not None)
check("refusal names the murph-node blocker", "ATFL_MURPH_NODE" in err)

# --- 3. GM backend gate: 'mock' default, 'roster' loads, junk refused ---
no_root = {k: v for k, v in BASE.items() if k != "ATFL_A8S_NODE_ROOT"}
err = raises({**no_root, "ATFL_GM": "roster"})
check("ATFL_GM=roster without ATFL_A8S_NODE_ROOT refused", err is not None)
check("refusal names the node-root blocker", "ATFL_A8S_NODE_ROOT" in err)
cfg = load({**BASE, "ATFL_GM": "roster"})
check("ATFL_GM=roster loads (roster backend selectable)", cfg["gm"] == "roster")
check("a8s node name defaults to atfl-server", cfg["a8s_node"] == "atfl-server")
cfg = load({"ATFL_A8S_NODE_ROOT": "/srv/atfl/a8s/x",
            "ATFL_A8S_NODE": "game-mailbox"})
check("ATFL_A8S_NODE override honored", cfg["a8s_node"] == "game-mailbox")
for bad in ("real", "bogus"):
    err = raises({**BASE, "ATFL_GM": bad})
    check(f"ATFL_GM={bad} refused", err is not None)
check("ATFL_GM case/space tolerant",
      load({**BASE, "ATFL_GM": " Roster "})["gm"] == "roster")

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
err = raises({**BASE, "ATFL_IMAGES": "hf"})
check("ATFL_IMAGES=hf without token refused", err is not None)
check("refusal names the HF-token blocker", "ATFL_HF_TOKEN" in err)
cfg = load({**BASE, "ATFL_IMAGES": "hf", "ATFL_HF_TOKEN": "hf_x"})
check("ATFL_IMAGES=hf with token loads", cfg["images_mode"] == "hf"
      and cfg["hf_token"] == "hf_x")
cfg = load({**BASE, "ATFL_IMAGES": "pollinations"})
check("ATFL_IMAGES=pollinations loads keyless (no token required)",
      cfg["images_mode"] == "pollinations" and cfg["hf_token"] is None)
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
cfg = load({**BASE, "ATFL_GAMES_DIR": "/tmp/atfl-test",
            "ATFL_COMPOSITE": "v2"})
check("games-dir/composite overrides honored",
      cfg["games_dir"] == "/tmp/atfl-test" and cfg["composite"] == "v2")
check("ATFL_COMPOSITE=bogus refused", raises({**BASE, "ATFL_COMPOSITE": "v3"}) is not None)

print("\nconfig demo green — startup refuses every half-wired shape.")
