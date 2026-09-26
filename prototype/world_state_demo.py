#!/usr/bin/env python3
"""Above the Fog Line — turn-loop driver (Phase 2 kickoff).

Exercises server.turn_loop.run_turn() — the shared five-step pipeline
from DESIGN.md §2.2 — against the fog-line-mystery-v1 seed, using the
MockGM. Runs three turns: a drink, an impossible feat (denied with a
partial effect), and an idle turn (to exercise the §2.4 catch-up lead),
then prints the mutations ledger and the §2.5 done-criteria check.

Run from the repo root: python3 prototype/world_state_demo.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.schema import create_db
from server.seed import seed
from server.gm import MockGM, DEMO_PLOT_PICK
from server.turn_loop import run_turn, verify_turn, TurnFailed


def main():
    db = create_db()
    seed(db, "GUID-FOG-0001", "neil@example.com")
    gm = MockGM()

    for player_input in [
        "I pick up the water bottle and drink deeply.",
        "I try to fell a dead tree for firewood.",
        "idle default",  # the GM drives with a conservative default (§1.3)
    ]:
        try:
            r = run_turn(db, player_input, gm)
        except TurnFailed as e:
            print(f"turn failed (no fiction sent): {e}")
            break
        print(f"--- turn {r.turn_no} (game clock {r.game_clock_start} -> {r.game_clock_end} min) ---")
        print(f'player: "{player_input}"')
        print(f"narrative: {r.narrative}\n")

    print("=== mutations ledger ===")
    for row in db.execute(
            "SELECT turn_id, entity_type, entity_id, field, old_value, new_value, cause FROM mutations"):
        print(f"  t{row[0]} {row[1]}#{row[2]} {row[3]}: {row[4]} -> {row[5]}  ({row[6][:60]})")

    print("\n=== §2.5 done-criteria check (per turn) ===")
    ok = True
    for (tid,) in db.execute("SELECT id FROM turns ORDER BY turn_no"):
        print(f"turn {tid}:")
        for name, state, note in verify_turn(db, tid):
            mark = "SKIP" if state == "skip" else ("ok" if state else "FAIL")
            if state is False:
                ok = False
            print(f"  [{mark}] {name} — {note}")

    g = dict(db.execute("SELECT * FROM games").fetchone())
    assert g["plot_concept"] == DEMO_PLOT_PICK, "plot pick not recorded"
    print(f"\nplot_concept={g['plot_concept']!r} · status={g['status']}")
    print("OK — turn-loop skeleton green." if ok else "CHECK FAILED")


if __name__ == "__main__":
    main()
