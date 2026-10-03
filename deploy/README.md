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
| `/var/lib/atfl/games` | game SQLite files + `mailer.db` (handoff bookkeeping + seen-set) |
| `/etc/atfl/atfl.env` | service env: ATFL_MURPH_NODE, ATFL_A8S_NODE, ATFL_A8S_NODE_ROOT, ATFL_A8S_AGENTS_DIR, ATFL_POLL_MIN, ATFL_TURN_LEN_MIN, ATFL_GM, ATFL_GAMES_DIR, ATFL_IMAGES, ATFL_IMAGE_API_KEY, ATFL_HF_TOKEN |
| `/etc/systemd/system/atfl.service` | the unit (this dir's `atfl.service`) |
| `/etc/systemd/system/a8s-atfl-server.service` | A8S node daemon for atfl-server (S3 transport; inbox_append.py bridge feeds the engine's poll_inbound inbox) |

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
# Relay edition 2026-10-02: the engine sends no email directly. It hands
# atfl_outbound envelopes to Murph's A8S node (ATFL_MURPH_NODE) and polls
# its own A8S inbox (ATFL_A8S_NODE) for Murph's atfl_inbound forwards.
ATFL_MURPH_NODE=murph
ATFL_A8S_NODE=atfl-server
ATFL_A8S_NODE_ROOT=/srv/atfl/a8s/atfl-server
# Inbound bridge 2026-10-03: the a8s node daemon delivers to an attached
# node by waking its definition's invoke command, NOT by leaving files in
# the agents inbox dir. atfl-server's definition
# (deploy/atfl-server-definition.json) runs inbox_append.py, which writes
# Murph's raw atfl_inbound envelopes here for poll_inbound() to scan.
ATFL_A8S_AGENTS_DIR=/srv/atfl/a8s/engine-inbox
ATFL_POLL_MIN=5
ATFL_TURN_LEN_MIN=60
ATFL_GM=mock
ATFL_GAMES_DIR=/var/lib/atfl/games
# Phase 3 images (composite per turn email): off | stub | real | hf.
# 'stub' wires deterministic placeholder panels (dev); 'real' (Gemini,
# deprioritized — paid direction retired 2026-09-29) needs the key;
# 'hf' is the no-cost HuggingFace Inference path and needs ATFL_HF_TOKEN.
# Both 'real' and 'hf' refuse to start without their key.
ATFL_IMAGES=off
ATFL_IMAGE_API_KEY=
ATFL_HF_TOKEN=
```

The relay needs no credentials on the engine side at all: the game's
mail identity (murph@inkboxmail.com) is Murph's operational detail, and
the A8S transport uses the node's local mailbox, not OAuth.

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
signups and replies feeling alive without busy-looping — 288 cheap A8S
inbox dir scans/day, and each forward is dispatched only for unseen
inkbox ids (quota-light by design, session #12). The 24h
standalone-nudge window is orthogonal (§2.3) — cadence doesn't touch it.
Revisit toward 1-2 min only if multiplayer makes turn latency feel
sluggish in playtest.

## Operations

`deploy/check-health.sh` — one-shot health check of the deployed engine,
run from the dev VM (tailnet SSH recipe; Neil's standing grant covers
this box). Checks only, never remediates: both units active, poll loop
cycled in the last 15 min with no tracebacks in the journal, engine
inbox dir present, `a8s health` (S3 remote + atfl-server node OK), and
games-dir contents as INFO. Exit 0 / SUMMARY: OK when green.

## Backups

`/var/lib/atfl/games` is the whole world: game files + mailer.db.
Copy the directory to back up; copy it back to restore. No dump
tooling needed — plain SQLite files.
