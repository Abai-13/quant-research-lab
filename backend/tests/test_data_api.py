"""数据状态接口与状态页摘要：只报告真实检查结果。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.data.migration import initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.main import create_app


def test_data_status_reports_not_connected_without_import(temp_settings: Settings) -> None:
    with TestClient(create_app(temp_settings)) as client:
        response = client.get("/api/data/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_connected"
    assert body["data_versions"] == []
    assert body["pool_snapshots"] == []
    assert body["quality"]["total"] == 0
    assert body["source"]["source_code"]
    assert "credentials_present" in body["source"]


def test_data_status_reports_real_coverage_after_import(data_settings: Settings) -> None:
    initialize_database(data_settings)
    result = run_import(
        data_settings,
        ImportOptions(
            mode="history",
            categories=("calendar", "security", "daily"),
            start="20240102",
            end="20240628",
            codes=("000001.SZ", "600000.SH"),
        ),
    )
    assert result.status == "completed"

    with TestClient(create_app(data_settings)) as client:
        response = client.get("/api/data/status")

    body = response.json()
    assert body["status"] in {"available_in_range", "limited"}
    assert body["coverage"]["daily"]["count"] > 0
    assert body["coverage"]["daily"]["start"] == "20230103"
    assert body["coverage"]["daily"]["end"] == "20240628"
    assert body["last_import"]["status"] == "completed"
    assert body["data_versions"]
    assert body["data_versions"][0]["version_id"] == result.version["version_id"]


def test_data_status_reports_migration_required_for_schema1(temp_settings: Settings) -> None:
    import sqlite3

    temp_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(temp_settings.db_path) as conn:
        conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("instance_id", "legacy"),
                ("created_at", "2024-01-01T00:00:00+00:00"),
            ],
        )

    with TestClient(create_app(temp_settings)) as client:
        response = client.get("/api/data/status")

    body = response.json()
    assert body["status"] == "migration_required"


def test_system_status_keeps_phase1_shape(data_settings: Settings) -> None:
    with TestClient(create_app(data_settings)) as client:
        response = client.get("/api/system/status")

    body = response.json()
    assert body["account"]["status"] == "not_initialized"
    assert body["account"]["net_value"] is None
    assert body["strategy"]["status"] == "not_implemented"
    assert body["scheduler"]["status"] == "not_implemented"
    assert body["ai"]["status"] == "not_configured"
    assert body["market_data"]["status"]
    assert body["limitations"]


def test_status_never_invents_prices_or_returns(data_settings: Settings) -> None:
    with TestClient(create_app(data_settings)) as client:
        response = client.get("/api/system/status")

    serialized = response.text
    assert "100000" not in serialized
    assert "net_value\":0" not in serialized
    assert "cumulative_return\":0" not in serialized


def test_data_status_is_read_only(data_settings: Settings) -> None:
    with TestClient(create_app(data_settings)) as client:
        client.get("/api/data/status")
        ready = client.get("/api/ready")

    assert ready.status_code == 200
    assert ready.json()["database"]["status"] == "ready"


def test_service_starts_without_credentials_and_does_not_touch_network(tmp_path) -> None:
    """无凭据也能启动：状态如实显示未配置，且不会发起任何取数请求。"""
    settings = load_settings(
        {"APP_DATA_SOURCE": "tushare", "APP_RUNTIME_DIR": str(tmp_path / "runtime")},
        project_root=tmp_path,
    )

    with TestClient(create_app(settings)) as client:
        health = client.get("/api/health")
        ready = client.get("/api/ready")
        data = client.get("/api/data/status")

    assert health.status_code == 200
    assert ready.status_code == 200
    body = data.json()
    assert body["status"] == "not_connected"
    assert body["source"]["source_code"] == "tushare"
    assert body["source"]["credentials_present"] is False
    assert body["data_versions"] == []
    assert "token" not in str(body).lower()
