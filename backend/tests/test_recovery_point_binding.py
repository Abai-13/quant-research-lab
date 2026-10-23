"""R2：显式替代恢复点必须绑定本库实例、路径与操作前完整内容。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.data.backup import MANIFEST_NAME, create_backup, restore_package
from app.data.errors import DataError
from app.data.migration import initialize_database
from app.data.retention import apply_cleanup, preview_cleanup
from app.data.store import write_records
from app.data.records import CATEGORY_DAILY
from test_cleanup import _seed

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _settings(tmp_path: Path, name: str) -> Settings:
    return load_settings(
        {
            "APP_DATA_SOURCE": "tushare-fixture",
            "APP_RUNTIME_DIR": str(tmp_path / name / "runtime"),
            "APP_DATA_FIXTURE_DIR": str(FIXTURES_DIR / "tushare"),
            "APP_IMPORT_MIN_INTERVAL_SECONDS": "0",
            "APP_IMPORT_BACKOFF_SECONDS": "0,0",
        },
        project_root=tmp_path / name,
    )


def _daily_count(settings: Settings) -> int:
    with sqlite3.connect(settings.db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0])


def _raw_files(settings: Settings) -> list[str]:
    root = Path(settings.raw_archive_dir)
    if not root.is_dir():
        return []
    return sorted(str(item.relative_to(root)) for item in root.rglob("*") if item.is_file())


def _apply(settings: Settings, preview_id: str, recovery_point: Path) -> dict:
    return apply_cleanup(
        settings,
        preview_id=preview_id,
        create_recovery_point=False,
        recovery_point=recovery_point,
    )


def test_recovery_point_from_another_database_is_rejected(
    data_settings: Settings, tmp_path: Path
) -> None:
    _seed(data_settings)
    other = _settings(tmp_path, "other")
    initialize_database(other)
    other_backup = create_backup(other, verify=True)
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    before_rows = _daily_count(data_settings)
    before_raw = _raw_files(data_settings)

    with pytest.raises(DataError) as excinfo:
        _apply(data_settings, preview["preview_id"], other_backup.path)
    assert excinfo.value.reason in {
        "recovery_point_instance_mismatch",
        "recovery_point_database_mismatch",
        "recovery_point_state_mismatch",
    }

    assert _daily_count(data_settings) == before_rows
    assert _raw_files(data_settings) == before_raw
    with sqlite3.connect(data_settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM archive_manifest").fetchone()[0] == 0
    # 其他实例的备份包本身也不能被本库清理删除。
    assert (other_backup.path / MANIFEST_NAME).is_file()


def test_same_instance_backup_does_not_cover_later_request_log_change(
    data_settings: Settings,
) -> None:
    _seed(data_settings)
    backup = create_backup(data_settings, verify=True)
    # 同实例、同 generation 下追加一行请求日志；只有完整内容证明才能发现。
    with sqlite3.connect(data_settings.db_path) as conn:
        conn.execute(
            "INSERT INTO import_request_log(batch_id, interface, request_params, page_no,"
            " raw_digest, status, attempt, started_at)"
            " VALUES ('b-late', 'daily', '{}', 1, NULL, 'ok', 1,"
            " '2024-06-01T00:00:00+00:00')"
        )
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    with pytest.raises(DataError) as excinfo:
        _apply(data_settings, preview["preview_id"], backup.path)
    assert excinfo.value.reason == "recovery_point_state_mismatch"


def test_stale_recovery_point_is_rejected(data_settings: Settings) -> None:
    _seed(data_settings)
    backup = create_backup(data_settings, verify=True)
    with sqlite3.connect(data_settings.db_path) as conn:
        write_records(
            conn,
            CATEGORY_DAILY,
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20240104",
                    "open": 12.0,
                    "high": 12.0,
                    "low": 12.0,
                    "close": 12.0,
                    "pre_close": 12.0,
                    "volume": 1.0,
                    "amount": 12.0,
                    "currency": "CNY",
                }
            ],
            source_code="tushare-fixture",
        )
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    with pytest.raises(DataError) as excinfo:
        _apply(data_settings, preview["preview_id"], backup.path)
    assert excinfo.value.reason == "recovery_point_state_mismatch"


def test_tampered_recovery_point_is_rejected(data_settings: Settings) -> None:
    _seed(data_settings)
    backup = create_backup(data_settings, verify=True)
    with sqlite3.connect(backup.path / "app.db") as conn:
        conn.execute(
            "UPDATE daily_price SET close=close+1 WHERE id=(SELECT MIN(id) FROM daily_price)"
        )
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    with pytest.raises(DataError) as excinfo:
        _apply(data_settings, preview["preview_id"], backup.path)
    assert excinfo.value.reason in {
        "recovery_point_invalid",
        "recovery_point_tampered",
    }


def test_same_instance_clone_with_patched_manifest_is_rejected(
    data_settings: Settings, tmp_path: Path
) -> None:
    _seed(data_settings)
    clone = _settings(tmp_path, "clone")
    clone.db_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(data_settings.db_path)
    target = sqlite3.connect(clone.db_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    # 克隆分叉：同 instance_id、同 generation，但多一行请求日志。
    # 克隆库没有原始响应文件，先移除引用，只保留可分叉的数据库行差异。
    with sqlite3.connect(clone.db_path) as conn:
        conn.execute("DELETE FROM raw_archive")
        conn.execute(
            "INSERT INTO import_request_log(batch_id, interface, request_params, page_no,"
            " raw_digest, status, attempt, started_at)"
            " VALUES ('b-clone', 'daily', '{}', 1, NULL, 'ok', 1,"
            " '2024-06-01T00:00:00+00:00')"
        )
    clone_backup = create_backup(clone, verify=True)
    # 把 manifest 的数据库路径改成本库路径，只剩实例 UUID/generation/路径相符。
    manifest_path = clone_backup.path / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["path"] = str(Path(data_settings.db_path).resolve())
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    with pytest.raises(DataError) as excinfo:
        _apply(data_settings, preview["preview_id"], clone_backup.path)
    assert excinfo.value.reason in {
        "recovery_point_state_mismatch",
        "recovery_point_tampered",
    }


def test_correct_recovery_point_is_accepted_and_actually_restorable(
    data_settings: Settings, tmp_path: Path
) -> None:
    _seed(data_settings)
    before_rows = _daily_count(data_settings)
    backup = create_backup(data_settings, verify=True)
    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    applied = _apply(data_settings, preview["preview_id"], backup.path)
    assert applied["status"] == "applied"
    assert applied["recovery_point_binding"] == "instance+path+content"
    assert Path(applied["recovery_point"]).resolve() == backup.path.resolve()

    target = tmp_path / "restored-from-recovery-point"
    restored = restore_package(backup.path, target)
    with sqlite3.connect(restored["database"]) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
            == before_rows
        )
        assert (
            conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        )
