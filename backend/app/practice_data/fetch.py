"""练习数据取数器：BaoStock 官方客户端适配与离线合成样本。

契约统一输出行 dict：
    code, trade_date, open, high, low, close, preclose,
    volume, amount, adjustflag, tradestatus

错误语义（供 API 映射）：
- SourceUnavailable：客户端缺失/登录失败等“当前不可取数”。
- SourceDataError：返回数据违反官方字段契约（缺失列、无法解析）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from typing import Any

__all__ = [
    "DAILY_FIELD_CONTRACT",
    "SourceDataError",
    "SourceUnavailable",
    "baostock_fetch_daily",
    "synthetic_fetch_daily",
    "normalize_daily_row",
]

# 与官网《历史行情指标参数》日线章节一致的列契约。
DAILY_FIELD_CONTRACT = (
    "date,code,open,high,low,close,preclose,volume,amount,"
    "adjustflag,turn,tradestatus,pctChg,isST"
)


class SourceUnavailable(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class SourceDataError(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def baostock_fetch_daily(
    code: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """通过官方客户端取不复权日线；显式调用，不缓存连接。"""
    try:
        import baostock as bs
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都不可伪装成功
        raise SourceUnavailable("baostock_not_installed", str(exc)) from exc

    lg = bs.login()
    try:
        if getattr(lg, "error_code", None) != "0":
            raise SourceUnavailable(
                "baostock_login_failed",
                f"login error_code={getattr(lg, 'error_code', None)!r} "
                f"error_msg={getattr(lg, 'error_msg', '')!r}",
            )
        rs = bs.query_history_k_data_plus(
            code,
            DAILY_FIELD_CONTRACT,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="3",
        )
        if getattr(rs, "error_code", None) != "0":
            raise SourceDataError(
                "baostock_query_failed",
                f"query_history_k_data_plus error_code={getattr(rs, 'error_code', None)!r} "
                f"error_msg={getattr(rs, 'error_msg', '')!r}",
            )
        fields = list(getattr(rs, "fields", []) or [])
        raw_rows = []
        while (rs.error_code == "0") and rs.next():
            raw_rows.append(rs.get_row_data())
        if fields != list(DAILY_FIELD_CONTRACT.split(",")):
            raise SourceDataError(
                "baostock_field_contract_mismatch",
                f"返回列 {fields!r} 与官方日线契约 {DAILY_FIELD_CONTRACT!r} 不一致",
            )
        rows = [normalize_daily_row(code, fields, raw) for raw in raw_rows]
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001 - 登出失败不掩盖取数成功
            pass
    return rows


def normalize_daily_row(
    code: str,
    fields: list[str],
    raw: list[Any],
) -> dict[str, Any]:
    """把官方原始行解析为统一契约；缺失/坏值显式报错，不冒充完整。"""
    if len(raw) != len(fields):
        raise SourceDataError(
            "baostock_row_shape_mismatch",
            f"{code} 行列数 {len(raw)} 与字段契约 {len(fields)} 不一致",
        )
    record = dict(zip(fields, raw))
    trade_date = str(record.get("date", "")).strip()
    if not trade_date:
        raise SourceDataError("baostock_empty_trade_date", f"{code} 返回行缺少日期")
    parsed: dict[str, Any] = {"code": code, "trade_date": trade_date}
    for column in ("open", "high", "low", "close", "preclose", "amount"):
        parsed[column] = _optional_float(code, trade_date, column, record.get(column))
    parsed["volume"] = _optional_int(code, trade_date, "volume", record.get("volume"))
    parsed["adjustflag"] = str(record.get("adjustflag", "")).strip() or None
    parsed["tradestatus"] = _optional_int(
        code, trade_date, "tradestatus", record.get("tradestatus")
    )
    if parsed["tradestatus"] is not None and parsed["tradestatus"] not in (0, 1):
        raise SourceDataError(
            "baostock_tradestatus_invalid",
            f"{code} {trade_date} tradestatus={parsed['tradestatus']!r} 不在 0/1 域",
        )
    if parsed["tradestatus"] == 1:
        for column in ("open", "high", "low", "close", "preclose"):
            if parsed[column] is None:
                raise SourceDataError(
                    "baostock_price_missing",
                    f"{code} {trade_date} 交易日 {column} 为空",
                )
    return parsed


def _optional_int(code: str, trade_date: str, column: str, value: Any) -> int | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return int(round(float(text)))
    except (TypeError, ValueError) as exc:
        raise SourceDataError(
            "baostock_value_not_numeric",
            f"{code} {trade_date} {column}={value!r} 无法解析为数值",
        ) from exc


def _optional_float(code: str, trade_date: str, column: str, value: Any) -> float | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        result = float(text)
    except (TypeError, ValueError) as exc:
        raise SourceDataError(
            "baostock_value_not_numeric",
            f"{code} {trade_date} {column}={value!r} 无法解析为数值",
        ) from exc
    if math.isnan(result) or math.isinf(result):
        raise SourceDataError(
            "baostock_value_not_numeric",
            f"{code} {trade_date} {column}={value!r} 不是有限数值",
        )
    return result


def _weekday_series(start_date: str, end_date: str) -> list[str]:
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    days: list[str] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    return days


def synthetic_fetch_daily(
    code: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """确定性合成日线；同一输入永远同结果，无网络。"""
    rows: list[dict[str, Any]] = []
    close = 10.0
    for trade_date in _weekday_series(start_date, end_date):
        seed = hashlib.sha256(f"{code}|{trade_date}".encode("utf-8")).digest()
        drift = ((seed[0] / 255.0) - 0.5) * 0.02
        day_open = round(close * (1.0 + drift), 4)
        high = round(day_open * 1.01, 4)
        low = round(day_open * 0.99, 4)
        day_close = round((day_open + high + low) / 3.0, 4)
        preclose = close
        volume = 1_000_000 + seed[1] * 10_000
        amount = round(day_close * volume, 2)
        rows.append(
            {
                "code": code,
                "trade_date": trade_date,
                "open": day_open,
                "high": high,
                "low": low,
                "close": day_close,
                "preclose": preclose,
                "volume": int(volume),
                "amount": amount,
                "adjustflag": "3",
                "tradestatus": 1,
            }
        )
        close = day_close
    return rows
