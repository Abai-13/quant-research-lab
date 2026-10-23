"""流式质量检查与全量语义等价、逻辑/修订计数与轻量状态。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from app.config import Settings
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.quality import run_dataset_quality_check
from app.data.records import CATEGORY_DAILY
from app.data.status import data_status
from app.data.store import coverage_summary, write_records


def _seed(settings: Settings) -> None:
    initialize_database(settings)
    result = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily", "adj_factor", "suspension"),
            start="20240102",
            end="20240628",
            codes=("000001.SZ", "600000.SH"),
        ),
    )
    assert result.status == "completed"


def _issue_signature(report: dict) -> set[str]:
    return {
        json.dumps(
            [item["rule_code"], item["scope"]],
            ensure_ascii=False,
            sort_keys=True,
        )
        for item in report["issues"]
    }


def test_streaming_quality_matches_full_scan(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    try:
        full = run_dataset_quality_check(
            conn, "tushare-fixture", as_of="20240628", today=date(2024, 6, 30), batch_size=5000
        )
        tiny = run_dataset_quality_check(
            conn, "tushare-fixture", as_of="20240628", today=date(2024, 6, 30), batch_size=1
        )
    finally:
        conn.close()
    assert full["record_count"] == tiny["record_count"]
    assert full["latest_trade_date"] == tiny["latest_trade_date"]
    assert _issue_signature(full) == _issue_signature(tiny)


def test_coverage_counts_separate_logical_records_and_revisions(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        before = coverage_summary(conn, "tushare-fixture")
        record = conn.execute(
            "SELECT * FROM daily_price WHERE ts_code='000001.SZ' ORDER BY trade_date LIMIT 1"
        ).fetchone()
        payload = {
            "ts_code": record["ts_code"],
            "trade_date": record["trade_date"],
            "open": record["open"],
            "high": record["high"],
            "low": record["low"],
            "close": (record["close"] or 0) + 1.0,
            "pre_close": record["pre_close"],
            "volume": record["volume"],
            "amount": record["amount"],
            "currency": record["currency"],
            "source_volume_raw": record["source_volume_raw"],
            "source_amount_raw": record["source_amount_raw"],
        }
        write_records(conn, CATEGORY_DAILY, [payload], source_code="tushare-fixture")
        after = coverage_summary(conn, "tushare-fixture")
    finally:
        conn.close()
    assert after["daily"]["logical_count"] == before["daily"]["logical_count"]
    assert after["daily"]["revision_rows"] == before["daily"]["revision_rows"] + 1
    assert after["daily"]["stale"] is False


def test_status_returns_bounded_lists_and_counts(data_settings: Settings) -> None:
    _seed(data_settings)
    status = data_status(data_settings)
    assert status["status"] in {"available_in_range", "limited", "pending_check", "partial_coverage"}
    assert status["version_count"] >= 1
    assert len(status["data_versions"]) <= 20
    assert status["record_counts"]["logical"] > 0
    assert status["record_counts"]["revision_rows"] >= status["record_counts"]["logical"]
    assert len(status["pool_snapshots"]) <= 10
