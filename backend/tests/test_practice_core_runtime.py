from __future__ import annotations

import sqlite3
from dataclasses import replace

from app.data.migration import SCHEMA_VERSION
from app.practice.runtime import practice_connect, practice_settings


def test_practice_runtime_isolated_and_initializes_schema4(temp_settings) -> None:
    practice = practice_settings(temp_settings)
    assert practice.db_path == temp_settings.runtime_dir / "practice" / "app.db"
    assert practice.db_path != temp_settings.db_path

    with practice_connect(temp_settings) as conn:
        metadata = dict(conn.execute("SELECT key, value FROM app_metadata"))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert metadata["schema_version"] == str(SCHEMA_VERSION)
        assert {"simulation_segment", "simulation_order", "practice_session", "practice_order"} <= tables
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    # 重复连接保持相同的练习数据库和结构。
    with practice_connect(temp_settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM practice_session").fetchone()[0] == 0
    assert not temp_settings.db_path.exists()


def test_practice_connect_rejects_non_sqlite_existing_file(temp_settings) -> None:
    practice = practice_settings(temp_settings)
    practice.db_path.parent.mkdir(parents=True)
    practice.db_path.write_bytes(b"not sqlite")

    try:
        practice_connect(temp_settings)
    except Exception as exc:
        assert getattr(exc, "reason", None) == "database_corrupt"
    else:
        raise AssertionError("corrupt practice database must be rejected and preserved")

    assert practice.db_path.read_bytes() == b"not sqlite"


def test_custom_runtime_named_practice_is_still_isolated_and_persistent(temp_settings, tmp_path) -> None:
    custom = replace(temp_settings, runtime_dir=tmp_path / "practice")
    practice = practice_settings(custom)
    assert practice.db_path == tmp_path / "practice" / "practice" / "app.db"

    with practice_connect(custom) as conn:
        assert not conn.in_transaction
        conn.execute(
            "INSERT INTO practice_session(practice_id,name,status,revision,start_date,end_date,revealed_date,"
            "data_version_id,source_code,segments_json,settings_json,user_state_json,created_at,updated_at)"
            " VALUES('persistent','persist','active',1,'20260101','20260102','20260101','v1','fixture','{}','{}','{}','now','now')"
        )
    with practice_connect(custom) as conn:
        row = conn.execute("SELECT practice_id,name FROM practice_session").fetchone()
        assert tuple(row) == ("persistent", "persist")
    assert not custom.db_path.exists()


def test_practice_order_schema_migrates_engine_statuses(temp_settings) -> None:
    practice = practice_settings(temp_settings)
    with practice_connect(temp_settings) as conn:
        conn.execute("DROP INDEX idx_practice_order_session_date")
        conn.execute("DROP TABLE practice_order")
        conn.execute(
            "CREATE TABLE practice_order (order_id TEXT PRIMARY KEY,practice_id TEXT NOT NULL,"
            "idempotency_key TEXT NOT NULL,trade_date TEXT NOT NULL,ts_code TEXT NOT NULL,"
            "direction TEXT NOT NULL CHECK(direction IN ('buy','sell')),planned_shares INTEGER NOT NULL,"
            "filled_shares INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL CHECK(status IN "
            "('pending','partially_filled','filled','rejected','cancelled')),detail_json TEXT NOT NULL,"
            "created_at TEXT NOT NULL,UNIQUE(practice_id,idempotency_key),"
            "FOREIGN KEY(practice_id) REFERENCES practice_session(practice_id))"
        )
    with practice_connect(temp_settings) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='practice_order'"
        ).fetchone()[0]
        assert "'unfilled'" in sql and "'expired'" in sql
        assert conn.execute("SELECT MAX(schema_version) FROM practice_schema").fetchone()[0] == 2
