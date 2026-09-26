"""Above the Fog Line — Phase 2 MVP server package.

The turn loop lives here. `turn_loop.run_turn()` is the shared per-turn
pipeline (DESIGN.md §2.2, five steps), driven by a GameMaster interface
so the mock GM can be swapped for the real R4T roster model later without
touching the loop. Email send/receive is NOT wired here yet — run_turn
returns a TurnResult the mailer will send once the game's Google account
exists (open question #1).
"""
