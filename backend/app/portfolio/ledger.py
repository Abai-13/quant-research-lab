"""阶段三 S2：实际账户账本——期初、追加事件、修订与对账。

账本约定版本 ``account-ledger-1.0``（S0/派工冻结语义的 S2 实现）：

- **追加式账本**：``account_event`` 只追加，从不更新或删除。更正事件以
  ``supersedes_event_id`` 显式替代被更正事件；被更正事件不允许被第二次更正，
  保证有效事件流无歧义。
- **状态是纯折叠**：账户状态 = 按确定顺序折叠全部有效事件的纯函数。有效事件流
  由"更正链根 → 链尾"片段组成，按 ``(business_date, 链根 recorded_at, 链根
  event_id)`` 排序；链尾内容在链根的原位置生效。更正必须与被更正事件同一生效日
  （``correction_business_date_mismatch``）——S2 更正不改生效日，生效日错误的
  记录须先显式澄清，不做静默重写。
- **修订与事件版本**：``account_revision`` 账户本地单调——账户创建时为 0，每次
  成功提交 +1，同时追加一份新的 ``account_snapshot``（现金 + 全部持仓行）；
  修订第 R 次提交对应快照 revision=R（事件行按 rowid 顺序即提交顺序）。
  ``event_version`` 同为账户本地单调：链根为 1，每个更正 = 其被更正行 + 1。
  旧快照永不被改写，供"当时已知"审计读取。
- **幂等**：幂等键必须为调用方提供的确定性键（无随机 nonce）。同
  ``(account_id, idempotency_key)`` 且规范化载荷一致 → 返回当初结果且不重复
  入账；摘要不同 → ``idempotency_conflict``。 ``content_sha256`` 不包含
  recorded_at / source / created_at 等登记性字段。
- **金额/股数**：整数分（cents）与非负整数股；未知成本/可卖量/取得日期保持
  ``NULL``，绝不用 0 冒充。实际买卖必须显式申报实际费用 fee_cents（缺失 →
  ``fee_required``）：本阶段没有费用模型，费用未提供时精确现金变动不可推导，
  因此只能拒绝（也不把估算冒充为实收券商费用）。
- **成本约定**：买入费用计入买入成本基础（总成本 = 既有成本 + 数量×价 + 费）。
  卖出按数量比例转出成本（``ROUND_HALF_UP``，只减不加，全卖清零）。既有成本
  未知时补买后**保持未知**并告警（``position_cost_mixed_unknown``）。
- **可卖量**：期初允许显式给出可卖数量/取得日期（可未知）。买入既有持仓的
  可卖量保持不变；新买入持仓 acquire_date 为买入日、可卖量保持未知（告警）。
  卖出校验：持有量不足 → ``position_insufficient``（拒绝）；已知可卖量不足 →
  ``sellable_insufficient``（拒绝）；可卖量未知 → 不伪造判定、放行并显式告警
  （锁定/可卖的用户申报为准，市场 T+1 规则在 S5 处理）。卖出锁定持仓产生
  ``position_locked_sale`` 告警（锁定是用户约束，不冒充市场限制）。
- **现金**：任何折叠步骤结果现金不可为负（``insufficient_cash``）。期初/
  入金/出金都不是投资收益；本模块不计算收益率。
- **公司行为**：S2 只支持已确认结果的健全记账（无任何自动生成）：显式
  ``share_delta``、``cash_delta_cents``，可选显式 ``new_total_cost_cents``。
  股数变化而未给新成本基础时总成本保持不变并告警。
- **领域隔离**：事件只允许挂 ``kind='actual'`` 账户（服务拒绝 + 数据库触发器
  ``account_event_requires_actual_account`` 双保险）。
- **原子提交**：预览只读；提交用 SAVEPOINT 包住"修订冲突检查 + 事件插入 +
  快照/持仓写入 + 修订推进"，任何失败整包回滚；调用方事务中则合并进外层事务，
  否则独立提交一次。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..data.digests import canonical_json_bytes, sha256_of_object
from ..data.errors import DataError
from ..data.timeutil import SH_TZ, UTC_TZ, parse_utc_iso, utc_now_iso

__all__ = [
    "ACCOUNT_LEDGER_VERSION",
    "SOURCE_MANUAL_ENTRY",
    "FLOW_EVENT_TYPES",
    "AccountEventRequest",
    "AccountState",
    "EventPreview",
    "EventPostResult",
    "create_account",
    "get_account",
    "preview_account_event",
    "post_account_event",
    "get_account_state",
    "list_account_events",
    "list_account_snapshots",
    "get_account_snapshot_state",
]

ACCOUNT_LEDGER_VERSION = "account-ledger-1.0"
SOURCE_MANUAL_ENTRY = "manual_entry"
_CORRECTION_TYPE = "correction"

FLOW_EVENT_TYPES = ("opening", "buy", "sell", "cash_in", "cash_out", "corporate_action")

_MAX_POSITIONS = 200
_MAX_EVENT_ROWS = 10_000


# ---------------------------------------------------------------- 输入规范化


def _int_field(
    payload: dict[str, Any],
    key: str,
    *,
    allow_none: bool = False,
    default: int | None = None,
    minimum: int | None = None,
) -> int | None:
    value = payload.get(key, default)
    if allow_none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataError(
            "ledger_field_not_integer",
            f"字段 {key} 必须是整数（不接受浮点/布尔/字符串）: {value!r}",
        )
    if minimum is not None and value < minimum:
        raise DataError(
            "ledger_field_out_of_range",
            f"字段 {key} 必须 >= {minimum}，当前 {value}",
        )
    return int(value)


def _optional_text(payload: dict[str, Any], key: str, *, max_length: int) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DataError("ledger_field_invalid", f"字段 {key} 必须是字符串或 null")
    text = value.strip()
    if len(text) > max_length:
        raise DataError("ledger_field_invalid", f"字段 {key} 长度超过上限 {max_length}")
    return text or None


_TS_CODE_PATTERN = re.compile(r"^[0-9]{6}\.(SZ|SH|BJ)$")


def _validate_ts_code(value: Any) -> str:
    """账本证券代码契约：6 位数字 + 交易所后缀（.SZ/.SH/.BJ）。

    字段格式校验在载荷规范化阶段完成，先于“事件必须出现在期初之后”等事件流
    顺序校验；这样非法代码始终得到稳定的 ``ts_code_invalid``，而不是被后续
    顺序错误掩盖。
    """
    if not isinstance(value, str) or not value.strip():
        raise DataError("ts_code_invalid", "证券代码必须是非空字符串")
    code = value.strip()
    if _TS_CODE_PATTERN.fullmatch(code) is None:
        raise DataError(
            "ts_code_invalid",
            f"证券代码必须是 6 位数字加交易所后缀 .SZ/.SH/.BJ: {value!r}",
        )
    return code


def _shanghai_today() -> str:
    return datetime.now(SH_TZ).strftime("%Y%m%d")


def _require_date_text(value: Any, *, code: str, field_name: str) -> str:
    try:
        return require_yyyymmdd_text(value, field=field_name)
    except ValueError as exc:
        raise DataError(code, f"{field_name} 不是合法日期: {value!r}") from exc


def require_yyyymmdd_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(field)
    from ..data.timeutil import parse_yyyymmdd

    date = parse_yyyymmdd(value, field=field)
    if date is None:
        raise ValueError(field)
    return date.strftime("%Y%m%d")


def _validate_business_date(value: Any) -> str:
    date_text = _require_date_text(value, code="business_date_invalid", field_name="business_date")
    today = _shanghai_today()
    if date_text > today:
        raise DataError(
            "business_date_in_future",
            f"事件生效日 {date_text} 晚于上海时区今天 {today}；不允许登记未来生效日",
        )
    return date_text


def _normalize_position(raw: Any, *, business_date: str, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DataError("opening_position_invalid", f"期初持仓 #{index} 必须是对象")
    allowed = (
        "ts_code", "shares", "total_cost_cents", "sellable_shares",
        "acquire_date", "locked", "price_status", "note",
    )
    unknown = set(raw) - set(allowed)
    if unknown:
        raise DataError("payload_unknown_field", f"期初持仓 #{index} 存在未知字段: {sorted(unknown)}")
    price_status = raw.get("price_status", "user_reported")
    if price_status != "user_reported":
        raise DataError(
            "payload_unknown_field",
            f"期初持仓 #{index} 的 price_status 只能是 user_reported（S2 不产生估算值）",
        )
    code = _validate_ts_code(raw.get("ts_code"))
    shares = _int_field(raw, "shares", allow_none=False)
    if shares < 1:
        raise DataError(
            "ledger_field_out_of_range",
            f"期初持仓 {code} 的 shares 必须 >= 1（空仓位不登记持仓行）",
        )
    total_cost = _int_field(raw, "total_cost_cents", allow_none=True, minimum=0)
    sellable = _int_field(raw, "sellable_shares", allow_none=True, minimum=0)
    if sellable is not None and sellable > shares:
        raise DataError(
            "position_sellable_exceeds_shares",
            f"期初持仓 {code} 的可卖数量 {sellable} 超过持有数量 {shares}",
        )
    acquire = _optional_text(raw, "acquire_date", max_length=8)
    if acquire is not None:
        acquire = _require_date_text(acquire, code="acquire_date_invalid", field_name="acquire_date")
        if acquire > business_date:
            raise DataError(
                "acquire_date_after_business_date",
                f"期初持仓 {code} 的取得日期 {acquire} 晚于期初日 {business_date}",
            )
    locked_raw = raw.get("locked", 0)
    if isinstance(locked_raw, bool):
        locked = 1 if locked_raw else 0
    elif isinstance(locked_raw, int) and not isinstance(locked_raw, bool) and locked_raw in (0, 1):
        locked = locked_raw
    else:
        raise DataError("ledger_field_invalid", "期初持仓 locked 只能是布尔或 0/1")
    return {
        "ts_code": code,
        "shares": shares,
        "total_cost_cents": total_cost,
        "sellable_shares": sellable,
        "acquire_date": acquire,
        "locked": locked,
        "price_status": "user_reported",
        "note": _optional_text(raw, "note", max_length=300),
    }


def _normalize_payload(event_type: str, raw: dict[str, Any], *, business_date: str) -> dict[str, Any]:
    """把调用方载荷规范化为确定性结构；规范化结果也用于内容摘要，可重复幂等。"""
    if not isinstance(raw, dict):
        raise DataError("payload_invalid", "事件载荷必须是对象")
    note = _optional_text(raw, "note", max_length=300)
    if event_type == "opening":
        allowed = {"cash_cents", "positions", "note"}
        unknown = set(raw) - allowed
        if unknown:
            raise DataError("payload_unknown_field", f"opening 载荷存在未知字段: {sorted(unknown)}")
        cash = _int_field(raw, "cash_cents", allow_none=False, minimum=0)
        positions_raw = raw.get("positions", [])
        if not isinstance(positions_raw, list):
            raise DataError("payload_invalid", "positions 必须是列表")
        if len(positions_raw) > _MAX_POSITIONS:
            raise DataError(
                "opening_positions_too_many",
                f"期初持仓最多 {_MAX_POSITIONS} 行，当前 {len(positions_raw)}",
            )
        positions = [
            _normalize_position(item, business_date=business_date, index=i)
            for i, item in enumerate(positions_raw)
        ]
        codes = [p["ts_code"] for p in positions]
        duplicates = sorted({c for c in codes if codes.count(c) > 1})
        if duplicates:
            raise DataError("opening_duplicate_position", f"期初持仓存在重复证券代码: {duplicates}")
        positions.sort(key=lambda p: p["ts_code"])
        return {"cash_cents": cash, "positions": positions, "note": note}
    if event_type in ("buy", "sell"):
        allowed = {"ts_code", "shares", "price_cents", "fee_cents", "note"}
        unknown = set(raw) - allowed
        if unknown:
            raise DataError("payload_unknown_field", f"{event_type} 载荷存在未知字段: {sorted(unknown)}")
        if "fee_cents" not in raw or raw.get("fee_cents") is None:
            raise DataError(
                "fee_required",
                f"{event_type} 载荷缺少显式 fee_cents；缺证据不得推导现金变动",
            )
        fee = _int_field(raw, "fee_cents", allow_none=False)
        if fee < 0:
            raise DataError("ledger_field_out_of_range", "实际费用不能为负")
        shares = _int_field(raw, "shares", allow_none=False)
        if shares < 1:
            raise DataError("ledger_field_out_of_range", "买卖数量必须 >= 1 股")
        price = _int_field(raw, "price_cents", allow_none=False)
        if price < 1:
            raise DataError("ledger_field_out_of_range", "成交价格必须为正（整数分/股）")
        return {
            "ts_code": _validate_ts_code(raw.get("ts_code")),
            "shares": shares,
            "price_cents": price,
            "fee_cents": fee,
            "note": note,
        }
    if event_type in ("cash_in", "cash_out"):
        allowed = {"amount_cents", "note"}
        unknown = set(raw) - allowed
        if unknown:
            raise DataError("payload_unknown_field", f"{event_type} 载荷存在未知字段: {sorted(unknown)}")
        return {
            "amount_cents": _int_field(raw, "amount_cents", allow_none=False, minimum=1),
            "note": note,
        }
    if event_type == "corporate_action":
        allowed = {"ts_code", "share_delta", "cash_delta_cents", "new_total_cost_cents", "note"}
        unknown = set(raw) - allowed
        if unknown:
            raise DataError("payload_unknown_field", f"corporate_action 载荷存在未知字段: {sorted(unknown)}")
        share_delta = _int_field(raw, "share_delta", allow_none=True, default=0)
        cash_delta = _int_field(raw, "cash_delta_cents", allow_none=True, default=0)
        new_cost = _int_field(raw, "new_total_cost_cents", allow_none=True, minimum=0)
        if share_delta == 0 and cash_delta == 0 and new_cost is None:
            raise DataError(
                "corporate_action_no_effect",
                "公司行为记录必须至少包含一项显式变动"
                "（share_delta / cash_delta_cents / new_total_cost_cents）",
            )
        return {
            "ts_code": _validate_ts_code(raw.get("ts_code")),
            "share_delta": share_delta,
            "cash_delta_cents": cash_delta,
            "new_total_cost_cents": new_cost,
            "note": note,
        }
    raise DataError("event_type_unknown", f"S2 不支持折叠事件类型 {event_type!r}")


def _canonical_payload_text(normalized: dict[str, Any]) -> str:
    return canonical_json_bytes(normalized).decode("utf-8")


def event_canonical_hash(
    *,
    account_id: str,
    event_type: str,
    business_date: str,
    idempotency_key: str,
    payload: dict[str, Any],
    supersedes_event_id: str | None,
    reason: str | None,
) -> str:
    """确定性内容摘要（不含 recorded_at/source/created_at 等登记性字段）。

    ``payload`` 必须是规范化后的载荷；同一幂等键且规范化载荷一致 → 摘要相等 →
    幂等重放，否则 ``idempotency_conflict``。
    """
    return sha256_of_object(
        {
            "ledger_version": ACCOUNT_LEDGER_VERSION,
            "account_id": account_id,
            "event_type": event_type,
            "business_date": business_date,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "supersedes_event_id": supersedes_event_id,
            "reason": reason,
        }
    )


def event_id_for(account_id: str, idempotency_key: str) -> str:
    """确定性 event_id：同 (账户, 幂等键) 永远得到同一 id。"""
    return "evt_" + sha256_of_object(["phase3-account-event", account_id, idempotency_key])[:20]


def snapshot_id_for(account_id: str, revision: int) -> str:
    """确定性 snapshot_id：同 (账户, 修订) 永远得到同一 id。"""
    return "as_" + sha256_of_object(["phase3-account-snapshot", account_id, str(revision)])[:20]


def _position_dict(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts_code": p["ts_code"],
        "shares": p["shares"],
        "total_cost_cents": p["total_cost_cents"],
        "sellable_shares": p["sellable_shares"],
        "acquire_date": p["acquire_date"],
        "locked": p["locked"],
        "price_status": p["price_status"],
        "note": p.get("note"),
    }


def _state_unknown_keys(positions: tuple[dict[str, Any], ...]) -> list[str]:
    keys: list[str] = []
    for p in positions:
        if p["total_cost_cents"] is None:
            keys.append("position_cost_unknown")
        if p["sellable_shares"] is None:
            keys.append("position_sellable_unknown")
        if p["acquire_date"] is None:
            keys.append("position_acquire_date_unknown")
    return keys


# ---------------------------------------------------------------- 事件请求


@dataclass(frozen=True)
class AccountEventRequest:
    """一个待预览/提交的实际账本事件（更正时 event_type='correction'）。"""

    account_id: str
    event_type: str
    business_date: str
    idempotency_key: str
    payload: dict[str, Any]
    supersedes_event_id: str | None = None
    reason: str | None = None
    source: str = SOURCE_MANUAL_ENTRY
    recorded_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, str) or not self.account_id.strip():
            raise DataError("ledger_field_invalid", "account_id 必须是非空字符串")
        known = FLOW_EVENT_TYPES + (_CORRECTION_TYPE,)
        if self.event_type not in known:
            raise DataError(
                "event_type_unknown",
                f"未知事件类型 {self.event_type!r}；支持: {'/'.join(known)}",
            )
        if not isinstance(self.idempotency_key, str) or not (1 <= len(self.idempotency_key.strip()) <= 120):
            raise DataError(
                "idempotency_key_invalid",
                "idempotency_key 必须是 1..120 字符的确定性键，绝不含随机数",
            )
        if self.supersedes_event_id is not None and self.event_type != _CORRECTION_TYPE:
            raise DataError(
                "non_correction_must_not_supersede",
                f"事件类型 {self.event_type} 不是更正事件，不能携带 supersedes_event_id",
            )
        if self.event_type == _CORRECTION_TYPE and self.supersedes_event_id is None:
            raise DataError(
                "correction_requires_supersedes",
                "更正事件必须显式指定 supersedes_event_id",
            )


@dataclass(frozen=True)
class AccountState:
    """一次折叠后的账户状态（快照投影）。金额整数分；股数非负整数。"""

    revision: int
    as_of: str | None
    cash_cents: int | None
    positions: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "as_of": self.as_of,
            "cash_cents": self.cash_cents,
            "positions": [_position_dict(p) for p in self.positions],
            "warnings": [dict(w) for w in self.warnings],
            "unknown_fields": self.unknown_field_codes(),
        }

    def unknown_field_codes(self) -> list[str]:
        """显式未知清单；未知字段保持 None，绝不用 0 冒充已知。"""
        codes: list[str] = []
        for p in self.positions:
            if p["total_cost_cents"] is None:
                codes.append("position_cost_unknown")
            if p["sellable_shares"] is None:
                codes.append("position_sellable_unknown")
            if p["acquire_date"] is None:
                codes.append("position_acquire_date_unknown")
        return codes


@dataclass(frozen=True)
class EventPreview:
    """只读预览结果：不写库。事件级校验失败 valid=False 并带稳定原因码。"""

    account_id: str
    event_type: str
    business_date: str
    valid: bool
    error_code: str | None
    error_detail: str | None
    replay: bool
    affected_revision: int | None
    before: AccountState | None
    after: AccountState | None
    unknown_fields: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "event_type": self.event_type,
            "business_date": self.business_date,
            "valid": self.valid,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "replay": self.replay,
            "affected_revision": self.affected_revision,
            "before": self.before.as_dict() if self.before is not None else None,
            "after": self.after.as_dict() if self.after is not None else None,
            "unknown_fields": list(self.unknown_fields),
            "ledger_version": ACCOUNT_LEDGER_VERSION,
        }


@dataclass(frozen=True)
class EventPostResult:
    """提交结果；``replayed=True`` 表示幂等重放（未重复入账）。"""

    event_id: str
    account_id: str
    event_type: str
    target_event_type: str
    business_date: str
    recorded_at: str
    source: str
    idempotency_key: str
    event_version: int
    supersedes_event_id: str | None
    snapshot_id: str | None
    revision: int
    cash_cents: int | None
    positions: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, str], ...]
    replayed: bool

    @property
    def unknown_fields(self) -> list[str]:
        return _state_unknown_keys(self.positions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "account_id": self.account_id,
            "event_type": self.event_type,
            "target_event_type": self.target_event_type,
            "business_date": self.business_date,
            "recorded_at": self.recorded_at,
            "source": self.source,
            "idempotency_key": self.idempotency_key,
            "event_version": self.event_version,
            "supersedes_event_id": self.supersedes_event_id,
            "snapshot_id": self.snapshot_id,
            "revision": self.revision,
            "cash_cents": self.cash_cents,
            "positions": [_position_dict(p) for p in self.positions],
            "warnings": [dict(w) for w in self.warnings],
            "unknown_fields": _state_unknown_keys(self.positions),
            "replayed": self.replayed,
            "ledger_version": ACCOUNT_LEDGER_VERSION,
        }


# ---------------------------------------------------------------- 读取工具


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        return {key: row[key] for key in row.keys()}
    except (AttributeError, TypeError):
        return dict(row)


def _load_account(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT account_id, kind, display_name, base_currency, account_revision,"
        " simulation_of_account_id, created_at, updated_at FROM account WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if row is None:
        raise DataError(
            "account_not_found",
            f"账户 {account_id} 不存在；事件只能挂在已创建的账户上",
        )
    account = _row_to_dict(row)
    if account["kind"] != "actual":
        raise DataError(
            "account_event_requires_actual_account",
            f"账户 {account_id} 是 {account['kind']} 账户；实际账本事件只允许挂在 kind='actual' 的账户上",
        )
    return account


_EVENT_ROW_COLUMNS = (
    "account_id, event_id, event_type, business_date, recorded_at, source, idempotency_key,"
    " event_version, supersedes_event_id, reason, payload, content_sha256"
)

_EVENT_ROWS_WITH_TEXT = (
    "event_id, event_type, business_date, recorded_at, source, idempotency_key,"
    " event_version, supersedes_event_id, reason, payload AS payload_text, content_sha256"
)


def _load_event_rows(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_EVENT_ROWS_WITH_TEXT} FROM account_event WHERE account_id=?",
        (account_id,),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        record = _row_to_dict(row)
        events.append(
            {
                "event_id": record["event_id"],
                "event_type": record["event_type"],
                "business_date": record["business_date"],
                "recorded_at": record["recorded_at"],
                "source": record["source"],
                "idempotency_key": record["idempotency_key"],
                "event_version": record["event_version"],
                "supersedes_event_id": record["supersedes_event_id"],
                "reason": record["reason"],
                "payload_text": record["payload_text"],
                "content_sha256": record["content_sha256"],
            }
        )
    return events


# ---------------------------------------------------------------- 有效事件流


def _resolve_chain_root_type(rows: list[dict[str, Any]], event_id: str) -> tuple[dict[str, Any], str]:
    """解析某事件所在的更正链根（行 + 链根事件语义类型）。"""
    by_id = {e["event_id"]: e for e in rows}
    target = by_id.get(event_id)
    if target is None:
        raise DataError(
            "correction_target_missing",
            f"事件 {event_id} 不存在于该账户",
        )
    guard = 0
    while target["supersedes_event_id"] is not None:
        guard += 1
        if guard > _MAX_EVENT_ROWS or target["supersedes_event_id"] not in by_id:
            raise DataError(
                "event_stream_ambiguous",
                f"事件 {event_id} 的更正链结构异常，无法解析链根",
            )
        target = by_id[str(target["supersedes_event_id"])]
    if target["event_type"] == _CORRECTION_TYPE:
        raise DataError(
            "event_stream_ambiguous",
            "更正链根不可能自己就是 correction 类型",
        )
    return target, target["event_type"]


def _effective_event_stream(rows: list[dict[str, Any]], *, account_id: str) -> list[dict[str, Any]]:
    """把事件行折叠为有效事件流；结构破坏/摘要不符一律显式拒绝。

    每条有效事件携带 event_id、root_event_id、target_event_type、business_date、
    recorded_at（链根值）、event_version（链尾值）、payload（存储形态解析后
    再按链根类型规范化，确保载荷净化）。
    """
    by_id = {e["event_id"]: e for e in rows}
    superseding: dict[str, dict[str, Any]] = {}
    for event in rows:
        target = event["supersedes_event_id"]
        if target is None:
            if event["event_type"] == _CORRECTION_TYPE:
                raise DataError(
                    "correction_requires_supersedes",
                    f"更正事件 {event['event_id']} 缺少 supersedes_event_id",
                )
            continue
        if event["event_type"] != _CORRECTION_TYPE:
            raise DataError(
                "non_correction_must_not_supersede",
                f"事件 {event['event_id']} 类型 {event['event_type']} 却携带 supersedes_event_id",
            )
        if target not in by_id:
            raise DataError(
                "correction_target_missing",
                f"更正事件 {event['event_id']} 的被更正事件 {target} 不存在",
            )
        if target == event["event_id"]:
            raise DataError("correction_target_self", "更正事件不能更正自身")
        if target in superseding:
            raise DataError(
                "superseded_event_ambiguous",
                f"被更正事件 {target} 已有更正 {superseding[target]['event_id']}"
                f"，又出现 {event['event_id']}；同一事件不允许被更正两次",
            )
        superseding[target] = event

    effective: list[dict[str, Any]] = []
    for root in [e for e in rows if e["supersedes_event_id"] is None]:
        chain = [root]
        tip = root
        while True:
            child = superseding.get(tip["event_id"])
            if child is None:
                break
            if child["business_date"] != tip["business_date"]:
                raise DataError(
                    "correction_business_date_mismatch",
                    f"更正 {child['event_id']} 生效日 {child['business_date']} 与被更正事件"
                    f" {tip['event_id']} 的 {tip['business_date']} 不一致；S2 更正不改生效日",
                )
            chain.append(child)
            tip = child
        try:
            stored_payload = json.loads(str(tip["payload_text"]))
        except ValueError as exc:
            raise DataError(
                "event_stream_invalid_payload",
                f"事件 {tip['event_id']} 存储载荷不是合法 JSON: {exc}",
            ) from exc
        try:
            payload = _normalize_payload(
                root["event_type"], stored_payload, business_date=root["business_date"]
            )
        except DataError as exc:
            raise DataError(
                "event_stream_invalid_payload",
                f"事件 {tip['event_id']} 载荷在链根类型 {root['event_type']} 下非法: {exc.detail}",
            ) from exc
        content_hash = event_canonical_hash(
            account_id=account_id,
            event_type=tip["event_type"],
            business_date=tip["business_date"],
            idempotency_key=str(tip["idempotency_key"]),
            payload=payload,
            supersedes_event_id=tip["supersedes_event_id"],
            reason=tip["reason"],
        )
        if content_hash != tip["content_sha256"]:
            raise DataError(
                "event_content_corrupt",
                f"事件 {tip['event_id']} 的内容摘要与存储载荷不一致；拒绝在折叠中采信",
            )
        effective.append(
            {
                "event_id": tip["event_id"],
                "root_event_id": root["event_id"],
                "target_event_type": root["event_type"],
                "business_date": root["business_date"],
                "recorded_at": root["recorded_at"],
                "event_version": tip["event_version"],
                "supersedes_event_id": tip["supersedes_event_id"],
                "payload": payload,
            }
        )
    effective.sort(key=lambda e: (e["business_date"], e["recorded_at"], e["root_event_id"]))
    for index, event in enumerate(effective):
        if event["target_event_type"] == "opening" and index != 0:
            raise DataError(
                "opening_already_exists",
                f"事件 {event['event_id']}: 期初事件必须是有效事件流的第一条",
            )
        if event["target_event_type"] != "opening" and index == 0:
            raise DataError(
                "event_before_opening",
                f"事件 {event['event_id']}: 非期初事件出现在期初事件之前；实际账本必须先有期初",
            )
    return effective


# ---------------------------------------------------------------- 状态折叠


def _fold_state(effective: list[dict[str, Any]], *, revision: int) -> AccountState:
    """把有效事件流折叠为账户状态；每一步都保证现金/股数/可卖量合法。"""
    warnings: list[dict[str, str]] = []
    cash: int | None = None
    positions: dict[str, dict[str, Any]] = {}
    as_of: str | None = None

    for event in effective:
        kind = event["target_event_type"]
        payload = event["payload"]
        label = f"事件 {event['event_id']} ({kind}@{event['business_date']})"
        as_of = event["business_date"]

        if kind == "opening":
            cash = int(payload["cash_cents"])
            positions = {p["ts_code"]: dict(p) for p in payload["positions"]}
            for pos in positions.values():
                if pos["total_cost_cents"] is None:
                    warnings.append({"code": "position_cost_unknown", "detail": pos["ts_code"]})
                if pos["sellable_shares"] is None:
                    warnings.append({"code": "position_sellable_unknown", "detail": pos["ts_code"]})
                if pos["acquire_date"] is None:
                    warnings.append(
                        {"code": "position_acquire_date_unknown", "detail": pos["ts_code"]}
                    )
            continue

        if kind == "buy":
            quantity = int(payload["shares"])
            cost_total = quantity * int(payload["price_cents"]) + int(payload["fee_cents"])
            available_cash = cash if cash is not None else 0
            if available_cash - cost_total < 0:
                raise DataError(
                    "insufficient_cash",
                    f"{label}: 现金 {available_cash} 分不足以完成需要 {cost_total} 分的买入"
                    "（数量×价+费）",
                )
            cash = available_cash - cost_total
            code = str(payload["ts_code"])
            pos = positions.get(code)
            if pos is None:
                positions[code] = {
                    "ts_code": code,
                    "shares": quantity,
                    "total_cost_cents": cost_total,
                    "sellable_shares": None,
                    "acquire_date": event["business_date"],
                    "locked": 0,
                    "price_status": "user_reported",
                    "note": payload.get("note"),
                }
                warnings.append(
                    {"code": "position_sellable_unknown_after_buy", "detail": f"{code} 新买入后的可卖量未知"}
                )
            else:
                pos["shares"] = int(pos["shares"]) + quantity
                if pos["total_cost_cents"] is None:
                    warnings.append(
                        {
                            "code": "position_cost_mixed_unknown",
                            "detail": f"{code} 既有成本未知，补买后保持未知，不伪造成本",
                        }
                    )
                else:
                    pos["total_cost_cents"] = int(pos["total_cost_cents"]) + cost_total
            continue

        if kind == "sell":
            quantity = int(payload["shares"])
            proceeds = quantity * int(payload["price_cents"]) - int(payload["fee_cents"])
            available_cash = cash if cash is not None else 0
            if available_cash + proceeds < 0:
                raise DataError(
                    "insufficient_cash",
                    f"{label}: 卖出净入账 {proceeds} 分使现金 {available_cash} 分变为负数",
                )
            code = str(payload["ts_code"])
            pos = positions.get(code)
            if pos is None:
                raise DataError(
                    "position_missing",
                    f"{label}: 持仓 {code} 不存在，不能卖出",
                )
            if int(pos["shares"]) < quantity:
                raise DataError(
                    "position_insufficient",
                    f"{label}: 持仓 {code} 只有 {pos['shares']} 股，不能卖出 {quantity} 股",
                )
            if pos["locked"]:
                warnings.append(
                    {"code": "position_locked_sale", "detail": f"{code} 处于用户锁定标记下，实际卖出记录如实保留"}
                )
            sellable = pos["sellable_shares"]
            if sellable is not None:
                if int(sellable) < quantity:
                    raise DataError(
                        "sellable_insufficient",
                        f"{label}: 持仓 {code} 已知可卖量 {sellable} 股少于申报卖出 {quantity} 股；"
                        "如账实不符请先对期初/持仓记录更正",
                    )
                pos["sellable_shares"] = int(sellable) - quantity
            else:
                warnings.append(
                    {"code": "position_sellable_unknown", "detail": f"{code} 可卖量未知，不伪造判定"}
                )
            total_before = int(pos["shares"])
            remaining = total_before - quantity
            cost = pos["total_cost_cents"]
            if remaining == 0:
                pos["shares"] = 0
                pos["total_cost_cents"] = 0
                pos["sellable_shares"] = 0
            else:
                pos["shares"] = remaining
                if cost is not None:
                    sold_cost = int(
                        (
                            Decimal(int(cost)) * Decimal(quantity) / Decimal(total_before)
                        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                    )
                    if sold_cost > int(cost):
                        sold_cost = int(cost)
                    pos["total_cost_cents"] = int(cost) - sold_cost
            cash = available_cash + proceeds
            continue

        if kind == "cash_in":
            cash = (cash if cash is not None else 0) + int(payload["amount_cents"])
            continue

        if kind == "cash_out":
            available_cash = cash if cash is not None else 0
            amount = int(payload["amount_cents"])
            if available_cash - amount < 0:
                raise DataError(
                    "insufficient_cash",
                    f"{label}: 现金 {available_cash} 分不足以完成 {amount} 分的出金",
                )
            cash = available_cash - amount
            continue

        if kind == "corporate_action":
            code = str(payload["ts_code"])
            pos = positions.get(code)
            if pos is None:
                raise DataError(
                    "corporate_action_position_missing",
                    f"{label}: 持仓 {code} 不存在；公司行为只登记已持有证券上确认的结果",
                )
            share_delta = int(payload["share_delta"])
            cash_delta = int(payload["cash_delta_cents"])
            available_cash = cash if cash is not None else 0
            if available_cash + cash_delta < 0:
                raise DataError(
                    "insufficient_cash",
                    f"{label}: 现金变动 {cash_delta} 分使现金 {available_cash} 分变为负数",
                )
            cash = available_cash + cash_delta
            shares_after = int(pos["shares"]) + share_delta
            if shares_after < 0:
                raise DataError(
                    "corporate_action_invalid_shares",
                    f"{label}: 持仓 {code} 股数 {pos['shares']} 加变动 {share_delta} 为负",
                )
            new_cost = payload["new_total_cost_cents"]
            if new_cost is not None and shares_after == 0 and int(new_cost) != 0:
                raise DataError(
                    "corporate_action_cost_for_empty_position",
                    f"{label}: {code} 股数清零后不能声明非零总成本 {new_cost}",
                )
            pos["shares"] = shares_after
            if shares_after == 0:
                pos["total_cost_cents"] = 0
                pos["sellable_shares"] = 0
            else:
                if new_cost is not None:
                    pos["total_cost_cents"] = int(new_cost)
                elif share_delta != 0:
                    warnings.append(
                        {
                            "code": "corporate_action_cost_basis_unchanged",
                            "detail": f"{code} 股数变动但未提供新总成本；总成本保持不变，"
                            "每股成本按剩余总成本除以新股数派生",
                        }
                    )
                sellable = pos["sellable_shares"]
                if sellable is not None:
                    pos["sellable_shares"] = min(int(sellable), shares_after)
            continue

        raise DataError(
            "event_stream_invalid_payload",
            f"{label}: 未知有效事件类型 {kind!r}；账本约定不猜测语义",
        )

    ordered_positions = tuple(positions[code] for code in sorted(positions))
    return AccountState(
        revision=revision,
        as_of=as_of,
        cash_cents=cash,
        positions=ordered_positions,
        warnings=tuple(warnings),
    )


def _fold_current_state(
    conn: sqlite3.Connection,
    account: dict[str, Any],
) -> AccountState:
    """读取事件行并折叠当前状态；校验事件行数与账户修订一致。"""
    rows = _load_event_rows(conn, account["account_id"])
    if len(rows) != int(account["account_revision"]):
        raise DataError(
            "event_stream_revision_mismatch",
            f"账户 {account['account_id']} 修订 {account['account_revision']} 与事件行数 {len(rows)} 不一致；"
            "账本可能被绕过服务直接改写，拒绝折叠",
        )
    effective = _effective_event_stream(rows, account_id=account["account_id"])
    return _fold_state(effective, revision=int(account["account_revision"]))


# ---------------------------------------------------------------- 服务 API


def create_account(
    conn: sqlite3.Connection,
    account_id: str,
    display_name: str,
    *,
    base_currency: str = "CNY",
) -> dict[str, Any]:
    """创建个人实际账户（kind='actual'），修订从 0 起，等待期初事件。"""
    if not isinstance(account_id, str) or not (1 <= len(account_id.strip()) <= 64):
        raise DataError("ledger_field_invalid", "account_id 必须是 1..64 字符")
    if not isinstance(display_name, str) or not (1 <= len(display_name.strip()) <= 200):
        raise DataError("ledger_field_invalid", "display_name 必须是 1..200 字符")
    if base_currency != "CNY":
        raise DataError(
            "base_currency_unsupported",
            f"阶段三首版只支持人民币（CNY）；收到 {base_currency!r}，不接受静默扩围",
        )
    if conn.execute("SELECT 1 FROM account WHERE account_id=?", (account_id,)).fetchone():
        raise DataError("account_already_exists", f"账户 {account_id} 已存在")
    now = utc_now_iso()
    cursor = conn.execute(
        "INSERT INTO account(account_id, kind, display_name, base_currency, account_revision,"
        " simulation_of_account_id, created_at, updated_at) VALUES (?, 'actual', ?, ?, 0, NULL, ?, ?)",
        (account_id, display_name.strip(), base_currency, now, now),
    )
    if getattr(cursor, "rowcount", 0) != 1:
        raise DataError("account_create_failed", f"账户 {account_id} 创建失败")
    return {
        "account_id": account_id,
        "kind": "actual",
        "display_name": display_name.strip(),
        "base_currency": base_currency,
        "account_revision": 0,
        "created_at": now,
        "updated_at": now,
    }


def get_account(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    """读取账户元信息（只读）。"""
    account = _load_account(conn, account_id)
    account.pop("simulation_of_account_id", None)
    return account


def get_account_state(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    """当前状态 = 依据全部有效事件重新折叠（与最新快照一致，旧快照保留）。"""
    account = _load_account(conn, account_id)
    return _fold_current_state(conn, account).as_dict()


def list_account_events(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """只读事件清单（含更正链信息与全部原始行），按 (生效日, recorded_at) 稳定排序。"""
    _load_account(conn, account_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
        limit = 200
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        offset = 0
    rows = conn.execute(
        f"SELECT {_EVENT_ROW_COLUMNS} FROM account_event WHERE account_id=?"
        " ORDER BY business_date, recorded_at, event_id LIMIT ? OFFSET ?",
        (account_id, int(limit), int(offset)),
    ).fetchall()
    events = [_row_to_dict(row) for row in rows]
    # 更正关系必须覆盖整个账户，不能只由当前页推导；否则更正落在第 2 页时，
    # 第 1 页的原事件会被错误标成 effective。这里只读 event_id 关系，不载入载荷。
    relation_rows = conn.execute(
        "SELECT event_id, supersedes_event_id FROM account_event"
        " WHERE account_id=? AND supersedes_event_id IS NOT NULL",
        (account_id,),
    ).fetchall()
    superseded_by: dict[str, str] = {}
    for relation in relation_rows:
        target = str(relation[1])
        if target in superseded_by:
            raise DataError(
                "superseded_event_ambiguous",
                f"被更正事件 {target} 存在多次更正，事件流存在歧义",
            )
        superseded_by[target] = str(relation[0])
    listed: list[dict[str, Any]] = []
    for event in events:
        try:
            payload = json.loads(str(event["payload"]))
        except ValueError:
            payload = None
        listed.append(
            {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "business_date": event["business_date"],
                "recorded_at": event["recorded_at"],
                "source": event["source"],
                "idempotency_key": event["idempotency_key"],
                "event_version": event["event_version"],
                "supersedes_event_id": event["supersedes_event_id"],
                "superseded_by": superseded_by.get(str(event["event_id"])),
                "effective": str(event["event_id"]) not in superseded_by,
                "reason": event["reason"],
                "payload": payload if isinstance(payload, dict) else None,
                "content_sha256": event["content_sha256"],
            }
        )
    return listed


def list_account_snapshots(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """只读快照清单（新修订在前）；旧快照永不被改写。"""
    _load_account(conn, account_id)
    if limit < 1 or limit > 1000:
        limit = 50
    rows = conn.execute(
        "SELECT snapshot_id, account_revision, as_of, cash_cents, data_version_id,"
        " source_code, created_at FROM account_snapshot WHERE account_id=?"
        " ORDER BY account_revision DESC LIMIT ?",
        (account_id, int(limit)),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_account_snapshot_state(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    revision: int,
) -> dict[str, Any] | None:
    """读取某一修订当时已知的快照状态（历史审计；不重新折叠）。"""
    account = _load_account(conn, account_id)
    snap = conn.execute(
        "SELECT snapshot_id, account_revision, as_of, cash_cents, data_version_id,"
        " source_code, created_at FROM account_snapshot WHERE snapshot_id=?",
        (snapshot_id_for(account_id, int(revision)),),
    ).fetchone()
    if snap is None:
        return None
    snapshot = _row_to_dict(snap)
    rows = conn.execute(
        "SELECT ts_code, shares, total_cost_cents, sellable_shares, acquire_date, locked,"
        " price_status FROM account_position WHERE snapshot_id=? ORDER BY ts_code",
        (snapshot["snapshot_id"],),
    ).fetchall()
    positions = [_row_to_dict(row) for row in rows]
    unknown: list[str] = []
    for p in positions:
        if p["total_cost_cents"] is None:
            unknown.append("position_cost_unknown")
        if p["sellable_shares"] is None:
            unknown.append("position_sellable_unknown")
        if p["acquire_date"] is None:
            unknown.append("position_acquire_date_unknown")
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "account_id": account["account_id"],
        "revision": int(snapshot["account_revision"]),
        "as_of": snapshot["as_of"],
        "cash_cents": int(snapshot["cash_cents"]),
        "positions": positions,
        "unknown_fields": unknown,
        "created_at": snapshot["created_at"],
    }


# ---------------------------------------------------------------- 预览与提交


def _prepare_context(
    conn: sqlite3.Connection,
    request: AccountEventRequest,
) -> dict[str, Any]:
    """公共入口校验：账户/生效日/修订一致性、载荷规范化与更正目标检查。"""
    business_date = _validate_business_date(request.business_date)
    account = _load_account(conn, request.account_id)
    rows = _load_event_rows(conn, request.account_id)
    if len(rows) >= _MAX_EVENT_ROWS:
        raise DataError(
            "event_limit_exceeded",
            f"账户 {request.account_id} 事件数已达 {_MAX_EVENT_ROWS} 上限",
        )
    if len(rows) != int(account["account_revision"]):
        raise DataError(
            "event_stream_revision_mismatch",
            f"账户 {request.account_id} 修订 {account['account_revision']} 与事件行数 {len(rows)} 不一致；"
            "账本可能被绕过服务直接改写",
        )
    by_id = {row["event_id"]: row for row in rows}
    if request.event_type == _CORRECTION_TYPE:
        target_id = str(request.supersedes_event_id)
        target = by_id.get(target_id)
        if target is None:
            raise DataError(
                "correction_target_missing",
                f"supersedes_event_id={target_id} 不存在于该账户",
            )
        already = [row for row in rows if row["supersedes_event_id"] == target_id]
        if already:
            raise DataError(
                "correction_target_already_superseded",
                f"被更正事件 {target_id} 已被 {already[0]['event_id']} 更正；"
                "如需继续更正请对最新更正事件发起更正",
            )
        if target["business_date"] != business_date:
            raise DataError(
                "correction_business_date_mismatch",
                f"更正 {business_date} 与被更正事件 {target['business_date']} 生效日不一致；"
                "S2 更正不改生效日",
            )
        _root, root_type = _resolve_chain_root_type(rows, target_id)
        target_event_type = root_type
    else:
        target_event_type = request.event_type
    payload = _normalize_payload(target_event_type, dict(request.payload), business_date=business_date)
    return {
        "account": account,
        "rows": rows,
        "business_date": business_date,
        "payload": payload,
        "target_type": target_event_type,
    }


def _recorded_at_text(value: str | None) -> str:
    if value is None:
        return utc_now_iso()
    try:
        parsed = parse_utc_iso(value, field="recorded_at")
    except ValueError as exc:
        raise DataError("recorded_at_invalid", f"recorded_at 不是合法的带时区时间: {value!r}") from exc
    assert parsed is not None
    return parsed.astimezone(UTC_TZ).isoformat(timespec="seconds")


def _content_hash_for(ctx: dict[str, Any], request: AccountEventRequest) -> str:
    return event_canonical_hash(
        account_id=request.account_id,
        event_type=request.event_type,
        business_date=ctx["business_date"],
        idempotency_key=request.idempotency_key,
        payload=ctx["payload"],
        supersedes_event_id=request.supersedes_event_id,
        reason=request.reason,
    )


def _replay_row(
    conn: sqlite3.Connection,
    ctx: dict[str, Any],
    request: AccountEventRequest,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_EVENT_ROW_COLUMNS} FROM account_event WHERE account_id=? AND idempotency_key=?",
        (request.account_id, request.idempotency_key),
    ).fetchone()
    if row is None:
        return None
    existing = _row_to_dict(row)
    if existing["content_sha256"] != _content_hash_for(ctx, request):
        raise DataError(
            "idempotency_conflict",
            f"幂等键 {request.idempotency_key!r} 已绑定另一载荷（事件 {existing['event_id']}）；"
            "同一键不得携带不同内容",
        )
    return existing


def _replay_result(conn: sqlite3.Connection, existing: dict[str, Any]) -> EventPostResult:
    """从既有行/快照重建当初结果；不重复入账、不改任何数据。"""
    account_id = str(existing.get("account_id"))
    rank = conn.execute(
        "SELECT COUNT(*) FROM account_event WHERE account_id=? AND rowid <"
        " (SELECT rowid FROM account_event WHERE event_id=?)",
        (account_id, existing["event_id"]),
    ).fetchone()
    revision = int(rank[0]) + 1 if rank is not None else 1
    snapshot_id = snapshot_id_for(account_id, revision)
    snap = conn.execute(
        "SELECT snapshot_id, cash_cents, as_of FROM account_snapshot WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if snap is None:
        raise DataError(
            "replay_snapshot_missing",
            f"重放参考快照 {snapshot_id} 缺失；快照可能被绕过服务直接删除，拒绝重放",
        )
    position_rows = conn.execute(
        "SELECT ts_code, shares, total_cost_cents, sellable_shares, acquire_date, locked,"
        " price_status FROM account_position WHERE snapshot_id=? ORDER BY ts_code",
        (snapshot_id,),
    ).fetchall()
    positions = [_row_to_dict(row) for row in position_rows]
    return EventPostResult(
        event_id=str(existing["event_id"]),
        account_id=account_id,
        event_type=str(existing["event_type"]),
        target_event_type=(
            str(existing["event_type"]) if existing["event_type"] != _CORRECTION_TYPE else _CORRECTION_TYPE
        ),
        business_date=str(existing["business_date"]),
        recorded_at=str(existing["recorded_at"]),
        source=str(existing["source"]),
        idempotency_key=str(existing["idempotency_key"]),
        event_version=int(existing["event_version"]),
        supersedes_event_id=existing["supersedes_event_id"],
        snapshot_id=str(snap["snapshot_id"]),
        revision=revision,
        cash_cents=int(snap["cash_cents"]),
        positions=tuple(positions),
        warnings=(),
        replayed=True,
    )


def _validated_source_reason(request: AccountEventRequest) -> tuple[str, str | None]:
    source = request.source or SOURCE_MANUAL_ENTRY
    if not isinstance(source, str) or not (1 <= len(source.strip()) <= 80):
        raise DataError("ledger_field_invalid", "source 必须是 1..80 字符")
    reason = request.reason
    if reason is not None:
        if not isinstance(reason, str):
            raise DataError("ledger_field_invalid", "reason 必须是字符串或 null")
        if len(reason.strip()) > 1000:
            raise DataError("ledger_field_invalid", "reason 长度超过上限 1000")
        reason = reason.strip() or None
    return source, reason


def _hypothetical_row(
    ctx: dict[str, Any],
    request: AccountEventRequest,
    *,
    recorded_at: str,
    content_hash: str,
    event_version: int,
) -> dict[str, Any]:
    """构造与提交时同构的事件行（仅用于纯计算折叠，不写库）。"""
    return {
        "event_id": event_id_for(request.account_id, request.idempotency_key),
        "event_type": request.event_type,
        "business_date": ctx["business_date"],
        "recorded_at": recorded_at,
        "source": request.source or SOURCE_MANUAL_ENTRY,
        "idempotency_key": request.idempotency_key,
        "event_version": event_version,
        "supersedes_event_id": request.supersedes_event_id,
        "reason": request.reason,
        "payload_text": _canonical_payload_text(ctx["payload"]),
        "content_sha256": content_hash,
    }


def _next_event_version(rows: list[dict[str, Any]], request: AccountEventRequest) -> int:
    """新事件的 event_version：非更正恒为 1；更正 = 被更正行版本 + 1。"""
    if request.event_type != _CORRECTION_TYPE:
        return 1
    target = next(
        (row for row in rows if row["event_id"] == request.supersedes_event_id), None
    )
    if target is None:
        raise DataError(
            "correction_target_missing",
            f"supersedes_event_id={request.supersedes_event_id} 不存在",
        )
    return int(target["event_version"]) + 1


def preview_account_event(
    conn: sqlite3.Connection,
    request: AccountEventRequest,
) -> EventPreview:
    """只读预览：计算 before/after、受影响修订与显式未知清单；不写任何库。

    账户级别错误（不存在/非实际账户/被绕过服务的改写）直接抛 ``DataError``；
    事件级校验失败通过 ``valid=False`` 与稳定原因码返回，供页面直接展示。
    """
    ctx = _prepare_context(conn, request)
    account = ctx["account"]
    current = _fold_current_state(conn, account)
    replay = _replay_row(conn, ctx, request)

    def _rejected(code: str, detail: str) -> EventPreview:
        return EventPreview(
            account_id=request.account_id,
            event_type=request.event_type,
            business_date=ctx["business_date"],
            valid=False,
            error_code=code,
            error_detail=detail,
            replay=False,
            affected_revision=None,
            before=current,
            after=None,
            unknown_fields=current.unknown_field_codes(),
        )

    if replay is not None:
        return EventPreview(
            account_id=request.account_id,
            event_type=request.event_type,
            business_date=ctx["business_date"],
            valid=True,
            error_code=None,
            error_detail=None,
            replay=True,
            affected_revision=None,
            before=current,
            after=current,
            unknown_fields=current.unknown_field_codes(),
        )

    recorded_at = _recorded_at_text(request.recorded_at)
    content_hash = _content_hash_for(ctx, request)
    event_version = _next_event_version(ctx["rows"], request)
    hypo = _hypothetical_row(
        ctx,
        request,
        recorded_at=recorded_at,
        content_hash=content_hash,
        event_version=event_version,
    )
    try:
        effective = _effective_event_stream(ctx["rows"] + [hypo], account_id=request.account_id)
        after = _fold_state(effective, revision=int(account["account_revision"]) + 1)
    except DataError as exc:
        return _rejected(exc.reason, str(exc))
    return EventPreview(
        account_id=request.account_id,
        event_type=request.event_type,
        business_date=ctx["business_date"],
        valid=True,
        error_code=None,
        error_detail=None,
        replay=False,
        affected_revision=int(account["account_revision"]) + 1,
        before=current,
        after=after,
        unknown_fields=after.unknown_field_codes(),
    )


def post_account_event(
    conn: sqlite3.Connection,
    request: AccountEventRequest,
    *,
    expected_account_revision: int,
) -> EventPostResult:
    """幂等提交一个事件：校验 + 折叠验证 + 单次原子写入。

    - 幂等重放优先：同键同载荷直接返回当初结果（不重复入账、不校验修订预期）；
    - 键冲突（同键异载荷）/修订预期陈旧在写入前显式失败；
    - SAVEPOINT 内完成"事件 + 快照 + 持仓 + 修订推进"，失败整包回滚。
    """
    ctx = _prepare_context(conn, request)
    account = ctx["account"]
    current_revision = int(account["account_revision"])
    replay = _replay_row(conn, ctx, request)
    if replay is not None:
        return _replay_result(conn, replay)

    if expected_account_revision != current_revision:
        raise DataError(
            "account_revision_conflict",
            f"账户 {request.account_id} 当前修订 {current_revision} 与预期 {expected_account_revision} 不一致；"
            "请刷新状态后重试（乐观并发保护）",
        )

    source, reason = _validated_source_reason(request)
    recorded_at = _recorded_at_text(request.recorded_at)
    payload_text = _canonical_payload_text(ctx["payload"])
    content_hash = _content_hash_for(ctx, request)
    event_id = event_id_for(request.account_id, request.idempotency_key)
    event_version = _next_event_version(ctx["rows"], request)
    new_revision = current_revision + 1
    snapshot_id = snapshot_id_for(request.account_id, new_revision)

    row_shape = _hypothetical_row(
        ctx,
        request,
        recorded_at=recorded_at,
        content_hash=content_hash,
        event_version=event_version,
    )

    conn.execute("SAVEPOINT phase3_ledger_post")
    try:
        effective = _effective_event_stream(ctx["rows"] + [row_shape], account_id=request.account_id)
        state = _fold_state(effective, revision=new_revision)
        conn.execute(
            "INSERT INTO account_event(event_id, account_id, event_type, business_date,"
            " recorded_at, source, idempotency_key, event_version, supersedes_event_id,"
            " reason, payload, content_sha256, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                request.account_id,
                request.event_type,
                ctx["business_date"],
                recorded_at,
                source,
                request.idempotency_key,
                event_version,
                request.supersedes_event_id,
                reason,
                payload_text,
                content_hash,
                utc_now_iso(),
            ),
        )
        as_of = state.as_of or ctx["business_date"]
        conn.execute(
            "INSERT INTO account_snapshot(snapshot_id, account_id, as_of, account_revision,"
            " cash_cents, data_version_id, source_code, created_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (snapshot_id, request.account_id, as_of, new_revision, state.cash_cents, source, utc_now_iso()),
        )
        for pos in state.positions:
            conn.execute(
                "INSERT INTO account_position(snapshot_id, ts_code, shares, total_cost_cents,"
                " sellable_shares, acquire_date, locked, price_status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    pos["ts_code"],
                    int(pos["shares"]),
                    int(pos["total_cost_cents"]) if pos["total_cost_cents"] is not None else None,
                    int(pos["sellable_shares"]) if pos["sellable_shares"] is not None else None,
                    pos["acquire_date"],
                    int(pos["locked"]),
                    pos["price_status"],
                ),
            )
        conn.execute(
            "UPDATE account SET account_revision=?, updated_at=? WHERE account_id=?",
            (new_revision, utc_now_iso(), request.account_id),
        )
    except BaseException as exc:
        conn.execute("ROLLBACK TO phase3_ledger_post")
        conn.execute("RELEASE phase3_ledger_post")
        if isinstance(exc, sqlite3.IntegrityError):
            raise DataError(
                "ledger_write_rejected_by_database",
                f"数据库层拒绝账本写入（约束/触发器/外键），整包已回滚: {exc}",
            ) from exc
        raise
    conn.execute("RELEASE phase3_ledger_post")

    return EventPostResult(
        event_id=event_id,
        account_id=request.account_id,
        event_type=request.event_type,
        target_event_type=ctx["target_type"],
        business_date=ctx["business_date"],
        recorded_at=recorded_at,
        source=source,
        idempotency_key=request.idempotency_key,
        event_version=event_version,
        supersedes_event_id=request.supersedes_event_id,
        snapshot_id=snapshot_id,
        revision=new_revision,
        cash_cents=state.cash_cents,
        positions=state.positions,
        warnings=state.warnings,
        replayed=False,
    )
