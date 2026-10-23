"""数据可用资格接口。

``check_eligibility(scope, as_of, data_version, purpose)`` 返回是否允许、稳定原因码、
受影响范围、最近数据日、质量清单与来源限制。用途至少区分展示、信号输入、成交参考、
估值参考与严格历史重放。

契约要点：

- 质量不满足或关键状态未知时，受影响的新计划输入被拒绝；
- 陈旧估值可以带标签展示，但不能变成新交易的合格价格；
- 没有行情不是 0 行情，资料不足不是可交易；
- 缺少历史发布时间证据时，严格历史重放资格为受限或不满足；
- 所有历史长度资格都按请求的 ``as_of``（含边界日）截断，未来行数不能补足历史。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from .chunks import VersionView, version_scope_codes
from .prices import PRICE_PURPOSES, price_for_purpose
from .quality import QUALITY_RULE_VERSION, MIN_HISTORY_TRADE_DAYS
from .timeutil import utc_now_iso

__all__ = ["EligibilityResult", "check_eligibility"]

# 稳定原因码
REASON_ELIGIBLE = "eligible"
REASON_UNKNOWN_PURPOSE = "unknown_purpose"
REASON_VERSION_NOT_FOUND = "version_not_found"
REASON_SOURCE_VERSION_MISMATCH = "source_version_mismatch"
REASON_NOT_CONFIGURED = "not_configured"
REASON_NO_DATA_IN_SCOPE = "no_data_in_scope"
REASON_MISSING_PRICE = "missing_price"
REASON_STALE_DATA = "stale_data"
REASON_QUALITY_BLOCKED = "quality_issue_high_severity"
REASON_NOT_A_TRADING_DAY = "not_a_trading_day"
REASON_SUSPENDED = "suspended"
REASON_INSUFFICIENT_HISTORY = "insufficient_history"
REASON_HISTORICAL_AVAILABILITY_UNPROVEN = "historical_availability_unproven"
REASON_CALENDAR_UNKNOWN = "calendar_unknown"
REASON_LIMITED = "limited"


@dataclass
class EligibilityResult:
    """资格判断结果。"""

    purpose: str
    as_of: str
    eligible: bool
    reason_code: str
    data_version_id: str | None = None
    affected_scope: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    latest_data_date: str | None = None
    quality: dict[str, Any] = field(default_factory=dict)
    source_limits: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)
    checked_at: str = field(default_factory=utc_now_iso)
    rule_version: str = QUALITY_RULE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "as_of": self.as_of,
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "data_version_id": self.data_version_id,
            "affected_scope": self.affected_scope,
            "reasons": self.reasons,
            "latest_data_date": self.latest_data_date,
            "quality": self.quality,
            "source_limits": self.source_limits,
            "details": self.details,
            "checked_at": self.checked_at,
            "rule_version": self.rule_version,
        }


def _source_scope_codes(conn: sqlite3.Connection, source_code: str) -> list[str]:
    """没有指定数据版本时，回退到该来源已入库的全部证券。"""
    rows = conn.execute(
        "SELECT ts_code FROM security_info WHERE source_code=?"
        " AND id IN (SELECT MAX(id) FROM security_info WHERE source_code=? GROUP BY ts_code)"
        " ORDER BY ts_code",
        (source_code, source_code),
    ).fetchall()
    return [row[0] for row in rows]


def _latest_data_date(conn: sqlite3.Connection, source_code: str, codes: Sequence[str]) -> str | None:
    if not codes:
        return None
    placeholders = ", ".join("?" for _ in codes)
    row = conn.execute(
        f"SELECT MAX(trade_date) FROM daily_price WHERE source_code=? AND ts_code IN ({placeholders})",
        [source_code, *codes],
    ).fetchone()
    return row[0] if row and row[0] else None


def _history_length(
    conn: sqlite3.Connection, source_code: str, ts_code: str, *, as_of: str
) -> int:
    """无版本读取路径的历史长度，按请求的 ``as_of``（含边界日）截断。

    不截断会让 ``as_of`` 之后的行情补足历史长度，造成时点泄漏。
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM daily_price WHERE source_code=? AND ts_code=? AND trade_date<=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM daily_price d2"
        "                    WHERE d2.source_code=daily_price.source_code"
        "                      AND d2.ts_code=daily_price.ts_code"
        "                      AND d2.trade_date=daily_price.trade_date)",
        (source_code, ts_code, as_of),
    ).fetchone()
    return int(row[0]) if row else 0


def _date_blocking_issues(
    conn: sqlite3.Connection,
    *,
    data_version_id: str | None,
    ts_code: str,
    trade_date: str,
) -> list[str]:
    """按证券+日期精确匹配已记录的高严重度阻断问题。

    只接受同时命中同一证券与同一业务日期的证据：scope 里的
    ``trade_date``（单条记录问题）或 ``trade_dates``（记录级汇总问题）。
    没有日期证据的问题不参与匹配，避免无关日期的异常误伤本次判断；
    与 ``_quality_summary`` 保持一致的版本治理口径。
    """
    query = "SELECT rule_code, scope FROM quality_issue WHERE severity='high'"
    params: list[Any] = []
    if data_version_id:
        query += " AND data_version_id=?"
        params.append(data_version_id)
    codes: set[str] = set()
    for rule_code, scope_text in conn.execute(query, params).fetchall():
        try:
            scope = json.loads(scope_text) if scope_text else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(scope, dict):
            continue
        if str(scope.get("ts_code") or "") != ts_code:
            continue
        if str(scope.get("trade_date") or "") == trade_date:
            codes.add(str(rule_code))
            continue
        dates = scope.get("trade_dates")
        if isinstance(dates, list) and str(trade_date) in {str(value) for value in dates}:
            codes.add(str(rule_code))
    return sorted(codes)


def _quality_summary(
    conn: sqlite3.Connection, data_version_id: str | None
) -> tuple[dict[str, Any], list[str]]:
    if data_version_id:
        rows = conn.execute(
            "SELECT rule_code, severity, COUNT(*) FROM quality_issue WHERE data_version_id=?"
            " GROUP BY rule_code, severity",
            (data_version_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT rule_code, severity, COUNT(*) FROM quality_issue GROUP BY rule_code, severity"
        ).fetchall()
    by_rule: dict[str, int] = {}
    by_severity = {"info": 0, "warn": 0, "high": 0}
    codes: list[str] = []
    for rule_code, severity, count in rows:
        by_rule[rule_code] = by_rule.get(rule_code, 0) + int(count)
        by_severity[str(severity)] = by_severity.get(str(severity), 0) + int(count)
        if str(severity) == "high":
            codes.append(str(rule_code))
    return {"by_rule": by_rule, "by_severity": by_severity, "blocking_codes": codes}, codes


def _source_limits(conn: sqlite3.Connection, source_code: str) -> list[str]:
    limits: list[str] = []
    rows = conn.execute(
        "SELECT interface, permission_status, unknown_fields, coverage_notes FROM source_registry"
        " WHERE source_code=? ORDER BY interface",
        (source_code,),
    ).fetchall()
    for interface, permission_status, unknown_fields, coverage_notes in rows:
        if permission_status == "documented":
            limits.append(f"{interface}: 文档宣称支持，账号实际访问未核验")
        if unknown_fields:
            limits.append(f"{interface}: 未知项 {unknown_fields}")
        if coverage_notes:
            limits.append(f"{interface}: {coverage_notes}")
    limits.append("供应商不提供逐条历史发布时刻，严格历史重放资格受限")
    limits.append("当前在市名单缺少退市证券与完整历史停牌状态，幸存者偏差未被消除")
    return limits


def check_eligibility(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    as_of: str,
    source_code: str,
    data_version_id: str | None = None,
    ts_codes: Sequence[str] | None = None,
    business_date: str | None = None,
) -> EligibilityResult:
    """判断某范围在某截止时点下是否满足指定用途。"""
    if purpose not in PRICE_PURPOSES:
        return EligibilityResult(
            purpose=purpose,
            as_of=as_of,
            eligible=False,
            reason_code=REASON_UNKNOWN_PURPOSE,
            data_version_id=data_version_id,
            reasons=[f"未知用途: {purpose}"],
        )

    view = None
    if data_version_id:
        row = conn.execute(
            "SELECT status, source_code FROM data_version WHERE version_id=?",
            (data_version_id,),
        ).fetchone()
        if row is None:
            return EligibilityResult(
                purpose=purpose,
                as_of=as_of,
                eligible=False,
                reason_code=REASON_VERSION_NOT_FOUND,
                data_version_id=data_version_id,
                reasons=[f"数据版本 {data_version_id} 不存在"],
            )
        version_source = str(row[1])
        if version_source != source_code:
            # 版本来源与请求来源必须一致；不读取任何行情，避免把另一来源的价格
            # 冒充为本次请求来源的数据。
            return EligibilityResult(
                purpose=purpose,
                as_of=as_of,
                eligible=False,
                reason_code=REASON_SOURCE_VERSION_MISMATCH,
                data_version_id=data_version_id,
                affected_scope={
                    "source_code": source_code,
                    "data_version_source_code": version_source,
                    "ts_codes": [],
                    "count": 0,
                    "business_date": business_date,
                },
                reasons=[
                    f"数据版本 {data_version_id} 属于来源 {version_source}，"
                    f"与请求来源 {source_code} 不一致"
                ],
            )
        view = VersionView(conn, data_version_id, expected_source_code=source_code)

    if ts_codes:
        codes = list(ts_codes)
    elif view is not None:
        codes = version_scope_codes(conn, data_version_id)
    else:
        codes = []
    if not codes and not ts_codes:
        codes = _source_scope_codes(conn, source_code)
    codes = sorted({code for code in codes if code})
    quality, blocking_codes = _quality_summary(conn, data_version_id)
    limits = _source_limits(conn, source_code)
    latest_data_date = (
        view.latest_data_date(codes) if view is not None else _latest_data_date(conn, source_code, codes)
    )

    result = EligibilityResult(
        purpose=purpose,
        as_of=as_of,
        eligible=False,
        reason_code=REASON_NO_DATA_IN_SCOPE,
        data_version_id=data_version_id,
        affected_scope={
            "source_code": source_code,
            "ts_codes": codes,
            "count": len(codes),
            "business_date": business_date,
        },
        quality=quality,
        source_limits=limits,
    )

    if not codes:
        result.reasons.append("范围内没有证券记录")
        return result

    details: list[dict[str, Any]] = []
    missing_price: list[str] = []
    stale: list[str] = []
    suspended: list[str] = []
    not_trading_day: list[str] = []
    insufficient_history: list[str] = []
    calendar_unknown: list[str] = []

    for code in codes:
        price = price_for_purpose(
            conn,
            purpose=purpose,
            source_code=source_code,
            ts_code=code,
            as_of=as_of,
            business_date=business_date,
            view=view,
        )
        history = (
            view.history_length(code, on_or_before=as_of)
            if view is not None
            else _history_length(conn, source_code, code, as_of=as_of)
        )
        details.append(
            {
                "ts_code": code,
                "price_date": price.price_date,
                "price": price.price,
                "currency": price.currency,
                "stale": price.stale,
                "stale_reason": price.stale_reason,
                "reason_code": price.reason_code,
                "history_trade_days": history,
                "notes": price.notes,
            }
        )
        if price.price is None:
            missing_price.append(code)
        if price.stale:
            stale.append(code)
        if price.reason_code == "suspended":
            suspended.append(code)
        if price.reason_code == "not_a_trading_day":
            not_trading_day.append(code)
        if price.reason_code == "calendar_unknown":
            calendar_unknown.append(code)
        if history < MIN_HISTORY_TRADE_DAYS:
            insufficient_history.append(code)

    result.details = details
    result.latest_data_date = latest_data_date

    if purpose == "display":
        result.eligible = True
        result.reason_code = REASON_ELIGIBLE if not missing_price else REASON_LIMITED
        result.reasons.append("展示用途允许带陈旧或缺失标签呈现")
        if missing_price:
            result.reasons.append(f"{len(missing_price)} 只证券没有可用价格，仅显示缺失状态")
        if stale:
            result.reasons.append(f"{len(stale)} 只证券的价格日期早于业务日期")
        return result

    if purpose == "valuation_reference":
        result.eligible = True
        result.reason_code = REASON_ELIGIBLE if not missing_price else REASON_LIMITED
        result.reasons.append("估值用途允许使用陈旧价格，但必须带陈旧标签")
        if missing_price:
            result.reasons.append(f"{len(missing_price)} 只证券没有可用估值价格")
        if stale:
            result.reasons.append(f"{len(stale)} 只证券的估值价格已陈旧")
        return result

    if purpose == "execution_reference":
        if suspended:
            result.reason_code = REASON_SUSPENDED
            result.reasons.append(f"{len(suspended)} 只证券在指定交易日全日停牌")
            return result
        if not_trading_day:
            result.reason_code = REASON_NOT_A_TRADING_DAY
            result.reasons.append(f"{len(not_trading_day)} 只证券的指定日期不是交易日")
            return result
        if calendar_unknown:
            result.reason_code = REASON_CALENDAR_UNKNOWN
            result.reasons.append(f"{len(calendar_unknown)} 只证券缺少交易日历证据")
            return result
        date_key = business_date or as_of
        for code in codes:
            blocking_rules = _date_blocking_issues(
                conn,
                data_version_id=data_version_id,
                ts_code=code,
                trade_date=date_key,
            )
            if blocking_rules:
                result.reason_code = REASON_QUALITY_BLOCKED
                result.affected_scope["blocking_scope"] = {
                    "ts_code": code,
                    "trade_date": date_key,
                }
                result.reasons.append(
                    f"证券 {code} 在 {date_key} 已记录高严重度质量问题，"
                    f"拒绝用于成交参考：" + "、".join(blocking_rules)
                )
                return result
        if missing_price:
            result.reason_code = REASON_MISSING_PRICE
            result.reasons.append(f"{len(missing_price)} 只证券在指定交易日缺少成交参考价")
            return result
        result.eligible = True
        result.reason_code = REASON_ELIGIBLE
        result.reasons.append("成交参考价可用（成交规则由阶段 3 处理）")
        return result

    # signal_input 与 strict_historical_replay
    if missing_price:
        result.reason_code = REASON_MISSING_PRICE
        result.reasons.append(f"{len(missing_price)} 只证券在截止时点缺少行情")
        return result
    if stale:
        result.reason_code = REASON_STALE_DATA
        result.reasons.append(f"{len(stale)} 只证券在截止时点没有当日行情")
        return result
    if insufficient_history:
        result.reason_code = REASON_INSUFFICIENT_HISTORY
        result.reasons.append(
            f"{len(insufficient_history)} 只证券的历史长度不足 {MIN_HISTORY_TRADE_DAYS} 个交易日"
        )
        return result
    if blocking_codes:
        result.reason_code = REASON_QUALITY_BLOCKED
        result.reasons.append("存在高严重度质量问题：" + "、".join(sorted(set(blocking_codes))))
        return result

    if purpose == "strict_historical_replay":
        result.reason_code = REASON_HISTORICAL_AVAILABILITY_UNPROVEN
        result.reasons.append("缺少历史发布时刻证据，严格历史重放资格不满足")
        return result

    result.eligible = True
    result.reason_code = REASON_ELIGIBLE
    result.reasons.append("信号输入可用（历史可用时刻证据仍受限，见来源限制）")
    return result


__all__ += [
    "REASON_CALENDAR_UNKNOWN",
    "REASON_ELIGIBLE",
    "REASON_HISTORICAL_AVAILABILITY_UNPROVEN",
    "REASON_INSUFFICIENT_HISTORY",
    "REASON_LIMITED",
    "REASON_MISSING_PRICE",
    "REASON_NOT_A_TRADING_DAY",
    "REASON_NOT_CONFIGURED",
    "REASON_NO_DATA_IN_SCOPE",
    "REASON_QUALITY_BLOCKED",
    "REASON_STALE_DATA",
    "REASON_SOURCE_VERSION_MISMATCH",
    "REASON_SUSPENDED",
    "REASON_UNKNOWN_PURPOSE",
    "REASON_VERSION_NOT_FOUND",
]
