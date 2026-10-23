"""阶段三 S3：理想目标组合分析入口（只读、确定性、研究用途）。

编排契约：

- 输入是已绑定的 ``BoundDataContext``（``purpose=signal_input``，VersionView 固定
  数据版本与来源）与账户当前状态；缺任一项显式拒绝；
- 候选集 = 已登记股票池快照成员 ∩ 数据版本范围 ∩ 绑定时的用途资格判定。
  绑定时 ``check_eligibility`` 的逐证券价格/陈旧/历史/质量判定逐项保留为排除
  原因；本模块不做任何 SQL 旁路、不从代码前缀推断支持性；
- 信号/排序/分配三个组件经注册表按名取用，可被显式替换（换名登记的替代实现）；
- 当前持仓中不在被选目标内的证券一律"另估"（``separate_assessment=True``）：
  绝不视为可卖、绝不自动清仓、绝不输出卖出量；估值数值仅用于信息展示，
  并显式标注其来自存储浮点行情的换算（ROUND_HALF_UP 到分），不作为交易依据；
- 当绑定时上下文因股票池证据未决/受限而处于 restricted 状态时，限制原样写入
  报告（研究用途、不构成可执行建议）；绝不声称收益或最优性。

冻结基线的达标门槛与排序由 ``selection`` 决定；分配口径由 ``allocation`` 决定。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

from ..data.chunks import VersionView, version_scope_codes
from ..data.digests import sha256_of_object
from ..data.errors import DataError
from ..data.prices import valuation_price
from ..data.universe import load_pool_snapshot
from .allocation import (
    ALLOCATION_POLICY_REGISTRY,
    AllocationRequest,
    BaselineEqualWeightCappedAllocation,
)
from .selection import BaselineMomentumRankSelection, SELECTION_POLICY_REGISTRY
from .signals import (
    BaselineMomentumTrendCalculator,
    SIGNAL_CALCULATOR_REGISTRY,
    SignalResult,
)

__all__ = ["TARGET_REPORT_VERSION", "compute_ideal_target"]

TARGET_REPORT_VERSION = "s3-target-1.0"

_DISCLAIMER_NOTICES = (
    "informational_analysis_only_not_investment_advice",
    "ideal_target_is_not_an_executable_rebalancing_plan",
    "no_optimality_or_profitability_claim",
)


def _report_notice(code: str, detail: str) -> dict[str, Any]:
    return {"code": code, "detail": detail}


def compute_ideal_target(
    conn,
    context: Any,
    *,
    account_state: Mapping[str, Any] | None = None,
    signal_name: str = SIGNAL_CALCULATOR_REGISTRY.names()[0],
    selection_name: str = SELECTION_POLICY_REGISTRY.names()[0],
    allocation_name: str = ALLOCATION_POLICY_REGISTRY.names()[0],
) -> dict[str, Any]:
    """计算一次理想目标组合分析报告（只读；不写库）。

    默认组件名取"基线"（注册表恒含冻结基线）；调用方可以替换为按新名登记的
    替代实现。账户状态须来自 ``ledger.get_account_state``（含修订）。
    """
    if context is None:
        raise DataError("s3_context_required", "必须提供已绑定的数据上下文")
    if getattr(context, "view", None) is None:
        raise DataError(
            "s3_context_requires_fixed_version_view",
            "绑定上下文必须可提供固定数据版本的 VersionView；缺失即拒绝",
        )
    view: VersionView = context.view
    if context.context.purpose != "signal_input":
        raise DataError(
            "s3_requires_signal_input_purpose",
            f"理想目标分析只接受 purpose=signal_input 的绑定上下文，当前 {context.context.purpose!r}",
        )
    if account_state is None:
        raise DataError(
            "s3_account_state_required",
            "必须显式提供账户当前状态（来自 get_account_state）；不得从未知状态推测现金",
        )
    bound_revision = context.context.account_revision
    state_revision = account_state.get("revision") if hasattr(account_state, "get") else None
    if (
        isinstance(state_revision, bool)
        or not isinstance(state_revision, int)
        or state_revision < 1
    ):
        raise DataError(
            "s3_account_revision_invalid",
            f"账户状态缺少有效修订号（revision={state_revision!r}）；"
            "报告与输入摘要必须绑定到与上下文一致的账户修订",
        )
    if state_revision != bound_revision:
        raise DataError(
            "s3_account_revision_mismatch",
            f"账户状态修订 {state_revision} 与绑定上下文账户修订 {bound_revision} 不一致；"
            "拒绝输出与绑定不符的理想目标组合",
        )
    as_of = context.context.as_of
    source_code = context.context.source_code
    data_version_id = context.context.data_version_id

    calculator_cls = SIGNAL_CALCULATOR_REGISTRY.require(signal_name)
    selection_cls = SELECTION_POLICY_REGISTRY.require(selection_name)
    allocation_cls = ALLOCATION_POLICY_REGISTRY.require(allocation_name)
    calculator = calculator_cls()
    selection_policy = selection_cls()
    allocation_policy = allocation_cls()

    notices: list[dict[str, Any]] = []

    bundle = context.rule_bundle
    if bundle is None:
        raise DataError(
            "s3_rule_bundle_required",
            "绑定上下文必须已加载规则组合；分配约束只接受版本化内容",
        )

    # 候选范围：股票池快照成员是已登记的可核验范围；未绑定股票池即拒绝（fail-closed）。
    pool_snapshot_id = context.context.pool_snapshot_id
    pool_members: list[str] = []
    if pool_snapshot_id is None:
        raise DataError(
            "s3_candidate_pool_not_bound",
            "signal_input 绑定必须显式携带合格股票池快照；无核验范围不得挑选候选",
        )
    pool = load_pool_snapshot(conn, pool_snapshot_id)
    if pool is None:
        raise DataError(
            "pool_snapshot_not_found",
            f"股票池快照 {pool_snapshot_id} 不存在",
        )
    if pool["data_version_id"] != data_version_id:
        raise DataError(
            "pool_snapshot_version_mismatch",
            f"股票池快照 {pool_snapshot_id} 基于 {pool['data_version_id']}，"
            f"与上下文 {data_version_id} 不一致；拒绝以此挑选候选",
        )
    pool_members = [str(code) for code in pool["members"]]

    version_codes = set(version_scope_codes(conn, data_version_id))
    eligibility = context.eligibility
    details: dict[str, dict[str, Any]] = {}
    if eligibility is not None:
        for item in eligibility.details:
            details[str(item["ts_code"])] = item

    excluded: list[dict[str, Any]] = []
    candidates: list[str] = []
    for code in pool_members:
        if code not in version_codes:
            excluded.append(
                _report_notice(
                    "pool_member_not_in_data_version",
                    f"{code} 不在数据版本 {data_version_id} 的范围内",
                )
            )
            continue
        detail = details.get(code)
        if detail is None:
            excluded.append(
                _report_notice(
                    "support_status_unknown",
                    f"{code} 缺少绑定时用途资格判定；fail-closed 不纳入候选",
                )
            )
            continue
        history_days = detail.get("history_trade_days")
        if detail.get("reason_code") != "ok":
            excluded.append(
                _report_notice(
                    f"signal_eligibility_{detail.get('reason_code')}",
                    f"{code}: " + "；".join(detail.get("notes") or []) or "用途资格判定未通过",
                )
            )
            continue
        if detail.get("stale"):
            excluded.append(
                _report_notice(
                    "signal_price_stale",
                    f"{code}: " + "；".join(detail.get("notes") or []),
                )
            )
            continue
        if not isinstance(history_days, int) or history_days < 120:
            excluded.append(
                _report_notice(
                    "signal_history_insufficient_observations",
                    f"{code}: 有效观察 {history_days!r} 条，基线要求 >= 120",
                )
            )
            continue
        candidates.append(code)
    candidates.sort()

    signals: list[SignalResult] = [calculator.calculate(view=view, as_of=as_of, ts_code=code) for code in candidates]
    selection = selection_policy.select(signals)
    allocation = allocation_policy.allocate(
        AllocationRequest(
            selected=tuple(selection.selected),
            cash_cents=int(account_state.get("cash_cents") or 0),
            bundle_content=bundle["content"],
            bundle_version=bundle["version"],
        )
    )

    selected_set = set(selection.selected)
    held_positions = [p for p in account_state["positions"] if int(p["shares"]) > 0]
    holdings_assessment: list[dict[str, Any]] = []
    for pos in held_positions:
        code = str(pos["ts_code"])
        if code in selected_set:
            holdings_assessment.append(
                {
                    "ts_code": code,
                    "shares": int(pos["shares"]),
                    "in_selected_target": True,
                    "separate_assessment": False,
                    "assumed_sellable": False,
                    "auto_liquidated": False,
                }
            )
            continue
        valuation = valuation_price(
            conn,
            source_code=source_code,
            ts_code=code,
            business_date=as_of,
            as_of=as_of,
            view=view,
        )
        shares = int(pos["shares"])
        if valuation.price is None:
            market_value_cents: int | None = None
            value_basis: str | None = "valuation_reference_missing_price"
        else:
            market_value_cents = int(
                (
                    Decimal(str(valuation.price)) * Decimal(shares) * Decimal(100)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            value_basis = "valuation_reference_floating_point_storage_rounded_half_up_cents"
        holdings_assessment.append(
            {
                "ts_code": code,
                "shares": shares,
                "in_selected_target": False,
                "separate_assessment": True,
                "assumed_sellable": False,
                "auto_liquidated": False,
                "market_value_cents": market_value_cents,
                "value_basis": value_basis,
                "valuation_price_date": valuation.price_date,
                "valuation_notes": list(valuation.notes),
            }
        )
    if holdings_assessment:
        notices.append(
            _report_notice(
                "holdings_outside_selected_target_are_assessed_separately",
                "当前持仓不在目标内的证券只做信息展示；不视为可卖、不清仓、不承诺流动性",
            )
        )

    if context.restricted:
        notices.append(
            _report_notice(
                f"sync_context_restricted_{context.restriction_code}",
                context.restriction_detail or "绑定上下文处于受限状态；余下内容仅供研究分析",
            )
        )
    if selection.selected:
        notices.append(
            _report_notice(
                "ideal_target_requires_manual_confirmation",
                "理想目标与分析结论需要人工确认后才可进入后续阶段；本报告不自动执行任何交易",
            )
        )

    report = {
        "report_version": TARGET_REPORT_VERSION,
        "as_of": as_of,
        "source_code": source_code,
        "data_version_id": data_version_id,
        "pool_snapshot_id": pool_snapshot_id,
        "account_revision": bound_revision,
        "restricted": context.restricted,
        "restriction_code": context.restriction_code,
        "restriction_detail": context.restriction_detail,
        "components": {
            "signal": {"name": calculator.code, "version": calculator.version},
            "selection": {"name": selection_policy.code, "version": selection_policy.version},
            "allocation": {"name": allocation_policy.code, "version": allocation_policy.version},
            "registries": {
                "signal_calculators": SIGNAL_CALCULATOR_REGISTRY.names(),
                "selection_policies": SELECTION_POLICY_REGISTRY.names(),
                "allocation_policies": ALLOCATION_POLICY_REGISTRY.names(),
            },
        },
        "input_digest": sha256_of_object(
            {
                "report_version": TARGET_REPORT_VERSION,
                "as_of": as_of,
                "source_code": source_code,
                "data_version_id": data_version_id,
                "pool_snapshot_id": pool_snapshot_id,
                "signal_name": calculator.code,
                "selection_name": selection_policy.code,
                "allocation_name": allocation_policy.code,
                "account_revision": bound_revision,
            }
        ),
        "candidates": candidates,
        "excluded": excluded,
        "signals": [signal.as_dict() for signal in signals],
        "selection": selection.as_dict(),
        "allocation": allocation.as_dict(),
        "holdings_assessment": holdings_assessment,
        "disclaimers": list(_DISCLAIMER_NOTICES),
        "notices": [dict(notice) for notice in notices],
    }
    return report
