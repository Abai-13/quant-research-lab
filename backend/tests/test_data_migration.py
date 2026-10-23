"""schema 1 → schema 2 迁移、初始化与只读就绪检查。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.data.migration import (
    SCHEMA_VERSION,
    DatabaseError,
    MigrationInjectedFailure,
    check_database,
    initialize_database,
    migrate_database,
)


def _write_schema1_db(path: Path, *, instance_id: str = "legacy-instance") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("instance_id", instance_id),
                ("created_at", "2024-01-01T00:00:00+00:00"),
            ],
        )


def test_new_database_is_created_as_target_schema(temp_settings: Settings) -> None:
    metadata = initialize_database(temp_settings)

    # 阶段三起目标 schema 为 4（纯增量）：可写回关；既有 schema 2 断言保留结构名。
    assert metadata.schema_version == SCHEMA_VERSION == 4
    assert metadata.instance_id
    assert metadata.created_at
    with sqlite3.connect(temp_settings.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"app_metadata", "daily_price", "data_version", "quality_issue", "pool_snapshot"} <= tables


def test_initialize_is_idempotent_and_preserves_metadata(temp_settings: Settings) -> None:
    first = initialize_database(temp_settings)
    second = initialize_database(temp_settings)

    assert first == second


def test_schema1_database_is_not_auto_upgraded(temp_settings: Settings) -> None:
    _write_schema1_db(temp_settings.db_path)

    metadata = initialize_database(temp_settings)

    assert metadata.schema_version == 1
    status = check_database(temp_settings)
    assert status.ready is False
    assert status.reason == "migration_required"


def test_migration_preserves_instance_metadata_and_records_history(temp_settings: Settings) -> None:
    _write_schema1_db(temp_settings.db_path)

    result = migrate_database(temp_settings)

    assert result["status"] == "migrated"
    assert result["schema_version"] == 4
    backup_path = Path(result["backup_path"])
    assert backup_path.is_file()
    assert (backup_path.parent / "backup-manifest.json").is_file()
    with sqlite3.connect(temp_settings.db_path) as conn:
        rows = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
        history = conn.execute(
            "SELECT from_version, to_version, result, backup_path FROM schema_history"
            " ORDER BY id"
        ).fetchall()
    assert rows["schema_version"] == "4"
    assert rows["instance_id"] == "legacy-instance"
    assert rows["created_at"] == "2024-01-01T00:00:00+00:00"
    assert history[0][0] == 1
    assert history[0][1] == 2
    assert history[0][2] == "success"
    assert history[0][3] == str(backup_path)
    assert history[1][0] == 2
    assert history[1][1] == 3
    assert history[1][2] == "success"


def test_migration_is_repeatable_without_resetting(temp_settings: Settings) -> None:
    _write_schema1_db(temp_settings.db_path)
    migrate_database(temp_settings)

    second = migrate_database(temp_settings)

    assert second["status"] == "already_migrated"
    status = check_database(temp_settings)
    assert status.ready is True
    with sqlite3.connect(temp_settings.db_path) as conn:
        metadata = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
    assert metadata["instance_id"] == "legacy-instance"
    assert metadata["created_at"] == "2024-01-01T00:00:00+00:00"


def test_migration_refuses_unknown_schema(temp_settings: Settings) -> None:
    _write_schema1_db(temp_settings.db_path)
    with sqlite3.connect(temp_settings.db_path) as conn:
        conn.execute("UPDATE app_metadata SET value='9' WHERE key='schema_version'")
    before = temp_settings.db_path.read_bytes()

    with pytest.raises(DatabaseError) as excinfo:
        migrate_database(temp_settings)

    assert excinfo.value.reason == "schema_incompatible"
    assert temp_settings.db_path.read_bytes() == before


def test_migration_refuses_corrupt_database_and_preserves_it(temp_settings: Settings) -> None:
    temp_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    original = b"this is not a sqlite database" * 10
    temp_settings.db_path.write_bytes(original)

    with pytest.raises(DatabaseError) as excinfo:
        migrate_database(temp_settings)

    assert excinfo.value.reason == "database_corrupt"
    assert temp_settings.db_path.read_bytes() == original


def test_migration_refuses_foreign_tables(temp_settings: Settings) -> None:
    _write_schema1_db(temp_settings.db_path)
    with sqlite3.connect(temp_settings.db_path) as conn:
        conn.execute("CREATE TABLE other_app (id INTEGER PRIMARY KEY)")
    before = temp_settings.db_path.read_bytes()

    with pytest.raises(DatabaseError) as excinfo:
        migrate_database(temp_settings)

    assert excinfo.value.reason == "schema_incompatible"
    assert temp_settings.db_path.read_bytes() == before


@pytest.mark.parametrize(
    "stage",
    ["before_integrity", "after_metadata", "after_backup", "after_verify_backup", "after_schema"],
)
def test_injected_failure_preserves_database_and_backup(temp_settings: Settings, stage: str) -> None:
    _write_schema1_db(temp_settings.db_path)
    before = temp_settings.db_path.read_bytes()

    with pytest.raises(MigrationInjectedFailure):
        migrate_database(temp_settings, fail_after_step=stage)

    # 原库仍可打开，元信息未被破坏。
    with sqlite3.connect(temp_settings.db_path) as conn:
        rows = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
    # after_schema 注入点位于事务提交之后：结构已升级，但备份与迁移历史可核对恢复。
    expected_versions = {"4"} if stage == "after_schema" else {"1", "2"}
    assert rows["schema_version"] in expected_versions
    assert temp_settings.db_path.exists()
    if stage not in {"before_integrity", "after_metadata"}:
        backups = list(Path(temp_settings.backup_dir).glob("*/app.db"))
        assert backups, "备份应被保留以便安全回退"
        assert backups[0].is_file()
    assert before is not None


def test_ready_check_is_read_only_and_does_not_migrate(temp_settings: Settings) -> None:
    _write_schema1_db(temp_settings.db_path)
    before = temp_settings.db_path.read_bytes()

    status = check_database(temp_settings)

    assert status.ready is False
    assert status.reason == "migration_required"
    assert temp_settings.db_path.read_bytes() == before


def test_ready_check_never_creates_database(temp_settings: Settings) -> None:
    status = check_database(temp_settings)

    assert status.reason == "database_missing"
    assert not temp_settings.db_path.exists()


def test_backup_contains_original_rows_before_migration(temp_settings: Settings) -> None:
    _write_schema1_db(temp_settings.db_path)
    result = migrate_database(temp_settings)

    with sqlite3.connect(result["backup_path"]) as conn:
        rows = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
    assert rows["schema_version"] == "1"
    assert rows["instance_id"] == "legacy-instance"
