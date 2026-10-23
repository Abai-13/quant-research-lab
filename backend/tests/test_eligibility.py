"""数据可用资格接口：不同用途的拒绝契约。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from app.config import Settings
from app.data.chunks import VersionView, publish_chunked_version
from app.data.eligibility import (
    REASON_ELIGIBLE,
    REASON_HISTORICAL_AVAILABILITY_UNPROVEN,
    REASON_INSUFFICIENT_HISTORY,
    REASON_LIMITED,
    REASON_MISSING_PRICE,
    REASON_NOT_A_TRADING_DAY,
    REASON_QUALITY_BLOCKED,
    REASON_SUSPENDED,
    check_eligibility,
)
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import


def _seed(settings: Settings) -> str:
    initialize_database(settings)
    result = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily", "adj_factor", "suspension"),
            start="20230103",
            end="20240628",
            codes=("000001.SZ", "600000.SH"),
        ),
    )
    assert result.status == "completed"
    return result.version["version_id"]


def _insert_short_history(settings: Settings, ts_code: str, *, days: int) -> None:
    """插入一段很短的行情（含截止日），用于验证历史长度不足时的拒绝契约。"""
    dates = ["20240628", "20240627", "20240626", "20240625", "20240624"][:days]
    with sqlite3.connect(settings.db_path) as conn:
        conn.executemany(
            "INSERT INTO daily_price(ts_code, trade_date, close, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES (?, ?, 10.0, 'tushare-fixture', 1, 'h',"
            " '2024-06-28T00:00:00+00:00', '2024-06-28T00:00:00+00:00')",
            [(ts_code, trade_date) for trade_date in dates],
        )
        conn.executemany(
            "INSERT INTO adj_factor(ts_code, trade_date, adj_factor, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES (?, ?, 1.0, 'tushare-fixture', 1, 'f',"
            " '2024-06-28T00:00:00+00:00', '2024-06-28T00:00:00+00:00')",
            [(ts_code, trade_date) for trade_date in dates],
        )
        conn.commit()


def _check(settings: Settings, **kwargs):
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return check_eligibility(
            conn,
            source_code="tushare-fixture",
            data_version_id=kwargs.pop("data_version_id", None),
            **kwargs,
        )
    finally:
        conn.close()


def test_display_purpose_allows_stale_but_labelled(data_settings: Settings) -> None:
    version_id = _seed(data_settings)

    result = _check(
        data_settings,
        purpose="display",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
        business_date="20240106",
    )

    assert result.eligible is True
    assert result.reason_code == REASON_ELIGIBLE
    assert result.details[0]["stale"] is True


def test_valuation_purpose_allows_stale_with_label(data_settings: Settings) -> None:
    version_id = _seed(data_settings)

    result = _check(
        data_settings,
        purpose="valuation_reference",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
        business_date="20240106",
    )

    assert result.eligible is True
    assert result.details[0]["stale"] is True
    assert result.details[0]["stale_reason"] == "price_date_before_business_date"


def test_signal_input_rejects_insufficient_history(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    _insert_short_history(data_settings, "800001.SZ", days=5)
    # 固定版本语义：直接插入的数据只能通过新版本可见；V2 发布后才参与资格判断。
    from app.data.chunks import publish_chunked_version
    from app.data.records import ALL_CATEGORIES

    with sqlite3.connect(data_settings.db_path) as conn:
        v2 = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20240624", "end": "20240628"},
            categories=list(ALL_CATEGORIES),
            rule_version="qr-test",
            quality_summary={"total": 0},
        )

    old_result = _check(
        data_settings,
        purpose="signal_input",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["800001.SZ"],
    )
    assert old_result.eligible is False
    assert old_result.reason_code == REASON_MISSING_PRICE

    result = _check(
        data_settings,
        purpose="signal_input",
        as_of="20240628",
        data_version_id=v2["version_id"],
        ts_codes=["800001.SZ"],
    )

    assert result.eligible is False
    assert result.reason_code == REASON_INSUFFICIENT_HISTORY


def test_signal_input_accepts_when_history_is_sufficient(data_settings: Settings) -> None:
    version_id = _seed(data_settings)

    result = _check(
        data_settings,
        purpose="signal_input",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["000001.SZ", "600000.SH"],
    )

    assert result.eligible is True
    assert result.reason_code == REASON_ELIGIBLE
    assert result.reasons


def test_execution_reference_rejects_non_trading_day(data_settings: Settings) -> None:
    version_id = _seed(data_settings)

    result = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240101",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
        business_date="20240101",
    )

    assert result.eligible is False
    assert result.reason_code == REASON_NOT_A_TRADING_DAY


def test_execution_reference_reports_unknown_calendar(data_settings: Settings) -> None:
    version_id = _seed(data_settings)

    result = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240106",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
        business_date="20240106",
    )

    assert result.eligible is False
    assert result.reason_code == "calendar_unknown"


def test_execution_reference_rejects_suspended_security(data_settings: Settings) -> None:
    version_id = _seed(data_settings)

    result = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240301",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
        business_date="20240301",
    )

    assert result.eligible is False
    assert result.reason_code == REASON_SUSPENDED


def test_strict_historical_replay_is_limited(data_settings: Settings) -> None:
    version_id = _seed(data_settings)

    result = _check(
        data_settings,
        purpose="strict_historical_replay",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
    )

    assert result.eligible is False
    assert result.reason_code in {
        REASON_INSUFFICIENT_HISTORY,
        REASON_HISTORICAL_AVAILABILITY_UNPROVEN,
    }
    assert any("历史" in reason or "历史长度" in reason for reason in result.reasons)


def test_unknown_purpose_is_rejected(data_settings: Settings) -> None:
    _seed(data_settings)

    result = _check(data_settings, purpose="unknown-purpose", as_of="20240628")

    assert result.eligible is False
    assert result.reason_code == "unknown_purpose"


def test_missing_version_is_reported(data_settings: Settings) -> None:
    _seed(data_settings)

    result = _check(
        data_settings,
        purpose="signal_input",
        as_of="20240628",
        data_version_id="dv_does_not_exist",
    )

    assert result.eligible is False
    assert result.reason_code == "version_not_found"


def test_result_contains_structured_evidence(data_settings: Settings) -> None:
    version_id = _seed(data_settings)

    result = _check(
        data_settings,
        purpose="display",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
    )

    payload = result.as_dict()
    assert payload["purpose"] == "display"
    assert payload["rule_version"]
    assert payload["quality"]["by_severity"]["high"] >= 0
    assert payload["source_limits"]
    assert payload["latest_data_date"]
    assert payload["checked_at"]


def _record_issue(
    settings: Settings,
    *,
    rule_code: str,
    severity: str,
    scope: dict,
    data_version_id: str | None = None,
) -> None:
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO quality_issue(rule_code, severity, scope, rule_version,"
            " data_version_id, detected_at) VALUES (?, ?, ?, 'qr-test', ?,"
            " '2024-06-30T00:00:00+00:00')",
            (rule_code, severity, json.dumps(scope, ensure_ascii=False, sort_keys=True), data_version_id),
        )
        conn.commit()


def _past_dates(count: int, *, end: str) -> list[str]:
    """以 ``end``（含）为边界向前取 ``count`` 个自然日，保证边界日在内。"""
    anchor = date(int(end[:4]), int(end[4:6]), int(end[6:8]))
    return [(anchor - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(count)]


def _future_dates(count: int, *, start: str) -> list[str]:
    anchor = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    return [(anchor - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(count)]


def _insert_future_padded_history(settings: Settings, ts_code: str, *, as_of: str) -> tuple[int, int]:
    """插入 pad 用数据：as_of 前 65 行 + as_of 后 65 行。

    修复前：总长度 130 ≥ 120，未来行数会补足历史长度；
    修复后：按 ``as_of`` 截断只有 65 行 < 120，历史不足被拒绝。
    """
    past = _past_dates(65, end=as_of)
    future = _future_dates(65, start="20990628")
    stamps = "2024-06-28T00:00:00+00:00"
    with sqlite3.connect(settings.db_path) as conn:
        conn.executemany(
            "INSERT INTO daily_price(ts_code, trade_date, close, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES (?, ?, 10.0, 'tushare-fixture', 1,"
            " 'h', ?, ?)",
            [(ts_code, trade_date, stamps, stamps) for trade_date in past + future],
        )
        conn.executemany(
            "INSERT INTO adj_factor(ts_code, trade_date, adj_factor, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES (?, ?, 1.0, 'tushare-fixture', 1,"
            " 'f', ?, ?)",
            [(ts_code, trade_date, stamps, stamps) for trade_date in past],
        )
        conn.commit()
    return len(past), len(past) + len(future)


def test_execution_reference_rejects_recorded_issue_for_same_security_and_date(
    data_settings: Settings,
) -> None:
    version_id = _seed(data_settings)
    _record_issue(
        data_settings,
        rule_code="Q005_invalid_ohlc_relation",
        severity="high",
        scope={"ts_code": "000001.SZ", "trade_date": "20240628"},
        data_version_id=version_id,
    )

    blocked = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
        business_date="20240628",
    )
    same_code_other_date = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
        business_date="20240102",
    )
    other_code_same_date = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["600000.SH"],
        business_date="20240628",
    )

    assert blocked.eligible is False
    assert blocked.reason_code == REASON_QUALITY_BLOCKED
    assert any(
        "000001.SZ" in reason and "20240628" in reason and "Q005_invalid_ohlc_relation" in reason
        for reason in blocked.reasons
    )
    # 范围精确：同证券其他日期、其他证券同日期都不被误伤。
    assert same_code_other_date.eligible is True
    assert same_code_other_date.reason_code == REASON_ELIGIBLE
    assert other_code_same_date.eligible is True


def test_execution_reference_matches_aggregated_trade_dates_only(
    data_settings: Settings,
) -> None:
    version_id = _seed(data_settings)
    _record_issue(
        data_settings,
        rule_code="Q005_invalid_ohlc_relation",
        severity="high",
        scope={"ts_code": "600000.SH", "affected_count": 3, "trade_dates": ["20240628"]},
        data_version_id=version_id,
    )

    on_date = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["600000.SH"],
        business_date="20240628",
    )
    other_date = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["600000.SH"],
        business_date="20240102",
    )

    assert on_date.eligible is False
    assert on_date.reason_code == REASON_QUALITY_BLOCKED
    assert other_date.eligible is True


def test_execution_reference_blocks_full_trade_date_set_beyond_200(
    data_settings: Settings,
) -> None:
    _seed(data_settings)
    # 质量汇总保存完整日期集合（250 个），不能因旧版 200 上限遗漏最早的日期。
    # 用无版本路径验证：阻断匹配只依赖 scope 日期集合本身，
    # 不受版本分块成员（2019 日期不是版本成员）日历影响。
    anchor = date(2019, 1, 1)
    dates = [(anchor + timedelta(days=offset)).strftime("%Y%m%d") for offset in range(250)]
    # fixture 日历只覆盖 2022-12-26 起的区间；为这些日期补交易日历证据，
    # 否则会在质量阻断之前先以 calendar_unknown 被拒。
    with sqlite3.connect(data_settings.db_path) as conn:
        conn.executemany(
            "INSERT INTO trade_calendar(exchange, cal_date, is_open, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES ('SSE', ?, 1, 'tushare-fixture', 1,"
            " 'c', '2024-06-30T00:00:00+00:00', '2024-06-30T00:00:00+00:00')",
            [(trade_date,) for trade_date in dates],
        )
        conn.commit()
    _record_issue(
        data_settings,
        rule_code="Q018_invalid_price_format",
        severity="high",
        scope={"ts_code": "600000.SH", "affected_count": 250, "trade_dates": dates},
    )

    earliest = _check(
        data_settings,
        purpose="execution_reference",
        as_of=dates[-1],
        ts_codes=["600000.SH"],
        business_date=dates[-1],
    )
    latest = _check(
        data_settings,
        purpose="execution_reference",
        as_of=dates[0],
        ts_codes=["600000.SH"],
        business_date=dates[0],
    )
    # 日期序列之外的某一天（不在集合中）不被误伤。
    not_affected = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240628",
        ts_codes=["600000.SH"],
        business_date="20240628",
    )

    assert len(dates) > 200
    assert len(set(dates)) == 250
    assert earliest.eligible is False
    assert earliest.reason_code == REASON_QUALITY_BLOCKED
    assert latest.eligible is False
    assert latest.reason_code == REASON_QUALITY_BLOCKED
    assert not_affected.eligible is True


def test_execution_reference_ignores_issue_without_date_evidence(
    data_settings: Settings,
) -> None:
    version_id = _seed(data_settings)
    # 记录级汇总问题没有日期证据：不能确定它覆盖本日期，不得用作阻断依据。
    _record_issue(
        data_settings,
        rule_code="Q003_duplicate_key_conflict",
        severity="high",
        scope={"category": "daily", "logical_key": "tushare-fixture|daily|000001.SZ|20240628"},
        data_version_id=version_id,
    )

    result = _check(
        data_settings,
        purpose="execution_reference",
        as_of="20240628",
        data_version_id=version_id,
        ts_codes=["000001.SZ"],
        business_date="20240628",
    )

    assert result.eligible is True
    assert result.reason_code == REASON_ELIGIBLE


def test_signal_history_cannot_be_padded_by_future_rows_without_version(
    data_settings: Settings,
) -> None:
    _seed(data_settings)
    past_count, total_count = _insert_future_padded_history(data_settings, "800001.SZ", as_of="20240628")

    result = _check(
        data_settings,
        purpose="signal_input",
        as_of="20240628",
        ts_codes=["800001.SZ"],
    )

    assert total_count >= 120, "测试数据必须包含被补足到阈值以上的总行数"
    assert result.eligible is False
    assert result.reason_code == REASON_INSUFFICIENT_HISTORY
    assert result.details[0]["history_trade_days"] == past_count


def test_signal_history_cannot_be_padded_by_future_rows_in_chunked_version(
    data_settings: Settings,
) -> None:
    from app.data.records import ALL_CATEGORIES

    _seed(data_settings)
    past_count, _total = _insert_future_padded_history(data_settings, "800001.SZ", as_of="20240628")
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        version = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20990101", "end": "20990628"},
            categories=list(ALL_CATEGORIES),
            rule_version="qr-test",
            quality_summary={"total": 0},
        )
        view = VersionView(conn, version["version_id"])
        # 边界日计入历史长度；截断前的全量长度包含未来行。
        assert view.history_length("800001.SZ") == 130
        assert view.history_length("800001.SZ", on_or_before="20240628") == past_count
        result = check_eligibility(
            conn,
            purpose="signal_input",
            as_of="20240628",
            source_code="tushare-fixture",
            data_version_id=version["version_id"],
            ts_codes=["800001.SZ"],
        )
    finally:
        conn.close()

    assert result.eligible is False
    assert result.reason_code == REASON_INSUFFICIENT_HISTORY
    assert result.details[0]["history_trade_days"] == past_count


def test_signal_history_cannot_be_padded_by_future_rows_in_legacy_version(
    data_settings: Settings,
) -> None:
    _seed(data_settings)
    past_count, _total = _insert_future_padded_history(data_settings, "800001.SZ", as_of="20240628")
    legacy_id = "dv_legacy_history_pad"
    with sqlite3.connect(data_settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, trade_date FROM daily_price WHERE ts_code='800001.SZ'"
            " AND source_code='tushare-fixture'"
        ).fetchall()
        factor_rows = conn.execute(
            "SELECT id, trade_date FROM adj_factor WHERE ts_code='800001.SZ'"
            " AND source_code='tushare-fixture'"
        ).fetchall()
        total_members = len(rows) + len(factor_rows)
        conn.execute(
            "INSERT INTO data_version(version_id, source_code, scope, categories, quality_summary,"
            " manifest_sha256, rule_version, status, created_at, published_at, manifest_version,"
            " member_storage, member_count, chunk_count, content_digest, retention_state,"
            " window_mode, window_start, archive_id)"
            " VALUES (?, 'tushare-fixture', '{}', '[\"adj_factor\",\"daily\"]', '{}', 'm', 'qr-test',"
            " 'published', '2024-06-30T00:00:00+00:00', '2024-06-30T00:00:00+00:00', 1, 'legacy',"
            " ?, 0, NULL, 'active', 'full', NULL, NULL)",
            (legacy_id, total_members),
        )
        conn.executemany(
            "INSERT INTO data_version_member(version_id, category, record_id, content_hash,"
            " logical_key) VALUES (?, ?, ?, ?, ?)",
            [
                (legacy_id, "daily", row_id, "h", f"tushare-fixture|daily|800001.SZ|{trade_date}")
                for row_id, trade_date in rows
            ]
            + [
                (
                    legacy_id,
                    "adj_factor",
                    row_id,
                    "f",
                    f"tushare-fixture|adj_factor|800001.SZ|{trade_date}",
                )
                for row_id, trade_date in factor_rows
            ],
        )
        conn.commit()
        view = VersionView(conn, legacy_id)
        assert view.history_length("800001.SZ") == 130
        assert view.history_length("800001.SZ", on_or_before="20240628") == past_count
        result = check_eligibility(
            conn,
            purpose="signal_input",
            as_of="20240628",
            source_code="tushare-fixture",
            data_version_id=legacy_id,
            ts_codes=["800001.SZ"],
        )

    assert result.eligible is False
    assert result.reason_code == REASON_INSUFFICIENT_HISTORY
    assert result.details[0]["history_trade_days"] == past_count
