"""阶段三 S5b：公司行为、NAV/收益（临时库聚焦测试）。"""

from __future__ import annotations

import sqlite3

import pytest

from app.data.errors import DataError
from app.data.migration import initialize_database
from app.portfolio.corporate_actions import (
    list_corporate_actions,
    receivable_summary,
    record_corporate_action,
    settle_corporate_action,
)
from app.portfolio.db import portfolio_connect
from app.portfolio.ledger import (
    AccountEventRequest,
    get_account_state,
    post_account_event,
)
from app.portfolio.performance import (
    compute_performance_point,
    performance_summary,
    value_owner,
)
from app.portfolio.rule_bundles import create_rule_bundle
from app.portfolio.simulation import (
    create_simulation_segment,
    get_simulation_account_state,
)
from tests.test_phase3_simulation import (
    _business_days,
    _fee_bundle,
    _market_rules_bundle,
    _opening,
    _position,
    _seed_version,
    _snapshot_lookup,
)

REC = "2025-06-05T01:00:00+00:00"


@pytest.fixture
def portfolio_conn(data_settings):
    initialize_database(data_settings)
    conn = portfolio_connect(data_settings.db_path)
    try:
        yield conn, data_settings
    finally:
        conn.close()


def _setup(
    conn,
    *,
    closes: list[float] | None = None,
    cash_cents: int = 1_000_000,
    positions: list[dict] | None = None,
    account_id: str = "acct_s5b",
) -> dict:
    closes = closes or [10.0] * 6
    days = _business_days(len(closes))
    version_id = _seed_version(conn, days=days, plans={"000001.SZ": closes})
    state = _opening(conn, cash_cents=cash_cents, positions=positions, account_id=account_id)
    return {
        "days": days,
        "version_id": version_id,
        "account_id": account_id,
        "snapshot_id": _snapshot_lookup(conn, account_id),
        "state": state,
    }


def test_actual_cash_dividend_receivable_then_settle(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _setup(
        conn,
        positions=[_position("000001.SZ", 100, sellable=100, cost=100_000)],
    )
    record = record_corporate_action(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        ts_code="000001.SZ",
        action_kind="cash_dividend",
        record_date="20240909",
        ex_date="20240910",
        pay_date="20240911",
        entitlement_shares=100,
        cash_receivable_cents=800,
        tax_status="confirmed",
        idempotency_key="div-1",
        data_version_id=env["version_id"],
    )
    assert record["status"] == "pending" and record["reused"] is False
    same = record_corporate_action(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        ts_code="000001.SZ",
        action_kind="cash_dividend",
        record_date="20240909",
        ex_date="20240910",
        pay_date="20240911",
        entitlement_shares=100,
        cash_receivable_cents=800,
        tax_status="confirmed",
        idempotency_key="div-1",
        data_version_id=env["version_id"],
    )
    assert same["reused"] is True and same["application_id"] == record["application_id"]

    receivables = receivable_summary(conn, env["account_id"], "20240910")
    assert receivables["confirmed_cents"] == 800
    valuation = value_owner(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        source_code="tushare-fixture",
        data_version_id=env["version_id"],
        as_of="20240910",
    )
    assert valuation["completeness"] == "complete"
    assert valuation["receivable_cents"] == 800
    assert valuation["nav_cents"] == 1_000_000 + 100 * 1000 + 800

    settled = settle_corporate_action(conn, record["application_id"], as_of="20240911")
    assert settled["cash_delta_cents"] == 800
    state = get_account_state(conn, env["account_id"])
    assert state["cash_cents"] == 1_000_800
    assert receivable_summary(conn, env["account_id"], "20240911")["confirmed_cents"] == 0
    again = settle_corporate_action(conn, record["application_id"], as_of="20240911")
    assert again["reused"] is True
    assert list_corporate_actions(conn, env["account_id"])[0]["status"] == "settled"


def test_actual_stock_dividend_and_unsupported_cases(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _setup(
        conn,
        positions=[_position("000001.SZ", 100, sellable=100, cost=100_000)],
    )
    record = record_corporate_action(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        ts_code="000001.SZ",
        action_kind="stock_dividend",
        record_date="20240909",
        list_date="20240911",
        entitlement_shares=50,
        tax_status="unknown",
        idempotency_key="bonus-1",
        data_version_id=env["version_id"],
    )
    settle_corporate_action(conn, record["application_id"], as_of="20240911")
    state = get_account_state(conn, env["account_id"])
    assert state["positions"][0]["shares"] == 150
    assert state["positions"][0]["total_cost_cents"] == 100_000

    unsupported = record_corporate_action(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        ts_code="000001.SZ",
        action_kind="other",
        record_date="20240909",
        entitlement_shares=100,
        idempotency_key="other-1",
        data_version_id=env["version_id"],
    )
    assert unsupported["status"] == "blocked_insufficient_evidence"
    with pytest.raises(DataError) as exc:
        settle_corporate_action(conn, unsupported["application_id"], as_of="20240912")
    assert exc.value.reason == "ca_blocked_insufficient_evidence"

    unconfirmed = record_corporate_action(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        ts_code="000001.SZ",
        action_kind="cash_dividend",
        record_date="20240909",
        ex_date="20240910",
        pay_date="20240911",
        entitlement_shares=150,
        cash_receivable_cents=1200,
        tax_status="estimated",
        idempotency_key="div-est-1",
        data_version_id=env["version_id"],
    )
    with pytest.raises(DataError) as exc:
        settle_corporate_action(conn, unconfirmed["application_id"], as_of="20240911")
    assert exc.value.reason == "ca_tax_status_not_confirmed"
    recv = receivable_summary(conn, env["account_id"], "20240910")
    assert recv["confirmed_cents"] == 0 and recv["unconfirmed_count"] == 1
    partial = value_owner(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        source_code="tushare-fixture",
        data_version_id=env["version_id"],
        as_of="20240910",
    )
    assert partial["completeness"] == "partial_unknown"
    assert partial["restriction_code"] == "receivable_tax_unconfirmed"
    assert partial["nav_cents"] is None


def test_simulation_corporate_action_is_isolated(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    days = _business_days(6)
    version_id = _seed_version(conn, days=days, plans={"000001.SZ": [10.0] * 6})
    _opening(
        conn,
        cash_cents=1_000_000,
        positions=[_position("000001.SZ", 100, sellable=100, cost=100_000)],
        account_id="acct_sim_ca",
    )
    snapshot_id = _snapshot_lookup(conn, "acct_sim_ca")
    rule_bundle_id = _market_rules_bundle(conn)
    fee_bundle_id = _fee_bundle(conn)
    segment = create_simulation_segment(
        conn,
        source_account_id="acct_sim_ca",
        base_account_snapshot_id=snapshot_id,
        rule_bundle_id=rule_bundle_id,
        fee_bundle_id=fee_bundle_id,
        source_code="tushare-fixture",
        data_version_id=version_id,
        as_of=days[2],
    )
    actual_before = get_account_state(conn, "acct_sim_ca")
    record = record_corporate_action(
        conn,
        owner_type="simulation_segment",
        owner_id=segment["segment_id"],
        ts_code="000001.SZ",
        action_kind="stock_dividend",
        record_date=days[2],
        list_date=days[3],
        entitlement_shares=50,
        idempotency_key="sim-bonus-1",
        data_version_id=version_id,
    )
    settle_corporate_action(conn, record["application_id"], as_of=days[3])
    sim_state = get_simulation_account_state(conn, segment["segment_id"])
    assert sim_state["positions"][0]["shares"] == 150
    assert sim_state["cash_cents"] == 1_000_000
    assert get_account_state(conn, "acct_sim_ca") == actual_before

    cash = record_corporate_action(
        conn,
        owner_type="simulation_segment",
        owner_id=segment["segment_id"],
        ts_code="000001.SZ",
        action_kind="cash_dividend",
        record_date=days[2],
        ex_date=days[3],
        pay_date=days[4],
        entitlement_shares=150,
        cash_receivable_cents=900,
        tax_status="confirmed",
        idempotency_key="sim-div-1",
        data_version_id=version_id,
    )
    assert receivable_summary(conn, segment["simulation_account_id"], days[3])["confirmed_cents"] == 900
    settle_corporate_action(conn, cash["application_id"], as_of=days[4])
    assert get_simulation_account_state(conn, segment["segment_id"])["cash_cents"] == 1_000_900
    assert get_account_state(conn, "acct_sim_ca") == actual_before


def test_nav_and_performance_with_external_cash_flow(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _setup(
        conn,
        closes=[10.0, 10.0, 11.0, 11.0, 11.0, 11.0],
        cash_cents=1_000_000,
        positions=[_position("000001.SZ", 100, sellable=100, cost=100_000)],
    )
    day0, day1, day2 = env["days"][0], env["days"][1], env["days"][2]
    first = value_owner(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        source_code="tushare-fixture",
        data_version_id=env["version_id"],
        as_of=day0,
    )
    assert first["nav_cents"] == 1_000_000 + 100 * 1000
    point0 = compute_performance_point(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        source_code="tushare-fixture",
        data_version_id=env["version_id"],
        as_of=day0,
    )
    assert point0["period_return_bp"] is None  # 没有上一估值，不伪造收益

    # 第二日外部入金 50,000，价格不变：收益应为 0。
    request = AccountEventRequest(
        account_id=env["account_id"],
        event_type="cash_in",
        business_date=day1,
        idempotency_key="flow-1",
        payload={"amount_cents": 50_000},
        recorded_at=REC,
        reason="外部入金",
    )
    post_account_event(conn, request, expected_account_revision=1)
    point1 = compute_performance_point(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        source_code="tushare-fixture",
        data_version_id=env["version_id"],
        as_of=day1,
    )
    assert point1["external_cash_flow_cents"] == 50_000
    assert point1["period_return_bp"] == 0
    assert point1["valuation"]["nav_cents"] == 1_150_000

    # 第三日价格 11：NAV 1,160,000，分母 1,150,000 → +870bp。
    point2 = compute_performance_point(
        conn,
        owner_type="account",
        owner_id=env["account_id"],
        source_code="tushare-fixture",
        data_version_id=env["version_id"],
        as_of=day2,
    )
    assert point2["valuation"]["nav_cents"] == 1_050_000 + 110_000
    assert point2["period_return_bp"] == 87
    summary = performance_summary(conn, "account", env["account_id"])
    assert summary["point_count"] == 3
    assert summary["incomplete_periods"] is True  # 第一天没有可比上一期
    assert summary["fees_total_cents"] == 0


def test_missing_valuation_marks_partial_unknown(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    days = _business_days(6)
    version_id = _seed_version(
        conn, days=days, plans={"000001.SZ": [10.0] * 6, "000006.SZ": [8.0] * 2}
    )
    _opening(
        conn,
        cash_cents=1_000_000,
        positions=[_position("600999.SH", 100, sellable=100, cost=80_000)],
        account_id="acct_missing_val",
    )
    valuation = value_owner(
        conn,
        owner_type="account",
        owner_id="acct_missing_val",
        source_code="tushare-fixture",
        data_version_id=version_id,
        as_of=days[4],
    )
    assert valuation["completeness"] == "partial_unknown"
    assert valuation["restriction_code"] == "valuation_price_missing"
    assert valuation["holdings_value_cents"] is None and valuation["nav_cents"] is None
    point = compute_performance_point(
        conn,
        owner_type="account",
        owner_id="acct_missing_val",
        source_code="tushare-fixture",
        data_version_id=version_id,
        as_of=days[4],
    )
    assert point["period_return_bp"] is None
