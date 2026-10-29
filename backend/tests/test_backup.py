"""备份包：DB + 外部文件、侧库快照、路径安全与独立恢复验证。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.data.backup import create_backup, restore_package, verify_backup
from app.data.errors import DataError
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.agent_runtime.store import AgentStore
from app.main import create_app


def _seed(settings: Settings) -> None:
    initialize_database(settings)
    result = run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily"),
            start="20240102",
            end="20240105",
            codes=("000001.SZ",),
        ),
    )
    assert result.status == "completed"


def _write_schema1(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("instance_id", "legacy-instance"),
                ("created_at", "2024-01-01T00:00:00+00:00"),
            ],
        )


def test_backup_includes_external_files_and_restores_independently(data_settings: Settings) -> None:
    _seed(data_settings)
    # 侧库存在且持续可写。
    from app.data.maintenance import open_maintenance, start_run

    side_conn = open_maintenance(data_settings)
    start_run(side_conn, kind="test", from_version=None, to_version=None)
    side_conn.close()
    side = Path(data_settings.runtime_dir) / "maintenance.db"
    assert side.exists()
    backup = create_backup(data_settings)
    assert backup.verified is True
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    kinds = {entry["kind"] for entry in manifest["files"]}
    assert "raw" in kinds
    assert "side_db" in kinds
    for entry in manifest["files"]:
        assert entry["package_path"].startswith("external/") or entry["package_path"] == "maintenance.db"

    # 备份后继续写侧库，不得让旧包摘要失效。
    with sqlite3.connect(side) as conn:
        conn.execute(
            "INSERT INTO maintenance_run(run_id, kind, state, started_at, updated_at)"
            " VALUES ('after-backup', 'test', 'completed', 'x', 'x')"
        )
    assert verify_backup(backup.path)["verified"] is True

    target = Path(data_settings.project_root) / "restored"
    restored = restore_package(backup.path, target)
    assert restored["status"] == "restored"
    assert restored["external_files"] >= 1
    with sqlite3.connect(restored["database"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0] > 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_of_schema1_database_succeeds(temp_settings: Settings) -> None:
    _write_schema1(temp_settings.db_path)
    backup = create_backup(temp_settings)
    assert backup.verified is True
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    assert manifest["database"]["schema_version"] == "1"
    assert [entry for entry in manifest["files"] if entry["kind"] == "raw"] == []


def test_backup_rejects_path_escape(data_settings: Settings) -> None:
    _seed(data_settings)
    with sqlite3.connect(data_settings.db_path) as conn:
        conn.execute(
            "INSERT INTO raw_archive(digest, path, content_bytes, interface, source_code,"
            " fetched_at, first_batch_id) VALUES ('x', '../evil', 1, 'daily', 'tushare-fixture',"
            " '2024-01-01T00:00:00+00:00', 'b')"
        )
    with pytest.raises(DataError) as excinfo:
        create_backup(data_settings)
    assert excinfo.value.reason == "backup_unsafe_source"


def test_verify_detects_tampered_external_file(data_settings: Settings) -> None:
    _seed(data_settings)
    backup = create_backup(data_settings)
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    raw_entry = next(entry for entry in manifest["files"] if entry["kind"] == "raw")
    target = backup.path / raw_entry["package_path"]
    target.write_bytes(b"tampered")
    result = verify_backup(backup.path)
    assert result["verified"] is False
    target.unlink()
    missing = verify_backup(backup.path)
    assert missing["verified"] is False and "file_missing" in missing["reason"]


def test_restore_rejects_escaped_and_unknown_packages_without_partial_target(data_settings: Settings, tmp_path: Path) -> None:
    _seed(data_settings)
    backup = create_backup(data_settings)
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["package_path"] = "../outside.bin"
    backup.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "escaped-restore"
    assert verify_backup(backup.path)["verified"] is False
    with pytest.raises(DataError):
        restore_package(backup.path, target)
    assert not target.exists()

    manifest["files"][0]["package_path"] = "external/valid-looking"
    manifest["format"] = "backup-package-v999"
    backup.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_backup(backup.path)["reason"] == "backup_format_unsupported"


def test_legacy_v1_backup_restores_into_current_runtime_layout(data_settings: Settings, tmp_path: Path) -> None:
    _seed(data_settings)
    backup = create_backup(data_settings)
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    manifest["format"] = "backup-package-v1"
    manifest["database"].pop("restore_path", None)
    for entry in manifest["files"]:
        entry.pop("restore_path", None)
    backup.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_backup(backup.path)["verified"] is True
    target = tmp_path / "legacy-runtime"
    restore_package(backup.path, target)
    assert (target / "data" / "app.db").is_file()
    with sqlite3.connect(target / "data" / "app.db") as conn:
        referenced = conn.execute("SELECT path FROM raw_archive LIMIT 1").fetchone()[0]
    assert (target / "data" / "raw" / referenced).is_file()


def test_full_runtime_backup_restores_and_resumes_business_in_new_directory(data_settings: Settings, tmp_path: Path) -> None:
    _seed(data_settings)
    from fastapi.testclient import TestClient

    with TestClient(create_app(data_settings)) as client:
        demo = client.post("/api/practice/demo")
        assert demo.status_code == 200, demo.text
        from app.data.chunks import VersionView
        from app.practice.runtime import practice_connect
        demo_source = demo.json()["demo"]
        practice_conn_for_calendar = practice_connect(data_settings)
        try:
            calendar = VersionView(practice_conn_for_calendar, demo_source["version_id"],
                                   expected_source_code=demo_source["source_code"]).calendar_days()
        finally:
            practice_conn_for_calendar.close()
        dates = calendar[:5]
        session_response = client.post("/api/practice/sessions", json={
            "name": "backup recovery simulation", "start_date": dates[0], "end_date": dates[-1],
            "cash_cents": 100_000, "positions": [], "data_version_id": demo_source["version_id"],
            "source_code": demo_source["source_code"], "settings": {"hide_system_hints": False},
            "idempotency_key": "backup-practice-session-1",
        })
        assert session_response.status_code == 200, session_response.text
        practice_id = session_response.json()["session"]["practice_id"]
        created = client.post("/api/research/records", json={
            "idempotency_key": "backup-record-1", "scope": "global", "kind": "hypothesis",
            "goal": "preserve a local record", "hypothesis": "restored history remains available",
            "disconfirming_condition": "record missing after restore", "evidence_as_of": "20250101",
            "source_refs": [],
        })
        assert created.status_code == 200, created.text

    from app.practice.runtime import practice_settings
    practice_config = practice_settings(data_settings)
    practice_raw = practice_config.raw_archive_dir / "synthetic" / "practice.json"
    practice_raw.parent.mkdir(parents=True, exist_ok=True)
    practice_raw.write_bytes(b"practice-raw-snapshot")
    import hashlib
    practice_digest = hashlib.sha256(practice_raw.read_bytes()).hexdigest()
    practice_conn = sqlite3.connect(practice_config.db_path)
    try:
        practice_conn.execute(
            "INSERT INTO raw_archive(digest,path,content_bytes,interface,source_code,fetched_at,first_batch_id)"
            " VALUES(?,?,?,?,?,?,?)",
            (practice_digest, "synthetic/practice.json", practice_raw.stat().st_size,
             "daily", "synthetic", "2025-01-01T00:00:00+00:00", "practice-batch"),
        )
        practice_conn.commit()
    finally:
        practice_conn.close()

    task_store = AgentStore(data_settings.runtime_dir / "agent" / "tasks.sqlite3")
    task_store.initialize()
    task, _ = task_store.create(
        key="backup-task-1", payload={"goal": "synthetic"}, practice_id=None, scope="setup",
        goal="synthetic", config_version="cfg-1", context={"scope": "setup", "context_fingerprint": "fp", "limitations": []},
        consent_digest="a" * 64, limits={"rounds": 2, "tool_calls": 1, "tokens": 100},
    )
    task_store.append_event(task["task_id"], "tool_completed", "Synthetic evidence persisted.",
                            "synthetic_tool", ["ev-backup-synthetic"])
    (data_settings.runtime_dir / "research" / "experiments" / "exp-synthetic" / "candidate-1").mkdir(parents=True)
    (data_settings.runtime_dir / "research" / "experiments" / "exp-synthetic" / "candidate-1" / "result.json").write_text(
        '{"sample":"offline"}', encoding="utf-8"
    )
    secret_dir = data_settings.runtime_dir / "credentials"
    secret_dir.mkdir(parents=True)
    (secret_dir / "secret-synthetic.dpapi").write_bytes(b"synthetic-secret-must-not-back-up")
    gateway_secret = data_settings.runtime_dir / "agent" / "gateway" / "secrets.dpapi"
    gateway_secret.parent.mkdir(parents=True, exist_ok=True)
    gateway_secret.write_bytes(b"synthetic-legacy-secret-must-not-back-up")

    backup = create_backup(data_settings)
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    package_paths = {entry["package_path"] for entry in manifest["files"]}
    assert {"practice/app.db", "research/research.sqlite3", "agent/tasks.sqlite3"} <= package_paths
    assert any(entry["kind"] == "runtime_file" and entry["path"].endswith("result.json") for entry in manifest["files"])
    assert any(entry.get("restore_path") == "practice/data/raw/synthetic/practice.json" for entry in manifest["files"])
    assert not any("credentials" in path.lower() or "secrets.dpapi" in path.lower() for path in package_paths)
    for packaged_file in backup.path.rglob("*"):
        if packaged_file.is_file():
            payload = packaged_file.read_bytes()
            assert b"synthetic-secret-must-not-back-up" not in payload
            assert b"synthetic-legacy-secret-must-not-back-up" not in payload

    target = tmp_path / "restored-runtime"
    restored = restore_package(backup.path, target)
    assert restored["database"] == str(target / "data" / "app.db")
    assert (target / "practice" / "app.db").is_file()
    assert (target / "research" / "research.sqlite3").is_file()
    assert (target / "practice" / "data" / "raw" / "synthetic" / "practice.json").read_bytes() == b"practice-raw-snapshot"
    assert (target / "research" / "experiments" / "exp-synthetic" / "candidate-1" / "result.json").read_text(encoding="utf-8") == '{"sample":"offline"}'
    assert not (target / "credentials").exists()
    assert not (target / "agent" / "gateway" / "secrets.dpapi").exists()

    resumed_settings = load_settings({"APP_RUNTIME_DIR": str(target)}, project_root=tmp_path)
    offline_copy = data_settings.runtime_dir.with_name("source-runtime-offline")
    import gc
    gc.collect()
    data_settings.runtime_dir.rename(offline_copy)
    try:
        with TestClient(create_app(resumed_settings)) as client:
            assert client.get("/api/health").status_code == 200
            records = client.get("/api/research/records").json()
            assert records["total"] == 1
            next_record = client.post("/api/research/records", json={
                "idempotency_key": "backup-record-2", "scope": "global", "kind": "hypothesis",
                "goal": "continue after restore", "hypothesis": "writes remain available",
                "disconfirming_condition": "restored runtime rejects writes", "evidence_as_of": "20250102",
                "source_refs": [],
            })
            assert next_record.status_code == 200, next_record.text
            assert client.get("/api/practice/options").status_code == 200
            sessions = client.get("/api/practice/sessions").json()
            assert sessions["total"] == 1
            assert sessions["sessions"][0]["practice_id"] == practice_id
        restored_tasks = AgentStore(target / "agent" / "tasks.sqlite3")
        assert restored_tasks.get(task["task_id"])["goal"] == "synthetic"
        events, _cursor = restored_tasks.events(task["task_id"], 0, 10)
        assert any(
            event["kind"] == "tool_completed" and event["evidence_ids"] == ["ev-backup-synthetic"]
            for event in events
        )
    finally:
        offline_copy.rename(data_settings.runtime_dir)
