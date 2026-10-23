"""阶段三 S3：信号计算接口（Protocol）与冻结基线信号。

冻结基线（本派工口径，日期/索引均为"有效观察序列"下标）：

- 信号价 ``P`` 只来自阶段二 ``signal_price``（``purpose=signal_input``）：以
  ``as_of`` 当日复权因子为同一锚重定基的序列，只包含 ``trade_date<=as_of``
  的实际行情行——无前视、无前向填充、无效价格不进入序列；
- 动量 ``momentum = P(t-5)/P(t-65) - 1``；趋势 ``trend = mean(P(t-119)..P(t))``；
- 必须具备至少 120 个有效观察（不足则显式排除并记录观察数；缺失/停牌行
  就是观察缺失，不用前一行情填）；
- 本模块只计算数值（momentum/trend/锚定价格），门槛过滤与排序属于
  SelectionPolicy 职责；计算过程绝不访问除已固定数据版本以外的任何数据，
  不做 SQL 旁路，不从前缀推断支持性。

数值一律 ``Decimal``（由存储浮点经 ``str`` 精确转换），公开结果原样保留
可重复的小数形态，不隐藏舍入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, Sequence, runtime_checkable

from ..data.prices import price_for_purpose
from .registry import ComponentRegistry

__all__ = [
    "SIGNAL_BASELINE_NAME",
    "SIGNAL_ANCHOR_NOTE",
    "SIGNAL_MIN_OBSERVATIONS",
    "SignalResult",
    "SignalCalculator",
    "BaselineMomentumTrendCalculator",
    "SIGNAL_CALCULATOR_REGISTRY",
    "latest_observation_offsets",
    "validate_offset_indices",
]

SIGNAL_BASELINE_NAME = "baseline_momentum5_trend120"

SIGNAL_ANCHOR_NOTE = (
    "signal prices are the phase-2 signal_input series: adjusted by the anchor factor "
    "on as_of, strictly trade_date<=as_of, no forward fill, no invalid prices"
)
SIGNAL_MIN_OBSERVATIONS = 120

_OFFSET_SHORT = 5
_OFFSET_LONG = 65
_OFFSET_TREND = 119


@runtime_checkable
class SignalCalculator(Protocol):
    """一个可替换的信号计算器：一次只算一只证券，接口保持最小。"""

    code: str
    version: str

    def calculate(self, *, view: Any, as_of: str, ts_code: str) -> "SignalResult": ...


@dataclass(frozen=True)
class SignalResult:
    """单个证券一次信号计算的结果（含显式排除原因）。"""

    ts_code: str
    as_of: str
    calculator_code: str
    calculator_version: str
    ok: bool
    reason_code: str | None
    detail: str | None
    momentum: Decimal | None
    trend: Decimal | None
    price_t: Decimal | None
    price_date_t: str | None
    observation_count: int
    anchor_date: str | None = None
    anchor_factor: float | None = None
    notes: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "as_of": self.as_of,
            "calculator_code": self.calculator_code,
            "calculator_version": self.calculator_version,
            "ok": self.ok,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "momentum": None if self.momentum is None else str(self.momentum),
            "trend": None if self.trend is None else str(self.trend),
            "price_t": None if self.price_t is None else str(self.price_t),
            "price_date_t": self.price_date_t,
            "observation_count": self.observation_count,
            "anchor_date": self.anchor_date,
            "anchor_factor": self.anchor_factor,
            "anchor_note": SIGNAL_ANCHOR_NOTE,
            "notes": list(self.notes),
        }


def _excluded(
    ts_code: str,
    as_of: str,
    code: str,
    version: str,
    reason_code: str,
    detail: str,
    observation_count: int,
    *,
    notes: Sequence[str] = (),
) -> SignalResult:
    return SignalResult(
        ts_code=ts_code,
        as_of=as_of,
        calculator_code=code,
        calculator_version=version,
        ok=False,
        reason_code=reason_code,
        detail=detail,
        momentum=None,
        trend=None,
        price_t=None,
        price_date_t=None,
        observation_count=observation_count,
        notes=tuple(notes),
    )


def latest_observation_offsets(offset: int, observation_count: int) -> int | None:
    """按有效观察条数换算下标：``t - offset``；越界返回 None（绝不回补数据）。"""
    index = observation_count - 1 - offset
    if index < 0:
        return None
    return index


def validate_offset_indices(observation_count: int, required: int = SIGNAL_MIN_OBSERVATIONS) -> bool:
    """索引边界：t-119..t 连续 120 个观察存在，即 observation_count>=120。"""
    return observation_count >= required


class BaselineMomentumTrendCalculator:
    """冻结基线信号：动量 + 120 日趋势（只用阶段二信号计算价）。"""

    code = SIGNAL_BASELINE_NAME
    version = "s3-signal-1.0"

    def calculate(self, *, view: Any, as_of: str, ts_code: str) -> SignalResult:
        conn = view.conn
        source_code = view.source_code
        price = price_for_purpose(
            conn,
            purpose="signal_input",
            source_code=source_code,
            ts_code=ts_code,
            as_of=as_of,
            view=view,
        )
        if price.price is None:
            return _excluded(
                ts_code,
                as_of,
                self.code,
                self.version,
                f"signal_price_{price.reason_code}",
                "；".join(price.notes) or "信号计算价不可用",
                observation_count=len(price.series),
            )
        if price.stale:
            return _excluded(
                ts_code,
                as_of,
                self.code,
                self.version,
                "signal_price_stale",
                f"截至 {as_of} 当日无行情（最后价格日 {price.price_date}）；"
                "基线信号要求截止时点当日有锚定观察",
                observation_count=len(price.series),
                notes=price.notes,
            )
        series = [
            (str(item["trade_date"]), Decimal(str(item["price"])))
            for item in price.series
        ]
        count = len(series)
        if not validate_offset_indices(count):
            return _excluded(
                ts_code,
                as_of,
                self.code,
                self.version,
                "signal_history_insufficient_observations",
                f"有效观察 {count} 条，基线要求 >= {SIGNAL_MIN_OBSERVATIONS} 条；"
                "缺失观察不前向填充",
                observation_count=count,
            )
        index_t = count - 1
        index_t_short = latest_observation_offsets(_OFFSET_SHORT, count)
        index_t_long = latest_observation_offsets(_OFFSET_LONG, count)
        index_t_trend = latest_observation_offsets(_OFFSET_TREND, count)
        assert index_t_short is not None and index_t_long is not None and index_t_trend is not None
        price_t = series[index_t][1]
        price_t_short = series[index_t_short][1]
        price_t_long = series[index_t_long][1]
        window = series[index_t_trend : index_t + 1]
        trend = sum((item[1] for item in window), Decimal("0")) / Decimal(len(window))
        momentum = price_t_short / price_t_long - Decimal("1")
        return SignalResult(
            ts_code=ts_code,
            as_of=as_of,
            calculator_code=self.code,
            calculator_version=self.version,
            ok=True,
            reason_code=None,
            detail=None,
            momentum=momentum,
            trend=trend,
            price_t=price_t,
            price_date_t=series[index_t][0],
            observation_count=count,
            anchor_date=price.factor_anchor_date,
            anchor_factor=price.factor_anchor,
        )


# 模块级注册表：S3 只含冻结基线；替代实现按新名字登记。
SIGNAL_CALCULATOR_REGISTRY = ComponentRegistry(kind="SignalCalculator")
SIGNAL_CALCULATOR_REGISTRY.register(SIGNAL_BASELINE_NAME, BaselineMomentumTrendCalculator)
