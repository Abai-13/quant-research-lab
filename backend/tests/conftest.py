from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, load_settings


@pytest.fixture
def temp_settings(tmp_path: Path) -> Settings:
    """每个测试使用独立的临时目录，避免触碰真实 .runtime。"""
    return load_settings({}, project_root=tmp_path)
