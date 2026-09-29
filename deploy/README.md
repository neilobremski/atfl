# Deploying the game server on free-micro-1 (Phase 2 MVP)

Target: Oracle Always Free VM `free-micro-1` (us-sanjose-1, Oracle Linux 9).
The server is one Python process looping `run_poll_cycle` — no inbound
ports, no web server, no database daemon. Everything durable lives in
the games dir (game SQLite files + mailer.db).

## Layout on the VM

| Path | What |
|---|---|
| `/srv/atfl/atfl` | git checkout of github.com/neilobremski/atfl |
| `/srv/atfl/venv` | Python venv (requirements.txt installed) |
| `/var/lib/atfl/games` | game SQLite files + `mailer.db` (per-game thread state + seen-set) |
| `/etc/atfl/atfl.env` | service env: ATFL_GAME_ADDRESS, ATFL_POLL_MIN, ATFL_TURN_LEN_MIN, ATFL_GM, ATFL_GAMES_DIR, ATFL_TOKEN_PATH, ATFL_IMAGES, ATFL_IMAGE_API_KEY, ATFL_HF_TOKEN |
| `/etc/atfl/token.json` | authorized_user OAuth JSON for the game's Gmail account (0600, `atfl:atfl`) |
| `/etc/systemd/system/atfl.service` | the unit (this dir's `atfl.service`) |

## One-time setup (as opc, over SSH)

Idempotent script — safe to re-run; never clobbers an existing
`/etc/atfl/atfl.env` and never force-pulls:

```bash
scp deploy/bootstrap-free-micro-1.sh opc@free-micro-1:
ssh opc@free-micro-1 './bootstrap-free-micro-1.sh --charter ~/r4t.md'
```

What it does: creates the `atfl` user, clones the repo, builds the venv,
fails loudly if `a8s`/`r4t`/`opencode` are missing on the atfl user's PATH
(install those first — the roster's r4t workers need all three), registers
the `atfl-server` a8s node, installs the fogline-gm charter to
`/srv/atfl/ar3/fogline-gm/r4t.md` (only with `--charter`; skipped
otherwise, never overwritten without `--update-charter`), writes a
mock-default `/etc/atfl/atfl.env` only if missing, then installs the unit
after a clean `systemd-analyze verify` and enables it. `--dry-run` prints
every planned action without writing anything; all paths are overridable
via `ATFL_HOME`/`GAMES_DIR`/`ETC_DIR`/`UNIT_DIR` env vars (see
`deploy/bootstrap_test.sh`, 20 checks, for the pinned invariants).

Manual equivalent of the same steps (kept for reference):

```bash
sudo useradd -r -m -d /srv/atfl -s /usr/sbin/nologin atfl
sudo mkdir -p /srv/atfl /var/lib/atfl/games /etc/atfl
sudo git clone https://github.com/neilobremski/atfl.git /srv/atfl/atfl
sudo python3 -m venv /srv/atfl/venv
sudo /srv/atfl/venv/bin/pip install -r /srv/atfl/atfl/deploy/requirements.txt
sudo chown -R atfl:atfl /srv/atfl /var/lib/atfl
```

Register the game server's mailbox-only a8s node (one-time; keeper
replies return to this node's mailbox, and RosterGM sends from its
root — see `server/gm.py` start-send-stop):

```bash
sudo -u atfl mkdir -p /srv/atfl/a8s/atfl-server
sudo -u atfl a8s add atfl-server /srv/atfl/a8s/atfl-server
```

Then `/etc/atfl/atfl.env` (600, `root:atfl`):

```
ATFL_GAME_ADDRESS=<the game's address, once OQ#1 closes>
ATFL_POLL_MIN=5
ATFL_TURN_LEN_MIN=60
ATFL_GM=mock
ATFL_GAMES_DIR=/var/lib/atfl/games
ATFL_TOKEN_PATH=/etc/atfl/token.json
# Roster backend (flip ATFL_GM to roster only for the dry run):
# the mailbox node RosterGM sends from. ATFL_A8S_NODE_ROOT is REQUIRED
# when ATFL_GM=roster (startup refuses without it); unused for mock.
ATFL_A8S_NODE=atfl-server
ATFL_A8S_NODE_ROOT=/srv/atfl/a8s/atfl-server
# Phase 3 images (composite per turn email): off | stub | real | hf.
# 'stub' wires deterministic placeholder panels (dev); 'real' (Gemini,
# deprioritized — paid direction retired 2026-09-29) needs the key (OQ#6);
# 'hf' is the no-cost HuggingFace Inference path and needs ATFL_HF_TOKEN.
# Both 'real' and 'hf' refuse to start without their key.
ATFL_IMAGES=off
ATFL_IMAGE_API_KEY=
ATFL_HF_TOKEN=
```

`/etc/atfl/token.json`: the game's Gmail `authorized_user` OAuth JSON
(gmail.modify scope), 0600 owned by `atfl:atfl`. The OAuth consent is a
one-time interactive step Neil does with the game's Google account —
**the server never stores passwords**, and Murph must not touch Neil's
mailbox for this (boundaries: no credential handling beyond the token
file Neil provides).

Install the unit, then enable + start:

```bash
sudo cp /srv/atfl/atfl/deploy/atfl.service /etc/systemd/system/atfl.service
sudo systemd-analyze verify atfl.service   # must come back clean (see note)
sudo systemctl daemon-reload && sudo systemctl enable --now atfl
journalctl -u atfl -f   # per-cycle logs land here
```

Verify note: `systemd-analyze verify` is the standing pre-deploy check for
this unit — never edit `atfl.service` without running it. On the real VM the
run is clean; the unit's structural invariants (paths, `atfl:atfl`,
`Restart=always`/30s, hardening, `WantedBy`) are pinned in
`prototype/deploy_demo.py`, which re-runs the verify pass hermetically
wherever `systemd-analyze` exists.

## Updates

```bash
cd /srv/atfl/atfl && sudo -u atfl git pull --ff-only && sudo systemctl restart atfl
```

Never force-push from anywhere; the service restarts cleanly (the loop
finishes its current cycle on SIGTERM, systemd waits).

## Poll cadence (decision 2026-09-26)

**5 minutes.** Rationale: DESIGN.md §1.2 says the server polls "every
few minutes" while play is ~one turn per player per day. 5 min keeps
signups and replies feeling alive without busy-looping the API — 288
cheap `messages.list` calls/day is noise against the Gmail budget, and
each `messages.get` runs only for unseen ids (quota-light by design,
session #12). The 24h standalone-nudge window is orthogonal (§2.3) —
cadence doesn't touch it. Revisit toward 1-2 min only if multiplayer
makes turn latency feel sluggish in playtest.

## Backups

`/var/lib/atfl/games` is the whole world: game files + mailer.db.
Copy the directory to back up; copy it back to restore. No dump
tooling needed — plain SQLite files.
