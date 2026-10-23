"""时间口径工具。

约定：
- 数据库时间戳统一为 UTC 带时区的 ISO-8601 字符串；
- 交易日按交易所业务日期保存为 ``YYYYMMDD`` 文本；
- 界面展示使用 Asia/Shanghai 时区。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# 中国大陆自 1991 年起不再使用夏令时，Asia/Shanghai 恒为 UTC+8。
# 这里使用固定偏移，避免在缺少 tzdata 的 Windows 环境上依赖 ZoneInfo 数据包。
SH_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
UTC_TZ = timezone.utc


class TimeParseError(ValueError):
    """日期或时间字符串不合法。"""


def utc_now_iso() -> str:
    """当前 UTC 时间，ISO-8601 带偏移，秒精度。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc_iso(value: str | None, *, field: str = "timestamp") -> datetime | None:
    """解析 UTC ISO-8601 字符串；空值返回 None。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeParseError(f"{field} 不是合法时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise TimeParseError(f"{field} 缺少时区: {value!r}")
    return parsed


def require_utc_iso(value: str | None, *, field: str = "timestamp") -> str:
    """要求可解析的带时区时间并返回标准化 UTC ISO 字符串。"""
    parsed = parse_utc_iso(value, field=field)
    if parsed is None:
        raise TimeParseError(f"{field} 不能为空")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_yyyymmdd(value: str | None, *, field: str = "trade_date") -> date | None:
    """解析 ``YYYYMMDD`` 业务日期；空值返回 None。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise TimeParseError(f"{field} 必须是 YYYYMMDD 格式: {value!r}") from exc


def require_yyyymmdd(value: str | None, *, field: str = "trade_date") -> str:
    """要求可解析的业务日期并返回 ``YYYYMMDD`` 文本。"""
    parsed = parse_yyyymmdd(value, field=field)
    if parsed is None:
        raise TimeParseError(f"{field} 不能为空")
    return parsed.strftime("%Y%m%d")


def yyyymmdd(value: date) -> str:
    """日期对象转 ``YYYYMMDD``。"""
    return value.strftime("%Y%m%d")


def resolve_date_range(start: str | None, end: str | None, *, field_prefix: str = "date") -> tuple[str, str]:
    """校验并解析日期区间，返回 (start, end)，均为 YYYYMMDD。"""
    start_date = parse_yyyymmdd(start, field=f"{field_prefix}_start")
    end_date = parse_yyyymmdd(end, field=f"{field_prefix}_end")
    if start_date is None or end_date is None:
        raise TimeParseError(f"{field_prefix} 区间的开始与结束日期都不能为空")
    if start_date > end_date:
        raise TimeParseError(f"{field_prefix} 区间开始日期晚于结束日期")
    return start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")


def display_shanghai(utc_iso: str | None) -> str | None:
    """UTC 时间转上海时间展示文本；输入 None 或不可解析时原样返回。"""
    if not utc_iso:
        return None
    parsed = datetime.fromisoformat(utc_iso)
    return parsed.astimezone(SH_TZ).isoformat(timespec="seconds")


def calendar_dates(start: str, end: str) -> list[str]:
    """闭区间内的自然日列表（YYYYMMDD），用于测试夹具构造。"""
    start_date = parse_yyyymmdd(start)
    end_date = parse_yyyymmdd(end)
    if start_date is None or end_date is None:
        raise TimeParseError("日期区间不完整")
    days: list[str] = []
    current = start_date
    while current <= end_date:
        days.append(yyyymmdd(current))
        current = current + timedelta(days=1)
    return days
