"""规范化记录定义：类别、逻辑键、内容字段与单位换算。

每个来源行先经过本模块转换成 CANONICAL_FIELDS 形式，再由 :mod:`app.data.store`
按逻辑键写入修订表。换算规则写在来源能力矩阵里，并在记录里保留来源原始数值，
避免不同来源的单位被静默拼接。

逻辑键 = 数据源 + 证券/交易所 + 业务日期 + 数据类别；内容摘要只覆盖业务字段，
不含修订号与时间戳，因此“仅请求时间变化”不会产生新的业务修订。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .digests import content_hash
from .timeutil import utc_now_iso

# 单位换算：Tushare 文档口径（daily 接口 vol=手、amount=千元）。
LOTS_TO_SHARES = 100.0
THOUSAND_YUAN_TO_YUAN = 1000.0

CATEGORY_CALENDAR = "calendar"
CATEGORY_SECURITY = "security"
CATEGORY_DAILY = "daily"
CATEGORY_ADJ_FACTOR = "adj_factor"
CATEGORY_SUSPENSION = "suspension"
CATEGORY_CORPORATE_ACTION = "corporate_action"

ALL_CATEGORIES: tuple[str, ...] = (
    CATEGORY_CALENDAR,
    CATEGORY_SECURITY,
    CATEGORY_DAILY,
    CATEGORY_ADJ_FACTOR,
    CATEGORY_SUSPENSION,
    CATEGORY_CORPORATE_ACTION,
)


@dataclass(frozen=True)
class RecordSpec:
    """一个数据类别的落库形状。"""

    category: str
    table: str
    key_fields: tuple[str, ...]
    content_fields: tuple[str, ...]
    extra_columns: tuple[str, ...] = ()

    @property
    def insert_columns(self) -> tuple[str, ...]:
        return self.key_fields + self.content_fields + self.extra_columns


RECORD_SPECS: dict[str, RecordSpec] = {
    CATEGORY_CALENDAR: RecordSpec(
        category=CATEGORY_CALENDAR,
        table="trade_calendar",
        key_fields=("exchange", "cal_date"),
        content_fields=("is_open", "pretrade_date"),
    ),
    CATEGORY_SECURITY: RecordSpec(
        category=CATEGORY_SECURITY,
        table="security_info",
        key_fields=("ts_code",),
        content_fields=(
            "symbol",
            "name",
            "market",
            "exchange",
            "list_date",
            "delist_date",
            "list_status",
        ),
    ),
    CATEGORY_DAILY: RecordSpec(
        category=CATEGORY_DAILY,
        table="daily_price",
        key_fields=("ts_code", "trade_date"),
        content_fields=(
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "volume",
            "amount",
            "currency",
            "source_volume_raw",
            "source_amount_raw",
        ),
    ),
    CATEGORY_ADJ_FACTOR: RecordSpec(
        category=CATEGORY_ADJ_FACTOR,
        table="adj_factor",
        key_fields=("ts_code", "trade_date"),
        content_fields=("adj_factor",),
    ),
    CATEGORY_SUSPENSION: RecordSpec(
        category=CATEGORY_SUSPENSION,
        table="suspension_event",
        key_fields=("ts_code", "trade_date", "event_type"),
        content_fields=("detail",),
    ),
    CATEGORY_CORPORATE_ACTION: RecordSpec(
        category=CATEGORY_CORPORATE_ACTION,
        table="corporate_action",
        key_fields=("ts_code", "end_date", "div_proc"),
        content_fields=(
            "ann_date",
            "record_date",
            "ex_date",
            "pay_date",
            "div_listdate",
            "is_effective",
            "cash_div",
            "cash_div_tax",
            "stk_div",
            "stk_bo_rate",
            "stk_co_rate",
            "base_share",
        ),
    ),
}


def spec_for(category: str) -> RecordSpec:
    return RECORD_SPECS[category]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip()
    return text or None


def _key_text(value: Any) -> str:
    """逻辑键上的文本字段：缺失统一成空字符串，避免 NULL 破坏唯一约束。"""
    text = _text(value)
    return text if text is not None else ""


def _parse_price(value: Any) -> tuple[float | None, str | None]:
    """价格字段解析：返回 ``(数值, 失效原因)``。

    价格契约是有效有限正数：布尔值与无法解析的类型不得静默转换成数字，
    非正价格不是有效价格。``数值`` 为 ``None`` 且 ``失效原因`` 为 ``None``
    表示合法缺失（含占位符）；有失效原因时必须留下质量证据，
    字段值回落为缺失，该价格不得参与计算或成交用途。
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, f"布尔值 {value} 不是合法价格类型"
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text or text.lower() in NON_PARSED_NUMBER_TOKENS:
            return None, None
        try:
            number = float(text)
        except ValueError:
            return None, f"无法解析为数字的价格值: {text!r}"
    if math.isnan(number) or math.isinf(number):
        return None, f"价格不是有限数值: {number}"
    if number <= 0:
        return None, f"价格必须为正数，实际为 {number}"
    return number, None


def detect_invalid_price_fields(
    row: Mapping[str, Any], fields: Iterable[str] | None = None
) -> list[tuple[str, str]]:
    """找出来源行里不满足“有限正数”价格契约的字段与原因。

    非有限数值（nan/inf）由 :func:`detect_non_finite_fields` 记录，
    这里不再重复；其余不合法类型与非正数值在本函数留证据。
    """
    found: list[tuple[str, str]] = []
    for field in fields or PRICE_FIELDS:
        value = row.get(field)
        if value is None:
            continue
        _number, reason = _parse_price(value)
        if reason is None:
            continue
        if isinstance(value, str) and value.strip().lower() in NON_FINITE_TOKENS:
            continue
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            continue
        found.append((str(field), reason))
    return found


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null", "-"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _date_text(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if len(text) == 8 and text.isdigit():
        return text
    return text


def _is_open_flag(value: Any) -> int:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in {"1", "1.0"}:
            return 1
        if stripped in {"0", "0.0"}:
            return 0
    number = _number(value)
    if number is None:
        return 0
    return 1 if number >= 1 else 0


def normalize_calendar(row: Mapping[str, Any], *, default_exchange: str = "SSE") -> dict[str, Any]:
    return {
        "exchange": _key_text(row.get("exchange")) or default_exchange,
        "cal_date": _key_text(row.get("cal_date")),
        "is_open": _is_open_flag(row.get("is_open")),
        "pretrade_date": _date_text(row.get("pretrade_date")),
    }


def normalize_security(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts_code": _key_text(row.get("ts_code")),
        "symbol": _text(row.get("symbol")),
        "name": _text(row.get("name")),
        "market": _text(row.get("market")),
        "exchange": _text(row.get("exchange")),
        "list_date": _date_text(row.get("list_date")),
        "delist_date": _date_text(row.get("delist_date")),
        "list_status": _text(row.get("list_status")),
    }


def normalize_daily(row: Mapping[str, Any]) -> dict[str, Any]:
    volume_raw = _number(row.get("vol") if row.get("vol") is not None else row.get("volume"))
    amount_raw = _number(row.get("amount"))
    volume = None if volume_raw is None else volume_raw * LOTS_TO_SHARES
    amount = None if amount_raw is None else amount_raw * THOUSAND_YUAN_TO_YUAN
    # 价格字段按“有效有限正数”契约解析：不合法值回落为缺失并留证据，不静默转成数字。
    prices: dict[str, float | None] = {}
    for field in PRICE_FIELDS:
        number, _reason = _parse_price(row.get(field))
        prices[field] = number
    return {
        "ts_code": _key_text(row.get("ts_code")),
        "trade_date": _key_text(row.get("trade_date")),
        "open": prices["open"],
        "high": prices["high"],
        "low": prices["low"],
        "close": prices["close"],
        "pre_close": prices["pre_close"],
        "volume": volume,
        "amount": amount,
        "currency": _text(row.get("currency")) or "CNY",
        "source_volume_raw": volume_raw,
        "source_amount_raw": amount_raw,
    }


def normalize_adj_factor(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts_code": _key_text(row.get("ts_code")),
        "trade_date": _key_text(row.get("trade_date")),
        "adj_factor": _number(row.get("adj_factor")),
    }


def normalize_suspension(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_type = (_text(row.get("suspend_type")) or "").upper()
    event_type = "S" if raw_type.startswith("S") else "R" if raw_type.startswith("R") else ""
    return {
        "ts_code": _key_text(row.get("ts_code")),
        "trade_date": _key_text(row.get("trade_date")),
        "event_type": event_type,
        "detail": _text(row.get("suspend_timing")),
    }


def normalize_corporate_action(row: Mapping[str, Any]) -> dict[str, Any]:
    div_proc = _text(row.get("div_proc")) or ""
    return {
        "ts_code": _key_text(row.get("ts_code")),
        "end_date": _key_text(row.get("end_date")),
        "div_proc": div_proc,
        "ann_date": _date_text(row.get("ann_date")),
        "record_date": _date_text(row.get("record_date")),
        "ex_date": _date_text(row.get("ex_date")),
        "pay_date": _date_text(row.get("pay_date")),
        "div_listdate": _date_text(row.get("div_listdate")),
        "is_effective": 1 if "实施" in div_proc else 0,
        "cash_div": _number(row.get("cash_div")),
        "cash_div_tax": _number(row.get("cash_div_tax")),
        "stk_div": _number(row.get("stk_div")),
        "stk_bo_rate": _number(row.get("stk_bo_rate")),
        "stk_co_rate": _number(row.get("stk_co_rate")),
        "base_share": _number(row.get("base_share")),
    }


NORMALIZERS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    CATEGORY_CALENDAR: lambda row: normalize_calendar(row),
    CATEGORY_SECURITY: normalize_security,
    CATEGORY_DAILY: normalize_daily,
    CATEGORY_ADJ_FACTOR: normalize_adj_factor,
    CATEGORY_SUSPENSION: normalize_suspension,
    CATEGORY_CORPORATE_ACTION: normalize_corporate_action,
}


def normalize_row(category: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """按类别把来源行规范化；缺少键字段的行返回空字典（调用方应记为契约错误）。"""
    normalizer = NORMALIZERS.get(category)
    if normalizer is None:
        raise KeyError(f"未知数据类别: {category}")
    record = normalizer(row)
    spec = spec_for(category)
    if not all(record.get(field) for field in spec.key_fields):
        return {}
    return record


NON_FINITE_TOKENS = frozenset({"nan", "-nan", "inf", "-inf", "infinity", "-infinity"})

# 来源把缺失数据表达为这些占位符或空文本：按缺失处理，不是解析异常。
NON_PARSED_NUMBER_TOKENS = frozenset({"nan", "none", "null", "-"})

PRICE_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "pre_close")


def detect_non_finite_fields(row: Mapping[str, Any], fields: Iterable[str]) -> list[str]:
    """找出来源行里非有限的数值字段。

    规范化会把无法解析的文本变成缺失值，但“非有限数值”本身是一条质量证据，
    必须在丢失之前被记录下来。
    """
    found: list[str] = []
    for field in fields:
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, str) and value.strip().lower() in NON_FINITE_TOKENS:
            found.append(field)
            continue
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            found.append(field)
    return found


def logical_key(category: str, record: Mapping[str, Any], source_code: str) -> str:
    spec = spec_for(category)
    parts = [source_code, category] + [str(record.get(field, "")) for field in spec.key_fields]
    return "|".join(parts)


def record_content_hash(category: str, record: Mapping[str, Any]) -> str:
    spec = spec_for(category)
    payload = {field: record.get(field) for field in spec.content_fields}
    return content_hash(payload)


def record_values(category: str, record: Mapping[str, Any]) -> tuple[Any, ...]:
    spec = spec_for(category)
    return tuple(record.get(field) for field in spec.content_fields)


def revision_metadata(source_code: str, *, batch_id: str | None = None) -> dict[str, Any]:
    """新记录写入时附加的时间口径字段。"""
    now = utc_now_iso()
    return {
        "source_code": source_code,
        "source_published_at": None,
        "historical_available_at": None,
        "observed_at": now,
        "ingested_at": now,
        "batch_id": batch_id,
    }


__all__ = [
    "ALL_CATEGORIES",
    "CATEGORY_ADJ_FACTOR",
    "CATEGORY_CALENDAR",
    "CATEGORY_CORPORATE_ACTION",
    "CATEGORY_DAILY",
    "CATEGORY_SECURITY",
    "CATEGORY_SUSPENSION",
    "LOTS_TO_SHARES",
    "NON_PARSED_NUMBER_TOKENS",
    "NORMALIZERS",
    "NON_FINITE_TOKENS",
    "PRICE_FIELDS",
    "RECORD_SPECS",
    "RecordSpec",
    "THOUSAND_YUAN_TO_YUAN",
    "detect_invalid_price_fields",
    "detect_non_finite_fields",
    "logical_key",
    "normalize_adj_factor",
    "normalize_calendar",
    "normalize_corporate_action",
    "normalize_daily",
    "normalize_row",
    "normalize_security",
    "normalize_suspension",
    "record_content_hash",
    "record_values",
    "revision_metadata",
    "spec_for",
]
