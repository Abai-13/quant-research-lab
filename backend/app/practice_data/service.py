"""显式刷新编排：请求校验 → 幂等 → 取数 → 版本写入。

只有 ``refresh`` 入口会访问网络（仅 ``baostock`` 源），且必须由
``POST /data/refresh`` 触发；合成源完全离线。任何一步失败都不写入
部分版本：取数与入库在同一事务内提交或回滚。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from . import store
from .fetch import (
    SourceDataError,
    SourceUnavailable,
    baostock_fetch_daily,
    synthetic_fetch_daily,
)

__all__ = ["RefreshError", "perform_refresh", "validate_refresh_payload"]

MAX_SYMBOLS = 20
MAX_SPAN_DAYS = 366
MAX_DAILY_ROWS = 20_000
MAX_UNIT_ATTEMPTS = 3
MAX_RETRY_WAIT_SECONDS = 60
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BAOSTOCK_SYMBOL_RE = re.compile(r"^(?:sh|sz|bj)\.\d{6}$", re.IGNORECASE)
_BAOSTOCK_SOURCE_LOCK = threading.Lock()

PROVIDERS: dict[str, Callable[[str, str, str], list[dict[str, Any]]]] = {
    "baostock": baostock_fetch_daily,
    "synthetic": synthetic_fetch_daily,
}

SYNTHETIC_SOURCES = {"synthetic"}


class RefreshError(RuntimeError):
    def __init__(self, reason: str, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code


def validate_refresh_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """校验显式刷新请求的形状；返回规范化后的请求体。"""
    source_code = str(payload.get("source_code", "")).strip()
    if not source_code:
        raise RefreshError("invalid_request", "source_code 不能为空")
    if source_code not in PROVIDERS:
        raise RefreshError("unknown_source", f"未声明数据源 {source_code!r}", 404)
    symbols_raw = payload.get("symbols")
    if not isinstance(symbols_raw, list) or not symbols_raw:
        raise RefreshError("invalid_request", "symbols 必须是非空列表")
    symbols = [str(item).strip().lower() if source_code == "baostock" else str(item).strip() for item in symbols_raw if str(item).strip()]
    if not symbols:
        raise RefreshError("invalid_request", "symbols 解析后为空")
    if len(symbols) != len(set(symbols)):
        raise RefreshError("invalid_request", "symbols 存在重复项")
    if len(symbols) > MAX_SYMBOLS:
        raise RefreshError(
            "symbols_too_many",
            f"小样本策略每次至多 {MAX_SYMBOLS} 个代码，实际 {len(symbols)} 个",
        )
    if source_code == "baostock" and any(not _BAOSTOCK_SYMBOL_RE.fullmatch(symbol) for symbol in symbols):
        raise RefreshError("symbol_invalid", "BaoStock 代码须为 sh|sz|bj.六位证券代码")
    symbols.sort()
    start_date = str(payload.get("start_date", "")).strip()
    end_date = str(payload.get("end_date", "")).strip()
    for label, value in (("start_date", start_date), ("end_date", end_date)):
        if not _DATE_RE.match(value):
            raise RefreshError(
                "invalid_request", f"{label}={value!r} 不符合 YYYY-MM-DD 格式"
            )
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise RefreshError("invalid_request", f"{label}={value!r} 不是有效日期") from exc
    if start_date > end_date:
        raise RefreshError("invalid_request", "start_date 必须不晚于 end_date")
    if (dt.date.fromisoformat(end_date) - dt.date.fromisoformat(start_date)).days + 1 > MAX_SPAN_DAYS:
        raise RefreshError(
            "date_range_too_long",
            f"小样本策略日期跨度至多 {MAX_SPAN_DAYS} 天",
        )
    idempotency_key = str(payload.get("idempotency_key", "")).strip()
    if not idempotency_key:
        raise RefreshError(
            "invalid_request", "idempotency_key 必填（显式刷新必须可重试复用）"
        )
    return {
        "source_code": source_code,
        "symbols": symbols,
        "start_date": start_date,
        "end_date": end_date,
        "idempotency_key": idempotency_key,
    }


def request_sha256(normalized: dict[str, Any]) -> str:
    canonical = store.encode_json(normalized)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def perform_refresh(
    conn: sqlite3.Connection,
    normalized: dict[str, Any],
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    publish_guard: Callable[[], bool] | None = None,
    fetcher: Callable[[str, str, str], list[dict[str, Any]]] | None = None,
    cached_units: dict[str, list[dict[str, Any]]] | None = None,
    checkpoint_callback: Callable[[str, list[dict[str, Any]]], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """执行一次显式刷新；返回 (响应载荷, 是否复用旧结果)。

    调用方负责连接的生命周期；本函数提交属于自己的写入事务。
    """
    source_code = normalized["source_code"]
    synthetic = source_code in SYNTHETIC_SOURCES
    store.ensure_practice_data_schema(conn)
    sha = request_sha256(normalized)
    previous = store.get_refresh_request(conn, normalized["idempotency_key"])
    if previous is not None:
        if previous["request_sha256"] != sha:
            raise RefreshError(
                "idempotency_conflict",
                "相同 idempotency_key 已绑定不同请求内容",
                409,
            )
        return dict(previous["response"]), True

    limitations = list(SOURCE_COVER_LIMITATIONS[synthetic])
    rows: list[dict[str, Any]] = []
    per_symbol_rows: dict[str, int] = {}
    fetch = fetcher or PROVIDERS[source_code]
    total_retry_wait = 0.0
    for unit_index, symbol in enumerate(normalized["symbols"], start=1):
        if cached_units and symbol in cached_units:
            symbol_rows = cached_units[symbol]
        else:
            for attempt in range(1, MAX_UNIT_ATTEMPTS + 1):
                try:
                    if source_code == "baostock":
                        with _BAOSTOCK_SOURCE_LOCK:
                            symbol_rows = fetch(symbol, normalized["start_date"], normalized["end_date"])
                    else:
                        symbol_rows = fetch(symbol, normalized["start_date"], normalized["end_date"])
                    break
                except SourceUnavailable as exc:
                    if attempt >= MAX_UNIT_ATTEMPTS:
                        raise RefreshError(exc.reason, exc.detail, 503) from exc
                    delay = float(2 ** (attempt - 1))
                    if total_retry_wait + delay > MAX_RETRY_WAIT_SECONDS:
                        raise RefreshError("retry_budget_exhausted", exc.detail, 503) from exc
                    time.sleep(delay)
                    total_retry_wait += delay
                except SourceDataError as exc:
                    raise RefreshError(exc.reason, exc.detail) from exc
        if len(symbol_rows) > MAX_DAILY_ROWS - len(rows):
            raise RefreshError("daily_rows_too_many", f"日线行数超过 {MAX_DAILY_ROWS}")
        seen_dates: set[str] = set()
        for row in symbol_rows:
            try:
                row_date = dt.date.fromisoformat(str(row["trade_date"]))
            except (KeyError, ValueError) as exc:
                raise RefreshError("trade_date_invalid", f"{symbol} 返回无效交易日期") from exc
            if not normalized["start_date"] <= row_date.isoformat() <= normalized["end_date"]:
                raise RefreshError("trade_date_out_of_range", f"{symbol} 返回范围外数据 {row_date}")
            if row_date.isoformat() in seen_dates:
                raise RefreshError("trade_date_duplicate", f"{symbol} 返回重复日期 {row_date}")
            seen_dates.add(row_date.isoformat())
            if source_code == "baostock" and row.get("adjustflag") != "3":
                raise RefreshError("adjustment_flag_invalid", f"{symbol} {row_date} 不是不复权数据")
            if row.get("volume") is not None and int(row["volume"]) < 0:
                raise RefreshError("volume_invalid", f"{symbol} {row_date} 成交量为负")
            if row.get("amount") is not None and float(row["amount"]) < 0:
                raise RefreshError("amount_invalid", f"{symbol} {row_date} 成交额为负")
            if row.get("tradestatus") == 0 and (
                (row.get("volume") not in (None, 0)) or (row.get("amount") not in (None, 0))
            ):
                raise RefreshError("suspension_inconsistent", f"{symbol} {row_date} 停牌行成交量/额非零")
            if row.get("tradestatus") == 1:
                values = [row.get(key) for key in ("open", "high", "low", "close")]
                if any(value is None or float(value) <= 0 for value in values):
                    raise RefreshError("price_invalid", f"{symbol} {row_date} 交易日价格缺失或非正")
                if float(row["high"]) < max(float(row["open"]), float(row["close"])) or float(row["low"]) > min(float(row["open"]), float(row["close"])):
                    raise RefreshError("ohlc_inconsistent", f"{symbol} {row_date} OHLC 区间不一致")
        per_symbol_rows[symbol] = len(symbol_rows)
        rows.extend(symbol_rows)
        if checkpoint_callback is not None:
            checkpoint_callback(symbol, symbol_rows)
        if progress_callback is not None:
            progress_callback(unit_index, len(normalized["symbols"]))

    span_rows = max(per_symbol_rows.values()) if per_symbol_rows else 0
    coverage = {
        "symbols": len(normalized["symbols"]),
        "daily_rows": len(rows),
        "content_sha256": hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "per_symbol_rows": per_symbol_rows,
        "date_span": normalized["start_date"] + ".." + normalized["end_date"],
        "calendar_checked": False,
        "unit_contract": {"price": "CNY/share", "volume": "shares", "amount": "CNY", "adjustflag": "3=unadjusted"},
        "corporate_actions": "not_included",
        "adjust_factor": "not_requested_upstream_interface_exists",
    }
    record_count = registry_count(rows)
    version_id = build_version_id(source_code, normalized, rows)
    created_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        if publish_guard is not None and not publish_guard():
            raise RefreshError("publication_fenced", "任务已取消或代次已变更，拒绝发布")
        store.insert_version(
            conn,
            {
                "version_id": version_id,
                "source_code": source_code,
                "synthetic": synthetic,
                "symbols": normalized["symbols"],
                "start_date": normalized["start_date"],
                "end_date": normalized["end_date"],
                "record_count": record_count,
                "coverage": coverage,
                "limitations": limitations,
                "created_at": created_at,
            },
        )
        store.insert_daily_rows(conn, version_id, rows)
        response = {
            "ok": True,
            "version": {
                "version_id": version_id,
                "source_code": source_code,
                "synthetic": synthetic,
                "symbols": normalized["symbols"],
                "start_date": normalized["start_date"],
                "end_date": normalized["end_date"],
                "record_count": record_count,
                "coverage": coverage,
                "limitations": limitations,
                "created_at": created_at,
            },
            "evidence": {
                "real_network": not synthetic,
                "offline_synthetic": synthetic,
                "provider": source_code,
                "fetched_rows": len(rows),
            },
            "reused": False,
        }
        store.put_refresh_request(
            conn, normalized["idempotency_key"], sha, response
        )
        conn.commit()
        return response, False
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        raise RefreshError("storage_failed", f"版本写入失败：{exc}") from exc


SOURCE_COVER_LIMITATIONS: dict[bool, list[str]] = {
    True: [
        "合成样本：工作日确定性序列，没有停牌、涨跌停、上市区间与真实公司行为。",
        "练习库共享数据版本：所有练习可复用，删除前版本不可复现的日子需新建。",
    ],
    False: [
        "真实源：当前只取不复权日线；上游有 query_adjust_factor，但本版未接入，含除权除息日价格保持原始值。",
        "未包含收盘后公司行为处理：分红/送转/停牌交易日历校验未启用，保持受限。",
    ],
}


def registry_count(rows: list[dict[str, Any]]) -> int:
    """逻辑行数：以版本号+代码+日期去重计数。"""
    unique: set[tuple[str, str]] = set()
    for row in rows:
        unique.add((row["code"], row["trade_date"]))
    return len(unique)


def build_version_id(
    source_code: str,
    normalized: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    digest_source = store.encode_json(
        {
            "source_code": source_code,
            "symbols": normalized["symbols"],
            "start_date": normalized["start_date"],
            "end_date": normalized["end_date"],
            "row_dates": [(row["code"], row["trade_date"]) for row in rows],
        }
    )
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:12]
    marker = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # Separate refreshes can legitimately fetch the same symbols/date span in
    # the same clock tick. Keep each immutable version unique; idempotent
    # retries are handled by the request table above.
    return f"{source_code}-{marker}-{digest}-{uuid4().hex[:8]}"
