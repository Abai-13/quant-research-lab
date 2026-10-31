from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.portfolio.simulation import get_simulation_account_state
from app.practice.runtime import practice_connect, practice_settings


def test_builtin_demo_binds_synthetic_market_rules_and_fills_once(temp_settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        demo_response = client.post("/api/practice/demo")
        assert demo_response.status_code == 200, demo_response.text
        demo = demo_response.json()
        source = demo["demo"]
        version = next(item for item in demo["versions"] if item["version_id"] == source["version_id"])
        assert source["start_date"] <= "20200630"
        assert source["end_date"] == "20250630"
        code = demo["symbols"][0]["ts_code"]
        created_response = client.post(
            "/api/practice/sessions",
            json={
                "name": "phase7 built-in synthetic fill",
                "start_date": source["start_date"],
                "end_date": source["end_date"],
                "cash_cents": 100_000_000,
                "positions": [],
                "data_version_id": version["version_id"],
                "source_code": version["source_code"],
                "settings": {"fee_bundle_id": demo["defaults"]["fee_bundle_id"]},
                "idempotency_key": "phase7-synthetic-demo-create",
            },
        )
        assert created_response.status_code == 200, created_response.text
        session = created_response.json()["session"]
        assert session["source"]["synthetic"] is True
        assert session["settings"]["rule_bundle_id"] == demo["defaults"]["rule_bundle_id"]
        practice_id = session["practice_id"]

        order_payload = {
            "ts_code": code,
            "direction": "buy",
            "planned_shares": 100,
            "idempotency_key": "phase7-synthetic-demo-order",
            "expected_revision": session["revision"],
        }
        first_order = client.post(f"/api/practice/sessions/{practice_id}/orders", json=order_payload)
        replay_order = client.post(f"/api/practice/sessions/{practice_id}/orders", json=order_payload)
        assert first_order.status_code == replay_order.status_code == 200
        assert first_order.json() == replay_order.json()
        session = first_order.json()["session"]

        advance_payload = {
            "idempotency_key": "phase7-synthetic-demo-advance",
            "expected_revision": session["revision"],
        }
        first_advance = client.post(f"/api/practice/sessions/{practice_id}/advance", json=advance_payload)
        replay_advance = client.post(f"/api/practice/sessions/{practice_id}/advance", json=advance_payload)
        assert first_advance.status_code == replay_advance.status_code == 200
        assert first_advance.json() == replay_advance.json()
        final_session = first_advance.json()["session"]
        order = final_session["user_state"]["orders"][0]
        assert order["filled_shares"] == 100
        position = next(item for item in final_session["user_state"]["positions"] if item["ts_code"] == code)
        assert position["shares"] == 100
        conn = practice_connect(practice_settings(temp_settings))
        try:
            row = conn.execute(
                "SELECT segments_json FROM practice_session WHERE practice_id=?", (practice_id,)
            ).fetchone()
            segment_id = json.loads(row[0])["user"]
            fills = conn.execute(
                "SELECT filled_shares,price_cents,fee_cents,slippage_cents FROM simulation_fill"
                " WHERE order_id IN (SELECT order_id FROM simulation_order WHERE segment_id=?)",
                (segment_id,),
            ).fetchall()
            assert len(fills) == 1
            fill_shares, price_cents, fee_cents, slippage_cents = map(int, fills[0])
            assert fee_cents > 0
            gross_cash_spent = fill_shares * price_cents + fee_cents + slippage_cents
            expected_cash = 100_000_000 - gross_cash_spent
            assert final_session["user_state"]["cash_cents"] == expected_cash
            assert get_simulation_account_state(conn, segment_id)["cash_cents"] == expected_cash
            update = final_session["user_state"]["last_advance"]["user_order_updates"][0]
            assert update["filled_shares"] == fill_shares == position["shares"]
            assert update["detail"]["cash_spent_cents"] == gross_cash_spent
        finally:
            conn.close()


def test_demo_rule_bundle_is_bound_to_exact_fixture_source(temp_settings) -> None:
    from app.api.practice import _explicit_demo_rule_bundle

    with TestClient(create_app(temp_settings)) as client:
        demo_response = client.post("/api/practice/demo")
        assert demo_response.status_code == 200, demo_response.text
        version_id = demo_response.json()["demo"]["version_id"]
    conn = practice_connect(practice_settings(temp_settings))
    try:
        scope = conn.execute("SELECT scope FROM data_version WHERE version_id=?", (version_id,)).fetchone()[0]
        assert json.loads(scope)["kind"] == "explicit_practice_synthetic_demo"
        assert _explicit_demo_rule_bundle(conn, version_id, "tushare-fixture") is not None
        # A source mismatch cannot reuse the built-in demo's synthetic evidence.
        assert _explicit_demo_rule_bundle(conn, version_id, "synthetic") is None
    finally:
        conn.close()
