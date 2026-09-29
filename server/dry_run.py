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

Exit codes: 0 = dry run finished and reported; 1 = a turn failed or the
leak check tripped (the report says which); 2 = startup/config refused.
"""

import argparse
import logging
import os
import sqlite3
import sys

from . import config, dispatch
from .gm import MockGM, RosterGM
from .turn_loop import _denylist

log = logging.getLogger("atfl.dry_run")


def _build_gm(cfg):
    if cfg["gm"] == "roster":
        return RosterGM(node_name=cfg["a8s_node"],
                        node_root=cfg["a8s_node_root"])
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
    args = ap.parse_args(argv)

    email = args.email.strip().lower()
    try:
        cfg = config.load()
    except config.ConfigError as e:
        log.error("startup refused: %s", e)
        return 2

    games_dir = args.games_dir or cfg["games_dir"]
    os.makedirs(games_dir, exist_ok=True)
    gm = _build_gm(cfg)
    print(f"GM backend   : {type(gm).__name__} (ATFL_GM={cfg['gm']})")
    print(f"games dir    : {games_dir}")
    print(f"scratch player: {email}")
    print("(no mail is sent by this tool; turn emails are rendered only)")

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
