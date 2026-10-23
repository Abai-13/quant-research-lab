"""固定版本读取：V1 发布 → V2 修订/新增 → V1 仍读回 V1 记录。"""

from __future__ import annotations

import sqlite3

from app.config import Settings
from app.data.chunks import VersionView, publish_chunked_version
from app.data.eligibility import check_eligibility
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.records import CATEGORY_DAILY, logical_key
from app.data.store import write_records


def _seed(settings: Settings) -> str:
    initialize_database(settings)
    result = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily", "adj_factor"),
            start="20240102",
            end="20240105",
            codes=("000001.SZ", "600000.SH"),
        ),
    )
    assert result.status == "completed"
    return result.version["version_id"]


def _record(close: float) -> dict:
    return {
        "ts_code": "000001.SZ",
        "trade_date": "20240103",
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


def test_revision_after_v1_does_not_change_v1_reads(data_settings: Settings) -> None:
    v1 = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        view1 = VersionView(conn, v1)
        old_row = view1.daily_on("000001.SZ", "20240103")
        assert old_row is not None
        old_close = float(old_row["close"])
        old_history = view1.history_length("000001.SZ")
        old_latest = view1.latest_data_date(["000001.SZ"])

        outcome = write_records(
            conn, CATEGORY_DAILY, [_record(old_close + 5.0)], source_code="tushare-fixture"
        )
        assert outcome.revised == 1
        v2 = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20240102", "end": "20240105"},
            categories=["daily", "calendar", "security", "adj_factor"],
            rule_version="qr-test",
            quality_summary={"total": 0},
            base_version_id=v1,
            affected_partitions={
                "daily": sorted(
                    {partition for category, partition in outcome.affected_partitions if category == "daily"}
                )
            },
        )
        assert v2["version_id"] != v1
        view2 = VersionView(conn, v2["version_id"])
        new_row = view2.daily_on("000001.SZ", "20240103")
        assert float(new_row["close"]) == old_close + 5.0

        # V1 视图与资格重新读取仍返回旧值。
        assert float(view1.daily_on("000001.SZ", "20240103")["close"]) == old_close
        assert view1.history_length("000001.SZ") == old_history
        assert view1.latest_data_date(["000001.SZ"]) == old_latest

        result1 = check_eligibility(
            conn,
            purpose="display",
            as_of="20240103",
            source_code="tushare-fixture",
            data_version_id=v1,
            ts_codes=["000001.SZ"],
        )
        result2 = check_eligibility(
            conn,
            purpose="display",
            as_of="20240103",
            source_code="tushare-fixture",
            data_version_id=v2["version_id"],
            ts_codes=["000001.SZ"],
        )
        assert result1.details[0]["price"] != result2.details[0]["price"]
        assert result1.details[0]["price"] != result2.details[0]["price"]
        # 历史长度按请求的 as_of（含边界日）截断：资格明细不能由 as_of 之后的行补足。
        assert result1.details[0]["history_trade_days"] == 250
        assert result1.details[0]["history_trade_days"] <= old_history
    finally:
        conn.close()


def test_same_content_different_order_is_idempotent(data_settings: Settings) -> None:
    initialize_database(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    try:
        members = [
            (
                index,
                f"tushare-fixture|daily|{index:06d}.SZ|20240102",
                f"h{index}",
            )
            for index in range(1, 21)
        ]
        first = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20240102", "end": "20240102"},
            categories=["daily"],
            rule_version="qr-test",
            quality_summary={},
            members={"daily": members},
        )
        second = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={"start": "20240102", "end": "20240102"},
            categories=["daily"],
            rule_version="qr-test",
            quality_summary={},
            members={"daily": list(reversed(members))},
        )
        assert first["version_id"] == second["version_id"]
        assert second["reused"] is True
        assert conn.execute("SELECT COUNT(*) FROM data_version").fetchone()[0] == 1
        # 空版本重复发布也是合法的幂等操作。
        empty1 = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={},
            categories=["daily"],
            rule_version="qr-test",
            quality_summary={},
            members={"daily": []},
        )
        empty2 = publish_chunked_version(
            conn,
            source_code="tushare-fixture",
            scope={},
            categories=["daily"],
            rule_version="qr-test",
            quality_summary={},
            members={"daily": []},
        )
        assert empty1["version_id"] == empty2["version_id"]
        assert empty2["reused"] is True
    finally:
        conn.close()
