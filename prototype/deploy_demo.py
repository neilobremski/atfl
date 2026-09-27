"""Deployment-shape smoke test — server/config.py + server/poll.py.

Runs poll.run_once against FakeGmail + MockGM in a temp games dir with
ATFL_GAME_ADDRESS set, plus the config failure modes. No network, no
token, nothing durable. Everything must stay green.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import config, poll
from server.gm import MockGM
from server.mailer import FakeGmail

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


# --- config validation ---
def cfg(**kw):
    env = dict(os.environ)
    env.pop("ATFL_GAME_ADDRESS", None)
    env.update(kw)
    return env

try:
    config.load(cfg())
    check("config refuses missing ATFL_GAME_ADDRESS", False)
except config.ConfigError:
    check("config refuses missing ATFL_GAME_ADDRESS", True)

try:
    config.load(cfg(ATFL_GAME_ADDRESS="game@example.com", ATFL_GM="real"))
    check("config refuses ATFL_GM=real until OQ#2 closes", False)
except config.ConfigError:
    check("config refuses ATFL_GM=real until OQ#2 closes", True)

try:
    config.load(cfg(ATFL_GAME_ADDRESS="game@example.com", ATFL_POLL_MIN="0"))
    check("config refuses ATFL_POLL_MIN=0", False)
except config.ConfigError:
    check("config refuses ATFL_POLL_MIN=0", True)

c = config.load(cfg(ATFL_GAME_ADDRESS="game@example.com"))
check("config defaults: poll 5min, turn 60min, mock gm",
      c["poll_min"] == 5 and c["turn_len_min"] == 60 and c["gm"] == "mock")
check("config defaults: games dir is the VM layout",
      c["games_dir"] == "/var/lib/atfl/games")

# --- poll run_once with FakeGmail ---
with tempfile.TemporaryDirectory() as tmp:
    games_dir = os.path.join(tmp, "games")
    c = config.load(cfg(ATFL_GAME_ADDRESS="game@example.com",
                        ATFL_GAMES_DIR=games_dir))
    fake = FakeGmail(game_address="game@example.com")
    fake.queue_inbound(sender="player@example.com", subject="start",
                       body="I start the game", header_message_id="<s1@x>")
    result = poll.run_once(fake, MockGM(), c)
    check("run_once processes signup via poll entry point",
          len(result["sent"]) == 1)
    sent = result["sent"][0]
    check("signup produces turn_email",
          sent["action"] == "turn_email" and sent["turn_no"] == 1)
    check("games dir auto-created by poll main path", os.path.isdir(games_dir))
    check("mailer.db (seen-set) lives in games dir",
          os.path.exists(os.path.join(games_dir, "mailer.db")))

    # a second cycle with the same inbound already processed: nothing new
    fake2 = FakeGmail(game_address="game@example.com")
    result2 = poll.run_once(fake2, MockGM(), c)
    check("second cycle with nothing new sends nothing",
          result2["sent"] == [] and result2["outcomes"] == [])

    # --once exits 0 in fake mode (exercises the real main() path)
    sys.argv = ["poll", "--fake"]
    os.environ["ATFL_GAMES_DIR"] = games_dir
    rc = poll.main(["--fake"])
    check("poll.main(['--fake']) exits 0", rc == 0)

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks green")
sys.exit(1 if failed else 0)
