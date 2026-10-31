"""历史股票池快照。

快照由 ``as_of + data_version_id + rule_version + 规则参数`` 固定：同样的输入必然
得到同样的成员与排除记录；新增未来行情或更晚修订不会改变已生成的快照。

限制必须显式带出：当前在市名单缺少退市证券、历史停牌状态不完整、使用今天修订的
信息，因此**不能**声称幸存者偏差已被消除。规则阈值写入版本化配置，不按结果调优。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .chunks import VersionView
from .digests import sha256_of_object
from .quality import QUALITY_RULE_VERSION
from .store import bump_generation
from .timeutil import utc_now_iso

POOL_RULE_VERSION = "pool-2026-10-22.1"


@contextmanager
def _pool_write(conn):
    """Commit standalone writes, while preserving a caller's outer transaction."""
    conn.execute("SAVEPOINT pool_snapshot_write")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO pool_snapshot_write")
        conn.execute("RELEASE pool_snapshot_write")
        raise
    else:
        conn.execute("RELEASE pool_snapshot_write")

# 排除原因码
REASON_NOT_LISTED_YET = "not_listed_yet"
REASON_DELISTED_BEFORE_AS_OF = "delisted_before_as_of"
REASON_LISTING_STATUS_UNKNOWN = "listing_status_unknown"
REASON_NO_QUOTE = "no_quote_on_as_of"
REASON_STALE_QUOTE = "stale_quote"
REASON_SUSPENDED = "suspended_on_as_of"
REASON_INSUFFICIENT_HISTORY = "insufficient_history"
REASON_BELOW_LIQUIDITY = "below_liquidity_threshold"
REASON_QUALITY_BLOCKED = "quality_issue_blocking"

LIMIT_HISTORICAL_UNIVERSE_INCOMPLETE = "historical_universe_incomplete"
LIMIT_SURVIVORSHIP_BIAS = "survivorship_bias_unresolved"
LIMIT_HISTORICAL_AVAILABILITY = "historical_availability_unproven"
LIMIT_CORPORATE_ACTION_UNKNOWN = "corporate_action_coverage_unknown"
LIMIT_SUSPENSION_UNKNOWN = "suspension_coverage_unknown"


@dataclass(frozen=True)
class PoolRule:
    """股票池规则参数（版本化；示例值不是已批准的生产参数）。"""

    min_history_trade_days: int = 120
    liquidity_window_days: int = 20
    min_avg_amount_cny: float = 50_000_000.0
    require_trading_on_as_of: bool = True
    max_quote_age_trade_days: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> dict[str, Any]:
        return {"rule_version": POOL_RULE_VERSION, **self.as_dict()}


DEFAULT_POOL_RULE = PoolRule()


def _latest_securities(
    conn: sqlite3.Connection, source_code: str, *, view: VersionView | None = None
) -> list[dict[str, Any]]:
    if view is not None:
        return [
            {
                "ts_code": row["ts_code"],
                "name": row["name"],
                "market": row["market"],
                "exchange": row["exchange"],
                "list_date": row["list_date"],
                "delist_date": row["delist_date"],
                "list_status": row["list_status"],
            }
            for row in view.securities()
        ]
    rows = conn.execute(
        "SELECT ts_code, name, market, exchange, list_date, delist_date, list_status FROM security_info"
        " WHERE source_code=? AND id IN ("
        "   SELECT MAX(id) FROM security_info WHERE source_code=? GROUP BY ts_code)",
        (source_code, source_code),
    ).fetchall()
    return [
        {
            "ts_code": row[0],
            "name": row[1],
            "market": row[2],
            "exchange": row[3],
            "list_date": row[4],
            "delist_date": row[5],
            "list_status": row[6],
        }
        for row in rows
    ]


def _quote_history(
    conn: sqlite3.Connection,
    source_code: str,
    ts_code: str,
    as_of: str,
    *,
    view: VersionView | None = None,
) -> list[sqlite3.Row]:
    if view is not None:
        return view.daily_series(ts_code, on_or_before=as_of)
    return conn.execute(
        "SELECT trade_date, amount, volume, close FROM daily_price WHERE source_code=? AND ts_code=?"
        " AND trade_date<=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM daily_price d2"
        "                    WHERE d2.source_code=daily_price.source_code"
        "                      AND d2.ts_code=daily_price.ts_code"
        "                      AND d2.trade_date=daily_price.trade_date)"
        " ORDER BY trade_date",
        (source_code, ts_code, as_of),
    ).fetchall()


def _trading_days(
    conn: sqlite3.Connection, source_code: str, as_of: str, *, view: VersionView | None = None
) -> list[str]:
    if view is not None:
        return view.calendar_days(on_or_before=as_of)
    rows = conn.execute(
        "SELECT cal_date FROM trade_calendar WHERE source_code=? AND is_open=1 AND cal_date<=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM trade_calendar c2"
        "                    WHERE c2.source_code=trade_calendar.source_code"
        "                      AND c2.exchange=trade_calendar.exchange"
        "                      AND c2.cal_date=trade_calendar.cal_date)"
        " ORDER BY cal_date",
        (source_code, as_of),
    ).fetchall()
    return [row[0] for row in rows]


def _suspended_dates(
    conn: sqlite3.Connection,
    source_code: str,
    ts_code: str,
    *,
    view: VersionView | None = None,
) -> set[str]:
    if view is not None:
        return view.suspension_dates(ts_code)
    rows = conn.execute(
        "SELECT trade_date, event_type, detail FROM suspension_event WHERE source_code=? AND ts_code=?"
        " AND revision_no = (SELECT MAX(revision_no) FROM suspension_event e2"
        "                    WHERE e2.source_code=suspension_event.source_code"
        "                      AND e2.ts_code=suspension_event.ts_code"
        "                      AND e2.trade_date=suspension_event.trade_date"
        "                      AND e2.event_type=suspension_event.event_type)",
        (source_code, ts_code),
    ).fetchall()
    return {row[0] for row in rows if row[1] == "S" and not row[2]}


def _blocking_codes(conn: sqlite3.Connection, data_version_id: str | None) -> set[str]:
    """被高严重度质量问题影响的证券（按 scope 中的 ts_code 提取）。"""
    import json

    blocking: set[str] = set()
    if data_version_id:
        rows = conn.execute(
            "SELECT scope FROM quality_issue WHERE severity='high' AND data_version_id=?",
            (data_version_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT scope FROM quality_issue WHERE severity='high'").fetchall()
    for (scope_json,) in rows:
        try:
            scope = json.loads(scope_json)
        except (TypeError, json.JSONDecodeError):
            continue
        code = scope.get("ts_code")
        if code:
            blocking.add(str(code))
    return blocking


def generate_pool_snapshot(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    data_version_id: str,
    source_code: str,
    rule: PoolRule = DEFAULT_POOL_RULE,
    ts_codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """生成（或复用）一个历史股票池快照。"""
    view = (
        VersionView(conn, data_version_id, expected_source_code=source_code)
        if data_version_id
        else None
    )
    securities = _latest_securities(conn, source_code, view=view)
    if ts_codes:
        wanted = set(ts_codes)
        securities = [item for item in securities if item["ts_code"] in wanted]
    trading_days = _trading_days(conn, source_code, as_of, view=view)
    trading_index = {day: index for index, day in enumerate(trading_days)}
    blocking = _blocking_codes(conn, data_version_id)

    members: list[str] = []
    exclusions: list[dict[str, Any]] = []
    has_corporate_actions = False
    has_suspensions = False

    for security in sorted(securities, key=lambda item: item["ts_code"]):
        code = security["ts_code"]
        list_date = security.get("list_date")
        delist_date = security.get("delist_date")
        list_status = security.get("list_status")

        if list_date is None:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_LISTING_STATUS_UNKNOWN,
                    "detail": "缺少上市日期，无法判断当时是否在市",
                }
            )
            continue
        if list_date > as_of:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_NOT_LISTED_YET,
                    "detail": f"上市日期 {list_date} 晚于截止时点 {as_of}",
                }
            )
            continue
        if delist_date and delist_date <= as_of:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_DELISTED_BEFORE_AS_OF,
                    "detail": f"退市日期 {delist_date} 不晚于截止时点 {as_of}",
                }
            )
            continue
        if list_status and list_status not in {"L", "D"}:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_LISTING_STATUS_UNKNOWN,
                    "detail": f"上市状态 {list_status} 不是已确认的 L/D",
                }
            )
            continue

        history = _quote_history(conn, source_code, code, as_of, view=view)
        if not history:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_NO_QUOTE,
                    "detail": f"截止时点 {as_of} 之前没有行情记录",
                }
            )
            continue

        last_trade_date = str(history[-1]["trade_date"])
        suspended = _suspended_dates(conn, source_code, code, view=view)
        if suspended:
            has_suspensions = True
        if last_trade_date in suspended:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_SUSPENDED,
                    "detail": f"最近行情日 {last_trade_date} 为全日停牌",
                }
            )
            continue

        if rule.require_trading_on_as_of:
            last_index = trading_index.get(last_trade_date)
            as_of_index = trading_index.get(as_of)
            if as_of_index is None:
                if last_trade_date != as_of:
                    exclusions.append(
                        {
                            "ts_code": code,
                            "reason_code": REASON_NO_QUOTE,
                            "detail": f"截止时点 {as_of} 不在已核验交易日历内",
                        }
                    )
                    continue
            elif last_index is None or as_of_index - last_index > rule.max_quote_age_trade_days:
                exclusions.append(
                    {
                        "ts_code": code,
                        "reason_code": REASON_STALE_QUOTE,
                        "detail": f"最新行情日 {last_trade_date} 距截止时点过久",
                    }
                )
                continue

        if len(history) < rule.min_history_trade_days:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_INSUFFICIENT_HISTORY,
                    "detail": f"历史长度 {len(history)} 个交易日，不足 {rule.min_history_trade_days}",
                }
            )
            continue

        window = history[-rule.liquidity_window_days :]
        amounts = [row["amount"] for row in window if row["amount"] is not None]
        if len(amounts) < rule.liquidity_window_days:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_INSUFFICIENT_HISTORY,
                    "detail": f"流动性窗口只有 {len(amounts)} 个有效成交额，不足 {rule.liquidity_window_days}",
                }
            )
            continue
        average_amount = sum(amounts) / len(amounts)
        if average_amount < rule.min_avg_amount_cny:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_BELOW_LIQUIDITY,
                    "detail": f"近 {rule.liquidity_window_days} 日平均成交额 {average_amount:.2f} 元低于阈值",
                }
            )
            continue

        if code in blocking:
            exclusions.append(
                {
                    "ts_code": code,
                    "reason_code": REASON_QUALITY_BLOCKED,
                    "detail": "该证券存在高严重度质量问题",
                }
            )
            continue

        members.append(code)

    if view is not None:
        has_corporate_actions = view.corporate_action_count() > 0
    else:
        action_rows = conn.execute(
            "SELECT COUNT(*) FROM corporate_action WHERE source_code=?", (source_code,)
        ).fetchone()
        has_corporate_actions = bool(action_rows and action_rows[0])

    limit_flags = [
        LIMIT_HISTORICAL_UNIVERSE_INCOMPLETE,
        LIMIT_SURVIVORSHIP_BIAS,
        LIMIT_HISTORICAL_AVAILABILITY,
    ]
    if not has_corporate_actions:
        limit_flags.append(LIMIT_CORPORATE_ACTION_UNKNOWN)
    if not has_suspensions:
        limit_flags.append(LIMIT_SUSPENSION_UNKNOWN)

    manifest = sha256_of_object(
        {
            "as_of": as_of,
            "data_version_id": data_version_id,
            "source_code": source_code,
            "rule": rule.fingerprint(),
            "members": members,
            "exclusions": exclusions,
            "limit_flags": sorted(limit_flags),
        }
    )
    snapshot_id = "pool_" + manifest[:16]

    existing = conn.execute(
        "SELECT generated_at, member_count FROM pool_snapshot WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    generated_at = existing[0] if existing else utc_now_iso()
    with _pool_write(conn):
        conn.execute(
            "INSERT OR IGNORE INTO pool_snapshot(snapshot_id, as_of, data_version_id, rule_version,"
            " params, limit_flags, member_count, manifest_sha256, generated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                as_of,
                data_version_id,
                POOL_RULE_VERSION,
                _json(rule.as_dict()),
                _json(sorted(limit_flags)),
                len(members),
                manifest,
                generated_at,
            ),
        )
        conn.execute("DELETE FROM pool_member WHERE snapshot_id=?", (snapshot_id,))
        conn.executemany(
            "INSERT INTO pool_member(snapshot_id, ts_code, seq) VALUES (?, ?, ?)",
            [(snapshot_id, code, index) for index, code in enumerate(members)],
        )
        conn.execute("DELETE FROM pool_exclusion WHERE snapshot_id=?", (snapshot_id,))
        conn.executemany(
            "INSERT INTO pool_exclusion(snapshot_id, ts_code, reason_code, detail, seq)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (snapshot_id, item["ts_code"], item["reason_code"], item["detail"], index)
                for index, item in enumerate(exclusions)
            ],
        )
        # 股票池快照是版本引用，必须让清理预览的代际失效。
        bump_generation(conn)

    return {
        "snapshot_id": snapshot_id,
        "as_of": as_of,
        "data_version_id": data_version_id,
        "rule_version": POOL_RULE_VERSION,
        "quality_rule_version": QUALITY_RULE_VERSION,
        "params": rule.as_dict(),
        "limit_flags": sorted(limit_flags),
        "member_count": len(members),
        "members": members,
        "exclusions": exclusions,
        "manifest_sha256": manifest,
        "generated_at": generated_at,
        "reused": existing is not None,
    }


def load_pool_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any] | None:
    """读取已保存的快照（只读）。"""
    row = conn.execute(
        "SELECT snapshot_id, as_of, data_version_id, rule_version, params, limit_flags, member_count,"
        " manifest_sha256, generated_at FROM pool_snapshot WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    members = [
        item[0]
        for item in conn.execute(
            "SELECT ts_code FROM pool_member WHERE snapshot_id=? ORDER BY seq", (snapshot_id,)
        ).fetchall()
    ]
    exclusions = [
        {"ts_code": item[0], "reason_code": item[1], "detail": item[2]}
        for item in conn.execute(
            "SELECT ts_code, reason_code, detail FROM pool_exclusion WHERE snapshot_id=? ORDER BY seq",
            (snapshot_id,),
        ).fetchall()
    ]
    import json

    return {
        "snapshot_id": row[0],
        "as_of": row[1],
        "data_version_id": row[2],
        "rule_version": row[3],
        "params": json.loads(row[4]),
        "limit_flags": json.loads(row[5]),
        "member_count": row[6],
        "manifest_sha256": row[7],
        "generated_at": row[8],
        "members": members,
        "exclusions": exclusions,
    }


def list_pool_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT snapshot_id, as_of, data_version_id, rule_version, member_count, generated_at"
        " FROM pool_snapshot ORDER BY generated_at, snapshot_id"
    ).fetchall()
    return [
        {
            "snapshot_id": row[0],
            "as_of": row[1],
            "data_version_id": row[2],
            "rule_version": row[3],
            "member_count": row[4],
            "generated_at": row[5],
        }
        for row in rows
    ]


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = [
    "DEFAULT_POOL_RULE",
    "LIMIT_CORPORATE_ACTION_UNKNOWN",
    "LIMIT_HISTORICAL_AVAILABILITY",
    "LIMIT_HISTORICAL_UNIVERSE_INCOMPLETE",
    "LIMIT_SUSPENSION_UNKNOWN",
    "LIMIT_SURVIVORSHIP_BIAS",
    "POOL_RULE_VERSION",
    "PoolRule",
    "REASON_BELOW_LIQUIDITY",
    "REASON_DELISTED_BEFORE_AS_OF",
    "REASON_INSUFFICIENT_HISTORY",
    "REASON_LISTING_STATUS_UNKNOWN",
    "REASON_NO_QUOTE",
    "REASON_NOT_LISTED_YET",
    "REASON_QUALITY_BLOCKED",
    "REASON_STALE_QUOTE",
    "REASON_SUSPENDED",
    "generate_pool_snapshot",
    "list_pool_snapshots",
    "load_pool_snapshot",
]
