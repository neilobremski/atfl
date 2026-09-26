#!/usr/bin/env python3
"""Above the Fog Line — world-state schema demo (Phase 0 prototype).

Creates the schema from research/phase0-world-state-schema.md, seeds the
opening scene (trail above the fog line, player, water bottle, red drop),
then runs ONE turn of the agentic flow:

  1. gather        (place + actors + objects; reconcile elapsed time)
  2. yes/no mutations (adjudicate the player's email input)
  3. narrative
  4. advance time
  5. update

A mock GM function stands in for the LLM; the schema operations are real.
Run: python3 prototype/world_state_demo.py
"""
import json
import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE games (
    guid TEXT PRIMARY KEY,
    player_email TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    plot_concept TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    turn_no INTEGER NOT NULL DEFAULT 0,
    game_clock_min INTEGER NOT NULL DEFAULT 0,
    started_at TEXT, ended_at TEXT
);
CREATE TABLE places (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    physical_state TEXT NOT NULL DEFAULT '{}',
    hidden_traits TEXT NOT NULL DEFAULT '{}',
    discovered INTEGER NOT NULL DEFAULT 1,
    last_visited_turn INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE actors (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    is_player INTEGER NOT NULL DEFAULT 0,
    location_slug TEXT NOT NULL,
    physical_state TEXT NOT NULL DEFAULT '{}',
    hidden_traits TEXT NOT NULL DEFAULT '{}',
    inventory TEXT NOT NULL DEFAULT '{}',
    ai_driver INTEGER NOT NULL DEFAULT 0,
    last_acted_turn INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE objects (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    physical_state TEXT NOT NULL DEFAULT '{}',
    hidden_traits TEXT NOT NULL DEFAULT '{}',
    holder TEXT NOT NULL,
    last_touched_turn INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE turns (
    id INTEGER PRIMARY KEY,
    game_guid TEXT NOT NULL,
    turn_no INTEGER NOT NULL,
    game_time_start_min INTEGER NOT NULL,
    game_time_len_min INTEGER NOT NULL,
    player_input TEXT,
    mutation_questions TEXT NOT NULL DEFAULT '[]',
    narrative TEXT,
    created_at TEXT
);
CREATE TABLE mutations (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    cause TEXT
);
CREATE TABLE assets (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    turn_created INTEGER NOT NULL,
    prompt TEXT,
    prompt_hash TEXT
);
"""

EMPTY_INVENTORY = {"hands": [None, None], "backpack": [None] * 8}


def j(x):
    return json.dumps(x)


def seed(db):
    c = db.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute(
        "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)",
        ("GUID-FOG-0001", "neil@example.com", "fog-line-mystery-v1",
         "a spell — the fog is a boundary, not weather", "active", 0, 0, now, None),
    )
    c.execute(
        "INSERT INTO places (slug,name,description,physical_state,hidden_traits) VALUES (?,?,?,?,?)",
        ("trailhead", "Trail above the fog line",
         "A narrow trail cresting a ridge. Below, an ocean of fog stretches to the horizon.",
         j({"fog_density": 0.9, "light": "dusk", "temp_c": 8, "wind": "light"}),
         j({"ward_line": True, "ward_note": "nothing from below has crossed in 40 years"})),
    )
    c.execute(
        "INSERT INTO actors (slug,name,kind,is_player,location_slug,physical_state,hidden_traits,inventory) VALUES (?,?,?,?,?,?,?,?)",
        ("player-neil", "You", "player", 1, "trailhead",
         j({"hp": 1.0, "hunger": 0.2, "fatigue": 0.3, "wetness": 0.0, "pose": "standing", "facing": "down-trail"}),
         j({"plot_hook": "marked — the red drop chose him"}),
         j(EMPTY_INVENTORY)),
    )
    c.execute(
        "INSERT INTO objects (slug,name,description,physical_state,hidden_traits,holder) VALUES (?,?,?,?,?,?)",
        ("water-bottle", "Water bottle",
         "A half-full bottle sitting on an otherwise empty trail.",
         j({"water_ml": 400, "cap_on": False, "tipped": False}),
         j({}), "place:trailhead"),
    )
    c.execute(
        "INSERT INTO objects (slug,name,description,physical_state,hidden_traits,holder) VALUES (?,?,?,?,?,?)",
        ("red-drop", "A red drop",
         "A single red drop on the player's cheek. It wasn't raining.",
         j({"volume_ml": 0.05, "dried": False}),
         j({"origin": "not blood, not rain — the fog condensing wrong"}),
         "actor:player-neil:hands[0]"),
    )
    db.commit()


def get(db, table, slug):
    row = db.execute(f"SELECT * FROM {table} WHERE slug=?", (slug,)).fetchone()
    return dict(row)


def mutate(db, turn_id, entity_type, entity_id, field, old, new, cause):
    db.execute(
        "INSERT INTO mutations (turn_id,entity_type,entity_id,field,old_value,new_value,cause) VALUES (?,?,?,?,?,?,?)",
        (turn_id, entity_type, entity_id, field, j(old), j(new), cause),
    )


def reconcile_elapsed_time(db, turn_id, turn_no):
    """Step 0 of gather: objects reconcile elapsed time since last touched."""
    log = []
    for row in db.execute("SELECT * FROM objects"):
        o = dict(row)
        elapsed = turn_no - o["last_touched_turn"]
        if elapsed <= 0:
            continue
        st = json.loads(o["physical_state"])
        if o["slug"] == "water-bottle" and not st.get("cap_on") and st.get("water_ml", 0) > 0:
            # ~2 ml/hour evaporates from an open bottle
            old = st["water_ml"]
            st["water_ml"] = max(0, round(old - 2 * (elapsed / 1.0), 1))
            db.execute("UPDATE objects SET physical_state=?, last_touched_turn=? WHERE id=?",
                       (j(st), turn_no, o["id"]))
            mutate(db, turn_id, "object", o["id"], "physical_state.water_ml", old, st["water_ml"],
                   "elapsed-time reconciliation: open bottle evaporates")
            log.append(f"  [reconcile] water bottle: {old} -> {st['water_ml']} ml ({elapsed} turn(s) elapsed)")
        if o["slug"] == "red-drop" and not st.get("dried") and elapsed >= 2:
            st["dried"] = True
            db.execute("UPDATE objects SET physical_state=?, last_touched_turn=? WHERE id=?",
                       (j(st), turn_no, o["id"]))
            mutate(db, turn_id, "object", o["id"], "physical_state.dried", False, True,
                   "elapsed-time reconciliation: the drop dries")
            log.append("  [reconcile] the red drop has dried on your cheek")
    return log


def mock_gm_adjudicate(player_input, gathered):
    """Stand-in for the LLM GM: emit yes/no mutation questions."""
    bottle = gathered["objects"]["water-bottle"]
    water = json.loads(bottle["physical_state"])["water_ml"]
    qs = []
    text = player_input.lower()
    if "drink" in text:
        can = water >= 50
        qs.append({
            "q": "Can the player drink from the bottle?",
            "answer": "yes" if can else "no",
            "rationale": f"bottle holds {water} ml; a mouthful needs ~50 ml",
            "effect": {"object:water-bottle": {"physical_state.water_ml": water - 150},
                       "actor:player-neil": {"physical_state.hunger": 0.1}} if can else None,
        })
    if "fell" in text or "chop" in text or "tree" in text:
        fatigue = json.loads(gathered["actors"]["player-neil"]["physical_state"])["fatigue"]
        qs.append({
            "q": "Is the player strong enough to fell a tree?",
            "answer": "no",
            "rationale": f"fatigue {fatigue}, no axe in inventory — only chips it",
            "effect": {"place:trailhead": {"physical_state.ground_wetness": "bark chips scattered"}},
        })
    if not qs:
        qs.append({"q": "Does anything change?", "answer": "no",
                   "rationale": "the player only looks around", "effect": None})
    return qs


def apply_effect(db, turn_id, entity_type, slug, field, new_value, cause):
    table = {"place": "places", "actor": "actors", "object": "objects"}[entity_type]
    row = get(db, table, slug)
    root, _, sub = field.partition(".")
    state = json.loads(row[root])
    old = state.get(sub)
    # walk dotted leftovers simply: only one level deep in this demo
    state[sub] = new_value
    db.execute(f"UPDATE {table} SET {root}=? WHERE id=?", (j(state), row["id"]))
    mutate(db, turn_id, entity_type, row["id"], field, old, new_value, cause)


def run_turn(db, player_input, turn_len_min=60):
    g = dict(db.execute("SELECT * FROM games").fetchone())
    turn_no = g["turn_no"] + 1
    t0 = time.time()
    cur = db.execute(
        "INSERT INTO turns (game_guid,turn_no,game_time_start_min,game_time_len_min,player_input,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (g["guid"], turn_no, g["game_clock_min"], turn_len_min, player_input,
         datetime.now(timezone.utc).isoformat()),
    )
    turn_id = cur.lastrowid
    print(f"--- turn {turn_no} (game clock {g['game_clock_min']} -> {g['game_clock_min']+turn_len_min} min) ---")
    print(f'player: "{player_input}"\n')

    # 1. gather
    place = get(db, "places", "trailhead")
    actor = get(db, "actors", "player-neil")
    objs = {r["slug"]: dict(r) for r in db.execute("SELECT * FROM objects")}
    print("[gather] place=trailhead, actors=player, objects=" + ", ".join(objs))
    for line in reconcile_elapsed_time(db, turn_id, turn_no):
        print(line)

    # 2. yes/no mutations
    gathered = {"places": {"trailhead": place}, "actors": {"player-neil": actor}, "objects": objs}
    questions = mock_gm_adjudicate(player_input, gathered)
    db.execute("UPDATE turns SET mutation_questions=? WHERE id=?", (j(questions), turn_id))
    print("\n[adjudicate]")
    for qd in questions:
        print(f"  Q: {qd['q']}\n  A: {qd['answer']} — {qd['rationale']}")
        if qd["answer"] == "yes" and qd["effect"]:
            for target, changes in qd["effect"].items():
                etype, slug = target.split(":", 1)
                for field, new in changes.items():
                    apply_effect(db, turn_id, etype, slug, field, new, f"mutation Q: {qd['q']}")
                    print(f"    -> {target} {field} = {new!r}")
        elif qd["answer"] == "no" and qd["effect"]:
            for target, changes in qd["effect"].items():
                etype, slug = target.split(":", 1)
                for field, new in changes.items():
                    apply_effect(db, turn_id, etype, slug, field, new, f"partial effect: {qd['q']}")
                    print(f"    -> (partial) {target} {field} = {new!r}")

    # 3. narrative — composed from the approved mutations
    bits = []
    for qd in questions:
        if qd["answer"] == "yes" and qd["effect"]:
            targets = ", ".join(qd["effect"].keys())
            bits.append(f"{qd['q']} ({targets} changed)")
        elif qd["answer"] == "no":
            bits.append(f"{qd['q']} ({qd['rationale']})")
    narrative = " ".join(bits) + " Below, the fog does not move."
    db.execute("UPDATE turns SET narrative=? WHERE id=?", (narrative, turn_id))
    print(f"\n[narrative] {narrative}")

    # 4+5. advance time + update touched rows
    db.execute("UPDATE games SET turn_no=?, game_clock_min=? WHERE guid=?",
               (turn_no, g["game_clock_min"] + turn_len_min, g["guid"]))
    db.execute("UPDATE places SET last_visited_turn=? WHERE slug='trailhead'", (turn_no,))
    db.execute("UPDATE actors SET last_acted_turn=? WHERE slug='player-neil'", (turn_no,))
    db.commit()
    print(f"\n[update] game_clock={g['game_clock_min']+turn_len_min} min, turn={turn_no} "
          f"({time.time()-t0:.2f}s)")


def show_audit(db):
    print("\n=== mutations ledger ===")
    for r in db.execute("SELECT turn_id, entity_type, entity_id, field, old_value, new_value, cause FROM mutations"):
        print(f"  t{r[0]} {r[1]}#{r[2]} {r[3]}: {r[4]} -> {r[5]}  ({r[6][:60]})")


if __name__ == "__main__":
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    seed(db)
    run_turn(db, "I pick up the water bottle and drink deeply.")
    run_turn(db, "I try to fell a dead tree for firewood.")
    show_audit(db)
    print("\nOK — schema demo complete.")
