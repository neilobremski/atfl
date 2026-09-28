"""Deployment-shape smoke test — server/config.py + server/poll.py.

Runs poll.run_once against FakeGmail + MockGM in a temp games dir with
ATFL_GAME_ADDRESS set, plus the config failure modes. Also pins the
deploy/atfl.service unit's structural invariants and, where available,
runs systemd-analyze verify against it. No network, no token, nothing
durable. Everything must stay green.
"""
import configparser
import os
import shutil
import subprocess
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
    check("config refuses unknown ATFL_GM value 'real'", False)
except config.ConfigError:
    check("config refuses unknown ATFL_GM value 'real'", True)

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
    os.environ["ATFL_GAME_ADDRESS"] = "game@example.com"  # config.load reads real env
    rc = poll.main(["--fake"])
    check("poll.main(['--fake']) exits 0", rc == 0)

# --- deploy/atfl.service unit pins (session #24, 2026-09-27) ---
# systemd-analyze verify pass 2026-09-27: the unit parses cleanly — the only
# atfl.service line in its output is the expected env gap ("Command
# /srv/atfl/venv/bin/python is not executable") because the venv lives on
# free-micro-1, not wherever the check runs. Any other atfl.service line
# (Unknown / Failed / refusing / invalid) would be a real unit bug. The
# structural pins below are hermetic so the demo stays green anywhere; the
# verify run itself is guarded on the binary's presence.
unit_path = os.path.join(os.path.dirname(__file__), "..", "deploy",
                         "atfl.service")
cp = configparser.ConfigParser(strict=False)
cp.optionxform = str  # keep directive case exactly as written
parsed = cp.read(unit_path)
check("deploy/atfl.service exists and parses", len(parsed) == 1)

known_directives = {
    "Unit": {"Description", "After", "Wants"},
    "Service": {"Type", "User", "Group", "WorkingDirectory",
                "EnvironmentFile", "ExecStart", "Restart", "RestartSec",
                "NoNewPrivileges", "ProtectSystem", "ReadWritePaths",
                "ProtectHome", "PrivateTmp"},
    "Install": {"WantedBy"},
}
check("unit has exactly [Unit]/[Service]/[Install] sections",
      set(cp.sections()) == set(known_directives))
check("unit uses only known directives (no typos or strays)",
      all(set(cp.options(sec)) <= known_directives[sec]
          for sec in known_directives))

svc = dict(cp.items("Service"))
check("unit: Type=simple, Restart=always, RestartSec=30",
      svc.get("Type") == "simple" and svc.get("Restart") == "always"
      and svc.get("RestartSec") == "30")
check("unit: runs as atfl:atfl",
      svc.get("User") == "atfl" and svc.get("Group") == "atfl")
check("unit: WorkingDirectory matches README checkout path",
      svc.get("WorkingDirectory") == "/srv/atfl/atfl")
check("unit: EnvironmentFile matches README env path",
      svc.get("EnvironmentFile") == "/etc/atfl/atfl.env")
check("unit: ExecStart is the venv python running the poll loop",
      svc.get("ExecStart", "").split()[:2] ==
      ["/srv/atfl/venv/bin/python", "-m"] and
      "server.poll" in svc.get("ExecStart", ""))
# NOTE: the --fake smoke run above left ATFL_GAMES_DIR set in os.environ;
# clear it so this asserts the shipped default, not the temp dir.
os.environ.pop("ATFL_GAMES_DIR", None)
c_default = config.load(cfg(ATFL_GAME_ADDRESS="game@example.com"))
check("unit: ReadWritePaths covers the config-default games dir",
      c_default["games_dir"] == "/var/lib/atfl/games"
      and svc.get("ReadWritePaths") == "/var/lib/atfl")
check("unit: hardening directives pinned",
      svc.get("NoNewPrivileges") == "true"
      and svc.get("ProtectSystem") == "strict"
      and svc.get("ProtectHome") == "true"
      and svc.get("PrivateTmp") == "true")
check("unit: ordered after network-online.target",
      "network-online.target" in cp.get("Unit", "After"))
check("unit: wanted by multi-user.target",
      cp.get("Install", "WantedBy") == "multi-user.target")

analyzer = shutil.which("systemd-analyze")
if analyzer is None:
    print("SKIP systemd-analyze verify (binary absent on this machine)")
    checks.append(("systemd-analyze verify skipped: binary absent", True))
else:
    proc = subprocess.run([analyzer, "verify", unit_path],
                          capture_output=True, text=True, timeout=60)
    svc_lines = [l for l in proc.stderr.splitlines()
                 if "atfl.service" in l]
    real_errors = [l for l in svc_lines
                   if any(tok in l for tok in ("Unknown", "Failed",
                                              "refusing", "invalid",
                                              "Illegal"))]
    check("systemd-analyze verify: no atfl.service parser errors",
          real_errors == [])
    only_known_gap = all("is not executable" in l for l in svc_lines)
    check("systemd-analyze verify: only the known target-VM-path gap "
          "(or silence on the real VM)", only_known_gap)

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks green")
sys.exit(1 if failed else 0)
