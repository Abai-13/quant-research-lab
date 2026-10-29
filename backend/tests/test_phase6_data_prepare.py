"""Phase 6 preparation contract tests; all databases are isolated under tmp_path."""

from __future__ import annotations

import json
import multiprocessing
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import load_settings
from app.api.practice import create_practice_router
from app.data.chunks import VersionView
from app.data.universe import (
    LIMIT_CORPORATE_ACTION_UNKNOWN,
    LIMIT_HISTORICAL_AVAILABILITY,
    LIMIT_HISTORICAL_UNIVERSE_INCOMPLETE,
    LIMIT_SUSPENSION_UNKNOWN,
    LIMIT_SURVIVORSHIP_BIAS,
    load_pool_snapshot,
)
from app.practice.runtime import practice_connect
from app.practice_data import (
    create_practice_data_router,
    reset_practice_connect_provider,
    set_practice_connect_provider,
)
from app.practice_data import service as data_service
from app.practice_data import store as data_store
from app.practice_data.bridge import publish_prepared_dataset


def _client(tmp_path):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    app = FastAPI()
    app.state.settings = settings
    app.include_router(create_practice_data_router())
    app.include_router(create_practice_router())
    set_practice_connect_provider(lambda _settings: practice_connect(settings))
    return TestClient(app), settings


def test_presets_are_read_only_and_limits_are_public(tmp_path):
    client, _settings = _client(tmp_path)
    try:
        payload = client.get("/data/presets").json()
        sources = client.get("/data/sources").json()
    finally:
        client.close()
        reset_practice_connect_provider()
    assert payload["ok"] is True
    assert {item["preset_id"] for item in payload["presets"]} >= {"recent-1y", "recent-6m"}
    assert payload["limits"]["max_symbols"] == 20
    assert payload["limits"]["max_daily_rows"] == 20_000
    baostock = next(item for item in sources["sources"] if item["source_code"] == "baostock")
    assert "did not repeat" in baostock["evidence"]["review_scope"]
    assert any("/data/prepare" in item and "\u542f\u52a8" in item for item in sources["limitations"])


def test_prepare_publishes_versionview_ready_snapshot_and_content_cache(tmp_path):
    client, settings = _client(tmp_path)
    body = {
        "source_code": "synthetic", "symbols": ["sh.600000", "sz.000001"],
        "start_date": "2024-07-01", "end_date": "2024-07-12",
        "refresh_mode": "synthetic",
        "idempotency_key": "phase6-data-prepare-1",
    }
    try:
        submitted = client.post("/data/prepare", json=body)
        assert submitted.status_code == 202
        job_id = submitted.json()["operation_id"]
        job = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/data/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "interrupted"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed", job
        assert {"operation_id", "kind", "attempt", "generation", "progress", "checkpoint",
                "cumulative_usage", "limits", "resumable", "stop_reason", "created_at", "updated_at"} <= set(job)
        assert job["kind"] == "data_prepare" and job["progress"]["phase"] == "completed"
        assert "fraction" not in job["progress"]
        assert {"sequence", "steps", "validation"} <= set(job["checkpoint"])
        assert job["result"]["data_version_id"]
        assert "request" not in job and "job_id" not in job
        prepared = job["result"]
        assert prepared["eligibility"] == "restricted_analysis"
        assert prepared["pool_snapshot_id"]
        with practice_connect(settings) as conn:
            view = VersionView(conn, prepared["data_version_id"], expected_source_code="synthetic")
            assert len(view.daily_series("600000.SH", on_or_before="20240712")) > 0
            snapshot = load_pool_snapshot(conn, prepared["pool_snapshot_id"])
            assert snapshot is not None
            assert set(snapshot["members"]) == {"000001.SZ", "600000.SH"}
            assert {
                LIMIT_HISTORICAL_UNIVERSE_INCOMPLETE,
                LIMIT_SURVIVORSHIP_BIAS,
                LIMIT_HISTORICAL_AVAILABILITY,
                LIMIT_CORPORATE_ACTION_UNKNOWN,
                LIMIT_SUSPENSION_UNKNOWN,
            }.issubset(snapshot["limit_flags"])
            quality = json.loads(conn.execute(
                "SELECT quality_summary FROM data_version WHERE version_id=?", (prepared["data_version_id"],)
            ).fetchone()[0])
            assert quality["eligibility"] == "restricted_analysis"
            assert quality["calendar"] == "weekday_sample"
            assert quality["corporate_actions"] == "unavailable"
            assert quality["synthetic_listing_anchor"] == "sample_start_date_not_actual_ipo"
            assert quality["survivorship_bias"] == "unresolved"
            assert quality["historical_availability"] == "unproven"
        assert any("not an actual listing date" in item for item in prepared["limitations"])
        assert any("Survivorship bias" in item for item in prepared["limitations"])
        created = client.post("/api/practice/sessions", json={
            "name": "phase6 prepared sample", "start_date": "20240701", "end_date": "20240712",
            "cash_cents": 100000, "data_version_id": prepared["data_version_id"],
            "source_code": "synthetic", "idempotency_key": "phase6-data-session-1",
        })
        assert created.status_code == 200, created.text
        session = created.json()["session"]
        assert session["settings"]["pool_snapshot_id"] == prepared["pool_snapshot_id"]
        advanced = client.post(f"/api/practice/sessions/{session['practice_id']}/advance", json={
            "expected_revision": session["revision"], "idempotency_key": "phase6-data-advance-1",
        })
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["session"]["revealed_date"] == "20240702"
        body["refresh_mode"] = "reuse_cache"
        body["idempotency_key"] = "phase6-data-prepare-2"
        cache_job = client.post("/data/prepare", json=body).json()
        assert cache_job["status"] == "completed"
        assert cache_job["result"]["cache_hit"] is True
        assert cache_job["result"]["data_version_id"] == prepared["data_version_id"]
        assert cache_job["result"]["pool_snapshot_id"] == prepared["pool_snapshot_id"]
        body["refresh_mode"] = "refresh"
        body["idempotency_key"] = "phase6-data-prepare-force-1"
        refreshed = client.post("/data/prepare", json=body).json()
        refreshed_id = refreshed["operation_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            refreshed = client.get(f"/data/jobs/{refreshed_id}").json()
            if refreshed["status"] in {"completed", "failed", "interrupted"}:
                break
            time.sleep(0.02)
        assert refreshed["status"] == "completed", refreshed
        assert refreshed["result"]["cache_hit"] is False
    finally:
        client.close()
        reset_practice_connect_provider()


def test_twenty_short_prepare_jobs_keep_executor_bounded_and_history_accounted(tmp_path):
    client, settings = _client(tmp_path)
    from app.practice_data import operations

    baseline_threads = len(threading.enumerate())
    baseline_children = len(multiprocessing.active_children())
    operations.startup_operations(settings)
    completed = []
    try:
        for index in range(20):
            code = f"sh.{600000 + index}"
            body = {
                "source_code": "synthetic", "symbols": [code],
                "start_date": "2024-07-01", "end_date": "2024-07-05",
                "idempotency_key": f"phase8-short-job-{index}",
            }
            submitted = client.post("/data/prepare", json=body)
            assert submitted.status_code == 202, submitted.text
            job_id = submitted.json()["operation_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                job = client.get(f"/data/jobs/{job_id}").json()
                if job["status"] in {"completed", "failed", "interrupted"}:
                    break
                time.sleep(0.01)
            assert job["status"] == "completed", job
            completed.append(job_id)
            assert len(threading.enumerate()) <= baseline_threads + 3
            assert len(multiprocessing.active_children()) == baseline_children
    finally:
        client.close()
        operations.shutdown_operations(wait=True)
        reset_practice_connect_provider()

    with practice_connect(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM practice_data_job").fetchone()[0] == 20
        assert conn.execute("SELECT COUNT(*) FROM practice_data_job_unit").fetchone()[0] == 20
        assert conn.execute("SELECT COUNT(*) FROM practice_data_version").fetchone()[0] == 20
    assert len(completed) == 20
    assert len(threading.enumerate()) <= baseline_threads + 1
    assert len(multiprocessing.active_children()) == baseline_children

def test_cancel_fences_late_source_result(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def delayed_source(symbol, start_date, end_date):
        calls.append(symbol)
        entered.set()
        assert release.wait(5)
        return data_service.synthetic_fetch_daily(symbol, start_date, end_date)

    monkeypatch.setitem(data_service.PROVIDERS, "synthetic", delayed_source)
    body = {
        "source_code": "synthetic", "symbols": ["sh.600000"],
        "start_date": "2024-07-01", "end_date": "2024-07-05",
        "idempotency_key": "phase6-data-cancel-1",
    }
    try:
        submission = client.post("/data/prepare", json=body).json()
        assert entered.wait(5)
        job_id = submission["operation_id"]
        cancelled = client.post(f"/data/jobs/{job_id}/cancel", json={"expected_generation": 1, "idempotency_key": "phase6-data-cancel-action-1"})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        replayed_cancel = client.post(f"/data/jobs/{job_id}/cancel", json={"expected_generation": 1, "idempotency_key": "phase6-data-cancel-action-1"})
        assert replayed_cancel.json()["generation"] == cancelled.json()["generation"]
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/data/jobs/{job_id}").json()
            if job["status"] == "cancelled":
                break
            time.sleep(0.02)
        assert job["status"] == "cancelled"
        assert job["resumable"] is True
        with practice_connect(settings) as conn:
            assert conn.execute("SELECT COUNT(*) FROM practice_data_version").fetchone()[0] == 0
        resumed = client.post(f"/data/jobs/{job_id}/resume", json={"expected_generation": 2, "idempotency_key": "phase6-data-resume-action-1"})
        assert resumed.status_code == 202
        assert resumed.json()["generation"] == 3
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/data/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "interrupted"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed", job
        assert job["attempt"] == 2
        assert len(calls) == 2
        with practice_connect(settings) as conn:
            assert conn.execute("SELECT COUNT(*) FROM practice_data_job_unit").fetchone()[0] == 1
    finally:
        release.set()
        client.close()
        reset_practice_connect_provider()


def test_price_only_real_source_is_blocked_from_simulation(tmp_path):
    client, settings = _client(tmp_path)
    client.close()
    reset_practice_connect_provider()
    with practice_connect(settings) as conn:
        data_store.ensure_practice_data_schema(conn)
        version = {
            "version_id": "baostock-test-version", "source_code": "baostock", "synthetic": False,
            "symbols": ["sh.600000"], "start_date": "2024-07-01", "end_date": "2024-07-05",
            "record_count": 0, "coverage": {"calendar_checked": False, "corporate_actions": "not_included"},
            "limitations": ["corporate actions unavailable"], "created_at": "2026-10-25T00:00:00+00:00",
        }
        data_store.insert_version(conn, version)
        conn.commit()
        prepared = publish_prepared_dataset(conn, version["version_id"])
    assert prepared["eligibility"] == "blocked"
    assert prepared["data_version_id"] is None
    assert prepared["pool_snapshot_id"] is None


def _synthetic_bridge_version(conn, *, version_id, rows, end_date="2024-07-07"):
    data_store.ensure_practice_data_schema(conn)
    version = {
        "version_id": version_id, "source_code": "synthetic", "synthetic": True,
        "symbols": ["sh.600000"], "start_date": "2024-07-01", "end_date": end_date,
        "record_count": len(rows), "coverage": {"content_sha256": "fixture"},
        "limitations": [], "created_at": "2026-10-25T00:00:00+00:00",
    }
    data_store.insert_version(conn, version)
    data_store.insert_daily_rows(conn, version_id, rows)


def test_synthetic_bridge_anchors_weekend_end_date_to_last_observed_row(tmp_path):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    reset_practice_connect_provider()
    row = {"code": "sh.600000", "trade_date": "2024-07-05", "open": 10, "high": 10,
           "low": 10, "close": 10, "preclose": 10, "volume": 1000, "amount": 10000,
           "adjustflag": "3", "tradestatus": 1}
    with practice_connect(settings) as conn:
        _synthetic_bridge_version(conn, version_id="weekend-sample", rows=[row])
        result = publish_prepared_dataset(conn, "weekend-sample")
        snapshot = load_pool_snapshot(conn, result["pool_snapshot_id"])
        assert result["effective_as_of"] == "20240705"
        assert snapshot["as_of"] == "20240705"


def test_synthetic_bridge_empty_data_is_blocked_without_snapshot(tmp_path):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    reset_practice_connect_provider()
    with practice_connect(settings) as conn:
        _synthetic_bridge_version(conn, version_id="empty-sample", rows=[])
        result = publish_prepared_dataset(conn, "empty-sample")
    assert result["eligibility"] == "blocked"
    assert result["pool_snapshot_id"] is None
