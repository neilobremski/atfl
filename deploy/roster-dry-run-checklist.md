# RosterGM dry-run checklist (free-micro-1)

First-time flip of `ATFL_GM=mock` → `roster` on the game VM, with rollback.
Prerequisites: this file assumes the `fogline-gm` R4T roster has been created
with the OpenCode path (standing OQ#2), and a test Gmail identity is available
(standing OQ#1/#4 — the dry run sends real turn emails, so it needs a real
account; use a scratch game that sends only to Neil/the operator's own address).

## 0. Preconditions (before touching the VM)

- [ ] `r4t tell fogline-gm` answers from the opencode rig on the operator
      machine — the same rig must exist on free-micro-1 under the `atfl` user.
      (Memory, 2026-09-28: Tailscale plan gives SSH once Neil runs the one-time
      Mac-side install; without it there is no terminal path to the VM.)
- [ ] Repo green locally: all `prototype/*_demo.py` pass, `poll --fake` smoke
      clean. RosterGM behavior is already pinned hermetically
      (`prototype/roster_demo.py`, 48 checks).
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
2. Seed a scratch game for the operator's own address only
   (`server/seed.py fog-line-mystery-v1`), so no outsider ever sees a turn.
3. Let one poll cycle produce turn 1. Watch the log:
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
