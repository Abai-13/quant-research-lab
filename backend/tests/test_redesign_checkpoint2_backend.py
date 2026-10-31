from fastapi.testclient import TestClient

from app.main import create_app
from app.agent_runtime.store import AgentStore
from app.api.agent import _bind_plan
from app.agent_tools.service import capture_context, validate_context
from fastapi import HTTPException
import pytest


def _draft(client, key="plan-create-0001"):
    demo = client.post("/api/practice/demo").json()
    payload = {
        "name": "checkpoint two plan",
        "start_date": demo["demo"]["start_date"],
        "end_date": demo["demo"]["end_date"],
        "cash_cents": 5_000_000,
        "positions": [],
        "data_version_id": demo["demo"]["version_id"],
        "source_code": demo["demo"]["source_code"],
        "settings": {},
    }
    symbol = demo["symbols"][0]["ts_code"]
    response = client.post("/api/practice/plans", json={
        "payload": payload, "symbols": [symbol], "goal": "练习观察", "idempotency_key": key,
    })
    return response, payload, symbol


def test_practice_plan_version_idempotency_and_atomic_confirm(temp_settings):
    with TestClient(create_app(temp_settings)) as client:
        created, payload, symbol = _draft(client)
        assert created.status_code == 200, created.text
        plan = created.json()
        assert plan["version"] == 1 and plan["status"] == "draft"
        assert plan["qualification"]["ready"] is True
        assert client.post("/api/practice/plans", json={
            "payload": payload, "symbols": [symbol], "goal": "练习观察", "idempotency_key": "plan-create-0001",
        }).json() == plan
        changed = client.put(f"/api/practice/plans/{plan['plan_id']}", json={
            "expected_version": 1, "payload": payload, "symbols": [symbol], "goal": "调整目标",
            "idempotency_key": "plan-update-0001",
        })
        assert changed.status_code == 200, changed.text
        assert changed.json()["version"] == 2
        assert client.put(f"/api/practice/plans/{plan['plan_id']}", json={
            "expected_version": 1, "payload": payload, "symbols": [symbol], "goal": "旧版本",
            "idempotency_key": "plan-update-stale",
        }).status_code == 409
        confirmed = client.post(f"/api/practice/plans/{plan['plan_id']}/confirm", json={
            "expected_version": 2, "idempotency_key": "plan-confirm-0001",
        })
        assert confirmed.status_code == 200, confirmed.text
        result = confirmed.json()
        assert result["plan"]["status"] == "confirmed"
        assert result["session"]["settings"]["watchlist_symbols"] == [symbol]
        again = client.post(f"/api/practice/plans/{plan['plan_id']}/confirm", json={
            "expected_version": 2, "idempotency_key": "plan-confirm-0001",
        })
        assert again.json() == result
        assert client.post(f"/api/practice/plans/{plan['plan_id']}/confirm", json={
            "expected_version": 2, "idempotency_key": "plan-confirm-0002",
        }).json()["session"]["practice_id"] == result["session"]["practice_id"]


def test_practice_plan_invalid_date_is_not_ready_and_does_not_create_session(temp_settings):
    with TestClient(create_app(temp_settings)) as client:
        created, payload, symbol = _draft(client)
        plan = created.json()
        bad_payload = dict(payload, start_date="2024-07-01")
        updated = client.put(f"/api/practice/plans/{plan['plan_id']}", json={
            "expected_version": 1, "payload": bad_payload, "symbols": [symbol], "goal": "日期错误",
            "idempotency_key": "plan-invalid-date",
        })
        assert updated.status_code == 200
        assert updated.json()["qualification"]["ready"] is False
        confirmed = client.post(f"/api/practice/plans/{plan['plan_id']}/confirm", json={
            "expected_version": 2, "idempotency_key": "plan-invalid-confirm",
        })
        assert confirmed.status_code == 422
        assert client.get("/api/practice/sessions").json()["total"] == 0


def test_workspace_only_reports_revealed_session_data(temp_settings):
    with TestClient(create_app(temp_settings)) as client:
        plan_response, _, symbol = _draft(client)
        plan = plan_response.json()
        created = client.post(f"/api/practice/plans/{plan['plan_id']}/confirm", json={
            "expected_version": 1, "idempotency_key": "workspace-confirm-0001",
        })
        assert created.status_code == 200, created.text
        practice_id = created.json()["session"]["practice_id"]
        workspace = client.get(f"/api/practice/sessions/{practice_id}/workspace")
        assert workspace.status_code == 200, workspace.text
        body = workspace.json()
        assert body["as_of"] == body["rows"][0]["price_date"]
        assert body["rows"][0]["ts_code"] == symbol
        assert body["rows"][0]["shares"] == 0
        assert body["rows"][0]["last_agent_action"] is None


def test_agent_plan_binding_checks_current_version_and_adds_server_context(temp_settings):
    with TestClient(create_app(temp_settings)) as client:
        created, payload, symbol = _draft(client, "agent-plan-create-0001")
        plan = created.json()
    context = capture_context(temp_settings, None)
    _bind_plan(temp_settings, context, plan["plan_id"], 1, None)
    assert context["plan_id"] == plan["plan_id"]
    assert context["plan_version"] == 1
    assert context["model_context"]["current_plan"]["goal"] == "练习观察"
    assert context["model_context"]["prior_plan_turns"] == []
    validate_context(temp_settings, context)
    with TestClient(create_app(temp_settings)) as client:
        updated = client.put(f"/api/practice/plans/{plan['plan_id']}", json={
            "expected_version": 1, "payload": payload, "symbols": [symbol], "goal": "新版本",
            "idempotency_key": "agent-plan-update-0001",
        })
        assert updated.status_code == 200
    with pytest.raises(Exception, match="方案版本已变化"):
        validate_context(temp_settings, context)
    with pytest.raises(HTTPException) as stale:
        _bind_plan(temp_settings, {}, plan["plan_id"], 1, None)
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException) as cross_practice:
        _bind_plan(temp_settings, {}, plan["plan_id"], 2, "different-practice")
    assert cross_practice.value.status_code == 409


def test_agent_task_store_filters_before_pagination(tmp_path):
    store = AgentStore(tmp_path / "agent" / "tasks.sqlite3")
    store.initialize()
    for index, (scope, plan_id) in enumerate((("setup", "p1"), ("simulation", "p1"),
                                               ("setup", "p2"), ("setup", "p1"))):
        context = {"scope": scope, "practice_id": "practice-a" if scope == "simulation" else None,
                   "context_fingerprint": f"fp-{index}", "plan_id": plan_id, "plan_version": 1,
                   "model_context": {"scope": scope}}
        store.create(key=f"task-idem-{index}", payload={"index": index}, practice_id=context["practice_id"],
                     scope=scope, goal="goal", config_version="cfg", context=context,
                     consent_digest="a" * 64, limits={"tokens": 4096}, plan_id=plan_id)
    rows, total = store.list(None, 1, 0, plan_id="p1", scope="setup")
    assert total == 2 and len(rows) == 1
    rows2, total2 = store.list(None, 10, 1, plan_id="p1", scope="setup")
    assert total2 == 2 and len(rows2) == 1
    simulation, simulation_total = store.list("practice-a", 10, 0, plan_id="p1", scope="simulation")
    assert simulation_total == len(simulation) == 1
