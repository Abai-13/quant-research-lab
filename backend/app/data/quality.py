"""数据质量规则。

规则集与阈值都有版本号（``QUALITY_RULE_VERSION``），阈值不按收益事后调优；
“大幅波动”“异常值”一律只标记，不自动改写成正常值。

严重度：

- ``high``：阻断受影响的新计划输入；
- ``warn``：可用但必须带提示；
- ``info``：限制说明（例如供应商不提供历史发布时刻）。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .timeutil import utc_now_iso, yyyymmdd

QUALITY_RULE_VERSION = "qr-2026-10-22.1"

# 版本化阈值
UNIT_RATIO_MIN = 0.05
UNIT_RATIO_MAX = 20.0
STALE_TRADING_DAYS = 5
MIN_HISTORY_TRADE_DAYS = 120

RULE_CODES: dict[str, str] = {
    "Q001_trading_day_gap": "交易日缺口：证券在应有行情的交易日缺失记录",
    "Q002_page_coverage_gap": "分页覆盖缺口：取数页面失败或缺页",
    "Q003_duplicate_key_conflict": "同一逻辑键在同一批次内出现互相冲突的内容",
    "Q004_non_finite_price": "价格或数量不是有限数值",
    "Q005_invalid_ohlc_relation": "OHLC 关系不合法（最高价低于最低价或开盘收盘越界）",
    "Q006_negative_volume_or_amount": "成交量或成交额为负",
    "Q007_unit_inconsistency": "单位换算后与价格规模明显不符，可能混入其他来源口径",
    "Q008_date_out_of_order": "来源返回的业务日期不是升序",
    "Q009_unknown_security": "行情对应的证券不在基础资料中",
    "Q010_before_listing_date": "行情日期早于上市日期",
    "Q011_after_delisting_date": "行情日期晚于退市日期",
    "Q012_suspension_conflict": "全日停牌期间仍存在行情记录",
    "Q013_corporate_action_gap": "公司行为或复权因子覆盖存在缺口",
    "Q014_stale_data": "数据相对截止时点陈旧",
    "Q015_future_dated_record": "记录的业务日期或观测时刻落在未来",
    "Q016_historical_availability_unproven": "来源不提供历史发布时刻，严格历史重放资格受限",
    "Q017_non_trading_day_quote": "非交易日存在行情记录",
    "Q018_invalid_price_format": "价格不是有效有限正数（布尔值、无法解析的类型或非正数值）",
}


@dataclass
class QualityIssue:
    rule_code: str
    severity: str
    scope: dict[str, Any] = field(default_factory=dict)
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "severity": self.severity,
            "scope": self.scope,
            "evidence": self.evidence,
        }


@dataclass
class QualityContext:
    """跨记录检查所需的上下文。"""

    trading_days: set[str] = field(default_factory=set)
    all_calendar_dates: set[str] = field(default_factory=set)
    securities: dict[str, dict[str, Any]] = field(default_factory=dict)
    suspensions: dict[tuple[str, str], str] = field(default_factory=dict)
    adj_factor_dates: dict[str, set[str]] = field(default_factory=dict)
    as_of: str | None = None
    now: date | None = None


def _is_finite(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return not (math.isnan(float(value)) or math.isinf(float(value)))
    return True


def check_daily_records(
    records: Sequence[Mapping[str, Any]],
    context: QualityContext,
) -> list[QualityIssue]:
    """检查日线记录本身与已知上下文的一致性。

    同一条规则对同一证券的多条记录只汇总为一条问题，避免逐行刷屏；
    证据里保留受影响条数与示例。
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    def _add(
        rule_code: str,
        severity: str,
        ts_code: str,
        detail: str,
        scope_extra: Mapping[str, Any] | None = None,
        trade_date: str | None = None,
    ) -> None:
        key = (rule_code, ts_code)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "rule_code": rule_code,
                "severity": severity,
                "ts_code": ts_code,
                "count": 0,
                "samples": [],
                "trade_dates": [],
                "scope_extra": dict(scope_extra or {}),
            }
            buckets[key] = bucket
        bucket["count"] += 1
        if trade_date and trade_date not in bucket["trade_dates"]:
            bucket["trade_dates"].append(trade_date)
        if len(bucket["samples"]) < 5:
            bucket["samples"].append(detail)

    seen: dict[tuple[str, str], str] = {}
    for record in records:
        code = str(record.get("ts_code") or "")
        trade_date = str(record.get("trade_date") or "")
        key = (code, trade_date)

        digest = repr(
            (
                record.get("open"),
                record.get("high"),
                record.get("low"),
                record.get("close"),
                record.get("volume"),
                record.get("amount"),
            )
        )
        if key in seen and seen[key] != digest:
            _add(
                "Q003_duplicate_key_conflict",
                "high",
                code,
                f"{trade_date} 同一逻辑键出现两套不同行情",
                trade_date=trade_date,
            )
        seen[key] = digest

        for field_name in ("open", "high", "low", "close", "pre_close", "volume", "amount"):
            value = record.get(field_name)
            if value is None:
                continue
            if not _is_finite(value):
                _add(
                    "Q004_non_finite_price",
                    "high",
                    code,
                    f"{trade_date} 的 {field_name} 不是有限数值",
                    {"field": field_name},
                    trade_date,
                )

        for field_name in ("open", "high", "low", "close", "pre_close"):
            value = record.get(field_name)
            if isinstance(value, bool) or (isinstance(value, (int, float)) and float(value) <= 0):
                _add(
                    "Q018_invalid_price_format",
                    "high",
                    code,
                    f"{trade_date} 的 {field_name}={value!r} 不是有效正数价格",
                    {"field": field_name},
                    trade_date,
                )

        volume = record.get("volume")
        amount = record.get("amount")
        if isinstance(volume, (int, float)) and volume < 0:
            _add(
                "Q006_negative_volume_or_amount",
                "high",
                code,
                f"{trade_date} 成交量为负: {volume}",
                {"field": "volume"},
                trade_date,
            )
        if isinstance(amount, (int, float)) and amount < 0:
            _add(
                "Q006_negative_volume_or_amount",
                "high",
                code,
                f"{trade_date} 成交额为负: {amount}",
                {"field": "amount"},
                trade_date,
            )

        high = record.get("high")
        low = record.get("low")
        open_ = record.get("open")
        close = record.get("close")
        if all(isinstance(value, (int, float)) for value in (high, low)):
            if float(high) < float(low):
                _add(
                    "Q005_invalid_ohlc_relation",
                    "high",
                    code,
                    f"{trade_date} high={high} < low={low}",
                    trade_date=trade_date,
                )
        if all(isinstance(value, (int, float)) for value in (high, low, open_)):
            if not (float(low) <= float(open_) <= float(high)):
                _add(
                    "Q005_invalid_ohlc_relation",
                    "high",
                    code,
                    f"{trade_date} open={open_} 不在 [{low}, {high}] 区间",
                    {"field": "open"},
                    trade_date,
                )
        if all(isinstance(value, (int, float)) for value in (high, low, close)):
            if not (float(low) <= float(close) <= float(high)):
                _add(
                    "Q005_invalid_ohlc_relation",
                    "high",
                    code,
                    f"{trade_date} close={close} 不在 [{low}, {high}] 区间",
                    {"field": "close"},
                    trade_date,
                )

        if (
            isinstance(volume, (int, float))
            and isinstance(amount, (int, float))
            and isinstance(close, (int, float))
            and float(close) > 0
            and float(volume) > 0
        ):
            expected = float(volume) * float(close)
            if expected > 0:
                ratio = float(amount) / expected
                if ratio < UNIT_RATIO_MIN or ratio > UNIT_RATIO_MAX:
                    _add(
                        "Q007_unit_inconsistency",
                        "high",
                        code,
                        f"{trade_date} 成交额/（成交量×收盘价）= {ratio:.4f}，"
                        f"超出 [{UNIT_RATIO_MIN}, {UNIT_RATIO_MAX}]",
                        trade_date=trade_date,
                    )

        security = context.securities.get(code)
        if security is None:
            _add(
                "Q009_unknown_security",
                "high",
                code,
                f"{trade_date} 的行情缺少对应证券的基础资料",
                trade_date=trade_date,
            )
        else:
            list_date = security.get("list_date")
            delist_date = security.get("delist_date")
            if list_date and trade_date and trade_date < list_date:
                _add(
                    "Q010_before_listing_date",
                    "high",
                    code,
                    f"{trade_date} 早于上市日期 {list_date}",
                    trade_date=trade_date,
                )
            if delist_date and trade_date and trade_date > delist_date:
                _add(
                    "Q011_after_delisting_date",
                    "high",
                    code,
                    f"{trade_date} 晚于退市日期 {delist_date}",
                    trade_date=trade_date,
                )

        suspension_type = context.suspensions.get((code, trade_date))
        if suspension_type == "S":
            _add(
                "Q012_suspension_conflict",
                "warn",
                code,
                f"{trade_date} 同时存在停牌事件与行情记录",
                trade_date=trade_date,
            )

        if context.trading_days and trade_date and trade_date not in context.trading_days:
            _add(
                "Q017_non_trading_day_quote",
                "warn",
                code,
                f"{trade_date} 不在已核验的交易日内",
                trade_date=trade_date,
            )

    issues: list[QualityIssue] = []
    for bucket in buckets.values():
        scope = {"ts_code": bucket["ts_code"], "affected_count": bucket["count"]}
        if bucket["trade_dates"]:
            # 受影响日期是范围证据的一部分；资格判断按证券+日期精确匹配。
            # 必须保留完整日期集合：截断会遗漏较早的问题日期，
            # 使 execution_reference 对受影响日期错误放行。
            scope["trade_dates"] = sorted(bucket["trade_dates"])
        scope.update(bucket["scope_extra"])
        issues.append(
            QualityIssue(
                rule_code=bucket["rule_code"],
                severity=bucket["severity"],
                scope=scope,
                evidence="；".join(bucket["samples"]) + f"（共 {bucket['count']} 条）",
            )
        )
    return issues


def check_trading_day_gaps(
    records: Sequence[Mapping[str, Any]],
    context: QualityContext,
) -> list[QualityIssue]:
    """检查证券在应有行情的交易日是否缺失（需要已核验日历）。"""
    issues: list[QualityIssue] = []
    if not context.trading_days:
        return issues
    by_code: dict[str, set[str]] = {}
    for record in records:
        code = str(record.get("ts_code") or "")
        trade_date = str(record.get("trade_date") or "")
        if code and trade_date:
            by_code.setdefault(code, set()).add(trade_date)
    for code, dates in by_code.items():
        security = context.securities.get(code) or {}
        list_date = security.get("list_date")
        delist_date = security.get("delist_date")
        present = {value for value in dates}
        expected = sorted(
            day
            for day in context.trading_days
            if (not list_date or day >= list_date) and (not delist_date or day <= delist_date)
        )
        missing = [day for day in expected if day not in present]
        if missing:
            issues.append(
                QualityIssue(
                    "Q001_trading_day_gap",
                    "warn",
                    {"ts_code": code, "missing_count": len(missing)},
                    f"缺失 {len(missing)} 个交易日，例如 {missing[:5]}",
                )
            )
    return issues


def check_source_publication_availability(
    *, source_code: str, categories: Sequence[str]
) -> list[QualityIssue]:
    """来源不提供历史发布时刻时，记录一条限制说明。

    当前已核验的来源（Tushare 及其离线夹具）都不给出逐条历史发布时刻，
    因此严格历史重放资格一律受限，而不是“无问题”。
    """
    return [
        QualityIssue(
            "Q016_historical_availability_unproven",
            "info",
            {"source_code": source_code, "categories": sorted(categories)},
            "来源接口不提供逐条历史发布时刻，严格历史重放资格受限",
        )
    ]


def check_staleness(
    *,
    latest_trade_date: str | None,
    trading_days: Sequence[str],
    as_of: str,
    severity: str = "warn",
) -> list[QualityIssue]:
    """按交易日判断新鲜度；节假日不要求产生新行情。"""
    if not latest_trade_date:
        return [
            QualityIssue(
                "Q014_stale_data",
                "high",
                {"as_of": as_of},
                "范围内没有任何行情记录",
            )
        ]
    days = [day for day in trading_days if day <= as_of]
    if not days:
        return []
    expected = days[-1]
    lag = len([day for day in days if day > latest_trade_date])
    if lag > STALE_TRADING_DAYS:
        return [
            QualityIssue(
                "Q014_stale_data",
                severity,
                {"as_of": as_of, "latest_trade_date": latest_trade_date, "lag_trading_days": lag},
                f"最新行情 {latest_trade_date} 落后截止时点 {lag} 个交易日",
            )
        ]
    return []


def check_future_records(
    records: Sequence[Mapping[str, Any]], *, today: date
) -> list[QualityIssue]:
    """检查业务日期落在未来的记录。"""
    issues: list[QualityIssue] = []
    today_text = yyyymmdd(today)
    for record in records:
        trade_date = str(record.get("trade_date") or "")
        if trade_date and trade_date > today_text:
            issues.append(
                QualityIssue(
                    "Q015_future_dated_record",
                    "high",
                    {"ts_code": record.get("ts_code"), "trade_date": trade_date},
                    f"业务日期 {trade_date} 晚于当前日期 {today_text}",
                )
            )
    return issues


def build_context(
    conn: sqlite3.Connection,
    source_code: str,
    *,
    as_of: str | None = None,
    now: date | None = None,
    start: str | None = None,
    end: str | None = None,
    view=None,
) -> QualityContext:
    """从数据库读取检查上下文（只读，按范围裁剪；可指定固定版本视图）。"""
    context = QualityContext(as_of=as_of, now=now or date.today())

    if view is not None:
        for day in view.calendar_days(on_or_before=end):
            context.all_calendar_dates.add(day)
            context.trading_days.add(day)
        for row in view.securities():
            context.securities[str(row["ts_code"])] = {
                "list_date": row["list_date"],
                "delist_date": row["delist_date"],
                "list_status": row["list_status"],
            }
        for ts_code, trade_date, event_type, detail in view.suspension_rows():
            if str(event_type) == "S" and not detail:
                context.suspensions[(str(ts_code), str(trade_date))] = str(event_type)
        return context

    calendar_query = (
        "SELECT exchange, cal_date, is_open FROM trade_calendar WHERE source_code=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM trade_calendar c2"
        "                    WHERE c2.source_code=trade_calendar.source_code"
        "                      AND c2.exchange=trade_calendar.exchange"
        "                      AND c2.cal_date=trade_calendar.cal_date)"
    )
    calendar_params: list[Any] = [source_code]
    if start:
        calendar_query += " AND cal_date>=?"
        calendar_params.append(start)
    if end:
        calendar_query += " AND cal_date<=?"
        calendar_params.append(end)
    rows = conn.execute(calendar_query, calendar_params).fetchall()
    for exchange, cal_date, is_open in rows:
        context.all_calendar_dates.add(cal_date)
        if int(is_open) == 1:
            context.trading_days.add(cal_date)

    rows = conn.execute(
        "SELECT ts_code, list_date, delist_date, list_status FROM security_info WHERE source_code=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM security_info s2"
        "                    WHERE s2.source_code=security_info.source_code"
        "                      AND s2.ts_code=security_info.ts_code)",
        (source_code,),
    ).fetchall()
    for ts_code, list_date, delist_date, list_status in rows:
        context.securities[ts_code] = {
            "list_date": list_date,
            "delist_date": delist_date,
            "list_status": list_status,
        }

    suspension_query = (
        "SELECT ts_code, trade_date, event_type, detail FROM suspension_event WHERE source_code=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM suspension_event e2"
        "                    WHERE e2.source_code=suspension_event.source_code"
        "                      AND e2.ts_code=suspension_event.ts_code"
        "                      AND e2.trade_date=suspension_event.trade_date"
        "                      AND e2.event_type=suspension_event.event_type)"
    )
    suspension_params: list[Any] = [source_code]
    if start:
        suspension_query += " AND trade_date>=?"
        suspension_params.append(start)
    if end:
        suspension_query += " AND trade_date<=?"
        suspension_params.append(end)
    rows = conn.execute(suspension_query, suspension_params).fetchall()
    for ts_code, trade_date, event_type, detail in rows:
        # 日内有限停牌（suspend_timing 非空）不是全日停牌，不算冲突。
        if event_type == "S" and not detail:
            context.suspensions[(ts_code, trade_date)] = event_type

    return context


def record_issues(
    conn: sqlite3.Connection,
    issues: Iterable[QualityIssue],
    *,
    batch_id: str | None = None,
    data_version_id: str | None = None,
) -> int:
    """把质量问题写入 ``quality_issue`` 表。"""
    detected_at = utc_now_iso()
    rows = [
        (
            issue.rule_code,
            issue.severity,
            _scope_json(issue.scope),
            issue.evidence,
            QUALITY_RULE_VERSION,
            data_version_id,
            batch_id,
            detected_at,
        )
        for issue in issues
    ]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO quality_issue(rule_code, severity, scope, evidence, rule_version,"
            " data_version_id, batch_id, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def _scope_json(scope: Mapping[str, Any]) -> str:
    import json

    return json.dumps(scope, ensure_ascii=False, sort_keys=True)


def summarize_issues(issues: Sequence[QualityIssue]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    severity: dict[str, int] = {"info": 0, "warn": 0, "high": 0}
    for issue in issues:
        counts[issue.rule_code] = counts.get(issue.rule_code, 0) + 1
        severity[issue.severity] = severity.get(issue.severity, 0) + 1
    return {
        "rule_version": QUALITY_RULE_VERSION,
        "total": len(issues),
        "by_rule": counts,
        "by_severity": severity,
        "blocking": severity.get("high", 0) > 0,
    }


def _daily_cursor(
    conn: sqlite3.Connection,
    source_code: str,
    view,
    batch_size: int,
):
    columns = "ts_code, trade_date, open, high, low, close, pre_close, volume, amount"
    daily_columns = ", ".join(f"d.{field.strip()}" for field in columns.split(","))
    if view is None:
        cursor = conn.execute(
            f"SELECT {columns} FROM daily_price WHERE source_code=?"
            " AND revision_no = (SELECT MAX(revision_no) FROM daily_price d2"
            "                    WHERE d2.source_code=daily_price.source_code"
            "                      AND d2.ts_code=daily_price.ts_code"
            "                      AND d2.trade_date=daily_price.trade_date)"
            " ORDER BY ts_code, trade_date",
            (source_code,),
        )
    else:
        cursor = conn.execute(
            f"SELECT {daily_columns} FROM data_version_chunk vc"
            " JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id"
            " JOIN daily_price d ON d.id=m.record_id"
            " WHERE vc.version_id=? AND vc.category='daily'"
            " ORDER BY d.ts_code, d.trade_date",
            (view.version_id,),
        )
    cursor.arraysize = batch_size
    return cursor


def run_dataset_quality_check(
    conn: sqlite3.Connection,
    source_code: str,
    *,
    as_of: str | None = None,
    today: date | None = None,
    data_version_id: str | None = None,
    batch_size: int = 5000,
) -> dict[str, Any]:
    """对已入库的完整数据集执行质量检查（流式，按证券有界处理）。

    不把全量日线读入内存；同一证券的记录连续处理，因此跨批重复键、交易日缺口、
    未来日期等检查语义与全量检查一致。
    """
    current = today or date.today()
    cutoff = as_of or yyyymmdd(current)
    view = None
    if data_version_id:
        from .chunks import VersionView

        # 指定版本时，版本的来源必须与本次检查声明的来源一致。
        view = VersionView(conn, data_version_id, expected_source_code=source_code)
    context = build_context(conn, source_code, as_of=cutoff, now=current, view=view)
    issues: list[QualityIssue] = []
    record_count = 0
    latest_trade_date: str | None = None
    current_code: str | None = None
    bucket: list[dict[str, Any]] = []

    def _flush(code: str | None, records: list[dict[str, Any]]) -> None:
        if not code or not records:
            return
        issues.extend(check_daily_records(records, context))
        issues.extend(check_trading_day_gaps(records, context))
        issues.extend(check_future_records(records, today=current))

    cursor = _daily_cursor(conn, source_code, view, batch_size)
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            record = {
                "ts_code": row[0],
                "trade_date": row[1],
                "open": row[2],
                "high": row[3],
                "low": row[4],
                "close": row[5],
                "pre_close": row[6],
                "volume": row[7],
                "amount": row[8],
            }
            record_count += 1
            trade_date = str(record["trade_date"] or "")
            if trade_date and (latest_trade_date is None or trade_date > latest_trade_date):
                latest_trade_date = trade_date
            code = str(record["ts_code"] or "")
            if code != current_code:
                _flush(current_code, bucket)
                current_code = code
                bucket = []
            bucket.append(record)
    _flush(current_code, bucket)

    issues.extend(
        check_source_publication_availability(
            source_code=source_code, categories=["daily", "adj_factor", "suspension", "corporate_action"]
        )
    )
    issues.extend(
        check_staleness(
            latest_trade_date=latest_trade_date,
            trading_days=sorted(context.trading_days),
            as_of=cutoff,
        )
    )
    summary = summarize_issues(issues)
    return {
        "summary": summary,
        "issues": [issue.as_dict() for issue in issues],
        "record_count": record_count,
        "latest_trade_date": latest_trade_date,
        "as_of": cutoff,
        "data_version_id": data_version_id,
        "batch_size": batch_size,
    }


def issue_counts_by_severity(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT severity, COUNT(*) FROM quality_issue GROUP BY severity"
    ).fetchall()
    counts = {"info": 0, "warn": 0, "high": 0}
    for severity, count in rows:
        counts[str(severity)] = int(count)
    return counts


__all__ = [
    "QUALITY_RULE_VERSION",
    "RULE_CODES",
    "QualityContext",
    "QualityIssue",
    "build_context",
    "check_daily_records",
    "check_future_records",
    "check_source_publication_availability",
    "check_staleness",
    "check_trading_day_gaps",
    "issue_counts_by_severity",
    "record_issues",
    "summarize_issues",
]
