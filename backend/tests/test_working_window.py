"""五年滚动工作窗口：增量发布真实裁剪成员，保留预热与锚点。"""

from __future__ import annotations

import sqlite3

from app.config import Settings
from app.data.chunks import publish_chunked_version, version_members
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.records import CATEGORY_DAILY
from app.data.store import write_records


def _daily(ts_code: str, trade_date: str, close: float = 10.0) -> dict:
    return {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": close,
        "volume": 1000.0,
        "amount": close * 1000.0,
        "currency": "CNY",
        "source_volume_raw": 10.0,
        "source_amount_raw": close,
    }


def test_working_version_trims_outside_window_and_keeps_anchor(temp_settings: Settings) -> None:
    initialize_database(temp_settings)
    conn = sqlite3.connect(temp_settings.db_path)
    try:
        write_records(
            conn,
            CATEGORY_DAILY,
            [
                _daily("000001.SZ", "20190102"),
                _daily("000001.SZ", "20200102"),
                _daily("000001.SZ", "20240102"),
            ],
            source_code="tushare-fixture",
        )
        version = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20190102", "end": "20240102"},
            categories=[CATEGORY_DAILY],
            rule_version="qr-test",
            quality_summary={},
            window_mode="working",
            window_start="20200601",
        )
        members = version_members(conn, version["version_id"])
        dates = {key.rsplit("|", 1)[1] for category, _rid, _hash, key in members if category == "daily"}
        assert "20190102" not in dates
        assert "20200102" in dates  # 窗口前最后一条锚点
        assert "20240102" in dates
        row = conn.execute(
            "SELECT window_mode, window_start FROM data_version WHERE version_id=?",
            (version["version_id"],),
        ).fetchone()
        assert row == ("working", "20200601")
    finally:
        conn.close()


def test_incremental_pipeline_publishes_working_window(data_settings: Settings) -> None:
    initialize_database(data_settings)
    first = run_import(
        data_settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily"),
            start="20240102",
            end="20240105",
            codes=("000001.SZ",),
        ),
    )
    assert first.status == "completed"
    assert first.version["window_mode"] == "full"
    second = run_import(
        data_settings,
        ImportOptions(
            mode="incremental",
            categories=("daily",),
            codes=("000001.SZ",),
            end="20240105",
        ),
    )
    assert second.status == "completed"
    assert second.version["window_mode"] == "working"
    assert second.version["window_start"]
    with sqlite3.connect(data_settings.db_path) as conn:
        row = conn.execute(
            "SELECT window_mode, window_start FROM data_version WHERE version_id=?",
            (second.version["version_id"],),
        ).fetchone()
    assert row[0] == "working"
    assert row[1] == "20190105"
