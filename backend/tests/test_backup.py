"""备份包：DB + 外部文件、侧库快照、路径安全与独立恢复验证。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.data.backup import create_backup, restore_package, verify_backup
from app.data.errors import DataError
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import


def _seed(settings: Settings) -> None:
    initialize_database(settings)
    result = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily"),
            start="20240102",
            end="20240105",
            codes=("000001.SZ",),
        ),
    )
    assert result.status == "completed"


def _write_schema1(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("instance_id", "legacy-instance"),
                ("created_at", "2024-01-01T00:00:00+00:00"),
            ],
        )


def test_backup_includes_external_files_and_restores_independently(data_settings: Settings) -> None:
    _seed(data_settings)
    # 侧库存在且持续可写。
    from app.data.maintenance import open_maintenance, start_run

    side_conn = open_maintenance(data_settings)
    start_run(side_conn, kind="test", from_version=None, to_version=None)
    side_conn.close()
    side = Path(data_settings.runtime_dir) / "maintenance.db"
    assert side.exists()
    backup = create_backup(data_settings)
    assert backup.verified is True
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    kinds = {entry["kind"] for entry in manifest["files"]}
    assert "raw" in kinds
    assert "side_db" in kinds
    for entry in manifest["files"]:
        assert entry["package_path"].startswith("external/") or entry["package_path"] == "maintenance.db"

    # 备份后继续写侧库，不得让旧包摘要失效。
    with sqlite3.connect(side) as conn:
        conn.execute(
            "INSERT INTO maintenance_run(run_id, kind, state, started_at, updated_at)"
            " VALUES ('after-backup', 'test', 'completed', 'x', 'x')"
        )
    assert verify_backup(backup.path)["verified"] is True

    target = Path(data_settings.project_root) / "restored"
    restored = restore_package(backup.path, target)
    assert restored["status"] == "restored"
    assert restored["external_files"] >= 1
    with sqlite3.connect(restored["database"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0] > 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_of_schema1_database_succeeds(temp_settings: Settings) -> None:
    _write_schema1(temp_settings.db_path)
    backup = create_backup(temp_settings)
    assert backup.verified is True
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    assert manifest["database"]["schema_version"] == "1"
    assert [entry for entry in manifest["files"] if entry["kind"] == "raw"] == []


def test_backup_rejects_path_escape(data_settings: Settings) -> None:
    _seed(data_settings)
    with sqlite3.connect(data_settings.db_path) as conn:
        conn.execute(
            "INSERT INTO raw_archive(digest, path, content_bytes, interface, source_code,"
            " fetched_at, first_batch_id) VALUES ('x', '../evil', 1, 'daily', 'tushare-fixture',"
            " '2024-01-01T00:00:00+00:00', 'b')"
        )
    with pytest.raises(DataError) as excinfo:
        create_backup(data_settings)
    assert excinfo.value.reason == "backup_unsafe_source"


def test_verify_detects_tampered_external_file(data_settings: Settings) -> None:
    _seed(data_settings)
    backup = create_backup(data_settings)
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    raw_entry = next(entry for entry in manifest["files"] if entry["kind"] == "raw")
    target = backup.path / raw_entry["package_path"]
    target.write_bytes(b"tampered")
    result = verify_backup(backup.path)
    assert result["verified"] is False
