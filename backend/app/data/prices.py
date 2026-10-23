"""三类价格用途：原始成交参考价、信号计算价、估值参考价。

三种价格分开提供，互不混用：

- **原始成交参考价**：来源的未复权 OHLC，保留币种与单位；不保证一定能成交，
  成交规则属于阶段 3；
- **信号计算价**：派生值，记录算法版本、基准日、因子来源与截止时点；
  只使用 ``trade_date <= as_of`` 的复权因子，不把未来公司行为倒灌进历史时点；
- **估值参考价**：指定业务日期可得的未复权价格，返回价格日期与陈旧原因。

缺失价格保持缺失（``None``），不填 0；停牌不伪造零成交的普通开市记录。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .errors import DataError


SIGNAL_ALGORITHM_VERSION = "sig-v1"

PRICE_PURPOSES: tuple[str, ...] = (
    "display",
    "signal_input",
    "execution_reference",
    "valuation_reference",
    "strict_historical_replay",
)


@dataclass
class PriceResult:
    """一次价格查询的结果。"""

    purpose: str
    ts_code: str
    as_of: str
    price_date: str | None = None
    price: float | None = None
    currency: str | None = None
    unit: str = "CNY"
    stale: bool = False
    stale_reason: str | None = None
    reason_code: str = "ok"
    algorithm_version: str | None = None
    factor_anchor_date: str | None = None
    factor_anchor: float | None = None
    series: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.price is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "ts_code": self.ts_code,
            "as_of": self.as_of,
            "price_date": self.price_date,
            "price": self.price,
            "currency": self.currency,
            "unit": self.unit,
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "reason_code": self.reason_code,
            "algorithm_version": self.algorithm_version,
            "factor_anchor_date": self.factor_anchor_date,
            "factor_anchor": self.factor_anchor,
            "series": self.series,
            "notes": self.notes,
        }


def _require_view_source(view, source_code: str) -> None:
    """固定版本视图的来源必须与调用方声明的来源一致。"""
    version_source = getattr(view, "source_code", None) if view is not None else None
    if version_source is not None and str(version_source) != source_code:
        raise DataError(
            "version_source_mismatch",
            f"价格查询声明的来源 {source_code} 与数据版本来源 {version_source} 不一致",
        )


def _usable_price(value: Any) -> float | None:
    """把已存储的价格值转成可用数字；不满足“有效有限正数”契约时返回缺失。

    布尔值与不合法类型不能静默变成数字；非正价格不是有效价格。
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number) or number <= 0:
        return None
    return number


def _latest_daily(
    conn: sqlite3.Connection,
    source_code: str,
    ts_code: str,
    *,
    on_or_before: str | None = None,
    view=None,
) -> sqlite3.Row | None:
    if view is not None:
        return view.latest_daily(ts_code, on_or_before=on_or_before)
    query = (
        "SELECT * FROM daily_price WHERE source_code=? AND ts_code=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM daily_price d2"
        "                    WHERE d2.source_code=daily_price.source_code"
        "                      AND d2.ts_code=daily_price.ts_code"
        "                      AND d2.trade_date=daily_price.trade_date)"
    )
    params: list[Any] = [source_code, ts_code]
    if on_or_before:
        query += " AND trade_date<=?"
        params.append(on_or_before)
    query += " ORDER BY trade_date DESC LIMIT 1"
    return conn.execute(query, params).fetchone()


def _daily_on(
    conn: sqlite3.Connection, source_code: str, ts_code: str, trade_date: str, *, view=None
) -> sqlite3.Row | None:
    if view is not None:
        return view.daily_on(ts_code, trade_date)
    return conn.execute(
        "SELECT * FROM daily_price WHERE source_code=? AND ts_code=? AND trade_date=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM daily_price d2"
        "                    WHERE d2.source_code=daily_price.source_code"
        "                      AND d2.ts_code=daily_price.ts_code"
        "                      AND d2.trade_date=daily_price.trade_date)",
        (source_code, ts_code, trade_date),
    ).fetchone()


def _factor_on(
    conn: sqlite3.Connection, source_code: str, ts_code: str, trade_date: str, *, view=None
) -> float | None:
    if view is not None:
        return view.factor_on(ts_code, trade_date)
    row = conn.execute(
        "SELECT adj_factor FROM adj_factor WHERE source_code=? AND ts_code=? AND trade_date=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM adj_factor a2"
        "                    WHERE a2.source_code=adj_factor.source_code"
        "                      AND a2.ts_code=adj_factor.ts_code"
        "                      AND a2.trade_date=adj_factor.trade_date)",
        (source_code, ts_code, trade_date),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _is_suspended(
    conn: sqlite3.Connection, source_code: str, ts_code: str, trade_date: str, *, view=None
) -> bool:
    if view is not None:
        return view.is_suspended(ts_code, trade_date)
    row = conn.execute(
        "SELECT event_type, detail FROM suspension_event WHERE source_code=? AND ts_code=? AND trade_date=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM suspension_event e2"
        "                    WHERE e2.source_code=suspension_event.source_code"
        "                      AND e2.ts_code=suspension_event.ts_code"
        "                      AND e2.trade_date=suspension_event.trade_date"
        "                      AND e2.event_type=suspension_event.event_type)",
        (source_code, ts_code, trade_date),
    ).fetchone()
    if row is None:
        return False
    return str(row[0]) == "S" and not row[1]


def _is_trading_day(
    conn: sqlite3.Connection, source_code: str, trade_date: str, *, view=None
) -> bool | None:
    if view is not None:
        return view.is_trading_day(trade_date)
    row = conn.execute(
        "SELECT is_open FROM trade_calendar WHERE source_code=? AND cal_date=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM trade_calendar c2"
        "                    WHERE c2.source_code=trade_calendar.source_code"
        "                      AND c2.exchange=trade_calendar.exchange"
        "                      AND c2.cal_date=trade_calendar.cal_date)",
        (source_code, trade_date),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]) == 1


def raw_execution_price(
    conn: sqlite3.Connection,
    *,
    source_code: str,
    ts_code: str,
    trade_date: str,
    view=None,
) -> PriceResult:
    """原始成交参考价：指定交易日的未复权收盘价与开高低。"""
    _require_view_source(view, source_code)
    result = PriceResult(purpose="execution_reference", ts_code=ts_code, as_of=trade_date)
    row = _daily_on(conn, source_code, ts_code, trade_date, view=view)
    trading_day = _is_trading_day(conn, source_code, trade_date, view=view)
    suspended = _is_suspended(conn, source_code, ts_code, trade_date, view=view)

    if trading_day is None:
        result.reason_code = "calendar_unknown"
        result.notes.append("缺少该日期的交易日历证据")
    elif not trading_day:
        result.reason_code = "not_a_trading_day"
        result.notes.append("该日期不是交易日")
    elif suspended:
        result.reason_code = "suspended"
        result.notes.append("该证券当日全日停牌，不能作为可成交参考价")
    elif row is None:
        result.reason_code = "missing_price"
        result.notes.append("该交易日没有行情记录；缺失价格不填 0")

    if row is not None:
        result.price_date = trade_date
        close = row["close"]
        result.price = _usable_price(close)
        result.currency = row["currency"]
        result.unit = "CNY"
        if result.price is None and result.reason_code == "ok":
            if close is None:
                result.reason_code = "missing_price"
                result.notes.append("行情缺少收盘价")
            else:
                result.reason_code = "invalid_price"
                result.notes.append(
                    f"行情收盘价 {close!r} 不是有效正数价格，不能作为成交参考"
                )
    return result


def adjusted_series(
    conn: sqlite3.Connection,
    *,
    source_code: str,
    ts_code: str,
    as_of: str,
    algorithm_version: str = SIGNAL_ALGORITHM_VERSION,
    view=None,
) -> tuple[list[dict[str, Any]], float | None, str | None]:
    """按截止时点计算复权序列。

    只使用 ``trade_date <= as_of`` 的复权因子，并以 ``as_of`` 当日因子为基准重定基，
    因此未来公司行为不会倒灌到历史时点。返回 (序列, 基准因子, 基准日期)。
    """
    _require_view_source(view, source_code)
    anchor = _factor_on(conn, source_code, ts_code, as_of, view=view)
    if anchor is None or anchor == 0:
        return [], None, None
    if view is not None:
        rows = [
            (row["trade_date"], row["close"])
            for row in view.daily_series(ts_code, on_or_before=as_of)
            if row["close"] is not None
        ]
        factors = dict(view.factor_series(ts_code, on_or_before=as_of))
    else:
        rows = conn.execute(
            "SELECT trade_date, close FROM daily_price WHERE source_code=? AND ts_code=? AND trade_date<=?"
            " AND close IS NOT NULL"
            " AND revision_no = (SELECT MAX(revision_no) FROM daily_price d2"
            "                    WHERE d2.source_code=daily_price.source_code"
            "                      AND d2.ts_code=daily_price.ts_code"
            "                      AND d2.trade_date=daily_price.trade_date)"
            " ORDER BY trade_date",
            (source_code, ts_code, as_of),
        ).fetchall()
        factors = None
    series: list[dict[str, Any]] = []
    for trade_date, close in rows:
        usable_close = _usable_price(close)
        if usable_close is None:
            # 不合法或非正的收盘价不能进入信号计算；缺失保持缺失。
            continue
        factor = factors.get(trade_date) if factors is not None else _factor_on(
            conn, source_code, ts_code, trade_date
        )
        if factor is None or factor == 0:
            continue
        series.append(
            {
                "trade_date": trade_date,
                "price": usable_close * float(factor) / float(anchor),
                "raw_close": usable_close,
                "factor": float(factor),
            }
        )
    return series, anchor, as_of


def signal_price(
    conn: sqlite3.Connection,
    *,
    source_code: str,
    ts_code: str,
    as_of: str,
    algorithm_version: str = SIGNAL_ALGORITHM_VERSION,
    view=None,
) -> PriceResult:
    """信号计算价：截止 ``as_of`` 的复权价格与其派生序列。"""
    _require_view_source(view, source_code)
    result = PriceResult(
        purpose="signal_input",
        ts_code=ts_code,
        as_of=as_of,
        algorithm_version=algorithm_version,
        unit="CNY",
    )
    series, anchor, anchor_date = adjusted_series(
        conn,
        source_code=source_code,
        ts_code=ts_code,
        as_of=as_of,
        algorithm_version=algorithm_version,
        view=view,
    )
    if anchor is None:
        result.reason_code = "adjustment_factor_missing"
        result.notes.append(f"缺少 {as_of} 的复权因子，无法生成信号计算价")
        return result
    result.factor_anchor = anchor
    result.factor_anchor_date = anchor_date
    result.series = series
    if not series:
        result.reason_code = "missing_price"
        result.notes.append("截止时点之前没有可用行情")
        return result
    last = series[-1]
    result.price_date = last["trade_date"]
    result.price = last["price"]
    result.currency = "CNY"
    if last["trade_date"] != as_of:
        result.stale = True
        result.stale_reason = "no_quote_on_as_of"
        result.notes.append(f"截止时点 {as_of} 当日无行情，价格日期为 {last['trade_date']}")
    else:
        result.notes.append("当日因子在次日开盘前才可发布，跨时点使用需人工确认")
    return result


def valuation_price(
    conn: sqlite3.Connection,
    *,
    source_code: str,
    ts_code: str,
    business_date: str,
    as_of: str,
    view=None,
) -> PriceResult:
    """估值参考价：指定业务日期可得的未复权收盘价，并给出陈旧信息。"""
    _require_view_source(view, source_code)
    result = PriceResult(purpose="valuation_reference", ts_code=ts_code, as_of=as_of)
    row = _latest_daily(conn, source_code, ts_code, on_or_before=business_date, view=view)
    if row is None:
        result.reason_code = "missing_price"
        result.notes.append("指定业务日期之前没有行情记录；缺失价格不填 0")
        return result
    result.price_date = str(row["trade_date"])
    close = row["close"]
    result.price = _usable_price(close)
    result.currency = row["currency"]
    result.unit = "CNY"
    if result.price is None and close is not None:
        result.notes.append(
            f"价格 {close!r} 不是有效正数价格，视为缺失且不能作为估值依据"
        )
    if result.price_date != business_date:
        result.stale = True
        result.stale_reason = "price_date_before_business_date"
        result.notes.append(f"业务日期 {business_date} 没有行情，使用 {result.price_date} 的价格")
    elif business_date > as_of:
        result.stale = True
        result.stale_reason = "business_date_after_as_of"
        result.notes.append("业务日期晚于截止时点，估值信息超出可用范围")
    return result


def price_for_purpose(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    source_code: str,
    ts_code: str,
    as_of: str,
    business_date: str | None = None,
    view=None,
) -> PriceResult:
    """按用途分发到具体价格函数。"""
    _require_view_source(view, source_code)
    if purpose == "signal_input" or purpose == "strict_historical_replay":
        return signal_price(
            conn, source_code=source_code, ts_code=ts_code, as_of=as_of, view=view
        )
    if purpose == "execution_reference":
        return raw_execution_price(
            conn,
            source_code=source_code,
            ts_code=ts_code,
            trade_date=business_date or as_of,
            view=view,
        )
    if purpose == "valuation_reference":
        return valuation_price(
            conn,
            source_code=source_code,
            ts_code=ts_code,
            business_date=business_date or as_of,
            as_of=as_of,
            view=view,
        )
    if purpose == "display":
        return valuation_price(
            conn,
            source_code=source_code,
            ts_code=ts_code,
            business_date=business_date or as_of,
            as_of=as_of,
            view=view,
        )
    raise ValueError(f"未知价格用途: {purpose}")


__all__ = [
    "PRICE_PURPOSES",
    "PriceResult",
    "SIGNAL_ALGORITHM_VERSION",
    "adjusted_series",
    "price_for_purpose",
    "raw_execution_price",
    "signal_price",
    "valuation_price",
]
