"""接口清单与取数范围（Scope Unit）构造。

一个范围单元 = “某个接口 + 某组请求参数”，对应一次逻辑取数（可多页）。
接口与数据类别的对应关系固定在这里，避免在管线里散落字符串。

范围约定：

- 交易日历按日期区间一次性取回；
- 股票基础资料按上市状态（L 上市 / D 退市）分别取回——只用当前在市名单不能充当历史证券全集；
- 日线默认按交易日期取全市场，也可按固定样本证券逐只取；
- 复权因子、停复牌、分红送转按证券逐只取。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .records import (
    CATEGORY_ADJ_FACTOR,
    CATEGORY_CALENDAR,
    CATEGORY_CORPORATE_ACTION,
    CATEGORY_DAILY,
    CATEGORY_SECURITY,
    CATEGORY_SUSPENSION,
)

INTERFACE_CATEGORY: dict[str, str] = {
    "trade_cal": CATEGORY_CALENDAR,
    "stock_basic": CATEGORY_SECURITY,
    "daily": CATEGORY_DAILY,
    "adj_factor": CATEGORY_ADJ_FACTOR,
    "suspend_d": CATEGORY_SUSPENSION,
    "dividend": CATEGORY_CORPORATE_ACTION,
}

CATEGORY_INTERFACES: dict[str, str] = {
    category: interface for interface, category in INTERFACE_CATEGORY.items()
}

DEFAULT_EXCHANGE = "SSE"
STOCK_BASIC_LIST_STATUSES: tuple[str, ...] = ("L", "D")


@dataclass(frozen=True)
class ScopeUnit:
    """一次逻辑取数范围。"""

    interface: str
    category: str
    params: dict[str, Any]
    label: str

    def key(self) -> str:
        parts = [self.interface] + [f"{k}={v}" for k, v in sorted(self.params.items())]
        return "|".join(parts)


def _sorted_codes(codes: Iterable[str]) -> list[str]:
    return sorted({code.strip() for code in codes if code and code.strip()})


def build_scope_units(
    *,
    categories: Sequence[str],
    start: str,
    end: str,
    codes: Sequence[str] = (),
    trade_dates: Sequence[str] = (),
    exchange: str = DEFAULT_EXCHANGE,
    daily_by_code: bool = True,
) -> list[ScopeUnit]:
    """构造范围单元列表。``trade_dates`` 用于日线按交易日取全市场。"""
    units: list[ScopeUnit] = []
    selected = set(categories)
    code_list = _sorted_codes(codes)

    if CATEGORY_CALENDAR in selected:
        units.append(
            ScopeUnit(
                interface="trade_cal",
                category=CATEGORY_CALENDAR,
                params={"exchange": exchange, "start_date": start, "end_date": end},
                label=f"交易日历 {exchange} {start}~{end}",
            )
        )

    if CATEGORY_SECURITY in selected:
        for status in STOCK_BASIC_LIST_STATUSES:
            units.append(
                ScopeUnit(
                    interface="stock_basic",
                    category=CATEGORY_SECURITY,
                    params={"list_status": status},
                    label=f"股票基础资料 list_status={status}",
                )
            )

    if CATEGORY_DAILY in selected:
        if daily_by_code and code_list:
            for code in code_list:
                units.append(
                    ScopeUnit(
                        interface="daily",
                        category=CATEGORY_DAILY,
                        params={"ts_code": code, "start_date": start, "end_date": end},
                        label=f"日线 {code} {start}~{end}",
                    )
                )
        else:
            for trade_date in sorted(set(trade_dates)):
                units.append(
                    ScopeUnit(
                        interface="daily",
                        category=CATEGORY_DAILY,
                        params={"trade_date": trade_date},
                        label=f"日线 {trade_date} 全市场",
                    )
                )

    if CATEGORY_ADJ_FACTOR in selected:
        for code in code_list:
            units.append(
                ScopeUnit(
                    interface="adj_factor",
                    category=CATEGORY_ADJ_FACTOR,
                    params={"ts_code": code, "start_date": start, "end_date": end},
                    label=f"复权因子 {code} {start}~{end}",
                )
            )

    if CATEGORY_SUSPENSION in selected:
        for code in code_list:
            units.append(
                ScopeUnit(
                    interface="suspend_d",
                    category=CATEGORY_SUSPENSION,
                    params={"ts_code": code, "start_date": start, "end_date": end},
                    label=f"停复牌 {code} {start}~{end}",
                )
            )

    if CATEGORY_CORPORATE_ACTION in selected:
        for code in code_list:
            units.append(
                ScopeUnit(
                    interface="dividend",
                    category=CATEGORY_CORPORATE_ACTION,
                    params={"ts_code": code},
                    label=f"分红送转 {code}",
                )
            )

    return units


def scope_summary(units: Sequence[ScopeUnit]) -> dict[str, Any]:
    """范围摘要（写入 import_batch.scope）。"""
    by_category: dict[str, list[str]] = {}
    for unit in units:
        by_category.setdefault(unit.category, []).append(unit.label)
    return {
        "unit_count": len(units),
        "categories": sorted(by_category),
        "labels": by_category,
    }


def unit_params_for_log(unit: ScopeUnit) -> Mapping[str, Any]:
    return dict(unit.params)
