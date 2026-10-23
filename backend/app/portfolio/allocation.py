"""阶段三 S3：理想目标分配策略接口与冻结基线分配（版本化约束、现金余量）。

冻结基线（同派工口径，fail-closed）：

- 唯一约束来源是版本化 ``rule_bundle`` 内容中的 ``target_constraints`` 块；
  块缺失时**不把 ``target_preferences`` 里 ``hint_only=True`` 的提示伪装成硬
  约束**——返回受限结果且不分配任何目标（原因
  ``target_constraints_not_versioned``）；
- 支持的约束字段：``cash_reserve_cents``（版本化整数分，>=0）与
  ``per_security_weight_cap_bp``（1..10000）；其余字段未知直接拒绝；
  ``turnover_budget_bp`` 按冻结语义属于 S4 调仓域：出现即显式拒绝
  （``s3_turnover_budget_unimplemented``），不估算、不静默忽略；
- 分配口径：投资额 = 现金 - 现金保留；每对象目标权重 = min(等权 1/k,
  单一上限 cap_bp/10000)；目标金额向下取整到分；投不出的现金是**合法余量**
  而不是错误；保留/分配/余量对账恒等：reserve + Σtarget + residual == cash；
- 输出显式携带冲突与通知；绝不声明最优性或盈利性，也不构成投资建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..data.errors import DataError
from .registry import ComponentRegistry

__all__ = [
    "ALLOCATION_BASELINE_NAME",
    "TARGET_CONSTRAINTS_KEY",
    "TargetConstraints",
    "parse_target_constraints",
    "AllocationRequest",
    "AllocationTarget",
    "AllocationResult",
    "AllocationPolicy",
    "BaselineEqualWeightCappedAllocation",
    "ALLOCATION_POLICY_REGISTRY",
]

ALLOCATION_BASELINE_NAME = "baseline_equal_weight_capped"

TARGET_CONSTRAINTS_KEY = "target_constraints"

_ALLOWED_CONSTRAINT_FIELDS = ("cash_reserve_cents", "per_security_weight_cap_bp")
_UNSUPPORTED_CONSTRAINT_FIELDS = ("turnover_budget_bp",)


@dataclass(frozen=True)
class TargetConstraints:
    """一次显式版本化约束集（已通过边界校验）。"""

    cash_reserve_cents: int
    per_security_weight_cap_bp: int
    source_bundle_version: str | None

    @property
    def cap_fraction(self) -> Decimal:
        return Decimal(self.per_security_weight_cap_bp) / Decimal(10_000)


def parse_target_constraints(
    bundle_content: Mapping[str, Any] | None,
    *,
    bundle_version: str | None = None,
) -> TargetConstraints | None:
    """解析版本化约束块；块缺失返回 None，字段/边界非法则显式拒绝。"""
    if bundle_content is None:
        return None
    if not isinstance(bundle_content, Mapping):
        raise DataError(
            "target_constraints_invalid",
            f"规则组合内容不是映射: {type(bundle_content)!r}",
        )
    if TARGET_CONSTRAINTS_KEY not in bundle_content:
        return None
    block = bundle_content[TARGET_CONSTRAINTS_KEY]
    if not isinstance(block, Mapping):
        raise DataError(
            "target_constraints_invalid",
            f"target_constraints 必须是映射，当前 {type(block)!r}",
        )
    unknown = sorted(set(block) - set(_ALLOWED_CONSTRAINT_FIELDS) - set(_UNSUPPORTED_CONSTRAINT_FIELDS))
    if unknown:
        raise DataError(
            "target_constraints_field_unknown",
            f"target_constraints 含未登记字段: {unknown}；"
            f"仅支持 {sorted(_ALLOWED_CONSTRAINT_FIELDS)} "
            f"（{list(_UNSUPPORTED_CONSTRAINT_FIELDS)} 在 S3 中显式未实现）",
        )
    if "turnover_budget_bp" in block:
        raise DataError(
            "s3_turnover_budget_unimplemented",
            "turnover_budget_bp 属于调仓/成交域（S4）的冻结约束；"
            "S3 理想目标分配不实现换手预算折算，按 fail-closed 显式拒绝而不是忽略",
        )
    reserve = _constraint_int(block, "cash_reserve_cents", minimum=0)
    cap = _constraint_int(block, "per_security_weight_cap_bp", minimum=1)
    if cap > 10_000:
        raise DataError(
            "target_constraints_field_invalid",
            f"per_security_weight_cap_bp 必须 <= 10000，当前 {cap}",
        )
    return TargetConstraints(
        cash_reserve_cents=reserve,
        per_security_weight_cap_bp=cap,
        source_bundle_version=bundle_version,
    )


def _constraint_int(block: Mapping[str, Any], key: str, *, minimum: int) -> int:
    value = block.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataError(
            "target_constraints_field_invalid",
            f"{key} 必须是整数（不接受浮点/布尔）: {value!r}",
        )
    if value < minimum:
        raise DataError(
            "target_constraints_field_invalid",
            f"{key} 必须 >= {minimum}，当前 {value}",
        )
    return int(value)


@dataclass(frozen=True)
class AllocationRequest:
    """分配输入：被选（已排序）候选、现金与规则组合内容。

    ``bundle_content=None`` 等价于"规则组合未提供约束块"，由策略决定 fail-closed
    行为（冻结基线：受理但输出受限结果）。
    """

    selected: tuple[str, ...]
    cash_cents: int
    bundle_content: Mapping[str, Any] | None
    bundle_version: str | None = None


@dataclass(frozen=True)
class AllocationTarget:
    ts_code: str
    weight: Decimal
    weight_bp: int
    target_value_cents: int
    cap_bound: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "weight": str(self.weight),
            "weight_bp": self.weight_bp,
            "target_value_cents": self.target_value_cents,
            "cap_bound": self.cap_bound,
        }


@dataclass(frozen=True)
class AllocationConflict:
    code: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class AllocationResult:
    """分配输出：目标、保留、余量、冲突与通知；对账恒等可逐分核对。"""

    policy_code: str
    policy_version: str
    ok: bool
    restricted: bool
    reason_code: str | None
    targets: tuple[AllocationTarget, ...]
    cash_cents: int
    reserved_cents: int
    investable_cents: int | None
    final_cash_cents: int
    conflicts: tuple[AllocationConflict, ...] = field(default=())
    notices: tuple[str, ...] = field(default=())
    constraints_source_version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "policy_code": self.policy_code,
            "policy_version": self.policy_version,
            "ok": self.ok,
            "restricted": self.restricted,
            "reason_code": self.reason_code,
            "targets": [item.as_dict() for item in self.targets],
            "cash_cents": self.cash_cents,
            "reserved_cents": self.reserved_cents,
            "investable_cents": self.investable_cents,
            "final_cash_cents": self.final_cash_cents,
            "conflicts": [item.as_dict() for item in self.conflicts],
            "notices": list(self.notices),
            "constraints_source_version": self.constraints_source_version,
        }
        return payload


@runtime_checkable
class AllocationPolicy(Protocol):
    """可替换的分配策略：输入被选候选/现金/版本化约束内容，输出分配报告。"""

    code: str
    version: str

    def allocate(self, request: AllocationRequest) -> AllocationResult: ...


class BaselineEqualWeightCappedAllocation:
    """冻结基线：等权目标 + 单一上限 + 版本化现金保留；现金为合法余量。"""

    code = ALLOCATION_BASELINE_NAME
    version = "s3-allocation-1.0"

    def allocate(self, request: AllocationRequest) -> AllocationResult:
        if isinstance(request.cash_cents, bool) or not isinstance(request.cash_cents, int):
            raise DataError(
                "allocation_input_invalid",
                f"cash_cents 必须是整数分: {request.cash_cents!r}",
            )
        if request.cash_cents < 0:
            raise DataError(
                "allocation_input_invalid",
                f"cash_cents 非负: {request.cash_cents}",
            )
        constraints = parse_target_constraints(
            request.bundle_content,
            bundle_version=request.bundle_version,
        )
        if constraints is None:
            return AllocationResult(
                policy_code=self.code,
                policy_version=self.version,
                ok=True,
                restricted=True,
                reason_code="target_constraints_not_versioned",
                targets=(),
                cash_cents=request.cash_cents,
                reserved_cents=0,
                investable_cents=None,
                final_cash_cents=request.cash_cents,
                conflicts=(),
                notices=(
                    "parameter bundle provides no target_constraints block; "
                    "hint_only preferences are never coerced into hard constraints; "
                    "allocation is restricted to cash until a versioned constraint set exists",
                    "no_optimality_or_profitability_claim",
                ),
            )
        reserve = constraints.cash_reserve_cents
        cap_fraction = constraints.cap_fraction
        if reserve > request.cash_cents:
            conflict = AllocationConflict(
                code="constraint_cash_reserve_exceeds_cash",
                detail=f"现金保留 {reserve} 分超过现金 {request.cash_cents} 分；不分配任何目标",
            )
            return AllocationResult(
                policy_code=self.code,
                policy_version=self.version,
                ok=False,
                restricted=False,
                reason_code="constraint_cash_reserve_exceeds_cash",
                targets=(),
                cash_cents=request.cash_cents,
                reserved_cents=0,
                investable_cents=None,
                final_cash_cents=request.cash_cents,
                conflicts=(conflict,),
                notices=("no_optimality_or_profitability_claim",),
                constraints_source_version=request.bundle_version,
            )
        investable = request.cash_cents - reserve
        selected = list(request.selected)
        if not selected:
            return AllocationResult(
                policy_code=self.code,
                policy_version=self.version,
                ok=True,
                restricted=False,
                reason_code=None,
                targets=(),
                cash_cents=request.cash_cents,
                reserved_cents=reserve,
                investable_cents=investable,
                final_cash_cents=request.cash_cents,
                conflicts=(),
                notices=(
                    "no_candidates_selected_cash_unchanged",
                    "no_optimality_or_profitability_claim",
                ),
                constraints_source_version=request.bundle_version,
            )
        equal_weight = Decimal(1) / Decimal(len(selected))
        cap_bound = cap_fraction < equal_weight
        weight = cap_fraction if cap_bound else equal_weight
        raw_total = (weight * Decimal(investable)).to_integral_value(rounding=ROUND_FLOOR)
        share = int(raw_total)
        targets = tuple(
            AllocationTarget(
                ts_code=ts_code,
                weight=weight,
                weight_bp=int((weight * Decimal(10_000)).to_integral_value(rounding=ROUND_FLOOR)),
                target_value_cents=share,
                cap_bound=cap_bound,
            )
            for ts_code in sorted(selected)
        )
        allocated_total = share * len(targets)
        residual = request.cash_cents - reserve - allocated_total
        return AllocationResult(
            policy_code=self.code,
            policy_version=self.version,
            ok=True,
            restricted=False,
            reason_code=None,
            targets=targets,
            cash_cents=request.cash_cents,
            reserved_cents=reserve,
            investable_cents=investable,
            final_cash_cents=residual,
            conflicts=(),
            notices=(
                (
                    "per_security_cap_active_below_equal_weight"
                    if cap_bound
                    else "per_security_cap_not_binding"
                ),
                "cash_residual_after_allocation_is_a_valid_result_not_an_error",
                "weights_are_equal_weight_baseline_not_optimal",
                "no_optimality_or_profitability_claim",
            ),
            constraints_source_version=request.bundle_version,
        )


# 模块级注册表：S3 只含冻结基线分配；替代策略按新名字登记。
ALLOCATION_POLICY_REGISTRY = ComponentRegistry(kind="AllocationPolicy")
ALLOCATION_POLICY_REGISTRY.register(ALLOCATION_BASELINE_NAME, BaselineEqualWeightCappedAllocation)
