"""练习数据源声明：官方文档证据、许可限定与覆盖范围。

数据来源纪律：
- 免费客户端许可不等于行情数据许可；本文件逐字段记录官网证据，
  未核验的资格保持受限（``permission_status="documented"`` 即
  "仅有官方文档宣称，账号实际资格未核验"），不编造资格。
- ``permission_status`` 语义沿用 ``app/data/sources.py``：
  documented / verified_ok / verified_denied / blocked / unknown。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BAOSTOCK_DEF",
    "SYNTHETIC_DEF",
    "SOURCE_LIMITATIONS",
    "source_definitions",
]

BAOSTOCK_DEF: dict[str, Any] = {
    "source_code": "baostock",
    "name": "BaoStock 免费历史行情",
    "needs_credentials": False,
    "permission_status": "documented",
    "license": (
        "客户端 Python 库免费、开源；数据平台自述为“免费、开源的证券数据平台"
        "（无需注册）”（官网 baostock.com）。官网免责声明未提供行情数据的再分发"
        "或商用书面授权条款，且第 3 条明确不保证服务可用性。"
    ),
    "coverage": {
        "daily": (
            "query_history_k_data_plus 日线：date/code/open/high/low/close/preclose"
            "（元，4 位小数）/volume（股）/amount（元）/adjustflag（3=不复权）/turn/"
            "tradestatus（1=正常 0=停牌）/pctChg/isST（0/1）；覆盖 1990-12-19 至今；"
            "停牌日开盘、最高、最低、收盘等于前收盘且成交量为 0。"
        ),
        "calendar": "query_trade_dates：calendar_date/is_trading_day（上交所 1990 起）。",
        "stock_basic": "query_stock_basic：code/code_name/ipoDate/outDate/type/status。",
        "dividend": (
            "query_dividend_data：除权除息/派息/送转计划字段；税后股利与送转说明为"
            "官方原始模糊文本（如“0.18或0.2”“10转3派2元…”），须按原串保存。"
        ),
    },
    "limitations": [
        "客户端开源许可不等于行情数据许可；再分发/商用无书面授权，仅限本项目本地与练习用途。",
        "官网确认存在 IP 级访问限制机制；具体限频阈值未公开，实现上保守（显式触发、小批量、串行）。",
        "复权采用涨跌幅复权算法，未提供 Tushare 式独立 adj_factor 序列；本版只取不复权日线。",
        "分红税后额与送转说明为模糊文本列，不做数值化换算。",
        "账号实际访问资格、历史覆盖完整性与真实限频未核验，保持“文档宣称”状态。",
    ],
    "evidence": {
        "verified_sample": (
            "2026-10-24 以官方 Python 客户端 0.9.4 取真实小样本（sh.600000 日线 "
            "2024-07-01..07-05 五行 + stock_basic + trade_dates + 2017 年分红），"
            "字段契约与官方文档一致；仅作开发核验，不提交样本数据。"
        ),
    },
}

SYNTHETIC_DEF: dict[str, Any] = {
    "source_code": "synthetic",
    "name": "离线合成练习样本",
    "needs_credentials": False,
    "permission_status": "verified_ok",
    "license": "本地确定性生成，无外部许可依赖；所有版本永久标注 synthetic。",
    "coverage": {
        "daily": (
            "确定性伪随机日线（工作日），字段与真实源同一契约；用于离线练习与回归。"
        ),
    },
    "limitations": [
        "合成数据不代表真实行情与真实公司行为，不用于任何真实收益判断。",
    ],
    "evidence": {},
}

SOURCE_LIMITATIONS: list[str] = [
    "本阶段只提供日线行情版本；交易日历校验、复权因子序列与分红写入未启用，"
    "公司行为对账保持受限。",
    "除显式 /data/refresh 外没有任何联网路径；GET 接口与启动流程不取数。",
]

AVAILABLE_SOURCES = ("baostock", "synthetic")


def source_definitions() -> list[dict[str, Any]]:
    """声明全部练习数据源；只读，无网络访问。"""
    defs = [dict(SYNTHETIC_DEF), dict(BAOSTOCK_DEF)]
    baostock_available = _baostock_available()
    for target in defs:
        if target["source_code"] == "baostock":
            target["available"] = baostock_available
            if not baostock_available:
                target["restricted_reason"] = "baostock_not_installed"
    return defs


def _baostock_available() -> bool:
    """检测客户端是否可导入；不登录、不联网。"""
    try:
        import baostock  # noqa: F401 - 仅探测可用性
    except Exception:
        return False
    # A successfully imported module is sufficient evidence of local
    # availability. ``find_spec`` raises ValueError for injected modules with
    # no ``__spec__`` (including lightweight offline test doubles).
    return True
