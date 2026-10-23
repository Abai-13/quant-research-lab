"""六年滚动工作窗口：连续边界样例证明窗口裁剪与旧数据可回收。"""

from __future__ import annotations

import sqlite3

from app.config import Settings
from app.data.chunks import VersionView, publish_chunked_version, version_members
from app.data.migration import initialize_database
from app.data.records import CATEGORY_DAILY
from app.data.retention import _window_start, apply_cleanup, preview_cleanup
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


def _six_years() -> list[str]:
    days: list[str] = []
    for year in range(2019, 2025):
        days.extend([f"{year}0102", f"{year}0701", f"{year}1231"])
    return days


def test_window_start_handles_leap_day() -> None:
    assert _window_start("20240229", 5) == "20190228"
    assert _window_start("20241231", 5) == "20191231"


def test_six_year_rolling_window_trims_and_reclaims(data_settings: Settings) -> None:
    initialize_database(data_settings)
    codes = ("000001.SZ", "600000.SH")
    days = _six_years()
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        write_records(
            conn,
            CATEGORY_DAILY,
            [_daily(code, day) for code in codes for day in days],
            source_code="tushare-fixture",
        )
        full = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": days[0], "end": days[-1]},
            categories=[CATEGORY_DAILY],
            rule_version="qr-test",
            quality_summary={},
        )
        # 让全量版本成为“过期普通版本”。
        with conn:
            conn.execute(
                "UPDATE data_version SET published_at='2020-01-01T00:00:00+00:00'"
                " WHERE version_id=?",
                (full["version_id"],),
            )
        # 以 2024 年末为截止的五年窗口滚动版本：预算窗口 20191231。
        working = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": days[0], "end": days[-1]},
            categories=[CATEGORY_DAILY],
            rule_version="qr-test",
            quality_summary={},
            base_version_id=full["version_id"],
            affected_partitions={},
            window_mode="working",
            window_start="20191231",
        )
        working_members = version_members(conn, working["version_id"])
        work_dates = {
            key.rsplit("|", 1)[1] for category, _rid, _hash, key in working_members
            if category == "daily"
        }
        assert "20190102" not in work_dates
        assert "20190701" in work_dates  # 窗口前最后一条锚点
        assert "20241231" in work_dates
        member_count_before = len(working_members)
        chunks_before = conn.execute("SELECT COUNT(*) FROM data_chunk").fetchone()[0]
        rows_before = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
    finally:
        conn.close()

    preview = preview_cleanup(data_settings, source_code="tushare-fixture", as_of="20241231")
    result = apply_cleanup(data_settings, preview_id=preview["preview_id"])
    assert result["status"] == "applied"

    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert (
            conn.execute(
                "SELECT retention_state FROM data_version WHERE version_id=?",
                (full["version_id"],),
            ).fetchone()[0]
            == "archived"
        )
        # 窗口前的 2019 年初记录已回收；锚点与窗口内记录保留。
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_price WHERE trade_date='20190102'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_price WHERE trade_date='20190701'"
        ).fetchone()[0] == len(codes)
        rows_after = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
        assert rows_after < rows_before
        chunks_after = conn.execute("SELECT COUNT(*) FROM data_chunk").fetchone()[0]
        assert chunks_after < chunks_before
        # 滚动版本成员与读取不受归档影响。
        assert len(version_members(conn, working["version_id"])) == member_count_before
        view = VersionView(conn, working["version_id"])
        assert view.daily_on("000001.SZ", "20241231") is not None
        assert view.daily_on("000001.SZ", "20190102") is None
    finally:
        conn.close()
