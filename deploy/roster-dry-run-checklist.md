# RosterGM dry-run checklist (free-micro-1)

First-time flip of `ATFL_GM=mock` → `roster` on the game VM, with rollback.
Prerequisites: this file assumes the `fogline-gm` R4T roster has been created
(the roster now exists at `~/ar3/fogline-gm/r4t.md`, registered 2026-09-28 —
see note below), and a real inbound identity is available for the dry run
(post-relay: the dry run sends real turn emails through the Murph relay, so
it needs the operator's own address — use a scratch game that sends only to
the operator's own address).

**2026-10-03 (session #96):** the flip was done and verified clean — see the
"Flip record" section below. The preconditions were updated for the relay era:
the Gmail identity line is retired (the drill uses the operator's own
Inkbox mailbox), and the roster daemon location is clarified (it runs on the
operator VM via `a8s-fogline-gm.service`, reachable from free-micro-1 through
the shared S3 mailbox — the roster does NOT run on free-micro-1).

**2026-09-28 (session #32):** the `fogline-gm` roster exists and answers on
the opencode free path (keeper/arbiter/critic, one turn at a time, rig
budget 8/hour). Machine note for the VM: the r4t worker PATH must contain
`opencode` — here fixed by symlinking `~/.opencode/bin/opencode` into
`~/.local/bin/` (first live turn failed with exit 127 until that landed);
the same fix (or PATH export) is needed on free-micro-1 under the `atfl`
user before the flip. The OpenCode sign-in precondition is retired —
opencode works with no login on the free path (verified 2026-09-28).

## 0. Preconditions (before touching the VM)

- [ ] The `fogline-gm` **a8s node daemon is running and reachable** — tells
      are async a8s messages processed by the daemon that owns the
      `fogline-gm` mailbox, and a tell sent while no daemon serves that
      mailbox sits in the S3 mailbox unprocessed (verified 2026-09-28).
      Current topology (2026-10-03): the daemon is
      `a8s-fogline-gm.service` on the operator VM, started on boot and
      reinstalled by the step-0 helper after host events; free-micro-1
      reaches it through the shared S3 mailbox — no roster process runs
      on free-micro-1, and no opencode/r4t install is needed under the
      `atfl` user there. (The 2026-09-28 plan to start the roster node on
      free-micro-1 is superseded.)
- [ ] **Start-send-stop is code-enforced** (session #40; was operator
      procedure before): `RosterGM._send_real` starts the `atfl-server`
      node, sends the tell from `node_root`, then stops the node in a
      finally before the reply poll — because `a8s tell` only *records*
      the outbox file (the S3 publish is the running daemon's job) and a
      running daemon consumes inbound before `a8s convo` sees it (both
      proven 2026-09-29). Start/tell/stop failures each raise TurnFailed
      loudly; a missing `node_root` fails before any subprocess runs.
      `node_name`/`node_root` come from `ATFL_A8S_NODE` /
      `ATFL_A8S_NODE_ROOT` in `/etc/atfl/atfl.env` (startup refuses a
      roster backend without the root). The `a8s` binary is resolved by
      `server/gm.py` `_a8s_bin()` (ATFL_A8S_BIN → PATH → ~/.ar3/a8s) for
      all three verbs — no PATH export needed. Pinned hermetically
      (`prototype/roster_demo.py` §12, 16 checks). The `fogline-gm`
      roster node itself must be started on the VM (and on boot) —
      tells are only processed while it runs (verified 2026-09-28) —
      with the opencode rig under the `atfl` user and `opencode` on the
      r4t worker PATH (Memory, 2026-09-28: Tailscale plan gives SSH once
      Neil runs the one-time Mac-side install; without it there is no
      terminal path to the VM).
- [ ] Repo green locally: all `prototype/*_demo.py` pass, `poll --fake` smoke
      clean. RosterGM behavior is pinned hermetically
      (`prototype/roster_demo.py`, 53 checks).
- [ ] `~/ar3/fogline-gm/r4t.md` landed with the hard rules (answer yes/no,
      bare JSON for adjudication, never see hidden state) — draft lines are in
      `research/phase4-truth-rule-worked-examples.md`, §runbook sketch.

## 1. Deploy the code

1. On the VM as `atfl` (per deploy/README.md): `git pull --ff-only`,
   reinstall venv from `deploy/requirements.txt`, confirm nothing changed in
   `deploy/atfl.service` that needs `systemd-analyze verify` first.
2. Keep `ATFL_GM=mock` in `/etc/atfl/atfl.env` for now. Restart
   (`systemctl restart atfl`), one poll cycle, check `journalctl -u atfl`
   shows the mock turn loop healthy — this proves the deploy itself is sound
   before the backend changes.

## 2. Point the rig at the roster, run the dry game

1. Set `ATFL_GM=roster` in `/etc/atfl/atfl.env` (with `ATFL_A8S_NODE`
   / `ATFL_A8S_NODE_ROOT` from the README — startup refuses without the
   root). Restart the unit.
2. Dry-run the two-turn scratch game in-process, no email needed:
   `python -m server.dry_run --email <operator-own-address>`. It plays
   turns 1+2 through real dispatch, then reports outcome actions, ledger
   row counts, and a denylist leak check against both rendered emails.
   `dry run: PASS` is the gate for step 3; `FAIL` prints which side
   broke (missing roster / failed outcome / leak hits).
   Crash resume: if the host dies mid-run (the keeper's reply waits are
   hours long), re-run with `--resume [GUID]`. The crashed turn's DB
   writes were never committed (run_turn commits once, at the end), so
   uncommitted steps are re-derived: the resumed run re-sends the pick
   and any later calls fresh, each correlated on its own sent_at plus
   the reply-shape gate, so stale answers to dead sends are skipped
   (live-verified 2026-10-01: a crash during turn-1 adjudicate re-derived
   the pick and re-sent it — the `<GUID>.pending.json` re-attach only
   fires when the resumed path reaches the exact recorded (call, turn_no)
   with the record intact; in practice that is the step that was in
   flight when nothing earlier needed re-deriving). Never `--resume`
   while the original process is still alive. Launch with `python3 -u`
   (unbuffered) so the log streams while the run is in flight — a
   redirected stdout otherwise buffers and `tail` shows nothing until
   the process ends.
3. Seed a scratch game for the operator's own address only
   (`server/seed.py fog-line-mystery-v1`), so no outsider ever sees a turn.
4. Let one poll cycle produce turn 1. Watch the log:
   - two tells fire in order: `adjudicate` (bare JSON), then
     `compose_narrative` (≤2000 words);
   - the envelope went to the roster, not the DB — nothing hidden leaked
     (roster_demo §schema checks already prove the envelope builder drops
     hidden_traits/plot_concept; this is the live confirmation);
   - a real turn email arrives at the scratch address with GUID, text render,
     and images per `ATFL_IMAGES`.

## 3. Failure drills (the loud channel works)

The whole point of the flip: verify every roster failure becomes a TurnFailed
with no silent partial state (session #29). Expect dispatch §2.6 to retry
once and then record a clean failed outcome with nothing sent:

- [ ] Roster missing / `r4t` errors: expect `roster call failed` →
      TurnFailed → retry → turn dies, logged. The unit stays up.
- [ ] Malformed adjudication JSON: same path; verify no partial turn row,
      no ledger rows, world untouched (roster_demo §7b pins this).
- [ ] Hallucinated slug in a mutation (e.g. `object:mara-lantern`): expect
      TurnFailed on the commit-side existence check, not a TypeError — this
      was a real gap fixed in session #29; watch for the loud failure, not
      the crash.
- [ ] Narrative leaking the plot concept: `secrecy_check` must catch it
      before send (roster_demo §secrecy pins this).

## 4. Acceptance bar (before any real playtest)

- [ ] 3 consecutive scratch turns complete with no failed outcomes.
- [ ] Every mutation the roster proposed landed in the ledger with its cause.
- [ ] The k7e-vs-SQLite rule held on any conflict: DB won (ledger shows why).

## Flip record (2026-10-03, session #96)

- Flipped `ATFL_GM=mock` → `roster` in `/etc/atfl/atfl.env` on free-micro-1
  (`sudo sed`, key verified `ATFL_GM=roster`), `sudo systemctl restart atfl`.
  No deployed-code change was needed: the VM checkout is at fe6b24b and
  `server/{gm,poll,config,turn_loop}.py` are md5-identical to local main.
- Verified clean: two consecutive 5-min poll cycles post-restart, each
  "0 inbound processed, 0 handed off, 0 nudged"; config refused nothing and
  RosterGM constructed fine (env keys ATFL_A8S_NODE=atfl-server,
  ATFL_A8S_NODE_ROOT=/srv/atfl/a8s/atfl-server, ATFL_A8S_AGENTS_DIR set;
  `a8s` resolves to `/srv/atfl/.ar3/a8s` via the ~/.ar3 fallback).
- The flip is currently INERT: `/var/lib/atfl/games/` holds mailer.db only
  (no game DBs), so no roster calls fire and no tells leave free-micro-1.
  The server is now playtest-ready per DESIGN.md §6.1 (a real GM behind the
  pipeline); the mock drills (#87–#93) all ran pre-flip.
- Still UNPROVEN: the cross-host roster leg — `a8s tell` from free-micro-1's
  transient atfl-server node to the fogline-gm mailbox, and the reply read
  back — has never fired in production. RosterGM was dry-run green from the
  operator VM (dryrun9, game ae90322a, PASS), and the reply mechanics
  (send-and-return, in-flight re-attach, REPLY_WAIT_S=7200 with one reprompt)
  are built for multi-hour legs, but the first production turn is still a
  live test. DELIBERATE: no synthetic roster turn was started in #96 — a
  real roster turn spans hours of keeper work (legs landed 34–69 min after
  send in the dry runs), and injecting a synthetic game while keeper's
  verification experiments are active risks interference. When Neil sends
  "write start", his real game IS the drill; until then the flipped-idle
  state is the correct resting state.
- Rollback is the §5 one-liner; nothing else changes between backends
  (state lives in SQLite, not the model).

## 5. Rollback

- Set `ATFL_GM=mock`, restart, run one poll cycle. The mock GM is the dev
  default and always a safe fallback — the two backends are swappable per
  turn because the state lives in SQLite, not the model.

## Notes

- The roster never writes the DB and never sends mail: all of that is the
  turn loop's job. If a drill shows otherwise, that's a bug, not a tuning
  problem — stop and report, don't keep turning knobs.
- Token cost is irrelevant at ~1 turn/day; latency of the 300s tell timeout
  is irrelevant for the same reason. Do not "optimize" the calling
  convention (two tells) to save calls — Option A was chosen for clean
  failure attribution (phase4-gm-integration.md §convention).
