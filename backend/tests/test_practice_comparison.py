from __future__ import annotations

import pytest

from app.data.errors import DataError
from app.data.migration import initialize_database
from app.portfolio.db import portfolio_connect
from app.portfolio.ledger import AccountEventRequest, create_account, post_account_event
from app.portfolio.simulation import get_simulation_account_state
from app.practice.comparison import advance_paths, get_comparison, initialize_paths
from test_phase3_simulation import _business_days, _fee_bundle, _market_rules_bundle, _seed_version


@pytest.fixture
def comparison_session(data_settings):
    initialize_database(data_settings)
    conn = portfolio_connect(data_settings.db_path)
    days = _business_days(8)
    version = _seed_version(
        conn,
        days=days,
        plans={"000001.SZ": [10.0 + i for i in range(len(days))]},
    )
    create_account(conn, "practice-seed", "练习种子")
    post_account_event(
        conn,
        AccountEventRequest(
            account_id="practice-seed",
            event_type="opening",
            business_date=days[0],
            idempotency_key="practice-seed-opening",
            payload={"cash_cents": 1_000_000, "positions": []},
            recorded_at="2025-01-01T00:00:00+00:00",
        ),
        expected_account_revision=0,
    )
    snapshot_id = conn.execute(
        "SELECT snapshot_id FROM account_snapshot WHERE account_id=?", ("practice-seed",)
    ).fetchone()[0]
    rule_id = _market_rules_bundle(
        conn,
        rules={
            "verification_status": "verified",
            "board_lot_shares": 100,
            "t_plus_one_sell_restriction": False,
            "same_day_sale_proceeds_reusable": False,
            "suspension_evidence": {
                "verification_status": "verified",
                "coverage": "full",
                "source_category": "suspension",
                "ts_codes": ["000001.SZ"],
                "start_date": days[0],
                "end_date": days[-1],
            },
            "price_limit_evidence": {
                "verification_status": "verified",
                "coverage": "full",
                "bounds": [
                    {
                        "ts_code": "000001.SZ",
                        "start_date": days[0],
                        "end_date": days[-1],
                        "lower_cents": 1,
                        "upper_cents": 100_000,
                    }
                ],
            },
        },
    )
    fee_id = _fee_bundle(conn)
    session = {
        "practice_id": "practice-fixture",
        "name": "comparison test",
        "status": "active",
        "revision": 1,
        "start_date": days[0],
        "end_date": days[-1],
        "revealed_date": days[0],
        "data_version_id": version,
        "source_code": "tushare-fixture",
        "seed_snapshot_id": snapshot_id,
        "settings": {
            "simulation_rule_bundle_id": rule_id,
            "fee_bundle_id": fee_id,
            "rebalance_every_n_days": 5,
        },
    }
    conn.execute(
        "CREATE TABLE practice_order (order_id TEXT PRIMARY KEY, practice_id TEXT NOT NULL,"
        " idempotency_key TEXT NOT NULL, trade_date TEXT NOT NULL, ts_code TEXT NOT NULL,"
        " direction TEXT NOT NULL, planned_shares INTEGER NOT NULL, filled_shares INTEGER NOT NULL DEFAULT 0,"
        " status TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL,"
        " UNIQUE(practice_id, idempotency_key))"
    )
    try:
        yield conn, session, days
    finally:
        conn.close()


def test_initialize_creates_four_independent_segments_with_same_seed(comparison_session):
    conn, session, _ = comparison_session
    segments = initialize_paths(conn, session)
    assert set(segments) == {"user", "incremental", "independent", "hold"}
    states = [get_simulation_account_state(conn, segment) for segment in segments.values()]
    assert {state["cash_cents"] for state in states} == {1_000_000}
    assert all(state["positions"] == [] for state in states)
    assert len(set(segments.values())) == 4
    comparison = get_comparison(conn, session)
    assert [path["nav_cents"] for path in comparison["paths"]] == [1_000_000] * 4
    assert [path["cost_cents"] for path in comparison["paths"]] == [0] * 4
    assert [path["trade_count"] for path in comparison["paths"]] == [0] * 4


def test_advance_is_exactly_next_calendar_day_and_repeat_is_rejected(comparison_session):
    conn, session, days = comparison_session
    initialize_paths(conn, session)
    result = advance_paths(conn, session, days[1])
    assert result["revealed_date"] == days[1]
    assert session["revealed_date"] == days[1]
    assert result["limitations"]["incremental"] == [
        "system_rebalance_blocked_missing_versioned_pool_or_parameters"
    ]
    comparison = get_comparison(conn, session)
    hold = next(path for path in comparison["paths"] if path["key"] == "hold")
    assert hold["nav_cents"] == 1_000_000
    assert [point["date"] for point in hold["points"]] == days[:2]
    with pytest.raises(DataError, match="只能推进下一交易日"):
        advance_paths(conn, session, days[1])


def test_user_order_uses_simulation_engine_and_reports_real_fill_and_fee(comparison_session):
    conn, session, days = comparison_session
    initialize_paths(conn, session)
    conn.execute(
        "INSERT INTO practice_order(order_id, practice_id, idempotency_key, trade_date,"
        " ts_code, direction, planned_shares, filled_shares, status, detail_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pending', '{}', ?)",
        (
            "po-1",
            session["practice_id"],
            "user-order-1",
            days[1],
            "000001.SZ",
            "buy",
            100,
            "2024-01-01T00:00:00+00:00",
        ),
    )
    result = advance_paths(conn, session, days[1])
    assert len(result["user_order_engine_ids"]) == 1
    order = conn.execute(
        "SELECT filled_shares, status, detail_json FROM practice_order WHERE order_id='po-1'"
    ).fetchone()
    assert (int(order[0]), str(order[1])) == (100, "filled")
    assert '"status": "filled"' in str(order[2])
    assert '"cash_spent_cents": 110500' in str(order[2])
    user = next(path for path in get_comparison(conn, session)["paths"] if path["key"] == "user")
    assert user["cost_cents"] == 500
    assert user["trade_count"] == 1
    assert user["nav_cents"] == 999_500
