"""Deployment-shape smoke test — server/config.py + server/poll.py
(relay edition, 2026-10-02).

Runs poll.run_once against the FakeGmail relay stand-in + MockGM in a
temp games dir with ATFL_A8S_NODE_ROOT set (ATFL_GAME_ADDRESS is
retired — the player-facing address is Murph's operational detail),
plus the config failure modes. Also pins the deploy/atfl.service
unit's structural invariants and, where available, runs
systemd-analyze verify against it. No network, no token, nothing
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

NODE_ROOT = tempfile.mkdtemp(prefix="atfl-node-")
ENGINE_NODE = "atfl-server"
MURPH_NODE = "murph"

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


# --- config validation ---
def cfg(**kw):
    env = dict(os.environ)
    env.pop("ATFL_A8S_NODE_ROOT", None)
    env.update(kw)
    return env

try:
    config.load(cfg())
    check("config refuses missing ATFL_A8S_NODE_ROOT", False)
except config.ConfigError:
    check("config refuses missing ATFL_A8S_NODE_ROOT", True)

try:
    config.load(cfg(ATFL_A8S_NODE_ROOT=NODE_ROOT, ATFL_GM="real"))
    check("config refuses unknown ATFL_GM value 'real'", False)
except config.ConfigError:
    check("config refuses unknown ATFL_GM value 'real'", True)

try:
    config.load(cfg(ATFL_A8S_NODE_ROOT=NODE_ROOT, ATFL_POLL_MIN="0"))
    check("config refuses ATFL_POLL_MIN=0", False)
except config.ConfigError:
    check("config refuses ATFL_POLL_MIN=0", True)

try:
    config.load(cfg(ATFL_A8S_NODE_ROOT=NODE_ROOT, ATFL_MURPH_NODE="  "))
    check("config refuses blank ATFL_MURPH_NODE", False)
except config.ConfigError:
    check("config refuses blank ATFL_MURPH_NODE", True)

c = config.load(cfg(ATFL_A8S_NODE_ROOT=NODE_ROOT))
check("config defaults: poll 5min, turn 60min, mock gm",
      c["poll_min"] == 5 and c["turn_len_min"] == 60 and c["gm"] == "mock")
check("config defaults: games dir is the VM layout",
      c["games_dir"] == "/var/lib/atfl/games")
check("config: relay routing keys present",
      c["murph_node"] == "murph" and c["a8s_node"] == "atfl-server"
      and c["a8s_node_root"] == NODE_ROOT)

# --- poll run_once with the relay stand-in ---
with tempfile.TemporaryDirectory() as tmp:
    games_dir = os.path.join(tmp, "games")
    c = config.load(cfg(ATFL_A8S_NODE_ROOT=NODE_ROOT,
                        ATFL_GAMES_DIR=games_dir))
    fake = FakeGmail(murph_node=MURPH_NODE, engine_node=ENGINE_NODE)
    fake.queue_inbound(sender="player@example.com", subject="start",
                       body="I start the game")
    result = poll.run_once(fake, MockGM(), c)
    check("run_once processes signup via poll entry point",
          len(result["sent"]) == 1)
    sent = result["sent"][0]
    check("signup produces turn_email",
          sent["action"] == "turn_email" and sent["turn_no"] == 1)
    check("signup hands an envelope to the murph node",
          sent["handoff"] is True)
    env = fake.outbox[-1]["envelope"]
    check("outbound envelope is an atfl_outbound for the player",
          env["kind"] == "atfl_outbound" and env["to"] == "player@example.com"
          and env["turn_no"] == 1)
    check("games dir auto-created by poll main path", os.path.isdir(games_dir))
    check("mailer.db (seen-set) lives in games dir",
          os.path.exists(os.path.join(games_dir, "mailer.db")))

    # a second cycle with the same inbound already processed: nothing new
    fake2 = FakeGmail(murph_node=MURPH_NODE, engine_node=ENGINE_NODE)
    result2 = poll.run_once(fake2, MockGM(), c)
    check("second cycle with nothing new sends nothing",
          result2["sent"] == [] and result2["outcomes"] == [])

    # --fake exits 0 (exercises the real main() path)
    os.environ["ATFL_GAMES_DIR"] = games_dir
    os.environ["ATFL_A8S_NODE_ROOT"] = NODE_ROOT
    # config.load reads real env — relay-era FakeGmail needs no address
    rc = poll.main(["--fake"])
    check("poll.main(['--fake']) exits 0", rc == 0)
    os.environ.pop("ATFL_GAMES_DIR", None)
    os.environ.pop("ATFL_A8S_NODE_ROOT", None)

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
                "ProtectHome", "PrivateTmp", "RestartPreventExitStatus"},
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
# NOTE: the --fake smoke run above set ATFL_GAMES_DIR in os.environ; it
# was popped after the run, so this asserts the shipped default.
c_default = config.load(cfg(ATFL_A8S_NODE_ROOT=NODE_ROOT))
rwp = (svc.get("ReadWritePaths") or "").split()
check("unit: ReadWritePaths covers the config-default games dir "
      "(+ the relay-era a8s node root)",
      c_default["games_dir"] == "/var/lib/atfl/games"
      and "/var/lib/atfl" in rwp and "/srv/atfl/a8s" in rwp)
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
