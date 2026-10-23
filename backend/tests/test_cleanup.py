"""清理预览/应用：只读预览、generation 失效、归档读回、恢复与引用保护。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.data.chunks import publish_chunked_version, version_members
from app.data.errors import DataError
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.records import ALL_CATEGORIES, CATEGORY_DAILY
from app.data.retention import (
    add_hold,
    apply_cleanup,
    preview_cleanup,
    restore_version,
)
from app.data.schema import create_schema
from app.data.store import write_records


def _seed(settings: Settings) -> tuple[str, str]:
    initialize_database(settings)
    result = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily"),
            start="20240102",
            end="20240103",
            codes=("000001.SZ",),
        ),
    )
    assert result.status == "completed"
    imported_version = result.version["version_id"]
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, trade_date, content_hash FROM daily_price WHERE ts_code='000001.SZ'"
            " ORDER BY trade_date"
        ).fetchall()
        members = {
            CATEGORY_DAILY: [
                (row["id"], f"tushare-fixture|daily|000001.SZ|{row['trade_date']}", row["content_hash"])
                for row in rows
            ]
        }
        v1 = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20240102", "end": "20240103"},
            categories=list(ALL_CATEGORIES),
            rule_version="qr-test",
            quality_summary={},
            members=members,
        )["version_id"]
        # 让 V1 成为“过期普通版本”。
        with conn:
            conn.execute(
                "UPDATE data_version SET published_at='2020-01-01T00:00:00+00:00'"
                " WHERE version_id=?",
                (v1,),
            )
        # V2 模拟滚动窗口：只保留较新一天，V1 的另一天分区成为唯一块。
        v2 = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20240103", "end": "20240103"},
            categories=list(ALL_CATEGORIES),
            rule_version="qr-test",
            quality_summary={},
            members={
                CATEGORY_DAILY: [
                    item for item in members[CATEGORY_DAILY] if not item[1].endswith("|20230110")
                ]
            },
        )["version_id"]
        # 测试设置：移除导入自动产生的共同基线版本，使 V1 拥有独占分区。
        with conn:
            conn.execute(
                "DELETE FROM data_version_chunk WHERE version_id=?", (imported_version,)
            )
            conn.execute("DELETE FROM data_version WHERE version_id=?", (imported_version,))
    finally:
        conn.close()
    return v1, v2


def test_preview_is_read_only_and_apply_archives_then_reclaims(data_settings: Settings) -> None:
    v1, v2 = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    try:
        before = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
        chunks_before = conn.execute("SELECT COUNT(*) FROM data_chunk").fetchone()[0]
        generation_before = conn.execute("SELECT generation FROM maintenance_generation").fetchone()[0]
    finally:
        conn.close()

    preview = preview_cleanup(
        data_settings, source_code="tushare-fixture", as_of="20300101"
    )
    assert preview["summary"]["candidate_versions"] >= 1
    conn = sqlite3.connect(data_settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM data_chunk").fetchone()[0] == chunks_before
        assert (
            conn.execute("SELECT generation FROM maintenance_generation").fetchone()[0]
            == generation_before
        )
    finally:
        conn.close()

    applied = apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert applied["status"] == "applied"
    assert applied["recovery_point"]

    conn = sqlite3.connect(data_settings.db_path)
    try:
        assert (
            conn.execute(
                "SELECT retention_state FROM data_version WHERE version_id=?", (v1,)
            ).fetchone()[0]
            == "archived"
        )
        with pytest.raises(DataError):
            version_members(conn, v1)
        # 窗口外、未被 V2 引用的记录被回收；V2 仍可读。
        remaining = conn.execute(
            "SELECT trade_date FROM daily_price WHERE ts_code='000001.SZ' ORDER BY trade_date"
        ).fetchall()
        remaining_dates = [row[0] for row in remaining]
        assert len(remaining_dates) == before - 1
        assert "20230110" not in remaining_dates
        assert version_members(conn, v2)
        archive = conn.execute(
            "SELECT archive_id, path, sha256 FROM archive_manifest WHERE version_id=?", (v1,)
        ).fetchone()
        assert archive is not None
        assert (Path(data_settings.runtime_dir) / "archives" / archive[1]).is_file()
    finally:
        conn.close()

    # 恢复到独立新库，旧 ID 仍可读。
    target = Path(data_settings.project_root) / "restored-cleanup.db"
    with sqlite3.connect(target) as scratch:
        create_schema(scratch)
    restored = restore_version(data_settings, version_id=v1, target_db=target)
    assert restored["record_count"] == before
    with sqlite3.connect(target) as scratch:
        assert version_members(scratch, v1)
        assert scratch.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0] == before


def test_preview_is_invalidated_by_later_writes(data_settings: Settings) -> None:
    _seed(data_settings)
    preview = preview_cleanup(data_settings, source_code="tushare-fixture", as_of="20300101")
    conn = sqlite3.connect(data_settings.db_path)
    try:
        write_records(
            conn,
            CATEGORY_DAILY,
            [{
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
            }],
            source_code="tushare-fixture",
        )
    finally:
        conn.close()
    with pytest.raises(DataError) as excinfo:
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert excinfo.value.reason == "preview_stale"


def test_new_pool_snapshot_reference_invalidates_preview(data_settings: Settings) -> None:
    from app.data.universe import generate_pool_snapshot

    v1, _v2 = _seed(data_settings)
    preview = preview_cleanup(data_settings, source_code="tushare-fixture", as_of="20300101")
    conn = sqlite3.connect(data_settings.db_path)
    try:
        generate_pool_snapshot(
            conn,
            as_of="20240103",
            data_version_id=v1,
            source_code="tushare-fixture",
        )
    finally:
        conn.close()
    with pytest.raises(DataError) as excinfo:
        apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert excinfo.value.reason == "preview_stale"


def test_hold_blocks_version_retirement(data_settings: Settings) -> None:
    v1, _v2 = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    try:
        add_hold(conn, target_type="version", target_id=v1, reason="research")
    finally:
        conn.close()
    preview = preview_cleanup(data_settings, source_code="tushare-fixture", as_of="20300101")
    actions = preview["plan"]["actions"]
    assert all(action["version_id"] != v1 for action in actions)
