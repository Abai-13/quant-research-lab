"""C4 闭环回归：接口重复读取、归档 manifest 备份、恢复碰撞、摘要未知。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.data.backup import create_backup, verify_backup
from app.data.chunks import publish_chunked_version, version_members
from app.data.errors import DataError
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.records import ALL_CATEGORIES, CATEGORY_DAILY
from app.data.retention import apply_cleanup, preview_cleanup, restore_version
from app.data.schema import create_schema
from app.data.sources import interface_specs
from app.data.store import coverage_summary


def test_interface_specs_are_repeatable() -> None:
    first = interface_specs("tushare-fixture")
    second = interface_specs("tushare-fixture")
    assert first
    assert first == second
    assert {item.interface for item in first} >= {"trade_cal", "stock_basic", "daily"}


def _seed_archive(settings: Settings) -> tuple[str, str]:
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
    imported = result.version["version_id"]
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, trade_date, content_hash FROM daily_price WHERE ts_code='000001.SZ'"
            " ORDER BY trade_date"
        ).fetchall()
        members = [
            (row["id"], f"tushare-fixture|daily|000001.SZ|{row['trade_date']}", row["content_hash"])
            for row in rows
        ]
        v1 = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20240102", "end": "20240103"},
            categories=[CATEGORY_DAILY],
            rule_version="qr-test",
            quality_summary={},
            members={CATEGORY_DAILY: members},
        )["version_id"]
        with conn:
            conn.execute(
                "UPDATE data_version SET published_at='2020-01-01T00:00:00+00:00'"
                " WHERE version_id=?",
                (v1,),
            )
        v2 = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20240103", "end": "20240103"},
            categories=[CATEGORY_DAILY],
            rule_version="qr-test",
            quality_summary={},
            members={CATEGORY_DAILY: members[1:]},
        )["version_id"]
        with conn:
            conn.execute("DELETE FROM data_version_chunk WHERE version_id=?", (imported,))
            conn.execute("DELETE FROM data_version WHERE version_id=?", (imported,))
    finally:
        conn.close()
    return v1, v2


def test_backup_copies_archive_gz_and_manifest(data_settings: Settings) -> None:
    _v1, _v2 = _seed_archive(data_settings)
    preview = preview_cleanup(data_settings, source_code="tushare-fixture", as_of="20300101")
    apply_cleanup(data_settings, preview_id=preview["preview_id"])
    backup = create_backup(data_settings)
    assert backup.verified
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    archive_entries = [entry for entry in manifest["files"] if entry["kind"] == "archive"]
    assert len(archive_entries) == 2  # .jsonl.gz 与 .manifest.json
    assert any(entry["path"].endswith(".jsonl.gz") for entry in archive_entries)
    assert any(entry["path"].endswith(".manifest.json") for entry in archive_entries)


def test_restore_rejects_non_empty_target(data_settings: Settings) -> None:
    v1, _v2 = _seed_archive(data_settings)
    preview = preview_cleanup(data_settings, source_code="tushare-fixture", as_of="20300101")
    apply_cleanup(data_settings, preview_id=preview["preview_id"])

    target = Path(data_settings.project_root) / "occupied.db"
    with sqlite3.connect(target) as conn:
        create_schema(conn)
        conn.execute(
            "INSERT INTO daily_price(id, ts_code, trade_date, close, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES (1, '999999.SZ', '20240102', 1.0,"
            " 'tushare-fixture', 1, 'h', 'x', 'x')"
        )
    with pytest.raises(DataError) as excinfo:
        restore_version(data_settings, version_id=v1, target_db=target)
    assert excinfo.value.reason == "restore_target_not_empty"
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0] == 1


def test_hold_types_protect_chunks_and_ranges(data_settings: Settings) -> None:
    from app.data.retention import add_hold, list_holds

    v1, v2 = _seed_archive(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    try:
        unique = conn.execute(
            "SELECT chunk_id FROM data_version_chunk WHERE version_id=?"
            " AND chunk_id NOT IN (SELECT chunk_id FROM data_version_chunk WHERE version_id=?)"
            " LIMIT 1",
            (v1, v2),
        ).fetchone()[0]
        add_hold(conn, target_type="chunk", target_id=unique, reason="chunk-test")
        add_hold(
            conn,
            target_type="record_range",
            target_id="daily|20230110|20230110",
            reason="range-test",
        )
        assert len(list_holds(conn)) == 2
        preview = preview_cleanup(data_settings, source_code="tushare-fixture", as_of="20300101")
        action = next(item for item in preview["plan"]["actions"] if item["version_id"] == v1)
        assert unique not in action["reclaimable_chunk_ids"]
        daily_rows = next(
            item for item in action["records"] if item["category"] == "daily"
        ) if action["records"] else {"record_ids": []}
        held_date_id = conn.execute(
            "SELECT id FROM daily_price WHERE trade_date='20230110'"
        ).fetchone()
        if held_date_id:
            assert int(held_date_id[0]) not in daily_rows["record_ids"]

        with conn:
            conn.execute(
                "INSERT INTO retention_hold(hold_id, target_type, target_id, reason, created_at)"
                " VALUES ('bad', 'unsupported', 'x', 'x', 'x')"
            )
    finally:
        conn.close()
    with pytest.raises(DataError) as excinfo:
        preview_cleanup(data_settings, source_code="tushare-fixture", as_of="20300101")
    assert excinfo.value.reason == "unsupported_hold_type"


def test_missing_coverage_stats_is_explicitly_unknown(temp_settings: Settings) -> None:
    initialize_database(temp_settings)
    conn = sqlite3.connect(temp_settings.db_path)
    try:
        summary = coverage_summary(conn, "tushare-fixture")
    finally:
        conn.close()
    assert summary["daily"]["source"] == "unknown"
    assert summary["daily"]["logical_count"] is None
    assert summary["daily"]["stale"] is True
