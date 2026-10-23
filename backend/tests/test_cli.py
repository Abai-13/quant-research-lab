"""手动操作入口：命令输出 JSON，凭据不进入输出。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.data.cli import main


def _run(capsys, settings: Settings, monkeypatch, argv: list[str]) -> dict:
    monkeypatch.setenv("APP_DATA_SOURCE", settings.data_source.source_code)
    monkeypatch.setenv("APP_RUNTIME_DIR", str(settings.runtime_dir))
    monkeypatch.setenv("APP_DATA_FIXTURE_DIR", str(settings.fixture_dir))
    monkeypatch.setenv("APP_IMPORT_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("APP_IMPORT_BACKOFF_SECONDS", "0,0")
    monkeypatch.delenv("APP_TUSHARE_TOKEN", raising=False)
    exit_code = main(argv)
    captured = capsys.readouterr()
    assert exit_code == 0, captured.out
    return json.loads(captured.out)


def test_source_check_lists_interfaces_without_credentials(
    data_settings: Settings, capsys, monkeypatch
) -> None:
    payload = _run(capsys, data_settings, monkeypatch, ["source-check"])

    assert payload["credential_check"] == "missing"
    assert payload["source"]["credentials_present"] is False
    interfaces = {item["interface"] for item in payload["interfaces"]}
    assert {"trade_cal", "stock_basic", "daily"} <= interfaces
    for item in payload["interfaces"]:
        assert item["permission_status"]
        assert item["evidence_date"]
        assert "token" not in json.dumps(payload).lower()


def test_status_command_reports_empty_state(data_settings: Settings, capsys, monkeypatch) -> None:
    payload = _run(capsys, data_settings, monkeypatch, ["status"])

    assert payload["status"] == "not_connected"


def test_import_quality_versions_and_pool_commands(
    data_settings: Settings, capsys, monkeypatch
) -> None:
    imported = _run(
        capsys,
        data_settings,
        monkeypatch,
        [
            "import-history",
            "--start",
            "20240102",
            "--end",
            "20240628",
            "--codes",
            "000001.SZ,600000.SH",
            "--categories",
            "calendar,security,daily,adj_factor",
        ],
    )
    assert imported["status"] == "completed"

    quality = _run(capsys, data_settings, monkeypatch, ["quality-check", "--as-of", "20240628"])
    assert quality["summary"]["rule_version"]

    versions = _run(capsys, data_settings, monkeypatch, ["versions"])
    assert versions["versions"]
    assert versions["versions"][0]["member_count"] > 0

    snapshot = _run(
        capsys,
        data_settings,
        monkeypatch,
        ["pool-snapshot", "--as-of", "20240628", "--min-history-days", "30",
         "--liquidity-window-days", "10", "--min-avg-amount", "1000000"],
    )
    assert snapshot["member_count"] >= 1
    assert snapshot["limit_flags"]

    eligibility = _run(
        capsys,
        data_settings,
        monkeypatch,
        ["eligibility", "--purpose", "display", "--as-of", "20240628"],
    )
    assert "eligible" in eligibility
    assert eligibility["purpose"] == "display"


def test_migrate_command_reports_status(data_settings: Settings, capsys, monkeypatch) -> None:
    import sqlite3

    data_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data_settings.db_path) as conn:
        conn.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("instance_id", "legacy"),
                ("created_at", "2024-01-01T00:00:00+00:00"),
            ],
        )

    payload = _run(capsys, data_settings, monkeypatch, ["migrate"])

    assert payload["status"] == "migrated"
    assert Path(payload["backup_path"]).is_file()
