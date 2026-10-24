from __future__ import annotations

import sqlite3
from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.practice.runtime import practice_connect, practice_settings
from app.practice.core import PracticeError, idempotent_response, save_idempotency


def test_practice_options_are_read_only_and_isolated(temp_settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        response = client.get("/api/practice/options")

    assert response.status_code == 200
    assert response.json()["demo_available"] is True
    assert practice_settings(temp_settings).db_path.is_file()
    assert temp_settings.db_path.is_file()
    assert practice_settings(temp_settings).db_path != temp_settings.db_path


def test_create_requires_idempotency_key(temp_settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        response = client.post("/api/practice/sessions", json={})

    assert response.status_code == 400
    assert response.json()["reason"] == "idempotency_key_required"


def test_demo_seed_is_explicit_synthetic_and_available_in_practice_database(temp_settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        response = client.post("/api/practice/demo")

    assert response.status_code == 200
    payload = response.json()
    assert payload["demo"]["synthetic"] is True
    assert payload["demo"]["version_id"] in {v["version_id"] for v in payload["versions"]}
    assert payload["demo"]["source_code"] == "tushare-fixture"
    with sqlite3.connect(practice_settings(temp_settings).db_path) as conn:
        rows = conn.execute(
            "SELECT source_code,scope FROM data_version WHERE version_id=?",
            (payload["demo"]["version_id"],),
        ).fetchone()
    assert rows[0] == "tushare-fixture"
    assert "explicit_practice_synthetic_demo" in rows[1]


def test_practice_data_router_uses_isolated_database_and_options_exposes_demo_version(temp_settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        sources = client.get("/api/practice/data/sources")
        before = client.get("/api/practice/data/versions")
        refresh = client.post(
            "/api/practice/data/refresh",
            json={
                "source_code": "synthetic",
                "symbols": ["600000.SH"],
                "start_date": "2024-07-01",
                "end_date": "2024-07-05",
                "idempotency_key": "integration-synthetic-refresh",
            },
        )
        after = client.get("/api/practice/data/versions")
        demo = client.post("/api/practice/demo")
        options = client.get("/api/practice/options")

    assert sources.status_code == before.status_code == 200
    assert sources.json()["ok"] is True
    assert before.json()["versions"] == []
    assert refresh.status_code == 200
    assert refresh.json()["evidence"]["offline_synthetic"] is True
    assert after.status_code == 200
    assert len(after.json()["versions"]) == 1
    assert demo.status_code == options.status_code == 200
    demo_version_id = demo.json()["demo"]["version_id"]
    option_version_ids = {item["version_id"] for item in options.json()["versions"]}
    assert demo_version_id in option_version_ids
    assert options.json()["defaults"]["data_version_id"] == demo_version_id

    isolated_db = practice_settings(temp_settings).db_path
    assert isolated_db.is_file()
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM practice_data_version"
        ).fetchone()[0] == 1
    with sqlite3.connect(temp_settings.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "practice_data_version" not in tables


def test_practice_api_create_order_advance_decision_finish_flow(temp_settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        demo = client.post("/api/practice/demo").json()
        version = next(
            item for item in demo["versions"]
            if item["version_id"] == demo["demo"]["version_id"]
        )
        created = client.post(
            "/api/practice/sessions",
            json={
                "name": "integration API flow",
                "start_date": demo["demo"]["start_date"],
                "end_date": demo["demo"]["end_date"],
                "cash_cents": 100_000_000,
                "positions": [],
                "data_version_id": version["version_id"],
                "source_code": version["source_code"],
                "settings": {"hide_system_hints": False},
                "idempotency_key": "integration-create-session",
            },
        )
        assert created.status_code == 200, created.text
        session = created.json()["session"]
        practice_id = session["practice_id"]
        code = demo["symbols"][0]["ts_code"]
        preview = client.post(
            f"/api/practice/sessions/{practice_id}/orders/preview",
            json={"ts_code": code, "direction": "buy", "planned_shares": 100},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["preview"]["writes_order"] is False
        order = client.post(
            f"/api/practice/sessions/{practice_id}/orders",
            json={
                "ts_code": code,
                "direction": "buy",
                "planned_shares": 100,
                "idempotency_key": "integration-place-order",
                "expected_revision": session["revision"],
            },
        )
        assert order.status_code == 200, order.text
        session = order.json()["session"]
        before_advance = session["revealed_date"]
        advanced = client.post(
            f"/api/practice/sessions/{practice_id}/advance",
            json={
                "idempotency_key": "integration-advance",
                "expected_revision": session["revision"],
            },
        )
        assert advanced.status_code == 200, advanced.text
        session = advanced.json()["session"]
        assert session["revealed_date"] > before_advance
        assert session["user_state"]["orders"]
        decision = client.post(
            f"/api/practice/sessions/{practice_id}/decisions",
            json={
                "judgment": "继续观察",
                "evidence": "只使用已揭晓行情",
                "exit_condition": "观点失效",
                "idempotency_key": "integration-decision",
            },
        )
        assert decision.status_code == 200, decision.text
        refreshed = client.get(f"/api/practice/sessions/{practice_id}")
        assert refreshed.status_code == 200
        session = refreshed.json()["session"]
        finished = client.post(
            f"/api/practice/sessions/{practice_id}/finish",
            json={
                "idempotency_key": "integration-finish",
                "expected_revision": session["revision"],
            },
        )
        assert finished.status_code == 200, finished.text
        assert finished.json()["session"]["status"] == "finished"
        report = client.get(f"/api/practice/sessions/{practice_id}/report")
        assert report.status_code == 200
        assert report.json()["report"]["decisions"]

    with sqlite3.connect(practice_settings(temp_settings).db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM account WHERE kind='actual'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM account WHERE kind='simulation'"
        ).fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM simulation_segment").fetchone()[0] == 4
    with sqlite3.connect(temp_settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0


def test_idempotency_replays_same_request_and_rejects_key_reuse(temp_settings) -> None:
    with practice_connect(temp_settings) as conn:
        conn.execute(
            "INSERT INTO practice_session(practice_id,name,status,revision,start_date,end_date,revealed_date,"
            "data_version_id,source_code,segments_json,settings_json,user_state_json,created_at,updated_at)"
            " VALUES('idempotent','test','active',1,'20260101','20260102','20260101','v1','fixture','{}','{}','{}','now','now')"
        )
        replay, request_hash = idempotent_response(
            conn, operation="advance", key="key-1", request={"expected_revision": 1}
        )
        assert replay is None
        save_idempotency(conn, operation="advance", key="key-1", request_hash=request_hash,
                         practice_id="idempotent", response={"ok": True, "revision": 2})
        replay, _ = idempotent_response(
            conn, operation="advance", key="key-1", request={"expected_revision": 1}
        )
        assert replay == {"ok": True, "revision": 2}
        with pytest.raises(PracticeError) as error:
            idempotent_response(conn, operation="advance", key="key-1", request={"expected_revision": 2})
        assert error.value.status_code == 409
