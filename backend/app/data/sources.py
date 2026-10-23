"""来源能力注册与核验记录。

每个接口保存一行核验记录：字段映射、单位、权限状态、覆盖、限频、费用与证据。
``permission_status`` 只取值：unknown / documented / verified_ok / verified_denied / blocked。
文档宣称支持与当前账号实际成功分列；没有账号验证的项保持 unknown。
本注册表是只读种子：生成于 2026-10-21 对 Tushare 官方文档逐页核对；账号核验缺凭据故 BLOCKED。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class InterfaceSpec:
    source_code: str
    interface: str
    canonical_fields: tuple[str, ...]
    field_mapping: tuple[tuple[str, str], ...]
    unit_notes: tuple[tuple[str, str], ...]
    permission_status: str
    permission_notes: str
    rate_limit: str
    cost_notes: str
    coverage_notes: str
    evidence_date: str
    docs_url: str
    unknown_fields: tuple[str, ...]


TUSHARE_DOC = "https://tushare.pro/document/2"

_SPEC_LIST: tuple[InterfaceSpec, ...] = (
    InterfaceSpec(
        source_code="tushare",
        interface="trade_cal",
        canonical_fields=("exchange", "cal_date", "is_open", "pretrade_date"),
        field_mapping=(("cal_date", "cal_date"), ("is_open_text", "is_open"), ("pretrade_date", "pretrade_date")),
        unit_notes=(("is_open", "供应商返回 '0'休市 '1'交易，规范化为整数 0/1"),),
        permission_status="documented",
        permission_notes="官方文档注明需 2000 积分；当前环境无凭据，账号实际访问未核验",
        rate_limit="官方文档未给出本接口单独限频，未核验 (unknown)",
        cost_notes="需至少 2000 积分（付费积分类档位）",
        coverage_notes="交易所交易日历（默认上交所）；文档说明各大交易所与上交所一致，北交所参考沪深",
        evidence_date="2026-10-21",
        docs_url="https://tushare.pro/document/2?doc_id=26",
        unknown_fields=("账号实际可访问性", "实际单次返回行数上限"),
    ),
    InterfaceSpec(
        source_code="tushare",
        interface="stock_basic",
        canonical_fields=("ts_code", "symbol", "name", "market", "exchange", "list_date", "delist_date", "list_status"),
        field_mapping=(("ts_code", "ts_code"),),
        unit_notes=(
            ("list_status", "L上市 D退市 P暂停上市 G过会未交易 UN未上市；默认查询仅返回 L，历史全集必须显式查询 D/P"),
            ("list_date/delist_date", "YYYYMMDD 日期文本；缺失为空"),
        ),
        permission_status="documented",
        permission_notes="官方文档注明 2000 积分起、每分钟 50 次；账号未核验",
        rate_limit="每分钟 50 次（文档宣称，未核验）",
        cost_notes="2000 积分起（付费档位）",
        coverage_notes="每次最多 6000 行，覆盖当前正常上市 A 股；默认 L 状态；退市需显式查询 D。当前在市名单不能充当历史证券全集",
        evidence_date="2026-10-21",
        docs_url="https://tushare.pro/document/2?doc_id=25",
        unknown_fields=("账号实际可访问性", "delist_date 完整性（是否存在缺失）"),
    ),
    InterfaceSpec(
        source_code="tushare",
        interface="daily",
        canonical_fields=("ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "volume", "amount"),
        field_mapping=(("ts_code", "ts_code"), ("trade_date", "trade_date"), ("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"), ("pre_close", "pre_close"), ("volume", "vol"), ("amount", "amount")),
        unit_notes=(
            ("open/high/low/close/pre_close", "人民币元，未复权（供应商口径）"),
            ("vol", "供应商单位为手，规范化为股：×100"),
            ("amount", "供应商单位为千元，规范化为元：×1000"),
            ("pre_close", "昨收价【除权价】，供应商口径"),
        ),
        permission_status="documented",
        permission_notes="基础积分每分钟 500 次、单次 6000 条（文档宣称）；账号未核验",
        rate_limit="每分钟 500 次、单次 6000 条（文档宣称，未核验）",
        cost_notes="基础积分（付费档位）",
        coverage_notes="当日行情 15~16 点入库；停牌期间不提供数据；官方建议按交易日循环取全市场而不是循环 ts_code",
        evidence_date="2026-10-21",
        docs_url="https://tushare.pro/document/2?doc_id=27",
        unknown_fields=("账号实际可访问性", "盘后 fixed auction 字段（阶段 2 不使用）"),
    ),
    InterfaceSpec(
        source_code="tushare",
        interface="adj_factor",
        canonical_fields=("ts_code", "trade_date", "adj_factor"),
        field_mapping=(("ts_code", "ts_code"), ("trade_date", "trade_date"), ("adj_factor", "adj_factor")),
        unit_notes=(("adj_factor", "供应商口径复权因子（Tushare 生产），无货币单位"),),
        permission_status="documented",
        permission_notes="2000 积分起，5000 以上可高频（文档宣称）；账号未核验",
        rate_limit="5000 积分以上可高频（文档宣称，未核验）",
        cost_notes="2000 积分起（付费档位）",
        coverage_notes="当日因子在盘前 9:15~9:20 完成入库；当日因子在当日开盘前不可用，信号计算不得把当日因子用于当日之前的价格调整",
        evidence_date="2026-10-21",
        docs_url="https://tushare.pro/document/2?doc_id=28",
        unknown_fields=("账号实际可访问性",),
    ),
    InterfaceSpec(
        source_code="tushare",
        interface="suspend_d",
        canonical_fields=("ts_code", "trade_date", "event_type", "detail"),
        field_mapping=(("ts_code", "ts_code"), ("trade_date", "trade_date"), ("event_type", "suspend_type"), ("detail", "suspend_timing")),
        unit_notes=(("event_type", "S-停牌 R-复牌，规范化为 S/R；suspend_timing 非空代表日内有限停牌"),),
        permission_status="documented",
        permission_notes="2000 积分，5000 积分可获更高频次（文档宣称）；账号未核验",
        rate_limit="5000 积分以上更高频次（文档宣称，精确限频未核验 unknown）",
        cost_notes="2000 积分起（付费档位）",
        coverage_notes="数据从 2000-01-03 起，单次 5000 行；更新时间不定期，无法证明历史发布时刻；日内停牌（suspend_timing 非空）并非全日停牌",
        evidence_date="2026-10-21",
        docs_url="https://tushare.pro/document/2?doc_id=214",
        unknown_fields=("账号实际可访问性", "更新时间不定期导致的历史可用时刻"),
    ),
    InterfaceSpec(
        source_code="tushare",
        interface="dividend",
        canonical_fields=("ts_code", "end_date", "ann_date", "div_proc", "cash_div", "cash_div_tax", "stk_div", "record_date", "ex_date", "pay_date", "div_listdate"),
        field_mapping=(("ts_code", "ts_code"), ("end_date", "end_date"), ("ann_date", "ann_date"), ("div_proc", "div_proc"), ("cash_div", "cash_div"), ("cash_div_tax", "cash_div_tax"), ("stk_div", "stk_div"), ("record_date", "record_date"), ("ex_date", "ex_date"), ("pay_date", "pay_date"), ("div_listdate", "div_listdate")),
        unit_notes=(
            ("cash_div/cash_div_tax", "每股分红（税后/税前），人民币元"),
            ("stk_div", "每股送转（股），含送股与转增"),
            ("div_proc", "预案/实施进度；只有『实施』记录是已生效事件，未生效的公告不得用于历史账务"),
        ),
        permission_status="documented",
        permission_notes="至少 2000 积分（文档宣称）；账号未核验",
        rate_limit="文档未注明单独限频 (unknown)",
        cost_notes="2000 积分起（付费档位）",
        coverage_notes="数据从 2000-01-01 起，单次 2000 行；每日 20~21 点更新；查询参数至少填一个",
        evidence_date="2026-10-21",
        docs_url="https://tushare.pro/document/2?doc_id=103",
        unknown_fields=("账号实际可访问性", "预案被取消后的记录口径"),
    ),
    InterfaceSpec(
        source_code="tushare",
        interface="bak_basic",
        canonical_fields=("trade_date", "ts_code", "list_status"),
        field_mapping=(("trade_date", "trade_date"), ("ts_code", "ts_code")),
        unit_notes=(("trade_date", "每个交易日的历史基础列表"),),
        permission_status="blocked",
        permission_notes="正式权限需要 5000 积分，超出当前 2000 档位评估范围，本阶段不启用",
        rate_limit="unknown",
        cost_notes="需要 5000 积分（更高付费档位）",
        coverage_notes="2016 年起的历史每日股票列表；是重放历史在市状态较好的候选，但需要额外积分，未申请、未核验",
        evidence_date="2026-10-21",
        docs_url="https://tushare.pro/document/2?doc_id=262",
        unknown_fields=("5000 积分账号下字段与覆盖",),
    ),
)

FIXTURE_SPEC_LIST: tuple[InterfaceSpec, ...] = tuple(
    InterfaceSpec(
        source_code="tushare-fixture",
        interface=spec.interface,
        canonical_fields=spec.canonical_fields,
        field_mapping=spec.field_mapping,
        unit_notes=spec.unit_notes,
        permission_status="verified_ok",
        permission_notes="离线夹具：仅用于测试与演示，绝不冒充真实行情",
        rate_limit="无网络调用，间隔 0",
        cost_notes="无成本",
        coverage_notes="夹具样本：3 只证券与有限日期区间；覆盖区间以夹具内容为准",
        evidence_date="2026-10-21",
        docs_url="backend/tests/fixtures/README.md",
        unknown_fields=(),
    )
    for spec in _SPEC_LIST
    if spec.interface in {"trade_cal", "stock_basic", "daily", "adj_factor", "suspend_d", "dividend"}
)


def interface_specs(source_code: str) -> tuple[InterfaceSpec, ...]:
    if source_code == "tushare-fixture":
        return FIXTURE_SPEC_LIST
    if source_code == "tushare":
        return _SPEC_LIST
    return ()


def get_interface_spec(source_code: str, interface: str) -> InterfaceSpec:
    for spec in interface_specs(source_code):
        if spec.interface == interface:
            return spec
    raise KeyError(f"来源 {source_code} 未注册接口 {interface}")


def required_canonical_fields(source_code: str, interface: str) -> tuple[str, ...]:
    return get_interface_spec(source_code, interface).canonical_fields


def sync_source_registry(conn: sqlite3.Connection, source_code: str) -> int:
    """把来源能力矩阵写入 ``source_registry``（幂等，按主键覆盖）。

    只写核验记录本身，不写入任何凭据或响应内容。
    """
    specs = interface_specs(source_code)
    rows = [
        (
            spec.source_code,
            spec.interface,
            "\n".join(f"{src}->{dst}" for src, dst in spec.field_mapping),
            "\n".join(f"{name}: {note}" for name, note in spec.unit_notes),
            spec.permission_status,
            spec.coverage_notes,
            spec.rate_limit,
            spec.cost_notes,
            spec.evidence_date,
            spec.docs_url,
            "\n".join(spec.unknown_fields),
        )
        for spec in specs
    ]
    with conn:
        conn.executemany(
            "INSERT INTO source_registry(source_code, interface, field_mapping, unit_notes,"
            " permission_status, coverage_notes, rate_limit, cost_notes, evidence_date, docs_url,"
            " unknown_fields) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_code, interface) DO UPDATE SET"
            " field_mapping=excluded.field_mapping, unit_notes=excluded.unit_notes,"
            " permission_status=excluded.permission_status, coverage_notes=excluded.coverage_notes,"
            " rate_limit=excluded.rate_limit, cost_notes=excluded.cost_notes,"
            " evidence_date=excluded.evidence_date, docs_url=excluded.docs_url,"
            " unknown_fields=excluded.unknown_fields",
            rows,
        )
    return len(rows)


def registry_rows(conn: sqlite3.Connection, source_code: str) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT interface, permission_status, rate_limit, cost_notes, coverage_notes,"
        " evidence_date, docs_url, unknown_fields, unit_notes, field_mapping"
        " FROM source_registry WHERE source_code=? ORDER BY interface",
        (source_code,),
    ).fetchall()
    return [
        {
            "interface": row[0],
            "permission_status": row[1],
            "rate_limit": row[2],
            "cost_notes": row[3],
            "coverage_notes": row[4],
            "evidence_date": row[5],
            "docs_url": row[6],
            "unknown_fields": row[7],
            "unit_notes": row[8],
            "field_mapping": row[9],
        }
        for row in rows
    ]

