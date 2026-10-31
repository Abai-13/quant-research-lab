import threading
import json
import time

from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.agent_runtime.store import AgentStore
from app.practice_data import operations as data_operations
from app.practice_data.operations import ensure_operation_schema
from app.practice.runtime import practice_connect, practice_settings
from app.research import service as research_service
from app.research import executor as research_executor
from app.data.backup import create_backup, restore_package
from app.data.migration import initialize_database


def make_task(store):
    view, _ = store.create(key="recovery-key-001", payload={"goal": "g"}, practice_id=None,
                           scope="setup", goal="g", config_version="cfg-1", context={
                               "scope": "setup", "context_fingerprint": "fp", "limitations": []},
                           consent_digest="a" * 64, limits={"rounds": 2, "tool_calls": 1, "tokens": 100})
    assert store.start(view["task_id"])
    return store.get(view["task_id"])


def test_crash_marks_inflight_model_request_unknown_and_refuses_resend(tmp_path):
    path = tmp_path / "agent.sqlite3"
    store = AgentStore(path)
    store.initialize()
    task = make_task(store)
    assert store.checkpoint_step(task["task_id"], task["generation"], "model:1", "pending", kind="external_model")

    recovered = AgentStore(path)
    assert recovered.initialize() == 1
    current = recovered.get(task["task_id"])
    assert current["status"] == "interrupted"
    assert current["generation"] == task["generation"] + 1
    assert current["checkpoint"]["steps"][0]["state"] == "unknown"
    assert current["checkpoint"]["steps"][0]["reason"] == "external_result_unknown"
    assert current["resumable"] is False
    assert recovered.claim_resume(task["task_id"], current["generation"],
                                  idempotency_key="resume-key-001", payload={"consent": True}) is None


def test_cancel_generation_fences_late_checkpoint_and_usage(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    store.initialize()
    task = make_task(store)
    cancelled = store.cancel(task["task_id"], task["generation"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["generation"] == task["generation"] + 1
    assert not store.checkpoint_step(task["task_id"], task["generation"], "tool:late", "committed")
    store.persist_elapsed(task["task_id"], task["generation"], 125, 125)
    assert store.get(task["task_id"])["usage"]["elapsed_ms"] == 125
    assert store.get(task["task_id"])["usage"]["attempt_elapsed_ms"] == 125


def test_resume_lease_is_single_winner_and_generation_checked(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    store.initialize()
    task = make_task(store)
    assert store.transition(task["task_id"], ("running",), "interrupted", reason="process_restarted",
                            generation=task["generation"])
    # Explicitly no steps or usage were committed, so user-confirmed retry is safe.
    outcomes = []
    def resume():
        outcomes.append(store.claim_resume(task["task_id"], task["generation"],
                                           idempotency_key="resume-key-001", payload={"consent": True}))
    threads = [threading.Thread(target=resume) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    winners = [outcome for outcome in outcomes if outcome is not None]
    assert len(winners) == 2
    assert sum(1 for _, created in winners if created) == 1
    resumed = winners[0][0]
    assert resumed["status"] == "queued"
    assert resumed["attempt"] == task["attempt"] + 1
    assert resumed["generation"] == task["generation"] + 1


def test_operation_view_hides_checkpoint_payload_details(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    store.initialize()
    task = make_task(store)
    view = store.get(task["task_id"])
    assert "context" in view
    assert view["checkpoint"] == {"sequence": 0, "steps": [], "validation": "not_checked"}


def test_backup_includes_agent_tasks_but_excludes_gateway_credentials(data_settings, tmp_path):
    initialize_database(data_settings)
    agent_path = data_settings.runtime_dir / "agent" / "tasks.sqlite3"
    store = AgentStore(agent_path)
    store.initialize()
    make_task(store)
    credential_dir = data_settings.runtime_dir / "agent" / "gateway"
    credential_dir.mkdir(parents=True, exist_ok=True)
    (credential_dir / "credential.bin").write_bytes(b"must-not-back-up")

    backup = create_backup(data_settings)
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    packaged = {entry["package_path"] for entry in manifest["files"]}
    assert "agent/tasks.sqlite3" in packaged
    assert not any("credential" in path or "gateway" in path for path in packaged)
    restored = restore_package(backup.path, tmp_path / "restore")
    assert (tmp_path / "restore" / "agent" / "tasks.sqlite3").is_file()


def test_data_executor_survives_sequential_app_lifespans(tmp_path):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    for index in range(2):
        with TestClient(create_app(settings)) as client:
            response = client.post("/api/practice/data/prepare", json={
                "source_code": "synthetic", "symbols": ["sh.600000"],
                "start_date": f"2024-02-0{index + 1}", "end_date": f"2024-02-0{index + 2}",
                "idempotency_key": f"lifespan-restart-{index:02d}",
            })
            assert response.status_code == 202, response.text
            operation_id = response.json()["operation_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                job = client.get(f"/api/practice/data/jobs/{operation_id}").json()
                if job["status"] in {"completed", "failed", "interrupted"}:
                    break
                time.sleep(0.02)
            assert job["status"] == "completed", job


def test_app_lifespan_starts_and_drains_research_executor(tmp_path, monkeypatch):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    calls = []
    monkeypatch.setattr(research_executor, "startup_research_executor",
                        lambda actual_settings: calls.append(("start", actual_settings)))
    monkeypatch.setattr(research_executor, "shutdown_research_executor",
                        lambda *, wait: calls.append(("stop", wait)))
    with TestClient(create_app(settings)):
        pass
    assert calls == [("start", settings), ("stop", True)]


def test_app_lifespan_waits_for_research_executor_drain(tmp_path, monkeypatch):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    shutdown_started = threading.Event()
    release_shutdown = threading.Event()
    monkeypatch.setattr(research_executor, "startup_research_executor", lambda actual_settings: None)

    def blocking_shutdown(*, wait):
        assert wait is True
        shutdown_started.set()
        assert release_shutdown.wait(5)

    monkeypatch.setattr(research_executor, "shutdown_research_executor", blocking_shutdown)
    client = TestClient(create_app(settings))
    client.__enter__()
    errors = []

    def close_client():
        try:
            client.__exit__(None, None, None)
        except BaseException as exc:  # propagate teardown failures from the helper thread
            errors.append(exc)

    closer = threading.Thread(target=close_client)
    closer.start()
    try:
        assert shutdown_started.wait(5)
        assert closer.is_alive()
    finally:
        release_shutdown.set()
    closer.join(5)
    assert not closer.is_alive()
    assert errors == []


def test_maintenance_preview_uses_research_reference_adapter(tmp_path, monkeypatch):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    calls = []
    monkeypatch.setattr(research_service, "maintenance_reference_snapshot",
                        lambda actual_settings: (calls.append(actual_settings) or {
                            "generation": 7, "references": {"cache": []}, "reference_details": []}))
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/maintenance/preview")
        assert response.status_code == 200, response.text
        assert response.json()["unknown_references"] is True
    assert calls == [settings]


def test_data_executor_last_shutdown_drains_before_next_generation():
    started = threading.Event()
    release = threading.Event()
    closing = threading.Event()
    shutdown_done = threading.Event()

    def blocked_job():
        started.set()
        assert release.wait(5)

    data_operations.startup_operations()
    future = data_operations._submit(blocked_job)
    assert started.wait(5)
    def close_generation():
        closing.set()
        data_operations.shutdown_operations(wait=False)
        shutdown_done.set()

    closer = threading.Thread(target=close_generation)
    closer.start()
    try:
        assert closing.wait(5)
        assert not shutdown_done.wait(0.05)
    finally:
        release.set()
    closer.join(5)
    assert shutdown_done.is_set()
    assert future.done()
    data_operations.startup_operations()
    assert data_operations._submit(lambda: "next generation").result(timeout=5) == "next generation"
    data_operations.shutdown_operations(wait=True)


def test_practice_data_startup_leaves_missing_database_absent(tmp_path):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    assert not practice_settings(settings).db_path.exists()
    data_operations.startup_operations(settings)
    try:
        assert not practice_settings(settings).db_path.exists()
    finally:
        data_operations.shutdown_operations(wait=True)


def test_practice_data_startup_requeues_persisted_inflight_job_idempotently(tmp_path, monkeypatch):
    settings = load_settings({"APP_RUNTIME_DIR": str(tmp_path / "runtime")}, project_root=tmp_path)
    with practice_connect(settings) as conn:
        ensure_operation_schema(conn)
        conn.execute("INSERT INTO practice_data_job(job_id,request_sha256,idempotency_key,request_json,status,total_units,generation,timeout_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     ("job-recovery", "sha", "key-recovery", json.dumps({"source_code": "synthetic", "symbols": ["sh.600000"]}), "running", 1, 4, 30, "now", "now"))
    submitted = []
    completed = threading.Event()

    def recovered_runner(*args):
        submitted.append(args)
        completed.set()

    monkeypatch.setattr(data_operations, "_run_job", recovered_runner)
    data_operations.startup_operations(settings)
    data_operations.startup_operations(settings)
    try:
        assert completed.wait(5)
        with practice_connect(settings) as conn:
            row = conn.execute("SELECT status,generation FROM practice_data_job WHERE job_id='job-recovery'").fetchone()
            assert tuple(row) == ("queued", 5)
        assert len(submitted) == 1
        assert submitted[0][1] == "job-recovery" and submitted[0][4] == 5
        assert submitted[0][5] is settings
    finally:
        data_operations.shutdown_operations(wait=True)
        data_operations.shutdown_operations(wait=True)
