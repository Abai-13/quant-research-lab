"""模拟下游调用者：坏数据必须阻止受影响的新计划输入。

阶段 2 不实现策略、账户或下单，这里用最小的“下游调用者”模拟未来阶段对数据资格的依赖：
只有资格通过时，新计划输入才会被接受。
"""

from __future__ import annotations

import sqlite3

from app.config import Settings
from app.data.eligibility import check_eligibility
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.data.prices import price_for_purpose


class SimulatedDownstreamCaller:
    """阶段 3 的最小替身：只消费“数据是否够用”的结论。"""

    def __init__(self, conn: sqlite3.Connection, source_code: str) -> None:
        self._conn = conn
        self._source_code = source_code

    def request_new_plan_input(
        self,
        *,
        purpose: str,
        as_of: str,
        data_version_id: str,
        ts_codes: list[str],
        business_date: str | None = None,
    ) -> dict:
        eligibility = check_eligibility(
            self._conn,
            purpose=purpose,
            as_of=as_of,
            source_code=self._source_code,
            data_version_id=data_version_id,
            ts_codes=ts_codes,
            business_date=business_date,
        )
        if not eligibility.eligible:
            return {
                "accepted": False,
                "reason_code": eligibility.reason_code,
                "reasons": eligibility.reasons,
            }
        prices = []
        for code in ts_codes:
            price = price_for_purpose(
                self._conn,
                purpose=purpose,
                source_code=self._source_code,
                ts_code=code,
                as_of=as_of,
                business_date=business_date,
            )
            prices.append(
                {"ts_code": code, "price": price.price, "price_date": price.price_date}
            )
        return {"accepted": True, "reason_code": "eligible", "prices": prices}


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


def test_new_plan_input_is_accepted_only_when_eligible(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        caller = SimulatedDownstreamCaller(conn, "tushare-fixture")
        accepted = caller.request_new_plan_input(
            purpose="signal_input",
            as_of="20240628",
            data_version_id=version_id,
            ts_codes=["000001.SZ", "600000.SH"],
        )
    finally:
        conn.close()

    assert accepted["accepted"] is True
    assert accepted["reason_code"] == "eligible"
    assert len(accepted["prices"]) == 2


def test_new_plan_input_is_rejected_when_history_is_short(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    with sqlite3.connect(data_settings.db_path) as setup:
        setup.executemany(
            "INSERT INTO daily_price(ts_code, trade_date, close, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES ('800001.SZ', ?, 10.0,"
            " 'tushare-fixture', 1, 'h', '2024-06-28T00:00:00+00:00', '2024-06-28T00:00:00+00:00')",
            [(value,) for value in ("20240628", "20240627", "20240626")],
        )
        setup.executemany(
            "INSERT INTO adj_factor(ts_code, trade_date, adj_factor, source_code, revision_no,"
            " content_hash, observed_at, ingested_at) VALUES ('800001.SZ', ?, 1.0,"
            " 'tushare-fixture', 1, 'f', '2024-06-28T00:00:00+00:00', '2024-06-28T00:00:00+00:00')",
            [(value,) for value in ("20240628", "20240627", "20240626")],
        )
        setup.commit()

    # 新插入的数据必须先发布为新版本，固定版本查询不会读到未发布的行。
    from app.data.chunks import publish_chunked_version
    from app.data.records import ALL_CATEGORIES

    with sqlite3.connect(data_settings.db_path) as setup:
        v2 = publish_chunked_version(
            setup,
            source_code="tushare-fixture",
            scope={"start": "20240626", "end": "20240628"},
            categories=list(ALL_CATEGORIES),
            rule_version="qr-test",
            quality_summary={"total": 0},
        )

    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        caller = SimulatedDownstreamCaller(conn, "tushare-fixture")
        old = caller.request_new_plan_input(
            purpose="signal_input",
            as_of="20240628",
            data_version_id=version_id,
            ts_codes=["800001.SZ"],
        )
        rejected = caller.request_new_plan_input(
            purpose="signal_input",
            as_of="20240628",
            data_version_id=v2["version_id"],
            ts_codes=["800001.SZ"],
        )
    finally:
        conn.close()

    assert old["accepted"] is False
    assert old["reason_code"] == "missing_price"
    assert rejected["accepted"] is False
    assert rejected["reason_code"] == "insufficient_history"
    assert "prices" not in rejected


def test_execution_input_is_rejected_on_suspended_day(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        caller = SimulatedDownstreamCaller(conn, "tushare-fixture")
        rejected = caller.request_new_plan_input(
            purpose="execution_reference",
            as_of="20240301",
            data_version_id=version_id,
            ts_codes=["000001.SZ"],
            business_date="20240301",
        )
    finally:
        conn.close()

    assert rejected["accepted"] is False
    assert rejected["reason_code"] == "suspended"


def test_stale_valuation_is_labelled_not_blocked(data_settings: Settings) -> None:
    version_id = _seed(data_settings)
    conn = sqlite3.connect(data_settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        caller = SimulatedDownstreamCaller(conn, "tushare-fixture")
        accepted = caller.request_new_plan_input(
            purpose="valuation_reference",
            as_of="20240628",
            data_version_id=version_id,
            ts_codes=["000001.SZ"],
            business_date="20240106",
        )
    finally:
        conn.close()

    assert accepted["accepted"] is True
    assert accepted["prices"][0]["price_date"] == "20240105"
