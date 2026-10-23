"""阶段三 S6：完全隔离的离线演示数据引导。

演示数据库固定位于 ``<runtime_dir>/phase3-demo``，与个人主库 `app.db` 分离。
所有数据均为合成假设，响应始终带 ``synthetic_demo=true``；不读取真实行情、
不接真实数据源、不写入用户账户。
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import Settings
from ..data.chunks import publish_chunked_version
from ..data.migration import initialize_database
from ..data.store import write_records
from ..data.universe import PoolRule, generate_pool_snapshot
from .db import portfolio_connect
from .ledger import AccountEventRequest, create_account, post_account_event
from .rule_bundles import create_rule_bundle, ensure_default_bundles

__all__ = ["DEMO_ACCOUNT_ID", "ensure_demo_database"]

DEMO_ACCOUNT_ID = "demo_actual"
DEMO_SOURCE = "tushare-fixture"
DEMO_DAYS = 160
DEMO_END = date(2025, 6, 30)


def _demo_settings(settings: Settings) -> Settings:
    root = settings.runtime_dir / "phase3-demo"
    return dataclasses.replace(
        settings,
        runtime_dir=root,
        db_path=root / "app.db",
        log_dir=root / "logs",
        backup_dir=root / "backups",
        raw_archive_dir=root / "raw",
        lock_dir=root / "locks",
    )


def _business_days(count: int, end: date) -> list[str]:
    days: list[str] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y%m%d"))
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _closes(count: int, start_cents: int, step_cents: int) -> list[float]:
    return [((start_cents + index * step_cents) / 100.0) for index in range(count)]


def _seed(conn, days: list[str]) -> dict[str, Any]:
    plans = {
        "000001.SZ": _closes(len(days), 1000, 3),
        "000002.SZ": _closes(len(days), 800, 2),
        "000003.SZ": _closes(len(days), 1200, 0),
    }
    daily: list[dict[str, Any]] = []
    factors: list[dict[str, Any]] = []
    for code, closes in plans.items():
        for day, close in zip(days, closes):
            daily.append(
                {
                    "ts_code": code,
                    "trade_date": day,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "pre_close": close,
                    "volume": 1_000_000.0,
                    "amount": 10_000_000.0,
                    "currency": "CNY",
                    "source_volume_raw": 1_000_000.0,
                    "source_amount_raw": 10_000_000.0,
                }
            )
            factors.append({"ts_code": code, "trade_date": day, "adj_factor": 1.0})
    calendar = [
        {"ts_code": "", "exchange": "SSE", "cal_date": day, "is_open": 1} for day in days
    ]
    securities = [
        {
            "ts_code": code,
            "symbol": code[:6],
            "name": f"演示{index + 1}",
            "market": "主板",
            "exchange": "SSE",
            "list_date": "20000101",
            "delist_date": None,
            "list_status": "L",
        }
        for index, code in enumerate(sorted(plans))
    ]
    suspension = [
        {"ts_code": "000003.SZ", "trade_date": days[0], "event_type": "R", "detail": None}
    ]
    write_records(conn, "daily", daily, source_code=DEMO_SOURCE)
    write_records(conn, "adj_factor", factors, source_code=DEMO_SOURCE)
    write_records(conn, "calendar", calendar, source_code=DEMO_SOURCE)
    write_records(conn, "security", securities, source_code=DEMO_SOURCE)
    write_records(conn, "suspension", suspension, source_code=DEMO_SOURCE)
    version_id = str(
        publish_chunked_version(
            conn,
            source_code=DEMO_SOURCE,
            scope={},
            categories=["daily", "adj_factor", "calendar", "security", "suspension"],
            rule_version="demo-synthetic",
            quality_summary={},
            window_mode="full",
        )["version_id"]
    )
    pool = generate_pool_snapshot(
        conn,
        as_of=days[-1],
        data_version_id=version_id,
        source_code=DEMO_SOURCE,
        rule=PoolRule(min_avg_amount_cny=0.0, min_history_trade_days=120),
        ts_codes=sorted(plans),
    )
    bundles = ensure_default_bundles(conn)
    demo_parameter = create_rule_bundle(
        conn,
        kind="parameter",
        version="demo-parameter-1",
        assumption_scope="synthetic_simulation",
        content={
            "bundle_kind": "parameter",
            "target_constraints": {
                "cash_reserve_cents": 50_000,
                "per_security_weight_cap_bp": 10_000,
            },
            "execution_assumptions": {
                "same_day_sale_proceeds_reusable": False,
                "verification_status": "unverified",
            },
            "rebalance_buffer": {
                "retain_unselected_holdings": True,
                "min_trade_notional_cents": 0,
            },
            "recommendation_validity": {"default_hours": 24, "max_hours": 168},
        },
    )
    demo_market = create_rule_bundle(
        conn,
        kind="parameter",
        version="demo-market-rules-1",
        assumption_scope="synthetic_simulation",
        content={
            "bundle_kind": "parameter",
            "market_rules": {
                "verification_status": "verified",
                "board_lot_shares": 100,
                "t_plus_one_sell_restriction": False,
                "same_day_sale_proceeds_reusable": False,
                "suspension_evidence": {
                    "verification_status": "verified",
                    "coverage": "full",
                    "source_category": "suspension",
                    "ts_codes": sorted(plans),
                    "start_date": days[0],
                    "end_date": days[-1],
                },
                "price_limit_evidence": {
                    "verification_status": "verified",
                    "coverage": "full",
                    "bounds": [
                        {
                            "ts_code": code,
                            "start_date": days[0],
                            "end_date": days[-1],
                            "lower_cents": 1,
                            "upper_cents": 100_000,
                        }
                        for code in sorted(plans)
                    ],
                },
            },
        },
    )
    create_account(conn, DEMO_ACCOUNT_ID, "离线演示账户（合成）")
    post_account_event(
        conn,
        AccountEventRequest(
            account_id=DEMO_ACCOUNT_ID,
            event_type="opening",
            business_date=days[-20],
            idempotency_key="demo-opening-1",
            payload={
                "cash_cents": 1_000_000,
                "positions": [
                    {
                        "ts_code": "000001.SZ",
                        "shares": 100,
                        "total_cost_cents": 100_000,
                        "sellable_shares": 100,
                        "acquire_date": days[-30],
                        "locked": False,
                    },
                    {
                        "ts_code": "000002.SZ",
                        "shares": 200,
                        "total_cost_cents": 160_000,
                        "sellable_shares": 200,
                        "acquire_date": days[-30],
                        "locked": False,
                    },
                ],
                "note": "离线演示合成期初；不代表真实持仓",
            },
            recorded_at="2025-06-30T01:00:00+00:00",
            reason="synthetic demo opening",
        ),
        expected_account_revision=0,
    )
    metadata = {
        "demo_version_id": version_id,
        "demo_pool_snapshot_id": str(pool["snapshot_id"]),
        "demo_parameter_bundle_id": demo_parameter["bundle_id"],
        "demo_fee_bundle_id": bundles["fee"],
        "demo_sim_rule_bundle_id": demo_market["bundle_id"],
        "demo_as_of": days[-1],
        "demo_sim_as_of": days[-10],
        "demo_sim_next": days[-9],
        "demo_start": days[0],
        "demo_end": days[-1],
    }
    for key, value in metadata.items():
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
            (key, str(value)),
        )
    return {
        "account_id": DEMO_ACCOUNT_ID,
        "source_code": DEMO_SOURCE,
        "version_id": version_id,
        "pool_snapshot_id": str(pool["snapshot_id"]),
        "as_of": days[-1],
        "parameter_bundle_id": demo_parameter["bundle_id"],
        "fee_bundle_id": bundles["fee"],
        "simulation_rule_bundle_id": demo_market["bundle_id"],
        "simulation_as_of": days[-10],
        "simulation_next_trade_date": days[-9],
        "coverage_start": days[0],
        "coverage_end": days[-1],
    }


def ensure_demo_database(settings: Settings) -> dict[str, Any]:
    """创建/复用独立演示库；返回可用于 UI 的合成上下文。"""
    demo = _demo_settings(settings)
    demo.runtime_dir.mkdir(parents=True, exist_ok=True)
    initialize_database(demo)
    conn = portfolio_connect(demo.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM account WHERE account_id=?", (DEMO_ACCOUNT_ID,)
        ).fetchone()
        if row is None:
            with conn:
                info = _seed(conn, _business_days(DEMO_DAYS, DEMO_END))
        else:
            meta = dict(
                conn.execute(
                    "SELECT key, value FROM app_metadata WHERE key LIKE 'demo_%'"
                ).fetchall()
            )
            required = (
                "demo_version_id",
                "demo_pool_snapshot_id",
                "demo_parameter_bundle_id",
                "demo_fee_bundle_id",
                "demo_sim_rule_bundle_id",
                "demo_as_of",
                "demo_sim_as_of",
                "demo_sim_next",
            )
            missing = [key for key in required if not meta.get(key)]
            if missing:
                raise RuntimeError(f"demo metadata incomplete: {missing}")
            info = {
                "account_id": DEMO_ACCOUNT_ID,
                "source_code": DEMO_SOURCE,
                "version_id": str(meta["demo_version_id"]),
                "pool_snapshot_id": str(meta["demo_pool_snapshot_id"]),
                "as_of": str(meta["demo_as_of"]),
                "parameter_bundle_id": str(meta["demo_parameter_bundle_id"]),
                "fee_bundle_id": str(meta["demo_fee_bundle_id"]),
                "simulation_rule_bundle_id": str(meta["demo_sim_rule_bundle_id"]),
                "simulation_as_of": str(meta["demo_sim_as_of"]),
                "simulation_next_trade_date": str(meta["demo_sim_next"]),
                "coverage_start": meta.get("demo_start"),
                "coverage_end": meta.get("demo_end"),
            }
        return {
            **info,
            "db_path": str(demo.db_path),
            "synthetic_demo": True,
            "notice": "离线演示：全部数据为合成假设，不连接真实行情/账户，不进入个人库",
        }
    finally:
        conn.close()
