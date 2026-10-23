from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app import __version__
from app.config import Settings, load_settings
from app.db import SCHEMA_VERSION
from app.main import create_app


def test_health_reports_process_liveness(temp_settings: Settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "service": "ai-quant-simulator",
        "name": "AI 量化模拟交易系统",
        "version": __version__,
        "mode": "local",
        "status": "ok",
    }


def test_ready_reports_database_metadata(temp_settings: Settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"]["status"] == "ready"
    assert body["database"]["schema_version"] == SCHEMA_VERSION
    assert body["database"]["instance_id"]
    assert body["database"]["created_at"]


def test_ready_returns_503_when_database_file_disappears(temp_settings: Settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        temp_settings.db_path.unlink()
        response = client.get("/api/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["database"]["reason"] == "database_missing"
    assert not temp_settings.db_path.exists()


def test_health_survives_corrupt_database_and_ready_reports_it(temp_settings: Settings) -> None:
    temp_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    original = b"not a database"
    temp_settings.db_path.write_bytes(original)

    with TestClient(create_app(temp_settings)) as client:
        health = client.get("/api/health")
        ready = client.get("/api/ready")

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["database"]["reason"] == "database_corrupt"
    assert temp_settings.db_path.read_bytes() == original


def test_ready_returns_503_for_incompatible_schema(temp_settings: Settings) -> None:
    temp_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(temp_settings.db_path) as conn:
        conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION + 5)),
                ("instance_id", "old-instance"),
                ("created_at", "2026-01-01T00:00:00+00:00"),
            ],
        )

    with TestClient(create_app(temp_settings)) as client:
        response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["database"]["reason"] == "schema_incompatible"


def test_system_status_is_honest_about_missing_features(temp_settings: Settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        response = client.get("/api/system/status")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "local"
    assert body["market_data"]["status"] == "not_connected"
    assert body["account"]["status"] == "available"
    assert body["account"]["net_value"] is None
    assert body["account"]["cumulative_return"] is None
    assert body["strategy"]["status"] == "available"
    assert body["scheduler"]["status"] == "not_implemented"
    assert body["ai"]["status"] == "not_configured"
    assert body["limitations"]
    serialized = response.text
    assert "100000" not in serialized
    assert "0.0" not in serialized


def test_startup_with_unwritable_database_keeps_health_available(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    settings = load_settings(
        {"APP_DB_PATH": str(blocker / "app.db")},
        project_root=tmp_path,
    )

    with TestClient(create_app(settings)) as client:
        health = client.get("/api/health")
        ready = client.get("/api/ready")

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["database"]["reason"] == "database_unavailable"


def test_ready_never_creates_database(temp_settings: Settings) -> None:
    app = create_app(temp_settings)
    # 不启动 lifespan（不执行初始化），直接检查 readiness 的只读行为。
    with TestClient(app) as client:
        temp_settings.db_path.unlink(missing_ok=True)
        response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["database"]["reason"] == "database_missing"
    assert not temp_settings.db_path.exists()
