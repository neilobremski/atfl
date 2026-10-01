"""Dry-run scratch game — Above the Fog Line server (Phase 4 / deploy tooling).

Runs a two-turn scratch game through the REAL dispatch path (signup ->
turn 1, one follow-up message -> turn 2) with the GM backend chosen by
ATFL_GM ('mock' or 'roster'), then reports outcome + a leak check.

This is the dry-run driver for deploy/roster-dry-run-checklist.md:
it exercises the turn loop end to end WITHOUT sending any email —
server.mailer is never touched. The player address is the operator's
own; nothing leaves this host except the roster tells (when ATFL_GM=roster).

Usage on the VM (from the checkout, with the systemd EnvironmentFile):

    export $(grep -v '^#' /etc/atfl/atfl.env | xargs)   # or however env lands
    /srv/atfl/venv/bin/python -m server.dry_run \
        --email neil@example.com [--signup-body "I look around"] \
        [--followup "I pick up the water bottle"] [--cleanup]

Flags:
    --email         scratch player's address (required; operator's own)
    --signup-body   text of the first message (default: "hello, who am I?")
    --followup      text of the second message (default: "I take the
                    water bottle and look down the trail.")
    --games-dir     where to put the scratch <GUID>.db (default: the
                    ATFL_GAMES_DIR from config)
    --cleanup       delete the scratch game DB at the end, printing the
                    final report first. Off by default so the operator
                    can inspect the DB afterwards.
    --resume [GUID] resume a dry run killed mid-turn (host reboot /
                    replacement): re-attaches to the in-flight roster
                    call recorded in <GUID>.pending.json instead of
                    re-sending it, and run_turn skips the steps the dead
                    attempt already recorded. With no GUID, exactly one
                    pending file must exist in the games dir. Only resume
                    AFTER the original process is confirmed dead — two
                    live drivers on one game will double-apply turns.

Exit codes: 0 = dry run finished and reported; 1 = a turn failed or the
leak check tripped (the report says which); 2 = startup/config refused.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys

from . import config, dispatch
from .dispatch import _turn_outcome, get_lock
from .gm import MockGM, RosterGM
from .turn_loop import _denylist

log = logging.getLogger("atfl.dry_run")


def _build_gm(cfg, pending_dir=None):
    if cfg["gm"] == "roster":
        return RosterGM(node_name=cfg["a8s_node"],
                        node_root=cfg["a8s_node_root"],
                        pending_dir=pending_dir)
    return MockGM()


def _leak_check(db, guid, text):
    """True when none of the denylist strings appear in the turn output."""
    text_l = (text or "").lower()
    hits = [b for b in _denylist(db, guid) if b and b in text_l]
    return hits


def _ledger_count(db):
    try:
        return db.execute("SELECT COUNT(*) FROM mutations").fetchone()[0]
    except sqlite3.Error:
        return 0


def _turn_report(tag, outcome, db, guid):
    body = (outcome.body or "") + "\n" + (outcome.html or "")
    hits = _leak_check(db, guid, body)
    print(f"\n--- {tag} ---")
    print(f"outcome action: {outcome.action}")
    print(f"turn no      : {outcome.turn_no}")
    print(f"note         : {outcome.note or '(none)'}")
    print(f"ledger rows  : {_ledger_count(db)}")
    if outcome.action == "turn_email":
        print(f"subject      : {outcome.subject}")
    if hits:
        print(f"LEAK CHECK FAILED: {hits}")
    else:
        print("leak check   : clean")
    return hits


def _resume(args, cfg, games_dir, gm, email):
    """Resume a dry run killed mid-turn. The pending file names the crashed
    game and turn; _turn_outcome -> run_turn reuses the dead attempt's open
    turn row, and the RosterGM re-attaches to the recorded in-flight call
    instead of re-sending it. A resumed turn 1 that completes continues
    into a fresh turn 2, mirroring the normal flow."""
    pendings = sorted(f for f in os.listdir(games_dir)
                      if f.endswith(".pending.json"))
    want = args.resume if isinstance(args.resume, str) else None
    if want:
        name = f"{want.strip().lower()}.pending.json"
        if name not in pendings:
            print(f"no pending file for GUID {want} in {games_dir}")
            return 2
        pendings = [name]
    elif len(pendings) != 1:
        print(f"--resume needs exactly one <guid>.pending.json in "
              f"{games_dir}; found {len(pendings)}"
              + (f": {', '.join(p[:-13] for p in pendings)}" if pendings
                 else "") + " — pass a GUID to choose.")
        return 2
    pending_path = os.path.join(games_dir, pendings[0])
    try:
        with open(pending_path) as f:
            pending = json.load(f)
    except (OSError, ValueError) as e:
        print(f"cannot read {pending_path}: {e}")
        return 2
    guid, turn_no = pending["game_guid"], pending["turn_no"]
    print(f"resuming game {guid} turn {turn_no} "
          f"(in-flight call: {pending['call']}, sent {pending['sent_at']})")
    db_path = os.path.join(games_dir, f"{guid}.db")
    if not os.path.isfile(db_path):
        print(f"game DB missing: {db_path} — nothing to resume")
        return 2
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    game = db.execute("SELECT * FROM games WHERE guid=?", (guid,)).fetchone()
    if game is None:
        print(f"no game row for {guid} — nothing to resume")
        db.close()
        return 2
    sender = game["player_email"]
    if sender != email:
        print(f"(note: game belongs to {sender}; using the game's address)")

    def run_crashed_turn(tag, player_input):
        try:
            with get_lock(guid):
                outcome = _turn_outcome(db, sender, gm, player_input, guid,
                                        cfg["turn_len_min"])
        except Exception as e:
            print(f"\n{tag} CRASHED before an outcome: "
                  f"{type(e).__name__}: {e}")
            return None
        return outcome

    leaks = []
    outcomes = []
    if turn_no == 1:
        outcome1 = run_crashed_turn("turn 1 (signup, resumed)",
                                    args.signup_body)
        if outcome1 is None:
            db.close()
            return 1
        leaks += _turn_report("turn 1 (signup, resumed)", outcome1, db, guid)
        outcomes.append(outcome1)
        if outcome1.action != "turn_email":
            print("\nresumed turn 1 did not complete; turn 2 not started")
            db.close()
            return 1
        # The crashed turn is over: its in-flight call either completed
        # (RosterGM._call clears the file) or never went through a
        # pending-tracking GM (mock). Either way the record is stale now —
        # a fresh turn 2 must not trip over it.
        try:
            os.remove(pending_path)
        except OSError:
            pass
        next_turn_no = 2
    elif turn_no == 2:
        next_turn_no = 2
    else:
        print(f"unexpected crashed turn_no {turn_no}; not resuming")
        db.close()
        return 2

    if next_turn_no == 2 and (turn_no == 2 or outcomes):
        body2 = f"{args.followup}\n\n[guid:{guid}]"
        tag = "turn 2 (follow-up, resumed)" if turn_no == 2 else "turn 2 (follow-up)"
        outcome2 = run_crashed_turn(tag, body2)
        if outcome2 is None:
            db.close()
            return 1
        leaks += _turn_report(tag, outcome2, db, guid)
        outcomes.append(outcome2)
        if outcome2.action == "turn_email":
            try:
                os.remove(pending_path)
            except OSError:
                pass
    db.close()

    ok = all(o.action == "turn_email" for o in outcomes) and not leaks
    print(f"\nscratch guid : {guid}")
    print(f"scratch db   : {db_path}")
    print(f"dry run (resumed): {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dry-run a two-turn scratch game.")
    ap.add_argument("--email", required=True,
                    help="scratch player's address (operator's own)")
    ap.add_argument("--signup-body", default="hello, who am I?",
                    help="text of the first (signup) message")
    ap.add_argument("--followup",
                    default="I take the water bottle and look down the trail.",
                    help="text of the second message")
    ap.add_argument("--games-dir", default=None,
                    help="override ATFL_GAMES_DIR for the scratch DB")
    ap.add_argument("--cleanup", action="store_true",
                    help="delete the scratch game DB after the report")
    ap.add_argument("--resume", nargs="?", const=True, default=False,
                    metavar="GUID",
                    help="resume a crashed dry run (see module docstring); "
                         "optional GUID selects among several pending files")
    args = ap.parse_args(argv)

    email = args.email.strip().lower()
    try:
        cfg = config.load()
    except config.ConfigError as e:
        log.error("startup refused: %s", e)
        return 2

    games_dir = args.games_dir or cfg["games_dir"]
    os.makedirs(games_dir, exist_ok=True)
    gm = _build_gm(cfg, pending_dir=games_dir)
    print(f"GM backend   : {type(gm).__name__} (ATFL_GM={cfg['gm']})")
    print(f"games dir    : {games_dir}")
    print(f"scratch player: {email}")
    print("(no mail is sent by this tool; turn emails are rendered only)")

    if args.resume:
        return _resume(args, cfg, games_dir, gm, email)

    # Turn 1 — the signup path, exactly as a real first email would take it.
    try:
        outcome1 = dispatch.new_game(games_dir, email, args.signup_body,
                                     gm, cfg["turn_len_min"])
    except Exception as e:
        print(f"\nTURN 1 CRASHED before an outcome: {type(e).__name__}: {e}")
        return 1
    guid = outcome1.guid
    db_path = os.path.join(games_dir, f"{guid}.db")
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    leaks = _turn_report("turn 1 (signup)", outcome1, db, guid)

    # Turn 2 — a real follow-up message, GUID-carried like a player reply.
    subj = f"Re: fog line [{guid[:8]}]"
    body2 = f"{args.followup}\n\n[guid:{guid}]"
    try:
        outcome2 = dispatch.dispatch_message(
            games_dir, email, subj, body2, gm, cfg["turn_len_min"])
    except Exception as e:
        print(f"\nTURN 2 CRASHED before an outcome: {type(e).__name__}: {e}")
        db.close()
        return 1
    leaks += _turn_report("turn 2 (follow-up)", outcome2, db, guid)
    db.close()

    ok = (outcome1.action == "turn_email" and outcome2.action == "turn_email"
          and not leaks)
    print(f"\nscratch guid : {guid}")
    print(f"scratch db   : {db_path}")
    if args.cleanup:
        try:
            os.remove(db_path)
            print("scratch db deleted (--cleanup)")
        except OSError as e:
            print(f"could not delete scratch db: {e}")
    else:
        print("(kept for inspection; delete it when done: it is a real game DB)")
    print(f"dry run: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
