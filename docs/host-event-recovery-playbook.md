# Host-event recovery playbook (Murph VM, sandbox side)

Consolidated 2026-10-05 from sessions #102, #103, #107, #109, #111
(~40 VM replacements observed and recovered; events #34–#40 in the Oct 4–5 window).

## What a host event does

A VM replacement (as distinct from a restart) wipes `/etc` and everything
installed system-wide outside `$HOME`. The persistent tree (`~/workspace`,
`~/.ar3`, `~/.config`, `~/.opencode`, `~/filedrops`) survives.

Confirmed casualties:
- `/etc/systemd/system/*.service`, `*.timer` — all three ATFL/a8s units
  (a8s-fogline-gm.service, a8s-murph.service, atfl-outbound-relay.service/.timer)
- pip installs outside the home tree — `cairosvg` (map_svg selftest dep) is
  the known recurring casualty; reinstall `2.9.1` with `pip --break-system-packages`
- `/etc/environment` is recreated at boot with the proxy vars (verified #103)

`/etc` losses are all covered by persistent copies in `~/workspace/a8s-systemd/`.

## Step 0: the per-session ritual (do this first, every session)

```
~/workspace/a8s-systemd/reinstall-a8s-units.sh   # idempotent; reports changed=N
```

- `changed=0` → no host event; skip to §3 checks.
- `changed=1` → full recovery below. (Event #39 hit ~1 minute after a
  recovery, wiping the just-reinstalled units — step 0 can legitimately run
  twice in one session.)

## Recovery order (after changed=1)

1. **Units** — the script above, then `sudo systemctl daemon-reload`,
   `enable --now` for the services + timer (the script handles it).
2. **pip deps** — `python3 -c "import cairosvg"`; if missing,
   `pip install --break-system-packages cairosvg==2.9.1`.
3. **A8S node** — `~/.ar3/a8s health` (bare `a8s` is NOT on PATH in worker
   shells; always the full path). Expect: `remote s3: OK`, storage OK,
   3 agents registered (atfl-server, fogline-gm, murph).
4. **Relay recovery** — the unit existing is NOT the relay working:
   wait for the timer's first post-restart elapse (every 10 min) and confirm
   BOTH legs:
   - `journalctl -u atfl-outbound-relay.service` shows Starting → Finished
     for the elapse;
   - `hidden_files/atfl_outbound_consumer.log` gains a line shaped
     `<UTC>T mode=send rc=0 :: scanned=N ignored=N would_send=0 sent=0
     replay_skipped=0 quarantined=0`
     (values vary; the *shape* — rc=0, quarantined=0 — is the signal).
5. **Final state check** — `playtest_watch.py`: pre-flight all OK,
   deploy parity `origin main == /srv/atfl/atfl`, relay elapses clean,
   staging none, start-chain 15/15, 0 games for masta@gibdon.com.
   On free-micro-1: `atfl` active, clean 5-min poll cycles,
   `/etc/atfl/atfl.env` = `ATFL_GM=roster, ATFL_COMPOSITE=v2, ATFL_IMAGES=off`,
   `/var/lib/atfl/games/` = mailer.db only (no games — correct resting state).

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

## Resting state reference (so "idle" reads as healthy)

| Signal | Healthy idle |
|---|---|
| step-0 changed | 0 |
| relay consumer drains | every 10 min, rc=0, `sent=0 quarantined=0` |
| free-micro-1 poll cycles | every 5 min, `0 inbound processed, 0 handed off, 0 nudged` |
| `/var/lib/atfl/games/` | mailer.db only |
| repo tree | clean (`git status` empty) |
| playtest_watch | exit 0, pre-flight all OK |
