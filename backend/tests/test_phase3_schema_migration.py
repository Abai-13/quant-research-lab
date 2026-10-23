"""阶段三 S1：schema 3 → 4 纯增量迁移与既有行为兼容。

全部测试使用隔离临时库；绝不触碰主仓库 .runtime 或任何真实运行库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.data import schema as schema_mod
from app.data.chunks import VersionView
from app.data.migration import (
    SCHEMA_VERSION,
    DatabaseError,
    check_database,
    initialize_database,
    migrate_database,
)
from app.data.status import data_status

from tests.test_data_migration import _write_schema1_db


def _write_schema3_db(path: Path, *, instance_id: str = "phase2-instance", with_history: bool = True) -> None:
    """构造一个真实的 schema 3 夹具库（阶段二目标形态）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "3"),
                ("instance_id", instance_id),
                ("created_at", "2025-06-01T00:00:00+00:00"),
            ],
        )
        schema_mod.create_schema_2(conn)
        if with_history:
            conn.execute(
                "INSERT INTO schema_history(from_version, to_version, applied_at,"
                " result, notes) VALUES (2, 3, '2025-06-02T00:00:00+00:00',"
                " 'success', 'phase 2 explicit migration')"
            )
        schema_mod.create_schema_3(conn)


def _seed_version_data(path: Path) -> tuple[str, str]:
    """在 schema 3 夹具中放入最小行情与已发布版本（用于升级后固定版本读取）。

    成员走 legacy 存储，manifest 摘要按阶段二算法重算登记，保证迁移的
    legacy→chunked 转换可以核对通过。
    """
    from app.data.chunks import legacy_manifest_digest

    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO daily_price(ts_code, trade_date, open, close, pre_close, volume,"
            " amount, source_code, revision_no, content_hash, observed_at, ingested_at)"
            " VALUES ('000001.SZ', '20250102', 10.0, 10.1, 10.0, 100.0, 1000.0,"
            " 'tushare-fixture', 1, 'seedhash1', '2025-06-01T00:00:00+00:00',"
            " '2025-06-01T00:00:00+00:00')"
        )
        row = conn.execute(
            "SELECT id FROM daily_price WHERE ts_code='000001.SZ' AND trade_date='20250102'"
        ).fetchone()
        record_id = int(row[0])
        logical_key = "daily|tushare-fixture|000001.SZ|20250102"
        conn.execute(
            "INSERT INTO data_version(version_id, source_code, scope, categories,"
            " quality_summary, manifest_sha256, rule_version, status, created_at,"
            " published_at, manifest_version, member_storage, member_count, chunk_count,"
            " retention_state, window_mode)"
            " VALUES ('dv_seed000000000001', 'tushare-fixture', '{}', '[\"daily\"]', '{}',"
            " 'placeholder', 'seed-rule', 'published', '2025-06-02T00:00:00+00:00',"
            " '2025-06-02T00:00:00+00:00', 1, 'legacy', 1, 0, 'active', 'full')"
        )
        conn.execute(
            "INSERT INTO data_version_member(version_id, category, record_id,"
            " content_hash, logical_key) VALUES ('dv_seed000000000001', 'daily', ?,"
            " 'seedhash1', ?)",
            (record_id, logical_key),
        )
        digest = legacy_manifest_digest(conn, "dv_seed000000000001")
        conn.execute(
            "UPDATE data_version SET manifest_sha256=? WHERE version_id='dv_seed000000000001'",
            (digest,),
        )
    return "dv_seed000000000001", "000001.SZ"


def test_fresh_database_is_created_as_schema_4(temp_settings: Settings) -> None:
    metadata = initialize_database(temp_settings)

    assert metadata.schema_version == SCHEMA_VERSION == 4
    with sqlite3.connect(temp_settings.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(schema_mod.PHASE3_TABLE_NAMES) <= tables
    with sqlite3.connect(temp_settings.db_path) as conn:
        rows = conn.execute("SELECT * FROM schema_history").fetchall()
    assert not rows, "新库直接创建目标 schema，不写虚假迁移历史"


def test_fresh_database_is_ready_without_migration(temp_settings: Settings) -> None:
    initialize_database(temp_settings)
    status = check_database(temp_settings)
    assert status.ready is True
    assert status.metadata is not None and status.metadata.schema_version == 4


def test_schema3_fixture_upgrades_to_schema_4_additively(temp_settings: Settings) -> None:
    _write_schema3_db(temp_settings.db_path)
    version_id, ts_code = _seed_version_data(temp_settings.db_path)
    before_tables = set(schema_mod.table_names(sqlite3.connect(temp_settings.db_path)))
    _check = sqlite3.connect(temp_settings.db_path)
    _check.close()

    result = migrate_database(temp_settings)

    assert result["status"] == "migrated"
    assert result["schema_version"] == 4
    assert result["converted_versions"] == 1  # legacy 成员仍被转换为分块
    backup_path = Path(result["backup_path"])
    assert backup_path.is_file()
    assert (backup_path.parent / "backup-manifest.json").is_file()

    with sqlite3.connect(temp_settings.db_path) as conn:
        metadata = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
        history = conn.execute(
            "SELECT from_version, to_version, result FROM schema_history ORDER BY id"
        ).fetchall()
        tables = set(schema_mod.table_names(conn))
        version_row = conn.execute(
            "SELECT member_storage, member_count FROM data_version WHERE version_id=?",
            (version_id,),
        ).fetchone()
        price_rows = conn.execute(
            "SELECT ts_code, trade_date, close FROM daily_price WHERE ts_code=?", (ts_code,)
        ).fetchall()
    assert metadata["schema_version"] == "4"
    assert metadata["instance_id"] == "phase2-instance"
    assert (history[-1][0], history[-1][1], history[-1][2]) == (3, 4, "success")
    assert set(schema_mod.PHASE3_TABLE_NAMES) <= tables
    # 既有表与数据原样保留；转换是二阶段既有语义，不改 version_id。
    assert before_tables <= tables
    assert version_row == ("chunked", 1)
    assert price_rows

    status = check_database(temp_settings)
    assert status.ready is True
    assert status.metadata is not None and status.metadata.schema_version == 4


def test_schema3_fixture_reads_fixed_version_after_upgrade(temp_settings: Settings) -> None:
    _write_schema3_db(temp_settings.db_path)
    version_id, ts_code = _seed_version_data(temp_settings.db_path)
    migrate_database(temp_settings)

    conn = sqlite3.connect(temp_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        view = VersionView(conn, version_id, expected_source_code="tushare-fixture")
        row = view.daily_on(ts_code, "20250102")
        assert row is not None and float(row["close"]) == 10.1
        assert view.history_length(ts_code, on_or_before="20250102") == 1
    finally:
        conn.close()


def test_migration_from_version3_without_history_still_succeeds(temp_settings: Settings) -> None:
    """阶段二整理期间新建的 schema 3 库没有 schema_history；3→4 不要求回填。"""
    _write_schema3_db(temp_settings.db_path, with_history=False)

    result = migrate_database(temp_settings)

    assert result["status"] == "migrated"
    assert result["schema_version"] == 4


def test_repeat_migration_is_idempotent(temp_settings: Settings) -> None:
    _write_schema3_db(temp_settings.db_path)
    _seed_version_data(temp_settings.db_path)
    migrate_database(temp_settings)

    second = migrate_database(temp_settings)
    assert second["status"] == "already_migrated"
    assert second["schema_version"] == 4

    with sqlite3.connect(temp_settings.db_path) as conn:
        count_3_4 = conn.execute(
            "SELECT COUNT(*) FROM schema_history WHERE from_version=3 AND to_version=4"
        ).fetchone()[0]
    assert count_3_4 == 1
    assert check_database(temp_settings).ready is True


def test_full_chain_from_schema1_reaches_schema4(temp_settings: Settings) -> None:
    _write_schema1_db(temp_settings.db_path)

    result = migrate_database(temp_settings)

    assert result["schema_version"] == 4
    with sqlite3.connect(temp_settings.db_path) as conn:
        history = conn.execute(
            "SELECT from_version, to_version FROM schema_history ORDER BY id"
        ).fetchall()
    assert history == [(1, 2), (2, 3), (3, 4)]


def test_unknown_schema_version_rejected_without_touching_file(temp_settings: Settings) -> None:
    _write_schema3_db(temp_settings.db_path)
    with sqlite3.connect(temp_settings.db_path) as conn:
        conn.execute("UPDATE app_metadata SET value='9' WHERE key='schema_version'")
    before = temp_settings.db_path.read_bytes()

    with pytest.raises(DatabaseError) as excinfo:
        migrate_database(temp_settings)

    assert excinfo.value.reason == "schema_incompatible"
    assert temp_settings.db_path.read_bytes() == before


def test_corrupt_database_rejected_without_touching_file(temp_settings: Settings) -> None:
    temp_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    original = b"this is not a sqlite database" * 10
    temp_settings.db_path.write_bytes(original)

    with pytest.raises(DatabaseError) as excinfo:
        migrate_database(temp_settings)

    assert excinfo.value.reason == "database_corrupt"
    assert temp_settings.db_path.read_bytes() == original


def test_unknown_table_in_schema3_rejected(temp_settings: Settings) -> None:
    _write_schema3_db(temp_settings.db_path)
    with sqlite3.connect(temp_settings.db_path) as conn:
        conn.execute("CREATE TABLE stranger_table (id INTEGER PRIMARY KEY)")
    before = temp_settings.db_path.read_bytes()

    with pytest.raises(DatabaseError) as excinfo:
        migrate_database(temp_settings)

    assert excinfo.value.reason == "schema_incompatible"
    assert temp_settings.db_path.read_bytes() == before
    with sqlite3.connect(temp_settings.db_path) as conn:
        rows = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
    assert rows["schema_version"] == "3", "拒绝时不得改写既有库"


def test_phase3_tables_absent_in_legend_schema3_and_status_requires_migration(
    temp_settings: Settings,
) -> None:
    _write_schema3_db(temp_settings.db_path)
    conn = sqlite3.connect(temp_settings.db_path)
    assert "account" not in schema_mod.table_names(conn)
    conn.close()

    report = data_status(temp_settings)
    assert report["status"] == "migration_required"
    assert report["database"]["schema_version"] == 3


def test_status_ready_on_schema4_empty_database(temp_settings: Settings) -> None:
    initialize_database(temp_settings)
    report = data_status(temp_settings)
    assert report["status"] == "not_connected"
    assert report["database"]["schema_version"] == 4
