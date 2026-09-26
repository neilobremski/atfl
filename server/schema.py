"""SQLite schema for one game file (<GUID>.db).

DESIGN.md §3 seed + research/phase0-world-state-schema.md.
One DB file per game; the file is the archive.
"""
import sqlite3

SCHEMA = """
CREATE TABLE games (
    guid TEXT PRIMARY KEY,
    player_email TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    plot_concept TEXT,  -- NULL until the GM picks one at game start (DESIGN §3.3)
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


def create_db(path=":memory:"):
    """Create a fresh game DB and return the connection."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db
