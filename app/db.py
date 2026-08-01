"""SQLite access layer: connection handling plus the schema migrations."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from . import config

_local = threading.local()


def utcnow() -> str:
    """Timestamps are stored as ISO-8601 UTC strings so they sort lexically."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    """One connection per thread; SQLite handles the cross-thread locking."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    conn.commit()


SCHEMA = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS show (
        id                 INTEGER PRIMARY KEY,   -- TVmaze show id
        name               TEXT NOT NULL,
        status             TEXT,
        premiered          TEXT,
        ended              TEXT,
        network            TEXT,
        language           TEXT,
        genres             TEXT,
        runtime            INTEGER,
        average_runtime    INTEGER,
        image              TEXT,
        image_original     TEXT,
        summary            TEXT,
        url                TEXT,
        tvdb_id            INTEGER,
        imdb_id            TEXT,
        schedule_time      TEXT,
        schedule_days      TEXT,
        remote_updated     INTEGER,
        synced_at          TEXT
    );

    CREATE TABLE IF NOT EXISTS episode (
        id          INTEGER PRIMARY KEY,          -- TVmaze episode id
        show_id     INTEGER NOT NULL REFERENCES show(id) ON DELETE CASCADE,
        season      INTEGER,
        number      INTEGER,
        name        TEXT,
        type        TEXT,
        is_special  INTEGER NOT NULL DEFAULT 0,
        airdate     TEXT,
        airstamp    TEXT,
        runtime     INTEGER,
        summary     TEXT,
        image       TEXT,
        url         TEXT
    );
    CREATE INDEX IF NOT EXISTS episode_show_idx ON episode(show_id, season, number);
    CREATE INDEX IF NOT EXISTS episode_airstamp_idx ON episode(airstamp);

    CREATE TABLE IF NOT EXISTS follow (
        show_id     INTEGER PRIMARY KEY REFERENCES show(id) ON DELETE CASCADE,
        followed_at TEXT NOT NULL,
        archived    INTEGER NOT NULL DEFAULT 0,
        favorite    INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS watch (
        episode_id INTEGER PRIMARY KEY REFERENCES episode(id) ON DELETE CASCADE,
        show_id    INTEGER NOT NULL REFERENCES show(id) ON DELETE CASCADE,
        watched_at TEXT NOT NULL,
        source     TEXT NOT NULL DEFAULT 'app'
    );
    CREATE INDEX IF NOT EXISTS watch_show_idx ON watch(show_id);
    CREATE INDEX IF NOT EXISTS watch_when_idx ON watch(watched_at);

    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS import_job (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        filename   TEXT,
        dry_run    INTEGER NOT NULL DEFAULT 0,
        status     TEXT NOT NULL,
        report     TEXT
    );
    """,
    # v2: imports run in the background, so they report progress as they go.
    """
    ALTER TABLE import_job ADD COLUMN stage TEXT;
    ALTER TABLE import_job ADD COLUMN done INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE import_job ADD COLUMN total INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE import_job ADD COLUMN error TEXT;
    """,
    # v3: shows you want to get to next are pinned to the top of Up Next.
    """
    ALTER TABLE follow ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
    """,
]


def migrate() -> None:
    conn = connect()
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row["version"] if row else 0
    for index, script in enumerate(SCHEMA, start=1):
        if index > current:
            conn.executescript(script)
            current = index
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (current,))
    conn.commit()


def get_meta(key: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
