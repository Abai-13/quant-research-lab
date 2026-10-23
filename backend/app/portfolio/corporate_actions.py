"""阶段三 S5b：受支持的公司行为（现金分红、送转股）与应收登记。

冻结口径（阶段三 §7.4）：

- 只处理已显式确认的现金分红与送/转股；配股、合并、退市等 ``other`` 一律
  标记 ``blocked_insufficient_evidence``，保留持仓与未决状态，不猜测处置。
- 现金分红必须提供税后净额且 ``tax_status='confirmed'`` 才能结算；estimated/
  unknown 保持 pending，不能把税前或估计现金冒充已确认收益。
- 权益登记日确定 entitlement；除权日之后、派息日之前作为应收项目（只影响
  NAV，不提前记为现金）；派息日/上市日才写入可用现金/可卖股数。
- 实际账户通过 S2 手工确认 ``corporate_action`` 事件结算；模拟段通过模拟域
  事件结算。两者都使用确定性幂等键，绝不重复入账，且模拟不写实际账本。
- 本模块不接真实公司行为数据源，也不自动推断比例/税率。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from ..data.digests import sha256_of_object
from ..data.errors import DataError
from ..data.timeutil import parse_yyyymmdd, utc_now_iso
from .ledger import (
    AccountEventRequest,
    get_account_state,
    post_account_event,
)
from .simulation import _atomic_write, apply_simulation_corporate_action

__all__ = [
    "CORPORATE_ACTION_KINDS",
    "record_corporate_action",
    "settle_corporate_action",
    "receivable_summary",
    "list_corporate_actions",
]

CORPORATE_ACTION_KINDS = ("cash_dividend", "stock_dividend", "other")
_TAX_STATUSES = ("unknown", "estimated", "confirmed")


def _date_text(value: Any, reason: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DataError(reason, f"日期必须是 YYYYMMDD 文本或空: {value!r}")
    try:
        parsed = parse_yyyymmdd(value, field=reason)
    except ValueError as exc:
        raise DataError(reason, f"日期不是真实日历日: {value!r}") from exc
    return parsed.strftime("%Y%m%d") if parsed is not None else None


def _owner_simulation_segment(conn, account_id: str) -> str | None:
    row = conn.execute(
        "SELECT segment_id, status FROM simulation_segment WHERE simulation_account_id=?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    if str(row[1]) != "active":
        raise DataError("s5_segment_closed", f"模拟段 {row[0]} 已关闭，不能登记公司行为")
    return str(row[0])


def _resolve_owner(conn, owner_type: str, owner_id: str) -> tuple[str, str | None]:
    if owner_type == "account":
        row = conn.execute("SELECT kind FROM account WHERE account_id=?", (owner_id,)).fetchone()
        if row is None:
            raise DataError("ca_account_not_found", f"账户 {owner_id} 不存在")
        if str(row[0]) != "actual":
            raise DataError(
                "ca_account_not_actual",
                "owner_type=account 只接受实际账户；模拟段请使用 simulation_segment",
            )
        return owner_id, None
    if owner_type == "simulation_segment":
        row = conn.execute(
            "SELECT simulation_account_id FROM simulation_segment WHERE segment_id=?",
            (owner_id,),
        ).fetchone()
        if row is None:
            raise DataError("ca_segment_not_found", f"模拟段 {owner_id} 不存在")
        return str(row[0]), owner_id
    raise DataError("ca_owner_type_invalid", f"未知 owner_type: {owner_type!r}")


def record_corporate_action(
    conn,
    *,
    owner_type: str,
    owner_id: str,
    ts_code: str,
    action_kind: str,
    record_date: str,
    entitlement_shares: int,
    idempotency_key: str,
    ex_date: str | None = None,
    pay_date: str | None = None,
    list_date: str | None = None,
    cash_receivable_cents: int | None = None,
    tax_status: str = "unknown",
    data_version_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """登记一条公司行为（幂等）；不提前记现金/股数。"""
    account_id, _segment_id = _resolve_owner(conn, owner_type, owner_id)
    if action_kind not in CORPORATE_ACTION_KINDS:
        raise DataError("ca_kind_invalid", f"未知公司行为类型: {action_kind!r}")
    if tax_status not in _TAX_STATUSES:
        raise DataError("ca_tax_status_invalid", f"未知税务状态: {tax_status!r}")
    record = _date_text(record_date, "ca_record_date_invalid")
    ex = _date_text(ex_date, "ca_ex_date_invalid")
    pay = _date_text(pay_date, "ca_pay_date_invalid")
    listed = _date_text(list_date, "ca_list_date_invalid")
    if isinstance(entitlement_shares, bool) or not isinstance(entitlement_shares, int) or entitlement_shares < 0:
        raise DataError("ca_entitlement_invalid", "entitlement_shares 必须是非负整数")
    if (
        cash_receivable_cents is not None
        and (
            isinstance(cash_receivable_cents, bool)
            or not isinstance(cash_receivable_cents, int)
            or cash_receivable_cents < 0
        )
    ):
        raise DataError("ca_cash_invalid", "cash_receivable_cents 必须是非负整数或空")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise DataError("ca_idempotency_required", "公司行为必须显式提供 idempotency_key")
    if data_version_id is not None:
        version_row = conn.execute(
            "SELECT 1 FROM data_version WHERE version_id=?", (data_version_id,)
        ).fetchone()
        if version_row is None:
            raise DataError("ca_data_version_not_found", f"数据版本 {data_version_id} 不存在")

    status = "pending"
    restriction = None
    if action_kind == "other":
        status = "blocked_insufficient_evidence"
        restriction = "unsupported_corporate_action"
    elif action_kind == "cash_dividend":
        if cash_receivable_cents is None or pay is None:
            status = "blocked_insufficient_evidence"
            restriction = "cash_amount_or_pay_date_missing"
        elif tax_status != "confirmed":
            restriction = "tax_status_not_confirmed"
    elif action_kind == "stock_dividend":
        if listed is None or entitlement_shares < 1:
            status = "blocked_insufficient_evidence"
            restriction = "list_date_or_shares_missing"

    existing = conn.execute(
        "SELECT application_id, action_kind, ts_code, record_date, entitlement_shares,"
        " cash_receivable_cents, pay_date, list_date, tax_status, status, data_version_id"
        " FROM corporate_action_application WHERE account_id=? AND idempotency_key=?",
        (account_id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if (
            str(existing[1]) == action_kind
            and str(existing[2]) == ts_code
            and str(existing[3]) == record
            and int(existing[4]) == entitlement_shares
            and existing[5] == cash_receivable_cents
            and existing[6] == pay
            and existing[7] == listed
            and str(existing[8]) == tax_status
            and str(existing[9]) == status
        ):
            return {
                "application_id": str(existing[0]),
                "status": str(existing[9]),
                "restriction_code": restriction,
                "reused": True,
            }
        raise DataError(
            "ca_idempotency_conflict",
            f"公司行为键 {idempotency_key} 已被不同内容占用",
        )

    application_id = "ca_" + sha256_of_object(
        [account_id, idempotency_key, action_kind, ts_code, record, entitlement_shares]
    )[:20]
    conn.execute(
        "INSERT INTO corporate_action_application(application_id, account_id, ts_code,"
        " action_kind, record_date, ex_date, pay_date, list_date, entitlement_shares,"
        " cash_receivable_cents, status, tax_status, data_version_id, idempotency_key,"
        " created_at, settled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            application_id,
            account_id,
            ts_code,
            action_kind,
            record,
            ex,
            pay,
            listed,
            entitlement_shares,
            cash_receivable_cents,
            status,
            tax_status,
            data_version_id,
            idempotency_key,
            utc_now_iso(),
        ),
    )
    return {
        "application_id": application_id,
        "status": status,
        "restriction_code": restriction,
        "reused": False,
    }


def settle_corporate_action(conn, application_id: str, *, as_of: str) -> dict[str, Any]:
    """在派息日/上市日把已确认公司行为写入账本；幂等且原子。"""
    as_of = _date_text(as_of, "ca_as_of_invalid")
    with _atomic_write(conn):
        row = conn.execute(
            "SELECT application_id, account_id, ts_code, action_kind, record_date,"
            " ex_date, pay_date, list_date, entitlement_shares, cash_receivable_cents,"
            " status, tax_status FROM corporate_action_application WHERE application_id=?",
            (application_id,),
        ).fetchone()
        if row is None:
            raise DataError("ca_application_not_found", f"公司行为 {application_id} 不存在")
        if str(row[10]) == "settled":
            return {"application_id": application_id, "status": "settled", "reused": True}
        if str(row[10]) == "blocked_insufficient_evidence":
            raise DataError(
                "ca_blocked_insufficient_evidence",
                "公司行为证据不足（不支持类型/缺派息日或上市日），拒绝结算",
            )
        action_kind = str(row[3])
        account_id = str(row[1])
        ts_code = str(row[2])
        segment_id = _owner_simulation_segment(conn, account_id)
        share_delta = 0
        cash_delta = 0
        business_date: str
        if action_kind == "cash_dividend":
            if str(row[11]) != "confirmed" or row[9] is None:
                raise DataError(
                    "ca_tax_status_not_confirmed",
                    "现金分红税后净额未确认；不得把估计/税前现金入账",
                )
            pay_date = row[6]
            if pay_date is None or as_of < str(pay_date):
                raise DataError("ca_not_payable_yet", "尚未到派息日，不能把应收转为现金")
            cash_delta = int(row[9])
            business_date = str(pay_date)
        elif action_kind == "stock_dividend":
            list_date = row[7]
            if list_date is None or as_of < str(list_date):
                raise DataError("ca_not_listed_yet", "送转股尚未到上市日")
            share_delta = int(row[8])
            business_date = str(list_date)
        else:
            raise DataError("ca_unsupported_action", f"不支持结算公司行为类型 {action_kind}")

        if segment_id is None:
            state = get_account_state(conn, account_id)
            post_account_event(
                conn,
                AccountEventRequest(
                    account_id=account_id,
                    event_type="corporate_action",
                    business_date=business_date,
                    idempotency_key=f"ca-settle:{application_id}",
                    payload={
                        "ts_code": ts_code,
                        "share_delta": share_delta,
                        "cash_delta_cents": cash_delta,
                        "note": f"公司行为结算 {application_id}",
                    },
                    recorded_at=utc_now_iso(),
                    reason="S5b 已确认公司行为（实际账本）",
                ),
                expected_account_revision=int(state["revision"]),
            )
        else:
            apply_simulation_corporate_action(
                conn,
                segment_id,
                ts_code=ts_code,
                share_delta=share_delta,
                cash_delta_cents=cash_delta,
                business_date=business_date,
                idempotency_key=f"ca-settle:{application_id}",
                note=f"公司行为结算 {application_id}",
            )
        conn.execute(
            "UPDATE corporate_action_application SET status='settled', settled_at=?"
            " WHERE application_id=?",
            (utc_now_iso(), application_id),
        )
    return {
        "application_id": application_id,
        "status": "settled",
        "cash_delta_cents": cash_delta,
        "share_delta": share_delta,
        "reused": False,
    }


def receivable_summary(conn, account_id: str, as_of: str) -> dict[str, Any]:
    """除权后、派息前的已确认现金应收；估计/未知税后净额不冒充 NAV。"""
    as_of = _date_text(as_of, "ca_as_of_invalid")
    rows = conn.execute(
        "SELECT cash_receivable_cents, tax_status, ex_date, pay_date, action_kind, status"
        " FROM corporate_action_application WHERE account_id=?"
        " AND action_kind='cash_dividend'",
        (account_id,),
    ).fetchall()
    confirmed = 0
    unconfirmed = 0
    for cash, tax, ex, pay, _kind, status in rows:
        if status in ("settled", "blocked_insufficient_evidence"):
            continue
        if ex is None or pay is None:
            continue
        if str(ex) <= as_of < str(pay):
            if str(tax) == "confirmed" and cash is not None:
                confirmed += int(cash)
            else:
                unconfirmed += 1
    return {
        "confirmed_cents": confirmed,
        "unconfirmed_count": unconfirmed,
        "basis": "ex_date_to_pay_date_confirmed_net_cash",
    }


def list_corporate_actions(
    conn, account_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = 50
    limit = min(limit, 200)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        offset = 0
    rows = conn.execute(
        "SELECT application_id, account_id, ts_code, action_kind, record_date, ex_date,"
        " pay_date, list_date, entitlement_shares, cash_receivable_cents, status,"
        " tax_status, data_version_id, idempotency_key, created_at, settled_at"
        " FROM corporate_action_application WHERE account_id=?"
        " ORDER BY record_date DESC, application_id DESC LIMIT ? OFFSET ?",
        (account_id, limit, offset),
    ).fetchall()
    names = (
        "application_id",
        "account_id",
        "ts_code",
        "action_kind",
        "record_date",
        "ex_date",
        "pay_date",
        "list_date",
        "entitlement_shares",
        "cash_receivable_cents",
        "status",
        "tax_status",
        "data_version_id",
        "idempotency_key",
        "created_at",
        "settled_at",
    )
    return [dict(zip(names, row)) for row in rows]
