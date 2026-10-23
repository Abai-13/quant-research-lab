"""质量规则：缺失、重复、异常价格、单位、时间顺序与陈旧数据。"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from app.config import Settings
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.quality import (
    QUALITY_RULE_VERSION,
    build_context,
    check_future_records,
    check_staleness,
    run_dataset_quality_check,
)


def _rule_codes(issues) -> set[str]:
    return {issue["rule_code"] for issue in issues}


def test_anomaly_import_detects_specific_rules(fixture_settings_factory) -> None:
    settings = fixture_settings_factory("anomalies")
    initialize_database(settings)

    result = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily"),
            start="20231229",
            end="20240110",
            codes=("000001.SZ", "900002.SZ", "900003.SZ"),
        ),
    )

    codes = _rule_codes(result.issues)
    assert "Q004_non_finite_price" in codes
    assert "Q005_invalid_ohlc_relation" in codes
    assert "Q006_negative_volume_or_amount" in codes
    assert "Q007_unit_inconsistency" in codes
    assert "Q009_unknown_security" in codes
    assert "Q010_before_listing_date" in codes
    assert "Q011_after_delisting_date" in codes
    assert "Q015_future_dated_record" in codes
    assert "Q017_non_trading_day_quote" in codes


def test_duplicate_conflicting_key_is_reported(fixture_settings_factory) -> None:
    settings = fixture_settings_factory("anomalies")
    initialize_database(settings)

    result = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("daily",),
            start="20240102",
            end="20240110",
            codes=("000001.SZ",),
        ),
    )

    assert "Q003_duplicate_key_conflict" in _rule_codes(result.issues)


def test_anomalous_values_are_not_silently_corrected(fixture_settings_factory) -> None:
    settings = fixture_settings_factory("anomalies")
    initialize_database(settings)

    run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("daily",),
            start="20240102",
            end="20240110",
            codes=("000001.SZ",),
        ),
    )

    with sqlite3.connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT trade_date, open, high, low, close, volume, amount, source_volume_raw,"
            " source_amount_raw FROM daily_price WHERE ts_code='000001.SZ' ORDER BY trade_date, id"
        ).fetchall()
    by_date: dict[str, list] = {}
    for row in rows:
        by_date.setdefault(row[0], []).append(row)
    # 非有限价格保持缺失（None），没有被改写成 0 或其他数值。
    assert by_date["20240103"][0][1] is None
    # 错误 OHLC 原样保留。
    assert by_date["20240104"][0][2] == 9.5
    assert by_date["20240104"][0][3] == 10.5
    # 负成交量原样保留。
    assert by_date["20240105"][0][5] == -100000.0
    # 单位混乱原样保留：来源原始值与规范化值都可核对。
    assert by_date["20240108"][0][8] == 1.0
    assert by_date["20240108"][0][6] == 1000.0


def test_dataset_quality_check_reports_rule_version(data_settings: Settings) -> None:
    initialize_database(data_settings)
    run_import(
        data_settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily"),
            start="20240102",
            end="20240628",
            codes=("000001.SZ", "600000.SH"),
        ),
    )
    conn = sqlite3.connect(data_settings.db_path)
    try:
        report = run_dataset_quality_check(conn, "tushare-fixture", as_of="20240628", today=date(2024, 6, 30))
    finally:
        conn.close()

    assert report["summary"]["rule_version"] == QUALITY_RULE_VERSION
    assert report["record_count"] > 0
    assert report["latest_trade_date"] == "20240628"
    codes = _rule_codes(report["issues"])
    assert "Q016_historical_availability_unproven" in codes


def test_staleness_is_measured_in_trading_days(data_settings: Settings) -> None:
    initialize_database(data_settings)
    run_import(
        data_settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "daily"),
            start="20240102",
            end="20240105",
            codes=("000001.SZ",),
        ),
    )
    conn = sqlite3.connect(data_settings.db_path)
    try:
        context = build_context(conn, "tushare-fixture", as_of="20240628")
    finally:
        conn.close()

    issues = check_staleness(
        latest_trade_date="20240105",
        trading_days=sorted(context.trading_days),
        as_of="20240628",
    )

    assert issues
    assert issues[0].rule_code == "Q014_stale_data"


def test_holidays_do_not_require_quotes(data_settings: Settings) -> None:
    initialize_database(data_settings)
    run_import(
        data_settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "daily"),
            start="20240102",
            end="20240628",
            codes=("000001.SZ",),
        ),
    )
    conn = sqlite3.connect(data_settings.db_path)
    try:
        context = build_context(conn, "tushare-fixture", as_of="20240628")
    finally:
        conn.close()

    # 2024-01-01 元旦、2024-02-12~16 春节休市，不要求行情。
    assert "20240101" not in context.trading_days
    assert "20240213" not in context.trading_days
    assert "20240215" not in context.trading_days
    assert "20240102" in context.trading_days


def test_future_records_are_flagged(data_settings: Settings) -> None:
    issues = check_future_records(
        [{"ts_code": "000001.SZ", "trade_date": "20991231"}], today=date(2024, 6, 28)
    )

    assert issues
    assert issues[0].rule_code == "Q015_future_dated_record"


def test_non_positive_stored_price_flags_q018_with_dates_in_scope() -> None:
    from app.data.quality import QualityContext, check_daily_records

    records = [
        {"ts_code": "000001.SZ", "trade_date": "20240102", "close": 0.0},
        {"ts_code": "000001.SZ", "trade_date": "20240103", "open": -1.0},
        {"ts_code": "000001.SZ", "trade_date": "20240105", "close": 10.0},
    ]

    issues = check_daily_records(records, QualityContext())

    q018 = [issue for issue in issues if issue.rule_code == "Q018_invalid_price_format"]
    assert q018
    assert q018[0].severity == "high"
    assert set(q018[0].scope["trade_dates"]) == {"20240102", "20240103"}
    assert any("正数" in sample for sample in (q018[0].evidence or "").split("；"))


def test_repeated_issue_keeps_full_trade_date_set_beyond_200() -> None:
    from datetime import date, timedelta

    from app.data.quality import QualityContext, check_daily_records

    anchor = date(2019, 1, 1)
    dates = [(anchor + timedelta(days=offset)).strftime("%Y%m%d") for offset in range(250)]
    records = [
        {"ts_code": "000001.SZ", "trade_date": trade_date, "close": 0.0} for trade_date in dates
    ]

    issues = check_daily_records(records, QualityContext())

    q018 = [issue for issue in issues if issue.rule_code == "Q018_invalid_price_format"]
    assert q018
    saved_dates = q018[0].scope["trade_dates"]
    assert len(saved_dates) == len(dates) > 200
    # 完整集合：最早与最新日期都在列表里，不能截断丢失较早日期。
    assert saved_dates[0] == dates[0]
    assert saved_dates[-1] == dates[-1]
    assert set(saved_dates) == set(dates)


def test_calendar_spans_year_boundary_with_pretrade_chain(data_settings: Settings) -> None:
    initialize_database(data_settings)
    run_import(
        data_settings,
        ImportOptions(
            mode="history",
            categories=("calendar",),
            start="20221226",
            end="20240628",
        ),
    )
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT cal_date, is_open, pretrade_date FROM trade_calendar WHERE source_code=?"
            " AND revision_no = (SELECT MAX(revision_no) FROM trade_calendar c2"
            "   WHERE c2.source_code=trade_calendar.source_code"
            "     AND c2.exchange=trade_calendar.exchange"
            "     AND c2.cal_date=trade_calendar.cal_date)"
            " ORDER BY cal_date",
            ("tushare-fixture",),
        ).fetchall()
    finally:
        conn.close()

    days = {row["cal_date"]: row for row in rows}
    # 跨年：2022 年最后一个交易日与 2023 年第一个交易日的衔接。
    assert "20221230" in days
    assert "20230103" in days
    assert days["20230103"]["pretrade_date"] == "20221230"
    # 元旦休市。
    assert days["20240101"]["is_open"] == 0
    assert days["20240102"]["pretrade_date"] == "20231229"
    # 周末不出现在夹具日历里（夹具只收录工作日与显式休市日）。
    assert "20240106" not in days
