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

    print("\n=== §6.3 per-turn stats (dogfooding) ===")
    for row in db.execute(
            "SELECT turn_id, adjudicate_ms, narrative_ms, secrecy_pass,"
            " mutations_count, send_ms, email_sent_at FROM turn_stats ORDER BY turn_id"):
        print(f"  turn {row[0]}: adjudicate={row[1]:.2f}ms narrative={row[2]:.2f}ms "
              f"secrecy={'pass' if row[3] else 'FAIL'} mutations={row[4]} "
              f"send_ms={row[5]} sent_at={row[6]} (not wired — no game address yet)")

    g = dict(db.execute("SELECT * FROM games").fetchone())
    assert g["plot_concept"] == DEMO_PLOT_PICK, "plot pick not recorded"
    print(f"\nplot_concept={g['plot_concept']!r} · status={g['status']}")

    print("\n=== death path (§2.5.7) ===")
    # Death is checked after a turn completes: set hp to 0, run one last
    # turn, and the game must end forever.
    db.execute("UPDATE actors SET physical_state=? WHERE slug='player'",
               ('{"hp": 0, "fatigue": 0, "hunger": 0}',))
    db.commit()
    r = run_turn(db, "I throw myself off the trail into the fog.", gm)
    g = dict(db.execute("SELECT * FROM games").fetchone())
    assert r.game_over and g["status"] == "dead" and g["ended_at"], \
        f"death not recorded: status={g['status']}"
    turns_before = db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    try:
        run_turn(db, "one more message", gm)
        raise AssertionError("run_turn ran on a dead game")
    except TurnFailed:
        pass
    turns_after = db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    assert turns_before == turns_after, "dead game gained a turn"
    nudge_ok = db.execute(
        "SELECT COUNT(*) FROM mutations WHERE cause LIKE '%nudge%'").fetchone()[0]
    print(f"  death recorded: status=dead, ended_at set; "
          f"no further turns run ({turns_after} total)")

    print("OK — turn-loop skeleton green." if ok else "CHECK FAILED")


if __name__ == "__main__":
    main()
