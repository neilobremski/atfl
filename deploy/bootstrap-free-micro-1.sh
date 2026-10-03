#!/usr/bin/env bash
# bootstrap-free-micro-1.sh — one-time game-server setup on the Oracle Always
# Free VM `free-micro-1` (Oracle Linux 9). Run as the `opc` user over SSH:
#
#   ./bootstrap-free-micro-1.sh [--dry-run] [--charter /path/to/r4t.md] [--update-charter]
#
# Idempotent: safe to re-run. Never clobbers an existing /etc/atfl/atfl.env
# (writes it only when missing) and never force-pushes/pulls with --force.
# Loud failures: missing prerequisites (a8s, opencode, r4t) stop the run with
# the install command printed, instead of silently continuing half-wired.
#
# Tunables (env overrides, default to the production layout):
#   ATFL_HOME  default /srv/atfl        (atfl user home + repo checkout)
#   GAMES_DIR  default /var/lib/atfl/games
#   ETC_DIR    default /etc/atfl        (atfl.env + token.json)
#   UNIT_DIR   default /etc/systemd/system
set -euo pipefail

ATFL_HOME="${ATFL_HOME:-/srv/atfl}"
GAMES_DIR="${GAMES_DIR:-/var/lib/atfl/games}"
ETC_DIR="${ETC_DIR:-/etc/atfl}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
REPO_URL="https://github.com/neilobremski/atfl.git"
NODE_NAME="atfl-server"

DRY_RUN=0
CHARTER_SRC=""
UPDATE_CHARTER=0

usage() {
  sed -n '2,14p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --charter) CHARTER_SRC="$2"; shift 2 ;;
    --update-charter) UPDATE_CHARTER=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

as_atfl() {
  run sudo -u atfl "$@"
}

log() { echo "==> $*"; }

fail() { echo "FATAL: $*" >&2; exit 1; }

need_cmd() {
  # $1 = command name, $2 = install hint printed on failure
  command -v "$1" >/dev/null 2>&1 || fail "$1 not found. $2"
}

# ---------------------------------------------------------------- user & dirs
log "user + directories"
if id atfl >/dev/null 2>&1; then
  echo "user atfl exists, keeping"
else
  run sudo useradd -r -m -d "$ATFL_HOME" -s /usr/sbin/nologin atfl
fi
run sudo mkdir -p "$ATFL_HOME" "$GAMES_DIR" "$ETC_DIR"

# ---------------------------------------------------------------- repo + venv
log "repo checkout"
if [[ -d "$ATFL_HOME/atfl/.git" ]]; then
  log "repo present, fast-forward pull only"
  as_atfl git -C "$ATFL_HOME/atfl" pull --ff-only
else
  as_atfl git clone "$REPO_URL" "$ATFL_HOME/atfl"
fi

log "venv"
if [[ -x "$ATFL_HOME/venv/bin/python" ]]; then
  echo "venv exists, reinstalling requirements"
else
  as_atfl python3 -m venv "$ATFL_HOME/venv"
fi
as_atfl "$ATFL_HOME/venv/bin/pip" install -r "$ATFL_HOME/atfl/deploy/requirements.txt"

# ------------------------------------------------- a8s / r4t / opencode gates
# RosterGM resolves the a8s binary as ATFL_A8S_BIN -> PATH -> ~atfl/.ar3/a8s,
# so the atfl user must see a working a8s. The roster's r4t workers need r4t
# and opencode on the atfl user's PATH (session #32: first live turn failed
# with exit 127 until opencode landed in ~/.local/bin).
log "prerequisite checks (a8s, r4t, opencode)"
A8S_BIN=""
for cand in "${ATFL_A8S_BIN:-}" "$(command -v a8s 2>/dev/null || true)" "$ATFL_HOME/.ar3/a8s"; do
  if [[ -n "$cand" && -x "$cand" ]]; then A8S_BIN="$cand"; break; fi
done
[[ -n "$A8S_BIN" ]] || fail "no a8s binary for the atfl user (checked ATFL_A8S_BIN, PATH, $ATFL_HOME/.ar3/a8s). Install the AR3 tooling as atfl first, then re-run."
echo "a8s: $A8S_BIN"

as_atfl env PATH="$ATFL_HOME/.local/bin:$PATH" command -v r4t >/dev/null 2>&1 \
  || fail "r4t not on the atfl user's PATH. Install per ~/.ar3/docs, then re-run."
as_atfl env PATH="$ATFL_HOME/.local/bin:$PATH" command -v opencode >/dev/null 2>&1 \
  || fail "opencode not on the atfl user's PATH (r4t workers need it; on this box the fix was a symlink into ~/.local/bin). Install opencode as atfl, then re-run."

# ------------------------------------------------------------ a8s node (once)
log "a8s node $NODE_NAME"
NODE_ROOT="$ATFL_HOME/a8s/$NODE_NAME"
as_atfl mkdir -p "$NODE_ROOT"   # a8s add requires the node dir to exist
if [[ -n "$(ls -A "$NODE_ROOT" 2>/dev/null)" ]]; then
  echo "node dir $NODE_ROOT already populated, keeping (delete it to re-register)"
else
  as_atfl "$A8S_BIN" add "$NODE_NAME" "$NODE_ROOT"
fi

# ------------------------------------------------- roster charter (if given)
# The fogline-gm charter (r4t.md) is the roster's live contract. The game repo
# does not carry it; pass the current copy explicitly. Never overwritten
# unless --update-charter is given.
if [[ -n "$CHARTER_SRC" ]]; then
  [[ -f "$CHARTER_SRC" ]] || fail "--charter file not found: $CHARTER_SRC"
  CHARTER_DST="$ATFL_HOME/ar3/fogline-gm/r4t.md"
  if [[ -f "$CHARTER_DST" && $UPDATE_CHARTER -eq 0 ]]; then
    echo "charter exists at $CHARTER_DST, keeping (pass --update-charter to replace)"
  else
    run sudo -u atfl mkdir -p "$(dirname "$CHARTER_DST")"
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "[dry-run] install charter $CHARTER_SRC -> $CHARTER_DST"
    else
      sudo install -o atfl -g atfl -m 0644 "$CHARTER_SRC" "$CHARTER_DST"
      sha256sum "$CHARTER_DST"
    fi
  fi
else
  echo "no --charter given, skipping roster charter install"
fi

# ------------------------------------------------------------ atfl.env (once)
# Never clobber: an operator-edited atfl.env is authoritative.
log "service env $ETC_DIR/atfl.env"
if [[ -f "$ETC_DIR/atfl.env" ]]; then
  echo "atfl.env exists, keeping (edit it by hand for the roster/hf flip)"
else
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] write $ETC_DIR/atfl.env (600 root:atfl, ATFL_GM=mock default)"
  else
    sudo tee "$ETC_DIR/atfl.env" >/dev/null <<'EOF'
# Relay edition 2026-10-02: the engine sends no email directly. It hands
# atfl_outbound envelopes to Murph's A8S node and polls its own A8S
# inbox for Murph's atfl_inbound forwards.
ATFL_MURPH_NODE=murph
ATFL_A8S_NODE=atfl-server
ATFL_A8S_NODE_ROOT=/srv/atfl/a8s/atfl-server
ATFL_POLL_MIN=5
ATFL_TURN_LEN_MIN=60
ATFL_GM=mock
ATFL_GAMES_DIR=/var/lib/atfl/games
# Images: off | stub | hf ('hf' needs ATFL_HF_TOKEN; no paid keys)
# Composite layout: v1 (default) | v2 (single scene + map overlay; the
# flip is Neil's call once he approves the overlay proof).
ATFL_IMAGES=off
ATFL_COMPOSITE=v1
ATFL_HF_TOKEN=
EOF
    sudo chmod 600 "$ETC_DIR/atfl.env"
    sudo chown root:atfl "$ETC_DIR/atfl.env"
  fi
fi
run sudo chown -R atfl:atfl "$ATFL_HOME" "$GAMES_DIR"

# ---------------------------------------------------------------- systemd unit
log "systemd unit"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dry-run] install $ATFL_HOME/atfl/deploy/atfl.service -> $UNIT_DIR/atfl.service, verify, enable --now"
else
  sudo cp "$ATFL_HOME/atfl/deploy/atfl.service" "$UNIT_DIR/atfl.service"
  sudo systemd-analyze verify atfl.service   # standing pre-deploy check; must be clean
  sudo systemctl daemon-reload
  sudo systemctl enable --now atfl
fi

log "done. Next: journalctl -u atfl -f (mock turn loop), then the roster flip per deploy/roster-dry-run-checklist.md."
