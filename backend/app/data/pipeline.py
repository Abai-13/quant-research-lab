"""导入管线：范围 → 取数 → 归档 → 规范化 → 质量检查 → 事务写入 → 固化清单。

不变量：

- 失败重试有界（每页总尝试次数、等待预算都可配置并记录），权限与契约错误不重试；
- 缺页、截断、空响应、网络失败是不同状态，缺页不冒充完整成功；
- 写入失败或进程中断时，已发布旧版本仍可读，未完成范围不会成为可用新版本；
- 同一 worktree 的写入串行，冲突有有界等待与明确错误；
- 相同内容重复导入不产生新的业务记录或修订。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from ..config import Settings
from .adapters.base import DataAdapter, DataRequest, FetchOutcome
from .adapters.registry import build_adapter
from .archive import write_raw_response
from .catalog import ScopeUnit, build_scope_units, scope_summary
from .errors import RETRIABLE_REASONS, DataError
from .locking import DataWriteLock
from .quality import (
    QUALITY_RULE_VERSION,
    QualityIssue,
    build_context,
    check_daily_records,
    check_future_records,
    check_source_publication_availability,
    check_trading_day_gaps,
    record_issues,
    summarize_issues,
)
from .records import (
    ALL_CATEGORIES,
    CATEGORY_DAILY,
    PRICE_FIELDS,
    detect_invalid_price_fields,
    detect_non_finite_fields,
    logical_key,
    normalize_row,
    spec_for,
)
from .chunks import publish_chunked_version
from .sources import sync_source_registry
from .store import latest_active_version_id, write_records
from .timeutil import parse_yyyymmdd, utc_now_iso, yyyymmdd

__all__ = ["ImportOptions", "ImportResult", "run_import", "incremental_window"]


@dataclass
class ImportOptions:
    """一次导入的范围与策略。"""

    mode: str = "history"
    categories: tuple[str, ...] = ()
    start: str | None = None
    end: str | None = None
    codes: tuple[str, ...] = ()
    trade_dates: tuple[str, ...] = ()
    daily_by_code: bool = True
    allow_partial: bool = False
    exchange: str = "SSE"
    resume_batch_id: str | None = None

    def resolved_categories(self) -> tuple[str, ...]:
        if self.categories:
            return tuple(self.categories)
        return ALL_CATEGORIES


@dataclass
class ImportResult:
    """一次导入的结果摘要。"""

    batch_id: str
    source_code: str
    mode: str
    status: str
    scope: dict[str, Any] = field(default_factory=dict)
    pages_ok: int = 0
    pages_failed: int = 0
    attempts: int = 0
    records: dict[str, dict[str, int]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    version: dict[str, Any] | None = None
    started_at: str = ""
    finished_at: str = ""
    waiting_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "source_code": self.source_code,
            "mode": self.mode,
            "status": self.status,
            "scope": self.scope,
            "pages_ok": self.pages_ok,
            "pages_failed": self.pages_failed,
            "attempts": self.attempts,
            "records": self.records,
            "issues": self.issues,
            "errors": self.errors,
            "version": self.version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "waiting_seconds": round(self.waiting_seconds, 3),
        }


class _WaitBudget:
    """等待预算：每页与整批各有上限，超出即停止重试。"""

    def __init__(self, per_page_seconds: float, batch_seconds: float) -> None:
        self._per_page = per_page_seconds
        self._batch = batch_seconds
        self.batch_used = 0.0

    def allow(self, seconds: float, page_used: float) -> bool:
        return page_used + seconds <= self._per_page and self.batch_used + seconds <= self._batch

    def consume(self, seconds: float) -> None:
        self.batch_used += seconds

    def page_used(self, page_used: float, seconds: float) -> float:
        return page_used + seconds


def _log_request(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    request: DataRequest,
    status: str,
    attempt: int,
    raw_digest: str | None = None,
    error_class: str | None = None,
    detail: str | None = None,
    elapsed_ms: int | None = None,
    started_at: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO import_request_log(batch_id, interface, request_params, page_no, raw_digest,"
            " status, error_class, detail, attempt, started_at, finished_at, elapsed_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                request.interface,
                json.dumps(request.log_params(), ensure_ascii=False, sort_keys=True),
                request.page_no,
                raw_digest,
                status,
                error_class,
                detail,
                attempt,
                started_at or utc_now_iso(),
                utc_now_iso(),
                elapsed_ms,
            ),
        )


def _fetch_with_retry(
    adapter: DataAdapter,
    request: DataRequest,
    settings: Settings,
    budget: _WaitBudget,
    *,
    batch_id: str,
    conn: sqlite3.Connection,
) -> tuple[FetchOutcome | None, str | None, int]:
    """取一页并按有界策略重试。返回 (结果, 失败状态, 尝试次数)。"""
    retry = settings.import_retry
    backoff = retry.backoff_seconds_sequence or (1.0,)
    page_used = 0.0
    attempt = 0
    last_reason: str | None = None
    last_detail: str | None = None
    last_error: DataError | None = None

    while attempt < retry.max_attempts_per_page:
        attempt += 1
        interval = adapter.min_interval_seconds()
        if interval > 0:
            if not budget.allow(interval, page_used):
                _log_request(
                    conn,
                    batch_id=batch_id,
                    request=request,
                    status="wait_budget_exhausted",
                    attempt=attempt,
                    error_class="wait_budget_exhausted",
                    detail="等待预算耗尽，未继续请求",
                )
                return None, "wait_budget_exhausted", attempt
            time.sleep(interval)
            budget.consume(interval)
            page_used = budget.page_used(page_used, interval)

        started = utc_now_iso()
        began = time.monotonic()
        outcome = adapter.fetch_page(request)
        elapsed_ms = int((time.monotonic() - began) * 1000)
        if outcome.is_success:
            return outcome, None, attempt

        last_error = outcome.error
        last_reason = outcome.error_reason or "unknown_api_error"
        last_detail = str(outcome.error) if outcome.error is not None else None
        if last_reason not in RETRIABLE_REASONS or attempt >= retry.max_attempts_per_page:
            break
        wait = backoff[min(attempt - 1, len(backoff) - 1)]
        if not budget.allow(wait, page_used):
            _log_request(
                conn,
                batch_id=batch_id,
                request=request,
                status="wait_budget_exhausted",
                attempt=attempt,
                error_class="wait_budget_exhausted",
                detail="等待预算耗尽，停止重试",
                elapsed_ms=elapsed_ms,
                started_at=started,
            )
            return None, "wait_budget_exhausted", attempt
        time.sleep(wait)
        budget.consume(wait)
        page_used = budget.page_used(page_used, wait)

    _log_request(
        conn,
        batch_id=batch_id,
        request=request,
        status=last_reason or "failed",
        attempt=attempt,
        error_class=last_reason,
        detail=last_detail,
    )
    if last_error is not None and getattr(last_error, "detail", None):
        last_detail = str(last_error.detail)
    return None, last_reason or "failed", attempt


def _process_unit(
    conn: sqlite3.Connection,
    adapter: DataAdapter,
    settings: Settings,
    *,
    batch_id: str,
    unit: ScopeUnit,
    budget: _WaitBudget,
    today: date,
) -> tuple[list[dict[str, Any]], list[QualityIssue], list[dict[str, Any]], int, int]:
    """处理一个取数范围：分页取数 → 归档 → 规范化。"""
    rows: list[dict[str, Any]] = []
    issues: list[QualityIssue] = []
    errors: list[dict[str, Any]] = []
    pages_ok = 0
    pages_failed = 0
    attempts_total = 0
    declared_total: int | None = None
    max_rows = adapter.max_rows(unit.interface)
    page_no = 1

    while page_no <= settings.import_retry.max_pages_per_scope:
        request = DataRequest(interface=unit.interface, params=dict(unit.params), page_no=page_no)
        outcome, failure, attempts = _fetch_with_retry(
            adapter, request, settings, budget, batch_id=batch_id, conn=conn
        )
        attempts_total += attempts
        if outcome is None:
            pages_failed += 1
            errors.append(
                {
                    "interface": unit.interface,
                    "label": unit.label,
                    "page_no": page_no,
                    "status": failure or "failed",
                    "attempts": attempts,
                }
            )
            issues.append(
                QualityIssue(
                    "Q002_page_coverage_gap",
                    "high",
                    {
                        "interface": unit.interface,
                        "label": unit.label,
                        "page_no": page_no,
                        "status": failure or "failed",
                    },
                    "取数失败，该范围未获得完整数据",
                )
            )
            return rows, issues, errors, pages_ok, pages_failed, attempts_total

        if outcome.status == "empty":
            status = "empty_end" if page_no > 1 else "empty_ok"
            _log_request(
                conn,
                batch_id=batch_id,
                request=request,
                status=status,
                attempt=attempts,
            )
            pages_ok += 1
            break

        if outcome.raw_bytes:
            archive = write_raw_response(
                conn,
                settings,
                source_code=adapter.source_code,
                interface=unit.interface,
                raw_bytes=outcome.raw_bytes,
                batch_id=batch_id,
            )
            digest = archive.digest
        else:
            digest = None
        _log_request(
            conn,
            batch_id=batch_id,
            request=request,
            status="ok",
            attempt=attempts,
            raw_digest=digest,
        )
        pages_ok += 1
        rows.extend(outcome.rows)
        if outcome.declared_total is not None:
            declared_total = max(declared_total or 0, outcome.declared_total)

        if len(outcome.rows) >= max_rows:
            page_no += 1
            continue
        break
    else:
        errors.append(
            {
                "interface": unit.interface,
                "label": unit.label,
                "status": "max_pages_exceeded",
                "detail": f"单范围分页数超过上限 {settings.import_retry.max_pages_per_scope}",
            }
        )
        issues.append(
            QualityIssue(
                "Q002_page_coverage_gap",
                "high",
                {"interface": unit.interface, "label": unit.label},
                "分页数超过上限，范围可能未取完整",
            )
        )

    if declared_total is not None and len(rows) < declared_total:
        pages_failed += 1
        issues.append(
            QualityIssue(
                "Q002_page_coverage_gap",
                "high",
                {
                    "interface": unit.interface,
                    "label": unit.label,
                    "received_rows": len(rows),
                    "declared_total": declared_total,
                },
                "返回行数少于响应声称的总数，判定为缺页",
            )
        )
        errors.append(
            {
                "interface": unit.interface,
                "label": unit.label,
                "status": "page_incomplete",
                "received_rows": len(rows),
                "declared_total": declared_total,
            }
        )

    return rows, issues, errors, pages_ok, pages_failed, attempts_total


def _normalize_rows(
    category: str, rows: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[QualityIssue]]:
    """规范化来源行；缺少键字段的行记为契约错误，不静默丢弃。"""
    records: list[dict[str, Any]] = []
    issues: list[QualityIssue] = []
    for index, row in enumerate(rows):
        record = normalize_row(category, row)
        if not record:
            issues.append(
                QualityIssue(
                    "Q003_duplicate_key_conflict",
                    "high",
                    {"category": category, "row_index": index},
                    "来源行缺少逻辑键字段，无法规范化",
                )
            )
            continue
        if category == CATEGORY_DAILY:
            non_finite = detect_non_finite_fields(row, PRICE_FIELDS)
            if non_finite:
                issues.append(
                    QualityIssue(
                        "Q004_non_finite_price",
                        "high",
                        {
                            "category": category,
                            "row_index": index,
                            "ts_code": record.get("ts_code"),
                            "trade_date": record.get("trade_date"),
                            "fields": non_finite,
                        },
                        "来源行包含非有限数值：" + "、".join(non_finite),
                    )
                )
            invalid_prices = detect_invalid_price_fields(row, PRICE_FIELDS)
            if invalid_prices:
                # 布尔值与不合法类型不能静默变成数字；先在丢失前留下证据，
                # 再由资格检查拒绝该记录用于计算/成交。
                issues.append(
                    QualityIssue(
                        "Q018_invalid_price_format",
                        "high",
                        {
                            "category": category,
                            "row_index": index,
                            "ts_code": record.get("ts_code"),
                            "trade_date": record.get("trade_date"),
                            "fields": [field for field, _reason in invalid_prices],
                        },
                        "来源行价格不满足有效有限正数契约："
                        + "；".join(f"{field}: {reason}" for field, reason in invalid_prices),
                    )
                )
        records.append(record)
    return records, issues


def _collect_dataset_members(
    conn: sqlite3.Connection, source_code: str
) -> dict[str, list[tuple[int, str, str]]]:
    """收集来源当前全部最新修订，作为数据版本的成员集合。"""
    members: dict[str, list[tuple[int, str, str]]] = {}
    for category in ALL_CATEGORIES:
        spec = spec_for(category)
        rows = conn.execute(
            f"SELECT id, {', '.join(spec.key_fields)}, content_hash FROM {spec.table}"
            " WHERE source_code=? AND id IN ("
            f"   SELECT MAX(id) FROM {spec.table} WHERE source_code=?"
            f"   GROUP BY {', '.join(spec.key_fields)})",
            (source_code, source_code),
        ).fetchall()
        entries: list[tuple[int, str, str]] = []
        for row in rows:
            key_values = row[1 : 1 + len(spec.key_fields)]
            key = logical_key(category, dict(zip(spec.key_fields, key_values)), source_code)
            entries.append((int(row[0]), key, str(row[2])))
        if entries:
            members[category] = entries
    return members


def incremental_window(
    conn: sqlite3.Connection, source_code: str, *, today: date | None = None
) -> dict[str, Any]:
    """按业务范围与成功检查点计算增量起点，不使用进程启动时间。"""
    current = today or date.today()
    row = conn.execute(
        "SELECT MAX(trade_date) FROM daily_price WHERE source_code=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM daily_price d2"
        "                    WHERE d2.source_code=daily_price.source_code"
        "                      AND d2.ts_code=daily_price.ts_code"
        "                      AND d2.trade_date=daily_price.trade_date)",
        (source_code,),
    ).fetchone()
    stored_max = row[0] if row else None
    checkpoint_row = conn.execute(
        "SELECT checkpoint FROM import_batch WHERE source_code=? AND status IN ('completed','partial')"
        " ORDER BY created_at DESC LIMIT 1",
        (source_code,),
    ).fetchone()
    checkpoint_date = None
    if checkpoint_row and checkpoint_row[0]:
        try:
            payload = json.loads(checkpoint_row[0])
            checkpoint_date = payload.get("last_trade_date")
        except json.JSONDecodeError:
            checkpoint_date = None
    candidates = [value for value in (stored_max, checkpoint_date) if value]
    latest = max(candidates) if candidates else None
    return {
        "last_known_trade_date": latest,
        "stored_max_trade_date": stored_max,
        "checkpoint_trade_date": checkpoint_date,
        "today": yyyymmdd(current),
    }


def run_import(
    settings: Settings,
    options: ImportOptions,
    *,
    adapter: DataAdapter | None = None,
    now: date | None = None,
) -> ImportResult:
    """执行一次导入。"""
    source_code = settings.data_source.source_code
    resolved_adapter = adapter or build_adapter(settings)
    today = now or date.today()
    started_at = utc_now_iso()
    batch_id = f"batch_{uuid.uuid4().hex[:16]}"
    budget = _WaitBudget(
        settings.import_retry.per_page_wait_budget_seconds,
        settings.import_retry.batch_wait_budget_seconds,
    )
    lock = DataWriteLock(
        settings.lock_dir / "data-write.lock",
        wait_seconds=settings.import_retry.lock_wait_seconds,
    )
    lock.acquire()
    try:
        return _run_import_locked(
            settings,
            options,
            adapter=resolved_adapter,
            today=today,
            started_at=started_at,
            batch_id=batch_id,
            budget=budget,
        )
    finally:
        lock.release()


def _run_import_locked(
    settings: Settings,
    options: ImportOptions,
    *,
    adapter: DataAdapter,
    today: date,
    started_at: str,
    batch_id: str,
    budget: _WaitBudget,
) -> ImportResult:
    source_code = settings.data_source.source_code
    db_path = settings.db_path
    if not db_path.exists():
        raise DataError("database_missing", "数据库文件不存在；请先初始化或迁移数据库")
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        metadata = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
        version = int(metadata.get("schema_version", "0"))
        if version != 3:
            raise DataError(
                "migration_required",
                f"数据库 schema 版本为 {version}，需要先执行显式迁移命令（目标 schema 3）",
            )

        sync_source_registry(conn, source_code)

        start, end, trade_dates, codes = _resolve_scope(conn, settings, options, source_code)
        units = build_scope_units(
            categories=options.resolved_categories(),
            start=start,
            end=end,
            codes=codes,
            trade_dates=trade_dates,
            exchange=options.exchange,
            daily_by_code=options.daily_by_code,
        )
        scope = scope_summary(units)
        scope.update({"start": start, "end": end, "codes": list(codes)})

        completed_units: list[str] = []
        checkpoint: dict[str, Any] = {"completed_units": completed_units, "last_trade_date": None}
        if options.resume_batch_id:
            previous = conn.execute(
                "SELECT checkpoint FROM import_batch WHERE batch_id=?", (options.resume_batch_id,)
            ).fetchone()
            if previous and previous[0]:
                try:
                    stored = json.loads(previous[0])
                    completed_units = list(stored.get("completed_units") or [])
                    checkpoint["last_trade_date"] = stored.get("last_trade_date")
                except json.JSONDecodeError:
                    completed_units = []

        config_snapshot = {
            "source_code": source_code,
            "mode": options.mode,
            "max_attempts_per_page": settings.import_retry.max_attempts_per_page,
            "backoff_seconds": list(settings.import_retry.backoff_seconds_sequence),
            "per_page_wait_budget_seconds": settings.import_retry.per_page_wait_budget_seconds,
            "batch_wait_budget_seconds": settings.import_retry.batch_wait_budget_seconds,
            "min_interval_seconds": settings.import_retry.min_interval_seconds,
            "revision_window_days": settings.import_retry.revision_window_days,
            "rule_version": QUALITY_RULE_VERSION,
        }

        with conn:
            conn.execute(
                "INSERT INTO import_batch(batch_id, source_code, mode, categories, scope, status,"
                " pages_expected, pages_ok, pages_failed, attempts, max_attempts_per_page, checkpoint,"
                " error_class, started_at, finished_at, config_snapshot, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'running', 0, 0, 0, 0, ?, ?, NULL, ?, NULL, ?, ?)",
                (
                    batch_id,
                    source_code,
                    options.mode,
                    json.dumps(sorted(options.resolved_categories()), ensure_ascii=False),
                    json.dumps(scope, ensure_ascii=False, sort_keys=True),
                    settings.import_retry.max_attempts_per_page,
                    json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                    started_at,
                    json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True),
                    started_at,
                ),
            )

        issues: list[QualityIssue] = []
        errors: list[dict[str, Any]] = []
        records_summary: dict[str, dict[str, int]] = {}
        pages_ok = 0
        pages_failed = 0
        attempts_total = 0
        affected_partitions: dict[str, set[str]] = {}

        context = build_context(conn, source_code, as_of=end, now=today, start=start, end=end)
        issues.extend(
            check_source_publication_availability(
                source_code=source_code, categories=options.resolved_categories()
            )
        )

        for unit in units:
            if unit.key() in completed_units:
                continue
            rows, unit_issues, unit_errors, unit_ok, unit_failed, unit_attempts = _process_unit(
                conn,
                adapter,
                settings,
                batch_id=batch_id,
                unit=unit,
                budget=budget,
                today=today,
            )
            pages_ok += unit_ok
            pages_failed += unit_failed
            attempts_total += unit_attempts
            issues.extend(unit_issues)
            errors.extend(unit_errors)

            records, normalize_issues = _normalize_rows(unit.category, rows)
            issues.extend(normalize_issues)

            outcome = write_records(
                conn,
                unit.category,
                records,
                source_code=source_code,
                batch_id=batch_id,
            )
            for conflict_key in outcome.conflicts:
                issues.append(
                    QualityIssue(
                        "Q003_duplicate_key_conflict",
                        "high",
                        {"category": unit.category, "logical_key": conflict_key},
                        "同一批次内同一逻辑键出现互相冲突的内容；保留首条",
                    )
                )
            for category, partition in outcome.affected_partitions:
                affected_partitions.setdefault(category, set()).add(partition)
            with conn:
                conn.commit()
            category_summary = records_summary.setdefault(
                unit.category,
                {"inserted": 0, "revised": 0, "unchanged": 0, "skipped": 0, "total": 0},
            )
            for field_name in ("inserted", "revised", "unchanged", "skipped", "total"):
                category_summary[field_name] += outcome.as_dict()[field_name]

            # 只有上下文相关类别真正变化时才刷新，避免每个范围单元重复全量读取。
            if unit.category in {"calendar", "security", "suspension", "adj_factor"}:
                context = build_context(conn, source_code, as_of=end, now=today, start=start, end=end)

            if unit.category == CATEGORY_DAILY:
                issues.extend(check_daily_records(records, context))
                issues.extend(check_trading_day_gaps(records, context))
                issues.extend(check_future_records(records, today=today))

            completed_units.append(unit.key())
            if unit.category == CATEGORY_DAILY and records:
                dates = [str(record.get("trade_date") or "") for record in records]
                dates = [value for value in dates if value]
                if dates:
                    latest = max(dates)
                    current = checkpoint.get("last_trade_date")
                    checkpoint["last_trade_date"] = max(current, latest) if current else latest
            checkpoint["completed_units"] = completed_units
            with conn:
                conn.execute(
                    "UPDATE import_batch SET checkpoint=?, pages_ok=?, pages_failed=?, attempts=?"
                    " WHERE batch_id=?",
                    (
                        json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                        pages_ok,
                        pages_failed,
                        attempts_total,
                        batch_id,
                    ),
                )

        summary = summarize_issues(issues)
        version_info: dict[str, Any] | None = None
        if pages_failed == 0 or options.allow_partial:
            base_version_id = latest_active_version_id(conn, source_code)
            window_mode = "working" if options.mode == "incremental" else "full"
            window_start: str | None = None
            if window_mode == "working":
                anchor_date = parse_yyyymmdd(end) or today
                try:
                    window_start = yyyymmdd(
                        anchor_date.replace(
                            year=anchor_date.year - settings.import_retry.working_window_years
                        )
                    )
                except ValueError:  # 2 月 29 日
                    window_start = yyyymmdd(
                        anchor_date.replace(
                            year=anchor_date.year - settings.import_retry.working_window_years,
                            day=28,
                        )
                    )
            scope_payload = dict(scope)
            scope_payload["partial"] = pages_failed > 0
            if pages_failed > 0:
                scope_payload["failed_units"] = [
                    error.get("label") for error in errors if error.get("status")
                ]
            version_info = publish_chunked_version(
                conn,
                source_code=source_code,
                scope=scope_payload,
                categories=sorted(affected_partitions) or list(ALL_CATEGORIES),
                rule_version=QUALITY_RULE_VERSION,
                quality_summary=summary,
                base_version_id=base_version_id,
                affected_partitions={
                    category: sorted(partitions)
                    for category, partitions in affected_partitions.items()
                },
                window_mode=window_mode,
                window_start=window_start,
            )
        else:
            summary["blocking"] = True

        record_issues(
            conn,
            issues,
            batch_id=batch_id,
            data_version_id=version_info["version_id"] if version_info else None,
        )

        status = "completed" if pages_failed == 0 else ("partial" if options.allow_partial else "failed")
        finished_at = utc_now_iso()
        with conn:
            conn.execute(
                "UPDATE import_batch SET status=?, pages_ok=?, pages_failed=?, attempts=?,"
                " checkpoint=?, error_class=?, finished_at=? WHERE batch_id=?",
                (
                    status,
                    pages_ok,
                    pages_failed,
                    attempts_total,
                    json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                    errors[0].get("status") if errors else None,
                    finished_at,
                    batch_id,
                ),
            )

        return ImportResult(
            batch_id=batch_id,
            source_code=source_code,
            mode=options.mode,
            status=status,
            scope=scope,
            pages_ok=pages_ok,
            pages_failed=pages_failed,
            attempts=attempts_total,
            records=records_summary,
            issues=[issue.as_dict() for issue in issues],
            errors=errors,
            version=version_info,
            started_at=started_at,
            finished_at=finished_at,
            waiting_seconds=budget.batch_used,
        )
    finally:
        conn.close()


def _resolve_scope(
    conn: sqlite3.Connection,
    settings: Settings,
    options: ImportOptions,
    source_code: str,
) -> tuple[str, str, list[str], list[str]]:
    """确定日期区间、交易日集合与证券样本。"""
    today = date.today()
    codes = [code.strip() for code in options.codes if code and code.strip()]

    if options.mode == "incremental":
        window = incremental_window(conn, source_code, today=today)
        revision_days = settings.import_retry.revision_window_days
        end = options.end or window["today"]
        latest = window["last_known_trade_date"]
        if latest:
            base = parse_yyyymmdd(latest)
            start_date = (base - timedelta(days=revision_days)) if base else today
            start = options.start or yyyymmdd(start_date)
        else:
            start = options.start or yyyymmdd(today - timedelta(days=revision_days))
        trade_dates = _known_trading_days(conn, source_code, start, end)
    else:
        start = options.start or yyyymmdd(today - timedelta(days=30))
        end = options.end or yyyymmdd(today)
        trade_dates = list(options.trade_dates)

    if not trade_dates:
        trade_dates = _known_trading_days(conn, source_code, start, end)
    return start, end, trade_dates, codes


def _known_trading_days(
    conn: sqlite3.Connection, source_code: str, start: str, end: str
) -> list[str]:
    rows = conn.execute(
        "SELECT cal_date FROM trade_calendar WHERE source_code=? AND is_open=1"
        " AND cal_date>=? AND cal_date<=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM trade_calendar c2"
        "                    WHERE c2.source_code=trade_calendar.source_code"
        "                      AND c2.exchange=trade_calendar.exchange"
        "                      AND c2.cal_date=trade_calendar.cal_date)"
        " ORDER BY cal_date",
        (source_code, start, end),
    ).fetchall()
    return [row[0] for row in rows]
