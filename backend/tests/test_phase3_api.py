"""阶段三 S6 最小 API 与隔离离线演示（临时库聚焦测试）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_account_preview_post_and_state(temp_settings: Settings) -> None:
    with _client(temp_settings) as client:
        created = client.post(
            "/api/portfolio/accounts",
            json={"account_id": "acct_api", "display_name": "API 测试"},
        )
        assert created.status_code == 200 and created.json()["ok"] is True
        event = {
            "event_type": "opening",
            "business_date": "20250301",
            "idempotency_key": "api-open-1",
            "payload": {"cash_cents": 100_000, "positions": []},
            "recorded_at": "2025-03-01T01:00:00+00:00",
            "reason": "API 期初",
        }
        preview = client.post("/api/portfolio/accounts/acct_api/events/preview", json=event)
        assert preview.status_code == 200
        assert preview.json()["preview"]["valid"] is True
        posted = client.post(
            "/api/portfolio/accounts/acct_api/events",
            json={**event, "expected_account_revision": 0},
        )
        assert posted.status_code == 200
        assert posted.json()["result"]["revision"] == 1
        state = client.get("/api/portfolio/accounts/acct_api").json()["state"]
        assert state["cash_cents"] == 100_000 and state["positions"] == []
        replay = client.post(
            "/api/portfolio/accounts/acct_api/events",
            json={**event, "expected_account_revision": 1},
        )
        assert replay.status_code == 200 and replay.json()["result"]["replayed"] is True


def test_offline_demo_is_isolated_and_supports_recommendation_and_simulation(
    temp_settings: Settings,
) -> None:
    with _client(temp_settings) as client:
        info = client.get("/api/portfolio/demo/bootstrap").json()
        assert info["ok"] is True and info["synthetic_demo"] is True
        assert info["account_id"] == "demo_actual"
        assert info["source_code"] == "tushare-fixture"
        assert client.get("/api/portfolio/accounts/demo_actual").status_code == 400
        demo_state = client.get("/api/portfolio/accounts/demo_actual?demo=true")
        assert demo_state.status_code == 200
        assert demo_state.json()["state"]["cash_cents"] == 1_000_000
        # 演示库位于独立目录，不写主库。
        assert (temp_settings.runtime_dir / "phase3-demo").is_dir()
        with sqlite3.connect(temp_settings.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM account WHERE account_id='demo_actual'"
            ).fetchone()[0] == 0

        for mode in ("incremental", "independent"):
            body = {
                "demo": True,
                "account_id": "demo_actual",
                "source_code": "tushare-fixture",
                "as_of": info["as_of"],
                "data_version_id": info["version_id"],
                "pool_snapshot_id": info["pool_snapshot_id"],
                "rule_bundle_id": info["parameter_bundle_id"],
                "fee_bundle_id": info["fee_bundle_id"],
                "mode": mode,
            }
            run = client.post("/api/portfolio/recommendation/runs", json=body)
            assert run.status_code == 200, run.text
            payload = run.json()
            assert payload["synthetic_demo"] is True
            assert payload["report"]["mode"] == mode
            assert payload["report"]["items"]
            read = client.get(
                f"/api/portfolio/recommendation/runs/{payload['run_id']}?demo=true"
            )
            assert read.status_code == 200 and read.json()["run"]["effective"] is True
            listed = client.get(
                "/api/portfolio/recommendation/runs?account_id=demo_actual&demo=true"
            )
            assert listed.status_code == 200 and listed.json()["runs"]

        snapshots = client.get(
            "/api/portfolio/accounts/demo_actual/snapshots?demo=true"
        ).json()["snapshots"]
        segment = client.post(
            "/api/portfolio/simulation/segments",
            json={
                "demo": True,
                "source_account_id": "demo_actual",
                "base_account_snapshot_id": snapshots[0]["snapshot_id"],
                "rule_bundle_id": info["simulation_rule_bundle_id"],
                "fee_bundle_id": info["fee_bundle_id"],
                "source_code": "tushare-fixture",
                "data_version_id": info["version_id"],
                "as_of": info["simulation_as_of"],
            },
        )
        assert segment.status_code == 200, segment.text
        segment_id = segment.json()["segment_id"]
        order = client.post(
            f"/api/portfolio/simulation/segments/{segment_id}/orders",
            json={
                "demo": True,
                "ts_code": "000001.SZ",
                "direction": "buy",
                "planned_shares": 100,
                "trade_date": info["simulation_next_trade_date"],
                "idempotency_key": "api-demo-order-1",
            },
        )
        assert order.status_code == 200 and order.json()["status"] == "pending"
        advance = client.post(
            f"/api/portfolio/simulation/segments/{segment_id}/advance",
            json={"demo": True, "on_trade_date": info["simulation_next_trade_date"]},
        )
        assert advance.status_code == 200
        assert advance.json()["processed"][0]["status"] == "filled"
        state = client.get(
            f"/api/portfolio/simulation/segments/{segment_id}/state?demo=true"
        ).json()["state"]
        assert state["cash_cents"] < 1_000_000
        assert state["positions"][0]["shares"] >= 100
        # 实际账户不受模拟影响
        actual = client.get("/api/portfolio/accounts/demo_actual?demo=true").json()["state"]
        assert actual["cash_cents"] == 1_000_000


def test_demo_corporate_action_and_performance(temp_settings: Settings) -> None:
    with _client(temp_settings) as client:
        info = client.get("/api/portfolio/demo/bootstrap").json()
        record = client.post(
            "/api/portfolio/corporate-actions",
            json={
                "demo": True,
                "owner_type": "account",
                "owner_id": "demo_actual",
                "ts_code": "000001.SZ",
                "action_kind": "cash_dividend",
                "record_date": "20250610",
                "ex_date": "20250611",
                "pay_date": "20250612",
                "entitlement_shares": 100,
                "cash_receivable_cents": 800,
                "tax_status": "confirmed",
                "idempotency_key": "api-demo-div-1",
                "data_version_id": info["version_id"],
            },
        )
        assert record.status_code == 200, record.text
        application_id = record.json()["application_id"]
        settled = client.post(
            f"/api/portfolio/corporate-actions/{application_id}/settle",
            json={"demo": True, "as_of": "20250612"},
        )
        assert settled.status_code == 200 and settled.json()["cash_delta_cents"] == 800
        state = client.get("/api/portfolio/accounts/demo_actual?demo=true").json()["state"]
        assert state["cash_cents"] == 1_000_800

        point = client.post(
            "/api/portfolio/performance/points",
            json={
                "demo": True,
                "owner_type": "account",
                "owner_id": "demo_actual",
                "source_code": "tushare-fixture",
                "data_version_id": info["version_id"],
                "as_of": info["as_of"],
            },
        )
        assert point.status_code == 200, point.text
        assert point.json()["point"]["valuation"]["completeness"] == "complete"
        summary = client.get(
            "/api/portfolio/performance/summary?owner_type=account&owner_id=demo_actual&demo=true"
        )
        assert summary.status_code == 200
        assert summary.json()["summary"]["point_count"] >= 1


def test_account_events_offset_paginates_with_total_count(temp_settings: Settings) -> None:
    """offset 必须在 SQL 层分页：limit=2、offset=2 返回第 3 条且 count 为总数 3。"""
    with _client(temp_settings) as client:
        assert client.post(
            "/api/portfolio/accounts",
            json={"account_id": "acct_page", "display_name": "分页测试"},
        ).status_code == 200
        events = [
            {
                "event_type": "opening",
                "business_date": "20250301",
                "idempotency_key": "page-open",
                "payload": {"cash_cents": 0, "positions": []},
                "recorded_at": "2025-03-01T01:00:00+00:00",
            },
            {
                "event_type": "cash_in",
                "business_date": "20250302",
                "idempotency_key": "page-in-1",
                "payload": {"amount_cents": 100},
                "recorded_at": "2025-03-02T01:00:00+00:00",
            },
            {
                "event_type": "cash_in",
                "business_date": "20250303",
                "idempotency_key": "page-in-2",
                "payload": {"amount_cents": 200},
                "recorded_at": "2025-03-03T01:00:00+00:00",
            },
        ]
        for revision, event in enumerate(events):
            response = client.post(
                "/api/portfolio/accounts/acct_page/events",
                json={**event, "expected_account_revision": revision},
            )
            assert response.status_code == 200, response.text

        page = client.get(
            "/api/portfolio/accounts/acct_page/events?limit=2&offset=2"
        )
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["count"] == 3
        assert body["limit"] == 2 and body["offset"] == 2
        assert len(body["events"]) == 1
        assert body["events"][0]["business_date"] == "20250303"
        assert body["events"][0]["idempotency_key"] == "page-in-2"


def test_account_events_cross_page_correction_marks_original_ineffective(
    temp_settings: Settings,
) -> None:
    """更正落在第 2 页时，第 1 页原事件仍必须显示 effective=false。"""
    with _client(temp_settings) as client:
        assert client.post(
            "/api/portfolio/accounts",
            json={"account_id": "acct_page_fix", "display_name": "跨页更正"},
        ).status_code == 200
        opening = {
            "event_type": "opening",
            "business_date": "20250301",
            "idempotency_key": "page-fix-open",
            "payload": {"cash_cents": 1000, "positions": []},
            "recorded_at": "2025-03-01T01:00:00+00:00",
        }
        posted = client.post(
            "/api/portfolio/accounts/acct_page_fix/events",
            json={**opening, "expected_account_revision": 0},
        )
        assert posted.status_code == 200, posted.text
        original_id = posted.json()["result"]["event_id"]
        correction = {
            "event_type": "correction",
            "business_date": "20250301",
            "idempotency_key": "page-fix-correct",
            "payload": {"cash_cents": 2000, "positions": []},
            "supersedes_event_id": original_id,
            "reason": "跨页更正测试",
            "recorded_at": "2025-03-01T02:00:00+00:00",
        }
        corrected = client.post(
            "/api/portfolio/accounts/acct_page_fix/events",
            json={**correction, "expected_account_revision": 1},
        )
        assert corrected.status_code == 200, corrected.text
        correction_id = corrected.json()["result"]["event_id"]

        page1 = client.get(
            "/api/portfolio/accounts/acct_page_fix/events?limit=1&offset=0"
        ).json()
        assert page1["count"] == 2 and len(page1["events"]) == 1
        assert page1["events"][0]["event_id"] == original_id
        assert page1["events"][0]["effective"] is False
        assert page1["events"][0]["superseded_by"] == correction_id

        page2 = client.get(
            "/api/portfolio/accounts/acct_page_fix/events?limit=1&offset=1"
        ).json()
        assert page2["count"] == 2 and len(page2["events"]) == 1
        assert page2["events"][0]["event_id"] == correction_id
        assert page2["events"][0]["effective"] is True
        assert page2["events"][0]["superseded_by"] is None
