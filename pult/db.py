"""SQLite schema and the key/value meta table."""

import glob
import html
import http.server
import json
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

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
        exit_code INTEGER
    );
    CREATE TABLE IF NOT EXISTS outbox (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id  INTEGER NOT NULL,
        text     TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        created  REAL NOT NULL
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

ensure_columns("jobs", {"project": "TEXT", "cost": "REAL", "turns": "INTEGER", "mode": "TEXT",
                        "engine": "TEXT NOT NULL DEFAULT 'claude'", "tokens": "INTEGER"})

ensure_columns(
    "outbox",
    {
        "kind": "TEXT NOT NULL DEFAULT 'text'",  # text | doc
        "parse_mode": "TEXT",
        "markup": "TEXT",
        "file_path": "TEXT",
    },
)
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
