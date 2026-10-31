"""Behavior checks against the real practice and simulation engines, TEMP only."""
from __future__ import annotations

import time
import uuid
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.practice import observation
from app.practice.core import PracticeError, require_session
from app.practice.observation_runner import _connection, execute_step
from app.practice.runtime import practice_settings


@pytest.fixture(scope="module")
def environment(tmp_path_factory):
    root = tmp_path_factory.mktemp("observation")
    settings = load_settings({"APP_DATA_SOURCE": "tushare-fixture", "APP_RUNTIME_DIR": str(root / "runtime")}, project_root=root)
    app = create_app(settings)
    with TestClient(app) as client:
        demo = client.post("/api/practice/demo").json()
        yield client, app, settings, demo


def request(client, method, path, body=None):
    response = client.request(method, path, json=body) if body is not None else client.request(method, path)
    assert response.status_code == 200, response.text
    return response.json()


def make_plan(env, *, orders=True, advance=True, steps=2, cap=1_000_000, start="20240102"):
    client, app, settings, demo = env
    session = request(client, "POST", "/api/practice/sessions", {
        "name": "runner behavior", "start_date": start, "end_date": "20240201",
        "cash_cents": 10_000_000, "positions": [], "source_code": demo["demo"]["source_code"],
        "data_version_id": demo["demo"]["version_id"], "settings": {}, "idempotency_key": uuid.uuid4().hex,
    })["session"]
    algorithm = request(client, "GET", "/api/practice/algorithms")["algorithms"][0]
    plan = request(client, "POST", "/api/practice/observe-plans", {
        "practice_id": session["practice_id"], "expected_revision": session["revision"],
        "idempotency_key": uuid.uuid4().hex, "consent": True, "symbols": ["000001.SZ"],
        "steps": steps, "max_order_value_cents": cap, "max_position_shares": 300,
        "deadline": "20240201", "algorithm_id": algorithm["algorithm_id"], "algorithm_version": algorithm["version"],
        "parameters": {}, "permissions": {"observe": True, "orders": orders, "advance": advance},
    })
    return session, plan


def body(plan):
    return {"expected_version": plan["version"], "idempotency_key": uuid.uuid4().hex, "consent": True}


def wait(client, plan):
    for _ in range(200):
        state = request(client, "GET", "/api/practice/observe-plans/" + plan["execution_id"])
        if state["state"] != "running": return state
        time.sleep(.03)
    pytest.fail("bounded runner did not stop")


@contextmanager
def transaction(env):
    conn = _connection(env[2])
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
    finally: conn.close()


def begin(env, plan):
    with transaction(env) as conn:
        return observation.begin_run(conn, plan["execution_id"], body(plan))["run_token"]


def test_real_registered_targets_create_filled_orders_and_bounded_progress(environment):
    client, app, settings, _ = environment
    session, plan = make_plan(environment)
    start = body(plan)
    running = request(client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/start", start)
    assert "run_token" not in running
    final = wait(client, plan)
    assert final["state"] == "completed", final
    assert final["completed_steps"] == 2
    after = request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"]
    assert after["revealed_date"] == "20240104"
    assert any(o["filled_shares"] > 0 for o in after["user_state"]["orders"]), after
    assert 0 < after["user_state"]["positions"][0]["shares"] <= 300
    assert any(e["payload"].get("selected") == ["000001.SZ"] for e in final["events"])
    request(client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/start", start)
    assert request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"] == after


@pytest.mark.parametrize("orders,advance,expected_steps", [(False, True, 2), (False, False, 1), (True, False, 1)])
def test_permissions_are_independent(environment, orders, advance, expected_steps):
    client = environment[0]
    session, plan = make_plan(environment, orders=orders, advance=advance)
    request(client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/start", body(plan))
    final = wait(client, plan)
    assert final["state"] == "completed", final
    assert final["completed_steps"] == expected_steps
    after = request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"]
    assert (after["revealed_date"] != session["revealed_date"]) is advance
    assert bool(after["user_state"]["orders"]) is orders
    if orders and not advance: assert final["stop_reason"] == "awaiting_manual_advance"


def test_pause_revoke_and_old_token_cannot_write(environment):
    session, plan = make_plan(environment)
    token = begin(environment, plan)
    with transaction(environment) as conn:
        current = observation.get_plan(conn, plan["execution_id"])
        paused = observation.mutate(conn, plan["execution_id"], {"expected_version": current["version"], "idempotency_key": "pause"}, "pause")
        assert not execute_step(conn, plan["execution_id"], token, environment[1].state.practice_actions, environment[2])
        resumed = observation.begin_run(conn, plan["execution_id"], body(paused), resume=True)
        assert resumed["run_token"] != token
        assert not execute_step(conn, plan["execution_id"], token, environment[1].state.practice_actions, environment[2])
        current = resumed["execution"]
        observation.mutate(conn, plan["execution_id"], {"expected_version": current["version"], "idempotency_key": "revoke"}, "revoke")
        assert not execute_step(conn, plan["execution_id"], resumed["run_token"], environment[1].state.practice_actions, environment[2])
        assert require_session(conn, session["practice_id"])["revision"] == session["revision"]


def test_checkpoint_recovery_requires_resume_and_does_not_repeat_committed_step(environment):
    client, app, settings, _ = environment
    session, plan = make_plan(environment, orders=False, steps=3)
    token = begin(environment, plan)
    with transaction(environment) as conn:
        assert execute_step(conn, plan["execution_id"], token, app.state.practice_actions, settings)
    app.state.observation_runner.recover()
    paused = request(client, "GET", "/api/practice/observe-plans/" + plan["execution_id"])
    assert paused["state"] == "paused" and paused["completed_steps"] == 1
    assert paused["stop_reason"] == "process_restarted_review_required"
    request(client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/resume", body(paused))
    final = wait(client, plan)
    assert final["state"] == "completed" and final["completed_steps"] == 3
    after = request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"]
    assert after["revision"] == session["revision"] + 3
    assert [e["payload"]["step"] for e in final["events"] if e["event_type"] == "step_completed"] == [1, 2, 3]


def test_uncommitted_step_rolls_back_effects_and_checkpoint(environment):
    _, app, settings, _ = environment
    session, plan = make_plan(environment)
    token = begin(environment, plan)
    conn = _connection(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        execute_step(conn, plan["execution_id"], token, app.state.practice_actions, settings)
        assert require_session(conn, session["practice_id"])["revision"] > session["revision"]
        conn.rollback()  # Same SQLite boundary as interruption before COMMIT.
        assert require_session(conn, session["practice_id"])["revision"] == session["revision"]
        assert conn.execute("SELECT COUNT(*) FROM practice_order WHERE practice_id=?", (session["practice_id"],)).fetchone()[0] == 0
        assert observation.get_plan(conn, plan["execution_id"])["completed_steps"] == 0
    finally: conn.close()
    app.state.observation_runner.recover()


def test_external_change_blocks_without_effects_and_pending_manual_order_is_protected(environment):
    client = environment[0]
    session, plan = make_plan(environment, orders=False)
    placed = request(client, "POST", f"/api/practice/sessions/{session['practice_id']}/orders", {
        "ts_code": "000001.SZ", "direction": "buy", "planned_shares": 100,
        "expected_revision": session["revision"], "idempotency_key": uuid.uuid4().hex,
    })["session"]
    rejected = client.post(f"/api/practice/observe-plans/{plan['execution_id']}/start", json=body(plan))
    assert rejected.status_code == 409
    # A newly authorized plan still must not advance a manual pending order.
    with transaction(environment) as conn:
        frozen = observation.decode_json(conn.execute("SELECT frozen_json FROM observation_execution WHERE execution_id=?", (plan["execution_id"],)).fetchone()[0])
        frozen["practice_revision"] = placed["revision"]
        conn.execute("UPDATE observation_execution SET frozen_json=? WHERE execution_id=?", (observation.encode_json(frozen), plan["execution_id"]))
    request(client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/start", body(plan))
    final = wait(client, plan)
    assert final["state"] == "blocked" and final["stop_reason"] == "pending_orders_require_review"
    assert request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"] == placed


def test_gap_over_order_limit_rolls_back_entire_step(environment):
    client, app, settings, _ = environment
    session, plan = make_plan(environment)
    token = begin(environment, plan)
    # Fault injection at the engine boundary: simulate a next-day gap exceeding cap.
    real = app.state.practice_actions["advance"]
    def gap(conn, pid, payload, settings):
        result = real(conn, pid, payload, settings)
        conn.execute("UPDATE simulation_fill SET price_cents=1000000 WHERE order_id IN (SELECT order_id FROM simulation_order WHERE segment_id=?)", (require_session(conn,pid)["segments"]["user"],))
        return result
    with pytest.raises(PracticeError, match="整步已回滚"):
        with transaction(environment) as conn:
            execute_step(conn, plan["execution_id"], token, {**app.state.practice_actions, "advance": gap}, settings)
    with transaction(environment) as conn:
        assert require_session(conn, session["practice_id"])["revision"] == session["revision"]
        assert observation.get_plan(conn, plan["execution_id"])["completed_steps"] == 0
        assert conn.execute("SELECT COUNT(*) FROM practice_order WHERE practice_id=?", (session["practice_id"],)).fetchone()[0] == 0
    app.state.observation_runner.recover()


def test_old_demo_cache_is_preserved_when_five_year_seed_is_created(temp_settings):
    from app.portfolio.demo import ensure_demo_database
    legacy = temp_settings.runtime_dir / "phase3-demo" / "app.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy cache preserved")
    seeded = ensure_demo_database(temp_settings)
    assert legacy.read_bytes() == b"legacy cache preserved"
    assert seeded["coverage_start"] <= "20200701"
    assert seeded["coverage_end"] == "20250630"


def test_pause_waits_for_atomic_step_then_prevents_later_writes(environment, monkeypatch):
    from app.practice import observation_runner
    client, app, settings, _ = environment
    session, plan = make_plan(environment, orders=False, steps=20)
    entered, release = Event(), Event()
    original = observation_runner._targets
    def held(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(observation_runner, "_targets", held)
    running = request(client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/start", body(plan))
    assert entered.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pause = pool.submit(request, client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/pause", {
            "expected_version": running["version"], "idempotency_key": uuid.uuid4().hex,
        })
        try:
            time.sleep(.05)
            assert not pause.done()
        finally: release.set()
        paused = pause.result(5)
    assert paused["state"] == "paused"
    before = request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"]
    time.sleep(.15)
    assert request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"] == before
    assert before["revision"] == session["revision"] + paused["completed_steps"]


def test_process_exit_before_commit_recovers_without_duplicate_effects(environment):
    client, app, settings, _ = environment
    session, plan = make_plan(environment, steps=1)
    token = begin(environment, plan)
    code = '''
import os,sys
from app.config import load_settings
from app.main import create_app
from app.practice.observation_runner import _connection,execute_step
settings=load_settings({"APP_RUNTIME_DIR":sys.argv[1],"APP_DATA_SOURCE":"tushare-fixture"})
app=create_app(settings)
conn=_connection(settings)
conn.execute("BEGIN IMMEDIATE")
execute_step(conn,sys.argv[2],sys.argv[3],app.state.practice_actions,settings)
os._exit(23)
'''
    result = subprocess.run([sys.executable, "-c", code, str(settings.runtime_dir), plan["execution_id"], token],
                            capture_output=True, timeout=15)
    assert result.returncode == 23, result.stderr.decode(errors="replace")
    app.state.observation_runner.recover()
    paused = request(client, "GET", "/api/practice/observe-plans/" + plan["execution_id"])
    assert paused["completed_steps"] == 0 and paused["state"] == "paused"
    before = request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"]
    assert before == session
    request(client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/resume", body(paused))
    final = wait(client, plan)
    assert final["state"] == "completed" and final["completed_steps"] == 1
    after = request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"]
    assert len(after["user_state"]["orders"]) == 1
    assert after["user_state"]["orders"][0]["filled_shares"] > 0


def test_total_holdings_limit_is_shared_across_authorized_symbols(environment):
    client, app, settings, _ = environment
    session, plan = make_plan(environment, steps=1)
    with transaction(environment) as conn:
        conn.execute("UPDATE observation_execution SET symbols_json=? WHERE execution_id=?",
                     (observation.encode_json(["000001.SZ", "000002.SZ"]), plan["execution_id"]))
    request(client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/start", body(plan))
    final = wait(client, plan)
    assert final["state"] == "completed", final
    after = request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"]
    assert 0 < sum(p["shares"] for p in after["user_state"]["positions"]) <= 300


@pytest.mark.parametrize("target", [
    {"ts_code": "000001.SZ", "target_value_cents": -100},
    {"ts_code": "000001.SZ", "target_value_cents": True},
    {"ts_code": "OUTSIDE", "target_value_cents": 100},
])
def test_unsupported_algorithm_targets_stop_without_any_effect(environment, monkeypatch, target):
    from app.practice import observation_runner
    client, app, settings, _ = environment
    session, plan = make_plan(environment, steps=1)
    original = observation_runner._targets
    def malformed(*args):
        report, synthetic = original(*args)
        report["allocation"]["targets"] = [target]
        return report, synthetic
    monkeypatch.setattr(observation_runner, "_targets", malformed)
    request(client, "POST", f"/api/practice/observe-plans/{plan['execution_id']}/start", body(plan))
    final = wait(client, plan)
    assert final["state"] == "blocked" and final["stop_reason"] == "algorithm_output_invalid"
    assert request(client, "GET", f"/api/practice/sessions/{session['practice_id']}")["session"] == session
