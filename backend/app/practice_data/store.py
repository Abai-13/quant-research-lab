"""练习数据域持久化：练习库中的共享版本与日线表。

表名加 ``practice_data_`` 前缀，不与 core 的 practice 身份/订单表冲突。
所有写入参与调用方给的连接；本模块不自行打开/提交个人库。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

__all__ = [
    "ensure_practice_data_schema",
    "insert_version",
    "insert_daily_rows",
    "list_versions",
    "get_version",
    "get_refresh_request",
    "put_refresh_request",
    "encode_json",
    "decode_json",
]

DATA_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS practice_data_version (
        version_id TEXT PRIMARY KEY,
        source_code TEXT NOT NULL,
        synthetic INTEGER NOT NULL CHECK(synthetic IN (0,1)),
        symbols_json TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        record_count INTEGER NOT NULL CHECK(record_count >= 0),
        coverage_json TEXT NOT NULL,
        limitations_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_data_daily (
        version_id TEXT NOT NULL,
        code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        preclose REAL,
        volume INTEGER,
        amount REAL,
        adjustflag TEXT,
        tradestatus INTEGER CHECK(tradestatus IN (0,1)),
        PRIMARY KEY(version_id, code, trade_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_practice_data_daily_code_date"
    " ON practice_data_daily(code, trade_date)",
    """
    CREATE TABLE IF NOT EXISTS practice_data_refresh_request (
        idempotency_key TEXT PRIMARY KEY,
        request_sha256 TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)


def ensure_practice_data_schema(conn: sqlite3.Connection) -> None:
    for statement in DATA_SCHEMA:
        conn.execute(statement)


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_json(value: str) -> Any:
    return json.loads(value)


def insert_version(conn: sqlite3.Connection, version: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO practice_data_version (
            version_id, source_code, synthetic, symbols_json, start_date,
            end_date, record_count, coverage_json, limitations_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            version["version_id"],
            version["source_code"],
            1 if version["synthetic"] else 0,
            encode_json(version["symbols"]),
            version["start_date"],
            version["end_date"],
            version["record_count"],
            encode_json(version["coverage"]),
            encode_json(version["limitations"]),
            version["created_at"],
        ),
    )


def insert_daily_rows(
    conn: sqlite3.Connection, version_id: str, rows: list[dict[str, Any]]
) -> int:
    """幂等写入日线行；同键重刷不重复入库。"""
    written = 0
    for row in rows:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO practice_data_daily (
                version_id, code, trade_date, open, high, low, close,
                preclose, volume, amount, adjustflag, tradestatus
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                version_id,
                row["code"],
                row["trade_date"],
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close"),
                row.get("preclose"),
                row.get("volume"),
                row.get("amount"),
                row.get("adjustflag"),
                row.get("tradestatus"),
            ),
        )
        written += cursor.rowcount
    return written


_VERSION_COLUMNS = (
    "version_id, source_code, synthetic, symbols_json, start_date, end_date,"
    " record_count, coverage_json, limitations_json, created_at"
)


def _row_to_version(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "version_id": row["version_id"],
        "source_code": row["source_code"],
        "synthetic": bool(row["synthetic"]),
        "symbols": decode_json(row["symbols_json"]),
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "record_count": row["record_count"],
        "coverage": decode_json(row["coverage_json"]),
        "limitations": decode_json(row["limitations_json"]),
        "created_at": row["created_at"],
    }


def list_versions(
    conn: sqlite3.Connection, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    try:
        rows = conn.execute(
            f"SELECT {_VERSION_COLUMNS} FROM practice_data_version"
            " ORDER BY created_at DESC, version_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # The core practice runtime may be available before the first explicit
        # data refresh has created this module's tables. A read must still
        # return an empty registry without creating tables or touching disk.
        if "no such table: practice_data_version" not in str(exc).lower():
            raise
        return [], 0
    total = conn.execute("SELECT COUNT(*) FROM practice_data_version").fetchone()[0]
    return [_row_to_version(row) for row in rows], int(total)


def get_version(conn: sqlite3.Connection, version_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_VERSION_COLUMNS} FROM practice_data_version WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    return _row_to_version(row) if row is not None else None


def get_refresh_request(
    conn: sqlite3.Connection, idempotency_key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT idempotency_key, request_sha256, response_json, created_at"
        " FROM practice_data_refresh_request WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "idempotency_key": row["idempotency_key"],
        "request_sha256": row["request_sha256"],
        "response": decode_json(row["response_json"]),
    }


def put_refresh_request(
    conn: sqlite3.Connection,
    idempotency_key: str,
    request_sha256: str,
    response: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO practice_data_refresh_request"
        " (idempotency_key, request_sha256, response_json, created_at)"
        " VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (idempotency_key, request_sha256, encode_json(response)),
    )
