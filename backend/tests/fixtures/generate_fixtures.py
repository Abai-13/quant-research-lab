"""生成离线夹具（**合成数据，不是真实行情**）。

夹具只用于自动化测试与本地演示：

- 数值由固定公式生成，不来自任何供应商接口，也不代表任何真实证券；
- 证券代码使用真实代码段以便观察字段映射，但名称一律带“示例”字样；
- 生成的目录包含 ``tushare``（常规样本）、``anomalies``（含质量问题的样本）与
  ``paging``（多页与缺页样本）。

运行方式（会覆盖既有夹具文件）：

    python backend/tests/fixtures/generate_fixtures.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent

EXCHANGE = "SSE"

SECURITIES = [
    {
        "ts_code": "000001.SZ",
        "symbol": "000001",
        "name": "示例银行A",
        "market": "主板",
        "exchange": "SZSE",
        "list_date": "19910403",
        "delist_date": "",
        "list_status": "L",
        "base_price": 10.0,
        "step": 0.11,
        "lots": 2_000_000,
    },
    {
        "ts_code": "600000.SH",
        "symbol": "600000",
        "name": "示例银行B",
        "market": "主板",
        "exchange": "SSE",
        "list_date": "19991110",
        "delist_date": "",
        "list_status": "L",
        "base_price": 20.0,
        "step": 0.13,
        "lots": 3_000_000,
    },
    {
        "ts_code": "000003.SZ",
        "symbol": "000003",
        "name": "示例退市C",
        "market": "主板",
        "exchange": "SZSE",
        "list_date": "19910129",
        "delist_date": "20240315",
        "list_status": "D",
        "base_price": 6.0,
        "step": 0.07,
        "lots": 400_000,
    },
    {
        # 上市日期未知：用于验证“历史状态未知”被排除并保留原因。
        "ts_code": "900001.SZ",
        "symbol": "900001",
        "name": "示例状态未知D",
        "market": "主板",
        "exchange": "SZSE",
        "list_date": "",
        "delist_date": "",
        "list_status": "L",
        "base_price": 5.0,
        "step": 0.05,
        "lots": 100_000,
    },
]

# 合成日历：工作日视为交易日，节假日整段休市。
CLOSED_RANGES = [
    ("20230101", "20230102"),  # 元旦
    ("20230121", "20230127"),  # 春节
    ("20230405", "20230405"),  # 清明
    ("20230501", "20230503"),  # 劳动节
    ("20230622", "20230624"),  # 端午
    ("20240101", "20240101"),
    ("20240212", "20240216"),
    ("20240404", "20240406"),
    ("20240501", "20240505"),
    ("20240610", "20240610"),
]

# 行情数据起点（交易日历更早，用于覆盖跨年场景）。
CALENDAR_START = date(2022, 12, 26)
DATA_START = date(2023, 1, 3)
CALENDAR_END = date(2024, 6, 28)


def _closed_days() -> set[str]:
    days: set[str] = set()
    for start, end in CLOSED_RANGES:
        current = date(int(start[:4]), int(start[4:6]), int(start[6:]))
        last = date(int(end[:4]), int(end[4:6]), int(end[6:]))
        while current <= last:
            days.add(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
    return days


def trading_days(start: date, end: date) -> list[str]:
    closed = _closed_days()
    days: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current.strftime("%Y%m%d") not in closed:
            days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return days


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def daily_rows(security: dict, days: list[str]) -> list[list]:
    """生成未复权日线；成交量单位为手，成交额单位为千元。"""
    fields = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"]
    rows: list[list] = []
    previous_close = security["base_price"]
    for index, trade_date in enumerate(days):
        if security["delist_date"] and trade_date > security["delist_date"]:
            continue
        drift = ((index * 7 + len(security["ts_code"])) % 11 - 5) * security["step"]
        close = round(security["base_price"] + drift, 2)
        open_ = round(previous_close, 2)
        high = round(max(open_, close) + 0.05, 2)
        low = round(min(open_, close) - 0.05, 2)
        lots = security["lots"] + ((index * 13) % 7) * 10_000
        amount_thousand = round(lots * close / 10, 3)
        rows.append(
            [
                security["ts_code"],
                trade_date,
                open_,
                high,
                low,
                close,
                previous_close,
                lots,
                amount_thousand,
            ]
        )
        previous_close = close
    return [fields] + rows


def build_main_fixtures() -> None:
    days = trading_days(DATA_START, CALENDAR_END)
    calendar_days = trading_days(CALENDAR_START, CALENDAR_END)

    cal_fields = ["exchange", "cal_date", "is_open", "pretrade_date"]
    cal_items = []
    previous_open: str | None = None
    for day in calendar_days:
        cal_items.append([EXCHANGE, day, "1", previous_open or ""])
        previous_open = day
    # 补充休市日，保证日历覆盖连续区间。
    all_days: list[str] = []
    current = CALENDAR_START
    while current <= CALENDAR_END:
        all_days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    closed = _closed_days()
    for day in all_days:
        if day in closed:
            cal_items.append([EXCHANGE, day, "0", previous_open or ""])
    cal_items.sort(key=lambda item: item[1])
    write_json(
        HERE / "tushare" / "trade_cal.json",
        {
            "interface": "trade_cal",
            "max_rows": 6000,
            "responses": [
                {
                    "match": {"exchange": EXCHANGE},
                    "pages": [{"fields": cal_fields, "items": cal_items, "total": len(cal_items)}],
                }
            ],
        },
    )

    basic_fields = [
        "ts_code",
        "symbol",
        "name",
        "market",
        "exchange",
        "list_date",
        "delist_date",
        "list_status",
    ]
    listed = [
        [
            item["ts_code"],
            item["symbol"],
            item["name"],
            item["market"],
            item["exchange"],
            item["list_date"],
            item["delist_date"],
            item["list_status"],
        ]
        for item in SECURITIES
        if item["list_status"] == "L"
    ]
    delisted = [
        [
            item["ts_code"],
            item["symbol"],
            item["name"],
            item["market"],
            item["exchange"],
            item["list_date"],
            item["delist_date"],
            item["list_status"],
        ]
        for item in SECURITIES
        if item["list_status"] == "D"
    ]
    write_json(
        HERE / "tushare" / "stock_basic.json",
        {
            "interface": "stock_basic",
            "max_rows": 6000,
            "responses": [
                {
                    "match": {"list_status": "L"},
                    "pages": [{"fields": basic_fields, "items": listed, "total": len(listed)}],
                },
                {
                    "match": {"list_status": "D"},
                    "pages": [{"fields": basic_fields, "items": delisted, "total": len(delisted)}],
                },
            ],
        },
    )

    daily_responses = []
    factor_responses = []
    factor_fields = ["ts_code", "trade_date", "adj_factor"]
    for security in SECURITIES:
        code = security["ts_code"]
        rows = daily_rows(security, days)
        daily_responses.append(
            {
                "match": {"ts_code": code},
                "pages": [{"fields": rows[0], "items": rows[1:], "total": len(rows) - 1}],
            }
        )
        factor_items = []
        for index, trade_date in enumerate(days):
            if security["delist_date"] and trade_date > security["delist_date"]:
                continue
            factor_items.append([code, trade_date, round(1.0 + index * 0.0005, 6)])
        factor_responses.append(
            {
                "match": {"ts_code": code},
                "pages": [{"fields": factor_fields, "items": factor_items, "total": len(factor_items)}],
            }
        )

    write_json(
        HERE / "tushare" / "daily.json",
        {
            "interface": "daily",
            "max_rows": 6000,
            "responses": daily_responses,
        },
    )
    write_json(
        HERE / "tushare" / "adj_factor.json",
        {
            "interface": "adj_factor",
            "max_rows": 6000,
            "responses": factor_responses,
        },
    )

    suspend_fields = ["ts_code", "trade_date", "suspend_type", "suspend_timing"]
    suspend_items = [["000001.SZ", "20240301", "S", ""]]
    write_json(
        HERE / "tushare" / "suspend_d.json",
        {
            "interface": "suspend_d",
            "max_rows": 5000,
            "responses": [
                {
                    "match": {"ts_code": "000001.SZ"},
                    "pages": [{"fields": suspend_fields, "items": suspend_items, "total": 1}],
                }
            ],
        },
    )

    dividend_fields = [
        "ts_code",
        "end_date",
        "ann_date",
        "div_proc",
        "stk_div",
        "stk_bo_rate",
        "stk_co_rate",
        "cash_div",
        "cash_div_tax",
        "record_date",
        "ex_date",
        "pay_date",
        "div_listdate",
        "base_share",
    ]
    dividend_items = [
        [
            "600000.SH",
            "20231231",
            "20240320",
            "实施",
            0.0,
            0.0,
            0.0,
            0.35,
            0.315,
            "20240620",
            "20240621",
            "20240621",
            "20240621",
            0.0,
        ]
    ]
    write_json(
        HERE / "tushare" / "dividend.json",
        {
            "interface": "dividend",
            "max_rows": 2000,
            "responses": [
                {
                    "match": {"ts_code": "600000.SH"},
                    "pages": [{"fields": dividend_fields, "items": dividend_items, "total": 1}],
                }
            ],
        },
    )


def build_anomaly_fixtures() -> None:
    """含质量问题的样本：缺失、重复、非有限价格、错误 OHLC、负量额、单位混乱等。"""
    cal_fields = ["exchange", "cal_date", "is_open", "pretrade_date"]
    cal_items = [["SSE", "20240102", "1", ""], ["SSE", "20240103", "1", "20240102"]]
    write_json(
        HERE / "anomalies" / "trade_cal.json",
        {
            "interface": "trade_cal",
            "max_rows": 6000,
            "responses": [
                {
                    "match": {"exchange": "SSE"},
                    "pages": [{"fields": cal_fields, "items": cal_items, "total": len(cal_items)}],
                }
            ],
        },
    )

    basic_fields = [
        "ts_code",
        "symbol",
        "name",
        "market",
        "exchange",
        "list_date",
        "delist_date",
        "list_status",
    ]
    basic_items = [
        ["000001.SZ", "000001", "示例银行A", "主板", "SZSE", "19910403", "", "L"],
        ["900002.SZ", "900002", "示例后上市B", "主板", "SZSE", "20240301", "", "L"],
        ["900003.SZ", "900003", "示例已退市C", "主板", "SZSE", "19910129", "20240103", "D"],
    ]
    write_json(
        HERE / "anomalies" / "stock_basic.json",
        {
            "interface": "stock_basic",
            "max_rows": 6000,
            "responses": [
                {
                    "match": {"list_status": "L"},
                    "pages": [
                        {
                            "fields": basic_fields,
                            "items": [row for row in basic_items if row[7] == "L"],
                            "total": 2,
                        }
                    ],
                },
                {
                    "match": {"list_status": "D"},
                    "pages": [
                        {
                            "fields": basic_fields,
                            "items": [row for row in basic_items if row[7] == "D"],
                            "total": 1,
                        }
                    ],
                },
            ],
        },
    )

    fields = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"]
    items = [
        # 正常记录
        ["000001.SZ", "20240102", 10.0, 10.2, 9.9, 10.1, 10.0, 1_000_000, 1_010_000.0],
        # 非有限价格
        ["000001.SZ", "20240103", "nan", 10.2, 9.9, 10.1, 10.1, 1_000_000, 1_010_000.0],
        # 错误 OHLC：最高价低于最低价
        ["000001.SZ", "20240104", 10.0, 9.5, 10.5, 10.1, 10.1, 1_000_000, 1_010_000.0],
        # 负成交量
        ["000001.SZ", "20240105", 10.0, 10.2, 9.9, 10.1, 10.1, -1_000, 1_010_000.0],
        # 单位混乱：成交额与规模明显不符
        ["000001.SZ", "20240108", 10.0, 10.2, 9.9, 10.1, 10.1, 1_000_000, 1.0],
        # 同一逻辑键两套互相冲突的内容
        ["000001.SZ", "20240103", 10.0, 10.2, 9.9, 9.5, 10.1, 1_000_000, 1_010_000.0],
        # 未知证券
        ["900001.SZ", "20240102", 10.0, 10.2, 9.9, 10.1, 10.0, 1_000_000, 1_010_000.0],
        # 上市前记录
        ["900002.SZ", "20231229", 10.0, 10.2, 9.9, 10.1, 10.0, 1_000_000, 1_010_000.0],
        # 退市后记录
        ["900003.SZ", "20240110", 10.0, 10.2, 9.9, 10.1, 10.0, 1_000_000, 1_010_000.0],
        # 非交易日行情（周六）
        ["000001.SZ", "20240106", 10.0, 10.2, 9.9, 10.1, 10.1, 1_000_000, 1_010_000.0],
        # 未来日期
        ["000001.SZ", "20991231", 10.0, 10.2, 9.9, 10.1, 10.1, 1_000_000, 1_010_000.0],
    ]
    write_json(
        HERE / "anomalies" / "daily.json",
        {
            "interface": "daily",
            "max_rows": 6000,
            "responses": [
                {
                    "match": {"ts_code": "000001.SZ"},
                    "pages": [{"fields": fields, "items": items, "total": len(items)}],
                }
            ],
        },
    )


def build_paging_fixtures() -> None:
    """多页与缺页样本。"""
    fields = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"]
    page1 = [
        ["000001.SZ", "20240102", 10.0, 10.2, 9.9, 10.1, 10.0, 1_000_000, 1_010_000.0],
        ["000001.SZ", "20240103", 10.1, 10.3, 10.0, 10.2, 10.1, 1_000_000, 1_020_000.0],
        ["000001.SZ", "20240104", 10.2, 10.4, 10.1, 10.3, 10.2, 1_000_000, 1_030_000.0],
    ]
    page2 = [
        ["000001.SZ", "20240105", 10.3, 10.5, 10.2, 10.4, 10.3, 1_000_000, 1_040_000.0],
        ["000001.SZ", "20240108", 10.4, 10.6, 10.3, 10.5, 10.4, 1_000_000, 1_050_000.0],
    ]
    write_json(
        HERE / "paging" / "daily.json",
        {
            "interface": "daily",
            "max_rows": 3,
            "responses": [
                {
                    "match": {"ts_code": "000001.SZ"},
                    "pages": [
                        {"fields": fields, "items": page1, "total": 5},
                        {"fields": fields, "items": page2, "total": 5},
                    ],
                }
            ],
        },
    )


def build_error_fixtures() -> None:
    """错误与重试样本：限频、权限、契约错误与缺页。"""
    fields = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"]
    ok_rows = [["000001.SZ", "20240102", 10.0, 10.2, 9.9, 10.1, 10.0, 1_000_000, 1_010_000.0]]
    write_json(
        HERE / "errors" / "daily.json",
        {
            "interface": "daily",
            "max_rows": 6000,
            "responses": [
                {
                    "match": {"ts_code": "000001.SZ"},
                    "pages": [
                        {
                            "fields": fields,
                            "items": ok_rows,
                            "total": 1,
                        }
                    ],
                },
                {
                    "match": {"ts_code": "900010.SZ"},
                    "pages": [
                        {"error": {"kind": "rate_limited", "message": "每分钟最多访问该接口 500 次"}}
                    ],
                },
                {
                    "match": {"ts_code": "900011.SZ"},
                    "pages": [{"error": {"kind": "no_permission", "message": "抱歉，您没有访问该接口的权限"}}],
                },
                {
                    "match": {"ts_code": "900012.SZ"},
                    "pages": [{"error": {"kind": "contract_error", "message": "缺少字段 items"}}],
                },
                {
                    "match": {"ts_code": "900013.SZ"},
                    "pages": [{"error": {"kind": "transport_error", "message": "模拟超时"}}],
                },
                {
                    "match": {"ts_code": "900014.SZ"},
                    "pages": [
                        {
                            "fields": fields,
                            "items": ok_rows,
                            "total": 9,
                        }
                    ],
                },
            ],
        },
    )


def main() -> None:
    build_main_fixtures()
    build_anomaly_fixtures()
    build_paging_fixtures()
    build_error_fixtures()
    print(f"夹具已生成: {HERE}")


if __name__ == "__main__":
    main()
