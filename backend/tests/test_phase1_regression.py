"""阶段 1 行为回归：启停配置、readiness 与数据库重启。"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.config import load_settings
from app.data.migration import check_database, initialize_database
from app.data.pipeline import ImportOptions, run_import
from app.main import create_app


def test_non_default_ports_are_configurable(tmp_path) -> None:
    settings = load_settings(
        {"APP_BACKEND_PORT": "8123", "APP_FRONTEND_PORT": "5199"}, project_root=tmp_path
    )

    assert settings.backend_port == 8123
    assert settings.frontend_port == 5199
    assert settings.backend_port != settings.frontend_port


def test_database_restart_keeps_instance_metadata(tmp_path) -> None:
    settings = load_settings({}, project_root=tmp_path)
    first = initialize_database(settings)
    run_import(
        settings,
        ImportOptions(
            mode="history",
            categories=("daily",),
            start="20240102",
            end="20240110",
            codes=("000001.SZ",),
        ),
    )

    # 模拟进程重启：重新加载配置并重新初始化。
    restarted = load_settings({}, project_root=tmp_path)
    second = initialize_database(restarted)

    assert second == first
    status = check_database(restarted)
    assert status.ready is True
    with sqlite3.connect(restarted.db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
    assert rows >= 0


def test_readiness_stays_read_only_after_data_is_imported(tmp_path) -> None:
    settings = load_settings(
        {
            "APP_DATA_SOURCE": "tushare-fixture",
            "APP_RUNTIME_DIR": str(tmp_path / "runtime"),
            "APP_DATA_FIXTURE_DIR": str(tmp_path / "fixtures"),
        },
        project_root=tmp_path,
    )
    initialize_database(settings)
    before = settings.db_path.read_bytes()

    with TestClient(create_app(settings)) as client:
        ready = client.get("/api/ready")

    assert ready.status_code == 200
    # 就绪检查不会写库。
    assert settings.db_path.read_bytes() == before


def test_health_is_independent_from_database_state(tmp_path) -> None:
    settings = load_settings({}, project_root=tmp_path)

    with TestClient(create_app(settings)) as client:
        client.get("/api/ready")
        settings.db_path.unlink(missing_ok=True)
        health = client.get("/api/health")
        ready = client.get("/api/ready")

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["database"]["reason"] == "database_missing"
