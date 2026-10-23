"""阶段三 S3：理想目标组合（信号/排序/分配，注册表可替换）聚焦测试。

全部使用隔离临时库（``data_settings``/``portfolio_conn``，schema 4，外键开启），
通过阶段二写入/发布/股票池/绑定路径构造数据；手算全部信号/分配数值。
不访问真实数据源、不运行应用、不改阶段二代码。
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3

import pytest

from app.data.chunks import VersionView, publish_chunked_version
from app.data.errors import DataError
from app.data.migration import initialize_database
from app.data.store import write_records
from app.data.universe import (
    PoolRule,
    generate_pool_snapshot,
    load_pool_snapshot,
)
from app.portfolio.allocation import (
    ALLOCATION_BASELINE_NAME,
    ALLOCATION_POLICY_REGISTRY,
    AllocationRequest,
    BaselineEqualWeightCappedAllocation,
    parse_target_constraints,
)
from app.portfolio.db import portfolio_connect
from app.portfolio.ledger import (
    AccountEventRequest,
    create_account,
    get_account_state,
    post_account_event,
)
from app.portfolio.registry import ComponentRegistry
from app.portfolio.rule_bundles import create_rule_bundle, ensure_default_bundles
from app.portfolio.selection import (
    SELECTION_BASELINE_NAME,
    SELECTION_POLICY_REGISTRY,
    BaselineMomentumRankSelection,
)
from app.portfolio.service import (
    RESTRICTION_UNIVERSE_UNVERIFIED,
    DataSelectionContext,
    bind_data_context,
    register_pool_scope_evidence,
)
from app.portfolio.signals import (
    SIGNAL_BASELINE_NAME,
    SIGNAL_CALCULATOR_REGISTRY,
    SIGNAL_MIN_OBSERVATIONS,
    BaselineMomentumTrendCalculator,
)
from app.portfolio.targeting import (
    TARGET_REPORT_VERSION,
    compute_ideal_target,
)

REC_1 = "2025-06-05T01:00:00+00:00"
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
    """确定性工作日近似（周一..周五）；省略日即为有序日历中的观察缺失。"""
    days: list[str] = []
    cursor = dt.date(2024, 9, 9)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y%m%d"))
        cursor += dt.timedelta(days=1)
    return days


def _closes_step(total: int, base: float = 10.0, up: float = 13.0, base_count: int = 65) -> list[float]:
    """前 ``base_count`` 个观察为 base，其后为 up（S1 手算样例接口）。"""
    return [base] * base_count + [up] * (total - base_count)


def _closes_flat(total: int, price: float = 10.0) -> list[float]:
    return [price] * total


def _closes_spike(total: int) -> list[float]:
    """t-5 单点抬升到 16，最后 5 个观察回落到 9：动量 > 0 但 P(t) <= trend。"""
    closes = [10.0] * total
    closes[total - 6] = 16.0
    for index in range(total - 5, total):
        closes[index] = 9.0
    return closes


def _seed_version(
    conn: sqlite3.Connection,
    *,
    days: list[str],
    plans: dict[str, list],
    source_code: str = SRC,
) -> str:
    """发布一个真实数据版本：daily/adj_factor/calendar/security 全走阶段二路径。

    ``plans[code]`` 至多与 ``days`` 等长（zip 截断）：前缀为该观察的收盘价，
    缺尾即缺观察行。注意同一 ``source_code`` 的后续发布会带全量现存行，因此
    需要“截断观察”的独立版本时必须换一个 source_code。
    """
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
    account_id: str = "acct_s3",
) -> dict:
    create_account(conn, account_id, "S3 测试账户")
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


def _position(ts_code: str, shares: int, total_cost_cents: int | None = None) -> dict:
    return {
        "ts_code": ts_code,
        "shares": shares,
        "total_cost_cents": total_cost_cents,
        "sellable_shares": shares,
        "acquire_date": "20250201",
        "locked": False,
    }


def _view(conn: sqlite3.Connection, version_id: str, source_code: str = SRC) -> VersionView:
    return VersionView(conn, version_id, expected_source_code=source_code)


# ------------------------------------------------------------------ 信号


def test_baseline_signal_exact_120_boundary_and_hand_checked_values(portfolio_conn) -> None:
    """恰好 120 观察时手算成立；119 条即排除且显式报告观察数。"""
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN)
    # 合格序列：65x10 + 55x13 → momentum=0.3, trend=(650+715)/120=11.375
    plans = {"000001.SZ": _closes_step(TREND_MIN)}
    version_id = _seed_version(conn, days=days, plans=plans)
    view = _view(conn, version_id)

    ok = BaselineMomentumTrendCalculator().calculate(
        view=view, as_of=days[-1], ts_code="000001.SZ"
    )
    assert ok.ok is True
    assert ok.reason_code is None
    assert ok.observation_count == TREND_MIN == SIGNAL_MIN_OBSERVATIONS
    assert str(ok.momentum) == "0.3"
    assert str(ok.trend) == "11.375"
    assert str(ok.price_t) == "13.0"
    assert ok.price_date_t == days[-1]

    # 119 条观察：独立来源发布，最后一个观察日缺行；as_of 即最后观察日。
    version_short = _seed_version(
        conn, days=days, plans={"000001.SZ": _closes_step(119)}, source_code=SRC + "-short"
    )
    short = BaselineMomentumTrendCalculator().calculate(
        view=_view(conn, version_short, source_code=SRC + "-short"), as_of=days[118], ts_code="000001.SZ"
    )
    assert short.ok is False
    assert short.reason_code == "signal_history_insufficient_observations"
    assert short.observation_count == 119
    assert short.momentum is None and short.trend is None


def test_baseline_signal_counts_observations_with_gaps_no_forward_fill(portfolio_conn) -> None:
    """中间缺 10 行仍凑齐 120 观察时可算；119 条时按观察数（不按日历跨度）排除。"""
    conn, settings = portfolio_conn
    days = _business_days(130)
    step = _closes_step(len(days))                       # 130 个观察
    gapped = list(step)
    for index in range(60, 70):                          # 删 10 个中间观察 → 120 条
        gapped[index] = None
    short = list(step)
    for index in range(0, 11):                           # 删 11 个观察 → 119 条
        short[index] = None

    plans = {"000001.SZ": gapped, "000002.SZ": short}
    version_id = _seed_version(conn, days=days, plans=plans)
    view = _view(conn, version_id)
    calculator = BaselineMomentumTrendCalculator()

    gapped = calculator.calculate(view=view, as_of=days[-1], ts_code="000001.SZ")
    assert gapped.ok is True
    assert gapped.observation_count == TREND_MIN
    # 全序列即窗口：60x10 + 60x13 = 1380；/120 = 11.5；t-5/t-65 均落在有效观察
    assert str(gapped.trend) == "11.5"
    assert str(gapped.momentum) == "0.3"
    assert gapped.price_date_t == days[-1]

    short = calculator.calculate(view=view, as_of=days[-1], ts_code="000002.SZ")
    assert short.ok is False
    assert short.reason_code == "signal_history_insufficient_observations"
    assert short.observation_count == 119
    assert short.momentum is None and short.trend is None


def test_baseline_signal_future_rows_do_not_leak_into_series(portfolio_conn) -> None:
    """as_of 之后有更多行情行也不进入窗口（无前视）：as_of 收口于 120 观察处。"""
    conn, settings = portfolio_conn
    days = _business_days(130)
    closes = _closes_step(len(days))
    version_id = _seed_version(conn, days=days, plans={"000001.SZ": closes})
    as_of = days[119]  # 前 120 个观察里最后一个
    signal = BaselineMomentumTrendCalculator().calculate(
        view=_view(conn, version_id), as_of=as_of, ts_code="000001.SZ"
    )
    assert signal.ok is True
    assert signal.observation_count == TREND_MIN
    assert signal.price_date_t == as_of
    # 截至该时点：65x10 + 55x13 → 与 120 边界手算一致（未来收盘 13 不会前移窗口）
    assert str(signal.trend) == "11.375"
    assert str(signal.momentum) == "0.3"


def test_ranking_momentum_desc_and_exact_tie_breaks_by_code_asc(portfolio_conn) -> None:
    """动量降序；Decimal 完全相同时按代码升序（不得凭前缀/顺序作弊）。"""
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN + 5)
    plans = {
        "000001.SZ": _closes_step(len(days)),                       # 0.3
        "000002.SZ": _closes_step(len(days)),                       # 0.3（平局）
        "000003.SZ": _closes_step(len(days), up=15.0),              # 0.5
    }
    version_id = _seed_version(conn, days=days, plans=plans)
    view = _view(conn, version_id)
    calculator = BaselineMomentumTrendCalculator()
    signals = [
        calculator.calculate(view=view, as_of=days[-1], ts_code=code)
        for code in sorted(plans)
    ]
    selection = BaselineMomentumRankSelection().select(signals)
    assert selection.selected == ("000003.SZ", "000001.SZ", "000002.SZ")
    assert selection.excluded == ()


def test_policy_gates_exclude_by_reason_codes(portfolio_conn) -> None:
    """门槛过滤：momentum<=0 / P(t)<=trend 各给出显式排除原因。"""
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN + 5)
    plans = {
        "000001.SZ": _closes_flat(len(days)),   # 动量 0
        "000002.SZ": _closes_spike(len(days)),  # 动量 0.6 但 P(t)=9 <= trend
    }
    version_id = _seed_version(conn, days=days, plans=plans)
    view = _view(conn, version_id)
    calculator = BaselineMomentumTrendCalculator()
    flat = calculator.calculate(view=view, as_of=days[-1], ts_code="000001.SZ")
    spike = calculator.calculate(view=view, as_of=days[-1], ts_code="000002.SZ")
    assert flat.ok is True and str(flat.momentum) == "0"
    assert spike.ok is True and str(spike.momentum) == "0.6"
    # 手算 trend = (114*10 + 16 + 5*9)/120 = 1201/120 = 10.0083…
    assert str(spike.trend).startswith("10.00833")

    selection = BaselineMomentumRankSelection().select([flat, spike])
    assert selection.selected == ()
    by_code = {item.ts_code: item for item in selection.excluded}
    assert by_code["000001.SZ"].reason_code == "momentum_not_positive"
    assert by_code["000001.SZ"].gate == "policy"
    assert by_code["000002.SZ"].reason_code == "price_not_above_trend"
    assert by_code["000002.SZ"].detail is not None and "9" in by_code["000002.SZ"].detail


# ------------------------------------------------------------------ 分配


def test_allocate_equal_weight_within_cap_hand_checked_reconciliation() -> None:
    """1 只被选：investable=999000, share=999000, 余量 0 → reserve+分配+余量==现金。"""
    policy = BaselineEqualWeightCappedAllocation()
    result = policy.allocate(
        AllocationRequest(
            selected=("000001.SZ",),
            cash_cents=1_000_000,
            bundle_content={
                "bundle_kind": "parameter",
                "target_constraints": {
                    "cash_reserve_cents": 1000,
                    "per_security_weight_cap_bp": 10_000,
                },
            },
            bundle_version="s3-test-1",
        )
    )
    assert result.ok is True and result.restricted is False
    assert result.reserved_cents == 1000
    assert result.investable_cents == 999_000
    targets = result.targets
    assert len(targets) == 1
    assert targets[0].ts_code == "000001.SZ"
    assert targets[0].cap_bound is False
    assert targets[0].weight_bp == 10_000
    assert targets[0].target_value_cents == 999_000
    # 对账恒等：reserve + Σtarget + residual == cash
    assert 1000 + sum(t.target_value_cents for t in targets) + result.final_cash_cents == 1_000_000
    assert result.final_cash_cents == 0
    assert any("per_security_cap_not_binding" in n for n in result.notices)


def test_allocate_cap_bound_five_selected_reconciliation() -> None:
    """5 只被选、上限 1000bp：share=99900、分配合计 499500、余量 499500。"""
    result = BaselineEqualWeightCappedAllocation().allocate(
        AllocationRequest(
            selected=tuple(f"00000{i}.SZ" for i in range(1, 6)),
            cash_cents=1_000_000,
            bundle_content={
                "target_constraints": {
                    "cash_reserve_cents": 1000,
                    "per_security_weight_cap_bp": 1000,
                }
            },
        )
    )
    assert result.ok is True
    assert all(t.weight_bp == 1000 and t.cap_bound for t in result.targets)
    assert all(t.target_value_cents == 99_900 for t in result.targets)
    assert 1000 + 5 * 99_900 + result.final_cash_cents == 1_000_000
    assert result.final_cash_cents == 499_500
    assert any("cap_active_below_equal_weight" in n for n in result.notices)


def test_allocate_fail_closed_paths() -> None:
    """缺约束块/未知字段/换手预算/保留超现金——全部显式受限或拒绝。"""
    policy = BaselineEqualWeightCappedAllocation()

    # 默认参数组合没有 target_constraints 块：hint_only 永不被折算成硬约束。
    restricted = policy.allocate(
        AllocationRequest(
            selected=("000001.SZ",), cash_cents=1_000_000, bundle_content={"target_preferences": {}}
        )
    )
    assert restricted.ok is True and restricted.restricted is True
    assert restricted.reason_code == "target_constraints_not_versioned"
    assert restricted.targets == ()
    assert restricted.final_cash_cents == 1_000_000
    assert any("hint_only" in n for n in restricted.notices)

    with pytest.raises(DataError) as unknown:
        parse_target_constraints(
            {
                "target_constraints": {
                    "cash_reserve_cents": 0,
                    "per_security_weight_cap_bp": 10_000,
                    "unknown_field": 1,
                }
            }
        )
    assert unknown.value.reason == "target_constraints_field_unknown"

    with pytest.raises(DataError) as turnover:
        parse_target_constraints(
            {
                "target_constraints": {
                    "cash_reserve_cents": 0,
                    "per_security_weight_cap_bp": 10_000,
                    "turnover_budget_bp": 500,
                }
            }
        )
    assert turnover.value.reason == "s3_turnover_budget_unimplemented"

    conflict = policy.allocate(
        AllocationRequest(
            selected=("000001.SZ",),
            cash_cents=500,
            bundle_content={
                "target_constraints": {
                    "cash_reserve_cents": 1000,
                    "per_security_weight_cap_bp": 10_000,
                }
            },
        )
    )
    assert conflict.ok is False
    assert conflict.reason_code == "constraint_cash_reserve_exceeds_cash"
    assert conflict.targets == ()
    assert conflict.final_cash_cents == 500  # 什么都不动


def test_allocate_no_selection_leaves_cash_untouched() -> None:
    """无被选候选：现金全留、余量等于现金、不报错误。"""
    result = BaselineEqualWeightCappedAllocation().allocate(
        AllocationRequest(
            selected=(),
            cash_cents=1_000_000,
            bundle_content={
                "target_constraints": {
                    "cash_reserve_cents": 1000,
                    "per_security_weight_cap_bp": 10_000,
                }
            },
        )
    )
    assert result.ok is True and result.targets == ()
    assert result.reserved_cents == 1000
    assert result.final_cash_cents == 1_000_000
    assert any("no_candidates_selected_cash_unchanged" in n for n in result.notices)


# ------------------------------------------------------------------ 注册表


def test_registries_hold_frozen_baselines_and_enforce_names() -> None:
    assert SIGNAL_BASELINE_NAME in SIGNAL_CALCULATOR_REGISTRY.names()
    assert SELECTION_BASELINE_NAME in SELECTION_POLICY_REGISTRY.names()
    assert ALLOCATION_BASELINE_NAME in ALLOCATION_POLICY_REGISTRY.names()
    for registry in (
        SIGNAL_CALCULATOR_REGISTRY,
        SELECTION_POLICY_REGISTRY,
        ALLOCATION_POLICY_REGISTRY,
    ):
        assert registry.names() == sorted(registry.names())
        with pytest.raises(DataError):
            registry.require("no_such_component")
        with pytest.raises(DataError):
            registry.register("Bad Name With Space", object)

    # 同名同实现幂等；同名不同实现即显式冲突。
    registry = ComponentRegistry(kind="Probe")
    class Impl:
        code = "probe"
        version = "1"
    registry.register("probe_impl", Impl)
    assert registry.names() == ["probe_impl"]
    registry.register("probe_impl", Impl)  # 幂等
    class Other:
        code = "probe"
        version = "1"
    with pytest.raises(DataError) as conflict:
        registry.register("probe_impl", Other)
    assert conflict.value.reason == "registry_name_conflict"


# ------------------------------------------------------------------ 编排（端到端）


def test_targeting_end_to_end_candidates_gates_and_allocation(portfolio_conn) -> None:
    """端到端手算：池内资格过滤→信号→门槛排序→版本化约束分配→持仓另估。"""
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN + 5)
    plans = {
        "000001.SZ": _closes_step(len(days)),     # momentum 0.3 合格
        "000004.SZ": _closes_flat(len(days)),     # 动量 0 → 门槛排除
        "000005.SZ": _closes_spike(len(days)),    # P(t) <= trend → 门槛排除
        "000010.SX": [None] * len(days),          # 全部缺价 → 用途资格排除
    }
    version_id = _seed_version(conn, days=days, plans=plans)
    as_of = days[-1]
    pool = _pool(conn, version_id, as_of, ["000001.SZ", "000004.SZ", "000005.SZ", "000010.SX"])
    assert set(pool["members"]) == {"000001.SZ", "000004.SZ", "000005.SZ", "000010.SX"}
    register_pool_scope_evidence(
        conn,
        snapshot_id=str(pool["snapshot_id"]),
        verification_source="test fixture",
        verified_by="test",
        evidence_payload="test evidence",
    )
    bundle_id = ensure_default_bundles(conn)["parameter"]
    account_state = _account(conn)
    bound = _binding(
        conn,
        DataSelectionContext(
            source_code=SRC,
            as_of=as_of,
            purpose="signal_input",
            data_version_id=version_id,
            account_revision=int(account_state["revision"]),
            rule_bundle_id=bundle_id,
            pool_snapshot_id=str(pool["snapshot_id"]),
        ),
    )

    report = compute_ideal_target(conn, bound, account_state=account_state)

    assert report["report_version"] == TARGET_REPORT_VERSION
    assert report["as_of"] == as_of
    assert report["data_version_id"] == version_id
    assert re.fullmatch(r"[0-9a-f]{64}", str(report["input_digest"]))

    # 候选与资格排除逐项保留原因（只有缺价的 000010.SX 在候选阶段排除）
    assert report["candidates"] == ["000001.SZ", "000004.SZ", "000005.SZ"]
    assert len(report["excluded"]) == 1
    assert report["excluded"][0]["code"] == "signal_eligibility_missing_price"
    assert str(report["excluded"][0]["detail"]).startswith("000010.SX")

    # 信号与门槛排序
    signal_by_code = {item["ts_code"]: item for item in report["signals"]}
    assert signal_by_code["000001.SZ"]["momentum"] == "0.3"
    # 125 条观察 → 窗口为最后 120 条：60x10 + 60x13 = 11.5
    assert signal_by_code["000001.SZ"]["trend"] == "11.5"
    selection = report["selection"]
    assert selection["selected"] == ["000001.SZ"]
    gate_reasons = {item["ts_code"]: item["reason_code"] for item in selection["excluded"]}
    assert gate_reasons["000004.SZ"] == "momentum_not_positive"
    assert gate_reasons["000005.SZ"] == "price_not_above_trend"

    # 默认参数组合没有版本化约束块：分配受限、不动现金
    allocation = report["allocation"]
    assert allocation["ok"] is True
    assert allocation["restricted"] is True
    assert allocation["reason_code"] == "target_constraints_not_versioned"
    assert allocation["targets"] == []
    assert allocation["cash_cents"] == 1_000_000
    assert allocation["final_cash_cents"] == 1_000_000

    # 绑定时域受限（股票池证据两旗标未决）必须原样入报告
    assert report["restricted"] is True
    assert report["restriction_code"] == RESTRICTION_UNIVERSE_UNVERIFIED
    restriction_notices = [
        n for n in report["notices"] if str(n["code"]).startswith("sync_context_restricted_")
    ]
    assert restriction_notices, "受限上下文必须写入通知"
    assert report["disclaimers"] == [
        "informational_analysis_only_not_investment_advice",
        "ideal_target_is_not_an_executable_rebalancing_plan",
        "no_optimality_or_profitability_claim",
    ]
    # 组件与注册表快照
    assert report["components"]["signal"]["name"] == SIGNAL_BASELINE_NAME
    assert report["components"]["selection"]["name"] == SELECTION_BASELINE_NAME
    assert report["components"]["allocation"]["name"] == ALLOCATION_BASELINE_NAME
    assert report["components"]["registries"]["signal_calculators"] == [SIGNAL_BASELINE_NAME]
    assert report["components"]["registries"]["selection_policies"] == [SELECTION_BASELINE_NAME]
    assert report["components"]["registries"]["allocation_policies"] == [ALLOCATION_BASELINE_NAME]

    # 报告确定性：同一输入两次计算摘要一致
    again = compute_ideal_target(conn, bound, account_state=account_state)
    assert again["input_digest"] == report["input_digest"]


def test_targeting_with_versioned_constraints_produces_targets(portfolio_conn) -> None:
    """版本化约束时：现金保留 + 目标金额 + 余量恒等成立，权重不超过上限。"""
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN + 5)
    plans = {"000001.SZ": _closes_step(len(days))}
    version_id = _seed_version(conn, days=days, plans=plans)
    as_of = days[-1]
    pool = _pool(conn, version_id, as_of, ["000001.SZ"])
    register_pool_scope_evidence(
        conn,
        snapshot_id=str(pool["snapshot_id"]),
        verification_source="test fixture",
        verified_by="test",
        evidence_payload="test evidence",
    )
    bundle = create_rule_bundle(
        conn,
        kind="parameter",
        version="s3-test-alloc-10000",
        assumption_scope="synthetic_simulation",
        content={
            "bundle_kind": "parameter",
            "target_constraints": {
                "cash_reserve_cents": 1000,
                "per_security_weight_cap_bp": 10_000,
            },
        },
    )
    account_state = _account(conn)
    bound = _binding(
        conn,
        DataSelectionContext(
            source_code=SRC,
            as_of=as_of,
            purpose="signal_input",
            data_version_id=version_id,
            account_revision=int(account_state["revision"]),
            rule_bundle_id=str(bundle["bundle_id"]),
            pool_snapshot_id=str(pool["snapshot_id"]),
        ),
    )
    report = compute_ideal_target(conn, bound, account_state=account_state)
    allocation = report["allocation"]
    assert allocation["restricted"] is False
    assert allocation["reason_code"] is None
    assert allocation["reserved_cents"] == 1000
    targets = allocation["targets"]
    assert len(targets) == 1
    assert targets[0]["ts_code"] == "000001.SZ"
    assert targets[0]["target_value_cents"] == 999_000
    assert targets[0]["cap_bound"] is False
    # 对账恒等逐分核对
    allocated = sum(item["target_value_cents"] for item in targets)
    assert (
        allocation["reserved_cents"] + allocated + allocation["final_cash_cents"]
        == allocation["cash_cents"]
    )
    assert allocation["constraints_source_version"] == "s3-test-alloc-10000"


def test_targeting_holdings_outside_selected_are_assessed_separately(portfolio_conn) -> None:
    """持仓不在目标内：另估、不可卖、不自动清仓、估值口径显式标注。"""
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN + 5)
    plans = {
        "000001.SZ": _closes_step(len(days)),   # 被选
        "000004.SZ": _closes_flat(len(days)),   # 池内但门槛排除；有持仓
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
    bundle_id = ensure_default_bundles(conn)["parameter"]
    account_state = _account(
        conn,
        positions=[
            _position("000001.SZ", 60, total_cost_cents=60_000),
            _position("000004.SZ", 40, total_cost_cents=40_000),
            _position("600999.SH", 10, total_cost_cents=None),
        ],
    )
    assert int(account_state["cash_cents"]) == 1_000_000
    bound = _binding(
        conn,
        DataSelectionContext(
            source_code=SRC,
            as_of=as_of,
            purpose="signal_input",
            data_version_id=version_id,
            account_revision=int(account_state["revision"]),
            rule_bundle_id=bundle_id,
            pool_snapshot_id=str(pool["snapshot_id"]),
        ),
    )
    report = compute_ideal_target(conn, bound, account_state=account_state)
    assert report["selection"]["selected"] == ["000001.SZ"]
    assessment = {item["ts_code"]: item for item in report["holdings_assessment"]}
    # 在目标内：只登记事实，不当作可卖
    assert assessment["000001.SZ"]["in_selected_target"] is True
    assert assessment["000001.SZ"]["separate_assessment"] is False
    assert assessment["000001.SZ"]["assumed_sellable"] is False
    # 池内但被门槛排除：另估，估值 = 40 股 x 10.00 元 x 100（元→分） = 40000 分
    assert assessment["000004.SZ"]["separate_assessment"] is True
    assert assessment["000004.SZ"]["assumed_sellable"] is False
    assert assessment["000004.SZ"]["auto_liquidated"] is False
    assert assessment["000004.SZ"]["market_value_cents"] == 40_000
    assert assessment["000004.SZ"]["value_basis"] == (
        "valuation_reference_floating_point_storage_rounded_half_up_cents"
    )
    assert assessment["000004.SZ"]["valuation_price_date"] == as_of
    # 不在版本/池内：另估且显式“缺价”，不得静默取 0 或编造
    assert assessment["600999.SH"]["separate_assessment"] is True
    assert assessment["600999.SH"]["market_value_cents"] is None
    assert assessment["600999.SH"]["value_basis"] == "valuation_reference_missing_price"
    notices = [n["code"] for n in report["notices"]]
    assert "holdings_outside_selected_target_are_assessed_separately" in notices


def test_targeting_requires_pool_account_and_bundle(portfolio_conn) -> None:
    """缺股票池/缺账户状态/缺规则组合——逐一显式拒绝（fail-closed）。"""
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN + 5)
    version_id = _seed_version(conn, days=days, plans={"000001.SZ": _closes_step(len(days))})
    as_of = days[-1]
    bundle_id = ensure_default_bundles(conn)["parameter"]
    account_state = _account(conn)

    no_pool = _binding(
        conn,
        DataSelectionContext(
            source_code=SRC,
            as_of=as_of,
            purpose="signal_input",
            data_version_id=version_id,
            account_revision=int(account_state["revision"]),
            rule_bundle_id=bundle_id,
        ),
    )
    with pytest.raises(DataError) as pool_error:
        compute_ideal_target(conn, no_pool, account_state=account_state)
    assert pool_error.value.reason == "s3_candidate_pool_not_bound"

    pool = _pool(conn, version_id, as_of, ["000001.SZ"])
    bound = _binding(
        conn,
        DataSelectionContext(
            source_code=SRC,
            as_of=as_of,
            purpose="signal_input",
            data_version_id=version_id,
            account_revision=int(account_state["revision"]),
            rule_bundle_id=bundle_id,
            pool_snapshot_id=str(pool["snapshot_id"]),
        ),
    )
    with pytest.raises(DataError) as state_error:
        compute_ideal_target(conn, bound, account_state=None)
    assert state_error.value.reason == "s3_account_state_required"


def test_targeting_rejects_purpose_other_than_signal_input(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN + 5)
    version_id = _seed_version(conn, days=days, plans={"000001.SZ": _closes_step(len(days))})
    as_of = days[-1]
    bundle_id = ensure_default_bundles(conn)["parameter"]
    account_state = _account(conn)
    bound = _binding(
        conn,
        DataSelectionContext(
            source_code=SRC,
            as_of=as_of,
            purpose="valuation_reference",
            data_version_id=version_id,
            account_revision=int(account_state["revision"]),
            rule_bundle_id=bundle_id,
        ),
    )
    with pytest.raises(DataError) as purpose_error:
        compute_ideal_target(conn, bound, account_state=account_state)
    assert purpose_error.value.reason == "s3_requires_signal_input_purpose"


def test_targeting_pool_version_mismatch_is_rejected(portfolio_conn) -> None:
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN + 5)
    # 第二个种子用不同来源发布，避免内容寻址得到同一个 version_id
    version_a = _seed_version(conn, days=days, plans={"000001.SZ": _closes_step(len(days))})
    version_b = _seed_version(
        conn, days=days, plans={"000001.SZ": _closes_step(len(days))}, source_code=SRC + "-b"
    )
    as_of = days[-1]
    bundle_id = ensure_default_bundles(conn)["parameter"]
    account_state = _account(conn)
    pool = _pool(conn, version_b, as_of, ["000001.SZ"], source_code=SRC + "-b")
    # 池基于版本 B，而上下文绑定版本 A → 绑定时即明确拒绝，不得混用
    with pytest.raises(DataError) as mismatch:
        _binding(
            conn,
            DataSelectionContext(
                source_code=SRC,
                as_of=as_of,
                purpose="signal_input",
                data_version_id=version_a,
                account_revision=int(account_state["revision"]),
                rule_bundle_id=bundle_id,
                pool_snapshot_id=str(pool["snapshot_id"]),
            ),
        )
    assert mismatch.value.reason == "pool_snapshot_version_mismatch"


def test_targeting_account_revision_match_mismatch_invalid(portfolio_conn) -> None:
    """账户修订必须与绑定上下文一致：一致出报告，不一致/缺失/畸形显式拒绝。"""
    conn, settings = portfolio_conn
    days = _business_days(TREND_MIN + 5)
    version_id = _seed_version(conn, days=days, plans={"000001.SZ": _closes_step(len(days))})
    as_of = days[-1]
    pool = _pool(conn, version_id, as_of, ["000001.SZ"])
    register_pool_scope_evidence(
        conn,
        snapshot_id=str(pool["snapshot_id"]),
        verification_source="test fixture",
        verified_by="test",
        evidence_payload="test evidence",
    )
    bundle_id = ensure_default_bundles(conn)["parameter"]
    account_state = _account(conn)
    assert int(account_state["revision"]) >= 1

    def _bound() -> object:
        return _binding(
            conn,
            DataSelectionContext(
                source_code=SRC,
                as_of=as_of,
                purpose="signal_input",
                data_version_id=version_id,
                account_revision=int(account_state["revision"]),
                rule_bundle_id=bundle_id,
                pool_snapshot_id=str(pool["snapshot_id"]),
            ),
        )

    # 一致：出报告，报告与输入摘要均绑定到该修订
    bound = _bound()
    report = compute_ideal_target(conn, bound, account_state=account_state)
    assert report["account_revision"] == account_state["revision"]
    assert re.fullmatch(r"[0-9a-f]{64}", str(report["input_digest"]))
    again = compute_ideal_target(conn, bound, account_state=account_state)
    assert again["input_digest"] == report["input_digest"]

    # 不一致：账本修订已前进，而上下文仍冻结旧修订 → 计算前拒绝
    stale_state = dict(account_state)
    stale_state["revision"] = int(account_state["revision"]) + 1
    with pytest.raises(DataError) as mismatch:
        compute_ideal_target(conn, bound, account_state=stale_state)
    assert mismatch.value.reason == "s3_account_revision_mismatch"

    # 反向不一致：上下文冻结了一个更高的修订
    high_context = _binding(
        conn,
        DataSelectionContext(
            source_code=SRC,
            as_of=as_of,
            purpose="signal_input",
            data_version_id=version_id,
            account_revision=int(account_state["revision"]) + 5,
            rule_bundle_id=bundle_id,
            pool_snapshot_id=str(pool["snapshot_id"]),
        ),
    )
    with pytest.raises(DataError) as high_mismatch:
        compute_ideal_target(conn, high_context, account_state=account_state)
    assert high_mismatch.value.reason == "s3_account_revision_mismatch"

    # 缺失/畸形修订：布尔、0、负数、非整数一律拒绝
    for bogus in (None, True, 0, -1, "1", 1.0):
        broken = dict(account_state)
        broken.pop("revision", None) if bogus is None else broken.update(revision=bogus)
        with pytest.raises(DataError) as invalid:
            compute_ideal_target(conn, bound, account_state=broken)
        assert invalid.value.reason == "s3_account_revision_invalid"
