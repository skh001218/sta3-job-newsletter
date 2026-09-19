from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .contracts import PersistenceStatus


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    question_id TEXT NOT NULL,
    question_version INTEGER NOT NULL,
    question_snapshot TEXT NOT NULL,
    response_json TEXT NOT NULL,
    comparison_json TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    comparison_ready_at TEXT,
    completed_at TEXT,
    notion_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED',
    notion_page_id TEXT,
    notion_block_id TEXT,
    notion_url TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_question
ON attempts(question_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notion_syncs (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archived_attempts (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
    archived_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_archived_attempts_created
ON archived_attempts(archived_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                UPDATE notion_syncs
                SET status = ?, last_error = 'Server restarted during synchronization'
                WHERE status = ?
                """,
                (PersistenceStatus.RETRY.value, PersistenceStatus.SYNCING.value),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
        finally:
            connection.close()
