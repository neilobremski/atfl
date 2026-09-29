#!/usr/bin/env bash
# bootstrap_test.sh — validates deploy/bootstrap-free-micro-1.sh without
# touching the system. All runs are --help, --dry-run, or grep invariants.
set -euo pipefail
cd "$(dirname "$0")"
S=./bootstrap-free-micro-1.sh
pass=0; fail=0
check() { # $1=name $2...=command
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "PASS: $name"; pass=$((pass+1));
  else echo "FAIL: $name"; fail=$((fail+1)); fi
}
check_grep() { # $1=name $2=pattern
  if grep -qE "$2" "$S"; then echo "PASS: $1"; pass=$((pass+1));
  else echo "FAIL: $1"; fail=$((fail+1)); fi
}

check "bash -n syntax clean" bash -n "$S"
check "--help exits 0" "$S" --help
if "$S" --help 2>/dev/null | grep -q -- --dry-run; then echo "PASS: --help documents --dry-run"; pass=$((pass+1)); else echo "FAIL: --help documents --dry-run"; fail=$((fail+1)); fi

# Sandbox dry-run: fake a8s/r4t/opencode on PATH, sandbox ATFL_HOME.
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
for b in a8s r4t opencode; do printf '#!/bin/sh\nexit 0\n' > "$SANDBOX/$b"; chmod +x "$SANDBOX/$b"; done
export PATH="$SANDBOX:$PATH"
export ATFL_HOME="$SANDBOX/atfl-home" GAMES_DIR="$SANDBOX/games" ETC_DIR="$SANDBOX/etc" UNIT_DIR="$SANDBOX/units"
echo "charter" > "$SANDBOX/r4t.md"
DRY_OUT="$("$S" --dry-run --charter "$SANDBOX/r4t.md" 2>&1)"
check "sandbox --dry-run exits 0" true  # exit code already asserted by set -e via capture
echo "$DRY_OUT" | grep -q "useradd" && echo "PASS: dry-run plans useradd" && pass=$((pass+1)) || { echo "FAIL: dry-run plans useradd"; fail=$((fail+1)); }
echo "$DRY_OUT" | grep -q "git clone" && echo "PASS: dry-run plans git clone" && pass=$((pass+1)) || { echo "FAIL: dry-run plans git clone"; fail=$((fail+1)); }
echo "$DRY_OUT" | grep -q "install charter" && echo "PASS: dry-run plans charter install" && pass=$((pass+1)) || { echo "FAIL: dry-run plans charter install"; fail=$((fail+1)); }
echo "$DRY_OUT" | grep -q "atfl.env" && echo "PASS: dry-run plans atfl.env" && pass=$((pass+1)) || { echo "FAIL: dry-run plans atfl.env"; fail=$((fail+1)); }
test ! -e "$SANDBOX/atfl-home" && echo "PASS: dry-run wrote nothing to ATFL_HOME" && pass=$((pass+1)) || { echo "FAIL: dry-run wrote nothing to ATFL_HOME"; fail=$((fail+1)); }
test ! -e "$SANDBOX/etc" && echo "PASS: dry-run wrote nothing to ETC_DIR" && pass=$((pass+1)) || { echo "FAIL: dry-run wrote nothing to ETC_DIR"; fail=$((fail+1)); }

# Loud-failure gate: no a8s anywhere -> nonzero exit naming a8s.
if PATH="/usr/bin:/bin" ATFL_HOME="$SANDBOX/x" "$S" --dry-run >/dev/null 2>&1; then
  echo "FAIL: missing a8s fails loudly"; fail=$((fail+1))
else
  echo "PASS: missing a8s fails loudly"; pass=$((pass+1))
fi

# Invariants baked into the script.
check_grep "set -euo pipefail" '^set -euo pipefail'
check_grep "ff-only pull, no --force anywhere" 'pull --ff-only'
! grep -vE '^\s*#' "$S" | grep -q -- '--force' && echo "PASS: no --force flag in code" && pass=$((pass+1)) || { echo "FAIL: no --force flag in code"; fail=$((fail+1)); }
check_grep "atfl.env never-clobber guard" 'atfl.env exists, keeping'
check_grep "charter never-clobber default" 'pass --update-charter to replace'
check_grep "systemd-analyze verify before enable" 'systemd-analyze verify atfl.service'
check_grep "mock is the safe default" 'ATFL_GM=mock'
check_grep "opencode PATH gate for r4t workers" 'opencode not on the atfl'
check_grep "a8s resolution mirrors RosterGM _a8s_bin" 'ATFL_A8S_BIN.*PATH.*\.ar3/a8s'

echo "--- $pass passed, $fail failed ---"
exit $((fail > 0))
