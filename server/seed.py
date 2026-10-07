"""Seed for scenario 'fog-line-mystery-v1' — DESIGN.md §3.

Turn 0 state (07:00 local, morning): the trailhead above the fog, the
player, the water bottle, the red drop on the player's cheek.
`plot_concept` stays NULL until the GM picks at game start (§3.3.2).
"""
import json
from datetime import datetime, timezone

EMPTY_INVENTORY = {"hands": [None, None], "backpack": [None] * 8}

SCENARIO_ID = "fog-line-mystery-v1"
TURN_LEN_MIN = 60  # one game-hour per turn (DESIGN §3.1)

# Fixed scenario geography: adjacency pairs for the deterministic map
# panel (Phase 3). Edges are scenario truth, not world state — the map
# renderer filters them to discovered places only, so hidden geography
# can never leak (§5, images.py).
EDGES = [
    ("trail-up", "trailhead"),
    ("trailhead", "trail-down"),
    ("trail-down", "fog-below"),
]


def _j(x):
    return json.dumps(x)


def seed(db, guid, player_email):
    c = db.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute(
        "INSERT INTO games (guid,player_email,scenario_id,plot_concept,status,turn_no,game_clock_min,started_at,ended_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (guid, player_email, SCENARIO_ID, None, "active", 0, 0, now, None),
    )
    for slug, name, desc, phys, disc in [
        ("trailhead", "Trailhead",
         "A narrow trail cresting a ridge. Below, an ocean of fog stretches to the horizon.",
         {"fog_density_local": 0.0, "fog_below": True, "light": "morning",
          "temp_c": 8, "wind": "light", "ground": "damp gravel", "trail_empty": True}, 1),
        ("trail-down", "Descent",
         "One steep descent drops straight toward the fog.",
         {"fog_density": 0.4, "light": "morning"}, 0),
        ("trail-up", "Switchbacks",
         "Switchbacks climb behind you, away from the fog.",
         {"fog_density": 0.0, "light": "morning"}, 0),
        ("fog-below", "Fog",
         "The fog ocean below the ridge — visible, never entered.",
         {"fog_density": 1.0}, 0),
    ]:
        c.execute(
            "INSERT INTO places (slug,name,description,physical_state,hidden_traits,discovered,last_visited_turn)"
            " VALUES (?,?,?,?,?,?,?)",
            (slug, name, desc, _j(phys), _j({}), disc, 0),
        )
    c.execute(
        "INSERT INTO actors (slug,name,kind,is_player,location_slug,physical_state,hidden_traits,inventory)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("player", "You", "player", 1, "trailhead",
         _j({"hp": 1.0, "hunger": 0.2, "fatigue": 0.3, "wetness": 0.0,
             "cold": 0.2, "pose": "standing", "facing": "down-trail"}),
         _j({}),
         _j(EMPTY_INVENTORY)),
    )
    c.execute(
        "INSERT INTO objects (slug,name,description,physical_state,hidden_traits,holder)"
        " VALUES (?,?,?,?,?,?)",
        ("water-bottle", "Water bottle",
         "A half-full bottle sitting on an otherwise empty trail.",
         _j({"water_ml": 400, "cap_on": False, "tipped": False}),
         _j({"unexplained": True, "owner": None}), "place:trailhead"),
    )
    c.execute(
        "INSERT INTO objects (slug,name,description,physical_state,hidden_traits,holder)"
        " VALUES (?,?,?,?,?,?)",
        ("red-drop", "A red drop",
         "A single red drop on the player's cheek. It isn't raining.",
         _j({"volume_ml": 0.05, "color": "red", "wet": True, "dried": False}),
         _j({"unexplained": True}),
         "actor:player:cheek"),  # on-body holder spot, DESIGN §3.2
    )
    db.commit()
