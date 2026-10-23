"""阶段三 S5a：模拟交易段与确定性订单/成交生命周期（方案 A，模拟域隔离）。

派工冻结口径：

- 模拟段从显式选择的**已持久化**账户快照创建：把冻结账户状态复制到独立的
  ``simulation`` 账户（``simulation_of_account_id`` 指向源实际账户），绝不
  改写实际账户；重复的同输入请求幂等（``UNIQUE(input_sha256)``，复用既有段）。
- 模拟账户的现金、持仓、可卖量与事件只写模拟域表
  （``simulation_event`` / ``simulation_account_state`` /
  ``simulation_position_state``），**绝不**写 ``account_event`` /
  ``account_snapshot`` / ``account_position``；S2 的 actual-only 服务校验和
  schema 触发器保持不变。
- 订单生命周期使用既有 schema4 表：pending →（处理日）filled /
  partially_filled / unfilled / rejected / expired / cancelled；planned、
  filled、remaining = planned − filled 分毫守恒。
- 只有**晚于段 as_of** 的交易日（冻结数据版本日历 is_open=1）可用冻结版本的
  raw open 参考价处理；日历/状态/价格/规则/费用证据缺失时保持持仓不动，并把
  订单标记 restricted/unfilled，绝不发明限额或冒充真实成交。
- 费用/滑点只用版本化费用组合，每个订单只计一次；费用与滑点分项保存，现金
  口径使用两者之和；卖出先于买入处理，未成交/失败的卖出绝不作为买入资金来源；
  余现金分毫可查。
- sellable/T+1/每手（board lot）规则只在版本化规则组合实际提供已核验证据
  时执行；买入受 board lot 约束，卖出允许零股；否则 fail-closed，不发明交易限制。
- 每个公开写命令都原子提交：调用方无事务时由本模块 BEGIN IMMEDIATE/COMMIT；
  调用方已有事务时使用 SAVEPOINT 参与而不擅自提交调用方事务。
- 本模块不实现公司行为生命周期、NAV/绩效、API/UI、真实数据接入或应用启动
  （S5b/后续阶段）。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterator, Mapping, Sequence

from ..data.chunks import VersionView
from ..data.digests import canonical_json_bytes, sha256_of_object
from ..data.errors import DataError
from ..data.timeutil import TimeParseError, parse_yyyymmdd, utc_now_iso
from .ledger import _validate_ts_code, get_account_snapshot_state, get_account_state
from .references import ReferenceTarget, register_reference_holds
from .rule_bundles import load_rule_bundle

__all__ = [
    "SIMULATION_APPLICATION_VERSION",
    "create_simulation_segment",
    "get_simulation_segment",
    "list_simulation_segments",
    "get_simulation_account_state",
    "list_simulation_events",
    "apply_simulation_corporate_action",
    "place_simulation_order",
    "advance_simulation_segment",
    "cancel_simulation_order",
    "close_simulation_segment",
]

SIMULATION_APPLICATION_VERSION = "s5a-simulation-2.0"

_ORDER_STATUSES = (
    "pending",
    "unfilled",
    "partially_filled",
    "filled",
    "rejected",
    "expired",
    "cancelled",
)

_KNOWN_FEE_COMPONENTS = {
    "commission": "per_side",
    "stamp_duty_sell": "sell_only",
    "transfer_fee": "both_sides",
    "slippage": "adverse_direction_model",
}


# ------------------------------------------------------------------ 事务边界


@contextmanager
def _atomic_write(conn) -> Iterator[None]:
    """公开写命令的原子边界。

    - 调用方没有打开事务：本模块 BEGIN IMMEDIATE / COMMIT，保证整包落盘；
    - 调用方已有事务：只创建 SAVEPOINT，失败回滚到保存点，成功也不提交
      调用方事务（不替调用方决定无关事务的提交）。
    """
    if conn.in_transaction:
        conn.execute("SAVEPOINT s5a_write")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO s5a_write")
            conn.execute("RELEASE s5a_write")
            raise
        else:
            conn.execute("RELEASE s5a_write")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ------------------------------------------------------------------ 费用（与 S4 相同口径的本地只读实现）


def _fee_schedule(fee_bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """解析并校验费用组合；任何缺口都不可用（返回空 dict 表示缺证据）。"""
    content = fee_bundle.get("content")
    if not isinstance(content, Mapping):
        return {}
    components = content.get("components")
    if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
        return {}
    schedule: dict[str, dict[str, Any]] = {}
    for component in components:
        if not isinstance(component, Mapping):
            return {}
        code = component.get("code")
        if code not in _KNOWN_FEE_COMPONENTS or str(code) in schedule:
            return {}
        code_text = str(code)
        charging = component.get("charging")
        if charging != _KNOWN_FEE_COMPONENTS[code_text]:
            return {}
        rate = component.get("rate_bp")
        if isinstance(rate, bool) or not isinstance(rate, int) or not (0 <= rate <= 10_000):
            return {}
        entry: dict[str, Any] = {"rate_bp": int(rate)}
        minimum = component.get("minimum_cents")
        if minimum is not None:
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
                return {}
            entry["minimum_cents"] = int(minimum)
        schedule[code_text] = entry
    for code in _KNOWN_FEE_COMPONENTS:
        if code not in schedule:
            return {}
    return schedule


def _order_fee_estimate(
    schedule: Mapping[str, Mapping[str, Any]],
    side: str,
    notional_cents: int,
    *,
    apply_minimum: bool = True,
) -> dict[str, Any]:
    """单笔订单（单侧）费用/滑点合成估计；只供模拟使用。

    ``fee_cents`` 只含佣金/税费/过户费；滑点单列 ``slippage_cents``，避免同一
    笔钱在费用与滑点两处重复计算。
    """
    assert side in ("buy", "sell")
    cents = Decimal(notional_cents)

    def _fee(component: str) -> int:
        return int(
            (cents * Decimal(schedule[component]["rate_bp"]) / Decimal(10_000)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )

    commission = _fee("commission")
    if apply_minimum:
        commission = max(
            commission, int(schedule["commission"].get("minimum_cents") or 0)
        )
    breakdown = {
        "commission_cents": commission,
        "stamp_duty_sell_cents": _fee("stamp_duty_sell") if side == "sell" else 0,
        "transfer_fee_cents": _fee("transfer_fee"),
    }
    fee_total = (
        breakdown["commission_cents"]
        + breakdown["stamp_duty_sell_cents"]
        + breakdown["transfer_fee_cents"]
    )
    return {
        "fee_cents": fee_total,
        "slippage_cents": _fee("slippage"),
        "breakdown": breakdown,
    }


# ------------------------------------------------------------------ 证据校验（停牌/涨跌停）


def _load_market_rules(conn, bundle_id: str) -> Mapping[str, Any] | None:
    row = conn.execute(
        "SELECT content FROM rule_bundle WHERE bundle_id=?", (bundle_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        content = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    if not isinstance(content, Mapping):
        return None
    rules = content.get("market_rules")
    if not isinstance(rules, Mapping) or rules.get("verification_status") != "verified":
        return None
    return rules


def _valid_date_text(value: Any) -> str | None:
    try:
        return _require_yyyymmdd(value, "s5_evidence_invalid")
    except DataError:
        return None


def _suspension_evidence_ok(
    view: VersionView,
    market_rules: Mapping[str, Any],
    *,
    code: str,
    trade_date: str,
) -> bool:
    """停牌覆盖证据：版本确实包含停牌数据集，且规则包声明该股票/日期完整覆盖。

    仅“类别存在”或任意其他股票/日期的记录不足以证明当前状态；任何缺口 fail-closed。
    """
    if "suspension" not in view.meta.category_list:
        return False
    evidence = market_rules.get("suspension_evidence")
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("verification_status") != "verified":
        return False
    if evidence.get("coverage") != "full":
        return False
    if evidence.get("source_category") != "suspension":
        return False
    codes = evidence.get("ts_codes")
    if codes != "all":
        if not isinstance(codes, list) or code not in codes:
            return False
    start = _valid_date_text(evidence.get("start_date"))
    end = _valid_date_text(evidence.get("end_date"))
    if start is None or end is None or start > end:
        return False
    return start <= trade_date <= end


def _price_limit_bounds(
    market_rules: Mapping[str, Any],
    *,
    code: str,
    trade_date: str,
) -> tuple[int, int] | None:
    """返回该股票/处理日的显式价格区间（分）；证据缺失或歧义返回 None。"""
    evidence = market_rules.get("price_limit_evidence")
    if not isinstance(evidence, Mapping):
        return None
    if evidence.get("verification_status") != "verified":
        return None
    if evidence.get("coverage") != "full":
        return None
    bounds = evidence.get("bounds")
    if not isinstance(bounds, list):
        return None
    matches: list[tuple[int, int]] = []
    for item in bounds:
        if not isinstance(item, Mapping) or item.get("ts_code") != code:
            continue
        start = _valid_date_text(item.get("start_date"))
        end = _valid_date_text(item.get("end_date"))
        if start is None or end is None or start > end:
            return None
        if not (start <= trade_date <= end):
            continue
        lower = item.get("lower_cents")
        upper = item.get("upper_cents")
        if (
            isinstance(lower, bool)
            or isinstance(upper, bool)
            or not isinstance(lower, int)
            or not isinstance(upper, int)
            or lower < 1
            or upper < lower
        ):
            return None
        matches.append((int(lower), int(upper)))
    if len(matches) != 1:
        return None
    return matches[0]


def _max_affordable_shares(
    schedule: Mapping[str, Mapping[str, Any]],
    *,
    price_cents: int,
    board_lot: int,
    planned_shares: int,
    cash_budget: int,
    apply_minimum: bool = True,
) -> int:
    """在现金预算内求最大可买整手数量（二分，费用单调）。

    不再从任意 planned_shares 按每手无限递减；只对单调的“手数→总成本”做有界查找。
    """
    max_lots = planned_shares // board_lot
    if max_lots < 1:
        return 0
    low, high = 0, max_lots
    while low < high:
        mid = (low + high + 1) // 2
        shares = mid * board_lot
        notional = shares * price_cents
        estimate = _order_fee_estimate(
            schedule, "buy", notional, apply_minimum=apply_minimum
        )
        cost = notional + int(estimate["fee_cents"]) + int(estimate["slippage_cents"])
        if cost <= cash_budget:
            low = mid
        else:
            high = mid - 1
    return low * board_lot


# ------------------------------------------------------------------ 小工具


def _price_to_cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        decimal_value = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    try:
        if decimal_value <= 0:
            return None
        return int((decimal_value * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except ArithmeticError:
        return None


def _require_yyyymmdd(value: Any, reason: str) -> str:
    """必须是真实日历日的 YYYYMMDD 文本（拒绝 20250231 这类伪日期）。"""
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise DataError(reason, f"日期必须是 YYYYMMDD 文本: {value!r}")
    try:
        parsed = parse_yyyymmdd(value, field=reason)
    except TimeParseError as exc:
        raise DataError(reason, f"日期不是真实日历日: {value!r}") from exc
    if parsed is None:
        raise DataError(reason, f"日期不能为空: {value!r}")
    return parsed.strftime("%Y%m%d")


def _int_value(
    value: Any,
    field: str,
    *,
    reason: str = "s5_payload_invalid",
    minimum: int | None = None,
    allow_none: bool = False,
) -> int | None:
    if value is None:
        if allow_none:
            return None
        raise DataError(reason, f"{field} 不能为空")
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataError(reason, f"{field} 必须是整数: {value!r}")
    if minimum is not None and value < minimum:
        raise DataError(reason, f"{field} 必须 >= {minimum}: {value!r}")
    return int(value)


def _optional_text(value: Any, field: str, *, max_length: int = 300) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DataError("s5_payload_invalid", f"{field} 必须是文本或空")
    text = value.strip()
    if len(text) > max_length:
        raise DataError("s5_payload_invalid", f"{field} 长度超过 {max_length}")
    return text or None


def _normalize_sim_position(raw: Any, *, business_date: str, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DataError("s5_payload_invalid", f"期初持仓 #{index} 必须是对象")
    allowed = {
        "ts_code",
        "shares",
        "total_cost_cents",
        "sellable_shares",
        "acquire_date",
        "locked",
        "price_status",
        "note",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise DataError("s5_payload_invalid", f"期初持仓 #{index} 存在未知字段: {sorted(unknown)}")
    price_status = raw.get("price_status", "user_reported")
    if price_status != "user_reported":
        raise DataError("s5_payload_invalid", "模拟期初持仓的 price_status 只能是 user_reported")
    code = _validate_ts_code(raw.get("ts_code"))
    shares = int(_int_value(raw.get("shares"), "shares", minimum=1) or 0)
    cost = _int_value(raw.get("total_cost_cents"), "total_cost_cents", minimum=0, allow_none=True)
    sellable = _int_value(raw.get("sellable_shares"), "sellable_shares", minimum=0, allow_none=True)
    if sellable is not None and sellable > shares:
        raise DataError(
            "s5_payload_invalid",
            f"期初持仓 {code} 的可卖数量 {sellable} 超过持有数量 {shares}",
        )
    acquire = _optional_text(raw.get("acquire_date"), "acquire_date", max_length=8)
    if acquire is not None:
        acquire = _require_yyyymmdd(acquire, "s5_payload_invalid")
        if acquire > business_date:
            raise DataError(
                "s5_payload_invalid",
                f"期初持仓 {code} 的取得日期 {acquire} 晚于期初日 {business_date}",
            )
    locked_raw = raw.get("locked", 0)
    if isinstance(locked_raw, bool):
        locked = 1 if locked_raw else 0
    elif isinstance(locked_raw, int) and locked_raw in (0, 1):
        locked = int(locked_raw)
    else:
        raise DataError("s5_payload_invalid", "期初持仓 locked 只能是布尔或 0/1")
    return {
        "ts_code": code,
        "shares": shares,
        "total_cost_cents": cost,
        "sellable_shares": sellable,
        "acquire_date": acquire,
        "locked": locked,
        "price_status": "user_reported",
        "note": _optional_text(raw.get("note"), "note"),
    }


def _normalize_event_payload(
    event_type: str, raw: Mapping[str, Any], *, business_date: str
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DataError("s5_payload_invalid", "事件载荷必须是对象")
    note = _optional_text(raw.get("note"), "note")
    if event_type == "opening":
        allowed = {"cash_cents", "positions", "note"}
        unknown = set(raw) - allowed
        if unknown:
            raise DataError("s5_payload_invalid", f"opening 载荷存在未知字段: {sorted(unknown)}")
        cash = int(_int_value(raw.get("cash_cents"), "cash_cents", minimum=0) or 0)
        positions_raw = raw.get("positions", [])
        if not isinstance(positions_raw, (list, tuple)):
            raise DataError("s5_payload_invalid", "positions 必须是列表")
        positions = [
            _normalize_sim_position(item, business_date=business_date, index=i)
            for i, item in enumerate(positions_raw)
        ]
        codes = [p["ts_code"] for p in positions]
        duplicates = sorted({c for c in codes if codes.count(c) > 1})
        if duplicates:
            raise DataError("s5_payload_invalid", f"期初持仓存在重复证券代码: {duplicates}")
        positions.sort(key=lambda p: p["ts_code"])
        return {"cash_cents": cash, "positions": positions, "note": note}
    if event_type in ("buy", "sell"):
        allowed = {"ts_code", "shares", "price_cents", "fee_cents", "slippage_cents", "note"}
        unknown = set(raw) - allowed
        if unknown:
            raise DataError("s5_payload_invalid", f"{event_type} 载荷存在未知字段: {sorted(unknown)}")
        return {
            "ts_code": _validate_ts_code(raw.get("ts_code")),
            "shares": int(_int_value(raw.get("shares"), "shares", minimum=1) or 1),
            "price_cents": int(_int_value(raw.get("price_cents"), "price_cents", minimum=1) or 1),
            "fee_cents": int(_int_value(raw.get("fee_cents"), "fee_cents", minimum=0) or 0),
            "slippage_cents": int(
                _int_value(raw.get("slippage_cents", 0), "slippage_cents", minimum=0) or 0
            ),
            "note": note,
        }
    if event_type == "corporate_action":
        allowed = {"ts_code", "share_delta", "cash_delta_cents", "new_total_cost_cents", "note"}
        unknown = set(raw) - allowed
        if unknown:
            raise DataError(
                "s5_payload_invalid", f"corporate_action 载荷存在未知字段: {sorted(unknown)}"
            )
        share_delta = int(
            _int_value(raw.get("share_delta", 0), "share_delta", minimum=0) or 0
        )
        cash_delta = int(
            _int_value(raw.get("cash_delta_cents", 0), "cash_delta_cents", minimum=0) or 0
        )
        new_cost = _int_value(
            raw.get("new_total_cost_cents"),
            "new_total_cost_cents",
            minimum=0,
            allow_none=True,
        )
        if share_delta == 0 and cash_delta == 0 and new_cost is None:
            raise DataError(
                "s5_corporate_action_no_effect",
                "模拟公司行为必须至少包含一项显式变动",
            )
        return {
            "ts_code": _validate_ts_code(raw.get("ts_code")),
            "share_delta": share_delta,
            "cash_delta_cents": cash_delta,
            "new_total_cost_cents": new_cost,
            "note": note,
        }
    raise DataError("s5_event_type_unknown", f"未知模拟事件类型: {event_type!r}")


# ------------------------------------------------------------------ 模拟域状态读写


def _load_state_row(conn, segment_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT cash_cents, revision, as_of, last_advanced_date"
        " FROM simulation_account_state WHERE segment_id=?",
        (segment_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "cash_cents": int(row[0]),
        "revision": int(row[1]),
        "as_of": str(row[2]),
        "last_advanced_date": None if row[3] is None else str(row[3]),
    }


def _load_positions(conn, segment_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ts_code, shares, total_cost_cents, sellable_shares, acquire_date, locked,"
        " price_status, note FROM simulation_position_state WHERE segment_id=? ORDER BY ts_code",
        (segment_id,),
    ).fetchall()
    positions: list[dict[str, Any]] = []
    for row in rows:
        positions.append(
            {
                "ts_code": str(row[0]),
                "shares": int(row[1]),
                "total_cost_cents": None if row[2] is None else int(row[2]),
                "sellable_shares": None if row[3] is None else int(row[3]),
                "acquire_date": row[4],
                "locked": int(row[5]),
                "price_status": str(row[6]),
                "note": row[7],
            }
        )
    return positions


def _write_position(conn, segment_id: str, position: Mapping[str, Any]) -> None:
    conn.execute(
        "INSERT INTO simulation_position_state(segment_id, ts_code, shares,"
        " total_cost_cents, sellable_shares, acquire_date, locked, price_status, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(segment_id, ts_code) DO UPDATE SET"
        " shares=excluded.shares, total_cost_cents=excluded.total_cost_cents,"
        " sellable_shares=excluded.sellable_shares, acquire_date=excluded.acquire_date,"
        " locked=excluded.locked, price_status=excluded.price_status, note=excluded.note",
        (
            segment_id,
            str(position["ts_code"]),
            int(position["shares"]),
            None if position["total_cost_cents"] is None else int(position["total_cost_cents"]),
            None if position["sellable_shares"] is None else int(position["sellable_shares"]),
            position["acquire_date"],
            int(position["locked"]),
            str(position["price_status"]),
            position["note"],
        ),
    )


def _insert_lot(
    conn,
    *,
    lot_id: str,
    segment_id: str,
    ts_code: str,
    shares: int,
    acquire_date: str,
    unlock_date: str | None,
) -> None:
    if shares < 1:
        return
    conn.execute(
        "INSERT INTO simulation_lot_state(lot_id, segment_id, ts_code, shares,"
        " acquire_date, unlock_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lot_id, segment_id, ts_code, int(shares), acquire_date, unlock_date, utc_now_iso()),
    )


def _available_sellable(
    conn, segment_id: str, ts_code: str, on_date: str
) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(shares), 0) FROM simulation_lot_state"
        " WHERE segment_id=? AND ts_code=? AND unlock_date IS NOT NULL AND unlock_date<=?",
        (segment_id, ts_code, on_date),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _unknown_lot_shares(conn, segment_id: str, ts_code: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(shares), 0) FROM simulation_lot_state"
        " WHERE segment_id=? AND ts_code=? AND unlock_date IS NULL",
        (segment_id, ts_code),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _consume_lots(
    conn, segment_id: str, ts_code: str, on_date: str, shares: int
) -> None:
    remaining = int(shares)
    rows = conn.execute(
        "SELECT lot_id, shares FROM simulation_lot_state"
        " WHERE segment_id=? AND ts_code=? AND unlock_date IS NOT NULL AND unlock_date<=?"
        " ORDER BY acquire_date, lot_id",
        (segment_id, ts_code, on_date),
    ).fetchall()
    for lot_id, lot_shares in rows:
        if remaining <= 0:
            break
        take = min(int(lot_shares), remaining)
        if take == int(lot_shares):
            conn.execute("DELETE FROM simulation_lot_state WHERE lot_id=?", (str(lot_id),))
        else:
            conn.execute(
                "UPDATE simulation_lot_state SET shares=shares-? WHERE lot_id=?",
                (take, str(lot_id)),
            )
        remaining -= take
    if remaining > 0:
        raise DataError(
            "s5_sellable_insufficient",
            f"模拟段 {segment_id} 持仓 {ts_code} 可卖批次不足 {shares} 股",
        )


def _event_content_hash(
    *,
    segment_id: str,
    event_type: str,
    business_date: str,
    idempotency_key: str,
    payload: Mapping[str, Any],
) -> str:
    return sha256_of_object(
        {
            "application_version": SIMULATION_APPLICATION_VERSION,
            "segment_id": segment_id,
            "event_type": event_type,
            "business_date": business_date,
            "idempotency_key": idempotency_key,
            "payload": dict(payload),
        }
    )


def _apply_opening(
    conn,
    segment_id: str,
    payload: Mapping[str, Any],
    *,
    business_date: str,
) -> None:
    if _load_state_row(conn, segment_id) is not None:
        raise DataError("s5_state_exists", f"模拟段 {segment_id} 已有期初状态，拒绝重复初始化")
    conn.execute(
        "INSERT INTO simulation_account_state(segment_id, cash_cents, revision, as_of,"
        " last_advanced_date, updated_at) VALUES (?, ?, 1, ?, NULL, ?)",
        (segment_id, int(payload["cash_cents"]), business_date, utc_now_iso()),
    )
    for index, position in enumerate(payload["positions"]):
        _write_position(conn, segment_id, position)
        code = str(position["ts_code"])
        shares = int(position["shares"])
        sellable = position["sellable_shares"]
        if sellable is None:
            _insert_lot(
                conn,
                lot_id="slot_"
                + sha256_of_object([segment_id, "opening", code, str(index), "u"])[:20],
                segment_id=segment_id,
                ts_code=code,
                shares=shares,
                acquire_date=str(position["acquire_date"] or business_date),
                unlock_date=None,
            )
            continue
        sellable = min(int(sellable), shares)
        unlock = str(position["acquire_date"] or business_date)
        if sellable > 0:
            _insert_lot(
                conn,
                lot_id="slot_"
                + sha256_of_object([segment_id, "opening", code, str(index), "s"])[:20],
                segment_id=segment_id,
                ts_code=code,
                shares=sellable,
                acquire_date=unlock,
                unlock_date=unlock,
            )
        unknown = shares - sellable
        if unknown > 0:
            _insert_lot(
                conn,
                lot_id="slot_"
                + sha256_of_object([segment_id, "opening", code, str(index), "u"])[:20],
                segment_id=segment_id,
                ts_code=code,
                shares=unknown,
                acquire_date=unlock,
                unlock_date=None,
            )


def _apply_buy(
    conn,
    segment_id: str,
    payload: Mapping[str, Any],
    *,
    business_date: str,
    state: Mapping[str, Any],
    lot_id: str,
    unlock_date: str | None,
) -> int:
    quantity = int(payload["shares"])
    price = int(payload["price_cents"])
    fee = int(payload["fee_cents"])
    slippage = int(payload["slippage_cents"])
    cost_total = quantity * price + fee + slippage
    if int(state["cash_cents"]) - cost_total < 0:
        raise DataError(
            "s5_insufficient_cash",
            f"模拟段 {segment_id} 现金 {state['cash_cents']} 分不足以支付 {cost_total} 分的买入",
        )
    code = str(payload["ts_code"])
    positions = {p["ts_code"]: p for p in _load_positions(conn, segment_id)}
    position = positions.get(code)
    if position is None:
        position = {
            "ts_code": code,
            "shares": quantity,
            "total_cost_cents": cost_total,
            "sellable_shares": None,
            "acquire_date": business_date,
            "locked": 0,
            "price_status": "user_reported",
            "note": None,
        }
    else:
        position["shares"] = int(position["shares"]) + quantity
        if position["total_cost_cents"] is not None:
            position["total_cost_cents"] = int(position["total_cost_cents"]) + cost_total
    _write_position(conn, segment_id, position)
    _insert_lot(
        conn,
        lot_id=lot_id,
        segment_id=segment_id,
        ts_code=code,
        shares=quantity,
        acquire_date=business_date,
        unlock_date=unlock_date,
    )
    return int(state["cash_cents"]) - cost_total


def _apply_sell(
    conn,
    segment_id: str,
    payload: Mapping[str, Any],
    *,
    business_date: str,
    state: Mapping[str, Any],
) -> int:
    quantity = int(payload["shares"])
    price = int(payload["price_cents"])
    fee = int(payload["fee_cents"])
    slippage = int(payload["slippage_cents"])
    proceeds = quantity * price - fee - slippage
    code = str(payload["ts_code"])
    positions = {p["ts_code"]: p for p in _load_positions(conn, segment_id)}
    position = positions.get(code)
    if position is None or int(position["shares"]) < quantity:
        raise DataError(
            "s5_position_insufficient",
            f"模拟段 {segment_id} 持仓 {code} 不足 {quantity} 股，不能卖出",
        )
    available = _available_sellable(conn, segment_id, code, business_date)
    if available < quantity:
        raise DataError(
            "s5_sellable_insufficient",
            f"模拟段 {segment_id} 持仓 {code} 可卖批次 {available} 股少于 {quantity} 股",
        )
    cash_after = int(state["cash_cents"]) + proceeds
    if cash_after < 0:
        raise DataError(
            "s5_insufficient_cash",
            f"模拟段 {segment_id} 卖出净入账 {proceeds} 分使现金变为负",
        )
    _consume_lots(conn, segment_id, code, business_date, quantity)
    total_before = int(position["shares"])
    remaining = total_before - quantity
    position["shares"] = remaining
    cost = position["total_cost_cents"]
    if remaining == 0:
        position["total_cost_cents"] = 0
        position["sellable_shares"] = 0
    elif cost is not None:
        sold_cost = int(
            (Decimal(int(cost)) * Decimal(quantity) / Decimal(total_before)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if sold_cost > int(cost):
            sold_cost = int(cost)
        position["total_cost_cents"] = int(cost) - sold_cost
        position["sellable_shares"] = _available_sellable(
            conn, segment_id, code, business_date
        )
    _write_position(conn, segment_id, position)
    return cash_after


def _apply_corporate_action(
    conn,
    segment_id: str,
    payload: Mapping[str, Any],
    *,
    business_date: str,
    state: Mapping[str, Any],
    lot_id: str,
) -> int:
    """模拟域已确认公司行为：现金分红/送转股，不写实际账本。"""
    code = str(payload["ts_code"])
    positions = {p["ts_code"]: p for p in _load_positions(conn, segment_id)}
    position = positions.get(code)
    if position is None:
        raise DataError(
            "s5_corporate_action_position_missing",
            f"模拟段 {segment_id} 未持有 {code}，不能登记公司行为",
        )
    cash_after = int(state["cash_cents"]) + int(payload["cash_delta_cents"])
    if cash_after < 0:
        raise DataError("s5_insufficient_cash", "公司行为现金变动使模拟现金为负")
    share_delta = int(payload["share_delta"])
    shares_after = int(position["shares"]) + share_delta
    if shares_after < 0:
        raise DataError("s5_corporate_action_invalid_shares", "公司行为使模拟持仓为负")
    position["shares"] = shares_after
    new_cost = payload["new_total_cost_cents"]
    if shares_after == 0:
        position["total_cost_cents"] = 0
        position["sellable_shares"] = 0
    elif new_cost is not None:
        position["total_cost_cents"] = int(new_cost)
    if share_delta > 0:
        _insert_lot(
            conn,
            lot_id=lot_id,
            segment_id=segment_id,
            ts_code=code,
            shares=share_delta,
            acquire_date=business_date,
            unlock_date=business_date,
        )
    _write_position(conn, segment_id, position)
    _refresh_sellable_columns(conn, segment_id, business_date)
    return cash_after


def _post_simulation_event(
    conn,
    *,
    segment_id: str,
    event_type: str,
    business_date: str,
    idempotency_key: str,
    payload: Mapping[str, Any],
    recorded_at: str,
    reason: str | None = None,
    unlock_date: str | None = None,
) -> dict[str, Any]:
    """追加一个模拟域事件并更新模拟状态；同一事务内完成，调用方负责事务。

    同一 ``(segment_id, idempotency_key)`` 且内容一致 → 幂等重放；内容不同 →
    显式冲突，绝不重复记账。
    """
    normalized = _normalize_event_payload(event_type, dict(payload), business_date=business_date)
    content_hash = _event_content_hash(
        segment_id=segment_id,
        event_type=event_type,
        business_date=business_date,
        idempotency_key=idempotency_key,
        payload=normalized,
    )
    existing = conn.execute(
        "SELECT event_id, content_sha256 FROM simulation_event"
        " WHERE segment_id=? AND idempotency_key=?",
        (segment_id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if str(existing[1]) != content_hash:
            raise DataError(
                "s5_event_idempotency_conflict",
                f"模拟事件键 {idempotency_key} 已被不同内容占用；拒绝覆盖",
            )
        return {"event_id": str(existing[0]), "replayed": True}
    event_id = "sevt_" + sha256_of_object([segment_id, idempotency_key])[:20]
    state = _load_state_row(conn, segment_id)
    if event_type == "opening":
        _apply_opening(conn, segment_id, normalized, business_date=business_date)
        new_revision = 1
        new_cash = int(normalized["cash_cents"])
        new_as_of = business_date
    else:
        if state is None:
            raise DataError("s5_state_missing", f"模拟段 {segment_id} 缺少期初状态，拒绝成交")
        if event_type == "buy":
            new_cash = _apply_buy(
                conn,
                segment_id,
                normalized,
                business_date=business_date,
                state=state,
                lot_id="slot_"
                + sha256_of_object([segment_id, idempotency_key, "buy"])[:20],
                unlock_date=unlock_date,
            )
        elif event_type == "sell":
            new_cash = _apply_sell(
                conn, segment_id, normalized, business_date=business_date, state=state
            )
        elif event_type == "corporate_action":
            new_cash = _apply_corporate_action(
                conn,
                segment_id,
                normalized,
                business_date=business_date,
                state=state,
                lot_id="slot_"
                + sha256_of_object([segment_id, idempotency_key, "ca"])[:20],
            )
        else:  # pragma: no cover - 由 _normalize_event_payload 拦截
            raise DataError("s5_event_type_unknown", f"未知模拟事件类型: {event_type!r}")
        new_revision = int(state["revision"]) + 1
        new_as_of = max(str(state["as_of"]), business_date)
        conn.execute(
            "UPDATE simulation_account_state SET cash_cents=?, revision=?, as_of=?, updated_at=?"
            " WHERE segment_id=?",
            (new_cash, new_revision, new_as_of, utc_now_iso(), segment_id),
        )
    conn.execute(
        "INSERT INTO simulation_event(event_id, segment_id, event_type, business_date,"
        " recorded_at, idempotency_key, payload, content_sha256, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            segment_id,
            event_type,
            business_date,
            recorded_at,
            idempotency_key,
            canonical_json_bytes(normalized).decode("utf-8"),
            content_hash,
            utc_now_iso(),
        ),
    )
    return {"event_id": event_id, "replayed": False}


def get_simulation_account_state(conn, segment_id: str) -> dict[str, Any]:
    """只读模拟账户状态（模拟域专属接口，不经过实际账本）。"""
    segment = conn.execute(
        "SELECT segment_id, simulation_account_id FROM simulation_segment WHERE segment_id=?",
        (segment_id,),
    ).fetchone()
    if segment is None:
        raise DataError("s5_segment_not_found", f"模拟段 {segment_id} 不存在")
    state = _load_state_row(conn, segment_id)
    if state is None:
        raise DataError("s5_state_missing", f"模拟段 {segment_id} 缺少期初状态")
    return {
        "segment_id": str(segment[0]),
        "account_id": str(segment[1]),
        "revision": int(state["revision"]),
        "as_of": str(state["as_of"]),
        "last_advanced_date": state["last_advanced_date"],
        "cash_cents": int(state["cash_cents"]),
        "positions": _load_positions(conn, segment_id),
    }


def apply_simulation_corporate_action(
    conn,
    segment_id: str,
    *,
    ts_code: str,
    share_delta: int,
    cash_delta_cents: int,
    business_date: str,
    idempotency_key: str,
    new_total_cost_cents: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """在模拟域登记已确认公司行为（现金分红/送转股），绝不写实际账本。"""
    business_date = _require_yyyymmdd(business_date, "s5_business_date_invalid")
    with _atomic_write(conn):
        segment = conn.execute(
            "SELECT status FROM simulation_segment WHERE segment_id=?", (segment_id,)
        ).fetchone()
        if segment is None:
            raise DataError("s5_segment_not_found", f"模拟段 {segment_id} 不存在")
        if str(segment[0]) != "active":
            raise DataError("s5_segment_closed", f"模拟段 {segment_id} 已关闭")
        event = _post_simulation_event(
            conn,
            segment_id=segment_id,
            event_type="corporate_action",
            business_date=business_date,
            idempotency_key=idempotency_key,
            payload={
                "ts_code": ts_code,
                "share_delta": share_delta,
                "cash_delta_cents": cash_delta_cents,
                "new_total_cost_cents": new_total_cost_cents,
                "note": note,
            },
            recorded_at=utc_now_iso(),
            reason="S5b 已确认公司行为（模拟域）",
        )
    return event


def list_simulation_events(conn, segment_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """有界只读模拟事件清单；按生效日/记录时间稳定排序。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 500:
        limit = 200
    rows = conn.execute(
        "SELECT event_id, event_type, business_date, recorded_at, idempotency_key,"
        " payload, content_sha256, created_at FROM simulation_event"
        " WHERE segment_id=? ORDER BY business_date, recorded_at, event_id LIMIT ?",
        (segment_id, int(limit)),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row[5]))
        except (TypeError, ValueError):
            payload = None
        events.append(
            {
                "event_id": str(row[0]),
                "event_type": str(row[1]),
                "business_date": str(row[2]),
                "recorded_at": str(row[3]),
                "idempotency_key": str(row[4]),
                "payload": payload if isinstance(payload, dict) else None,
                "content_sha256": str(row[6]),
                "created_at": str(row[7]),
            }
        )
    return events


# ------------------------------------------------------------------ 创建模拟段


def create_simulation_segment(
    conn,
    *,
    source_account_id: str,
    base_account_snapshot_id: str,
    rule_bundle_id: str,
    fee_bundle_id: str,
    source_code: str,
    data_version_id: str,
    as_of: str,
) -> dict[str, Any]:
    """从显式选择的已持久化账户快照创建（或复用）一个模拟段。

    冻结口径：全输入显式、确定性；同输入重复请求返回既有段（幂等，不重复
    复制状态、不重复登记事件）。绝不改写源账户；期初只写模拟域表。
    """
    if not isinstance(source_account_id, str) or not source_account_id.strip():
        raise DataError("s5_source_account_required", "必须显式提供源实际账户 account_id")
    as_of = _require_yyyymmdd(as_of, "s5_as_of_invalid")
    source_row = conn.execute(
        "SELECT kind FROM account WHERE account_id=?", (source_account_id,)
    ).fetchone()
    if source_row is None:
        raise DataError("s5_source_account_not_found", f"源账户 {source_account_id} 不存在")
    if str(source_row[0]) != "actual":
        raise DataError(
            "s5_source_account_not_actual",
            f"模拟段只能从一个 kind='actual' 的实际账户创建: {source_account_id}",
        )
    snapshot_row = conn.execute(
        "SELECT account_id, account_revision, as_of, cash_cents FROM account_snapshot"
        " WHERE snapshot_id=?",
        (base_account_snapshot_id,),
    ).fetchone()
    if snapshot_row is None:
        raise DataError(
            "s5_base_snapshot_not_found",
            f"基线账户快照 {base_account_snapshot_id} 不存在；模拟段必须绑定已持久化快照",
        )
    if str(snapshot_row[0]) != source_account_id:
        raise DataError(
            "s5_base_snapshot_owner_mismatch",
            f"快照 {base_account_snapshot_id} 属于账户 {snapshot_row[0]}，"
            f"与源账户 {source_account_id} 不一致",
        )
    snapshot_as_of = _require_yyyymmdd(
        str(snapshot_row[2]), "s5_base_snapshot_as_of_invalid"
    )
    if snapshot_as_of > as_of:
        raise DataError(
            "s5_base_snapshot_after_segment_as_of",
            f"基线快照生效日 {snapshot_as_of} 晚于模拟段 as_of {as_of}；"
            "拒绝用未来状态创建更早的模拟段",
        )
    if not isinstance(source_code, str) or not source_code.strip():
        raise DataError("s5_source_code_required", "必须显式提供 source_code")
    if not isinstance(data_version_id, str) or not data_version_id.strip():
        raise DataError("s5_data_version_required", "必须显式提供 data_version_id")
    version_row = conn.execute(
        "SELECT source_code FROM data_version WHERE version_id=?", (data_version_id,)
    ).fetchone()
    if version_row is None:
        raise DataError("s5_data_version_not_found", f"数据版本 {data_version_id} 不存在")
    if str(version_row[0]) != source_code:
        raise DataError(
            "s5_source_mismatch",
            f"数据版本 {data_version_id} 属于来源 {version_row[0]}，与请求来源 {source_code} 不一致",
        )
    rule_bundle = load_rule_bundle(conn, rule_bundle_id)
    if rule_bundle is None:
        raise DataError("s5_rule_bundle_not_found", f"规则组合 {rule_bundle_id} 不存在")
    fee_bundle = load_rule_bundle(conn, fee_bundle_id)
    if fee_bundle is None:
        raise DataError("s5_fee_bundle_not_found", f"费用组合 {fee_bundle_id} 不存在")
    if fee_bundle.get("kind") != "fee":
        raise DataError("s5_fee_bundle_kind_mismatch", f"{fee_bundle_id} 不是费用组合")
    input_payload: dict[str, Any] = {
        "application_version": SIMULATION_APPLICATION_VERSION,
        "source_account_id": source_account_id,
        "base_account_snapshot_id": base_account_snapshot_id,
        "rule_bundle_id": rule_bundle["bundle_id"],
        "fee_bundle_id": fee_bundle["bundle_id"],
        "source_code": source_code,
        "data_version_id": data_version_id,
        "as_of": as_of,
    }
    input_sha256 = sha256_of_object(input_payload)
    existing = conn.execute(
        "SELECT segment_id, simulation_account_id, status FROM simulation_segment"
        " WHERE input_sha256=?",
        (input_sha256,),
    ).fetchone()
    if existing is not None:
        return {
            "segment_id": str(existing[0]),
            "simulation_account_id": str(existing[1]),
            "status": str(existing[2]),
            "input_sha256": input_sha256,
            "reused": True,
        }

    created_at = utc_now_iso()
    segment_id = "segsim_" + input_sha256[:20]
    simulation_account_id = "sim_" + input_sha256[:16]
    frozen_state = get_account_snapshot_state(
        conn, source_account_id, revision=int(snapshot_row[1])
    )
    if frozen_state is None:
        raise DataError(
            "s5_base_snapshot_state_missing",
            f"基线快照 {base_account_snapshot_id} 的冻结状态不可读；拒绝以部分状态模拟",
        )
    seed_positions: list[dict[str, Any]] = []
    for position in frozen_state["positions"]:
        seed_positions.append(
            {
                "ts_code": str(position["ts_code"]),
                "shares": int(position["shares"]),
                "total_cost_cents": (
                    None if position["total_cost_cents"] is None else int(position["total_cost_cents"])
                ),
                "sellable_shares": (
                    None if position["sellable_shares"] is None else int(position["sellable_shares"])
                ),
                "acquire_date": position["acquire_date"],
                "locked": int(position["locked"] or 0),
                "price_status": "user_reported",
                "note": position.get("note"),
            }
        )

    with _atomic_write(conn):
        raced = conn.execute(
            "SELECT segment_id, simulation_account_id, status FROM simulation_segment"
            " WHERE input_sha256=?",
            (input_sha256,),
        ).fetchone()
        if raced is not None:
            return {
                "segment_id": str(raced[0]),
                "simulation_account_id": str(raced[1]),
                "status": str(raced[2]),
                "input_sha256": input_sha256,
                "reused": True,
            }
        already_sim = conn.execute(
            "SELECT 1 FROM account WHERE account_id=? AND kind='simulation'"
            " AND simulation_of_account_id=?",
            (simulation_account_id, source_account_id),
        ).fetchone()
        if already_sim is None:
            conn.execute(
                "INSERT INTO account(account_id, kind, display_name, base_currency,"
                " account_revision, simulation_of_account_id, created_at, updated_at)"
                " VALUES (?, 'simulation', ?, 'CNY', 0, ?, ?, ?)",
                (
                    simulation_account_id,
                    f"模拟账户（{source_account_id} 的 S5a 快照段）",
                    source_account_id,
                    created_at,
                    created_at,
                ),
            )
        conn.execute(
            "INSERT INTO simulation_segment(segment_id, source_account_id, simulation_account_id,"
            " base_account_snapshot_id, rule_bundle_id, fee_bundle_id, source_code, data_version_id,"
            " as_of, input_sha256, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                segment_id,
                source_account_id,
                simulation_account_id,
                base_account_snapshot_id,
                rule_bundle["bundle_id"],
                fee_bundle["bundle_id"],
                source_code,
                data_version_id,
                as_of,
                input_sha256,
                created_at,
            ),
        )
        # 期初只写模拟域：冻结状态来自基线快照，不访问当前实际账户。
        _post_simulation_event(
            conn,
            segment_id=segment_id,
            event_type="opening",
            business_date=snapshot_as_of,
            idempotency_key=f"sim-seed:{segment_id}",
            payload={
                "cash_cents": int(frozen_state["cash_cents"]),
                "positions": seed_positions,
                "note": f"S5a 种子自基线快照 {base_account_snapshot_id}",
            },
            recorded_at=created_at,
            reason="S5a 模拟段冻结状态复制（模拟域）",
        )
        register_reference_holds(
            conn,
            [
                ReferenceTarget(
                    target_type="version",
                    target_id=data_version_id,
                    reason="simulation_frozen_market_evidence",
                )
            ],
            reason_prefix=f"simulation_segment:{segment_id}",
        )
    return {
        "segment_id": segment_id,
        "simulation_account_id": simulation_account_id,
        "status": "active",
        "input_sha256": input_sha256,
        "reused": False,
    }


# ------------------------------------------------------------------ 订单


def place_simulation_order(
    conn,
    segment_id: str,
    *,
    ts_code: str,
    direction: str,
    planned_shares: int,
    trade_date: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """登记一个 pending 订单；trade_date 必须严格晚于段 as_of（处理日边界）。"""
    with _atomic_write(conn):
        segment = conn.execute(
            "SELECT segment_id, simulation_account_id, status, as_of FROM simulation_segment"
            " WHERE segment_id=?",
            (segment_id,),
        ).fetchone()
        if segment is None:
            raise DataError("s5_segment_not_found", f"模拟段 {segment_id} 不存在")
        if str(segment[2]) != "active":
            raise DataError("s5_segment_closed", f"模拟段 {segment_id} 已关闭，不再接受订单")
        if direction not in ("buy", "sell"):
            raise DataError("s5_direction_unknown", f"未知方向: {direction!r}；只支持 buy/sell")
        code = _validate_ts_code(ts_code)
        if (
            isinstance(planned_shares, bool)
            or not isinstance(planned_shares, int)
            or planned_shares < 1
        ):
            raise DataError(
                "s5_planned_shares_invalid", f"planned_shares 必须是正整数: {planned_shares!r}"
            )
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise DataError("s5_idempotency_key_required", "订单必须显式提供 idempotency_key")
        trade_date = _require_yyyymmdd(trade_date, "s5_trade_date_invalid")
        if trade_date <= str(segment[3]):
            raise DataError(
                "s5_trade_date_not_future",
                f"订单处理日 {trade_date} 必须晚于段 as_of {segment[3]}；"
                "不得在冻结时点当日假装成交",
            )
        # 幂等重放必须先于推进时点守卫：同一键同内容的重复提交即使在已推进到
        # 该日之后，也应返回既有订单，而不是被“禁止补录过去订单”误拒。
        existing = conn.execute(
            "SELECT order_id, status, planned_shares, ts_code, direction, trade_date"
            " FROM simulation_order WHERE segment_id=? AND idempotency_key=?",
            (str(segment[0]), idempotency_key),
        ).fetchone()
        if existing is not None:
            if (
                int(existing[2]) != planned_shares
                or str(existing[3]) != code
                or str(existing[4]) != direction
                or str(existing[5]) != trade_date
            ):
                raise DataError(
                    "s5_order_idempotency_conflict",
                    f"idempotency_key {idempotency_key} 已被不同内容的订单占用",
                )
            return {
                "order_id": str(existing[0]),
                "status": str(existing[1]),
                "reused": True,
            }
        state = _load_state_row(conn, str(segment[0]))
        if (
            state is not None
            and state["last_advanced_date"] is not None
            and trade_date <= str(state["last_advanced_date"])
        ):
            raise DataError(
                "s5_trade_date_already_advanced",
                f"订单处理日 {trade_date} 不晚于该段已推进到的 "
                f"{state['last_advanced_date']}；禁止事后补入过去订单",
            )
        order_id = "so_" + sha256_of_object(
            [str(segment[0]), idempotency_key, code, direction, planned_shares, trade_date]
        )[:20]
        conn.execute(
            "INSERT INTO simulation_order(order_id, segment_id, trade_date, ts_code, direction,"
            " planned_shares, filled_shares, remaining_shares, status, reason_code,"
            " model_price_cents, fee_estimate_cents, slippage_estimate_cents,"
            " last_attempt_date, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'pending', NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (
                order_id,
                str(segment[0]),
                trade_date,
                code,
                direction,
                planned_shares,
                planned_shares,
                idempotency_key,
                utc_now_iso(),
            ),
        )
    return {"order_id": order_id, "status": "pending", "reused": False}


def _mark_order(
    conn,
    order_id: str,
    *,
    status: str,
    filled_shares: int,
    planned_shares: int,
    reason_code: str | None,
    model_price_cents: int | None,
    fee_estimate_cents: int | None,
    slippage_estimate_cents: int | None = None,
) -> None:
    conn.execute(
        "UPDATE simulation_order SET status=?, filled_shares=?, remaining_shares=?,"
        " reason_code=?, model_price_cents=?, fee_estimate_cents=?,"
        " slippage_estimate_cents=? WHERE order_id=?",
        (
            status,
            filled_shares,
            planned_shares - filled_shares,
            reason_code,
            model_price_cents,
            fee_estimate_cents,
            slippage_estimate_cents,
            order_id,
        ),
    )


def _insert_fill(
    conn,
    order_id: str,
    trade_date: str,
    fill_seq: int,
    filled_shares: int,
    price_cents: int,
    fee_cents: int,
    slippage_cents: int,
    breakdown: Mapping[str, Any],
) -> str:
    fill_id = "sfill_" + sha256_of_object(
        [order_id, fill_seq, trade_date, filled_shares, price_cents, fee_cents, slippage_cents]
    )[:20]
    conn.execute(
        "INSERT INTO simulation_fill(fill_id, order_id, fill_seq, trade_date,"
        " filled_shares, price_cents, fee_cents, slippage_cents, fee_breakdown, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fill_id,
            order_id,
            fill_seq,
            trade_date,
            filled_shares,
            price_cents,
            fee_cents,
            slippage_cents,
            canonical_json_bytes({**dict(breakdown), "slippage_applied_cents": slippage_cents}).decode(
                "utf-8"
            ),
            utc_now_iso(),
        ),
    )
    return fill_id


def _no_fill(
    conn,
    order_id: str,
    *,
    status: str,
    filled_shares: int,
    planned_shares: int,
    reason_code: str,
    model_price_cents: int | None = None,
) -> dict[str, Any]:
    """本次未成交：pending 记 unfilled（终态）；partially_filled 保留并可后续重试。"""
    if status == "partially_filled":
        conn.execute(
            "UPDATE simulation_order SET reason_code=?, model_price_cents=?"
            " WHERE order_id=?",
            (reason_code, model_price_cents, order_id),
        )
        return {
            "order_id": order_id,
            "status": "partially_filled",
            "filled_shares": filled_shares,
            "reason_code": reason_code,
        }
    _mark_order(
        conn,
        order_id,
        status="unfilled",
        filled_shares=0,
        planned_shares=planned_shares,
        reason_code=reason_code,
        model_price_cents=model_price_cents,
        fee_estimate_cents=None,
        slippage_estimate_cents=None,
    )
    return {"order_id": order_id, "status": "unfilled", "reason_code": reason_code}


def _refresh_sellable_columns(conn, segment_id: str, on_date: str) -> None:
    rows = conn.execute(
        "SELECT ts_code FROM simulation_position_state WHERE segment_id=?",
        (segment_id,),
    ).fetchall()
    for row in rows:
        code = str(row[0])
        available = _available_sellable(conn, segment_id, code, on_date)
        unknown = _unknown_lot_shares(conn, segment_id, code)
        value = None if (available == 0 and unknown > 0) else available
        conn.execute(
            "UPDATE simulation_position_state SET sellable_shares=?"
            " WHERE segment_id=? AND ts_code=?",
            (value, segment_id, code),
        )


def _next_trading_day(view: VersionView, after: str) -> str | None:
    """冻结日历中严格晚于 after 的第一个交易日；没有则 None（未知，不猜）。"""
    for day in view.calendar_days():
        if day > after:
            return day
    return None


def _process_pending_order(
    conn,
    view: VersionView,
    segment: Mapping[str, str],
    schedule: Mapping[str, Mapping[str, Any]] | None,
    market_rules: Mapping[str, Any] | None,
    order_row: Mapping[str, Any],
    *,
    execution_date: str,
    attempt_date: str,
    same_day_reuse: bool | None = None,
    cash_budget: int | None = None,
) -> dict[str, Any]:
    """处理一个 pending/partially_filled 订单；缺证据 fail-closed，持仓不动。

    ``cash_budget`` 是当日允许买入使用的现金上限（不含未获规则允许的同日卖出
    收入）；``same_day_reuse`` 为规则包显式布尔值，缺失时买入 fail-closed。
    部分成交订单的剩余量在后续有效交易日继续尝试；关闭时由 close 置 expired。
    """
    order_id = str(order_row["order_id"])
    planned_total = int(order_row["planned_shares"])
    filled_before = int(order_row["filled_shares"])
    attempt = int(order_row["remaining_shares"])
    order_status = str(order_row["status"])
    code = str(order_row["ts_code"])
    side = str(order_row["direction"])
    segment_id = str(segment["segment_id"])
    trade_date = execution_date
    # 持久化本次推进尝试日（不是订单计划日）：同一交易日重复推进不会再尝试/
    # 追加成交；后续交易日仍可继续。
    conn.execute(
        "UPDATE simulation_order SET last_attempt_date=? WHERE order_id=?",
        (attempt_date, order_id),
    )

    trading_day = view.is_trading_day(trade_date)
    if trading_day is None:
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_calendar_unknown",
        )
    if not trading_day:
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_not_a_trading_day",
        )
    if market_rules is None:
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_market_rule_evidence_missing",
        )

    board_lot: int | None = None
    if side == "buy":
        board_lot_raw = market_rules.get("board_lot_shares")
        if (
            isinstance(board_lot_raw, bool)
            or not isinstance(board_lot_raw, int)
            or board_lot_raw < 1
        ):
            return _no_fill(
                conn, order_id, status=order_status, filled_shares=filled_before,
                planned_shares=planned_total, reason_code="s5_board_lot_evidence_missing",
            )
        board_lot = int(board_lot_raw)
        if attempt % board_lot != 0:
            _mark_order(
                conn,
                order_id,
                status="rejected",
                filled_shares=filled_before,
                planned_shares=planned_total,
                reason_code="s5_board_lot_not_multiple",
                model_price_cents=None,
                fee_estimate_cents=None,
                slippage_estimate_cents=None,
            )
            return {
                "order_id": order_id,
                "status": "rejected",
                "reason_code": "s5_board_lot_not_multiple",
            }

    if not _suspension_evidence_ok(
        view, market_rules, code=code, trade_date=trade_date
    ):
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_suspension_evidence_missing",
        )
    if view.is_suspended(code, trade_date):
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_suspended",
        )
    daily_row = view.daily_on(code, trade_date)
    open_price_cents = None if daily_row is None else _price_to_cents(daily_row["open"])
    if open_price_cents is None:
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_open_price_missing",
        )
    limit_bounds = _price_limit_bounds(market_rules, code=code, trade_date=trade_date)
    if limit_bounds is None:
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_price_limit_evidence_missing",
            model_price_cents=open_price_cents,
        )
    lower_cents, upper_cents = limit_bounds
    if open_price_cents <= lower_cents or open_price_cents >= upper_cents:
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_open_at_price_limit",
            model_price_cents=open_price_cents,
        )

    if schedule is None or not schedule:
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_fee_evidence_missing",
            model_price_cents=open_price_cents,
        )

    t_plus_one = market_rules.get("t_plus_one_sell_restriction")
    if not isinstance(t_plus_one, bool):
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_t_plus_one_evidence_missing",
            model_price_cents=open_price_cents,
        )

    sim_state = get_simulation_account_state(conn, segment_id)
    position = next(
        (p for p in sim_state["positions"] if p["ts_code"] == code), None
    )
    prior_row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(fee_cents), 0), COALESCE(SUM(slippage_cents), 0)"
        " FROM simulation_fill WHERE order_id=?",
        (order_id,),
    ).fetchone()
    prior_count = int(prior_row[0]) if prior_row is not None else 0
    prior_fee = int(prior_row[1]) if prior_row is not None else 0
    prior_slippage = int(prior_row[2]) if prior_row is not None else 0
    apply_minimum = prior_count == 0

    fillable = attempt
    unlock_date: str | None = None
    if side == "sell":
        if position is None or int(position["shares"]) <= 0:
            return _no_fill(
                conn, order_id, status=order_status, filled_shares=filled_before,
                planned_shares=planned_total, reason_code="s5_position_missing",
                model_price_cents=open_price_cents,
            )
        if position["locked"]:
            return _no_fill(
                conn, order_id, status=order_status, filled_shares=filled_before,
                planned_shares=planned_total, reason_code="s5_position_locked",
                model_price_cents=open_price_cents,
            )
        available = _available_sellable(conn, segment_id, code, trade_date)
        unknown = _unknown_lot_shares(conn, segment_id, code)
        fillable = min(attempt, available)
        if fillable < 1:
            reason = (
                "s5_position_sellable_unknown"
                if available == 0 and unknown > 0
                else "s5_limit_reduced_by_sellable_shares"
            )
            return _no_fill(
                conn, order_id, status=order_status, filled_shares=filled_before,
                planned_shares=planned_total, reason_code=reason,
                model_price_cents=open_price_cents,
            )
    else:
        if same_day_reuse is None:
            return _no_fill(
                conn, order_id, status=order_status, filled_shares=filled_before,
                planned_shares=planned_total, reason_code="s5_same_day_cash_rule_missing",
                model_price_cents=open_price_cents,
            )
        cash_ok = (
            isinstance(sim_state["cash_cents"], int)
            and not isinstance(sim_state["cash_cents"], bool)
        )
        if not cash_ok or cash_budget is None:
            return _no_fill(
                conn, order_id, status=order_status, filled_shares=filled_before,
                planned_shares=planned_total, reason_code="s5_cash_state_unknown",
                model_price_cents=open_price_cents,
            )
        assert board_lot is not None
        fillable = _max_affordable_shares(
            schedule,
            price_cents=open_price_cents,
            board_lot=board_lot,
            planned_shares=attempt,
            cash_budget=int(cash_budget),
            apply_minimum=apply_minimum,
        )
        if fillable < board_lot:
            return _no_fill(
                conn, order_id, status=order_status, filled_shares=filled_before,
                planned_shares=planned_total, reason_code="s5_insufficient_funds",
                model_price_cents=open_price_cents,
            )
        if int(cash_budget) > int(sim_state["cash_cents"]):
            fillable = _max_affordable_shares(
                schedule,
                price_cents=open_price_cents,
                board_lot=board_lot,
                planned_shares=attempt,
                cash_budget=int(sim_state["cash_cents"]),
                apply_minimum=apply_minimum,
            )
        if fillable < board_lot:
            return _no_fill(
                conn, order_id, status=order_status, filled_shares=filled_before,
                planned_shares=planned_total, reason_code="s5_insufficient_funds",
                model_price_cents=open_price_cents,
            )
        if t_plus_one is True:
            unlock_date = _next_trading_day(view, trade_date)
        else:
            unlock_date = trade_date

    notional = fillable * open_price_cents
    estimate = _order_fee_estimate(
        schedule, side, notional, apply_minimum=apply_minimum
    )
    fee_cents = int(estimate["fee_cents"])
    slippage_cents = int(estimate["slippage_cents"])
    if (
        side == "sell"
        and int(sim_state["cash_cents"]) + notional - fee_cents - slippage_cents < 0
    ):
        return _no_fill(
            conn, order_id, status=order_status, filled_shares=filled_before,
            planned_shares=planned_total, reason_code="s5_insufficient_cash",
            model_price_cents=open_price_cents,
        )

    fill_seq = conn.execute(
        "SELECT COALESCE(MAX(fill_seq), 0) + 1 FROM simulation_fill WHERE order_id=?",
        (order_id,),
    ).fetchone()[0]
    event = _post_simulation_event(
        conn,
        segment_id=segment_id,
        event_type=side,
        business_date=trade_date,
        idempotency_key=f"simfill:{order_id}:{int(fill_seq)}",
        payload={
            "ts_code": code,
            "shares": fillable,
            "price_cents": open_price_cents,
            "fee_cents": fee_cents,
            "slippage_cents": slippage_cents,
            "note": "S5a 模拟成交（模型价，语义仅模拟，非真实执行）",
        },
        recorded_at=utc_now_iso(),
        reason="S5a 模拟成交（模拟域）",
        unlock_date=unlock_date,
    )
    _insert_fill(
        conn,
        order_id,
        trade_date,
        int(fill_seq),
        fillable,
        open_price_cents,
        fee_cents,
        slippage_cents,
        estimate["breakdown"],
    )
    new_filled = filled_before + fillable
    status = "filled" if new_filled == planned_total else "partially_filled"
    reason_code = None
    if status == "partially_filled":
        reason_code = (
            "s5_limit_reduced_by_sellable_shares" if side == "sell" else "s5_insufficient_funds"
        )
    _mark_order(
        conn,
        order_id,
        status=status,
        filled_shares=new_filled,
        planned_shares=planned_total,
        reason_code=reason_code,
        model_price_cents=open_price_cents,
        fee_estimate_cents=prior_fee + fee_cents,
        slippage_estimate_cents=prior_slippage + slippage_cents,
    )
    return {
        "order_id": order_id,
        "status": status,
        "filled_shares": fillable,
        "event_id": event["event_id"],
        "net_proceeds_cents": (
            notional - fee_cents - slippage_cents if side == "sell" else 0
        ),
        "cash_spent_cents": (
            notional + fee_cents + slippage_cents if side == "buy" else 0
        ),
    }


def advance_simulation_segment(conn, segment_id: str, *, on_trade_date: str) -> dict[str, Any]:
    """推进段内 pending 订单；sell 先于 buy；整批原子提交，重复调用绝不重复记账。

    只处理 ``trade_date <= on_trade_date`` 的订单（未来日期订单保持 pending）。
    """
    on_trade_date = _require_yyyymmdd(on_trade_date, "s5_on_trade_date_invalid")
    with _atomic_write(conn):
        segment_row = conn.execute(
            "SELECT segment_id, simulation_account_id, source_account_id, rule_bundle_id,"
            " fee_bundle_id, source_code, data_version_id, as_of, status"
            " FROM simulation_segment WHERE segment_id=?",
            (segment_id,),
        ).fetchone()
        if segment_row is None:
            raise DataError("s5_segment_not_found", f"模拟段 {segment_id} 不存在")
        column_names = (
            "segment_id",
            "simulation_account_id",
            "source_account_id",
            "rule_bundle_id",
            "fee_bundle_id",
            "source_code",
            "data_version_id",
            "as_of",
            "status",
        )
        segment = dict(zip(column_names, segment_row))
        state_row = _load_state_row(conn, segment_id)
        if state_row is None:
            raise DataError("s5_state_missing", f"模拟段 {segment_id} 缺少期初状态")
        last_advanced = state_row["last_advanced_date"]
        if last_advanced is not None and on_trade_date < last_advanced:
            raise DataError(
                "s5_on_trade_date_regressed",
                f"推进日 {on_trade_date} 早于该段已推进到的 {last_advanced}；"
                "禁止逆序推进或事后补执行过去交易日",
            )
        if segment["status"] != "active":
            pending_left = conn.execute(
                "SELECT COUNT(*) FROM simulation_order WHERE segment_id=? AND status IN"
                "('pending','partially_filled')",
                (str(segment["segment_id"]),),
            ).fetchone()[0]
            if pending_left == 0:
                return {
                    "segment_id": str(segment["segment_id"]),
                    "status": "closed",
                    "processed": [],
                    "processed_count": 0,
                }
            raise DataError(
                "s5_segment_closed",
                f"模拟段 {segment_id} 已关闭但仍有未终止订单 {int(pending_left)} 行；"
                "先显式终止（cancel/close）再推进",
            )
        version_row = conn.execute(
            "SELECT source_code FROM data_version WHERE version_id=?",
            (segment["data_version_id"],),
        ).fetchone()
        if version_row is None:
            raise DataError(
                "s5_data_version_not_found", f"数据版本 {segment['data_version_id']} 不存在"
            )
        view = VersionView(
            conn, segment["data_version_id"], expected_source_code=segment["source_code"]
        )
        market_rules = _load_market_rules(conn, str(segment["rule_bundle_id"]))
        reuse_raw = (
            market_rules.get("same_day_sale_proceeds_reusable")
            if market_rules is not None
            else None
        )
        same_day_reuse = reuse_raw if isinstance(reuse_raw, bool) else None
        fee_bundle = load_rule_bundle(conn, str(segment["fee_bundle_id"]))
        schedule = _fee_schedule(fee_bundle) if fee_bundle is not None else None
        if fee_bundle is not None and not schedule:
            schedule = None  # 缺证据 → 每个订单在处理时以 s5_fee_evidence_missing fail-closed

        orders = conn.execute(
            "SELECT order_id, trade_date, ts_code, direction, planned_shares,"
            " filled_shares, remaining_shares, status FROM simulation_order"
            " WHERE segment_id=? AND status IN ('pending','partially_filled')"
            " AND trade_date<=?"
            " AND (last_attempt_date IS NULL OR last_attempt_date < ?)"
            " ORDER BY trade_date, direction DESC, created_at, order_id",
            (segment_id, on_trade_date, on_trade_date),
        ).fetchall()
        names = (
            "order_id",
            "trade_date",
            "ts_code",
            "direction",
            "planned_shares",
            "filled_shares",
            "remaining_shares",
            "status",
        )
        order_dicts = [dict(zip(names, row)) for row in orders]
        by_date: dict[str, list[dict[str, Any]]] = {}
        for item in order_dicts:
            # 首次执行按订单计划日；部分成交的剩余量在当前推进日继续尝试。
            execution_date = (
                str(item["trade_date"])
                if str(item["status"]) == "pending"
                else on_trade_date
            )
            item["execution_date"] = execution_date
            by_date.setdefault(execution_date, []).append(item)
        processed: list[dict[str, Any]] = []
        for trade_date in sorted(by_date):
            day_orders = by_date[trade_date]
            sells = [item for item in day_orders if str(item["direction"]) == "sell"]
            buys = [item for item in day_orders if str(item["direction"]) == "buy"]
            # 同一处理日内先卖后买；只有该日规则允许时，当日卖出收入才进入买入预算。
            day_start_cash = int(
                get_simulation_account_state(conn, segment_id)["cash_cents"]
            )
            sale_proceeds = 0
            for order in sells:
                outcome = _process_pending_order(
                    conn,
                    view,
                    segment,
                    schedule,
                    market_rules,
                    order,
                    execution_date=str(order["execution_date"]),
                    attempt_date=on_trade_date,
                    same_day_reuse=same_day_reuse,
                    cash_budget=None,
                )
                processed.append(outcome)
                sale_proceeds += int(outcome.get("net_proceeds_cents") or 0)
            buy_budget = day_start_cash + (
                sale_proceeds if same_day_reuse is True else 0
            )
            for order in buys:
                outcome = _process_pending_order(
                    conn,
                    view,
                    segment,
                    schedule,
                    market_rules,
                    order,
                    execution_date=str(order["execution_date"]),
                    attempt_date=on_trade_date,
                    same_day_reuse=same_day_reuse,
                    cash_budget=buy_budget,
                )
                processed.append(outcome)
                buy_budget -= int(outcome.get("cash_spent_cents") or 0)
        _refresh_sellable_columns(conn, segment_id, on_trade_date)
        conn.execute(
            "UPDATE simulation_account_state SET last_advanced_date=?, updated_at=?"
            " WHERE segment_id=?",
            (on_trade_date, utc_now_iso(), segment_id),
        )
    return {
        "segment_id": segment_id,
        "on_trade_date": on_trade_date,
        "processed": processed,
        "processed_count": len(processed),
    }


def cancel_simulation_order(conn, order_id: str, *, reason: str) -> dict[str, Any]:
    """显式取消：pending → cancelled；已处理订单不可改写。"""
    if not isinstance(reason, str) or not reason.strip():
        raise DataError("s5_cancel_reason_required", "取消必须显式登记原因")
    with _atomic_write(conn):
        row = conn.execute(
            "SELECT status FROM simulation_order WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None:
            raise DataError("s5_order_not_found", f"模拟订单 {order_id} 不存在")
        if str(row[0]) != "pending":
            raise DataError(
                "s5_order_not_pending",
                f"订单 {order_id} 状态为 {row[0]}，只有 pending 订单可取消",
            )
        conn.execute(
            "UPDATE simulation_order SET status='cancelled', reason_code=? WHERE order_id=?",
            ("s5_cancelled:" + reason.strip(), order_id),
        )
    return {"order_id": order_id, "status": "cancelled", "reused": False}


def close_simulation_segment(conn, segment_id: str, *, reason: str) -> dict[str, Any]:
    """关闭段并把 pending/partially_filled 剩余量标记 expired；成交不再改动。"""
    if not isinstance(reason, str) or not reason.strip():
        raise DataError("s5_close_reason_required", "关闭必须显式登记原因")
    with _atomic_write(conn):
        row = conn.execute(
            "SELECT status FROM simulation_segment WHERE segment_id=?", (segment_id,)
        ).fetchone()
        if row is None:
            raise DataError("s5_segment_not_found", f"模拟段 {segment_id} 不存在")
        if str(row[0]) == "closed":
            return {"segment_id": segment_id, "status": "closed", "reused": True}
        conn.execute(
            "UPDATE simulation_order SET status='expired',"
            " reason_code=? WHERE segment_id=? AND status IN ('pending','partially_filled')",
            ("s5_expired_on_segment_close:" + reason.strip(), segment_id),
        )
        conn.execute(
            "UPDATE simulation_segment SET status='closed' WHERE segment_id=?", (segment_id,)
        )
    return {"segment_id": segment_id, "status": "closed", "reused": False}


# ------------------------------------------------------------------ 只读


def list_simulation_segments(
    conn, source_account_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = 50
    limit = min(limit, 200)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        offset = 0
    rows = conn.execute(
        "SELECT segment_id, source_account_id, simulation_account_id, base_account_snapshot_id,"
        " rule_bundle_id, fee_bundle_id, source_code, data_version_id, as_of, status, created_at"
        " FROM simulation_segment WHERE source_account_id=?"
        " ORDER BY created_at DESC, segment_id DESC LIMIT ? OFFSET ?",
        (source_account_id, limit, offset),
    ).fetchall()
    names = (
        "segment_id",
        "source_account_id",
        "simulation_account_id",
        "base_account_snapshot_id",
        "rule_bundle_id",
        "fee_bundle_id",
        "source_code",
        "data_version_id",
        "as_of",
        "status",
        "created_at",
    )
    return [dict(zip(names, row)) for row in rows]


def get_simulation_segment(conn, segment_id: str) -> dict[str, Any]:
    """只读清单：段 + 订单 + 成交 + 模拟域状态（不重算、不改写）。"""
    segment_row = conn.execute(
        "SELECT segment_id, source_account_id, simulation_account_id, base_account_snapshot_id,"
        " rule_bundle_id, fee_bundle_id, source_code, data_version_id, as_of, input_sha256,"
        " status, created_at FROM simulation_segment WHERE segment_id=?",
        (segment_id,),
    ).fetchone()
    if segment_row is None:
        raise DataError("s5_segment_not_found", f"模拟段 {segment_id} 不存在")
    names = (
        "segment_id",
        "source_account_id",
        "simulation_account_id",
        "base_account_snapshot_id",
        "rule_bundle_id",
        "fee_bundle_id",
        "source_code",
        "data_version_id",
        "as_of",
        "input_sha256",
        "status",
        "created_at",
    )
    segment = dict(zip(names, segment_row))
    orders = conn.execute(
        "SELECT order_id, trade_date, ts_code, direction, planned_shares, filled_shares,"
        " remaining_shares, status, reason_code, model_price_cents, fee_estimate_cents,"
        " slippage_estimate_cents, last_attempt_date, idempotency_key, created_at"
        " FROM simulation_order WHERE segment_id=?"
        " ORDER BY trade_date, created_at, order_id",
        (segment_id,),
    ).fetchall()
    order_names = (
        "order_id",
        "trade_date",
        "ts_code",
        "direction",
        "planned_shares",
        "filled_shares",
        "remaining_shares",
        "status",
        "reason_code",
        "model_price_cents",
        "fee_estimate_cents",
        "slippage_estimate_cents",
        "last_attempt_date",
        "idempotency_key",
        "created_at",
    )
    fills = conn.execute(
        "SELECT f.fill_id, f.order_id, f.fill_seq, f.trade_date, f.filled_shares,"
        " f.price_cents, f.fee_cents, f.slippage_cents, f.fee_breakdown, f.created_at"
        " FROM simulation_fill f JOIN simulation_order o ON o.order_id=f.order_id"
        " WHERE o.segment_id=? ORDER BY f.trade_date, f.fill_seq, f.fill_id",
        (segment_id,),
    ).fetchall()
    fill_names = (
        "fill_id",
        "order_id",
        "fill_seq",
        "trade_date",
        "filled_shares",
        "price_cents",
        "fee_cents",
        "slippage_cents",
        "fee_breakdown",
        "created_at",
    )
    fill_items = []
    for row in fills:
        item = dict(zip(fill_names, row))
        try:
            item["breakdown"] = json.loads(str(item.pop("fee_breakdown")))
        except (TypeError, ValueError):
            item["breakdown"] = None
        fill_items.append(item)
    simulation_state = get_simulation_account_state(conn, segment_id)
    source_state = get_account_state(conn, str(segment["source_account_id"]))
    return {
        "segment": segment,
        "orders": [dict(zip(order_names, row)) for row in orders],
        "fills": fill_items,
        "simulation_state": simulation_state,
        "source_state_unchanged_check": {
            "account_id": str(segment["source_account_id"]),
            "revision": source_state["revision"],
            "cash_cents": source_state["cash_cents"],
        },
    }
