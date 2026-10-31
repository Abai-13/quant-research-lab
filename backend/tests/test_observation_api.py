from fastapi.testclient import TestClient
from app.main import create_app


def test_observation_authorization_persists_and_is_idempotent(temp_settings):
    with TestClient(create_app(temp_settings)) as client:
        demo = client.post("/api/practice/demo").json()
        symbol = demo["symbols"][0]["ts_code"]
        created = client.post("/api/practice/sessions", json={
            "name": "observation test", "start_date": demo["demo"]["start_date"],
            "end_date": demo["demo"]["end_date"], "cash_cents": 1000000,
            "positions": [], "data_version_id": demo["demo"]["version_id"],
            "source_code": demo["demo"]["source_code"], "settings": {},
            "idempotency_key": "obs-session-create",
        })
        assert created.status_code == 200, created.text
        session = created.json()["session"]
        algo = client.get("/api/practice/algorithms").json()["algorithms"][0]
        body = {
            "practice_id": session["practice_id"], "expected_revision": session["revision"],
            "idempotency_key": "obs-create-key", "consent": True, "symbols": [symbol],
            "steps": 3, "max_order_value_cents": 10000, "max_position_shares": 100,
            "deadline": session["end_date"], "algorithm_id": algo["algorithm_id"],
            "algorithm_version": algo["version"], "parameters": {},
            "permissions": {"observe": True, "orders": False, "advance": False},
        }
        denied = dict(body, consent=False, idempotency_key="obs-denied")
        assert client.post("/api/practice/observe-plans", json=denied).status_code == 400
        first = client.post("/api/practice/observe-plans", json=body)
        assert first.status_code == 200, first.text
        again = client.post("/api/practice/observe-plans", json=body)
        assert again.json()["execution_id"] == first.json()["execution_id"]
        eid = first.json()["execution_id"]
        assert client.post(f"/api/practice/observe-plans/{eid}/pause", json={
            "expected_version": 1, "idempotency_key": "obs-pause",
        }).json()["state"] == "paused"
        assert client.get(f"/api/practice/observe-plans/{eid}").json()["state"] == "paused"
        assert client.get(f"/api/practice/observe-plans?practice_id={session['practice_id']}").json()["plans"][0]["execution_id"] == eid
