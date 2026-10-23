from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, load_settings

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def temp_settings(tmp_path: Path) -> Settings:
    """每个测试使用独立的临时目录，避免触碰真实 .runtime。"""
    return load_settings({}, project_root=tmp_path)


@pytest.fixture
def data_settings(tmp_path: Path) -> Settings:
    """离线夹具模式的数据层配置：只读本地夹具，不触网、不使用真实凭据。"""
    return load_settings(
        {
            "APP_DATA_SOURCE": "tushare-fixture",
            "APP_RUNTIME_DIR": str(tmp_path / "runtime"),
            "APP_DATA_FIXTURE_DIR": str(FIXTURES_DIR / "tushare"),
            "APP_IMPORT_MIN_INTERVAL_SECONDS": "0",
            "APP_IMPORT_BACKOFF_SECONDS": "0,0",
        },
        project_root=tmp_path,
    )


@pytest.fixture
def fixture_settings_factory(tmp_path: Path):
    """按夹具目录构造配置（异常样本、分页样本、错误样本）。"""

    def _factory(name: str, **overrides: str) -> Settings:
        values = {
            "APP_DATA_SOURCE": "tushare-fixture",
            "APP_RUNTIME_DIR": str(tmp_path / f"runtime-{name}"),
            "APP_DATA_FIXTURE_DIR": str(FIXTURES_DIR / name),
            "APP_IMPORT_MIN_INTERVAL_SECONDS": "0",
            "APP_IMPORT_BACKOFF_SECONDS": "0,0",
        }
        values.update(overrides)
        return load_settings(values, project_root=tmp_path)

    return _factory
