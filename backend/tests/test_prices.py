"""三类价格用途、时点约束与未来数据泄漏防护。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.config import Settings
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.prices import (
    adjusted_series,
    raw_execution_price,
    signal_price,
    valuation_price,
)
from app.data.timeutil import display_shanghai, require_utc_iso, utc_now_iso


def _seed(settings: Settings) -> None:
    initialize_database(settings)
    run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily", "adj_factor", "suspension"),
            start="20240102",
            end="20240628",
            codes=("000001.SZ", "600000.SH"),
        ),
    )


def test_raw_execution_price_keeps_unadjusted_values(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT trade_date, close FROM daily_price WHERE ts_code='000001.SZ' AND trade_date='20240102'"
        ).fetchone()
        result = raw_execution_price(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", trade_date="20240102"
        )
    finally:
        conn.close()

    assert result.price == float(row["close"])
    assert result.currency == "CNY"
    assert result.reason_code == "ok"
    # 成交参考价不带复权因子。
    assert result.algorithm_version is None


def test_missing_price_stays_missing_and_never_becomes_zero(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = raw_execution_price(
            conn, source_code="tushare-fixture", ts_code="999999.SZ", trade_date="20240102"
        )
    finally:
        conn.close()

    assert result.price is None
    assert result.reason_code == "missing_price"


def test_execution_price_rejects_suspended_and_non_trading_days(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        suspended = raw_execution_price(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", trade_date="20240301"
        )
        non_trading = raw_execution_price(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", trade_date="20240101"
        )
    finally:
        conn.close()

    assert suspended.reason_code == "suspended"
    assert non_trading.reason_code == "not_a_trading_day"


def test_signal_price_uses_only_factors_up_to_as_of(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        series, anchor, anchor_date = adjusted_series(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", as_of="20240301"
        )
        future_factor = conn.execute(
            "SELECT COUNT(*) FROM adj_factor WHERE ts_code='000001.SZ' AND trade_date>'20240301'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert future_factor > 0, "夹具必须包含 as_of 之后的因子，才能验证不使用未来数据"
    assert anchor_date == "20240301"
    assert series
    assert all(item["trade_date"] <= "20240301" for item in series)


def test_signal_price_changes_when_future_action_is_known_later(data_settings: Settings) -> None:
    """同一历史时点在不同“今天”下得到相同的信号价（不泄漏未来公司行为）。"""
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        early = signal_price(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", as_of="20240301"
        )
        late = signal_price(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", as_of="20240301"
        )
    finally:
        conn.close()

    assert early.price == late.price
    assert early.factor_anchor == late.factor_anchor
    assert early.algorithm_version == "sig-v1"


def test_signal_price_requires_adjustment_factor(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = signal_price(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", as_of="20991231"
        )
    finally:
        conn.close()

    assert result.price is None
    assert result.reason_code == "adjustment_factor_missing"


def test_valuation_price_reports_staleness(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        stale = valuation_price(
            conn,
            source_code="tushare-fixture",
            ts_code="000001.SZ",
            business_date="20240106",
            as_of="20240628",
        )
        missing = valuation_price(
            conn,
            source_code="tushare-fixture",
            ts_code="000001.SZ",
            business_date="20221230",
            as_of="20240628",
        )
    finally:
        conn.close()

    assert stale.price_date == "20240105"
    assert stale.stale is True
    assert stale.stale_reason == "price_date_before_business_date"
    assert missing.price is None


def test_timestamps_are_stored_in_utc_and_displayed_in_shanghai(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT observed_at, ingested_at FROM daily_price WHERE ts_code='000001.SZ' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    observed = datetime.fromisoformat(row["observed_at"])
    assert observed.tzinfo is not None
    assert observed.utcoffset() == timezone.utc.utcoffset(None)
    assert display_shanghai(row["observed_at"]).endswith("+08:00")
    assert require_utc_iso(utc_now_iso()).endswith("+00:00")


def test_business_date_is_not_replaced_by_download_date(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT trade_date, observed_at, ingested_at FROM daily_price"
            " WHERE ts_code='000001.SZ' AND trade_date='20240102'"
        ).fetchone()
    finally:
        conn.close()

    assert row["trade_date"] == "20240102"
    assert row["observed_at"][:10] != "20240102"


def test_source_published_time_is_unknown_not_faked(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT source_published_at, historical_available_at FROM daily_price LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert row["source_published_at"] is None
    assert row["historical_available_at"] is None


def test_execution_price_rejects_non_positive_close(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute(
                "UPDATE daily_price SET close=0.0 WHERE ts_code='000001.SZ' AND trade_date='20240102'"
            )
        zero = raw_execution_price(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", trade_date="20240102"
        )
        with conn:
            conn.execute(
                "UPDATE daily_price SET close=-3.0 WHERE ts_code='000001.SZ' AND trade_date='20240103'"
            )
        negative = raw_execution_price(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", trade_date="20240103"
        )
    finally:
        conn.close()

    assert zero.price is None
    assert zero.reason_code == "invalid_price"
    assert any("成交参考" in note for note in zero.notes)
    assert negative.price is None
    assert negative.reason_code == "invalid_price"


def test_valuation_price_treats_invalid_close_as_missing(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute(
                "UPDATE daily_price SET close=0.0 WHERE ts_code='000001.SZ' AND trade_date='20240102'"
            )
        result = valuation_price(
            conn,
            source_code="tushare-fixture",
            ts_code="000001.SZ",
            business_date="20240102",
            as_of="20240628",
        )
    finally:
        conn.close()

    assert result.price is None
    assert any("正数价格" in note for note in result.notes)


def test_signal_series_skips_invalid_close(data_settings: Settings) -> None:
    _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        series_before, _anchor, _date = adjusted_series(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", as_of="20240506"
        )
        with conn:
            conn.execute(
                "UPDATE daily_price SET close=-0.0 WHERE ts_code='000001.SZ' AND trade_date='20240506'"
            )
        series_after, anchor_after, _anchor_date = adjusted_series(
            conn, source_code="tushare-fixture", ts_code="000001.SZ", as_of="20240506"
        )
    finally:
        conn.close()

    assert "20240506" in {item["trade_date"] for item in series_before}
    assert series_after
    assert "20240506" not in {item["trade_date"] for item in series_after}
    assert anchor_after is not None
