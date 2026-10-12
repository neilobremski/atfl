# Host-event recovery playbook (Murph VM, sandbox side)

Consolidated 2026-10-05 from sessions #102, #103, #107, #109, #111.
Updated 2026-10-12 with field lessons from recovery of events #38, #39, #40
(Oct 11–12; no recovery since has changed the procedure, this is the current
reference).

## What a host event does

A VM replacement (as distinct from a restart) wipes `/etc` and everything
installed system-wide outside `$HOME`. The persistent tree (`~/workspace`,
`~/.ar3`, `~/.config`, `~/.opencode`, `~/filedrops`) survives.

Confirmed casualties:
- `/etc/systemd/system/*.service`, `*.timer` — the five units
  (a8s-fogline-gm.service, atfl-outbound-relay.service/.timer,
  atfl-inbound-relay.service/.timer).
  `a8s-murph.service` is NOT among them — retired 2026-10-07 (MCP cutover);
  it is not reinstalled.
- pip installs outside the home tree — `cairosvg` (map_svg selftest dep) is
  the known recurring casualty. The reinstall script ensures it into the
  PERSISTENT user site (`~/.local/lib/python3.12/site-packages`); the stale
  system-site copy was removed so the import resolves to the persistent one.
- `/etc/environment` is recreated at boot with the proxy vars (verified #103)

`/etc` losses are all covered by persistent copies in `~/workspace/a8s-systemd/`.

## Step 0: the per-session ritual (do this first, every session)

```
~/workspace/a8s-systemd/reinstall-a8s-units.sh   # idempotent; reports changed=N
```

(The script lives in `~/workspace/a8s-systemd/`, NOT `~/workspace/tools/`.
Stale references to the tools/ path have wasted sessions before.)

- `changed=0` → no host event; skip to the resting-state reference and
  run the steady-state checks.
- `changed=1` → full recovery below, then VERIFY the sentinel was rewritten:
  `cat ~/workspace/a8s-systemd/.last-boot-id` must equal
  `cat /proc/sys/kernel/random/boot_id`. (Events #39/#40: the sentinel file
  itself had vanished; the script re-records the current boot.)
- (Event #39 hit ~1 minute after a recovery, wiping the just-reinstalled
  units — step 0 can legitimately run twice in one session.)

## Recovery order (after changed=1)

1. **Units** — the script above, then `sudo systemctl daemon-reload`,
   `enable --now` for the services + timers (the script handles it).
2. **pip deps** — `python3 -c "import cairosvg"`; expect importable
   (2.9.1). The script installs it into the PERSISTENT user site when
   missing — no manual `pip install` needed. If the import resolves to a
   system path, the stale system-site copy is back; remove it so the user
   site wins.
3. **A8S node** — `~/.ar3/a8s health` (bare `a8s` is NOT on PATH in worker
   shells; always the full path). Expect: `remote s3: OK`, storage OK,
   3 agents registered (atfl-server, fogline-gm, murph).
4. **Relay recovery** — the unit existing is NOT the relay working:
   primary check is the verifier (covers both legs at once):
   `bash ~/workspace/goals/above-the-fog-line-game-project/hidden_files/verify_postreinstall_relays.sh`
   expect `all-ok` — both timers armed, last inbound/outbound cycles rc=0,
   401 flap counter 0 consecutive.
   Secondary (raw) signal: the timers fire every 5 min (`OnCalendar=*:0/5`);
   `hidden_files/atfl_outbound_consumer.log` gains a line shaped
   `<UTC>T mode=send rc=0 :: scanned=N ignored=N would_send=0 sent=0
   replay_skipped=0 quarantined=0`
   (values vary; the *shape* — rc=0, quarantined=0 — is the signal).
5. **Final state check** — the steady-state session checklist (recovery
   sessions since Oct 11 use exactly this; `playtest_watch.py`'s old
   "no games" expectation is stale — the game is live in playtest):
   - roster: `a8s run fogline-gm` daemon alive, keeper/arbiter/critic
     inboxes empty; roster log
     `~/.config/r4t/rosters/fogline-gm/log/YYYY-MM-DD.md` shows no new
     errors beyond the known dream-budget loop;
   - engine (free-micro-1): `atfl.service` active; clean 5-min poll cycles
     (`0 inbound processed, 0 handed off`); turns table latest row =
     expected turn_no/created_at (game db is
     `/var/lib/atfl/games/<guid>.db` — there is NO `sqlite3` binary on the
     box, query with `python3 -c "import sqlite3, ..."`);
   - deployed env: `ATFL_GM=roster`, `ATFL_COMPOSITE=maponly` (Neil's
     2026-10-10 verdict: scene images paused), `ATFL_IMAGES=pollinations`;
   - `/var/lib/atfl/games/` = `mailer.db` + the live game db + `assets/`
     (no longer "mailer.db only" — that was the pre-launch resting state);
   - idle verifier: `hidden_files/verify_idle_turn.py` PENDING rc=0
     outside the gate window (gate = latest `turns.created_at` + 24h,
     keyed off the turn row, NOT the Inkbox send time).

   SSH: use the wrapper — `bash ~/workspace/tools/ssh-vm.sh <cmd>`
   (lives in `tools/`, NOT goal hidden_files/). It encodes the full
   recipe: `opc@100.120.196.47` over the tailnet via the egress proxy,
   key `~/workspace/oracle-vm/free-micro-1`,
   `UserKnownHostsFile=/home/hatch/.ssh/known_hosts`
   (root shells must pass it explicitly — they read /root/.ssh otherwise).
   Direct SSH without the proxy fails `Operation not permitted` from the
   sandbox; always go through the wrapper. `/etc/atfl/atfl.env` is
   root-readable only: `sudo -n cat` through the wrapper.

   Raw recipe (reference only; the wrapper is the supported path):

   ```
   ssh -o ConnectTimeout=10 -o UserKnownHostsFile=/home/hatch/.ssh/known_hosts \
       -o ProxyCommand='nc -X connect -x hatch-egress-proxy:3130 %h %p' \
       -i /home/hatch/workspace/oracle-vm/free-micro-1 opc@100.120.196.47 ...
   ```

   If `Host key verification failed` after a host event:
   `/home/hatch/.ssh/known_hosts` may have been truncated (event #41: emptied
   to 0 bytes, key survived only in `known_hosts.old` as a hashed-name entry
   whose salt no longer matched the target name). Check the server's presented
   fingerprint (`ssh -v` → `Server host key: ssh-ed25519 SHA256:...`) against
   `ssh-keygen -lf /home/hatch/.ssh/known_hosts` — the known-good value is
   `SHA256:xQcIvsEHxfEt75w9t17hP6CONdiclYqCmbcs9R04fOw`. If it matches, the key
   is the genuine one; re-add it under the plain target name
   (`100.120.196.47 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK38xIWWctWYtILwA/8EOo35/k6kU+DmKe0NoRpjF43C`),
   never `-o StrictHostKeyChecking=no`.

## Integrity audit (host events can strand state)

Before declaring recovery complete, check what was mid-flight when the
replacement hit:

- Sent ledger `hidden_files/murph_relay_sent.log` — intact, tail dated pre-event.
- Thread pins `hidden_files/murph_relay_threads.json` — intact.
- `~/filedrops/murph/.outbox` — NO pending files (only `.receipts`); a
  pending file here would mean a send died mid-flight and needs review.
- Consumer log's last pre-event drain — should be a clean no-op; anything
  else is a thread to pull.

## Forensics notes

- **A turn record's header is not evidence of how the turn ended** (fogline-gm,
  #109): a host-killed turn wears the previous turn's outcome in its header
  (`exit: -9, 300.06s, timed_out: true` byte-identical to a genuine cap-kill).
  Cross-check the log's `RETRY ... exit -9 in 300.x s` and
  `RECOVERED ... from an unfinished turn` lines; a record that ends mid-write
  (no completion tail) is host-killed, not cap-killed.
- **A zero can be a damaged needle, not an absence** (#105/AGENTS.md): when a
  log scan returns 0 on a send known to exist, re-scan with a second predicate
  written differently before concluding it never sent.
- `sudo grep` output redacts `_TOKEN=<redacted>` even when the value is
  EMPTY — measure secret presence by length/count, never by reading through
  redaction (#111).
- **Trust the terminal result, not the interim poll** (2026-10-12, #38
  recovery): a `sleep`-prefixed backgrounded probe made `process.poll`
  report "running" after the child had already exited, misdirecting one
  verification cycle. Polls are snapshots; the completion report is the
  evidence.
- **The idle gate keys off `turns.created_at`, not the email send time**
  (2026-10-12): the 24h sweep rule fires on the first 5-min poll after
  latest turn row `created_at` + 24h. The Inkbox send time does not move
  the gate; a Neil reply inserts a player turn row and resets the window.

## Resting state reference (so "idle" reads as healthy)

| Signal | Healthy idle |
|---|---|
| step-0 changed | 0 |
| relay timer elapses | every 5 min each leg, rc=0, `sent=0 quarantined=0` |
| free-micro-1 poll cycles | every 5 min, `0 inbound processed, 0 handed off, 0 nudged` |
| `/var/lib/atfl/games/` | mailer.db + live game db + assets/ |
| repo tree | clean (`git status` empty) |
| verify_idle_turn.py | PENDING rc=0 outside the gate window |
| A8S inbox | empty (no reply from Ares to the dream-budget report yet) |
