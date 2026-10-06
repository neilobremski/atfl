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
    started_at TEXT, ended_at TEXT,
    -- mailer bookkeeping (DESIGN §1.1/§5.3): the last handoff time
    -- drives the nudge gate. Threading lives on Murph's side now
    -- (relay edition 2026-10-02); the old thread_message_id/thread_refs
    -- columns are retired — pre-relay DB files keep them vestigially.
    last_email_at TEXT       -- when we last handed anything to Murph
);
-- Bug reports (docs/playtest-bug-handling.md): an open row pauses the
-- game (no turns, no idle turns, no nudges). dispatch.ensure_bugs_table
-- is the idempotent backstop for DBs created before this column family.
CREATE TABLE IF NOT EXISTS bugs (
    id INTEGER PRIMARY KEY,
    game_guid TEXT,
    reporter TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    reported_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | closed
    resolution_note TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bugs_game ON bugs(game_guid);
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
-- DESIGN.md §6.3 dogfooding hook: one stats row per turn (no dashboard
-- yet). mutations_count / GM latencies / secrecy result are recorded by
-- run_turn; send_ms is filled by the mailer when the turn email leaves.
CREATE TABLE turn_stats (
    turn_id INTEGER PRIMARY KEY,
    adjudicate_ms REAL,
    narrative_ms REAL,
    secrecy_pass INTEGER,   -- 1 pass, 0 fail (fail rows roll back with the turn)
    mutations_count INTEGER,
    send_ms REAL,           -- NULL until the mailer sends the turn email
    email_sent_at TEXT,
    recorded_at TEXT
);
"""


def create_db(path=":memory:"):
    """Create a fresh game DB and return the connection."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    ensure_mailer_columns(db)
    return db


def ensure_mailer_columns(db):
    """Add the mailer bookkeeping column to the games table if a DB
    predates it (DESIGN §1.1/§5.3 nudge gate). Idempotent. The old
    thread_message_id/thread_refs columns are retired (relay edition
    2026-10-02) and no longer added."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(games)")}
    for col in ("last_email_at",):
        if col not in cols:
            db.execute(f"ALTER TABLE games ADD COLUMN {col} TEXT")
    db.commit()
