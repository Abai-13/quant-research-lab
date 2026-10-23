"""价格解析契约：有效有限正数；不合法类型不得静默转成数字。"""

from __future__ import annotations

from app.data.records import (
    detect_invalid_price_fields,
    normalize_daily,
)


def test_valid_prices_parse_to_positive_numbers() -> None:
    record = normalize_daily(
        {
            "ts_code": "000001.SZ",
            "trade_date": "20240102",
            "open": "10.0",
            "high": 10.2,
            "low": " 9.9 ",
            "close": 10.1,
            "pre_close": 10,
            "vol": 1000,
            "amount": 1010.0,
        }
    )

    assert record["open"] == 10.0
    assert record["high"] == 10.2
    assert record["low"] == 9.9
    assert record["close"] == 10.1
    assert record["pre_close"] == 10.0
    # 量额解析保持既有语义。
    assert record["source_volume_raw"] == 1000.0
    assert record["volume"] == 100_000.0
    assert record["amount"] == 1_010_000.0


def test_bool_price_is_not_silently_converted() -> None:
    record = normalize_daily(
        {"ts_code": "000001.SZ", "trade_date": "20240102", "close": True, "pre_close": False}
    )

    assert record["close"] is None
    assert record["pre_close"] is None


def test_non_positive_price_fails_contract() -> None:
    zero = normalize_daily(
        {"ts_code": "000001.SZ", "trade_date": "20240102", "close": 0, "pre_close": 0.0}
    )
    negative = normalize_daily(
        {"ts_code": "000001.SZ", "trade_date": "20240102", "close": -10.5, "pre_close": "-0"}
    )

    assert zero["close"] is None and zero["pre_close"] is None
    assert negative["close"] is None and negative["pre_close"] is None


def test_missing_and_placeholder_stay_missing_without_exception() -> None:
    record = normalize_daily(
        {"ts_code": "000001.SZ", "trade_date": "20240102", "close": None, "pre_close": "-"}
    )

    assert record["close"] is None
    assert record["pre_close"] is None


def test_detect_invalid_price_fields_reports_reasons() -> None:
    row = {
        "open": 10.0,
        "high": True,
        "low": -1.0,
        "close": 0,
        "pre_close": "not-a-number",
        "volume": -1000,
        "amount": 0,
    }

    found = dict(detect_invalid_price_fields(row))

    assert set(found) == {"high", "low", "close", "pre_close"}
    assert "布尔值" in found["high"]
    assert "正数" in found["low"]
    assert "正数" in found["close"]
    assert "无法解析" in found["pre_close"]


def test_detect_invalid_price_fields_skips_valid_and_non_finite() -> None:
    # 非有限数值由 detect_non_finite_fields 单独记录，这里不重复。
    row = {"open": "10", "high": 10.5, "low": None, "close": "nan", "pre_close": "inf"}

    assert detect_invalid_price_fields(row) == []


def test_detect_invalid_price_fields_defaults_to_price_fields() -> None:
    found = detect_invalid_price_fields({"open": True})

    assert [field for field, _reason in found] == ["open"]
