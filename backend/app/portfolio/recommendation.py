"""阶段三 S4：建议编排（增量默认 + 同预算独立目标，含成本/快照/幂等）。

编排契约（本派工冻结口径）：

- 输入与 S3 理想目标一致：已绑定的 ``BoundDataContext``（purpose=signal_input，
  VersionView 固定数据版本）+ ``ledger.get_account_state`` 的账户状态 + 显式
  ``account_id`` + 版本化规则/费用组合。账户修订必须与绑定上下文一致；
  不一致/缺失/畸形在计算前拒绝（稳定原因码 ``s3_account_revision_mismatch`` /
  ``s3_account_revision_invalid``）。
- 两种模式共享同一冻结输入与总预算基础（现金-版本化保留）：``incremental``
  是默认渐进调整（从当前持仓向理想目标过渡，按 delta 下单）；``independent``
  是可选的同预算独立目标（目标独立于当前持仓给出；当前持仓只做显式转换
  评估——绝不假装"自由清仓"；锁定/卖不出/无估值的持仓保持可见并带限制码）。
- 假设性卖出变现默认不计入买入预算（``execution_assumptions.same_day_sale_proceeds_reusable``
  在默认参数组合是保守 unverified/false）；只有参数组合显式核验为
  ``verified`` 才允许正交计入，且逐项标注
  ``contingent_on_same_day_sale_proceeds`` 并显式声明变现未发生。
- 费用只来自版本化费用组合（默认合成模拟假设，绝不冒充真实市况费率）；
  组件缺失/字段不完整/charging 不匹配一律 fail-closed（``s4_fee_evidence_missing``）。
- 全部输入/输出以不可变工件持久化（``analysis_artifact``）；``analysis_run.
  input_sha256`` 全局唯一实现同输入幂等（复用既有 run，不重算、不重复写库）；
  输入变化（如账户修订前进而上下文重新绑定）产生新的不可变 run。失效只写
  ``valid_until``/``invalidation_reason``，不改写工件。
- 读取路径（``get_recommendation_run`` 等只读）只反序列化摘要已核验的持久
  工件，绝不重新计算；账户修订已前进时在读取结果上标注 ``input_invalidated``。
- 不可行性显式可见（``buy_blocked``/``sell_blocked``/``insufficient_funds`` 等），
  绝不隐藏或自动改写目标；剩余现金是对账合法结果，恒等式可逐分核对。
- 本模块只读写阶段三 S4 自有持久化表；不读写真实市场数据，不做 API/UI，
  也不是 S5 模拟器。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from ..data.chunks import VersionView, version_scope_codes
from ..data.digests import canonical_json_bytes, sha256_of_object
from ..data.errors import DataError
from ..data.prices import raw_execution_price, valuation_price
from ..data.timeutil import utc_now_iso
from ..data.universe import load_pool_snapshot
from .ledger import snapshot_id_for
from .references import ReferenceTarget, register_reference_holds
from .rule_bundles import load_rule_bundle
from .targeting import compute_ideal_target

__all__ = [
    "ANALYSIS_APPLICATION_VERSION",
    "ANALYSIS_MODES",
    "ensure_analysis_artifact_table",
    "create_recommendation_run",
    "get_recommendation_run",
    "list_recommendation_runs",
    "invalidate_recommendation_run",
]

ANALYSIS_APPLICATION_VERSION = "s4-recommendation-2.0"

ANALYSIS_MODES = ("incremental", "independent")

DEFAULT_RECOMMENDATION_VALIDITY_HOURS = 24
MAX_RECOMMENDATION_VALIDITY_HOURS = 24 * 7
DEFAULT_REBALANCE_BUFFER: dict[str, Any] = {
    "retain_unselected_holdings": True,
    "min_trade_notional_cents": 0,
    "basis": "synthetic_conservative_retention_buffer",
}

_NOTICE_SYNTHETIC_FEES = (
    "fees_and_costs_are_synthetic_simulation_assumptions_never_real_market_or_broker_costs"
)
_NOTICE_CONTINGENT_ONLY = (
    "hypothetical_sale_proceeds_never_fund_buys_unless_a_buy_is_explicitly_labeled"
    "_contingent_on_same_day_sale_proceeds"
)
_NOTICE_NO_FREE_LIQUIDATION = (
    "converting_holdings_costs_estimated_fees_and_may_be_blocked_by_locks_or_missing_prices"
    "_liquidation_is_never_assumed_free"
)
_NOTICE_MANUAL_CONFIRMATION = (
    "recommendations_are_informational_and_require_manual_confirmation_never_executable"
    "_without_future_explicit_stages"
)
_NOTICE_INFEASIBILITY_VISIBLE = (
    "infeasible_or_reduced_plans_are_reported_verbatim_never_silently_adjusted"
)

_KNOWN_FEE_COMPONENTS = {
    "commission": "per_side",
    "stamp_duty_sell": "sell_only",
    "transfer_fee": "both_sides",
    "slippage": "adverse_direction_model",
}
_FEE_CODES_EXCLUDING_SLIPPAGE = ("commission", "stamp_duty_sell", "transfer_fee")


def ensure_analysis_artifact_table(conn) -> None:
    """校验 S4 工件表由正式 schema 迁移建立；不在运行时执行 DDL。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_artifact'"
    ).fetchone()
    if row is None:
        raise DataError(
            "s4_schema_missing",
            "analysis_artifact 表不存在；必须先执行正式 schema 迁移，"
            "不得用运行时 CREATE TABLE 绕过",
        )


# ------------------------------------------------------------------ 入口校验


def _positions(account_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = account_state.get("positions") or []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise DataError("s4_position_invalid", f"positions 必须是序列: {type(raw)!r}")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise DataError("s4_position_invalid", f"positions 元素必须是映射: {item!r}")
        ts_code = item.get("ts_code")
        if not isinstance(ts_code, str) or not ts_code.strip():
            raise DataError("s4_position_invalid", f"position 缺少有效 ts_code: {item!r}")
        if ts_code in seen:
            raise DataError("s4_position_duplicate", f"同一 ts_code 出现多个持仓行: {ts_code}")
        seen.add(ts_code)
        shares = item.get("shares")
        if isinstance(shares, bool) or not isinstance(shares, int) or shares < 0:
            raise DataError(
                "s4_position_invalid", f"{ts_code} 的 shares 必须是非负整数: {shares!r}"
            )
        sellable = item.get("sellable_shares")
        if sellable is not None and (
            isinstance(sellable, bool) or not isinstance(sellable, int) or sellable < 0
        ):
            raise DataError(
                "s4_position_invalid",
                f"{ts_code} 的 sellable_shares 必须是非负整数或 None: {sellable!r}",
            )
        locked = item.get("locked")
        if isinstance(locked, bool):
            locked_value = locked
        elif isinstance(locked, int) and locked in (0, 1):
            locked_value = bool(locked)
        else:
            raise DataError(
                "s4_position_invalid",
                f"{ts_code} 的 locked 必须是 0/1 或 True/False: {locked!r}",
            )
        normalized.append(
            {
                "ts_code": ts_code,
                "shares": int(shares),
                "sellable_shares": None if sellable is None else int(sellable),
                "locked": locked_value,
            }
        )
    normalized.sort(key=lambda item: item["ts_code"])
    return normalized


def _validate_entry(conn, context: Any, account_state: Mapping[str, Any]) -> dict[str, Any]:
    """与 S3 相同语义的入口一眼判定；全部拒绝必须发生在任何计算/写库之前。"""
    if context is None:
        raise DataError("s4_context_required", "必须提供已绑定的数据上下文")
    if getattr(context, "view", None) is None:
        raise DataError(
            "s4_context_requires_fixed_version_view",
            "绑定上下文必须可提供固定数据版本的 VersionView；缺失即拒绝",
        )
    view: VersionView = context.view
    if context.context.purpose != "signal_input":
        raise DataError(
            "s4_requires_signal_input_purpose",
            f"建议编排只接受 purpose=signal_input 的绑定上下文，当前 {context.context.purpose!r}",
        )
    if account_state is None:
        raise DataError(
            "s4_account_state_required",
            "必须显式提供账户当前状态（来自 get_account_state）",
        )
    bound_revision = context.context.account_revision
    state_revision = account_state.get("revision")
    if isinstance(state_revision, bool) or not isinstance(state_revision, int) or state_revision < 1:
        raise DataError(
            "s3_account_revision_invalid",
            f"账户状态缺少有效修订号（revision={state_revision!r}）；"
            "建议必须绑定与上下文一致的账户修订",
        )
    if state_revision != bound_revision:
        raise DataError(
            "s3_account_revision_mismatch",
            f"账户状态修订 {state_revision} 与绑定上下文账户修订 {bound_revision} 不一致；"
            "拒绝编排建议",
        )
    cash_cents = account_state.get("cash_cents")
    if isinstance(cash_cents, bool) or not isinstance(cash_cents, int) or cash_cents < 0:
        raise DataError(
            "s4_account_cash_invalid",
            f"cash_cents 必须是非负整数分（未知即拒绝，不用 0 冒充）: {cash_cents!r}",
        )
    pool_snapshot_id = context.context.pool_snapshot_id
    if pool_snapshot_id is None:
        raise DataError(
            "s4_candidate_pool_not_bound",
            "signal_input 绑定必须显式携带合格股票池快照；无核验范围不得编排候选",
        )
    pool = load_pool_snapshot(conn, pool_snapshot_id)
    if pool is None:
        raise DataError("pool_snapshot_not_found", f"股票池快照 {pool_snapshot_id} 不存在")
    if pool["data_version_id"] != context.context.data_version_id:
        raise DataError(
            "pool_snapshot_version_mismatch",
            f"股票池快照 {pool_snapshot_id} 基于 {pool['data_version_id']}，"
            f"与上下文 {context.context.data_version_id} 不一致；拒绝以此编排候选",
        )
    return {
        "view": view,
        "as_of": context.context.as_of,
        "source_code": context.context.source_code,
        "data_version_id": context.context.data_version_id,
        "pool_snapshot_id": pool_snapshot_id,
        "bound_revision": int(state_revision),
        "cash_cents": int(cash_cents),
        "positions": _positions(account_state),
        "binding_restricted": bool(context.restricted),
        "binding_restriction_code": context.restriction_code,
        "binding_restriction_detail": context.restriction_detail,
        "rule_bundle": context.rule_bundle,
    }


# ------------------------------------------------------------------ 费用


def _fee_schedule(fee_bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """解析并校验费用组合；任何缺口都 fail-closed，不发明费率。"""
    content = fee_bundle.get("content")
    if not isinstance(content, Mapping):
        raise DataError("s4_fee_evidence_missing", f"费用组合内容不是映射: {type(content)!r}")
    components = content.get("components")
    if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
        raise DataError("s4_fee_evidence_missing", f"费用组合缺少 components: {components!r}")
    schedule: dict[str, dict[str, Any]] = {}
    for component in components:
        if not isinstance(component, Mapping):
            raise DataError("s4_fee_evidence_missing", f"费率组件必须是映射: {component!r}")
        code = component.get("code")
        if code not in _KNOWN_FEE_COMPONENTS:
            raise DataError(
                "s4_fee_evidence_missing",
                f"费用组合含未登记组件（缺证据 fail-closed，不发明费率）: {code!r}",
            )
        code_text = str(code)
        if code_text in schedule:
            raise DataError("s4_fee_evidence_missing", f"费用组件重复登记: {code_text}")
        charging = component.get("charging")
        if charging != _KNOWN_FEE_COMPONENTS[code_text]:
            raise DataError(
                "s4_fee_evidence_missing",
                f"费用组件 {code_text} 的 charging={charging!r} 与登记口径 "
                f"{_KNOWN_FEE_COMPONENTS[code_text]!r} 不一致",
            )
        rate = component.get("rate_bp")
        if isinstance(rate, bool) or not isinstance(rate, int) or not (0 <= rate <= 10_000):
            raise DataError(
                "s4_fee_evidence_missing",
                f"费用组件 {code_text} 的 rate_bp 必须是 0..10000 整数: {rate!r}",
            )
        entry: dict[str, Any] = {"rate_bp": int(rate)}
        minimum = component.get("minimum_cents")
        if minimum is not None:
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
                raise DataError(
                    "s4_fee_evidence_missing",
                    f"费用组件 {code_text} 的 minimum_cents 必须是非负整数: {minimum!r}",
                )
            entry["minimum_cents"] = int(minimum)
        schedule[code_text] = entry
    for code in _KNOWN_FEE_COMPONENTS:
        if code not in schedule:
            raise DataError(
                "s4_fee_evidence_missing",
                f"费用组合缺少登记组件 {code}；缺证据 fail-closed，不发明费率",
            )
    return schedule


def _order_fees(
    schedule: Mapping[str, Mapping[str, Any]], side: str, notional_cents: int
) -> dict[str, Any]:
    """单笔订单（单侧）费用/滑点估计：全部合成假设，仅用于保守估计。"""
    assert side in ("buy", "sell")
    cents = Decimal(notional_cents)

    def _bp(component: str) -> int:
        return int(
            (cents * Decimal(schedule[component]["rate_bp"]) / Decimal(10_000)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )

    commission = max(_bp("commission"), int(schedule["commission"].get("minimum_cents") or 0))
    components: dict[str, int] = {"commission_cents": commission}
    if side == "sell":
        components["stamp_duty_sell_cents"] = _bp("stamp_duty_sell")
    else:
        components["stamp_duty_sell_cents"] = 0
    components["transfer_fee_cents"] = _bp("transfer_fee")
    fee_cents = sum(components[name + "_cents"] for name in _FEE_CODES_EXCLUDING_SLIPPAGE)
    return {
        "fee_cents": fee_cents,
        "slippage_cents": _bp("slippage"),
        "components": components,
    }


# ------------------------------------------------------------------ 价格与持仓


def _reference_price_cents(conn, ctx: Mapping[str, Any], view: VersionView, code: str) -> int | None:
    """可成交参考价（as_of 的未复权收盘，元→分，HALF_UP）；缺失返回 None。"""
    pricing = raw_execution_price(
        conn,
        source_code=ctx["source_code"],
        ts_code=code,
        trade_date=ctx["as_of"],
        view=view,
    )
    if pricing.reason_code != "ok" or pricing.price is None:
        return None
    return int(
        (Decimal(str(pricing.price)) * Decimal(100)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _valuation_market_value_cents(
    conn, ctx: Mapping[str, Any], view: VersionView, code: str, shares: int
) -> int | None:
    """持仓估值（估值参考价，元→分，HALF_UP）；缺价返回 None，绝不填 0。"""
    valuation = valuation_price(
        conn,
        source_code=ctx["source_code"],
        ts_code=code,
        business_date=ctx["as_of"],
        as_of=ctx["as_of"],
        view=view,
    )
    if valuation.price is None:
        return None
    return int(
        (
            Decimal(str(valuation.price)) * Decimal(shares) * Decimal(100)
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def _sellable_shares(pos: Mapping[str, Any]) -> int:
    held = int(pos["shares"])
    sellable = pos["sellable_shares"]
    return held if sellable is None else min(held, int(sellable))


# ------------------------------------------------------------------ 持仓评估


def _holdings_assessment_items(
    conn, ctx: Mapping[str, Any], view: VersionView, positions: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """全部持仓的另估信息（S3 契约：不是可卖、不自动清仓）。"""
    version_codes = set(version_scope_codes(conn, ctx["data_version_id"]))
    items: list[dict[str, Any]] = []
    missing: list[str] = []
    total = 0
    complete = True
    for pos in positions:
        code = str(pos["ts_code"])
        shares = int(pos["shares"])
        if shares == 0:
            continue
        market_value = _valuation_market_value_cents(conn, ctx, view, code, shares)
        if market_value is None:
            missing.append(code)
            complete = False
        else:
            total += market_value
        reasons: list[str] = []
        restriction_code = None
        if code not in version_codes:
            restriction_code = "holdings_unsupported_or_unvalued"
            reasons.append("持仓不在数据版本范围内：不按版本行情处理")
        if market_value is None:
            reasons.append("缺估值价（不填 0）")
        if pos["locked"]:
            reasons.append("存在锁定持仓")
        if pos["sellable_shares"] is None:
            reasons.append("可卖数量未知")
        items.append(
            {
                "ts_code": code,
                "decision": "assessment_only",
                "quantity_shares": shares,
                "weight_bp": None,
                "restriction_code": restriction_code,
                "reason": "；".join(reasons) or "估值参考；不视为可卖",
                "market_value_cents": market_value,
                "sellable_shares": pos["sellable_shares"],
                "locked": bool(pos["locked"]),
                "cost_detail": None,
            }
        )
    estimation = {
        "holdings_value_cents": total if complete else None,
        "completeness": "complete" if complete else "partial_unknown",
        "missing_valuation_codes": missing,
        "basis": "valuation_reference_close_as_of_yuan_to_cents_round_half_up",
    }
    return items, estimation


# ------------------------------------------------------------------ 卖出候选（incremental）


def _sell_items(
    conn,
    ctx: Mapping[str, Any],
    view: VersionView,
    positions: Sequence[Mapping[str, Any]],
    target_shares_by_code: Mapping[str, int | None],
    sizing_unresolved_codes: set[str],
    schedule: Mapping[str, Mapping[str, Any]],
    buffer_rules: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """目标外清仓 + 目标内减仓；锁定/可卖未知/无价 fail-closed 阻断。

    卖出变现全部是假设性估计（contingent，含滑点扣减），只返回其逐项
    供报告显式标注；绝不自动清仓锁定或不可核实的持仓。
    """
    version_codes = set(version_scope_codes(conn, ctx["data_version_id"]))
    items: list[dict[str, Any]] = []
    for pos in positions:
        code = str(pos["ts_code"])
        held = int(pos["shares"])
        if held == 0:
            continue
        if code in sizing_unresolved_codes:
            # 目标内但无法定价：目标份额未知，既不买入也不卖出，只保留持仓评估。
            continue
        target_shares = target_shares_by_code.get(code)
        if target_shares is None and not (
            isinstance(buffer_rules, Mapping)
            and buffer_rules.get("retain_unselected_holdings") is False
        ):
            # 保留缓冲：未入选目标不等于应清仓。只有版本化规则显式允许退出时
            # 才产生卖出建议；否则保留并显式说明，避免无意义全量换股。
            items.append(
                {
                    "ts_code": code,
                    "decision": "sell_blocked",
                    "quantity_shares": 0,
                    "requested_shares": 0,
                    "weight_bp": None,
                    "restriction_code": "retention_buffer_unselected",
                    "reason": "未入选目标但受版本化保留缓冲保护；不自动清仓，需人工决定",
                    "cost_detail": None,
                }
            )
            continue
        delta = (
            -held
            if target_shares is None
            else int(target_shares) - held
        )
        if delta >= 0:
            continue
        want = -delta
        restriction_code = None
        reasons: list[str] = []
        if pos["locked"]:
            restriction_code = "position_locked"
            reasons.append("锁定持仓绝无自动清仓或卖出的假设；转换需人工核证")
        elif pos["sellable_shares"] is None:
            restriction_code = "position_sellable_unknown"
            reasons.append("可卖数量未知：fail-closed 不建议卖出")
        elif code not in version_codes:
            restriction_code = "holdings_unsupported_or_unvalued"
            reasons.append("持仓不在数据版本范围内：不能按版本行情卖出")
        sell_cap = _sellable_shares(pos)
        sell_shares = min(want, sell_cap)
        price_cents = _reference_price_cents(conn, ctx, view, code)
        if price_cents is None and restriction_code is None:
            restriction_code = "no_reference_price"
            reasons.append("缺少可成交参考价：fail-closed 不下单")
        if restriction_code is not None:
            items.append(
                {
                    "ts_code": code,
                    "decision": "sell_blocked",
                    "quantity_shares": 0,
                    "requested_shares": want,
                    "weight_bp": None,
                    "restriction_code": restriction_code,
                    "reason": "；".join(reasons),
                    "cost_detail": None,
                }
            )
            continue
        if sell_shares == 0:
            items.append(
                {
                    "ts_code": code,
                    "decision": "sell_blocked",
                    "quantity_shares": 0,
                    "requested_shares": want,
                    "weight_bp": None,
                    "restriction_code": "sellable_zero_shares",
                    "reason": "可卖数量为 0：不下单",
                    "cost_detail": None,
                }
            )
            continue
        min_trade_notional = 0
        if isinstance(buffer_rules, Mapping):
            raw_min = buffer_rules.get("min_trade_notional_cents")
            if (
                isinstance(raw_min, int)
                and not isinstance(raw_min, bool)
                and raw_min >= 0
            ):
                min_trade_notional = int(raw_min)
        if min_trade_notional > 0 and sell_shares * int(price_cents) < min_trade_notional:
            items.append(
                {
                    "ts_code": code,
                    "decision": "sell_blocked",
                    "quantity_shares": 0,
                    "requested_shares": want,
                    "weight_bp": None,
                    "restriction_code": "below_min_trade_notional",
                    "reason": "拟卖出名义额低于版本化最小调整门槛；保持不动",
                    "cost_detail": None,
                }
            )
            continue
        notional = sell_shares * price_cents
        fees = _order_fees(schedule, "sell", notional)
        contingent = notional - fees["fee_cents"] - fees["slippage_cents"]
        shortfall = want - sell_shares
        items.append(
            {
                "ts_code": code,
                "decision": "sell_proposed",
                "quantity_shares": sell_shares,
                "requested_shares": want,
                "weight_bp": None,
                "restriction_code": (
                    "sell_constrained_by_sellable_shares"
                    if shortfall > 0
                    else None
                ),
                "reason": (
                    "向理想目标过渡的减持建议；全部为合成模拟假设，无任何真实市场性质"
                    if shortfall == 0
                    else "受可卖数量限制的减仓建议；剩余缺口显式保留"
                ),
                "reference_price_cents": price_cents,
                "cost_detail": {
                    "notional_cents": notional,
                    "fee_cents": fees["fee_cents"],
                    "slippage_cents": fees["slippage_cents"],
                    "contingent_estimated_proceeds_cents": contingent,
                },
            }
        )
    return items


# ------------------------------------------------------------------ 独立模式转换评估


def _conversion_assessment_items(
    conn,
    ctx: Mapping[str, Any],
    view: VersionView,
    positions: Sequence[Mapping[str, Any]],
    schedule: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """独立模式：不提议清仓，只显式列出转换障碍与成本估计（保持可见）。"""
    version_codes = set(version_scope_codes(conn, ctx["data_version_id"]))
    items: list[dict[str, Any]] = []
    for pos in positions:
        code = str(pos["ts_code"])
        shares = int(pos["shares"])
        if shares == 0:
            continue
        restriction_code = None
        reasons: list[str] = []
        conversion_cost: int | None = None
        if pos["locked"]:
            restriction_code = "position_locked"
            reasons.append("锁定持仓不可卖；独立目标不可能通过自动清仓实现")
        elif pos["sellable_shares"] is None:
            restriction_code = "position_sellable_unknown"
            reasons.append("可卖数量未知；fail-closed，绝不假设可变现")
        elif code not in version_codes:
            restriction_code = "holdings_unsupported_or_unvalued"
            reasons.append("持仓不在数据版本范围内；估值缺失，不能假装可变现")
        else:
            market_value = _valuation_market_value_cents(conn, ctx, view, code, shares)
            if market_value is None:
                restriction_code = "no_reference_price"
                reasons.append("缺少估值价；不假设免费变现")
            else:
                fees = _order_fees(schedule, "sell", market_value)
                conversion_cost = fees["fee_cents"] + fees["slippage_cents"]
                reasons.append(
                    "若日后人工决定转仓，需估计下方合成费用/滑点；本报告不承诺任何变现"
                )
        items.append(
            {
                "ts_code": code,
                "decision": "conversion_assessment",
                "quantity_shares": shares,
                "weight_bp": None,
                "restriction_code": restriction_code,
                "reason": "；".join(reasons),
                "conversion_cost_cents_estimate": conversion_cost,
                "conversion_cost_basis": (
                    None
                    if conversion_cost is None
                    else "valuation_close_as_of_synthetic_fees_only"
                ),
                "cost_detail": None,
            }
        )
    return items


# ------------------------------------------------------------------ 买入候选


def _buy_items(
    schedule: Mapping[str, Mapping[str, Any]],
    target_entries: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    reusable: bool,
    cash_budget_cents: int,
    proceeds_funding_cents: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """逐码（代码升序）的买入规划；资金只来自版本化预算（+ 显式 contingent）。

    返回 (items, cash_used_cents, contingent_used_cents)。``proceeds_funding_cents``
    只在推荐配置显式核验开通时非 0，被使用的部分逐项标注 contingent；否则恒 0。
    """
    held_by_code = {str(pos["ts_code"]): int(pos["shares"]) for pos in positions}
    items: list[dict[str, Any]] = []
    remaining_cash = cash_budget_cents
    contingent_remaining = proceeds_funding_cents
    cash_used = 0
    contingent_used = 0
    for entry in sorted(target_entries, key=lambda item: item["ts_code"]):
        code = str(entry["ts_code"])
        target_shares = int(entry["target_shares"])
        if target_shares <= 0:
            items.append(
                {
                    "ts_code": code,
                    "decision": "buy_blocked",
                    "quantity_shares": 0,
                    "requested_shares": 0,
                    "weight_bp": entry["weight_bp"],
                    "restriction_code": "target_value_below_one_share",
                    "reason": "目标金额不足一股：不可行性显式保留",
                    "cost_detail": None,
                }
            )
            continue
        pending = max(target_shares - held_by_code.get(code, 0), 0)
        if pending == 0:
            # 持仓已达标/超目标：不产生买入行（hold/卖出分别处理）。
            continue
        chosen = 0
        chosen_notional = 0
        chosen_fees = 0
        chosen_slippage = 0
        cash_part = 0
        contingent_part = 0
        for candidate in range(pending, 0, -1):
            notional = candidate * int(entry["reference_price_cents"])
            fees = _order_fees(schedule, "buy", notional)
            total = notional + fees["fee_cents"] + fees["slippage_cents"]
            if total <= remaining_cash:
                chosen = candidate
                chosen_notional = notional
                chosen_fees = fees["fee_cents"]
                chosen_slippage = fees["slippage_cents"]
                cash_part = total
                contingent_part = 0
                break
            if reusable and total <= remaining_cash + contingent_remaining:
                deficit = total - remaining_cash
                chosen = candidate
                chosen_notional = notional
                chosen_fees = fees["fee_cents"]
                chosen_slippage = fees["slippage_cents"]
                cash_part = remaining_cash
                contingent_part = deficit
                break
        if chosen <= 0:
            items.append(
                {
                    "ts_code": code,
                    "decision": "buy_blocked",
                    "quantity_shares": 0,
                    "requested_shares": pending,
                    "weight_bp": entry["weight_bp"],
                    "restriction_code": "insufficient_funds",
                    "reason": "可用预算不足（含费用/滑点估计）：不可行性显式保留",
                    "cost_detail": None,
                }
            )
            continue
        remaining_cash -= cash_part
        contingent_remaining -= contingent_part
        cash_used += cash_part
        contingent_used += contingent_part
        shortfall_shares = pending - chosen
        output_cost = {
            "notional_cents": chosen_notional,
            "fee_cents": chosen_fees,
            "slippage_cents": chosen_slippage,
            "funded_by_cash_by_cash_cents": cash_part,
            "funded_by_contingent_by_contingent_cents": contingent_part,
        }
        if shortfall_shares > 0:
            items.append(
                {
                    "ts_code": code,
                    "decision": "buy_partial",
                    "quantity_shares": chosen,
                    "requested_shares": pending,
                    "weight_bp": entry["weight_bp"],
                    "restriction_code": "insufficient_funds",
                    "reason": "可用预算不足：只买入受预算约束的份额；缺口显式保留",
                    "reference_price_cents": entry["reference_price_cents"],
                    "cost_detail": {
                        **output_cost,
                        "shortfall_shares": shortfall_shares,
                        "shortfall_notional_cents": shortfall_shares
                        * int(entry["reference_price_cents"]),
                    },
                }
            )
        else:
            items.append(
                {
                    "ts_code": code,
                    "decision": "buy_full",
                    "quantity_shares": chosen,
                    "requested_shares": pending,
                    "weight_bp": entry["weight_bp"],
                    "restriction_code": (
                        "contingent_on_same_day_sale_proceeds"
                        if contingent_part > 0
                        else None
                    ),
                    "reason": (
                        "买入依赖（尚未发生的）当日卖出变现，保持在显式标注的 contingent"
                        if contingent_part > 0
                        else "按目标数量买入；资金全部来自现金预算"
                    ),
                    "reference_price_cents": entry["reference_price_cents"],
                    "cost_detail": output_cost,
                }
            )
    return items, cash_used, contingent_used


# ------------------------------------------------------------------ 核心


def _run_id_for(input_sha256: str) -> str:
    return "ar_" + input_sha256[:24]


def _find_account_snapshot_id(conn, account_id: str, revision: int) -> str:
    snapshot_id = snapshot_id_for(account_id, revision)
    row = conn.execute(
        "SELECT 1 FROM account_snapshot WHERE snapshot_id=? AND account_id=?"
        " AND account_revision=?",
        (snapshot_id, account_id, revision),
    ).fetchone()
    if row is None:
        raise DataError(
            "s4_account_snapshot_missing",
            f"账户 {account_id} 修订 {revision} 的不可变快照不存在；"
            "建议必须绑定已持久化的账户快照",
        )
    return snapshot_id


def _input_payload(
    *,
    mode: str,
    ctx: Mapping[str, Any],
    account_id: str,
    account_snapshot_id: str,
    rule_bundle: Mapping[str, Any],
    fee_bundle: Mapping[str, Any],
    note: str | None,
    requested_valid_until: str | None,
) -> dict[str, Any]:
    """确定性输入快照：不含任何时间戳/随机数，dict 键序经规范化摘要。"""
    payload: dict[str, Any] = {
        "application_version": ANALYSIS_APPLICATION_VERSION,
        "mode": mode,
        "account_id": account_id,
        "account_snapshot_id": account_snapshot_id,
        "account_revision": ctx["bound_revision"],
        "cash_cents": ctx["cash_cents"],
        "positions": [dict(pos) for pos in ctx["positions"]],
        "as_of": ctx["as_of"],
        "source_code": ctx["source_code"],
        "data_version_id": ctx["data_version_id"],
        "pool_snapshot_id": ctx["pool_snapshot_id"],
        "rule_bundle_id": rule_bundle["bundle_id"],
        "rule_bundle_sha256": rule_bundle["content_sha256"],
        "fee_bundle_id": fee_bundle["bundle_id"],
        "fee_bundle_sha256": fee_bundle["content_sha256"],
    }
    if note is not None:
        payload["note"] = note
    payload["requested_valid_until"] = requested_valid_until
    return payload


def _persist_artifact(conn, run_id: str, kind: str, payload: Mapping[str, Any], created_at: str) -> str:
    text = canonical_json_bytes(dict(payload)).decode("utf-8")
    sha = sha256_of_object(dict(payload))
    conn.execute(
        "INSERT INTO analysis_artifact(run_id, artifact_kind, content, content_sha256, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, kind, text, sha, created_at),
    )
    return sha


def _read_artifact(conn, run_id: str, kind: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT content, content_sha256 FROM analysis_artifact WHERE run_id=? AND artifact_kind=?",
        (run_id, kind),
    ).fetchone()
    if row is None:
        raise DataError("s4_artifact_missing", f"run {run_id} 缺少 {kind} 工件")
    content_text = str(row[0])
    stored_sha = str(row[1])
    try:
        payload = json.loads(content_text)
    except (TypeError, ValueError) as exc:
        raise DataError(
            "s4_artifact_corrupt", f"run {run_id} 的 {kind} 工件不是有效 JSON"
        ) from exc
    recomputed = sha256_of_object(payload)
    if recomputed != stored_sha:
        raise DataError(
            "s4_artifact_corrupt",
            f"run {run_id} 的 {kind} 工件摘要不一致：存储 {stored_sha}，重算 {recomputed}",
        )
    return payload


def create_recommendation_run(
    conn,
    context: Any,
    *,
    account_id: str,
    account_state: Mapping[str, Any],
    fee_bundle_id: str,
    mode: str = "incremental",
    note: str | None = None,
    valid_until: str | None = None,
) -> dict[str, Any]:
    """生成一条不可变建议 run（幂等：同输入返回既有 run，不重算）。

    ``valid_until`` 为可选的显式 UTC 时间；缺省时按版本化
    ``recommendation_validity.default_hours`` 生成默认期限。期限是结果快照的
    一部分，读取时过期即标记 ``expired``，但不改写工件。
    """
    if not isinstance(mode, str) or mode not in ANALYSIS_MODES:
        raise DataError(
            "s4_mode_unknown", f"未知编排模式: {mode!r}；支持 {list(ANALYSIS_MODES)}"
        )
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise DataError("s4_note_invalid", f"note 必须是非空字符串或 None: {note!r}")
    requested_valid_until: str | None = None
    if valid_until is not None:
        if not isinstance(valid_until, str) or not valid_until.strip():
            raise DataError("s4_expiry_invalid", f"valid_until 必须是带时区的 ISO 时间: {valid_until!r}")
        try:
            parsed_valid_until = datetime.fromisoformat(valid_until.strip())
        except ValueError as exc:
            raise DataError(
                "s4_expiry_invalid", f"valid_until 不是合法 ISO 时间: {valid_until!r}"
            ) from exc
        if parsed_valid_until.tzinfo is None:
            raise DataError("s4_expiry_invalid", "valid_until 必须带时区")
        requested_valid_until = parsed_valid_until.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        )
    if not isinstance(account_id, str) or not account_id.strip():
        raise DataError("s4_account_id_required", "必须显式提供 account_id（上下文不含账户）")
    if not isinstance(account_state, Mapping):
        raise DataError(
            "s4_account_state_required",
            f"account_state 必须是来自 get_account_state 的映射: {type(account_state)!r}",
        )
    ctx = _validate_entry(conn, context, account_state)

    fee_bundle = load_rule_bundle(conn, fee_bundle_id)
    if fee_bundle is None:
        raise DataError(
            "s4_fee_evidence_missing",
            f"费用组合 {fee_bundle_id} 不存在；缺费用证据 fail-closed，不发明费率",
        )
    if fee_bundle.get("kind") != "fee":
        raise DataError("s4_fee_evidence_missing", f"{fee_bundle_id} 不是费用组合")
    schedule = _fee_schedule(fee_bundle)

    parameter_bundle = ctx["rule_bundle"]
    if parameter_bundle is None:
        raise DataError(
            "s4_rule_bundle_required",
            "绑定上下文必须已加载参数规则组合；预算/约束只接受版本化内容",
        )
    parameter_content = parameter_bundle.get("content")
    if not isinstance(parameter_content, Mapping):
        raise DataError(
            "s4_rule_bundle_evidence_missing",
            f"参数组合内容不是映射: {type(parameter_content)!r}",
        )

    account_row = conn.execute(
        "SELECT account_revision FROM account WHERE account_id=?", (account_id,)
    ).fetchone()
    if account_row is None:
        raise DataError("s4_account_not_found", f"账户 {account_id} 不存在")
    if int(account_row[0]) != ctx["bound_revision"]:
        raise DataError(
            "s4_account_revision_stale",
            f"账户 {account_id} 的最新修订 {int(account_row[0])} 与上下文绑定 "
            f"{ctx['bound_revision']} 不一致；上下文/账户状态已变化，请重新绑定后再编排",
        )
    snapshot_id = _find_account_snapshot_id(conn, account_id, ctx["bound_revision"])
    ensure_analysis_artifact_table(conn)

    input_payload = _input_payload(
        mode=mode,
        ctx=ctx,
        account_id=account_id,
        account_snapshot_id=snapshot_id,
        rule_bundle=parameter_bundle,
        fee_bundle=fee_bundle,
        note=note,
        requested_valid_until=requested_valid_until,
    )
    input_sha256 = sha256_of_object(input_payload)
    run_id = _run_id_for(input_sha256)
    existing = conn.execute(
        "SELECT run_id FROM analysis_run WHERE input_sha256=?", (input_sha256,)
    ).fetchone()
    if existing is not None:
        stored_input = _read_artifact(conn, run_id, "input")
        if sha256_of_object(stored_input) != input_sha256:
            raise DataError(
                "s4_input_digest_conflict",
                f"run {run_id} 的输入工件与本次输入摘要不一致；拒绝以陈旧工件冒充复用",
            )
        report = _read_artifact(conn, run_id, "output")
        return {
            "run_id": run_id,
            "input_sha256": input_sha256,
            "reused": True,
            "recomputed": False,
            "report": report,
        }

    generated_at = utc_now_iso()
    validity_cfg = parameter_content.get("recommendation_validity")
    default_hours = DEFAULT_RECOMMENDATION_VALIDITY_HOURS
    max_hours = MAX_RECOMMENDATION_VALIDITY_HOURS
    if isinstance(validity_cfg, Mapping):
        raw_default = validity_cfg.get("default_hours")
        if (
            isinstance(raw_default, int)
            and not isinstance(raw_default, bool)
            and 1 <= raw_default <= max_hours
        ):
            default_hours = int(raw_default)
        raw_max = validity_cfg.get("max_hours")
        if (
            isinstance(raw_max, int)
            and not isinstance(raw_max, bool)
            and 1 <= raw_max <= MAX_RECOMMENDATION_VALIDITY_HOURS
        ):
            max_hours = int(raw_max)
        if default_hours > max_hours:
            default_hours = max_hours
    generated_dt = datetime.fromisoformat(generated_at)
    if requested_valid_until is None:
        valid_until_dt = generated_dt + timedelta(hours=default_hours)
    else:
        valid_until_dt = datetime.fromisoformat(requested_valid_until)
        if valid_until_dt < generated_dt:
            raise DataError("s4_expiry_invalid", "valid_until 不能早于生成时间")
        if valid_until_dt > generated_dt + timedelta(hours=max_hours):
            raise DataError(
                "s4_expiry_invalid",
                f"valid_until 超过版本化最大期限 {max_hours} 小时",
            )
    effective_valid_until = valid_until_dt.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )
    ideal = compute_ideal_target(conn, context, account_state=account_state)
    _assert_ctx_consistent_with_ideal(ideal, ctx)

    allocation = ideal["allocation"]
    restricted = bool(ideal.get("restricted"))
    restriction_code = ideal.get("restriction_code")
    restriction_detail = ideal.get("restriction_detail")
    if not restricted and allocation.get("restricted"):
        restricted = True
        restriction_code = allocation.get("reason_code")
        restriction_detail = "理想目标分配受限（缺版本化约束块）；不允许任何下单/置换建议"
    if not restricted and allocation.get("ok") is False:
        restricted = True
        restriction_code = allocation.get("reason_code")
        restriction_detail = "理想目标分配存在冲突（如现金保留超过现金）；禁止下单"
    if not restricted and ctx["binding_restricted"]:
        restricted = True
        restriction_code = ctx["binding_restriction_code"]
        restriction_detail = (
            f"绑定上下文受限（{ctx['binding_restriction_detail']}）；不产生任何交易建议"
        )

    positions = ctx["positions"]
    assessment_items, holdings_estimation = _holdings_assessment_items(
        conn, ctx, ctx["view"], positions
    )
    notices = [
        {"code": code, "detail": ""}
        for code in (
            _NOTICE_SYNTHETIC_FEES,
            _NOTICE_NO_FREE_LIQUIDATION,
            _NOTICE_MANUAL_CONFIRMATION,
            _NOTICE_INFEASIBILITY_VISIBLE,
        )
    ]
    notices.append({"code": _NOTICE_CONTINGENT_ONLY, "detail": "default_not_reusable"})

    if restricted:
        items = sorted(assessment_items, key=lambda item: item["ts_code"])
        reserved_cents = int(allocation.get("reserved_cents") or 0)
        budget = {
            "cash_cents": ctx["cash_cents"],
            "reserved_cents": reserved_cents,
            "same_day_sale_proceeds_reusable": False,
            "contingent_estimated_proceeds_cents": 0,
            "budget_base_available_for_buys_cents": 0,
            "valuation": holdings_estimation,
        }
        totals = {
            "buy_planned_notional_cents": 0,
            "buy_fees_cents": 0,
            "buy_slippage_cents": 0,
            "sell_contingent_estimated_proceeds_cents": 0,
            "sell_fees_cents": 0,
            "sell_slippage_cents": 0,
            "cash_funded_buys_cents": 0,
            "residual_cash_cents": ctx["cash_cents"],
        }
        report = _build_report(
            mode=mode,
            account_id=account_id,
            ctx=ctx,
            items=items,
            restricted=True,
            restriction_code=restriction_code,
            restriction_detail=restriction_detail,
            budget=budget,
            totals=totals,
            notices=notices,
            run_id=run_id,
            input_sha256=input_sha256,
            ideal=ideal,
            note=note,
            valid_from=generated_at,
            valid_until=effective_valid_until,
        )
        _persist_run(
            conn,
            run_id=run_id,
            input_sha256=input_sha256,
            mode=mode,
            account_id=account_id,
            ctx=ctx,
            snapshot_id=snapshot_id,
            restricted=True,
            restriction_code=restriction_code,
            restriction_detail=restriction_detail,
            generated_at=generated_at,
            valid_until=effective_valid_until,
            input_payload=input_payload,
            report=report,
            items=items,
            target_kind=None,
            target_rows=[],
            targeted_cash_cents=None,
        )
        return {
            "run_id": run_id,
            "input_sha256": input_sha256,
            "reused": False,
            "recomputed": True,
            "report": report,
        }

    # ---- 未受限：正常编排（目标份额、买卖、预算） ----
    execution_assumptions = parameter_content.get("execution_assumptions") or {}
    reusable = bool(
        execution_assumptions.get("same_day_sale_proceeds_reusable") is True
        and execution_assumptions.get("verification_status") == "verified"
    )
    if execution_assumptions.get("same_day_sale_proceeds_reusable") is True and not reusable:
        raise DataError(
            "s4_reusable_proceeds_unverified",
            "参数组合打开了当日卖出变现可复用，但 verification_status 未核验为 "
            "verified；fail-closed 拒绝按未核验假设编排",
        )
    if reusable and mode == "independent":
        raise DataError(
            "s4_reusable_proceeds_only_incremental",
            "same_day_sale_proceeds_reusable 只对增量模式有意义；独立目标不做任何自动"
            "清仓，不能依赖未发生的卖出变现",
        )

    reference_prices: dict[str, int | None] = {}
    target_entries: list[dict[str, Any]] = []
    target_shares_by_code: dict[str, int | None] = {}
    sizing_unresolved_codes: set[str] = set()
    early_buy_items: list[dict[str, Any]] = []
    for target in allocation.get("targets") or []:
        code = str(target["ts_code"])
        price_cents = _reference_price_cents(conn, ctx, ctx["view"], code)
        reference_prices[code] = price_cents
        target_value = int(target["target_value_cents"])
        if price_cents is None:
            sizing_unresolved_codes.add(code)
            target_shares_by_code[code] = None
            early_buy_items.append(
                {
                    "ts_code": code,
                    "decision": "buy_blocked",
                    "quantity_shares": 0,
                    "requested_shares": None,
                    "weight_bp": int(target["weight_bp"]),
                    "restriction_code": "no_reference_price",
                    "reason": "目标股票缺少可成交参考价：无法确定目标份额；fail-closed",
                    "cost_detail": None,
                }
            )
            continue
        shares_needed = target_value // int(price_cents)
        target_shares_by_code[code] = shares_needed
        target_entries.append(
            {
                "ts_code": code,
                "target_shares": shares_needed,
                "weight_bp": int(target["weight_bp"]),
                "target_value_cents": target_value,
                "reference_price_cents": int(price_cents),
            }
        )

    sell_items: list[dict[str, Any]] = []
    conversion_items: list[dict[str, Any]] = []
    contingent_proceeds_cents = 0
    if mode == "independent":
        conversion_items = _conversion_assessment_items(
            conn, ctx, ctx["view"], positions, schedule
        )
    else:
        sell_items = _sell_items(
            conn,
            ctx,
            ctx["view"],
            positions,
            target_shares_by_code,
            sizing_unresolved_codes,
            schedule,
            parameter_content.get("rebalance_buffer"),
        )
        contingent_proceeds_cents = sum(
            int(item["cost_detail"]["contingent_estimated_proceeds_cents"])
            for item in sell_items
            if item["decision"] == "sell_proposed" and item["cost_detail"] is not None
        )
        contingent_proceeds_cents = max(contingent_proceeds_cents, 0)

    reserved_cents = int(allocation.get("reserved_cents") or 0)
    cash_cents = ctx["cash_cents"]
    if reserved_cents > cash_cents:
        raise DataError(
            "s4_internal_consistency",
            f"理想目标现金保留 {reserved_cents} 超过账户现金 {cash_cents}",
        )
    budget_base = cash_cents - reserved_cents
    proceeds_funding = contingent_proceeds_cents if reusable and mode == "incremental" else 0
    buy_items, cash_used, contingent_used = _buy_items(
        schedule, target_entries, positions, reusable, budget_base, proceeds_funding
    )
    buy_items = early_buy_items + buy_items

    if reusable and contingent_used > contingent_proceeds_cents:
        raise DataError(
            "s4_internal_consistency",
            "contingent 使用超过假设性卖出变现合计；拒绝不一致的对账",
        )

    if mode == "independent":
        hold_items: list[dict[str, Any]] = []
        target_items: list[dict[str, Any]] = [
            _independent_target_item(entry)
            for entry in sorted(target_entries, key=lambda item: item["ts_code"])
        ]
        other_assessment = conversion_items
        items = target_items + buy_items + other_assessment
    else:
        hold_items = _hold_keep_items(positions, target_shares_by_code)
        covered = {
            str(item["ts_code"])
            for item in buy_items + sell_items + hold_items
        }
        other_assessment = [
            item for item in assessment_items if str(item["ts_code"]) not in covered
        ]
        items = buy_items + sell_items + hold_items + other_assessment
    items.sort(key=lambda item: (str(item["ts_code"]), str(item["decision"])))
    for sequence, item in enumerate(items, start=1):
        item["seq"] = sequence

    buy_fees_total = sum(
        int(item["cost_detail"]["fee_cents"])
        for item in buy_items
        if item["cost_detail"] is not None
    )
    buy_slippage_total = sum(
        int(item["cost_detail"]["slippage_cents"])
        for item in buy_items
        if item["cost_detail"] is not None
    )
    buy_planned_notional = sum(
        int(item["cost_detail"]["notional_cents"])
        for item in buy_items
        if item["cost_detail"] is not None
    )
    sell_fees_total = sum(
        int(item["cost_detail"]["fee_cents"])
        for item in sell_items
        if item["decision"] == "sell_proposed" and item["cost_detail"] is not None
    )
    sell_slippage_total = sum(
        int(item["cost_detail"]["slippage_cents"])
        for item in sell_items
        if item["decision"] == "sell_proposed" and item["cost_detail"] is not None
    )
    final_cash = budget_base - cash_used
    assert final_cash >= 0, "剩余现金不允许为负；预算逐笔扣减必须守恒"
    # 对账恒等式：cash == reserved + cash_used + final_cash（分毫不差）。
    assert (
        reserved_cents + cash_used + final_cash == cash_cents
    ), "预算对账破坏：reserved + buys(现金部分) + 剩余现金 必须等于账户现金"

    totals = {
        "buy_planned_notional_cents": buy_planned_notional,
        "buy_fees_cents": buy_fees_total,
        "buy_slippage_cents": buy_slippage_total,
        "sell_contingent_estimated_proceeds_cents": contingent_proceeds_cents,
        "sell_fees_cents": sell_fees_total,
        "sell_slippage_cents": sell_slippage_total,
        "cash_funded_buys_cents": cash_used,
        "residual_cash_cents": final_cash,
    }
    budget = {
        "cash_cents": cash_cents,
        "reserved_cents": reserved_cents,
        "same_day_sale_proceeds_reusable": reusable,
        "contingent_estimated_proceeds_cents": (
            contingent_proceeds_cents if reusable and mode == "incremental" else 0
        ),
        "contingent_planned_cents": contingent_used,
        "budget_base_available_for_buys_cents": budget_base,
        "valuation": holdings_estimation,
        "contingent_labels": (
            "buy items beyond cash budget are each labeled contingent_on_same_day_sale_proceeds"
            if proceeds_funding
            else "all buys funded strictly by cash budget; no hypothetical proceeds used"
        ),
    }
    report = _build_report(
        mode=mode,
        account_id=account_id,
        ctx=ctx,
        items=items,
        restricted=False,
        restriction_code=None,
        restriction_detail=None,
        budget=budget,
        totals=totals,
        notices=notices,
        run_id=run_id,
        input_sha256=input_sha256,
        ideal=ideal,
        note=note,
        valid_from=generated_at,
        valid_until=effective_valid_until,
    )
    held_by_code = {str(pos["ts_code"]): int(pos["shares"]) for pos in positions}
    target_rows = [
        {
            "ts_code": str(entry["ts_code"]),
            "target_shares": int(entry["target_shares"]),
            "delta_shares": int(entry["target_shares"]) - held_by_code.get(str(entry["ts_code"]), 0),
            "price_basis": "raw_execution_close_as_of_cents",
            "limit_price_cents": int(entry["reference_price_cents"]),
            "reason": "独立于当前持仓的版本化理想目标" if mode == "independent"
            else "从当前持仓向理想目标过渡的目标份额",
            "seq": sequence_order,
        }
        for sequence_order, entry in enumerate(
            sorted(target_entries, key=lambda item: item["ts_code"]), start=1
        )
    ]
    target_kind = "ideal" if mode == "independent" else "executable_transition"
    _persist_run(
        conn,
        run_id=run_id,
        input_sha256=input_sha256,
        mode=mode,
        account_id=account_id,
        ctx=ctx,
        snapshot_id=snapshot_id,
        restricted=False,
        restriction_code=None,
        restriction_detail=None,
        generated_at=generated_at,
        valid_until=effective_valid_until,
        input_payload=input_payload,
        report=report,
        items=items,
        target_kind=target_kind,
        target_rows=target_rows,
        targeted_cash_cents=final_cash,
    )
    return {
        "run_id": run_id,
        "input_sha256": input_sha256,
        "reused": False,
        "recomputed": True,
        "report": report,
    }


def _persist_run(
    conn,
    *,
    run_id: str,
    input_sha256: str,
    mode: str,
    account_id: str,
    ctx: Mapping[str, Any],
    snapshot_id: str,
    restricted: bool,
    restriction_code: str | None,
    restriction_detail: str | None,
    generated_at: str,
    valid_until: str,
    input_payload: Mapping[str, Any],
    report: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    target_kind: str | None,
    target_rows: Sequence[Mapping[str, Any]],
    targeted_cash_cents: int | None,
) -> None:
    """全部持久化在同一连接/调用方事务边界内完成（不提交；调用方负责 commit）。"""
    conn.execute(
        "INSERT INTO analysis_run(run_id, mode, account_id, account_snapshot_id,"
        " account_revision, source_code, data_version_id, pool_snapshot_id, as_of,"
        " data_cutoff_date, rule_bundle_id, input_sha256, status, restriction_code,"
        " restriction_detail, generated_at, valid_from, valid_until, invalidation_reason,"
        " source_changed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, NULL, 0)",
        (
            run_id,
            mode,
            account_id,
            snapshot_id,
            ctx["bound_revision"],
            ctx["source_code"],
            ctx["data_version_id"],
            ctx["pool_snapshot_id"],
            ctx["as_of"],
            None,
            ctx["rule_bundle"]["bundle_id"],
            input_sha256,
            "restricted" if restricted else "complete",
            restriction_code,
            restriction_detail,
            generated_at,
            generated_at,
            valid_until,
        ),
    )
    _persist_artifact(conn, run_id, "input", input_payload, generated_at)
    _persist_artifact(conn, run_id, "output", report, generated_at)
    for sequence, item in enumerate(items, start=1):
        conn.execute(
            "INSERT INTO analysis_item(run_id, seq, ts_code, decision, reason,"
            " quantity_shares, weight_bp, restriction_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                sequence,
                str(item["ts_code"]),
                str(item["decision"]),
                item.get("reason"),
                item.get("quantity_shares"),
                item.get("weight_bp"),
                item.get("restriction_code"),
            ),
        )
    if target_kind is not None and target_rows:
        target_id = "tgt_" + sha256_of_object([run_id, target_kind])[:16]
        conn.execute(
            "INSERT INTO portfolio_target(target_id, run_id, kind, cash_cents,"
            " turnover_budget_bp, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (target_id, run_id, target_kind, int(targeted_cash_cents), None, generated_at),
        )
        for row in target_rows:
            conn.execute(
                "INSERT INTO target_item(target_id, ts_code, target_shares, delta_shares,"
                " price_basis, limit_price_cents, reason, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    target_id,
                    str(row["ts_code"]),
                    int(row["target_shares"]),
                    int(row["delta_shares"]),
                    row["price_basis"],
                    row["limit_price_cents"],
                    row["reason"],
                    int(row["seq"]),
                ),
            )
    reasons_prefix = f"analysis_run:{run_id}"
    register_reference_holds(
        conn,
        [
            ReferenceTarget(
                target_type="version",
                target_id=ctx["data_version_id"],
                reason="run_input_evidence",
            ),
            ReferenceTarget(
                target_type="snapshot",
                target_id=ctx["pool_snapshot_id"],
                reason="run_candidate_pool_evidence",
            ),
        ],
        reason_prefix=reasons_prefix,
    )


# ------------------------------------------------------------------ 读取与失效


def list_recommendation_runs(
    conn,
    account_id: str,
    *,
    mode: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """有界列表：默认 50 行、最多 200 行；新生成的 run 在前。"""
    if mode is not None and mode not in ANALYSIS_MODES:
        raise DataError("s4_mode_unknown", f"未知编排模式: {mode!r}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = 50
    limit = min(limit, 200)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        offset = 0
    if mode:
        rows = conn.execute(
            "SELECT run_id, generated_at, account_revision FROM analysis_run"
            " WHERE account_id=? AND mode=? ORDER BY generated_at DESC, run_id DESC"
            " LIMIT ? OFFSET ?",
            (account_id, mode, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT run_id, generated_at, account_revision FROM analysis_run"
            " WHERE account_id=? ORDER BY generated_at DESC, run_id DESC LIMIT ? OFFSET ?",
            (account_id, limit, offset),
        ).fetchall()
    return [
        {"run_id": row[0], "generated_at": row[1], "account_revision": int(row[2])}
        for row in rows
    ]


def get_recommendation_run(conn, run_id: str) -> dict[str, Any]:
    """GET 等价只读：只反序列化已核验工件；绝不重新计算，也绝不改写。"""
    row = conn.execute(
        "SELECT run_id, mode, account_id, account_revision, source_code, data_version_id,"
        " pool_snapshot_id, as_of, rule_bundle_id, input_sha256, status, restriction_code,"
        " restriction_detail, generated_at, valid_from, valid_until, invalidation_reason,"
        " source_changed FROM analysis_run WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise DataError("s4_run_not_found", f"建议 run {run_id} 不存在")
    report = _read_artifact(conn, run_id, "output")
    input_payload = _read_artifact(conn, run_id, "input")
    account_revision_now = conn.execute(
        "SELECT account_revision FROM account WHERE account_id=?", (str(row[2]),)
    ).fetchone()
    revision_now = int(account_revision_now[0]) if account_revision_now is not None else None
    input_invalidated = bool(revision_now is not None and revision_now > int(row[3]))
    valid_until = row[15]
    expired = False
    if valid_until is not None:
        try:
            expired = datetime.fromisoformat(str(valid_until)) <= datetime.now(timezone.utc)
        except ValueError:
            expired = True
    effective = (
        not input_invalidated
        and not expired
        and not bool(row[17])
    )
    return {
        "run_id": row[0],
        "mode": row[1],
        "account_id": row[2],
        "account_revision": row[3],
        "source_code": row[4],
        "data_version_id": row[5],
        "pool_snapshot_id": row[6],
        "as_of": row[7],
        "rule_bundle_id": row[8],
        "input_sha256": row[9],
        "status": row[10],
        "restriction_code": row[11],
        "restriction_detail": row[12],
        "generated_at": row[13],
        "valid_from": row[14],
        "valid_until": valid_until,
        "invalidation_reason": row[16],
        "source_changed": bool(row[17]),
        "account_revision_current": revision_now,
        "input_invalidated": input_invalidated,
        "expired": expired,
        "effective": effective,
        "input_payload": input_payload,
        "report": report,
    }


def invalidate_recommendation_run(
    conn, run_id: str, *, reason: str, expiry_time: str | None = None
) -> dict[str, Any]:
    """显式失效：只写 valid_until/invalidation_reason；工件永不改写。"""
    row = conn.execute(
        "SELECT valid_until, invalidation_reason FROM analysis_run WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise DataError("s4_run_not_found", f"建议 run {run_id} 不存在")
    if row[1] is not None:
        # 只有显式失效原因才算“已失效”；默认期限到期不是显式失效。
        if str(row[1]) == reason:
            return {"run_id": run_id, "valid_until": str(row[0]), "already": True}
        raise DataError(
            "s4_run_already_invalidated",
            f"建议 run {run_id} 已失效（{row[1]}）；不接受改写失效原因",
        )
    until = expiry_time or utc_now_iso()
    if not isinstance(until, str) or not until.strip():
        raise DataError("s4_expiry_invalid", f"valid_until 必须是显式时间: {until!r}")
    try:
        parsed_until = datetime.fromisoformat(until.strip())
    except ValueError as exc:
        raise DataError("s4_expiry_invalid", f"valid_until 不是合法 ISO 时间: {until!r}") from exc
    if parsed_until.tzinfo is None:
        raise DataError("s4_expiry_invalid", "valid_until 必须带时区")
    until = parsed_until.astimezone(timezone.utc).isoformat(timespec="seconds")
    if not isinstance(reason, str) or not reason.strip():
        raise DataError("s4_invalidation_reason_required", "失效必须显式登记原因")
    conn.execute(
        "UPDATE analysis_run SET valid_until=?, invalidation_reason=? WHERE run_id=?",
        (until, reason, run_id),
    )
    return {"run_id": run_id, "valid_until": until, "already": False}


# ------------------------------------------------------------------ 内部小工具


def _independent_target_item(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts_code": str(entry["ts_code"]),
        "decision": "independent_target",
        "quantity_shares": int(entry["target_shares"]),
        "weight_bp": entry["weight_bp"],
        "restriction_code": "contingent_on_converting_current_holdings",
        "reason": "同预算独立目标；实现需后续人工转仓，转仓成本/障碍详见转换评估项",
        "reference_price_cents": entry["reference_price_cents"],
        "target_value_cents": entry["target_value_cents"],
        "cost_detail": None,
    }


def _hold_keep_items(
    positions: Sequence[Mapping[str, Any]],
    target_shares_by_code: Mapping[str, int | None],
) -> list[dict[str, Any]]:
    """已恰好达到目标份额的持仓（incremental）保持可见。"""
    items: list[dict[str, Any]] = []
    for pos in positions:
        code = str(pos["ts_code"])
        held = int(pos["shares"])
        if held == 0:
            continue
        target_shares = target_shares_by_code.get(code)
        if target_shares is not None and int(target_shares) == held:
            items.append(
                {
                    "ts_code": code,
                    "decision": "hold",
                    "quantity_shares": held,
                    "requested_shares": 0,
                    "weight_bp": None,
                    "restriction_code": None,
                    "reason": "持仓已达目标数量；无需买入",
                    "cost_detail": None,
                }
            )
    return items


def _assert_ctx_consistent_with_ideal(ideal: Mapping[str, Any], ctx: Mapping[str, Any]) -> None:
    if ideal.get("as_of") != ctx["as_of"]:
        raise DataError("s4_internal_consistency", "理想目标时点与缓存的上下文不一致")
    if ideal.get("data_version_id") != ctx["data_version_id"]:
        raise DataError("s4_internal_consistency", "理想目标数据版本与上下文不一致")
    if ideal.get("account_revision") != ctx["bound_revision"]:
        raise DataError("s4_internal_consistency", "理想目标账户修订与上下文不一致")


def _build_report(
    *,
    mode: str,
    account_id: str,
    ctx: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    restricted: bool,
    restriction_code: str | None,
    restriction_detail: str | None,
    budget: Mapping[str, Any],
    totals: Mapping[str, Any],
    notices: Sequence[Mapping[str, Any]],
    run_id: str,
    input_sha256: str,
    ideal: Mapping[str, Any],
    note: str | None,
    valid_from: str,
    valid_until: str,
) -> dict[str, Any]:
    sections: dict[str, list[dict[str, Any]]] = {
        "buys_and_targets": [],
        "sells": [],
        "holdings_kept": [],
        "holdings_assessment": [],
    }
    for item in items:
        decision = str(item.get("decision", ""))
        if decision.startswith("buy") or decision == "independent_target":
            sections["buys_and_targets"].append(dict(item))
        elif decision.startswith("sell"):
            sections["sells"].append(dict(item))
        elif decision == "hold":
            sections["holdings_kept"].append(dict(item))
        elif decision in ("assessment_only", "conversion_assessment"):
            sections["holdings_assessment"].append(dict(item))
        else:
            sections.setdefault("other", []).append(dict(item))
    return {
        "application_version": ANALYSIS_APPLICATION_VERSION,
        "mode": mode,
        "run_id": run_id,
        "input_sha256": input_sha256,
        "account_id": account_id,
        "account_revision": ctx["bound_revision"],
        "valid_from": valid_from,
        "valid_until": valid_until,
        "as_of": ctx["as_of"],
        "source_code": ctx["source_code"],
        "data_version_id": ctx["data_version_id"],
        "pool_snapshot_id": ctx["pool_snapshot_id"],
        "restricted": restricted,
        "restriction_code": restriction_code,
        "restriction_detail": restriction_detail,
        "budget": budget,
        "totals": totals,
        "items": [dict(item) for item in items],
        "sections": sections,
        "ideal_target_components": ideal.get("components"),
        "ideal_input_sha256": ideal.get("input_digest"),
        "disclaimers": [
            "informational_recommendation_only_not_executable",
            "requires_manual_confirmation",
            "no_optimality_or_profitability_claim",
        ],
        "note": note,
    }
