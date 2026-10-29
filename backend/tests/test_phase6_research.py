from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from decimal import Decimal, ROUND_HALF_UP
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import research as research_api
from app.api.research import create_research_router
from app.agent_tools.research import invoke_research_tool
from app.agent_tools.service import _output, capture_context, execute_tool, tool_definitions
from app.research.exposure import assess_exposure, normalize_symbol, register_exposure
from app.research.service import ResearchError, create_record, validate_plan
from app.research.store import research_connect, research_path
from app.research.store import maintenance_reference_snapshot
from app.research.runner import _summarize_variant_metrics
from app.research.service import create_plan, maintenance_reference_snapshot as service_reference_snapshot
from app.research import executor as research_executor
from app.research import store as research_store
from app.practice.comparison import _next_rebalance_is_due
from app.main import create_app
from app.practice.runtime import practice_connect, practice_settings
from app.practice.comparison import _get_next_trade_date
from app.practice.core import require_session


def _plan(**overrides):
    value = {
        "engine_id": "practice_deterministic_v1",
        "source_context": {"practice_id": "p1", "revision": 1, "revealed_date": "20250101",
                           "source_code": "synthetic", "data_version_id": "v1", "source_instance_id": "runtime",
                           "settings_fingerprint": "settings", "algorithm_fingerprint": "algorithm",
                           "fee_fingerprint": "fee", "segment_fingerprint": "segment"},
        "training_dates": ["20250102", "20250103", "20250106"],
        "evaluation_dates": ["20250107", "20250108"],
        "warmup_dates": [], "parameter_name": "rebalance_every_n_days", "candidates": [10, 20],
        "baseline_value": 5, "primary_metric": "net_return_bps", "selection_rule": "highest_net_return_bps",
        "risk_constraints": {"max_drawdown_bps": -5000}, "budget": {"wall_seconds": 180, "max_bytes": 104857600},
    }
    return {**value, **overrides}


def test_research_api_append_only_search_and_as_of(temp_settings):
    with practice_connect(temp_settings) as practice:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        for practice_id in ("p1", "p2"):
            practice.execute(
                "INSERT INTO practice_session(practice_id,name,status,revision,start_date,end_date,revealed_date,data_version_id,source_code,segments_json,settings_json,user_state_json,created_at,updated_at) VALUES(?,?,'active',1,'20250101','20991231',?,'v1','synthetic','[]','{}','{}','now','now')",
                (practice_id, practice_id, today),
            )
    app = FastAPI()
    app.state.settings = temp_settings
    app.include_router(create_research_router())
    with TestClient(app) as client:
        legal_source_refs = [
            {"owner_type": "practice", "owner_id": "p1", "source_code": "synthetic", "data_version_id": "v1"},
            {"owner_type": "practice", "owner_id": "p1", "source_code": "synthetic", "data_version_id": "v2",
             "symbols": ["AAA"], "interval": {"start_date": "20250101", "end_date": "20250102"}},
            {"owner_type": "practice", "owner_id": "p1", "source_code": "evidence", "data_version_id": "evidence-v1",
             "evidence_id": "ev-1", "research_record_id": "rr-source-1"},
        ]
        record_body = {
            "idempotency_key": "record-create-1",
            "scope": "simulation", "kind": "hypothesis", "goal": "reduce churn", "hypothesis": "10 day cadence lowers fees",
            "disconfirming_condition": "net return falls", "evidence_as_of": "20250101",
            "source_refs": legal_source_refs,
        }
        created = client.post("/api/research/records", json=record_body).json()["record"]
        replayed = client.post("/api/research/records", json=record_body).json()["record"]
        conflict_body = {**record_body, "goal": "different payload"}
        conflict = client.post("/api/research/records", json=conflict_body)
        changed = client.post(f"/api/research/records/{created['record_id']}/revisions", json={
            "idempotency_key": "record-revision-1",
            "scope": "simulation", "kind": "hypothesis", "goal": "revised", "hypothesis": "include drawdown",
            "disconfirming_condition": "drawdown rises", "evidence_as_of": "20250102",
            "source_refs": legal_source_refs,
        }).json()["revision"]
        later_revision = client.post(f"/api/research/records/{created['record_id']}/revisions", json={
            "idempotency_key": "record-revision-visible-later",
            "scope": "simulation", "kind": "hypothesis", "goal": "later revision", "hypothesis": "must stay hidden",
            "disconfirming_condition": "later visibility", "evidence_as_of": today,
            "source_refs": [{"owner_id": "p1"}],
        }).json()["revision"]
        with research_connect(temp_settings) as conn:
            conn.execute(
                "UPDATE research_revision_visibility SET visible_as_of='20991231' WHERE revision_id=?",
                (later_revision["revision_id"],),
            )
        future = client.post(f"/api/research/records/{created['record_id']}/revisions", json={
            "idempotency_key": "record-revision-2",
            "scope": "simulation", "kind": "hypothesis", "goal": "future", "hypothesis": "future result leaked",
            "disconfirming_condition": "future", "evidence_as_of": "20991231",
            "source_refs": [{"owner_id": "p1"}],
        })
        as_of = datetime.now(timezone.utc).strftime("%Y%m%d")
        unscoped = client.post("/api/research/records", json={
            "idempotency_key": "record-create-unknown",
            "kind": "hypothesis", "goal": "unknown", "hypothesis": "must not be injected",
            "disconfirming_condition": "unknown", "evidence_as_of": as_of, "source_refs": [{"owner_id": "p1"}],
        }).json()["record"]
        foreign = client.post("/api/research/records", json={
            "idempotency_key": "record-create-foreign",
            "scope": "simulation", "kind": "report", "goal": "foreign", "hypothesis": "other simulation future",
            "disconfirming_condition": "unknown", "evidence_as_of": "20261025",
            "source_refs": [{"owner_id": "p2"}],
        }).json()["record"]
        global_record = client.post("/api/research/records", json={
            "idempotency_key": "record-global-current", "scope": "global", "kind": "reference",
            "goal": "global", "hypothesis": "global facts visible at current date",
            "disconfirming_condition": "later evidence", "evidence_as_of": as_of,
        }).json()["record"]
        future_global = client.post("/api/research/records", json={
            "idempotency_key": "record-global-future", "scope": "global", "kind": "reference",
            "goal": "future global", "hypothesis": "future global facts",
            "disconfirming_condition": "later evidence", "evidence_as_of": "20991231",
        }).json()["record"]
        undated_global = client.post("/api/research/records", json={
            "idempotency_key": "record-global-undated", "scope": "global", "kind": "reference",
            "goal": "undated global", "hypothesis": "unknown global evidence",
            "disconfirming_condition": "unknown evidence",
        }).json()["record"]
        hidden = client.get(f"/api/research/records/{created['record_id']}?as_of=20250101&practice_id=p1")
        visible = client.get(f"/api/research/records/{created['record_id']}?as_of={as_of}&practice_id=p1")
        unknown = client.get(f"/api/research/records/{unscoped['record_id']}?as_of={as_of}&practice_id=p1")
        unrelated = client.get(f"/api/research/records/{foreign['record_id']}?as_of={as_of}&practice_id=p1")
        visible_global = client.get(f"/api/research/records/{global_record['record_id']}?as_of={as_of}&practice_id=p1")
        hidden_future_global = client.get(f"/api/research/records/{future_global['record_id']}?as_of={as_of}&practice_id=p1")
        hidden_undated_global = client.get(f"/api/research/records/{undated_global['record_id']}?as_of={as_of}&practice_id=p1")
        search = client.get(f"/api/research/records?q=drawdown&practice_id=p1&as_of={as_of}").json()
        hidden_later_search = client.get(f"/api/research/records?q=must%20stay%20hidden&practice_id=p1&as_of={as_of}").json()
        foreign_search = client.get(f"/api/research/records?q=other%20simulation&practice_id=p1&as_of={as_of}").json()
    assert changed["parent_revision_id"] == created["revision_id"]
    assert replayed["record_id"] == created["record_id"]
    assert conflict.status_code == 409
    assert hidden.status_code == 404
    assert unknown.status_code == unrelated.status_code == 404
    assert visible_global.status_code == 200
    assert hidden_future_global.status_code == 404
    assert hidden_undated_global.status_code == 404
    assert visible.json()["record"]["body"]["hypothesis"] == "include drawdown"
    assert visible.json()["record"]["revision_id"] == changed["revision_id"]
    assert visible.json()["record"]["body"]["source_refs"] == legal_source_refs
    assert len(visible.json()["record"]["revision_history"]) == 2
    assert future.status_code == 422
    assert search["records"] and search["records"][0]["record_id"] == created["record_id"], search
    assert search["records"][0]["revision_id"] == changed["revision_id"]
    assert hidden_later_search["records"] == []
    assert foreign_search["records"] == []
    with research_connect(temp_settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_revision WHERE record_id=?", (created["record_id"],)).fetchone()[0] == 3
    assert research_path(temp_settings).parent == temp_settings.runtime_dir / "research"
    agent_result = invoke_research_tool(temp_settings, {"scope": "simulation", "practice_id": "p1", "revealed_date": as_of}, "search_research_records", {"query": "global facts", "limit": 10})
    assert [item["record_id"] for item in agent_result["records"]] == [global_record["record_id"]]
    assert agent_result["records"][0]["revision_id"] == global_record["revision_id"]
    assert agent_result["records"][0]["evidence_as_of"] == as_of
    assert agent_result["records"][0]["as_of"] == agent_result["as_of"] == as_of
    assert agent_result["records"][0]["scope"] == "global"
    # Search and direct read must identify the same latest visible revision.
    revised_search = invoke_research_tool(
        temp_settings, {"scope": "simulation", "practice_id": "p1", "revealed_date": as_of},
        "search_research_records", {"query": "drawdown", "limit": 10},
    )
    revised_item = next(item for item in revised_search["records"] if item["record_id"] == created["record_id"])
    revised_read = invoke_research_tool(
        temp_settings, {"scope": "simulation", "practice_id": "p1", "revealed_date": as_of},
        "read_research_record", {"record_id": created["record_id"]},
    )
    assert revised_item["revision_id"] == revised_read["revision_id"] == changed["revision_id"]
    assert revised_item["evidence_as_of"] == revised_read["evidence_as_of"] == "20250102"
    assert revised_item["owner_id"] == revised_read["owner_id"] == "p1"
    assert revised_item["body"]["source_refs"] == revised_read["body"]["source_refs"] == legal_source_refs
    assert revised_read["as_of"] == revised_search["as_of"] == as_of
    hidden_result = invoke_research_tool(
        temp_settings, {"scope": "simulation", "practice_id": "p1", "revealed_date": as_of},
        "search_research_records", {"query": "must stay hidden", "limit": 10},
    )
    foreign_result = invoke_research_tool(
        temp_settings, {"scope": "simulation", "practice_id": "p1", "revealed_date": as_of},
        "search_research_records", {"query": "other simulation", "limit": 10},
    )
    for unavailable in (hidden_result, foreign_result):
        assert unavailable["records"] == []
        assert unavailable["notice"] == "当前可见范围内没有找到匹配记录。可以换个关键词或说法再试。"
    wrapped = _output(
        {"practice_id": "p1", "revision": 1, "revealed_date": as_of,
         "context_fingerprint": "test-context", "limitations": []},
        "search_research_records", revised_search, "visible research snapshot",
    )
    wrapped_item = next(item for item in wrapped["evidence"][0]["data"]["records"]
                        if item["record_id"] == created["record_id"])
    assert wrapped["evidence"][0]["as_of"] == wrapped["as_of"] == as_of
    assert wrapped["evidence"][0]["version"]["practice_id"] == wrapped_item["owner_id"] == "p1"
    assert wrapped_item["revision_id"] == changed["revision_id"]
    future_results = [
        invoke_research_tool(temp_settings, {"scope": "simulation", "practice_id": "p1", "revealed_date": as_of}, "search_research_records", {"query": query, "limit": 10})
        for query in ("must not be injected", "future global facts")
    ]
    for unavailable in future_results:
        assert unavailable["records"] == []
        assert unavailable["notice"] == "当前可见范围内没有找到匹配记录。可以换个关键词或说法再试。"
    for hidden_id in (unscoped["record_id"], foreign["record_id"], future_global["record_id"], undated_global["record_id"]):
        try:
            invoke_research_tool(temp_settings, {"scope": "simulation", "practice_id": "p1", "revealed_date": as_of}, "read_research_record", {"record_id": hidden_id})
        except ResearchError as exc:
            assert exc.reason == "record_not_found"
        else:
            raise AssertionError(f"Agent read exposed an unknown or future record: {hidden_id}")


def test_plan_bounds_and_candidate_whitelist():
    valid = validate_plan(_plan())
    assert valid["budget"]["estimated_logical_steps"] == 10
    assert validate_plan(_plan(budget={"wall_seconds": 240, "max_bytes": 1024}))["budget"]["wall_seconds"] == 240
    for bad in (
        _plan(candidates=[5, 10, 20, 25]),
        _plan(training_dates=["20250102", "20250106"], evaluation_dates=["20250106"]),
        _plan(training_dates=[f"2025{month:02d}01" for month in range(1, 14)] * 30),
        _plan(budget={"wall_seconds": 301, "max_bytes": 1024}),
    ):
        try:
            validate_plan(bad)
        except ResearchError:
            pass
        else:
            raise AssertionError("invalid plan was accepted")


def test_frozen_plan_idempotency_is_atomic_across_sqlite_connections(temp_settings):
    app = FastAPI()
    app.state.settings = temp_settings
    app.include_router(create_research_router())
    body = {"idempotency_key": "plan-concurrent-retry", **_plan()}
    start = threading.Barrier(3)

    with TestClient(app) as client:
        def post_plan():
            start.wait(timeout=5)
            return client.post("/api/research/plans", json=body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(post_plan) for _ in range(2)]
            start.wait(timeout=5)
            responses = [request.result(timeout=10) for request in requests]

        assert [response.status_code for response in responses] == [200, 200]
        plans = [response.json()["plan"] for response in responses]
        assert plans[0]["plan_id"] == plans[1]["plan_id"]

        conflict = client.post(
            "/api/research/plans",
            json={**body, "candidates": [20]},
        )
        assert conflict.status_code == 409

    with research_connect(temp_settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_plan").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM research_idempotency WHERE operation='create_plan'").fetchone()[0] == 1


def test_research_plan_api_waits_for_held_write_lock_before_commit(temp_settings, monkeypatch):
    with research_connect(temp_settings):
        pass
    path = research_path(temp_settings)
    first = sqlite3.connect(path, timeout=2.0, isolation_level=None)
    begin_attempted = threading.Event()
    begin_acquired = threading.Event()
    wait_seconds = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                begin_attempted.set()
                started = time.monotonic()
                result = super().execute(sql, parameters)
                wait_seconds.append(time.monotonic() - started)
                begin_acquired.set()
                return result
            return super().execute(sql, parameters)

    def observed_connect(settings):
        conn = sqlite3.connect(
            research_path(settings), timeout=2.0, isolation_level=None,
            factory=ObservedConnection,
        )
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(research_api, "research_connect", observed_connect)
    app = FastAPI()
    app.state.settings = temp_settings
    app.include_router(create_research_router())
    body = {"idempotency_key": "plan-lock-wait", **_plan()}

    # Keep A's write transaction open while the actual POST route reaches its
    # production create_plan BEGIN IMMEDIATE call through connection B.
    first.execute("BEGIN IMMEDIATE")
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pending = pool.submit(client.post, "/api/research/plans", json=body)
            assert begin_attempted.wait(timeout=2), "API did not reach create_plan's write-lock call"
            time.sleep(0.25)
            assert not begin_acquired.is_set(), "connection B acquired A's held write lock"
            assert not pending.done(), "API response completed while B was still waiting"
            first.commit()
            assert begin_acquired.wait(timeout=2)
            response = pending.result(timeout=2)
            assert response.status_code == 200, response.text
            assert wait_seconds and wait_seconds[0] >= 0.2
        finally:
            if first.in_transaction:
                first.rollback()
            first.close()

    with research_connect(temp_settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_plan").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM research_idempotency WHERE operation='create_plan'").fetchone()[0] == 1


def test_research_variant_metrics_round_exact_integer_inputs_without_float():
    nav_cents = 20_000_000_000_000_001
    notional_cents = 3_000_000_000_000
    exact_turnover = Decimal(notional_cents) * Decimal(10_000) / Decimal(nav_cents)
    assert exact_turnover == Decimal("1.499999999999999925")
    assert exact_turnover < Decimal("1.5")
    # The old int/float division rounded this value to exactly 1.5; this is the
    # negative control showing why the monetary inputs must stay as Decimal.
    assert notional_cents * 10_000 / nav_cents == 1.5

    result = _summarize_variant_metrics(
        [nav_cents, nav_cents],
        (1, 37, notional_cents),
        restrictions=["synthetic"],
        performance_method="fixture-v1",
    )
    expected_turnover = int(exact_turnover.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    assert expected_turnover == 1
    assert result == {
        "return_bps": 0,
        "drawdown_bps": 0,
        "cost_cents": 37,
        "trade_count": 1,
        "turnover_bps": expected_turnover,
        "restrictions": ["synthetic"],
        "performance_method": "fixture-v1",
    }


def test_failed_frozen_plan_response_write_rolls_back_for_retry(temp_settings):
    app = FastAPI()
    app.state.settings = temp_settings
    app.include_router(create_research_router())
    body = {"idempotency_key": "plan-retry-after-failure", **_plan()}

    with research_connect(temp_settings) as conn:
        conn.execute(
            "CREATE TRIGGER reject_plan_idempotency BEFORE INSERT ON research_idempotency "
            "WHEN NEW.operation='create_plan' BEGIN SELECT RAISE(ABORT, 'forced test failure'); END"
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.post("/api/research/plans", json=body)
        assert failed.status_code == 500

        with research_connect(temp_settings) as conn:
            assert conn.execute("SELECT COUNT(*) FROM research_plan").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM research_idempotency WHERE operation='create_plan'").fetchone()[0] == 0
            conn.execute("DROP TRIGGER reject_plan_idempotency")

        retried = client.post("/api/research/plans", json=body)
        assert retried.status_code == 200, retried.text

    with research_connect(temp_settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_plan").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM research_idempotency WHERE operation='create_plan'").fetchone()[0] == 1


def test_agent_research_legacy_visibility_fallback_is_read_only(temp_settings):
    path = research_path(temp_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ("rr_visible", "rev_visible", {
            "scope": "simulation", "evidence_as_of": "20250101",
            "source_refs": [{"owner_id": "practice-1"}], "goal": "legacy visible",
        }, "2024-12-31T12:00:00+00:00"),
        ("rr_foreign", "rev_foreign", {
            "scope": "simulation", "evidence_as_of": "20250101",
            "source_refs": [{"owner_id": "practice-2"}], "goal": "legacy foreign",
        }, "2024-12-31T12:00:00+00:00"),
        ("rr_spoof", "rev_spoof", {
            "scope": "simulation", "evidence_as_of": "20250101",
            "simulation_visible_as_of": "20991231",
            "source_refs": [{"owner_id": "practice-1"}], "goal": "legacy forged clock",
        }, "2025-01-03T12:00:00+00:00"),
        ("rr_global_future", "rev_global_future", {
            "scope": "global", "evidence_as_of": "20991231", "goal": "future global",
        }, "2024-12-31T12:00:00+00:00"),
        ("rr_global_visible", "rev_global_visible", {
            "scope": "global", "evidence_as_of": "20250101", "goal": "current global",
        }, "2024-12-31T12:00:00+00:00"),
    ]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE research_record(record_id TEXT PRIMARY KEY,kind TEXT NOT NULL,created_at TEXT NOT NULL,current_revision_id TEXT NOT NULL)")
        conn.execute("CREATE TABLE research_revision(revision_id TEXT PRIMARY KEY,record_id TEXT NOT NULL,parent_revision_id TEXT,body_json TEXT NOT NULL,body_sha256 TEXT NOT NULL,created_at TEXT NOT NULL)")
        for record_id, revision_id, body, created_at in rows:
            conn.execute("INSERT INTO research_record VALUES(?,?,?,?)", (record_id, "observation", created_at, revision_id))
            conn.execute("INSERT INTO research_revision VALUES(?,?,?,?,?,?)", (
                revision_id, record_id, None, json.dumps(body, sort_keys=True),
                hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(), created_at,
            ))
        conn.commit()

    before_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    context = {"scope": "simulation", "practice_id": "practice-1", "revealed_date": "20250102"}
    result = invoke_research_tool(temp_settings, context, "search_research_records", {"query": "", "limit": 20})
    ids = {item["record_id"] for item in result["records"]}
    assert ids == {"rr_visible", "rr_global_visible"}
    assert "notice" not in result
    assert invoke_research_tool(temp_settings, context, "read_research_record", {"record_id": "rr_visible"})["body"]["goal"] == "legacy visible"
    for hidden_id in ("rr_foreign", "rr_spoof", "rr_global_future"):
        with pytest.raises(ResearchError) as excinfo:
            invoke_research_tool(temp_settings, context, "read_research_record", {"record_id": hidden_id})
        assert excinfo.value.reason == "record_not_found"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_revision_visibility'").fetchone() is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_digest


def test_agent_research_query_does_not_initialize_missing_store(temp_settings):
    path = research_path(temp_settings)
    context = {"scope": "simulation", "practice_id": "practice-1", "revealed_date": "20250102"}
    with pytest.raises(ResearchError) as excinfo:
        invoke_research_tool(temp_settings, context, "search_research_records", {"query": "", "limit": 5})
    assert excinfo.value.reason == "research_database_missing"
    assert not path.exists()
    assert not path.parent.exists()


def test_agent_research_visibility_owner_mismatch_never_uses_legacy_fallback(temp_settings):
    body = {
        "scope": "simulation", "evidence_as_of": "20250101",
        "source_refs": [{"owner_id": "practice-1"}], "goal": "must not downgrade",
    }
    with research_connect(temp_settings) as conn:
        created = create_record(conn, {
            "kind": "observation", "goal": body["goal"], "hypothesis": "test",
            "disconfirming_condition": "test", **body,
        }, visibility={"owner_id": "practice-2", "visible_as_of": "20250101", "practice_revision": 1})
    context = {"scope": "simulation", "practice_id": "practice-1", "revealed_date": "20250102"}
    with pytest.raises(ResearchError) as excinfo:
        invoke_research_tool(temp_settings, context, "read_research_record", {"record_id": created["record_id"]})
    assert excinfo.value.reason == "record_not_found"
    assert invoke_research_tool(temp_settings, context, "search_research_records", {
        "query": "must not downgrade", "limit": 5,
    }) == {
        "records": [], "as_of": "20250102", "practice_id": "practice-1",
        "scope": "application_research_only",
        "notice": "当前可见范围内没有找到匹配记录。可以换个关键词或说法再试。",
    }


def test_agent_research_query_replays_visible_record_without_like_wildcards(temp_settings):
    context = {"scope": "simulation", "practice_id": "practice-search", "revealed_date": "20250102"}
    with research_connect(temp_settings) as conn:
        record = create_record(conn, {
            "kind": "synthetic_agent_acceptance_note",
            "goal": "M3queryXshouldnotmatchY",
            "hypothesis": "visible synthetic research record",
            "disconfirming_condition": "synthetic counterexample",
            "scope": "simulation",
            "evidence_as_of": "20250101",
            "source_refs": [{"owner_type": "practice", "owner_id": "practice-search"}],
        }, visibility={
            "owner_id": "practice-search", "visible_as_of": "20250101", "practice_revision": 1,
        })

    exact = invoke_research_tool(temp_settings, context, "search_research_records", {
        "query": "M3queryXshouldnotmatchY", "kind": "synthetic_agent_acceptance_note", "limit": 10,
    })["records"]
    assert [item["record_id"] for item in exact] == [record["record_id"]]
    detail = invoke_research_tool(temp_settings, context, "read_research_record", {
        "record_id": record["record_id"],
    })
    assert exact[0]["revision_id"] == detail["revision_id"] == record["revision_id"]

    # LIKE's '_' and '%' used to fabricate matches for absent literal substrings.
    for query in ("M3query_shouldnotmatchY", "M3query%shouldnotmatchY", r"M3query\XshouldnotmatchY"):
        result = invoke_research_tool(temp_settings, context, "search_research_records", {
            "query": query, "kind": "synthetic_agent_acceptance_note", "limit": 10,
        })
        assert result["records"] == []
        assert result["notice"] == "当前可见范围内没有找到匹配记录。可以换个关键词或说法再试。"


def test_exposure_assessment_ignores_version_and_marks_unknown(temp_settings):
    with research_connect(temp_settings) as conn:
        register_exposure(conn, owner_type="practice", owner_id="p1", source_instance_id="runtime",
                          source_code="synthetic", data_version_id="v1", symbols=["600000.sh"],
                          dates=["20250102"], purpose="exploration")
        seen = assess_exposure(conn, symbols=["600000.SH"], dates=["20250102"])
        register_exposure(conn, owner_type="practice", owner_id="p2", source_instance_id="runtime-2",
                          source_code="synthetic", data_version_id="v2", symbols=["SH.600000"],
                          dates=["20250102"], purpose="training")
        cross_version = assess_exposure(conn, symbols=["600000.SH"], dates=["20250102"])
        unseen = assess_exposure(conn, symbols=["600000.SH"], dates=["20250103"])
        conn.execute("INSERT INTO exposure_history VALUES('h1','600000.SH','20250103','unknown_history','unknown','legacy','now')")
        unknown = assess_exposure(conn, symbols=["600000.SH"], dates=["20250103"])
        conn.execute("INSERT INTO exposure_ledger VALUES('legacy-alias','practice','old','runtime','fixture','old-v','SH.600000','20250104','exploration','known','now')")
        legacy_alias = assess_exposure(conn, symbols=["600000.SH"], dates=["20250104"])
    assert seen["eligibility"] == "blocked_seen_or_unknown"
    assert cross_version["eligibility"] == "blocked_seen_or_unknown"
    assert cross_version["overlap_count"] == 1
    assert unseen["eligibility"] == "eligible_unseen_in_application"
    assert unknown["unknown_count"] == 1
    assert legacy_alias["eligibility"] == "blocked_seen_or_unknown"
    assert legacy_alias["overlap_count"] == 1


def test_security_aliases_unknown_codes_and_20_by_252_exposure_query(temp_settings):
    assert normalize_symbol("sh.600000") == normalize_symbol("600000.SH") == "600000.SH"
    assert normalize_symbol("sz.000001") == normalize_symbol("000001.SZ") == "000001.SZ"
    for invalid in ("600000.HK", "600000.SZ", "ABC", "sh.600000.XX", "000001.SH"):
        try:
            normalize_symbol(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unknown or mismatched code accepted: {invalid}")
    symbols = [f"{600000 + index:06d}.SH" for index in range(20)]
    start = datetime(2025, 1, 1, tzinfo=timezone.utc).date()
    dates = [(start + timedelta(days=index)).strftime("%Y%m%d") for index in range(252)]
    with research_connect(temp_settings) as conn:
        register_exposure(conn, owner_type="practice", owner_id="scale", source_instance_id="runtime",
                          source_code="synthetic", data_version_id="v1", symbols=["sh.600019"],
                          dates=[dates[-1]], purpose="exploration")
        result = assess_exposure(conn, symbols=symbols, dates=dates)
    assert result["eligibility"] == "blocked_seen_or_unknown"
    assert result["overlap_count"] == 1


def test_legacy_practice_history_is_seeded_as_unknown_not_unseen(temp_settings):
    with practice_connect(temp_settings) as practice:
        practice.execute("INSERT INTO practice_session(practice_id,name,status,revision,start_date,end_date,revealed_date,data_version_id,source_code,segments_json,settings_json,user_state_json,created_at,updated_at) VALUES('legacy','legacy','finished',1,'20250102','20250110','20250106','v-old','synthetic','{}','{}','{}','now','now')")
    with research_connect(temp_settings) as conn:
        result = assess_exposure(conn, symbols=["600000.SH"], dates=["20250106"])
    assert result["eligibility"] == "blocked_seen_or_unknown"
    assert result["unknown_count"] > 0


def test_simulation_research_is_visible_at_server_bound_revealed_date(temp_settings):
    with practice_connect(temp_settings) as practice:
        for practice_id in ("sim-a", "sim-b"):
            practice.execute(
                "INSERT INTO practice_session(practice_id,name,status,revision,start_date,end_date,revealed_date,data_version_id,source_code,segments_json,settings_json,user_state_json,created_at,updated_at) VALUES(?,?,'active',1,'20241101','20241231','20241119','v1','synthetic','[]','{}','{}','now','now')",
                (practice_id, practice_id),
            )
    app = FastAPI()
    app.state.settings = temp_settings
    app.include_router(create_research_router())
    with TestClient(app) as client:
        created_response = client.post("/api/research/records", json={
            "idempotency_key": "sim-record-day-1", "scope": "simulation", "kind": "hypothesis",
            "goal": "same-day record", "hypothesis": "visible immediately at the revealed date",
            "disconfirming_condition": "counterexample", "evidence_as_of": "20241119",
            "source_refs": [{"owner_type": "practice", "owner_id": "sim-a", "data_version_id": "v1"}],
        })
        assert created_response.status_code == 200, created_response.text
        created = created_response.json()["record"]
        same_day = client.get(f"/api/research/records/{created['record_id']}?as_of=20241119&practice_id=sim-a")
        assert same_day.status_code == 200, same_day.text
        same_day_search = client.get("/api/research/records?q=same-day&as_of=20241119&practice_id=sim-a")
        assert same_day_search.json()["total"] == 1

        with practice_connect(temp_settings) as practice:
            practice.execute("UPDATE practice_session SET revealed_date='20241120',revision=revision+1 WHERE practice_id='sim-a'")
        revised = client.post(f"/api/research/records/{created['record_id']}/revisions", json={
            "idempotency_key": "sim-record-day-2", "scope": "simulation", "kind": "hypothesis",
            "goal": "revised after advance", "hypothesis": "new revision",
            "disconfirming_condition": "counterexample", "evidence_as_of": "20241120",
            "source_refs": [{"owner_type": "practice", "owner_id": "sim-a", "data_version_id": "v1"}],
        })
        assert revised.status_code == 200, revised.text
        past = client.get(f"/api/research/records/{created['record_id']}?as_of=20241119&practice_id=sim-a")
        current = client.get(f"/api/research/records/{created['record_id']}?as_of=20241120&practice_id=sim-a")
        foreign = client.get(f"/api/research/records/{created['record_id']}?as_of=20241120&practice_id=sim-b")
        assert past.status_code == current.status_code == 200
        assert past.json()["record"]["body"]["hypothesis"] == "visible immediately at the revealed date"
        assert current.json()["record"]["body"]["hypothesis"] == "new revision"
        assert foreign.status_code == 404
        assert client.get(f"/api/research/records/{created['record_id']}?as_of=20241121&practice_id=sim-a").status_code == 404
        forged = client.post("/api/research/records", json={
            "idempotency_key": "sim-forged-visible-date", "scope": "simulation", "kind": "hypothesis",
            "goal": "forged clock", "hypothesis": "backdated", "disconfirming_condition": "counterexample",
            "evidence_as_of": "20241120", "simulation_visible_as_of": "20240101",
            "source_refs": [{"owner_type": "practice", "owner_id": "sim-a"}],
        })
        assert forged.status_code == 200, forged.text
        assert "simulation_visible_as_of" not in forged.json()["record"]
        assert client.get(f"/api/research/records/{forged.json()['record']['record_id']}?as_of=20241119&practice_id=sim-a").status_code == 404
        with research_connect(temp_settings) as research:
            legacy = create_record(research, {
                "scope": "simulation", "kind": "hypothesis", "goal": "legacy forged field",
                "hypothesis": "untrusted JSON clock", "disconfirming_condition": "counterexample",
                "evidence_as_of": "20241119", "simulation_visible_as_of": "20241119",
                "source_refs": [{"owner_type": "practice", "owner_id": "sim-a"}],
            })
        assert client.get(f"/api/research/records/{legacy['record_id']}?as_of=20241119&practice_id=sim-a").status_code == 404


def test_agent_research_tools_are_scoped_and_draft_only(temp_settings):
    # Production bootstrap creates the store; a read-only Agent tool must not.
    research_connect(temp_settings).close()
    context = {"scope": "simulation", "practice_id": "p1", "revealed_date": "20250105"}
    prepared = invoke_research_tool(temp_settings, context, "prepare_experiment_draft", {"plan": _plan()})
    assert prepared["starts_experiment"] is False
    assert prepared["discloses_evaluation"] is False
    assert invoke_research_tool(temp_settings, context, "search_research_records", {"query": "", "limit": 5})["records"] == []


def test_rebalance_parameter_changes_existing_engine_schedule():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE valuation_snapshot(owner_type TEXT, owner_id TEXT, as_of TEXT)")
    for index in range(1, 12):
        conn.execute("INSERT INTO valuation_snapshot VALUES('simulation_segment','seg',?)", (f"202501{index:02d}",))
        session = {"start_date": "20250101", "settings": {"rebalance_every_n_days": 5}}
        five = _next_rebalance_is_due(conn, session, "seg")
        session["settings"]["rebalance_every_n_days"] = 10
        ten = _next_rebalance_is_due(conn, session, "seg")
        if index == 6:
            assert five is True and ten is False
        if index == 11:
            assert ten is True
    conn.close()


def test_isolated_runner_uses_copy_and_reveals_only_final_evaluation(temp_settings):
    app = create_app(temp_settings)
    app.include_router(create_research_router())
    with TestClient(app) as client:
        demo = client.post("/api/practice/demo").json()
        version_id = demo["demo"]["version_id"]
        source_code = demo["demo"]["source_code"]
        with practice_connect(temp_settings) as practice:
            from app.data.chunks import VersionView
            calendar = VersionView(practice, version_id, expected_source_code=source_code).calendar_days()
        dates = calendar[:4]
        assert len(dates) == 4
        created = client.post("/api/practice/sessions", json={
            "name": "research runner fixture", "start_date": dates[0], "end_date": dates[3],
            "cash_cents": 100_000_000, "positions": [], "data_version_id": version_id,
            "source_code": source_code, "settings": {"hide_system_hints": False, "rebalance_every_n_days": 10},
            "idempotency_key": "research-runner-create",
        }).json()["session"]
        with practice_connect(temp_settings) as practice:
            training_date = _get_next_trade_date(practice, require_session(practice, created["practice_id"]))
            session_for_dates = require_session(practice, created["practice_id"])
            session_for_dates["revealed_date"] = training_date
            evaluation_date = _get_next_trade_date(practice, session_for_dates)
        practice_path = practice_settings(temp_settings).db_path
        digest_before = hashlib.sha256(practice_path.read_bytes()).hexdigest()
        source_context = capture_context(temp_settings, created["practice_id"])
        source_context["source_instance_id"] = hashlib.sha256(str(temp_settings.runtime_dir.resolve()).encode("utf-8")).hexdigest()
        plan = _plan(
            source_context=source_context,
            training_dates=[training_date], evaluation_dates=[evaluation_date], candidates=[10],
        )
        stored = client.post("/api/research/plans", json={**plan, "idempotency_key": "runner-plan"})
        assert stored.status_code == 200, stored.text
        result = client.post("/api/research/experiments", json={"plan_id": stored.json()["plan"]["plan_id"], "idempotency_key": "runner-start"})
        digest_after = hashlib.sha256(practice_path.read_bytes()).hexdigest()
        assert digest_before == digest_after
        assert result.status_code == 202, result.text
        op = result.json()["operation"]
        assert {"operation_id", "kind", "status", "attempt", "generation", "progress", "checkpoint", "cumulative_usage", "limits", "resumable", "stop_reason", "created_at", "updated_at"}.issubset(op)
        assert op["kind"] == "research_experiment"
        for _ in range(400):
            current = client.get(f"/api/research/experiments/{op['operation_id']}").json()["operation"]
            if current["status"] in {"completed", "failed", "interrupted", "cancelled"}:
                break
            time.sleep(0.025)
        assert current["status"] == "completed", current
        assert "evaluation" not in current and current["progress"]["phase"] == "completed"
        assert current["cumulative_usage"]["logical_steps"] == 3
        assert current["cumulative_usage"]["wall_seconds"] <= current["limits"]["wall_seconds"]
        completed_payload = client.get(f"/api/research/experiments/{op['operation_id']}").json()
        assert set(completed_payload["result"]["evaluation"]) == {"baseline", "selected"}
        assert len(completed_payload["operation"]["attempts"]) == 1
        assert completed_payload["operation"]["source_context_digest"]
        assert completed_payload["result"]["source_context_digest"]
        with research_connect(temp_settings) as research:
            from app.research.exposure import assess_exposure
            training_exposure = research.execute(
                "SELECT COUNT(*) FROM exposure_ledger WHERE owner_type='research_experiment' AND owner_id=? AND trade_date=? AND purpose='training'",
                (op["operation_id"], training_date),
            ).fetchone()[0]
            assert training_exposure > 0
            with practice_connect(temp_settings) as practice:
                symbols = [row[0] for row in practice.execute(
                    "SELECT ts_code FROM pool_member WHERE snapshot_id=?",
                    (created["settings"]["pool_snapshot_id"],),
                )]
            assert assess_exposure(research, symbols=symbols, dates=[training_date])["eligibility"] == "blocked_seen_or_unknown"
        tool_context = capture_context(temp_settings, created["practice_id"])
        names = {item["function"]["name"] for item in tool_definitions()}
        assert {"search_research_records", "read_research_record", "prepare_experiment_draft", "get_experiment_status"}.issubset(names)
        draft = execute_tool(temp_settings, tool_context, "prepare_experiment_draft", {"plan": plan})
        assert draft["data"]["starts_experiment"] is False
        try:
            invoke_research_tool(temp_settings, tool_context, "get_experiment_status", {"experiment_id": op["operation_id"]})
        except ResearchError as error:
            assert error.reason == "experiment_not_visible"
        else:
            raise AssertionError("an earlier simulation saw a later experiment")


def _frozen_plan(settings):
    with research_connect(settings) as conn:
        return create_plan(conn, _plan())


def test_research_operation_cancel_generation_fence_resume_budget_and_idempotency(temp_settings, monkeypatch):
    class HoldingExecutor:
        def __init__(self): self.jobs = []
        def submit(self, fn, *args): self.jobs.append((fn, args))
        def shutdown(self, **kwargs): pass
    holding = HoldingExecutor()
    monkeypatch.setattr(research_executor, "_executor", holding)
    monkeypatch.setattr(research_executor, "_settings", temp_settings)
    monkeypatch.setattr(research_executor, "_stopping", False)
    plan = _frozen_plan(temp_settings)
    operation = research_executor.create_operation(temp_settings, plan["plan_id"], {"plan_id": plan["plan_id"], "idempotency_key": "op-start"})
    assert operation["status"] == "queued" and operation["kind"] == "research_experiment"
    try:
        research_executor.create_operation(temp_settings, plan["plan_id"], {"plan_id": plan["plan_id"], "idempotency_key": "op-overlap"})
    except ResearchError as exc:
        assert exc.reason == "active_experiment_exists"
    else:
        raise AssertionError("single-instance executor admitted a concurrent operation")
    committed = {"sequence": 1, "steps": [{"step_id": "training:10", "state": "committed"}], "validation": "checkpointed"}
    with research_connect(temp_settings) as conn:
        conn.execute("UPDATE research_operation SET usage_json=? WHERE operation_id=?", (json.dumps({"wall_seconds": 23, "logical_steps": 4}), operation["operation_id"]))
        conn.execute("UPDATE research_operation SET checkpoint_json=? WHERE operation_id=?", (json.dumps(committed), operation["operation_id"]))
        conn.execute("INSERT INTO research_experiment(experiment_id,plan_id,status,status_json,created_at,updated_at,generation) VALUES(?,?,?,?,?,?,?)", (operation["operation_id"], plan["plan_id"], "running", json.dumps({"training_results": [{"candidate": 10, "return_bps": 10, "drawdown_bps": -1}]}), "now", "now", 1))
    cancelled = research_executor.cancel_operation(temp_settings, operation["operation_id"], 1)
    assert cancelled["status"] == "cancelled" and cancelled["generation"] == 2 and cancelled["resumable"]
    assert research_executor.cancel_operation(temp_settings, operation["operation_id"], 1) == cancelled
    with research_connect(temp_settings) as conn:
        stale = conn.execute("UPDATE research_experiment SET status='completed' WHERE experiment_id=? AND generation=1 AND EXISTS(SELECT 1 FROM research_operation WHERE operation_id=? AND generation=1 AND status='running')", (operation["operation_id"], operation["operation_id"])).rowcount
        assert stale == 0
    with research_executor._lock:
        research_executor._active.discard(operation["operation_id"])
    resumed = research_executor.resume_operation(temp_settings, operation["operation_id"], 2, "resume-1")
    assert resumed["status"] == "queued" and resumed["generation"] == 3 and resumed["attempt"] == 2
    assert resumed["cumulative_usage"] == {"logical_steps": 4, "wall_seconds": 23}
    assert resumed["checkpoint"] == committed
    replay = research_executor.resume_operation(temp_settings, operation["operation_id"], 2, "resume-1")
    assert replay["generation"] == 3 and len(holding.jobs) == 2
    with research_connect(temp_settings) as conn:
        conn.execute("UPDATE research_operation SET status='completed' WHERE operation_id=?", (operation["operation_id"],))
    with research_executor._lock:
        research_executor._active.discard(operation["operation_id"])
    exhausted = research_executor.create_operation(temp_settings, plan["plan_id"], {"plan_id": plan["plan_id"], "idempotency_key": "op-budget"})
    with research_connect(temp_settings) as conn:
        conn.execute("UPDATE research_operation SET usage_json=? WHERE operation_id=?", (json.dumps({"wall_seconds": 180, "logical_steps": 10}), exhausted["operation_id"]))
    research_executor.cancel_operation(temp_settings, exhausted["operation_id"], 1)
    with research_executor._lock:
        research_executor._active.discard(exhausted["operation_id"])
    try:
        research_executor.resume_operation(temp_settings, exhausted["operation_id"], 2, "resume-exhausted")
    except ResearchError as exc:
        assert exc.reason == "budget_exhausted"
    else:
        raise AssertionError("resume reset cumulative wall budget")


def test_research_operation_http_cancel_and_resume_routes(temp_settings, monkeypatch):
    class HoldingExecutor:
        def __init__(self): self.jobs = []
        def submit(self, fn, *args): self.jobs.append((fn, args))
        def shutdown(self, **kwargs): pass
    holding = HoldingExecutor()
    monkeypatch.setattr(research_executor, "_executor", holding)
    monkeypatch.setattr(research_executor, "_settings", temp_settings)
    monkeypatch.setattr(research_executor, "_stopping", False)
    plan = _frozen_plan(temp_settings)
    app = FastAPI()
    app.state.settings = temp_settings
    app.include_router(create_research_router())
    with TestClient(app) as client:
        started = client.post("/api/research/experiments", json={"plan_id": plan["plan_id"], "idempotency_key": "http-start"})
        assert started.status_code == 202
        op = started.json()["operation"]
        cancelled = client.post(f"/api/research/experiments/{op['operation_id']}/cancel", json={"expected_generation": 1})
        assert cancelled.json()["operation"]["status"] == "cancelled"
        cancelled_get = client.get(f"/api/research/experiments/{op['operation_id']}").json()
        assert cancelled_get["operation"]["generation"] == 2 and "result" not in cancelled_get
        with research_executor._lock:
            research_executor._active.discard(op["operation_id"])
        resumed = client.post(f"/api/research/experiments/{op['operation_id']}/resume", json={"expected_generation": 2, "idempotency_key": "http-resume"})
        assert resumed.status_code == 202
        assert resumed.json()["operation"]["attempt"] == 2
        replay = client.post(f"/api/research/experiments/{op['operation_id']}/resume", json={"expected_generation": 2, "idempotency_key": "http-resume"})
        assert replay.status_code == 202 and replay.json()["operation"]["generation"] == 3
        assert len(holding.jobs) == 2
    with research_executor._lock:
        research_executor._active.discard(op["operation_id"])


def test_failed_research_exposes_training_summary_and_attempt_history_only(temp_settings, monkeypatch):
    class HoldingExecutor:
        def submit(self, fn, *args): pass
        def shutdown(self, **kwargs): pass
    monkeypatch.setattr(research_executor, "_executor", HoldingExecutor())
    monkeypatch.setattr(research_executor, "_settings", temp_settings)
    monkeypatch.setattr(research_executor, "_stopping", False)
    plan = _frozen_plan(temp_settings)
    operation = research_executor.create_operation(temp_settings, plan["plan_id"],
        {"plan_id": plan["plan_id"], "idempotency_key": "failed-evidence-start"})
    summary = {"completed_training_candidates": [{"candidate": 10, "return_bps": 12}],
               "selected_candidate": 10, "evaluation": None}
    with research_connect(temp_settings) as conn:
        conn.execute("INSERT INTO research_experiment(experiment_id,plan_id,status,status_json,created_at,updated_at,generation) VALUES(?,?,?,?,?,?,?)",
                     (operation["operation_id"], plan["plan_id"], "failed",
                      json.dumps({"final_summary": summary, "source_context_digest": "a" * 64}), "now", "now", 1))
        conn.execute("UPDATE research_operation SET status='failed',resumable=1 WHERE operation_id=?", (operation["operation_id"],))
        conn.execute("UPDATE research_operation_attempt SET status='failed' WHERE operation_id=?", (operation["operation_id"],))
    result = research_executor.get_operation(temp_settings, operation["operation_id"])
    assert result["result"] == summary
    assert result["result"]["evaluation"] is None
    assert result["attempts"][0]["status"] == "failed"
    with research_executor._lock:
        research_executor._active.discard(operation["operation_id"])


def test_research_startup_marks_only_committed_checkpoints_resumable(temp_settings, monkeypatch):
    class IdleExecutor:
        def shutdown(self, **kwargs): pass
    with research_connect(temp_settings) as conn:
        plan = create_plan(conn, _plan())
        for op_id, checkpoint in (("safe-op", {"sequence": 1, "steps": [{"step_id": "training:10", "state": "committed"}], "validation": "checkpointed"}),
                                  ("unsafe-op", {"sequence": 1, "steps": [{"step_id": "evaluation", "state": "unknown"}], "validation": "not_checked"})):
            conn.execute("INSERT INTO research_operation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (op_id, op_id, plan["plan_id"], "running", 1, 1, "{}", json.dumps(checkpoint), json.dumps({"wall_seconds": 3, "logical_steps": 1}), json.dumps({"wall_seconds": 180, "estimated_logical_steps": 10}), 0, None, op_id, "sha", "now", "now"))
    monkeypatch.setattr(research_executor, "_executor", None)
    monkeypatch.setattr(research_executor, "_settings", None)
    monkeypatch.setattr(research_executor, "_stopping", False)
    research_executor.startup_research_executor(temp_settings)
    try:
        safe = research_executor.get_operation(temp_settings, "safe-op")
        unsafe = research_executor.get_operation(temp_settings, "unsafe-op")
        assert safe["status"] == unsafe["status"] == "interrupted"
        assert safe["resumable"] is True and unsafe["resumable"] is False
    finally:
        assert research_executor._executor is not None
        research_executor.shutdown_research_executor(wait=True)
        assert research_executor._executor is None


def test_maintenance_snapshot_is_monotonic_and_reports_real_references(temp_settings):
    with research_connect(temp_settings) as conn:
        create_plan(conn, _plan())
    first = maintenance_reference_snapshot(temp_settings)
    assert service_reference_snapshot(temp_settings) == first
    with research_connect(temp_settings) as conn:
        create_plan(conn, _plan(source_context={**_plan()["source_context"], "data_version_id": "v2"}))
    second = maintenance_reference_snapshot(temp_settings)
    assert second["generation"] > first["generation"]
    assert first["references"]["cache"] == ["v1"]
    assert second["references"]["cache"] == ["v1", "v2"]
    assert all(item["source_code"] == "synthetic" for item in second["reference_details"])


def test_maintenance_reference_snapshot_fails_closed_when_unreadable(temp_settings, monkeypatch):
    real_path = research_store.research_path(temp_settings)
    monkeypatch.setattr(research_store, "research_path", lambda settings: settings.runtime_dir / "missing-research.sqlite3")
    try:
        maintenance_reference_snapshot(temp_settings)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("unreadable research reference DB must not appear empty/safe")
    real_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(real_path).close()
    monkeypatch.setattr(research_store, "research_path", lambda settings: real_path)
    try:
        maintenance_reference_snapshot(temp_settings)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("unknown research schema must fail closed")
