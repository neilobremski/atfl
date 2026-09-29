# RosterGM dry-run checklist (free-micro-1)

First-time flip of `ATFL_GM=mock` → `roster` on the game VM, with rollback.
Prerequisites: this file assumes the `fogline-gm` R4T roster has been created
(the roster now exists at `~/ar3/fogline-gm/r4t.md`, registered 2026-09-28 —
see note below), and a test Gmail identity is available
(standing OQ#1/#4 — the dry run sends real turn emails, so it needs a real
account; use a scratch game that sends only to Neil/the operator's own address).

**2026-09-28 (session #32):** the `fogline-gm` roster exists and answers on
the opencode free path (keeper/arbiter/critic, one turn at a time, rig
budget 8/hour). Machine note for the VM: the r4t worker PATH must contain
`opencode` — here fixed by symlinking `~/.opencode/bin/opencode` into
`~/.local/bin/` (first live turn failed with exit 127 until that landed);
the same fix (or PATH export) is needed on free-micro-1 under the `atfl`
user before the flip. The OpenCode sign-in precondition is retired —
opencode works with no login on the free path (verified 2026-09-28).

## 0. Preconditions (before touching the VM)

- [ ] The `fogline-gm` **a8s node is running** (`a8s start fogline-gm`) on
      the operator machine — tells are async a8s messages and are only
      processed while the node runs (verified 2026-09-28: a tell sent while
      the node was down sat in the S3 mailbox unprocessed). The same
      applies on free-micro-1: the roster node must be started (and
      restarted on boot) alongside the game server. The opencode rig must
      exist under the `atfl` user with `opencode` on the r4t worker PATH
      (Memory, 2026-09-28: Tailscale plan gives SSH once Neil runs the
      one-time Mac-side install; without it there is no terminal path to
      the VM).
- [ ] **Transport rework done** (session #34, hardened #36): `RosterGM`
      sends `a8s tell fogline-gm '<envelope>'` and polls the game server's
      own a8s mailbox (`atfl-server`, mailbox-only node) for keeper's reply
      (`a8s convo fogline-gm --from fogline-gm:keeper --json`, sender+
      timestamp matched in Python), up to a 30-min reply wait (roster wake
      latency is minutes-scale; the `r4t idle` dreaming pass can hold the
      single wake slot 15+ min). **Start-send-stop:** the S3 publish is done
      by the node's running daemon, so a never-started node never delivers
      (proven 2026-09-29) — the server must `a8s start atfl-server`, send,
      then `a8s stop atfl-server` before polling (a running daemon would
      consume the reply before the poll sees it). The `compose_narrative`
      envelope carries the approved adjudication array (fresh a8s threads
      per tell — keeper can't see the earlier tell). Node root goes in
      `RosterGM(node_root=...)`; send/poll halves are injectable for
      hermetic tests. The `a8s` binary is resolved by `server/gm.py`
      `_a8s_bin()` (ATFL_A8S_BIN → PATH → ~/.ar3/a8s) — no PATH export
      needed for the binary itself, but `a8s start/stop` needs the same
      resolution in the game server's environment.
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

1. Set `ATFL_GM=roster` in `/etc/atfl/atfl.env`. Restart the unit.
2. Dry-run the two-turn scratch game in-process, no email needed:
   `python -m server.dry_run --email <operator-own-address>`. It plays
   turns 1+2 through real dispatch, then reports outcome actions, ledger
   row counts, and a denylist leak check against both rendered emails.
   `dry run: PASS` is the gate for step 3; `FAIL` prints which side
   broke (missing roster / failed outcome / leak hits).
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
