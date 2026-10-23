"""阶段三 S5a：模拟交易段与订单/成交生命周期聚焦测试（方案 A，模拟域隔离）。

全部使用隔离临时库（``data_settings``/``portfolio_conn``，schema 4，外键开启），
通过阶段二写入/发布路径与阶段三 S2 账本构造数据；全部数值手算核对。
模拟账户状态只从 ``get_simulation_account_state`` 读取；实际账户仍走 S2 账本，
用于断言实际/模拟隔离。不访问真实数据源、不运行应用、不改阶段二/四代码。
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from app.data.chunks import publish_chunked_version
from app.data.errors import DataError
from app.data.migration import initialize_database
from app.data.store import write_records
from app.portfolio import simulation as simulation_module
from app.portfolio.db import portfolio_connect
from app.portfolio.ledger import (
    AccountEventRequest,
    create_account,
    get_account_state,
    post_account_event,
)
from app.portfolio.recommendation import ensure_analysis_artifact_table  # noqa: F401  (回归确证不触碰)
from app.portfolio.rule_bundles import create_rule_bundle
from app.portfolio.simulation import (
    SIMULATION_APPLICATION_VERSION,
    advance_simulation_segment,
    cancel_simulation_order,
    close_simulation_segment,
    create_simulation_segment,
    get_simulation_account_state,
    get_simulation_segment,
    list_simulation_events,
    list_simulation_segments,
    place_simulation_order,
)

REC_1 = "2025-06-05T01:00:00+00:00"
REC_2 = "2025-06-05T02:00:00+00:00"
SRC = "tushare-fixture"


# ------------------------------------------------------------------ 夹具


@pytest.fixture
def portfolio_conn(data_settings):
    initialize_database(data_settings)
    conn = portfolio_connect(data_settings.db_path)
    try:
        yield conn, data_settings
    finally:
        conn.close()


def _business_days(count: int) -> list[str]:
    days: list[str] = []
    cursor = dt.date(2024, 9, 9)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y%m%d"))
        cursor += dt.timedelta(days=1)
    return days


def _seed_version(
    conn: sqlite3.Connection,
    *,
    days: list[str],
    plans: dict[str, list],
    source_code: str = SRC,
    include_suspension: bool = True,
) -> str:
    daily: list[dict[str, object]] = []
    factors: list[dict[str, object]] = []
    for code, closes in plans.items():
        assert closes and len(closes) <= len(days), f"{code} 序列超出天序列"
        for day, close in zip(days, closes):
            daily.append(
                {
                    "ts_code": code,
                    "trade_date": day,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "pre_close": None,
                    "volume": 1000.0,
                    "amount": 10000.0,
                    "currency": "CNY",
                    "source_volume_raw": 1000.0,
                    "source_amount_raw": 10000.0,
                }
            )
            factors.append({"ts_code": code, "trade_date": day, "adj_factor": 1.0})
    calendar = [
        {"ts_code": "", "exchange": "SSE", "cal_date": day, "is_open": 1} for day in days
    ]
    calendar.append({"ts_code": "", "exchange": "SSE", "cal_date": "20250308", "is_open": 0})
    # 显式停牌数据集：类别存在本身是非停牌判定的证据；当天无 S 行视为未停牌。
    suspension = [
        {
            "ts_code": "000001.SZ",
            "trade_date": days[0],
            "event_type": "R",
            "detail": None,
        }
    ]
    securities = [
        {
            "ts_code": code,
            "symbol": code[:6],
            "name": code,
            "market": "主板",
            "exchange": "SSE",
            "list_date": "20000101",
            "delist_date": None,
            "list_status": "L",
        }
        for code in sorted(plans)
    ]
    write_records(conn, "daily", daily, source_code=source_code)
    write_records(conn, "adj_factor", factors, source_code=source_code)
    write_records(conn, "calendar", calendar, source_code=source_code)
    write_records(conn, "security", securities, source_code=source_code)
    categories = ["daily", "adj_factor", "calendar", "security"]
    if include_suspension:
        write_records(conn, "suspension", suspension, source_code=source_code)
        categories.append("suspension")
    return str(
        publish_chunked_version(
            conn,
            source_code=source_code,
            scope={},
            categories=categories,
            rule_version="test-rules",
            quality_summary={},
            window_mode="full",
        )["version_id"]
    )


def _market_rules_bundle(conn: sqlite3.Connection, *, version: str = "s5-test-rules-1", rules: dict | None = None) -> str:
    content = {"bundle_kind": "parameter"}
    if rules is not None:
        content["market_rules"] = rules
    return str(
        create_rule_bundle(
            conn,
            kind="parameter",
            version=version,
            assumption_scope="synthetic_simulation",
            content=content,
        )["bundle_id"]
    )


def _fee_bundle(conn: sqlite3.Connection, version: str = "s5-test-fee-flat500") -> str:
    return str(
        create_rule_bundle(
            conn,
            kind="fee",
            version=version,
            assumption_scope="synthetic_simulation",
            content={
                "bundle_kind": "fee",
                "currency": "CNY",
                "components": [
                    {"code": "commission", "rate_bp": 0, "minimum_cents": 500, "charging": "per_side"},
                    {"code": "stamp_duty_sell", "rate_bp": 0, "charging": "sell_only"},
                    {"code": "transfer_fee", "rate_bp": 0, "charging": "both_sides"},
                    {"code": "slippage", "rate_bp": 0, "charging": "adverse_direction_model"},
                ],
            },
        )["bundle_id"]
    )


def _incomplete_fee_bundle(conn: sqlite3.Connection, version: str = "s5-test-fee-partial") -> str:
    return str(
        create_rule_bundle(
            conn,
            kind="fee",
            version=version,
            assumption_scope="synthetic_simulation",
            content={
                "bundle_kind": "fee",
                "currency": "CNY",
                "components": [
                    {"code": "commission", "rate_bp": 0, "minimum_cents": 500, "charging": "per_side"},
                ],
            },
        )["bundle_id"]
    )


def _suspension_evidence(codes, days: list[str]) -> dict:
    """合成规则包显式声明的停牌覆盖范围（仅用于测试假设）。"""
    return {
        "verification_status": "verified",
        "coverage": "full",
        "source_category": "suspension",
        "ts_codes": list(codes),
        "start_date": days[0],
        "end_date": days[-1],
    }


def _price_limit_evidence(plans: dict[str, list], days: list[str]) -> dict:
    """按合成价格声明每个证券的显式上下限（±10%，仅测试假设）。"""
    bounds = []
    for code, closes in plans.items():
        price_cents = int(round(float(closes[0]) * 100))
        bounds.append(
            {
                "ts_code": code,
                "start_date": days[0],
                "end_date": days[-1],
                "lower_cents": int(price_cents * 0.9),
                "upper_cents": int(price_cents * 1.1),
            }
        )
    return {"verification_status": "verified", "coverage": "full", "bounds": bounds}


def _market_rules(
    *,
    plans: dict[str, list],
    days: list[str],
    t_plus_one: bool = False,
    same_day_reuse: bool = False,
    suspension_codes=None,
    include_price_limits: bool = True,
    include_suspension: bool = True,
    include_t_plus_one: bool = True,
) -> dict:
    rules: dict = {
        "verification_status": "verified",
        "board_lot_shares": 100,
        "same_day_sale_proceeds_reusable": same_day_reuse,
    }
    if include_t_plus_one:
        rules["t_plus_one_sell_restriction"] = t_plus_one
    if include_suspension:
        rules["suspension_evidence"] = _suspension_evidence(
            suspension_codes if suspension_codes is not None else list(plans), days
        )
    if include_price_limits:
        rules["price_limit_evidence"] = _price_limit_evidence(plans, days)
    return rules


def _opening(
    conn: sqlite3.Connection,
    *,
    cash_cents: int,
    positions: list[dict] | None = None,
    account_id: str = "acct_actual_a",
    business_date: str = "20240901",
) -> dict:
    create_account(conn, account_id, "S5a 源测试账户")
    request = AccountEventRequest(
        account_id=account_id,
        event_type="opening",
        business_date=business_date,
        idempotency_key=f"open-{business_date}",
        payload={"cash_cents": cash_cents, "positions": list(positions or [])},
        recorded_at=REC_1,
        reason="期初录入",
    )
    post_account_event(conn, request, expected_account_revision=0)
    # 让实际账户夹具落盘：随后模拟写命令应自行拥有并提交自己的事务。
    conn.commit()
    state = get_account_state(conn, account_id)
    state["account_id"] = account_id
    return state


def _segment(
    conn: sqlite3.Connection,
    env: dict,
    *,
    rules_version: str | None = None,
    rules: dict | None = None,
    fee_bundle_id: str | None = None,
) -> str:
    rule_bundle_id = (
        _market_rules_bundle(conn, version=rules_version, rules=rules)
        if rules_version is not None
        else env["rule_bundle_id"]
    )
    return create_simulation_segment(
        conn,
        source_account_id=env["account_id"],
        base_account_snapshot_id=env["snapshot_id"],
        rule_bundle_id=rule_bundle_id,
        fee_bundle_id=fee_bundle_id or env["fee_bundle_id"],
        source_code=SRC,
        data_version_id=env["version_id"],
        as_of=env["as_of"],
    )


def _env(
    conn: sqlite3.Connection,
    *,
    cash_cents: int = 10_000_000,
    positions: list[dict] | None = None,
    account_id: str = "acct_actual_a",
    business_date: str = "20240901",
    include_suspension: bool = True,
) -> dict:
    days = _business_days(6)
    plans = {"000001.SZ": [10.0] * 6, "000006.SZ": [8.0] * 4}
    version_id = _seed_version(
        conn, days=days, plans=plans, include_suspension=include_suspension
    )
    as_of = days[2]
    rule_bundle_id = _market_rules_bundle(
        conn,
        rules=_market_rules(plans=plans, days=days),
    )
    fee_bundle_id = _fee_bundle(conn)
    state = _opening(
        conn, cash_cents=cash_cents, positions=positions, account_id=account_id,
        business_date=business_date,
    )
    return {
        "version_id": version_id,
        "days": days,
        "as_of": as_of,
        "trade_date": days[3],
        "rule_bundle_id": rule_bundle_id,
        "fee_bundle_id": fee_bundle_id,
        "account_id": account_id,
        "state": state,
        "snapshot_id": f"asnap_{account_id}@rev1",
    }


def _position(ts_code: str, shares: int, sellable: int | None, cost: int | None = None) -> dict:
    return {
        "ts_code": ts_code,
        "shares": shares,
        "total_cost_cents": cost,
        "sellable_shares": sellable,
        "acquire_date": "20240801",
        "locked": False,
    }


def _snapshot_lookup(conn: sqlite3.Connection, account_id: str) -> str:
    row = conn.execute(
        "SELECT snapshot_id FROM account_snapshot WHERE account_id=? AND account_revision=1",
        (account_id,),
    ).fetchone()
    assert row is not None
    return str(row[0])


# ------------------------------------------------------------------ 段创建与隔离


def test_segment_copies_frozen_state_isolated_and_idempotent(portfolio_conn) -> None:
    """创建段：冻结状态复制到独立模拟账户；重复请求幂等；与实际账户隔离。"""
    conn, _settings = portfolio_conn
    env = _env(conn)
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    assert created["reused"] is False
    assert created["segment_id"].startswith("segsim_")
    sim_id = created["simulation_account_id"]
    assert sim_id.startswith("sim_")

    sim_state = get_simulation_account_state(conn, created["segment_id"])
    assert sim_state["cash_cents"] == 10_000_000
    assert sim_state["positions"] == []
    row = conn.execute(
        "SELECT kind, simulation_of_account_id FROM account WHERE account_id=?", (sim_id,)
    ).fetchone()
    assert row is not None and row[0] == "simulation" and row[1] == env["account_id"]

    # 模拟域隔离：模拟账户没有任何 S2 实际账本事件/快照/持仓行。
    assert conn.execute(
        "SELECT COUNT(*) FROM account_event WHERE account_id=?", (sim_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM account_snapshot WHERE account_id=?", (sim_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM account_position p JOIN account_snapshot s"
        " ON s.snapshot_id=p.snapshot_id WHERE s.account_id=?",
        (sim_id,),
    ).fetchone()[0] == 0
    events = list_simulation_events(conn, created["segment_id"])
    assert len(events) == 1 and events[0]["event_type"] == "opening"

    segment = conn.execute(
        "SELECT status, source_account_id, simulation_account_id FROM simulation_segment"
        " WHERE segment_id=?",
        (created["segment_id"],),
    ).fetchone()
    assert segment is not None and segment[0] == "active"
    assert segment[1] == env["account_id"] and segment[2] == sim_id

    again = _segment(conn, env)
    assert again["reused"] is True
    assert again["segment_id"] == created["segment_id"]
    assert again["simulation_account_id"] == sim_id
    segments = list_simulation_segments(conn, env["account_id"])
    assert len(segments) == 1


def test_simulation_activity_never_touches_actual_account(portfolio_conn) -> None:
    """个人/模拟隔离：成交只发生在模拟账户，实际账户状态分毫不动。"""
    conn, _settings = portfolio_conn
    env = _env(conn)
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    before = get_account_state(conn, env["account_id"])
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="iso-buy-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    after = get_account_state(conn, env["account_id"])
    assert dict(after) == dict(before)

    read = get_simulation_segment(conn, created["segment_id"])
    assert read["source_state_unchanged_check"]["cash_cents"] == before["cash_cents"]
    assert read["simulation_state"]["cash_cents"] == 9_899_500


# ------------------------------------------------------------------ 手算核心：满额成交


def test_hand_calculated_full_fill_and_duplicate_advance_not_double_pasting(
    portfolio_conn,
) -> None:
    """手算：100000 现金、10 元 ×100 股、费 5 元 → 现金 98995、市值 1000、NAV 99995。"""
    conn, _settings = portfolio_conn
    env = _env(conn)  # 现金 100_000 元 = 10_000_000 分
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    result = place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="core-buy-1",
    )
    assert result["status"] == "pending" and result["reused"] is False
    order_id = result["order_id"]

    state_before = get_simulation_account_state(conn, created["segment_id"])
    advance = advance_simulation_segment(
        conn, created["segment_id"], on_trade_date=env["trade_date"]
    )
    assert advance["processed_count"] == 1
    assert advance["processed"][0]["status"] == "filled"

    state_after = get_simulation_account_state(conn, created["segment_id"])
    # 模型价 = raw open = 10.00 元 → 1000 分；费（佣金最低 500 分）一次性。
    notional = 100 * 1000
    assert notional == 100_000
    cash_after = state_before["cash_cents"] - notional - 500
    assert cash_after == 9_899_500
    assert state_after["cash_cents"] == cash_after
    position = state_after["positions"][0]
    assert position["ts_code"] == "000001.SZ" and position["shares"] == 100
    market_value = 100 * 1000  # 全部按同一 raw open 参考价估值（页内手算口径）
    assert market_value == 100_000         # 10 元 × 100 股 = 1000 元 = 100000 分
    nav = state_after["cash_cents"] + market_value
    assert nav == 9_999_500                # NAV = 99995 元

    order_row = conn.execute(
        "SELECT status, filled_shares, remaining_shares, model_price_cents,"
        " fee_estimate_cents, reason_code FROM simulation_order WHERE order_id=?",
        (order_id,),
    ).fetchone()
    assert tuple(order_row) == ("filled", 100, 0, 1000, 500, None)

    # 重复推进：不重复记账、不新增成交行。
    fills_before = conn.execute(
        "SELECT COUNT(*) FROM simulation_fill WHERE order_id=?", (order_id,)
    ).fetchone()[0]
    assert fills_before == 1
    again = advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    assert again["processed_count"] == 0
    assert get_simulation_account_state(conn, created["segment_id"]) == state_after
    fills_after = conn.execute(
        "SELECT COUNT(*) FROM simulation_fill WHERE order_id=?", (order_id,)
    ).fetchone()[0]
    assert fills_after == 1

    # 订单幂等：同 idempotency_key 重复下单返回既有订单。
    re_place = place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="core-buy-1",
    )
    assert re_place["reused"] is True and re_place["order_id"] == order_id
    with pytest.raises(DataError) as conflict:
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="buy",
            planned_shares=200,
            trade_date=env["trade_date"],
            idempotency_key="core-buy-1",
        )
    assert conflict.value.reason == "s5_order_idempotency_conflict"


# ------------------------------------------------------------------ 时间边界


def test_future_date_boundary_and_calendar_evidence(portfolio_conn) -> None:
    """future 边界：处理日必须晚段 as_of；未到期保持 pending；非交易日 fail-closed。"""
    conn, _settings = portfolio_conn
    env = _env(conn)
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    with pytest.raises(DataError) as err_past:
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="buy",
            planned_shares=100,
            trade_date=env["as_of"],
            idempotency_key="past-1",
        )
    assert err_past.value.reason == "s5_trade_date_not_future"

    # 未来订单：在更早日期推进时不处理。
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="future-1",
    )
    early_state = get_simulation_account_state(conn, created["segment_id"])
    early = advance_simulation_segment(
        conn, created["segment_id"], on_trade_date=env["days"][2]
    )
    assert early["processed_count"] == 0
    early_row = conn.execute(
        "SELECT status FROM simulation_order WHERE idempotency_key='future-1'"
    ).fetchone()
    assert early_row is not None and early_row[0] == "pending"
    after_early = get_simulation_account_state(conn, created["segment_id"])
    # 推进时点会前进（last_advanced_date），但现金与持仓不能因未成交订单改变。
    assert after_early["cash_cents"] == early_state["cash_cents"]
    assert after_early["positions"] == early_state["positions"]

    advanced = advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    assert advanced["processed_count"] == 1
    assert advanced["processed"][0]["status"] == "filled"

    # 非交易日（日历 is_open=0，如 20250308）→ unfilled + 不动持仓。
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date="20250308",
        idempotency_key="holiday-1",
    )
    holiday = advance_simulation_segment(conn, created["segment_id"], on_trade_date="20250308")
    assert holiday["processed"][0]["status"] == "unfilled"
    assert holiday["processed"][0]["reason_code"] == "s5_not_a_trading_day"
    state = get_simulation_account_state(conn, created["segment_id"])
    assert state["cash_cents"] == 9_899_500  # 持仓与现金保持

    # 版本窗口外（无日历行，20250310）→ unfilled（日历未知）+ 不动持仓。
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date="20250310",
        idempotency_key="unknown-cal-1",
    )
    unknown = advance_simulation_segment(conn, created["segment_id"], on_trade_date="20250310")
    assert unknown["processed"][0]["status"] == "unfilled"
    assert unknown["processed"][0]["reason_code"] == "s5_calendar_unknown"
    assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == 9_899_500


# ------------------------------------------------------------------ 部分成交与资金


def test_partial_fill_buy_and_residual_cash(portfolio_conn) -> None:
    """部分成交：预算不足时按整手削减；remaining 与现金严格守恒。"""
    conn, _settings = portfolio_conn
    days = _business_days(6)
    plans = {"000005.SZ": [4.0] * 6}
    version_id = _seed_version(conn, days=days, plans=plans)
    as_of = days[2]
    trade_date = days[3]
    rule_bundle_id = _market_rules_bundle(
        conn,
        version="s5-test-rules-partial",
        rules=_market_rules(plans=plans, days=days),
    )
    fee_bundle_id = _fee_bundle(conn, version="s5-test-fee-partial-flat500")
    state = _opening(conn, cash_cents=60_000, account_id="acct_partial")
    snapshot_id = _snapshot_lookup(conn, state["account_id"])
    created = create_simulation_segment(
        conn,
        source_account_id=state["account_id"],
        base_account_snapshot_id=snapshot_id,
        rule_bundle_id=rule_bundle_id,
        fee_bundle_id=fee_bundle_id,
        source_code=SRC,
        data_version_id=version_id,
        as_of=as_of,
    )
    sim_id = created["simulation_account_id"]
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000005.SZ",
        direction="buy",
        planned_shares=200,
        trade_date=trade_date,
        idempotency_key="partial-buy-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=trade_date)
    order_row = conn.execute(
        "SELECT status, filled_shares, remaining_shares, model_price_cents,"
        " fee_estimate_cents, reason_code FROM simulation_order"
        " WHERE idempotency_key='partial-buy-1'"
    ).fetchone()
    # 手算：每手 100×400=40_000，费 500 → 一手 40_500；两手 81_000 > 60_000。
    assert tuple(order_row) == (
        "partially_filled",
        100,
        100,
        400,   # 4.00 元 = 400 分
        500,   # 费（含滑点 0）实际 payment
        "s5_insufficient_funds",
    )
    assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == 60_000 - 40_000 - 500
    fills = conn.execute(
        "SELECT filled_shares, price_cents, fee_cents FROM simulation_fill"
        " WHERE order_id=(SELECT order_id FROM simulation_order WHERE idempotency_key='partial-buy-1')"
    ).fetchall()
    assert [tuple(item) for item in fills] == [(100, 400, 500)]


def test_sell_limited_by_sellable_and_pays_fees(portfolio_conn) -> None:
    """卖出受可卖数量限制：部分成交、余量保留、卖出先于买入处理。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        cash_cents=10_000_000,
        positions=[_position("000001.SZ", 100, sellable=60, cost=100_000)],
        account_id="acct_sell_lim",
    )
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    sim_id = created["simulation_account_id"]
    assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == 10_000_000
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="sell",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="sell-lim-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    order_row = conn.execute(
        "SELECT status, filled_shares, remaining_shares, fee_estimate_cents, reason_code"
        " FROM simulation_order WHERE idempotency_key='sell-lim-1'"
    ).fetchone()
    # 手算：可卖 60 < 计划 100 → 成交 60 股 ×1000 分 = 60_000，费 500；
    assert tuple(order_row) == (
        "partially_filled",
        60,
        40,
        500,
        "s5_limit_reduced_by_sellable_shares",
    )
    assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == 10_000_000 + 60*1000 - 500


def test_failed_sell_never_funds_later_buy(portfolio_conn) -> None:
    """失败卖出不作为买入资金：卖出 unfilled 无资金来源；买入只靠现金。"""
    conn, _settings = portfolio_conn
    env = _env(conn, cash_cents=600_000, account_id="acct_sellfail")  # 6000 元
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    sim_id = created["simulation_account_id"]
    # 同一处理日：sell 计划先失败（无持仓），buy 只能用现金预算（600 元）。
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="sell",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="fail-sell-1",
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="after-sell-buy-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    sell_row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='fail-sell-1'"
    ).fetchone()
    assert tuple(sell_row) == ("unfilled", "s5_position_missing")
    buy_row = conn.execute(
        "SELECT status, filled_shares, remaining_shares FROM simulation_order"
        " WHERE idempotency_key='after-sell-buy-1'"
    ).fetchone()
    # 100 股 ×1000 + 500 = 100_500 > 600_000？不需要：600_000 现金足够 → 完全成交。
    assert tuple(buy_row) == ("filled", 100, 0)
    assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == 600_000 - 100_500

    # 更紧预算：现金 60_000，无卖出资金 → 买入一档也买不起 → unfilled。
    close_simulation_segment(conn, created["segment_id"], reason="close-for-budget-case")
    env2 = _env(conn, cash_cents=60_000, account_id="acct_sellfail2")
    env2["snapshot_id"] = _snapshot_lookup(conn, env2["account_id"])
    created2 = _segment(conn, env2)
    sim2 = created2["simulation_account_id"]
    place_simulation_order(
        conn,
        created2["segment_id"],
        ts_code="000001.SZ",
        direction="sell",
        planned_shares=100,
        trade_date=env2["trade_date"],
        idempotency_key="fail-sell-2",
    )
    place_simulation_order(
        conn,
        created2["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env2["trade_date"],
        idempotency_key="after-sell-buy-2",
    )
    advance_simulation_segment(conn, created2["segment_id"], on_trade_date=env2["trade_date"])
    assert get_simulation_account_state(conn, created2["segment_id"])["cash_cents"] == 60_000


# ------------------------------------------------------------------ 证据缺失 fail-closed


def test_missing_market_rules_fails_closed(portfolio_conn) -> None:
    """无 market_rules 证据 → 订单 unfilled + 持仓不动，不发明规则。"""
    conn, _settings = portfolio_conn
    env = _env(conn)
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    no_rules = _market_rules_bundle(conn, version="s5-test-rules-none", rules=None)
    created = create_simulation_segment(
        conn,
        source_account_id=env["account_id"],
        base_account_snapshot_id=env["snapshot_id"],
        rule_bundle_id=no_rules,
        fee_bundle_id=env["fee_bundle_id"],
        source_code=SRC,
        data_version_id=env["version_id"],
        as_of=env["as_of"],
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="no-rules-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='no-rules-1'"
    ).fetchone()
    assert tuple(row) == ("unfilled", "s5_market_rule_evidence_missing")
    sim_state = get_simulation_account_state(conn, created["segment_id"])
    assert sim_state["cash_cents"] == 10_000_000
    assert sim_state["positions"] == []


def test_missing_price_fails_closed(portfolio_conn) -> None:
    """处理日缺行情（000006.SZ 只有 4 天）→ unfilled；持仓不动。"""
    conn, _settings = portfolio_conn
    env = _env(conn)
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000006.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["days"][4],  # 该码只有 days[0..3] 四天行情
        idempotency_key="no-price-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["days"][4])
    row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='no-price-1'"
    ).fetchone()
    assert tuple(row) == ("unfilled", "s5_open_price_missing")
    assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == 10_000_000


def test_missing_fee_evidence_fails_closed(portfolio_conn) -> None:
    """费用证据缺失（组件不足）→ unfilled s5_fee_evidence_missing。"""
    conn, _settings = portfolio_conn
    env = _env(conn)
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(
        conn, env, fee_bundle_id=_incomplete_fee_bundle(conn)
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="no-fee-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='no-fee-1'"
    ).fetchone()
    assert tuple(row) == ("unfilled", "s5_fee_evidence_missing")
    assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == 10_000_000


def test_t_plus_one_evidence_unknown_acquisition_date_fails_closed(portfolio_conn) -> None:
    """启用 T+1 核验规则时，取得日未知 → fail-closed unfilled。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[_position("000001.SZ", 100, sellable=None, cost=None)],
        account_id="acct_t1",
    )
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    t1_bundle = _market_rules_bundle(
        conn,
        version="s5-test-rules-t1",
        rules=_market_rules(
            plans={"000001.SZ": [10.0] * len(env["days"])},
            days=env["days"],
            t_plus_one=True,
        ),
    )
    created = create_simulation_segment(
        conn,
        source_account_id=env["account_id"],
        base_account_snapshot_id=env["snapshot_id"],
        rule_bundle_id=t1_bundle,
        fee_bundle_id=env["fee_bundle_id"],
        source_code=SRC,
        data_version_id=env["version_id"],
        as_of=env["as_of"],
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="sell",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="t1-sell-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='t1-sell-1'"
    ).fetchone()
    # 期初持仓行不带 acquire_date（None 被忽略）→ 该日遇 acquire_date_unknown fail-closed。
    assert row[0] == "unfilled"


# ------------------------------------------------------------------ 生命周期终止


def test_cancel_and_close_lifecycle(portfolio_conn) -> None:
    """cancel pending → cancelled；close 段 → pending 订单 expired；重复 close 幂等。"""
    conn, _settings = portfolio_conn
    env = _env(conn)
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    state_before = get_simulation_account_state(conn, created["segment_id"])
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="cancel-1",
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=200,
        trade_date=env["days"][4],
        idempotency_key="expire-1",
    )
    cancelled = cancel_simulation_order(
        conn,
        conn.execute(
            "SELECT order_id FROM simulation_order WHERE idempotency_key='cancel-1'"
        ).fetchone()[0],
        reason="no longer needed",
    )
    assert cancelled["status"] == "cancelled"
    # 已处理订单不可取消：推进到 expire-1 的处理日（days[4]），使其成交。
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["days"][4])
    filled_id = conn.execute(
        "SELECT order_id FROM simulation_order WHERE idempotency_key='expire-1'"
    ).fetchone()
    # expire-1 计划 200 → 200 股 ×1000+500=200_500 ≤ 10_000_000 → filled
    with pytest.raises(DataError) as err_cancel_filled:
        cancel_simulation_order(conn, filled_id[0], reason="late")
    assert err_cancel_filled.value.reason == "s5_order_not_pending"

    # 关闭段：pending？全都被处理 → 无 pending；然后幂等 close。
    closed = close_simulation_segment(conn, created["segment_id"], reason="application end")
    assert closed["status"] == "closed" and closed["reused"] is False
    again = close_simulation_segment(conn, created["segment_id"], reason="application end")
    assert again["reused"] is True
    # 关闭后再 place → 拒绝
    with pytest.raises(DataError) as err_closed:
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="buy",
            planned_shares=100,
            trade_date=env["trade_date"],
            idempotency_key="after-close-1",
        )
    assert err_closed.value.reason == "s5_segment_closed"
    # 关闭后再推进：无害无处理
    later = advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["days"][5])
    assert later["processed"] == []

    # pending 订单关闭 → expired 并永不处理
    env2 = _env(conn, account_id="acct_close_pending2")
    env2["snapshot_id"] = _snapshot_lookup(conn, env2["account_id"])
    created2 = _segment(conn, env2)
    place_simulation_order(
        conn,
        created2["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env2["trade_date"],
        idempotency_key="expire-2",
    )
    close_simulation_segment(conn, created2["segment_id"], reason="test close")
    expired = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='expire-2'"
    ).fetchone()
    assert expired is not None and expired[0] == "expired"
    assert str(expired[1]).startswith("s5_expired_on_segment_close:")
    sim_state2 = get_simulation_account_state(conn, created2["segment_id"])
    assert sim_state2["cash_cents"] == 10_000_000
    assert sim_state2["positions"] == []
    assert state_before is not None


def test_misc_validation_errors(portfolio_conn) -> None:
    """入口校验：未知段/未知方向/段关闭/account 缺失。"""
    conn, _settings = portfolio_conn
    env = _env(conn)
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    with pytest.raises(DataError) as err_segment:
        place_simulation_order(
            conn,
            "segsim_missing",
            ts_code="000001.SZ",
            direction="buy",
            planned_shares=100,
            trade_date=env["trade_date"],
            idempotency_key="x",
        )
    assert err_segment.value.reason == "s5_segment_not_found"
    with pytest.raises(DataError) as err_direction:
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="short",
            planned_shares=100,
            trade_date=env["trade_date"],
            idempotency_key="x2",
        )
    assert err_direction.value.reason == "s5_direction_unknown"
    with pytest.raises(DataError) as err_account:
        create_simulation_segment(
            conn,
            source_account_id="acct_missing",
            base_account_snapshot_id=env["snapshot_id"],
            rule_bundle_id=env["rule_bundle_id"],
            fee_bundle_id=env["fee_bundle_id"],
            source_code=SRC,
            data_version_id=env["version_id"],
            as_of=env["as_of"],
        )
    assert err_account.value.reason == "s5_source_account_not_found"
    close_simulation_segment(conn, created["segment_id"], reason="relaunch")
    with pytest.raises(DataError) as err_closed_place:
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="buy",
            planned_shares=100,
            trade_date=env["trade_date"],
            idempotency_key="x3",
        )
    assert err_closed_place.value.reason == "s5_segment_closed"
    read = get_simulation_segment(conn, created["segment_id"])
    assert read["segment"]["status"] == "closed"
    assert SIMULATION_APPLICATION_VERSION == "s5a-simulation-2.0"


# ------------------------------------------------------------------ 零股卖出 / 关闭残量


def test_zero_lot_sell_is_allowed(portfolio_conn) -> None:
    """卖出不受买入 board lot 约束：150 股（非整手）可一次卖出。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[_position("000001.SZ", 150, sellable=150, cost=150_000)],
        account_id="acct_odd_lot",
    )
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="sell",
        planned_shares=150,
        trade_date=env["trade_date"],
        idempotency_key="odd-lot-sell-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    row = conn.execute(
        "SELECT status, filled_shares, remaining_shares FROM simulation_order"
        " WHERE idempotency_key='odd-lot-sell-1'"
    ).fetchone()
    assert tuple(row) == ("filled", 150, 0)
    assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == (
        10_000_000 + 150 * 1000 - 500
    )


def test_close_expires_partially_filled_remainder(portfolio_conn) -> None:
    """关闭段：partial 的剩余量一并 expired；关闭后推进不再处理。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[_position("000001.SZ", 100, sellable=60, cost=100_000)],
        account_id="acct_close_partial",
    )
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="sell",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="partial-close-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    partial = conn.execute(
        "SELECT status, filled_shares, remaining_shares FROM simulation_order"
        " WHERE idempotency_key='partial-close-1'"
    ).fetchone()
    assert tuple(partial) == ("partially_filled", 60, 40)

    close_simulation_segment(conn, created["segment_id"], reason="close with remainder")
    row = conn.execute(
        "SELECT status, filled_shares, remaining_shares, reason_code FROM simulation_order"
        " WHERE idempotency_key='partial-close-1'"
    ).fetchone()
    assert row[0] == "expired" and row[1] == 60 and row[2] == 40
    assert str(row[3]).startswith("s5_expired_on_segment_close:")
    later = advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["days"][5])
    assert later["processed"] == [] and later["status"] == "closed"


# ------------------------------------------------------------------ 证据缺失 fail-closed


def test_missing_suspension_evidence_fails_closed(portfolio_conn) -> None:
    """冻结版本没有停牌数据集 → 不能把“无记录”当作未停牌，订单保持 unfilled。"""
    conn, _settings = portfolio_conn
    env = _env(conn, include_suspension=False, account_id="acct_no_suspension")
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="no-suspension-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='no-suspension-1'"
    ).fetchone()
    assert tuple(row) == ("unfilled", "s5_suspension_evidence_missing")
    assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == 10_000_000


# ------------------------------------------------------------------ 重启落盘 / 失败回滚


def test_restart_durability_and_no_double_posting(portfolio_conn) -> None:
    """写命令自行提交；断开连接后重连仍可读，重复推进不重复入账。"""
    conn, settings = portfolio_conn
    env = _env(conn, account_id="acct_restart")
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="restart-buy-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    before = get_simulation_account_state(conn, created["segment_id"])
    events_before = len(list_simulation_events(conn, created["segment_id"]))
    assert before["cash_cents"] == 9_899_500
    assert events_before == 2  # opening + buy

    conn.close()
    reopened = portfolio_connect(settings.db_path)
    try:
        after = get_simulation_account_state(reopened, created["segment_id"])
        assert after == before
        assert len(list_simulation_events(reopened, created["segment_id"])) == events_before
        again = advance_simulation_segment(
            reopened, created["segment_id"], on_trade_date=env["trade_date"]
        )
        assert again["processed_count"] == 0
        assert get_simulation_account_state(reopened, created["segment_id"]) == before
        assert len(list_simulation_events(reopened, created["segment_id"])) == events_before
    finally:
        reopened.close()


def test_advance_failure_rolls_back_all_orders(portfolio_conn, monkeypatch) -> None:
    """一批推进中任一笔成交写入失败 → 整批回滚：订单仍 pending、现金/事件不变。"""
    conn, _settings = portfolio_conn
    env = _env(conn, account_id="acct_rollback")
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    for key in ("rb-buy-1", "rb-buy-2"):
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="buy",
            planned_shares=100,
            trade_date=env["trade_date"],
            idempotency_key=key,
        )

    calls = {"n": 0}
    real_insert_fill = simulation_module._insert_fill

    def failing_insert_fill(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected fill write failure")
        return real_insert_fill(*args, **kwargs)

    monkeypatch.setattr(simulation_module, "_insert_fill", failing_insert_fill)
    with pytest.raises(RuntimeError):
        advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    monkeypatch.setattr(simulation_module, "_insert_fill", real_insert_fill)

    statuses = conn.execute(
        "SELECT idempotency_key, status FROM simulation_order WHERE segment_id=?"
        " ORDER BY idempotency_key",
        (created["segment_id"],),
    ).fetchall()
    assert [tuple(row) for row in statuses] == [
        ("rb-buy-1", "pending"),
        ("rb-buy-2", "pending"),
    ]
    state = get_simulation_account_state(conn, created["segment_id"])
    assert state["cash_cents"] == 10_000_000
    assert state["positions"] == []
    assert len(list_simulation_events(conn, created["segment_id"])) == 1  # 仅 opening


# ------------------------------------------------------------------ 规则/证据覆盖负例


def test_suspension_evidence_must_cover_current_code(portfolio_conn) -> None:
    """类别存在但规则包只覆盖其他证券 → 当前证券仍 fail-closed。"""
    conn, _settings = portfolio_conn
    env = _env(conn, account_id="acct_susp_cover")
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    rules = _market_rules(
        plans={"000001.SZ": [10.0] * len(env["days"])},
        days=env["days"],
        suspension_codes=["000006.SZ"],
    )
    created = _segment(
        conn, env, rules_version="s5-test-rules-susp-cover", rules=rules
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="susp-cover-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='susp-cover-1'"
    ).fetchone()
    assert tuple(row) == ("unfilled", "s5_suspension_evidence_missing")


def test_price_limit_evidence_missing_fails_closed(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _env(conn, account_id="acct_no_limit")
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    rules = _market_rules(
        plans={"000001.SZ": [10.0] * len(env["days"])},
        days=env["days"],
        include_price_limits=False,
    )
    created = _segment(conn, env, rules_version="s5-test-rules-no-limit", rules=rules)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="no-limit-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='no-limit-1'"
    ).fetchone()
    assert tuple(row) == ("unfilled", "s5_price_limit_evidence_missing")


def test_open_at_price_limit_fails_closed(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _env(conn, account_id="acct_at_limit")
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    rules = _market_rules(
        plans={"000001.SZ": [10.0] * len(env["days"])}, days=env["days"]
    )
    for item in rules["price_limit_evidence"]["bounds"]:
        if item["ts_code"] == "000001.SZ":
            item["lower_cents"] = 900
            item["upper_cents"] = 1000  # open=1000 正好触及上限
    created = _segment(conn, env, rules_version="s5-test-rules-at-limit", rules=rules)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="at-limit-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='at-limit-1'"
    ).fetchone()
    assert tuple(row) == ("unfilled", "s5_open_at_price_limit")


def test_t_plus_one_flag_missing_fails_closed(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[_position("000001.SZ", 100, sellable=100, cost=100_000)],
        account_id="acct_t1_missing",
    )
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    rules = _market_rules(
        plans={"000001.SZ": [10.0] * len(env["days"])},
        days=env["days"],
        include_t_plus_one=False,
    )
    created = _segment(conn, env, rules_version="s5-test-rules-t1-missing", rules=rules)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="sell",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="t1-missing-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    row = conn.execute(
        "SELECT status, reason_code FROM simulation_order WHERE idempotency_key='t1-missing-1'"
    ).fetchone()
    assert tuple(row) == ("unfilled", "s5_t_plus_one_evidence_missing")


def test_same_day_sale_proceeds_rule_controls_buy_funding(portfolio_conn) -> None:
    """未获规则允许时，同日卖出收入不能给买入融资；显式允许后才可用。"""
    conn, _settings = portfolio_conn
    for reuse, account_id, version, expected_buy, expected_cash in (
        (False, "acct_reuse_false", "s5-test-rules-reuse-no", "unfilled", 100_500),
        (True, "acct_reuse_true", "s5-test-rules-reuse-yes", "filled", 0),
    ):
        env = _env(
            conn,
            cash_cents=1_000,
            positions=[_position("000001.SZ", 100, sellable=100, cost=100_000)],
            account_id=account_id,
        )
        env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
        rules = _market_rules(
            plans={"000001.SZ": [10.0] * len(env["days"])},
            days=env["days"],
            same_day_reuse=reuse,
        )
        created = _segment(conn, env, rules_version=version, rules=rules)
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="sell",
            planned_shares=100,
            trade_date=env["trade_date"],
            idempotency_key=f"{version}-sell",
        )
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="buy",
            planned_shares=100,
            trade_date=env["trade_date"],
            idempotency_key=f"{version}-buy",
        )
        advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
        buy_row = conn.execute(
            "SELECT status FROM simulation_order WHERE idempotency_key=?",
            (f"{version}-buy",),
        ).fetchone()
        assert str(buy_row[0]) == expected_buy
        assert get_simulation_account_state(conn, created["segment_id"])["cash_cents"] == expected_cash


def test_advance_is_monotonic_and_rejects_backdated_orders(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _env(conn, account_id="acct_monotonic")
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["days"][3],
        idempotency_key="mono-1",
    )
    advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["days"][3])

    with pytest.raises(DataError) as err_backdated:
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="buy",
            planned_shares=100,
            trade_date=env["days"][3],
            idempotency_key="mono-backdated",
        )
    assert err_backdated.value.reason == "s5_trade_date_already_advanced"

    with pytest.raises(DataError) as err_regress:
        advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["days"][2])
    assert err_regress.value.reason == "s5_on_trade_date_regressed"

    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["days"][4],
        idempotency_key="mono-2",
    )
    advanced = advance_simulation_segment(
        conn, created["segment_id"], on_trade_date=env["days"][4]
    )
    assert advanced["processed_count"] == 1
    assert advanced["processed"][0]["status"] == "filled"


def test_invalid_calendar_dates_are_rejected(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _env(conn, account_id="acct_bad_date")
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    with pytest.raises(DataError) as err_trade_date:
        place_simulation_order(
            conn,
            created["segment_id"],
            ts_code="000001.SZ",
            direction="buy",
            planned_shares=100,
            trade_date="20250231",
            idempotency_key="bad-date-1",
        )
    assert err_trade_date.value.reason == "s5_trade_date_invalid"
    with pytest.raises(DataError) as err_on_date:
        advance_simulation_segment(conn, created["segment_id"], on_trade_date="20250231")
    assert err_on_date.value.reason == "s5_on_trade_date_invalid"


# ------------------------------------------------------------------ 部分成交后续执行 / 可卖解锁


def test_partial_sell_continues_on_later_trading_day(portfolio_conn) -> None:
    """T+1 批次：首日只能卖已解锁 100 股，次日剩余 100 股解锁后继续执行。"""
    conn, _settings = portfolio_conn
    env = _env(conn, account_id="acct_partial_continue")
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    rules = _market_rules(
        plans={"000001.SZ": [10.0] * len(env["days"])},
        days=env["days"],
        t_plus_one=True,
    )
    created = _segment(
        conn, env, rules_version="s5-test-rules-continue", rules=rules
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["days"][3],
        idempotency_key="cont-buy-1",
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["days"][4],
        idempotency_key="cont-buy-2",
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="sell",
        planned_shares=200,
        trade_date=env["days"][4],
        idempotency_key="cont-sell-1",
    )

    first = advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["days"][3])
    assert first["processed_count"] == 1 and first["processed"][0]["status"] == "filled"
    second = advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["days"][4])
    statuses = {item["order_id"]: item["status"] for item in second["processed"]}
    sell_order = conn.execute(
        "SELECT order_id FROM simulation_order WHERE idempotency_key='cont-sell-1'"
    ).fetchone()[0]
    assert statuses[str(sell_order)] == "partially_filled"
    row = conn.execute(
        "SELECT status, filled_shares, remaining_shares, fee_estimate_cents"
        " FROM simulation_order WHERE idempotency_key='cont-sell-1'"
    ).fetchone()
    assert tuple(row) == ("partially_filled", 100, 100, 500)

    third = advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["days"][5])
    assert third["processed_count"] == 1
    assert third["processed"][0]["status"] == "filled"
    row = conn.execute(
        "SELECT status, filled_shares, remaining_shares, fee_estimate_cents"
        " FROM simulation_order WHERE idempotency_key='cont-sell-1'"
    ).fetchone()
    assert tuple(row) == ("filled", 200, 0, 500)  # 最低佣金整单只计一次
    fills = conn.execute(
        "SELECT fill_seq, filled_shares, fee_cents FROM simulation_fill"
        " WHERE order_id=? ORDER BY fill_seq",
        (sell_order,),
    ).fetchall()
    assert [tuple(item) for item in fills] == [(1, 100, 500), (2, 100, 0)]
    state = get_simulation_account_state(conn, created["segment_id"])
    # 买 200×1000+500×2=201000；卖 200×1000-500=199500；净费用 1500。
    assert state["cash_cents"] == 10_000_000 - 1_500
    assert state["positions"][0]["shares"] == 0


def test_same_day_repeat_advance_does_not_retry_partial_order(portfolio_conn) -> None:
    """同日重复推进：partially_filled 不再次尝试/追加成交；次日才继续。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        cash_cents=10_000_000,
        positions=[_position("000001.SZ", 100, sellable=60, cost=100_000)],
        account_id="acct_same_day_repeat",
    )
    env["snapshot_id"] = _snapshot_lookup(conn, env["account_id"])
    created = _segment(conn, env)
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="sell",
        planned_shares=160,
        trade_date=env["trade_date"],
        idempotency_key="same-day-sell",
    )
    place_simulation_order(
        conn,
        created["segment_id"],
        ts_code="000001.SZ",
        direction="buy",
        planned_shares=100,
        trade_date=env["trade_date"],
        idempotency_key="same-day-buy",
    )
    first = advance_simulation_segment(conn, created["segment_id"], on_trade_date=env["trade_date"])
    assert first["processed_count"] == 2
    sell_id = conn.execute(
        "SELECT order_id FROM simulation_order WHERE idempotency_key='same-day-sell'"
    ).fetchone()[0]
    before_state = get_simulation_account_state(conn, created["segment_id"])
    before_order = tuple(
        conn.execute(
            "SELECT status, filled_shares, remaining_shares, fee_estimate_cents,"
            " last_attempt_date FROM simulation_order WHERE order_id=?",
            (sell_id,),
        ).fetchone()
    )
    before_fills = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(filled_shares),0), COALESCE(SUM(fee_cents),0)"
        " FROM simulation_fill WHERE order_id=?",
        (sell_id,),
    ).fetchone()
    assert before_order[0] == "partially_filled" and before_order[1] == 60
    assert before_order[2] == 100
    assert before_order[4] == env["trade_date"]

    repeated = advance_simulation_segment(
        conn, created["segment_id"], on_trade_date=env["trade_date"]
    )
    assert repeated["processed_count"] == 0 and repeated["processed"] == []
    assert get_simulation_account_state(conn, created["segment_id"]) == before_state
    after_order = tuple(
        conn.execute(
            "SELECT status, filled_shares, remaining_shares, fee_estimate_cents,"
            " last_attempt_date FROM simulation_order WHERE order_id=?",
            (sell_id,),
        ).fetchone()
    )
    assert after_order == before_order
    after_fills = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(filled_shares),0), COALESCE(SUM(fee_cents),0)"
        " FROM simulation_fill WHERE order_id=?",
        (sell_id,),
    ).fetchone()
    assert tuple(after_fills) == tuple(before_fills)

    # 下一个交易日仍会继续尝试剩余量（本场景买入批次已解锁，可继续成交）。
    next_day = advance_simulation_segment(
        conn, created["segment_id"], on_trade_date=env["days"][4]
    )
    assert next_day["processed_count"] == 1
    assert next_day["processed"][0]["status"] == "filled"
