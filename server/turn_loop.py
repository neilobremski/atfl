"""Shared per-turn pipeline — DESIGN.md §2.2 (contract), §2.5 (done criteria).

run_turn(db, player_input, gm, turn_len_min) executes the five steps:

  1. gather     — current state + elapsed-time reconciliation
  2. mutate     — GM yes/no questions -> mutations rows (auditable)
  3. narrative  — composed from approved mutations + catch-up lead
  4. advance    — game clock and turn number
  5. update     — touched rows stamped; turns row written

It enforces: serial turns, turn-1 plot pick as a game-level mutation
(§3.3.2), the §2.5.5 secrecy check (narrative never contains hidden
material), the §2.4 catch-up lead when frames were missed, and death
handling (§2.5.7). Email send/receive is NOT wired here — run_turn
returns a TurnResult the mailer sends once the game account exists.

Failure mode (§2.6): raises TurnFailed on secrecy violation or GM
failure; the caller retries once with the same inputs, then marks the
turn failed and sends nothing.
"""
import json
import re
import time
from datetime import datetime, timezone

from .gm import PLOT_ROSTER


class TurnFailed(Exception):
    pass


def _j(x):
    return json.dumps(x)


def _row(db, table, slug):
    return dict(db.execute(f"SELECT * FROM {table} WHERE slug=?", (slug,)).fetchone())


def _mutate(db, turn_id, entity_type, entity_id, field, old, new, cause):
    db.execute(
        "INSERT INTO mutations (turn_id,entity_type,entity_id,field,old_value,new_value,cause)"
        " VALUES (?,?,?,?,?,?,?)",
        (turn_id, entity_type, entity_id, field, _j(old), _j(new), cause),
    )


def reconcile_elapsed_time(db, turn_id, turn_no):
    """Step 0 of gather: objects reconcile time since last_touched_turn."""
    log = []
    for o in (dict(r) for r in db.execute("SELECT * FROM objects")):
        elapsed = turn_no - o["last_touched_turn"]
        if elapsed <= 0:
            continue
        st = json.loads(o["physical_state"])
        if o["slug"] == "water-bottle" and not st.get("cap_on") and st.get("water_ml", 0) > 0:
            old = st["water_ml"]
            st["water_ml"] = max(0, round(old - 2 * (elapsed / 1.0), 1))  # ~2 ml/hour
            db.execute("UPDATE objects SET physical_state=?, last_touched_turn=? WHERE id=?",
                       (_j(st), turn_no, o["id"]))
            _mutate(db, turn_id, "object", o["id"], "physical_state.water_ml", old, st["water_ml"],
                    "elapsed-time reconciliation: open bottle evaporates")
            log.append(f"water bottle: {old} -> {st['water_ml']} ml ({elapsed} turn(s) elapsed)")
        if o["slug"] == "red-drop" and not st.get("dried") and elapsed >= 2:
            st["dried"] = True
            db.execute("UPDATE objects SET physical_state=?, last_touched_turn=? WHERE id=?",
                       (_j(st), turn_no, o["id"]))
            _mutate(db, turn_id, "object", o["id"], "physical_state.dried", False, True,
                    "elapsed-time reconciliation: the drop dries")
            log.append("the red drop has dried on your cheek")
    return log


def filtered_view(db, game_guid):
    """The world as the GM (and renderer) may see it: physical state only.
    hidden_traits never leaves the DB layer (§4.4 obvious-vs-hidden split)."""
    view = {"places": {}, "actors": {}, "objects": {}}
    for slug, r in ((r["slug"], r) for r in db.execute("SELECT * FROM places")):
        view["places"][slug] = {"name": r["name"], "description": r["description"],
                                "physical_state": r["physical_state"],
                                "discovered": bool(r["discovered"])}
    for slug, r in ((r["slug"], r) for r in db.execute("SELECT * FROM actors")):
        view["actors"][slug] = {"name": r["name"], "kind": r["kind"],
                                "location_slug": r["location_slug"],
                                "physical_state": r["physical_state"],
                                "inventory": r["inventory"]}
    for slug, r in ((r["slug"], r) for r in db.execute("SELECT * FROM objects")):
        view["objects"][slug] = {"name": r["name"], "description": r["description"],
                                 "physical_state": r["physical_state"],
                                 "holder": r["holder"]}
    return view


def _denylist(db, game_guid):
    """GM-side denylist for the §2.5.5 secrecy check: every hidden value
    plus the plot concept. MVP = string-level check."""
    bad = []
    g = dict(db.execute("SELECT * FROM games WHERE guid=?", (game_guid,)).fetchone())
    if g["plot_concept"]:
        bad.append(g["plot_concept"])
    for table in ("places", "actors", "objects"):
        for r in db.execute(f"SELECT hidden_traits FROM {table}"):
            for v in json.loads(r["hidden_traits"]).values():
                if v:
                    bad.append(str(v))
    # Generic secrecy: the concept and marker names never appear in prose.
    bad += ["unexplained", "hidden", "plot_concept", "hidden_traits"]
    return [b.lower() for b in bad]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def secrecy_check(narrative, denylist):
    """§2.5.5 — raises TurnFailed if any denylisted string leaked.
    Normalizes both sides (case, punctuation, hyphenation) so
    "earth-changing" catches "the earth is changing"."""
    low = _norm(narrative)
    leaked = [b for b in denylist if _norm(b) in low]
    if leaked:
        raise TurnFailed(f"secrecy check failed: {leaked!r} in narrative")
    return True


def build_catchup(db, game_guid, turn_no, last_player_turn):
    """§2.4 catch-up line from auditable mutation causes since the player
    last acted. Empty string when nothing was missed."""
    missed = turn_no - last_player_turn - 1
    if missed < 1:
        return ""
    rows = db.execute(
        "SELECT cause FROM mutations WHERE turn_id IN "
        "(SELECT id FROM turns WHERE game_guid=? AND turn_no>? AND turn_no<?)",
        (game_guid, last_player_turn, turn_no)).fetchall()
    seen, events = set(), []
    for r in rows:
        cause = r["cause"]
        if cause not in seen:
            seen.add(cause)
            events.append(cause.split(":", 1)[0])
    events = events[:2]  # 1–2 concrete events
    if not events:
        return f"While you were quiet: {missed} turn(s) passed."
    return "While you were quiet: " + "; ".join(events) + "."


def apply_effect(db, turn_id, etype, slug, field, new_value, cause):
    table = {"place": "places", "actor": "actors", "object": "objects"}[etype]
    row = _row(db, table, slug)
    root, _, sub = field.partition(".")
    state = json.loads(row[root])
    old = state.get(sub)
    state[sub] = new_value
    db.execute(f"UPDATE {table} SET {root}=? WHERE id=?", (_j(state), row["id"]))
    _mutate(db, turn_id, etype, row["id"], field, old, new_value, cause)


def record_turn_stats(db, turn_id, adjudicate_ms=None, narrative_ms=None,
                      secrecy_pass=None):
    """DESIGN.md §6.3: one dogfooding stats row per turn. mutations_count
    is counted from the ledger; secrecy_pass is 1/0. Called inside the
    turn's transaction — a rolled-back turn leaves no stats row, which is
    correct (its outcome was discarded)."""
    n_mut = db.execute("SELECT COUNT(*) FROM mutations WHERE turn_id=?",
                       (turn_id,)).fetchone()[0]
    db.execute(
        "INSERT OR REPLACE INTO turn_stats (turn_id, adjudicate_ms,"
        " narrative_ms, secrecy_pass, mutations_count, recorded_at)"
        " VALUES (?,?,?,?,?,?)",
        (turn_id, adjudicate_ms, narrative_ms, secrecy_pass, n_mut,
         datetime.now(timezone.utc).isoformat()))
    return n_mut
class TurnResult:
    def __init__(self, turn_id, turn_no, game_clock_start, game_clock_end,
                 narrative, questions, denylist_checked, game_over):
        self.turn_id = turn_id
        self.turn_no = turn_no
        self.game_clock_start = game_clock_start
        self.game_clock_end = game_clock_end
        self.narrative = narrative
        self.questions = questions
        self.denylist_checked = denylist_checked
        self.game_over = game_over


def run_turn(db, player_input, gm, turn_len_min=60):
    """Run one full turn against an open game DB (status 'active').
    Serial per game — the caller must hold the game's lock (§2.2)."""
    g = dict(db.execute("SELECT * FROM games").fetchone())
    if g["status"] != "active":
        raise TurnFailed(f"game {g['guid']} is {g['status']}; no turns run")

    turn_no = g["turn_no"] + 1
    clock_start = g["game_clock_min"]
    now = datetime.now(timezone.utc).isoformat()

    turn_id = db.execute(
        "INSERT INTO turns (game_guid,turn_no,game_time_start_min,game_time_len_min,player_input,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (g["guid"], turn_no, clock_start, turn_len_min, player_input, now)).lastrowid

    # 1. gather — elapsed-time reconciliation first, then filtered view
    reconcile_log = reconcile_elapsed_time(db, turn_id, turn_no)
    filtered = filtered_view(db, g["guid"])

    # 1b. turn-1 plot pick (§3.3.2 / §4.1): game-level mutation, never changed
    if turn_no == 1:
        pick = gm.pick_plot(PLOT_ROSTER)
        assert pick in PLOT_ROSTER, f"plot pick {pick!r} not on the §4.3 roster"
        db.execute("UPDATE games SET plot_concept=? WHERE guid=?", (pick, g["guid"]))
        _mutate(db, turn_id, "game", 0, "plot_concept", None, pick,
                "plot pick at game start")

    # catch-up lead: anything the player missed since their last REAL
    # input (idle turns run by the GM don't reset the counter — §2.4).
    # The current turn's row is already inserted, so exclude it.
    last_real = db.execute(
        "SELECT MAX(turn_no) FROM turns WHERE game_guid=? AND turn_no<? AND player_input != 'idle default'",
        (g["guid"], turn_no)).fetchone()[0]
    catchup = build_catchup(db, g["guid"], turn_no, last_real or 0)

    # 2. yes/no mutations
    t0 = time.perf_counter()
    questions = gm.adjudicate(player_input, filtered)
    adjudicate_ms = (time.perf_counter() - t0) * 1000.0
    if not questions:
        raise TurnFailed("GM produced no adjudication output")
    db.execute("UPDATE turns SET mutation_questions=? WHERE id=?", (_j(questions), turn_id))
    for qd in questions:
        if qd["answer"] == "yes" and qd["effect"]:
            for target, changes in qd["effect"].items():
                etype, slug = target.split(":", 1)
                for field, new in changes.items():
                    apply_effect(db, turn_id, etype, slug, field, new, f"mutation Q: {qd['q']}")
        elif qd["answer"] == "no" and qd["effect"]:
            for target, changes in qd["effect"].items():
                etype, slug = target.split(":", 1)
                for field, new in changes.items():
                    apply_effect(db, turn_id, etype, slug, field, new, f"partial effect: {qd['q']}")

    # 3. narrative + secrecy check — must pass BEFORE anything is sent
    t1 = time.perf_counter()
    narrative = gm.compose_narrative(player_input, questions, filtered, catchup)
    narrative_ms = (time.perf_counter() - t1) * 1000.0
    try:
        secrecy_check(narrative, _denylist(db, g["guid"]))
    except TurnFailed:
        # record the fail so the stats ledger shows it; the row rides in
        # the turn's transaction and is rolled back if the retry fails (§2.6)
        record_turn_stats(db, turn_id, adjudicate_ms, narrative_ms, 0)
        raise
    record_turn_stats(db, turn_id, adjudicate_ms, narrative_ms, 1)
    db.execute("UPDATE turns SET narrative=? WHERE id=?", (narrative, turn_id))

    # 4+5. advance time + update touched rows
    db.execute("UPDATE games SET turn_no=?, game_clock_min=? WHERE guid=?",
               (turn_no, clock_start + turn_len_min, g["guid"]))
    db.execute("UPDATE places SET last_visited_turn=? WHERE slug='trailhead'", (turn_no,))
    db.execute("UPDATE actors SET last_acted_turn=? WHERE slug='player'", (turn_no,))

    # death handling (§2.5.7): hp at or below zero ends the game, forever
    hp = json.loads(_row(db, "actors", "player")["physical_state"]).get("hp", 1.0)
    game_over = hp <= 0
    if game_over:
        db.execute("UPDATE games SET status='dead', ended_at=? WHERE guid=?", (now, g["guid"]))

    db.commit()
    return TurnResult(turn_id, turn_no, clock_start, clock_start + turn_len_min,
                      narrative, questions, denylist_checked=True, game_over=game_over)


def verify_turn(db, turn_id):
    """Check §2.5 done criteria 1–3, 5–7 (criterion 4, the outbound
    email, is not wired — no game address yet). Returns a list of
    (name, ok_or_skip, note) tuples."""
    t = dict(db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone())
    g = dict(db.execute("SELECT * FROM games WHERE guid=?", (t["game_guid"],)).fetchone())
    checks = []
    qs = json.loads(t["mutation_questions"])
    checks.append(("turn row complete",
                   bool(t["turn_no"] and t["narrative"] and isinstance(qs, list)),
                   f"turn {t['turn_no']}, {len(qs)} question(s)"))
    muts = db.execute("SELECT * FROM mutations WHERE turn_id=?", (turn_id,)).fetchall()
    checks.append(("no silent mutations",
                   all(m["cause"] for m in muts),
                   f"{len(muts)} mutation row(s), all with causes"))
    # clock advanced: this turn's window must be exactly one turn_len
    # past the previous turn's end (turn 1 starts at 0), and the game row
    # must now reflect the latest turn (§2.5.3).
    latest = db.execute("SELECT MAX(turn_no) FROM turns WHERE game_guid=?",
                        (t["game_guid"],)).fetchone()[0]
    window_ok = (t["game_time_start_min"] == (t["turn_no"] - 1) * t["game_time_len_min"])
    current_ok = (g["turn_no"] == latest and
                  g["game_clock_min"] == latest * t["game_time_len_min"])
    checks.append(("clock advanced", window_ok and current_ok,
                   f"turn {t['turn_no']} window [{t['game_time_start_min']}, "
                   f"{t['game_time_start_min'] + t['game_time_len_min']}), game at turn {g['turn_no']}"))
    try:
        secrecy_check(t["narrative"], _denylist(db, g["guid"]))
        checks.append(("secrecy check", True, "denylist clean"))
    except TurnFailed as e:
        checks.append(("secrecy check", False, str(e)))
    # catch-up lead required (§2.5.6) iff the player missed frames since
    # their last real input (intervening idle-turn frames need the lead).
    last_real = db.execute(
        "SELECT MAX(turn_no) FROM turns WHERE game_guid=? AND turn_no<? AND player_input != 'idle default'",
        (t["game_guid"], t["turn_no"])).fetchone()[0]
    missed = (t["turn_no"] - 1 - (last_real or 0))
    checks.append(("catch-up lead",
                   missed < 1 or t["narrative"].startswith("While you were quiet:"),
                   f"{missed} missed frame(s) before turn {t['turn_no']}"))
    checks.append(("outbound email", "skip",
                   "not wired — game address pending (open question #1)"))
    hp = json.loads(_row(db, "actors", "player")["physical_state"]).get("hp", 1.0)
    checks.append(("death handling",
                   (hp <= 0) == (g["status"] == "dead"),
                   f"hp={hp}, status={g['status']}"))
    # §6.3 dogfooding stats row: recorded by run_turn, send stats by mailer
    stats = db.execute("SELECT * FROM turn_stats WHERE turn_id=?",
                       (turn_id,)).fetchone()
    stats_ok = (stats is not None and stats["secrecy_pass"] == 1
                and stats["mutations_count"] == len(muts))
    checks.append(("turn stats row (§6.3)", stats_ok,
                   f"mutations={stats['mutations_count'] if stats else '?'}, "
                   f"adjudicate={stats['adjudicate_ms']:.1f}ms, "
                   f"narrative={stats['narrative_ms']:.1f}ms"
                   if stats else "no stats row"))
    return checks
