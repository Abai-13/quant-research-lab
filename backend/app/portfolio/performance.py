"""阶段三 S5b：估值快照、净资产与收益/回撤。

冻结口径（阶段三 §8）：

- ``NAV = 现金 + 持仓市值 + 应收 - 应付``。持仓无法可靠估值时返回
  ``partial_unknown``，不把未知当 0；已知部分单列。
- 无外部现金流：``r = NAV_end / NAV_previous - 1``；有外部现金流 C 时：
  ``r = NAV_end / (NAV_previous + C) - 1``，仅当分母为正且估值完整。
- 现金分红在除权后、派息前是应收；只有税后净额确认才计入 NAV，估计/未知
  只标限制，不冒充已确认收益。
- 收益率以基点整数保存；缺少上一估值/分母非正/估值不完整时返回 None 与
  限制码，不填 0，不拼接不可比区间。
- 仅供本地合成/手工数据使用；不接真实行情或公司行为来源。
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

from ..data.chunks import VersionView
from ..data.digests import sha256_of_object
from ..data.errors import DataError
from ..data.prices import valuation_price
from ..data.timeutil import utc_now_iso
from .corporate_actions import receivable_summary
from .ledger import get_account_state, list_account_events
from .simulation import get_simulation_account_state

__all__ = [
    "PERFORMANCE_METHOD_VERSION",
    "value_owner",
    "compute_performance_point",
    "performance_summary",
    "list_performance_points",
]

PERFORMANCE_METHOD_VERSION = "s5b-nav-1.0"


def _require_date(value: Any, reason: str) -> str:
    from ..data.timeutil import parse_yyyymmdd

    if not isinstance(value, str):
        raise DataError(reason, f"日期必须是 YYYYMMDD: {value!r}")
    try:
        parsed = parse_yyyymmdd(value, field=reason)
    except ValueError as exc:
        raise DataError(reason, f"日期不是真实日历日: {value!r}") from exc
    if parsed is None:
        raise DataError(reason, "日期不能为空")
    return parsed.strftime("%Y%m%d")


def _owner_state(conn, owner_type: str, owner_id: str) -> tuple[str, dict[str, Any]]:
    if owner_type == "account":
        row = conn.execute("SELECT kind FROM account WHERE account_id=?", (owner_id,)).fetchone()
        if row is None:
            raise DataError("perf_owner_not_found", f"账户 {owner_id} 不存在")
        if str(row[0]) != "actual":
            raise DataError("perf_owner_invalid", "account owner 必须是实际账户")
        return owner_id, get_account_state(conn, owner_id)
    if owner_type == "simulation_segment":
        row = conn.execute(
            "SELECT simulation_account_id FROM simulation_segment WHERE segment_id=?",
            (owner_id,),
        ).fetchone()
        if row is None:
            raise DataError("perf_owner_not_found", f"模拟段 {owner_id} 不存在")
        return str(row[0]), get_simulation_account_state(conn, owner_id)
    raise DataError("perf_owner_type_invalid", f"未知 owner_type: {owner_type!r}")


def _valuation_id(owner_type: str, owner_id: str, as_of: str, data_version_id: str, payload: Mapping[str, Any]) -> str:
    return "val_" + sha256_of_object(
        [owner_type, owner_id, as_of, data_version_id, dict(payload)]
    )[:20]


def value_owner(
    conn,
    *,
    owner_type: str,
    owner_id: str,
    source_code: str,
    data_version_id: str,
    as_of: str,
    persist: bool = True,
) -> dict[str, Any]:
    """估值并（可选）持久化不可变估值快照；缺价返回 partial_unknown。"""
    as_of = _require_date(as_of, "perf_as_of_invalid")
    account_id, state = _owner_state(conn, owner_type, owner_id)
    view = VersionView(conn, data_version_id, expected_source_code=source_code)
    positions = list(state["positions"])
    holdings_total = 0
    missing: list[str] = []
    for position in positions:
        shares = int(position["shares"])
        if shares == 0:
            continue
        price = valuation_price(
            conn,
            source_code=source_code,
            ts_code=str(position["ts_code"]),
            business_date=as_of,
            as_of=as_of,
            view=view,
        )
        if price.price is None:
            missing.append(str(position["ts_code"]))
            continue
        holdings_total += int(
            (
                Decimal(str(price.price)) * Decimal(shares) * Decimal(100)
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    receivables = receivable_summary(conn, account_id, as_of)
    cash_cents = state["cash_cents"]
    complete = not missing and int(receivables["unconfirmed_count"]) == 0
    restriction_code = None
    if missing:
        restriction_code = "valuation_price_missing"
    elif int(receivables["unconfirmed_count"]) > 0:
        restriction_code = "receivable_tax_unconfirmed"
    holdings_value = holdings_total if not missing else None
    nav_cents = (
        int(cash_cents) + int(holdings_total) + int(receivables["confirmed_cents"])
        if complete
        else None
    )
    payload = {
        "owner_type": owner_type,
        "owner_id": owner_id,
        "as_of": as_of,
        "source_code": source_code,
        "data_version_id": data_version_id,
        "cash_cents": cash_cents,
        "holdings_value_cents": holdings_value,
        "receivable_cents": int(receivables["confirmed_cents"]),
        "payable_cents": 0,
        "nav_cents": nav_cents,
        "completeness": "complete" if complete else "partial_unknown",
        "restriction_code": restriction_code,
        "missing_valuation_codes": missing,
        "unconfirmed_receivable_count": int(receivables["unconfirmed_count"]),
    }
    valuation_id = _valuation_id(owner_type, owner_id, as_of, data_version_id, payload)
    if persist:
        conn.execute(
            "INSERT OR IGNORE INTO valuation_snapshot(valuation_id, owner_type, owner_id,"
            " as_of, source_code, data_version_id, cash_cents, holdings_value_cents,"
            " receivable_cents, payable_cents, nav_cents, completeness, restriction_code,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                valuation_id,
                owner_type,
                owner_id,
                as_of,
                source_code,
                data_version_id,
                cash_cents,
                holdings_value,
                int(receivables["confirmed_cents"]),
                0,
                nav_cents,
                payload["completeness"],
                restriction_code,
                utc_now_iso(),
            ),
        )
    payload["valuation_id"] = valuation_id
    return payload


def _latest_valuation_before(conn, owner_type: str, owner_id: str, as_of: str):
    return conn.execute(
        "SELECT valuation_id, as_of, nav_cents, completeness FROM valuation_snapshot"
        " WHERE owner_type=? AND owner_id=? AND as_of<? ORDER BY as_of DESC LIMIT 1",
        (owner_type, owner_id, as_of),
    ).fetchone()


def _external_cash_flow(conn, owner_type: str, owner_id: str, as_of: str) -> tuple[int, bool]:
    """实际账户当日的有效入金-出金；模拟段无外部现金流。"""
    if owner_type != "account":
        return 0, False
    events = list_account_events(conn, owner_id, limit=1000)
    total = 0
    for event in events:
        if not event["effective"] or event["business_date"] != as_of:
            continue
        payload = event.get("payload") or {}
        if event["event_type"] == "cash_in":
            total += int(payload.get("amount_cents") or 0)
        elif event["event_type"] == "cash_out":
            total -= int(payload.get("amount_cents") or 0)
    # 事件列表有界；达到上限时无法证明流量完整 → 标记受限。
    return total, len(events) >= 1000


def compute_performance_point(
    conn,
    *,
    owner_type: str,
    owner_id: str,
    source_code: str,
    data_version_id: str,
    as_of: str,
) -> dict[str, Any]:
    """计算并持久化一个收益点；不可比较时 period_return_bp 为 None。"""
    as_of = _require_date(as_of, "perf_as_of_invalid")
    valuation = value_owner(
        conn,
        owner_type=owner_type,
        owner_id=owner_id,
        source_code=source_code,
        data_version_id=data_version_id,
        as_of=as_of,
    )
    previous = _latest_valuation_before(conn, owner_type, owner_id, as_of)
    flow, flow_incomplete = _external_cash_flow(conn, owner_type, owner_id, as_of)
    period_return_bp = None
    restriction_code = valuation.get("restriction_code")
    if flow_incomplete:
        restriction_code = restriction_code or "external_cash_flow_incomplete"
    if (
        valuation["nav_cents"] is not None
        and previous is not None
        and previous[3] == "complete"
        and previous[2] is not None
        and not flow_incomplete
    ):
        denominator = int(previous[2]) + int(flow)
        if denominator > 0:
            period_return_bp = int(
                (
                    (Decimal(int(valuation["nav_cents"])) / Decimal(denominator) - 1)
                    * Decimal(10_000)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
        else:
            restriction_code = restriction_code or "non_positive_denominator"
    point_id = "perf_" + sha256_of_object(
        [owner_type, owner_id, as_of, PERFORMANCE_METHOD_VERSION]
    )[:20]
    conn.execute(
        "INSERT OR IGNORE INTO performance_point(point_id, owner_type, owner_id, as_of,"
        " valuation_id, method_version, external_cash_flow_cents, period_return_bp,"
        " nav_cents, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            point_id,
            owner_type,
            owner_id,
            as_of,
            valuation["valuation_id"],
            PERFORMANCE_METHOD_VERSION,
            int(flow),
            period_return_bp,
            valuation["nav_cents"],
            utc_now_iso(),
        ),
    )
    return {
        "point_id": point_id,
        "owner_type": owner_type,
        "owner_id": owner_id,
        "as_of": as_of,
        "valuation": valuation,
        "external_cash_flow_cents": int(flow),
        "period_return_bp": period_return_bp,
        "restriction_code": restriction_code,
    }


def list_performance_points(
    conn, owner_type: str, owner_id: str, *, limit: int = 200, offset: int = 0
) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = 200
    limit = min(limit, 500)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        offset = 0
    rows = conn.execute(
        "SELECT point_id, as_of, valuation_id, external_cash_flow_cents,"
        " period_return_bp, nav_cents, method_version, created_at FROM performance_point"
        " WHERE owner_type=? AND owner_id=? ORDER BY as_of LIMIT ? OFFSET ?",
        (owner_type, owner_id, limit, offset),
    ).fetchall()
    names = (
        "point_id",
        "as_of",
        "valuation_id",
        "external_cash_flow_cents",
        "period_return_bp",
        "nav_cents",
        "method_version",
        "created_at",
    )
    return [dict(zip(names, row)) for row in rows]


def performance_summary(
    conn, owner_type: str, owner_id: str, *, limit: int = 500
) -> dict[str, Any]:
    points = list_performance_points(conn, owner_type, owner_id, limit=limit)
    rates = [p["period_return_bp"] for p in points]
    incomplete = any(rate is None for rate in rates)
    cumulative_bp = None
    max_drawdown_bp = None
    if points and not incomplete:
        index = Decimal(1)
        peak = Decimal(1)
        worst = Decimal(0)
        for rate in rates:
            index *= Decimal(1) + Decimal(int(rate)) / Decimal(10_000)
            if index > peak:
                peak = index
            drawdown = (index / peak - 1) * Decimal(10_000)
            if drawdown < worst:
                worst = drawdown
        cumulative_bp = int(
            ((index - 1) * Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        max_drawdown_bp = int(worst.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    fees = _fees_total(conn, owner_type, owner_id)
    return {
        "owner_type": owner_type,
        "owner_id": owner_id,
        "method_version": PERFORMANCE_METHOD_VERSION,
        "points": points,
        "point_count": len(points),
        "cumulative_return_bp": cumulative_bp,
        "max_drawdown_bp": max_drawdown_bp,
        "incomplete_periods": incomplete,
        "fees_total_cents": fees,
    }


def _fees_total(conn, owner_type: str, owner_id: str) -> int:
    if owner_type == "account":
        events = list_account_events(conn, owner_id, limit=1000)
        total = 0
        for event in events:
            if not event["effective"]:
                continue
            if event["event_type"] in ("buy", "sell"):
                total += int((event.get("payload") or {}).get("fee_cents") or 0)
        return total
    account_row = conn.execute(
        "SELECT simulation_account_id FROM simulation_segment WHERE segment_id=?",
        (owner_id,),
    ).fetchone()
    if account_row is None:
        raise DataError("perf_owner_not_found", f"模拟段 {owner_id} 不存在")
    row = conn.execute(
        "SELECT COALESCE(SUM(f.fee_cents + f.slippage_cents), 0) FROM simulation_fill f"
        " JOIN simulation_order o ON o.order_id=f.order_id WHERE o.segment_id=?",
        (owner_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0
