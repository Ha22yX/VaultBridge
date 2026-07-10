from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .settings import database_path


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  host TEXT NOT NULL,
  port INTEGER NOT NULL DEFAULT 22,
  username TEXT NOT NULL,
  password_enc TEXT NOT NULL,
  target_path TEXT NOT NULL,
  include_paths TEXT NOT NULL,
  exclude_patterns TEXT NOT NULL,
  schedule_kind TEXT NOT NULL DEFAULT 'daily',
  day_of_week INTEGER,
  hour INTEGER NOT NULL DEFAULT 3,
  minute INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  message TEXT,
  commit_hash TEXT,
  phase TEXT,
  current_path TEXT,
  total_files INTEGER NOT NULL DEFAULT 0,
  copied_files INTEGER NOT NULL DEFAULT 0,
  total_bytes INTEGER NOT NULL DEFAULT 0,
  copied_bytes INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
"""


RUN_COLUMN_MIGRATIONS = {
    "phase": "TEXT",
    "current_path": "TEXT",
    "total_files": "INTEGER NOT NULL DEFAULT 0",
    "copied_files": "INTEGER NOT NULL DEFAULT 0",
    "total_bytes": "INTEGER NOT NULL DEFAULT 0",
    "copied_bytes": "INTEGER NOT NULL DEFAULT 0",
}


def connect() -> sqlite3.Connection:
    path: Path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        for column, definition in RUN_COLUMN_MIGRATIONS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
        conn.execute(
            """
            UPDATE runs
            SET status = 'failed',
                phase = 'interrupted',
                message = COALESCE(message, '') || ' App restarted before the backup finished.',
                finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
            WHERE status = 'running'
            """
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
