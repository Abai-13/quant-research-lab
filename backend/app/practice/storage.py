"""练习隔离库的最小持久化结构与 JSON 读写辅助。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


PRACTICE_SCHEMA_VERSION = 2

PRACTICE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS practice_schema (
        schema_version INTEGER PRIMARY KEY,
        installed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_session (
        practice_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','finished')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        revealed_date TEXT NOT NULL,
        data_version_id TEXT NOT NULL,
        source_code TEXT NOT NULL,
        seed_snapshot_id TEXT,
        segments_json TEXT NOT NULL,
        settings_json TEXT NOT NULL,
        user_state_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(start_date <= revealed_date AND revealed_date <= end_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_practice_session_updated ON practice_session(updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS practice_idempotency (
        operation TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        practice_id TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(operation, idempotency_key),
        FOREIGN KEY(practice_id) REFERENCES practice_session(practice_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_order (
        order_id TEXT PRIMARY KEY,
        practice_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('buy','sell')),
        planned_shares INTEGER NOT NULL CHECK(planned_shares > 0),
        filled_shares INTEGER NOT NULL DEFAULT 0 CHECK(filled_shares >= 0),
        status TEXT NOT NULL CHECK(status IN (
            'pending','unfilled','partially_filled','filled','rejected','expired','cancelled'
        )),
        detail_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(practice_id, idempotency_key),
        FOREIGN KEY(practice_id) REFERENCES practice_session(practice_id),
        CHECK(filled_shares <= planned_shares)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_practice_order_session_date ON practice_order(practice_id, trade_date)",
    """
    CREATE TABLE IF NOT EXISTS practice_decision (
        decision_id TEXT PRIMARY KEY,
        practice_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        decision_date TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        judgment TEXT NOT NULL,
        evidence TEXT NOT NULL,
        exit_condition TEXT NOT NULL,
        is_backfill INTEGER NOT NULL DEFAULT 0 CHECK(is_backfill IN (0,1)),
        corrects_decision_id TEXT,
        UNIQUE(practice_id, idempotency_key),
        FOREIGN KEY(practice_id) REFERENCES practice_session(practice_id),
        FOREIGN KEY(corrects_decision_id) REFERENCES practice_decision(decision_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_practice_decision_session_date ON practice_decision(practice_id, decision_date, recorded_at)",
    """
    CREATE TABLE IF NOT EXISTS practice_plan (
        plan_id TEXT PRIMARY KEY,
        current_version INTEGER NOT NULL CHECK(current_version >= 1),
        status TEXT NOT NULL CHECK(status IN ('draft','confirmed')),
        practice_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_plan_version (
        plan_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version >= 1),
        status TEXT NOT NULL CHECK(status IN ('draft','confirmed')),
        payload_json TEXT NOT NULL,
        symbols_json TEXT NOT NULL,
        goal TEXT NOT NULL,
        practice_id TEXT,
        qualification_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(plan_id, version),
        FOREIGN KEY(plan_id) REFERENCES practice_plan(plan_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_plan_idempotency (
        operation TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(operation, idempotency_key)
    )
    """,
)


def ensure_practice_schema(conn: sqlite3.Connection) -> None:
    """保证 practice 专用表存在；不提交调用方事务。"""
    for statement in PRACTICE_SCHEMA:
        conn.execute(statement)
    order_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='practice_order'"
    ).fetchone()[0]
    if "'unfilled'" not in order_sql or "'expired'" not in order_sql:
        conn.execute("DROP INDEX IF EXISTS idx_practice_order_session_date")
        conn.execute("ALTER TABLE practice_order RENAME TO practice_order_old")
        conn.execute(PRACTICE_SCHEMA[4])
        conn.execute(
            "INSERT INTO practice_order(order_id,practice_id,idempotency_key,trade_date,ts_code,direction,"
            "planned_shares,filled_shares,status,detail_json,created_at)"
            " SELECT order_id,practice_id,idempotency_key,trade_date,ts_code,direction,planned_shares,"
            "filled_shares,status,detail_json,created_at FROM practice_order_old"
        )
        conn.execute("DROP TABLE practice_order_old")
        conn.execute(PRACTICE_SCHEMA[5])
    conn.execute(
        "INSERT OR IGNORE INTO practice_schema(schema_version) VALUES (?)",
        (PRACTICE_SCHEMA_VERSION,),
    )


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_json(value: str) -> Any:
    return json.loads(value)
