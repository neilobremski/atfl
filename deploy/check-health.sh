#!/usr/bin/env bash
# check-health.sh — one-shot health check of the deployed ATFL engine on free-micro-1.
#
# Runs from the dev VM over the tailnet (Neil's standing grant covers this box).
# Checks only — no remediation. Exit 0 when everything is green, 1 otherwise.
# Prints one line per check: OK / FAIL / INFO.
#
# What it verifies:
#   1. atfl.service is active (and how many times systemd restarted it)
#   2. a8s-atfl-server.service is active
#   3. the poll loop cycled in the last 15 min with no tracebacks in the last 30 min
#   4. games dir contents (INFO only — a real playtest legitimately has a game DB)
#   5. the engine inbox dir exists
#   6. `a8s health`: S3 remote OK and the atfl-server node OK
set -u

KEY="$HOME/workspace/oracle-vm/free-micro-1"
HOST="opc@100.120.196.47"
SSH=(ssh -o "ProxyCommand=nc -X connect -x hatch-egress-proxy:3130 %h %p"
     -o StrictHostKeyChecking=no -o ConnectTimeout=25 -i "$KEY" "$HOST")

fail=0
say() { printf '%-5s %s\n' "$1" "$2"; }
failcheck() { say FAIL "$1"; fail=1; }

OUT="$("${SSH[@]}" '
set -u
echo "S1:$(systemctl is-active atfl.service 2>/dev/null || echo dead)"
echo "S2:$(systemctl is-active a8s-atfl-server.service 2>/dev/null || echo dead)"
echo "S3:$(systemctl show atfl.service -p NRestarts --value 2>/dev/null || echo ?)"
echo "S4:$(journalctl -u atfl.service --since "15 min ago" --no-pager -o cat 2>/dev/null | grep -c "cycle done" || true)"
echo "S5:$(journalctl -u atfl.service --since "30 min ago" --no-pager -o cat 2>/dev/null | grep -ciE "traceback|a8stransporterror|failed with" || true)"
echo "S6:$(ls /var/lib/atfl/games/ 2>/dev/null | tr "\n" "," || echo MISSING)"
echo "S7:$([ -d /srv/atfl/a8s/engine-inbox/atfl-server/inbox ] && echo yes || echo no)"
echo "S8:$(sudo -u atfl bash -lc "cd /srv/atfl && HOME=/srv/atfl a8s health" 2>/dev/null | grep -cE "remote s3: OK|atfl-server: OK" || true)"
' 2>/dev/null)" || { failcheck "ssh to free-micro-1 failed (tailnet/proxy/key?)"; printf 'SUMMARY: FAIL\n'; exit 1; }

get() { printf '%s' "$OUT" | grep "^$1:" | cut -d: -f2-; }

v1="$(get S1)"; v2="$(get S2)"; v3="$(get S3)"; v4="$(get S4)"
v5="$(get S5)"; v6="$(get S6)"; v7="$(get S7)"; v8="$(get S8)"

[ "$v1" = "active" ] && say OK "atfl.service active (NRestarts=$v3)" || failcheck "atfl.service is '$v1' (NRestarts=$v3)"
[ "$v2" = "active" ] && say OK "a8s-atfl-server.service active" || failcheck "a8s-atfl-server.service is '$v2'"
[ "$v4" -ge 1 ] 2>/dev/null && say OK "poll loop cycled in last 15 min ($v4 cycles)" || failcheck "no poll cycles in last 15 min"
[ "$v5" -eq 0 ] 2>/dev/null && say OK "no tracebacks/errors in journal (30 min)" || failcheck "journal shows $v5 error lines in last 30 min"
say INFO "games dir: ${v6:-empty}"
[ "$v7" = "yes" ] && say OK "engine inbox dir present" || failcheck "engine inbox dir missing"
[ "$v8" -ge 2 ] 2>/dev/null && say OK "a8s health: remote s3 OK, atfl-server OK" || failcheck "a8s health degraded (s3/node lines OK: $v8)"

[ "$fail" -eq 0 ] && printf 'SUMMARY: OK\n' || printf 'SUMMARY: FAIL\n'
exit "$fail"
