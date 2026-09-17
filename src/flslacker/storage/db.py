"""SQLite store: snapshots, plans, jobs, grants, receipts and an append-only journal."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from flslacker.contracts.models import utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, adapter_epoch INTEGER NOT NULL, secret TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS targets (
    target_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, label_key TEXT NOT NULL, doc TEXT NOT NULL,
    UNIQUE (session_id, label_key));
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, target_id TEXT NOT NULL,
    received_at TEXT NOT NULL, state_hash TEXT NOT NULL, content_hash TEXT NOT NULL,
    raw TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS snapshots_by_target ON snapshots (target_id, received_at);
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, plan_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
    request_id TEXT UNIQUE, target_id TEXT, updated_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_by_state ON jobs (state);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS before_images (
    job_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY, actor TEXT NOT NULL, fingerprint TEXT NOT NULL,
    job_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL, request_id TEXT NOT NULL, sender TEXT NOT NULL, sequence INTEGER NOT NULL,
    kind TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY (session_id, request_id));
CREATE TABLE IF NOT EXISTS approval_requests (
    plan_id TEXT PRIMARY KEY, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
    status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_status (
    session_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipe_runs (
    run_id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL, capture_job_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, event TEXT NOT NULL,
    job_id TEXT, actor TEXT, doc TEXT NOT NULL);
"""


def to_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, separators=(",", ":"), default=str)


class Database:
    """One connection, serialized by a re-entrant lock. Writes use IMMEDIATE transactions."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._depth = 0
        self.conn.executescript(SCHEMA)  # executescript commits on its own
        with self.transaction():
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(f"unsupported database schema {row['value']}")

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self.conn
                finally:
                    self._depth -= 1
                return
            self.conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self.conn
            except BaseException:
                self._depth = 0
                self.conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self.conn.execute("COMMIT")

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.transaction():
            return self.conn.execute(sql, params)

    def doc(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = self.one(sql, params)
        return None if row is None else json.loads(row["doc"])

    def docs(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [json.loads(row["doc"]) for row in self.all(sql, params)]

    def journal(self, event: str, doc: Any, job_id: str | None = None, actor: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO journal (at, event, job_id, actor, doc) VALUES (?, ?, ?, ?, ?)",
                (utcnow().isoformat(), event, job_id, actor, to_json(doc)),
            )

    def close(self) -> None:
        with self.lock:
            self.conn.close()
