from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.db import (
    SCHEMA_VERSION,
    DatabaseError,
    check_database,
    initialize_database,
)


def _write_metadata_db(path: Path, schema_version: int, instance_id: str = "existing-instance") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", str(schema_version)),
                ("instance_id", instance_id),
                ("created_at", "2026-01-01T00:00:00+00:00"),
            ],
        )


def test_initialize_creates_metadata(temp_settings: Settings) -> None:
    metadata = initialize_database(temp_settings)

    assert metadata.schema_version == SCHEMA_VERSION
    assert metadata.instance_id
    assert metadata.created_at
    assert temp_settings.db_path.is_file()


def test_initialize_is_idempotent_and_preserves_metadata(temp_settings: Settings) -> None:
    first = initialize_database(temp_settings)
    second = initialize_database(temp_settings)
    third = initialize_database(temp_settings)

    assert first == second == third
    with sqlite3.connect(temp_settings.db_path) as conn:
        rows = conn.execute("SELECT key, value FROM app_metadata ORDER BY key").fetchall()
    assert rows == [
        ("created_at", first.created_at),
        ("instance_id", first.instance_id),
        ("schema_version", str(SCHEMA_VERSION)),
    ]


def test_initialize_reuses_existing_compatible_database(temp_settings: Settings) -> None:
    first = initialize_database(temp_settings)
    # 模拟进程重启：用相同路径重新加载配置与应用，不重置元信息。
    restarted = load_settings({}, project_root=temp_settings.project_root)

    second = initialize_database(restarted)

    assert second == first


def test_ready_after_initialize(temp_settings: Settings) -> None:
    metadata = initialize_database(temp_settings)

    status = check_database(temp_settings)

    assert status.ready is True
    assert status.reason is None
    assert status.metadata == metadata


def test_check_does_not_create_missing_database(temp_settings: Settings) -> None:
    status = check_database(temp_settings)

    assert status.ready is False
    assert status.reason == "database_missing"
    assert not temp_settings.db_path.exists()


def test_corrupt_database_is_reported_and_preserved(temp_settings: Settings) -> None:
    temp_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    original = b"this is not a sqlite database" * 10
    temp_settings.db_path.write_bytes(original)

    with pytest.raises(DatabaseError) as excinfo:
        initialize_database(temp_settings)
    assert excinfo.value.reason == "database_corrupt"

    status = check_database(temp_settings)
    assert status.ready is False
    assert status.reason == "database_corrupt"
    assert temp_settings.db_path.read_bytes() == original


def test_incompatible_schema_is_reported_and_preserved(temp_settings: Settings) -> None:
    _write_metadata_db(temp_settings.db_path, schema_version=SCHEMA_VERSION + 1)
    before = temp_settings.db_path.read_bytes()

    with pytest.raises(DatabaseError) as excinfo:
        initialize_database(temp_settings)
    assert excinfo.value.reason == "schema_incompatible"

    status = check_database(temp_settings)
    assert status.ready is False
    assert status.reason == "schema_incompatible"
    assert temp_settings.db_path.read_bytes() == before


def test_foreign_tables_are_not_modified(temp_settings: Settings) -> None:
    temp_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(temp_settings.db_path) as conn:
        conn.execute("CREATE TABLE other_app (id INTEGER PRIMARY KEY)")
    before = temp_settings.db_path.read_bytes()

    with pytest.raises(DatabaseError) as excinfo:
        initialize_database(temp_settings)
    assert excinfo.value.reason == "schema_incompatible"

    with sqlite3.connect(temp_settings.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "app_metadata" not in tables
    assert temp_settings.db_path.read_bytes() == before


def test_unreadable_parent_is_reported_without_crashing(temp_settings: Settings) -> None:
    blocker = temp_settings.project_root / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    settings = load_settings(
        {"APP_DB_PATH": str(blocker / "app.db")},
        project_root=temp_settings.project_root,
    )

    with pytest.raises(DatabaseError) as excinfo:
        initialize_database(settings)
    assert excinfo.value.reason == "database_unavailable"
    assert check_database(settings).reason == "database_unavailable"


def test_chinese_path_with_spaces_works(tmp_path: Path) -> None:
    root = tmp_path / "中文 路径"
    settings = load_settings({"APP_RUNTIME_DIR": "运行 数据"}, project_root=root)

    metadata = initialize_database(settings)
    status = check_database(settings)

    assert status.ready is True
    assert status.metadata == metadata
