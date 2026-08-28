"""SQLite schema and the key/value meta table."""

import json
import sqlite3
import threading

from .core import DB_PATH, log

db_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)

db.execute("PRAGMA journal_mode=WAL")

db.executescript(
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS jobs (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id   INTEGER NOT NULL,
        prompt    TEXT NOT NULL,
        state     TEXT NOT NULL,          -- pending | queued | running | done | failed | cancelled
        created   REAL NOT NULL,
        started   REAL,
        finished  REAL,
        result    TEXT,
        exit_code INTEGER,
        project   TEXT,
        cost      REAL,
        turns     INTEGER,
        mode      TEXT,
        engine    TEXT NOT NULL DEFAULT 'claude',
        tokens    INTEGER,
        step      INTEGER NOT NULL DEFAULT 0,   -- which failover step this job is on
        handover  TEXT                          -- engine that handed it over, if any
    );
    CREATE TABLE IF NOT EXISTS outbox (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    INTEGER NOT NULL,
        text       TEXT NOT NULL,
        attempts   INTEGER NOT NULL DEFAULT 0,
        created    REAL NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'text',  -- text | doc
        parse_mode TEXT,
        markup     TEXT,
        file_path  TEXT
    );
    """
)

db.commit()
def ensure_columns(table, columns):
    """Additive schema migration -- keeps an older state.db usable."""
    with db_lock:
        have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log(f"migrated: {table}.{name}")
        db.commit()

# A database created before any of these columns existed keeps working: the table
# above is only used for a brand-new file, and this brings an old one up to it.
ensure_columns("jobs", {"project": "TEXT", "cost": "REAL", "turns": "INTEGER", "mode": "TEXT",
                        "engine": "TEXT NOT NULL DEFAULT 'claude'", "tokens": "INTEGER",
                        "step": "INTEGER NOT NULL DEFAULT 0", "handover": "TEXT"})

ensure_columns("outbox", {"kind": "TEXT NOT NULL DEFAULT 'text'", "parse_mode": "TEXT",
                          "markup": "TEXT", "file_path": "TEXT"})
def meta_get(key, default=None):
    with db_lock:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default
def meta_set(key, value):
    with db_lock:
        db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        db.commit()
def meta_del(key):
    with db_lock:
        db.execute("DELETE FROM meta WHERE key=?", (key,))
        db.commit()
def meta_get_json(key, default=None):
    raw = meta_get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default
def meta_set_json(key, value):
    meta_set(key, json.dumps(value, ensure_ascii=False))
