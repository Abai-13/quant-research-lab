from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP

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


def test_f1_f2_buy_sell_cash_fees_nav_and_return_match_independent_decimal(comparison_session):
    conn, session, days = comparison_session
    segments = initialize_paths(conn, session)
    initial_cash = 1_000_000

    def add_order(order_id, key, trade_date, direction):
        conn.execute(
            "INSERT INTO practice_order(order_id, practice_id, idempotency_key, trade_date,"
            " ts_code, direction, planned_shares, filled_shares, status, detail_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 100, 0, 'pending', '{}', ?)",
            (order_id, session["practice_id"], key, trade_date, "000001.SZ", direction,
             "2024-01-01T00:00:00+00:00"),
        )

    def independent_cash_and_fees():
        rows = conn.execute(
            "SELECT o.direction,f.filled_shares,f.price_cents,f.fee_cents,f.slippage_cents"
            " FROM simulation_fill f JOIN simulation_order o ON o.order_id=f.order_id"
            " WHERE o.segment_id=? ORDER BY f.trade_date,f.fill_seq",
            (segments["user"],),
        ).fetchall()
        cash, fees = initial_cash, 0
        for row in rows:
            quantity, price, fee, slippage = map(int, row[1:])
            notional = int((Decimal(quantity) * Decimal(price)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            ))
            fees += fee + slippage
            cash += -(notional + fee + slippage) if row[0] == "buy" else notional - fee - slippage
        return cash, fees

    add_order("f1-buy", "f1-buy", days[1], "buy")
    advance_paths(conn, session, days[1])
    cash_f1, fees_f1 = independent_cash_and_fees()
    from app.data.chunks import VersionView

    view = VersionView(
        conn, session["data_version_id"], expected_source_code=session["source_code"]
    )
    buy_rows = conn.execute(
        "SELECT f.filled_shares FROM simulation_fill f JOIN simulation_order o ON o.order_id=f.order_id"
        " WHERE o.segment_id=? AND o.direction='buy'", (segments["user"],)
    ).fetchall()
    shares_after_buy = sum(int(row[0]) for row in buy_rows)
    close_f1 = Decimal(str(view.daily_series("000001.SZ", on_or_before=days[1])[-1]["close"]))
    market_value_f1 = int((close_f1 * Decimal(shares_after_buy) * Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ))
    nav_f1 = cash_f1 + market_value_f1
    return_f1 = int(((Decimal(nav_f1) / Decimal(initial_cash) - 1) * Decimal(10_000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ))
    user_f1 = next(path for path in get_comparison(conn, session)["paths"] if path["key"] == "user")
    assert get_simulation_account_state(conn, segments["user"])["cash_cents"] == cash_f1
    assert user_f1["cost_cents"] == fees_f1
    assert user_f1["nav_cents"] == nav_f1
    assert user_f1["return_bps"] == return_f1

    add_order("f2-sell", "f2-sell", days[2], "sell")
    advance_paths(conn, session, days[2])
    cash_f2, fees_f2 = independent_cash_and_fees()
    nav_f2 = cash_f2
    return_f2 = int(((Decimal(nav_f2) / Decimal(initial_cash) - 1) * Decimal(10_000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ))
    user_f2 = next(path for path in get_comparison(conn, session)["paths"] if path["key"] == "user")
    assert cash_f2 > cash_f1
    assert fees_f2 > fees_f1  # sell-side fee schedule includes its configured tax component
    assert get_simulation_account_state(conn, segments["user"])["cash_cents"] == cash_f2
    assert user_f2["cost_cents"] == fees_f2
    assert user_f2["nav_cents"] == nav_f2
    assert user_f2["return_bps"] == return_f2


def test_order_detail_tracks_cumulative_state_and_each_partial_fill_attempt(comparison_session):
    conn, session, days = comparison_session
    segments = initialize_paths(conn, session)
    segment_id = segments["user"]
    # The offline fixture has enough cash for the first 100-share lot, but not
    # the full 200-share order. A later synthetic cash injection funds attempt 2.
    conn.execute(
        "UPDATE simulation_account_state SET cash_cents=110500 WHERE segment_id=?",
        (segment_id,),
    )
    conn.execute(
        "INSERT INTO practice_order(order_id, practice_id, idempotency_key, trade_date,"
        " ts_code, direction, planned_shares, filled_shares, status, detail_json, created_at)"
        " VALUES ('po-partial', ?, 'partial-attempts', ?, '000001.SZ', 'buy', 200, 0,"
        " 'pending', ?, '2024-01-01T00:00:00+00:00')",
        (session["practice_id"], days[1], json.dumps({
            "status": "pending", "filled_shares": 0, "remaining_shares": 200,
            "model_price_cents": None, "fee_estimate_cents": None,
        })),
    )

    advance_paths(conn, session, days[1])
    order = conn.execute(
        "SELECT order_id, filled_shares, status, detail_json FROM practice_order"
        " WHERE order_id='po-partial'"
    ).fetchone()
    detail = json.loads(order[3])
    simulation_order_id = conn.execute(
        "SELECT order_id FROM simulation_order WHERE idempotency_key='partial-attempts'"
    ).fetchone()[0]
    engine = conn.execute(
        "SELECT status,filled_shares,remaining_shares,model_price_cents,fee_estimate_cents"
        " FROM simulation_order WHERE idempotency_key='partial-attempts'"
    ).fetchone()
    assert (int(order[1]), order[2]) == (100, "partially_filled")
    assert (detail["filled_shares"], detail["remaining_shares"]) == (int(engine[1]), int(engine[2]))
    assert detail["model_price_cents"] == int(engine[3])
    assert detail["fee_estimate_cents"] == int(engine[4])
    assert detail["cash_flow_scope"] == "latest_attempt"
    assert len(detail["attempts"]) == 1
    first_attempt = detail["attempts"][0]
    assert first_attempt["trade_date"] == days[1]
    first_cash = conn.execute(
        "SELECT SUM(f.filled_shares*f.price_cents+f.fee_cents+f.slippage_cents)"
        " FROM simulation_fill f WHERE f.order_id=? AND f.trade_date=?",
        (simulation_order_id, days[1]),
    ).fetchone()[0]
    assert first_attempt["cash_spent_cents"] == first_cash == 110_500
    assert detail["cash_spent_total_cents"] == first_cash

    conn.execute(
        "UPDATE simulation_account_state SET cash_cents=130000 WHERE segment_id=?",
        (segment_id,),
    )
    advance_paths(conn, session, days[2])
    order = conn.execute(
        "SELECT order_id, filled_shares, status, detail_json FROM practice_order"
        " WHERE order_id='po-partial'"
    ).fetchone()
    detail = json.loads(order[3])
    engine = conn.execute(
        "SELECT status,filled_shares,remaining_shares,model_price_cents,fee_estimate_cents"
        " FROM simulation_order WHERE idempotency_key='partial-attempts'"
    ).fetchone()
    assert (int(order[1]), order[2]) == (200, "filled")
    assert (detail["status"], detail["filled_shares"], detail["remaining_shares"]) == (
        "filled", int(engine[1]), int(engine[2])
    )
    assert detail["remaining_shares"] == 0
    assert detail["model_price_cents"] == int(engine[3])
    assert detail["fee_estimate_cents"] == int(engine[4])
    assert detail["cash_flow_scope"] == "latest_attempt"
    assert len(detail["attempts"]) == 2
    expected_total = 0
    for attempt in detail["attempts"]:
        attempt_cash = conn.execute(
            "SELECT SUM(f.filled_shares*f.price_cents+f.fee_cents+f.slippage_cents)"
            " FROM simulation_fill f WHERE f.order_id=? AND f.trade_date=?",
            (simulation_order_id, attempt["trade_date"]),
        ).fetchone()[0]
        assert attempt["cash_spent_cents"] == attempt_cash
        expected_total += int(attempt_cash)
    assert detail["cash_spent_total_cents"] == expected_total


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
