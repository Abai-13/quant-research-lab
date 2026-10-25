from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.data.chunks import VersionView
from app.agent_tools.service import capture_context
from app.main import create_app
from app.practice.runtime import practice_connect, practice_settings
from app.research.runner import run_experiment
from app.research.service import ResearchError


SOURCE_FIELDS = {
    "practice_id", "revision", "revealed_date", "data_version_id", "source_code",
    "source_instance_id", "settings_fingerprint", "algorithm_fingerprint",
    "fee_fingerprint", "segment_fingerprint",
}


def _create_session(client: TestClient, settings):
    demo_response = client.post("/api/practice/demo")
    assert demo_response.status_code == 200, demo_response.text
    demo = demo_response.json()["demo"]
    with practice_connect(settings) as conn:
        calendar = VersionView(conn, demo["version_id"], expected_source_code=demo["source_code"]).calendar_days()
    dates = calendar[:5]
    assert len(dates) == 5
    created = client.post("/api/practice/sessions", json={
        "name": "source-context TEMP fixture", "start_date": dates[0], "end_date": dates[-1],
        "cash_cents": 100_000, "positions": [], "data_version_id": demo["version_id"],
        "source_code": demo["source_code"], "settings": {"hide_system_hints": False},
        "idempotency_key": "phase6-context-session",
    })
    assert created.status_code == 200, created.text
    return created.json()["session"], dates


def test_research_context_is_read_only_and_matches_frozen_plan_contract(temp_settings):
    app = create_app(temp_settings)
    with TestClient(app) as client:
        session, dates = _create_session(client, temp_settings)
        practice_path = practice_settings(temp_settings).db_path
        before = hashlib.sha256(practice_path.read_bytes()).hexdigest()
        response = client.get(
            f"/api/practice/sessions/{session['practice_id']}/research-context?expected_revision=1"
        )
        after = hashlib.sha256(practice_path.read_bytes()).hexdigest()
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["availability"] == "available"
        source = payload["source_context"]
        assert set(source) == SOURCE_FIELDS
        assert source["practice_id"] == session["practice_id"]
        assert source["revision"] == session["revision"] == 1
        assert source["revealed_date"] == session["revealed_date"] == dates[0]
        assert source["data_version_id"] == session["source"]["version_id"]
        assert source["source_code"] == session["source"]["code"]
        shared = capture_context(temp_settings, session["practice_id"], expected_revision=1)
        for key in SOURCE_FIELDS - {"source_instance_id"}:
            assert source[key] == shared[key]
        assert source["source_instance_id"] == hashlib.sha256(str(temp_settings.runtime_dir.resolve()).encode("utf-8")).hexdigest()
        for key in SOURCE_FIELDS - {"practice_id", "revision", "revealed_date", "data_version_id", "source_code"}:
            assert len(source[key]) == 64 and all(char in "0123456789abcdef" for char in source[key])
        assert "runtime_dir" not in json.dumps(payload) and str(temp_settings.runtime_dir) not in json.dumps(payload)
        assert before == after

        plan_response = client.post("/api/research/plans", json={
            "idempotency_key": "phase6-context-plan",
            "engine_id": "practice_deterministic_v1", "source_context": source,
            "training_dates": [dates[1]], "evaluation_dates": [dates[2]], "warmup_dates": [],
            "parameter_name": "rebalance_every_n_days", "candidates": [10], "baseline_value": 5,
            "primary_metric": "net_return_bps", "selection_rule": "highest_net_return_bps",
            "risk_constraints": {}, "budget": {"wall_seconds": 30, "max_bytes": 1048576},
        })
        assert plan_response.status_code == 200, plan_response.text
        assert plan_response.json()["plan"]["source_context"] == source

        advanced = client.post(f"/api/practice/sessions/{session['practice_id']}/advance", json={
            "expected_revision": 1, "idempotency_key": "phase6-context-advance",
        })
        assert advanced.status_code == 200, advanced.text
        stale = client.get(
            f"/api/practice/sessions/{session['practice_id']}/research-context?expected_revision=1"
        )
        assert stale.status_code == 409
        current = client.get(
            f"/api/practice/sessions/{session['practice_id']}/research-context?expected_revision=2"
        )
        assert current.status_code == 200
        assert current.json()["source_context"]["revealed_date"] == dates[1]
        try:
            run_experiment(temp_settings, plan_response.json()["plan"]["plan_id"], experiment_id="exp-stale-revision")
        except ResearchError as exc:
            assert exc.reason == "source_context_changed"
        else:
            raise AssertionError("frozen plan ran after its practice revision changed")


def test_research_context_reports_legacy_missing_segments_without_fingerprints(temp_settings):
    app = create_app(temp_settings)
    with TestClient(app) as client:
        session, _dates = _create_session(client, temp_settings)
        with practice_connect(temp_settings) as conn:
            conn.execute("UPDATE practice_session SET segments_json='{}' WHERE practice_id=?", (session["practice_id"],))
            conn.commit()
        response = client.get(
            f"/api/practice/sessions/{session['practice_id']}/research-context?expected_revision=1"
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["availability"] == "supplementation_required"
        assert set(payload["missing_fields"]) == {"algorithm_fingerprint", "fee_fingerprint", "segment_fingerprint"}
        assert "source_context" not in payload
        assert response.json().get("settings_fingerprint") is None


def test_research_context_blocks_revealed_date_outside_frozen_calendar(temp_settings):
    app = create_app(temp_settings)
    with TestClient(app) as client:
        session, dates = _create_session(client, temp_settings)
        available = set(dates)
        candidate = date.fromisoformat(dates[0][:4] + "-" + dates[0][4:6] + "-" + dates[0][6:]) + timedelta(days=1)
        while candidate.isoformat().replace("-", "") in available:
            candidate += timedelta(days=1)
        revealed_date = candidate.isoformat().replace("-", "")
        assert dates[0] < revealed_date < dates[-1]
        with practice_connect(temp_settings) as conn:
            conn.execute("UPDATE practice_session SET revealed_date=? WHERE practice_id=?", (revealed_date, session["practice_id"]))
            conn.commit()
        response = client.get(
            f"/api/practice/sessions/{session['practice_id']}/research-context?expected_revision=1"
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["availability"] == "supplementation_required"
        assert payload["reason"] == "source_data_time_unavailable"
        assert set(payload["missing_fields"]) == {"revealed_date", "data_version_id"}
        assert "source_context" not in payload


def test_research_context_hides_unknown_practice_ids(temp_settings):
    app = create_app(temp_settings)
    with TestClient(app) as client:
        response = client.get("/api/practice/sessions/not-visible/research-context?expected_revision=1")
    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "practice_not_found"}
