"""阶段三 S4：建议编排（input 快照/幂等/费用/独立模式/失效）聚焦测试。

全部使用隔离临时库（``data_settings``/``portfolio_conn``，schema 4，外键开启），
通过阶段二写入/发布/股票池/绑定路径与阶段三 S2 账本构造数据；手算全部
信号/费用/预算数值。不访问真实数据源、不运行应用、不改动阶段二/三代码。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import sqlite3

import pytest

from app.data.chunks import publish_chunked_version
from app.data.errors import DataError
from app.data.migration import initialize_database
from app.data.store import write_records
from app.data.universe import (
    PoolRule,
    generate_pool_snapshot,
    load_pool_snapshot,
)
from app.portfolio.db import portfolio_connect
from app.portfolio.ledger import (
    AccountEventRequest,
    create_account,
    get_account_state,
    post_account_event,
)
from app.portfolio.recommendation import (
    ANALYSIS_APPLICATION_VERSION,
    ensure_analysis_artifact_table,
    create_recommendation_run,
    get_recommendation_run,
    invalidate_recommendation_run,
    list_recommendation_runs,
)
from app.portfolio.rule_bundles import (
    DEFAULT_FEE_BUNDLE,
    create_rule_bundle,
    ensure_default_bundles,
)
from app.portfolio.service import (
    RESTRICTION_UNIVERSE_UNVERIFIED,
    DataSelectionContext,
    bind_data_context,
    register_pool_scope_evidence,
)

REC_1 = "2025-06-05T01:00:00+00:00"
REC_2 = "2025-06-05T02:00:00+00:00"
SRC = "tushare-fixture"
TREND_MIN = 120

_TEST_POOL_RULE = PoolRule(min_avg_amount_cny=0.0)


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


def _closes_step(total: int, base: float = 10.0, up: float = 13.0, base_count: int = 65) -> list[float]:
    return [base] * base_count + [up] * (total - base_count)


def _closes_flat(total: int, price: float = 10.0) -> list[float]:
    return [price] * total


def _seed_version(
    conn: sqlite3.Connection,
    *,
    days: list[str],
    plans: dict[str, list],
    source_code: str = SRC,
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
                    "open": close if close is not None else None,
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
    return str(
        publish_chunked_version(
            conn,
            source_code=source_code,
            scope={},
            categories=["daily", "adj_factor", "calendar", "security"],
            rule_version="test-rules",
            quality_summary={},
            window_mode="full",
        )["version_id"]
    )


def _pool(
    conn: sqlite3.Connection, version_id: str, as_of: str, ts_codes: list[str], source_code: str = SRC
) -> dict:
    snapshot = generate_pool_snapshot(
        conn,
        as_of=as_of,
        data_version_id=version_id,
        source_code=source_code,
        rule=_TEST_POOL_RULE,
        ts_codes=list(ts_codes),
    )
    loaded = load_pool_snapshot(conn, str(snapshot["snapshot_id"]))
    assert loaded is not None and loaded["data_version_id"] == version_id
    return loaded


def _binding(conn: sqlite3.Connection, context: DataSelectionContext):
    return bind_data_context(conn, context)


def _account(
    conn: sqlite3.Connection,
    *,
    cash_cents: int = 1_000_000,
    positions: list[dict] | None = None,
    account_id: str = "acct_s4",
) -> dict:
    create_account(conn, account_id, "S4 测试账户")
    request = AccountEventRequest(
        account_id=account_id,
        event_type="opening",
        business_date="20250301",
        idempotency_key="open-1",
        payload={"cash_cents": cash_cents, "positions": list(positions or [])},
        recorded_at=REC_1,
        reason="期初录入",
    )
    post_account_event(conn, request, expected_account_revision=0)
    return get_account_state(conn, account_id)


def _position(
    ts_code: str,
    shares: int,
    total_cost_cents: int | None = None,
    *,
    locked: bool = False,
    sellable_shares: int | None = None,
) -> dict:
    return {
        "ts_code": ts_code,
        "shares": shares,
        "total_cost_cents": total_cost_cents,
        "sellable_shares": shares if sellable_shares is None else sellable_shares,
        "acquire_date": "20250201",
        "locked": locked,
    }


def _env(
    conn: sqlite3.Connection,
    *,
    cash_cents: int = 1_000_000,
    positions: list[dict] | None = None,
    reserve_cents: int = 50_000,
    account_id: str = "acct_s4",
    use_versioned_constraints: bool = True,
) -> dict:
    """一个定位在"理想目标可编排"场景的无状态环境（两码 + 账户 + 绑定）。"""
    days = _business_days(TREND_MIN + 5)
    plans = {
        "000001.SZ": _closes_step(len(days)),   # momentum 0.3，P(t)>trend：被选
        "000004.SZ": _closes_flat(len(days)),   # 动量 0：门槛排除；池内
    }
    version_id = _seed_version(conn, days=days, plans=plans)
    as_of = days[-1]
    pool = _pool(conn, version_id, as_of, ["000001.SZ", "000004.SZ"])
    register_pool_scope_evidence(
        conn,
        snapshot_id=str(pool["snapshot_id"]),
        verification_source="test fixture",
        verified_by="test",
        evidence_payload="test evidence",
    )
    bundle = ensure_default_bundles(conn)
    if use_versioned_constraints:
        parameter_bundle_id = str(
            create_rule_bundle(
                conn,
                kind="parameter",
                version=f"s4-test-alloc-{reserve_cents}",
                assumption_scope="synthetic_simulation",
                content={
                    "bundle_kind": "parameter",
                    "target_constraints": {
                        "cash_reserve_cents": reserve_cents,
                        "per_security_weight_cap_bp": 10_000,
                    },
                    # 该夹具显式允许未入选目标持仓退出，以保留既有清仓算术用例；
                    # 默认参数组合仍为保守保留（见 rule_bundles）。
                    "rebalance_buffer": {
                        "retain_unselected_holdings": False,
                        "min_trade_notional_cents": 0,
                    },
                },
            )["bundle_id"]
        )
    else:
        parameter_bundle_id = str(bundle["parameter"])
    initial_state = _account(conn, cash_cents=cash_cents, positions=positions, account_id=account_id)
    bound_default = _binding(
        conn,
        DataSelectionContext(
            source_code=SRC,
            as_of=as_of,
            purpose="signal_input",
            data_version_id=version_id,
            account_revision=int(initial_state["revision"]),
            rule_bundle_id=parameter_bundle_id,
            pool_snapshot_id=str(pool["snapshot_id"]),
        ),
    )
    # S1 修复本里股票池证据两旗标未决 → 真实绑定恒受限；S4 编排测试用等价的
    # "已核验范围" 上下文解锁编排逻辑本身（replace 只改受限标注，不改数据）。
    bound_unrestricted = dataclasses.replace(
        bound_default, restricted=False, restriction_code=None, restriction_detail=None
    )
    return {
        "conn": conn,
        "version_id": version_id,
        "as_of": as_of,
        "pool": pool,
        "parameter_bundle_id": parameter_bundle_id,
        "fee_bundle_id": str(bundle["fee"]),
        "account_id": account_id,
        "bound_default": bound_default,
        "bound": bound_unrestricted,
        "initial_state": initial_state,
    }


def _advance_revision(env: dict, *, delta_cents: int = 100) -> dict:
    """在账户上追加一笔 cash_in 事件，推进修订并产生新的不可变快照。"""
    conn = env["conn"]
    request = AccountEventRequest(
        account_id=env["account_id"],
        event_type="cash_in",
        business_date="20250302",
        idempotency_key=f"cash-in-{env['initial_state']['revision'] + 1}",
        payload={"amount_cents": delta_cents},
        recorded_at=REC_2,
        reason="测试入金",
    )
    state_before = get_account_state(conn, env["account_id"])
    post_account_event(
        conn, request, expected_account_revision=int(state_before["revision"])
    )
    new_state = get_account_state(conn, env["account_id"])
    parameter_bundle_id = env["parameter_bundle_id"]
    new_binding = dataclasses.replace(
        _rebind(env, int(new_state["revision"]), parameter_bundle_id),
        restricted=False,
        restriction_code=None,
        restriction_detail=None,
    )
    env["new_state"] = new_state
    env["new_bound"] = new_binding
    return env


def _rebind(env: dict, revision: int, parameter_bundle_id: str):
    conn = env["conn"]
    return bind_data_context(
        conn,
        DataSelectionContext(
            source_code=SRC,
            as_of=env["as_of"],
            purpose="signal_input",
            data_version_id=env["version_id"],
            account_revision=revision,
            rule_bundle_id=parameter_bundle_id,
            pool_snapshot_id=str(env["pool"]["snapshot_id"]),
        ),
    )


def _counts(conn: sqlite3.Connection, run_id: str) -> dict:
    ensure_analysis_artifact_table(conn)
    return {
        "run_rows": conn.execute(
            "SELECT COUNT(*) FROM analysis_run WHERE run_id=?", (run_id,)
        ).fetchone()[0],
        "items": conn.execute(
            "SELECT COUNT(*) FROM analysis_item WHERE run_id=?", (run_id,)
        ).fetchone()[0],
        "targets": conn.execute(
            "SELECT COUNT(*) FROM portfolio_target WHERE run_id=?", (run_id,)
        ).fetchone()[0],
        "target_items": conn.execute(
            "SELECT COUNT(*) FROM target_item WHERE target_id IN"
            " (SELECT target_id FROM portfolio_target WHERE run_id=?)",
            (run_id,),
        ).fetchone()[0],
        "artifacts": conn.execute(
            "SELECT COUNT(*) FROM analysis_artifact WHERE run_id=?", (run_id,)
        ).fetchone()[0],
        "holds": conn.execute(
            "SELECT COUNT(*) FROM retention_hold WHERE reason LIKE ?", (f"analysis_run:{run_id}%",)
        ).fetchone()[0],
    }


# ------------------------------------------------------------------ 编排端到端


def test_recommendation_end_to_end_incremental_full_orchestration(portfolio_conn) -> None:
    """增量模式端到端手算：买满/卖清仓/锁定可见/费用与预算逐分对账。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        cash_cents=1_000_000,
        positions=[
            _position("000001.SZ", 500, total_cost_cents=65_000),   # 目标内减仓路径的持有人
            _position("000004.SZ", 40, total_cost_cents=40_000),    # 目标外 → 建议清仓
            _position("600999.SH", 10, total_cost_cents=None),      # 版本外 → sell_blocked
        ],
    )
    result = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
        mode="incremental",
    )
    assert result["reused"] is False and result["recomputed"] is True
    run_id = result["run_id"]
    assert re.fullmatch(r"ar_[0-9a-f]{24}", run_id)

    report = result["report"]
    assert report["application_version"] == ANALYSIS_APPLICATION_VERSION
    assert report["restricted"] is False
    assert report["mode"] == "incremental"
    assert report["account_id"] == "acct_s4"
    items = {item["ts_code"] + "/" + item["decision"]: item for item in report["items"]}
    assert set(items) == {
        "000001.SZ/buy_full",
        "000004.SZ/sell_proposed",
        "600999.SH/sell_blocked",
    }
    buy = items["000001.SZ/buy_full"]
    # 理想目标：可投资金 = 现金 1_000_000 − 保留 50_000 = 950_000 → 等权重
    # 到 000001.SZ 收盘价 13.00 → 1300 分 → 730 股；待买 = 730 − 500 = 230 股。
    # 手算费用：notional 230×1300 = 299_000；佣金 299_000×5bp = 149.5 → 150 →
    # max(150, 500) = 500；过户 299_000×1bp = 29.9 → 30；滑点 10bp = 299。
    # total = 299_000 + 500 + 30 + 299 = 299_829 ≤ 预算 950_000 → 买满。
    assert buy["quantity_shares"] == 230
    cost = buy["cost_detail"]
    assert cost["notional_cents"] == 299_000
    assert cost["fee_cents"] == 500 + 30
    assert cost["slippage_cents"] == 299
    assert buy["restriction_code"] is None
    cash_used = 299_829
    sell = items["000004.SZ/sell_proposed"]
    # 清仓 40 股 × 1000 分 = 40_000；佣金 min 500；印花 20；过户 4；滑点 40。
    assert sell["quantity_shares"] == 40
    assert sell["cost_detail"]["fee_cents"] == 524
    assert sell["cost_detail"]["contingent_estimated_proceeds_cents"] == 39_436
    assert sell["restriction_code"] is None
    blocked = items["600999.SH/sell_blocked"]
    assert blocked["restriction_code"] == "holdings_unsupported_or_unvalued"
    assert blocked["quantity_shares"] == 0

    totals = report["totals"]
    assert totals["cash_funded_buys_cents"] == cash_used
    assert totals["residual_cash_cents"] == 1_000_000 - 50_000 - cash_used
    assert totals["sell_contingent_estimated_proceeds_cents"] == 39_436
    budget = report["budget"]
    # 对账恒等式：reserved + cash_used + residual == cash（逐分）
    assert (
        budget["reserved_cents"] + totals["cash_funded_buys_cents"] + totals["residual_cash_cents"]
        == 1_000_000
    )
    assert budget["same_day_sale_proceeds_reusable"] is False
    assert budget["contingent_estimated_proceeds_cents"] == 0

    # 持久化行与工件
    stored = _counts(conn, run_id)
    assert stored["run_rows"] == 1 and stored["items"] == len(report["items"])
    assert stored["targets"] == 1 and stored["target_items"] == 1
    assert stored["artifacts"] == 2 and stored["holds"] == 2
    status_row = conn.execute(
        "SELECT status, restriction_code, mode, account_revision FROM analysis_run WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert status_row[0] == "complete" and status_row[1] is None
    target_row = conn.execute(
        "SELECT kind, cash_cents FROM portfolio_target WHERE run_id=?", (run_id,)
    ).fetchone()
    assert target_row[0] == "executable_transition"
    assert target_row[1] == totals["residual_cash_cents"]
    target_item_row = conn.execute(
        "SELECT ts_code, target_shares, delta_shares FROM target_item WHERE target_id"
        " IN (SELECT target_id FROM portfolio_target WHERE run_id=?)",
        (run_id,),
    ).fetchone()
    assert target_item_row[0] == "000001.SZ"
    assert target_item_row[1] == 730 and target_item_row[2] == 230


def test_recommendation_same_input_is_idempotent(portfolio_conn) -> None:
    """同一输入第二次编排：返回同 run，不重算、不新增任何行。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[_position("000001.SZ", 500, total_cost_cents=65_000)],
    )
    first = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    before = _counts(conn, first["run_id"])
    second = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    after = _counts(conn, second["run_id"])
    assert second["reused"] is True and second["recomputed"] is False
    assert second["run_id"] == first["run_id"]
    assert second["report"] == first["report"]
    assert before == after


def test_recommendation_changed_input_creates_new_immutable_run(portfolio_conn) -> None:
    """输入变化（账号修订前进 + 现金变化）→ 新 run；旧 run 与工件一字不变。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[_position("000004.SZ", 40, total_cost_cents=40_000)],
    )
    old = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    old_id = old["run_id"]
    old_artifact = conn.execute(
        "SELECT content, content_sha256 FROM analysis_artifact WHERE run_id=? AND artifact_kind='output'",
        (old_id,),
    ).fetchone()
    old_counts = _counts(conn, old_id)

    env = _advance_revision(env, delta_cents=50_000)
    new = create_recommendation_run(
        conn,
        env["new_bound"],
        account_id=env["account_id"],
        account_state=env["new_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    assert new["run_id"] != old_id
    assert new["reused"] is False
    new_counts = _counts(conn, old_id)
    assert new_counts == old_counts
    new_artifact = conn.execute(
        "SELECT content, content_sha256 FROM analysis_artifact WHERE run_id=? AND artifact_kind='output'",
        (old_id,),
    ).fetchone()
    assert tuple(new_artifact) == tuple(old_artifact)
    # 旧读取路径：修订前进口不重算，只标注 invalidated
    old_read = get_recommendation_run(conn, old_id)
    assert old_read["input_invalidated"] is True
    assert old_read["effective"] is False
    assert old_read["account_revision_current"] == int(env["new_state"]["revision"])
    assert old_read["report"] == old["report"]


def test_recommendation_independent_mode_uses_same_budget_without_sells(portfolio_conn) -> None:
    """独立模式：同一预算的独立目标；不提议卖出；持仓只做转换评估。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[
            _position("000004.SZ", 40, total_cost_cents=40_000),
        ],
    )
    result = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
        mode="independent",
    )
    assert result["reused"] is False
    report = result["report"]
    assert report["mode"] == "independent"
    decisions = {(i["ts_code"], i["decision"]) for i in report["items"]}
    assert ("000004.SZ", "sell_proposed") not in decisions
    # 目标与转换评估显式可见
    assert ("000001.SZ", "independent_target") in decisions
    assert ("000004.SZ", "conversion_assessment") in decisions
    target_item = next(
        i for i in report["items"] if i["decision"] == "independent_target"
    )
    # 理想目标 950_000 // 1300 = 730 股（独立于当前持仓给出）
    assert target_item["quantity_shares"] == 730
    budget = report["budget"]
    assert budget["contingent_estimated_proceeds_cents"] == 0
    target_row = conn.execute(
        "SELECT kind FROM portfolio_target WHERE run_id=?", (result["run_id"],)
    ).fetchone()
    assert target_row[0] == "ideal"
    identity = (
        budget["reserved_cents"]
        + report["totals"]["cash_funded_buys_cents"]
        + report["totals"]["residual_cash_cents"]
    )
    assert identity == budget["cash_cents"]


def test_recommendation_insufficient_funds_stays_partial_and_visible(portfolio_conn) -> None:
    """资金不足：部分买入 + 显式缺口；不隐藏也不改写目标。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        cash_cents=5_000,
        reserve_cents=1_000,
    )
    result = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    report = result["report"]
    item = next(i for i in report["items"] if i["ts_code"] == "000001.SZ")
    # 目标 4_000 // 1300 = 3 股；3 股总价 4_404 > 预算 4_000；2 股 3_103 ≤ → 只买 2
    # 手算：notional 2_600；佣金 max(1.3→1, 500) = 500；过户 2_600×1bp = 0.26 → 0；
    # 滑点 2_600×10bp = 2.6 → 3；total = 3_103。
    assert item["decision"] == "buy_partial"
    assert item["requested_shares"] == 3
    assert item["quantity_shares"] == 2
    assert item["restriction_code"] == "insufficient_funds"
    assert item["cost_detail"]["notional_cents"] == 2_600
    totals = report["totals"]
    assert totals["cash_funded_buys_cents"] == 3_103
    assert (
        report["budget"]["reserved_cents"]
        + totals["cash_funded_buys_cents"]
        + totals["residual_cash_cents"]
        == 5_000
    )


def test_recommendation_locked_position_never_sold(portfolio_conn) -> None:
    """锁定持仓：绝不自动清仓；sell_blocked + position_locked 显式保留。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[
            _position("000004.SZ", 40, total_cost_cents=40_000, locked=True),
        ],
    )
    result = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    item = next(
        i
        for i in result["report"]["items"]
        if i["ts_code"] == "000004.SZ" and i["decision"] == "sell_blocked"
    )
    assert item["restriction_code"] == "position_locked"
    assert item["quantity_shares"] == 0


def test_recommendation_stale_account_state_is_rejected_before_any_write(
    portfolio_conn,
) -> None:
    """stale 账户状态：s3_account_revision_mismatch 在任何写库前拒绝。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[_position("000004.SZ", 40, total_cost_cents=40_000)],
    )
    first = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    before = _counts(conn, first["run_id"])
    run_rows_before = conn.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0]

    env = _advance_revision(env)
    stale_bound = _rebind(env, int(env["new_state"]["revision"]), env["parameter_bundle_id"])
    with pytest.raises(DataError) as err:
        create_recommendation_run(
            conn,
            stale_bound,
            account_id=env["account_id"],
            account_state=env["initial_state"],  # 修订已过时（revision=1）
            fee_bundle_id=env["fee_bundle_id"],
        )
    assert err.value.reason == "s3_account_revision_mismatch"
    run_rows_after = conn.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0]
    assert run_rows_after == run_rows_before
    assert _counts(conn, first["run_id"]) == before


def test_recommendation_default_bundle_produces_restricted_run(portfolio_conn) -> None:
    """默认参数组合（无版本化约束/绑定受限）→ 受限 run，仅持仓另估可见。"""
    conn, _settings = portfolio_conn
    env = _env(
        conn,
        positions=[_position("000004.SZ", 40, total_cost_cents=40_000)],
        use_versioned_constraints=False,
    )
    result = create_recommendation_run(
        conn,
        env["bound_default"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    assert result["reused"] is False
    report = result["report"]
    assert result["run_id"]
    assert report["restricted"] is True
    stored = _counts(conn, result["run_id"])
    assert stored["targets"] == 0 and stored["target_items"] == 0
    status_row = conn.execute(
        "SELECT status, restriction_code FROM analysis_run WHERE run_id=?",
        (result["run_id"],),
    ).fetchone()
    assert status_row[0] == "restricted"
    assert status_row[1] == RESTRICTION_UNIVERSE_UNVERIFIED
    assert (
        stored["items"]
        == conn.execute(
            "SELECT COUNT(*) FROM analysis_item WHERE run_id=? AND decision='assessment_only'",
            (result["run_id"],),
        ).fetchone()[0]
    )


def test_recommendation_fee_evidence_missing_fails_closed(portfolio_conn) -> None:
    """费用证据缺失：未知 bundle/组件不齐/类别错 → s4_fee_evidence_missing。"""
    conn, _settings = portfolio_conn
    env = _env(conn, positions=[])
    with pytest.raises(DataError) as err:
        create_recommendation_run(
            conn,
            env["bound"],
            account_id=env["account_id"],
            account_state=env["initial_state"],
            fee_bundle_id="rb_missing_sym",
        )
    assert err.value.reason == "s4_fee_evidence_missing"

    with pytest.raises(DataError) as err_kind:
        create_recommendation_run(
            conn,
            env["bound"],
            account_id=env["account_id"],
            account_state=env["initial_state"],
            fee_bundle_id=env["parameter_bundle_id"],
        )
    assert err_kind.value.reason == "s4_fee_evidence_missing"

    partial = create_rule_bundle(
        conn,
        kind="fee",
        version="s4-test-fee-partial",
        assumption_scope="synthetic_simulation",
        content={
            "bundle_kind": "fee",
            "currency": "CNY",
            "components": [
                {"code": "commission", "rate_bp": 5, "minimum_cents": 500, "charging": "per_side"},
            ],
        },
    )
    with pytest.raises(DataError) as err_partial:
        create_recommendation_run(
            conn,
            env["bound"],
            account_id=env["account_id"],
            account_state=env["initial_state"],
            fee_bundle_id=str(partial["bundle_id"]),
        )
    assert err_partial.value.reason == "s4_fee_evidence_missing"
    # fail-closed 逐组件检查：首个缺失登记组件（stamp_duty_sell）必须被点名。
    assert "stamp_duty_sell" in str(err_partial.value)

    content = dict(DEFAULT_FEE_BUNDLE)
    broken = create_rule_bundle(
        conn,
        kind="fee",
        version="s4-test-fee-charging-mismatch",
        assumption_scope="synthetic_simulation",
        content={
            **content,
            "components": [
                {**c, "charging": "per_order"} if c["code"] == "slippage" else c
                for c in content["components"]
            ],
        },
    )
    with pytest.raises(DataError) as err_charging:
        create_recommendation_run(
            conn,
            env["bound"],
            account_id=env["account_id"],
            account_state=env["initial_state"],
            fee_bundle_id=str(broken["bundle_id"]),
        )
    assert err_charging.value.reason == "s4_fee_evidence_missing"


def test_recommendation_invalidate_and_read_back(portfolio_conn) -> None:
    """显式失效：只写 valid_until/reason；工件不再改写；重复失效幂等/冲突码。"""
    conn, _settings = portfolio_conn
    env = _env(conn, positions=[])
    result = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    run_id = result["run_id"]
    artifact_before = conn.execute(
        "SELECT content FROM analysis_artifact WHERE run_id=? AND artifact_kind='output'",
        (run_id,),
    ).fetchone()
    invalidated = invalidate_recommendation_run(
        conn,
        run_id,
        reason="test_manual_invalidation",
        expiry_time="2025-06-05T09:00:00+00:00",
    )
    assert invalidated["already"] is False
    again = invalidate_recommendation_run(conn, run_id, reason="test_manual_invalidation")
    assert again["already"] is True
    with pytest.raises(DataError) as err:
        invalidate_recommendation_run(
            conn, run_id, reason="different_reason_cannot_rewrite"
        )
    assert err.value.reason == "s4_run_already_invalidated"
    artifact_after = conn.execute(
        "SELECT content FROM analysis_artifact WHERE run_id=? AND artifact_kind='output'",
        (run_id,),
    ).fetchone()
    assert artifact_after == artifact_before
    read = get_recommendation_run(conn, run_id)
    assert read["valid_until"] == "2025-06-05T09:00:00+00:00"
    assert read["invalidation_reason"] == "test_manual_invalidation"
    assert read["effective"] is False
    assert read["input_invalidated"] is False  # 账户修订尚未前进
    with pytest.raises(DataError) as err_missing:
        invalidate_recommendation_run(conn, "ar_nosuch", reason="x")
    assert err_missing.value.reason == "s4_run_not_found"


def test_recommendation_list_runs_filters_mode_and_reports_revision(portfolio_conn) -> None:
    """列表：按账号过滤；mode 过滤；返回 account_revision。"""
    conn, _settings = portfolio_conn
    env = _env(conn, positions=[])
    created = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    rows = list_recommendation_runs(conn, env["account_id"])
    assert [r["run_id"] for r in rows] == [created["run_id"]]
    assert rows[0]["account_revision"] == int(env["initial_state"]["revision"])
    incremental_only = list_recommendation_runs(conn, env["account_id"], mode="incremental")
    assert [r["run_id"] for r in incremental_only] == [created["run_id"]]
    independent_only = list_recommendation_runs(conn, env["account_id"], mode="independent")
    assert independent_only == []
    with pytest.raises(DataError) as err:
        list_recommendation_runs(conn, env["account_id"], mode="other")
    assert err.value.reason == "s4_mode_unknown"


def test_recommendation_misc_validation_errors(portfolio_conn) -> None:
    """入口校验：未知模式/错误 note/未知账号/快照缺失。"""
    conn, _settings = portfolio_conn
    env = _env(conn, positions=[])
    with pytest.raises(DataError) as err_mode:
        create_recommendation_run(
            conn,
            env["bound"],
            account_id=env["account_id"],
            account_state=env["initial_state"],
            fee_bundle_id=env["fee_bundle_id"],
            mode="unknown-mode",
        )
    assert err_mode.value.reason == "s4_mode_unknown"
    with pytest.raises(DataError) as err_account:
        create_recommendation_run(
            conn,
            env["bound"],
            account_id="acct_missing",
            account_state=env["initial_state"],
            fee_bundle_id=env["fee_bundle_id"],
        )
    assert err_account.value.reason == "s4_account_not_found"
    with pytest.raises(DataError) as err_note:
        create_recommendation_run(
            conn,
            env["bound"],
            account_id=env["account_id"],
            account_state=env["initial_state"],
            fee_bundle_id=env["fee_bundle_id"],
            note="   ",
        )
    assert err_note.value.reason == "s4_note_invalid"
    with pytest.raises(DataError) as err_state:
        create_recommendation_run(
            conn,
            env["bound"],
            account_id=env["account_id"],
            account_state=None,
            fee_bundle_id=env["fee_bundle_id"],
        )
    assert err_state.value.reason == "s4_account_state_required"
    with pytest.raises(DataError) as err_conflict:
        create_recommendation_run(
            conn,
            env["bound"],
            account_id=env["account_id"],
            account_state={"revision": 7, "cash_cents": 100, "positions": []},
            fee_bundle_id=env["fee_bundle_id"],
        )
    assert err_conflict.value.reason == "s3_account_revision_mismatch"


def test_recommendation_retention_buffer_keeps_unselected_holdings(portfolio_conn) -> None:
    """默认保留缓冲：未入选目标的持仓不自动清仓，显式规则才允许退出。"""
    import dataclasses

    conn, _settings = portfolio_conn
    env = _env(
        conn,
        cash_cents=1_000_000,
        positions=[_position("000004.SZ", 40, total_cost_cents=40_000)],
    )
    parameter_bundle_id = str(
        create_rule_bundle(
            conn,
            kind="parameter",
            version="s4-test-retain-1",
            assumption_scope="synthetic_simulation",
            content={
                "bundle_kind": "parameter",
                "target_constraints": {
                    "cash_reserve_cents": 50_000,
                    "per_security_weight_cap_bp": 10_000,
                },
                "rebalance_buffer": {
                    "retain_unselected_holdings": True,
                    "min_trade_notional_cents": 0,
                },
            },
        )["bundle_id"]
    )
    bound = dataclasses.replace(
        _rebind(env, int(env["initial_state"]["revision"]), parameter_bundle_id),
        restricted=False,
        restriction_code=None,
        restriction_detail=None,
    )
    result = create_recommendation_run(
        conn,
        bound,
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
    )
    items = {item["ts_code"] + "/" + item["decision"]: item for item in result["report"]["items"]}
    assert "000004.SZ/sell_proposed" not in items
    kept = items["000004.SZ/sell_blocked"]
    assert kept["restriction_code"] == "retention_buffer_unselected"
    assert kept["quantity_shares"] == 0


def test_recommendation_default_validity_and_bounded_list(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _env(conn, positions=[])
    first = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
        mode="incremental",
    )
    second = create_recommendation_run(
        conn,
        env["bound"],
        account_id=env["account_id"],
        account_state=env["initial_state"],
        fee_bundle_id=env["fee_bundle_id"],
        mode="independent",
    )
    read = get_recommendation_run(conn, first["run_id"])
    assert read["valid_until"] is not None and read["valid_from"] is not None
    assert read["expired"] is False and read["effective"] is True
    # 模拟默认期限到期：只影响读取状态，不改写工件。
    conn.execute(
        "UPDATE analysis_run SET valid_until='2020-01-01T00:00:00+00:00' WHERE run_id=?",
        (first["run_id"],),
    )
    expired = get_recommendation_run(conn, first["run_id"])
    assert expired["expired"] is True and expired["effective"] is False
    assert expired["report"] == read["report"]

    listed = list_recommendation_runs(conn, env["account_id"], limit=1)
    assert len(listed) == 1
    assert listed[0]["run_id"] in {first["run_id"], second["run_id"]}
    page2 = list_recommendation_runs(conn, env["account_id"], limit=1, offset=1)
    assert len(page2) == 1 and page2[0]["run_id"] != listed[0]["run_id"]


def test_recommendation_rejects_advanced_account_with_old_binding(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    env = _env(conn, positions=[])
    _advance_revision(env, delta_cents=100)
    before = conn.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0]
    with pytest.raises(DataError) as err:
        create_recommendation_run(
            conn,
            env["bound"],  # 仍绑定旧修订 1
            account_id=env["account_id"],
            account_state=env["initial_state"],
            fee_bundle_id=env["fee_bundle_id"],
        )
    assert err.value.reason == "s4_account_revision_stale"
    assert conn.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0] == before


def test_analysis_artifact_is_schema_managed_not_runtime_ddl(portfolio_conn) -> None:
    conn, _settings = portfolio_conn
    ensure_analysis_artifact_table(conn)  # 正式 schema 已建表 → 通过
    conn.execute("DROP TABLE analysis_artifact")
    with pytest.raises(DataError) as err:
        ensure_analysis_artifact_table(conn)
    assert err.value.reason == "s4_schema_missing"
