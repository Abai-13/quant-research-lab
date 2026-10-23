"""阶段三 S3：选择/排序策略接口与冻结基线排序。

冻结基线（同派工口径）：

- 候选门槛：``momentum > 0`` 且 ``P(t) > trend``；其余候选显式排除并保留原因
  （momentum_not_positive / price_not_above_trend；计算器自身排除的原因原样上行）；
- 排序：momentum 降序，动量完全相同（Decimal 精确相等）时按证券代码升序；
- 只做确定性选择，不涉及现金/权重/约束，不声称任何收益、评分含义或最优性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, Sequence, runtime_checkable

from ..data.errors import DataError
from .registry import ComponentRegistry
from .signals import SignalResult

__all__ = [
    "SELECTION_BASELINE_NAME",
    "ExcludedCandidate",
    "SelectionResult",
    "SelectionPolicy",
    "BaselineMomentumRankSelection",
    "SELECTION_POLICY_REGISTRY",
]

SELECTION_BASELINE_NAME = "baseline_momentum_rank"


@runtime_checkable
class SelectionPolicy(Protocol):
    """可替换的选择策略：输入信号结果，输出确定性排序与逐项排除原因。"""

    code: str
    version: str

    def select(self, signals: Sequence[SignalResult]) -> "SelectionResult": ...


@dataclass(frozen=True)
class ExcludedCandidate:
    ts_code: str
    reason_code: str
    detail: str | None = None
    gate: str = "policy"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "gate": self.gate,
        }


@dataclass(frozen=True)
class SelectionResult:
    """一次确定性排序结果：被选序列恒按最终排序输出；排除逐项带原因。"""

    policy_code: str
    policy_version: str
    selected: tuple[str, ...]
    excluded: tuple[ExcludedCandidate, ...] = field(default=())
    notes: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_code": self.policy_code,
            "policy_version": self.policy_version,
            "selected": list(self.selected),
            "excluded": [item.as_dict() for item in self.excluded],
            "notes": list(self.notes),
        }


class BaselineMomentumRankSelection:
    """冻结基线：门槛过滤 + momentum 降序 + 代码升序平局规则。"""

    code = SELECTION_BASELINE_NAME
    version = "s3-selection-1.0"

    def select(self, signals: Sequence[SignalResult]) -> SelectionResult:
        by_code: dict[str, SignalResult] = {}
        for signal in signals:
            if signal.ts_code in by_code:
                raise DataError(
                    "candidate_signal_duplicate",
                    f"信号输入中存在重复证券 {signal.ts_code}",
                )
            by_code[signal.ts_code] = signal
        excluded: list[ExcludedCandidate] = []
        eligible: list[tuple[Decimal, str]] = []
        for ts_code in sorted(by_code):
            signal = by_code[ts_code]
            if not signal.ok:
                excluded.append(
                    ExcludedCandidate(
                        ts_code=ts_code,
                        reason_code=signal.reason_code or "signal_unavailable",
                        detail=signal.detail,
                        gate="calculator",
                    )
                )
                continue
            momentum = signal.momentum
            trend = signal.trend
            price_t = signal.price_t
            assert momentum is not None and trend is not None and price_t is not None
            if momentum <= Decimal("0"):
                excluded.append(
                    ExcludedCandidate(
                        ts_code=ts_code,
                        reason_code="momentum_not_positive",
                        detail=f"momentum={momentum}",
                        gate="policy",
                    )
                )
                continue
            if price_t <= trend:
                excluded.append(
                    ExcludedCandidate(
                        ts_code=ts_code,
                        reason_code="price_not_above_trend",
                        detail=f"P(t)={price_t} <= trend={trend}",
                        gate="policy",
                    )
                )
                continue
            eligible.append((momentum, ts_code))
        eligible.sort(key=lambda item: (-item[0], item[1]))
        return SelectionResult(
            policy_code=self.code,
            policy_version=self.version,
            selected=tuple(ts_code for _, ts_code in eligible),
            excluded=tuple(excluded),
            notes=(
                "deterministic_ordering_momentum_desc_code_asc_no_tie_randomization",
                "no_optimality_or_profitability_claim",
            ),
        )


# 模块级注册表：S3 只含冻结基线排序；替代策略按新名字登记。
SELECTION_POLICY_REGISTRY = ComponentRegistry(kind="SelectionPolicy")
SELECTION_POLICY_REGISTRY.register(SELECTION_BASELINE_NAME, BaselineMomentumRankSelection)
