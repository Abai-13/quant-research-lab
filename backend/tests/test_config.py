from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, load_settings


def test_defaults_are_local_and_anchored_to_project_root(tmp_path: Path) -> None:
    settings = load_settings({}, project_root=tmp_path)

    assert settings.backend_host == "127.0.0.1"
    assert settings.loops_back_only is True
    assert settings.backend_port == 8000
    assert settings.frontend_port == 5173
    assert settings.runtime_dir == tmp_path.resolve() / ".runtime"
    assert settings.db_path == tmp_path.resolve() / ".runtime" / "data" / "app.db"
    assert settings.log_dir == tmp_path.resolve() / ".runtime" / "logs"


def test_ports_are_configurable(tmp_path: Path) -> None:
    settings = load_settings(
        {"APP_BACKEND_PORT": "8100", "APP_FRONTEND_PORT": "5273"},
        project_root=tmp_path,
    )

    assert settings.backend_port == 8100
    assert settings.frontend_port == 5273


def test_relative_runtime_paths_resolve_against_project_root(tmp_path: Path) -> None:
    settings = load_settings(
        {"APP_RUNTIME_DIR": "var/runtime", "APP_DB_PATH": "custom/meta.db"},
        project_root=tmp_path,
    )

    assert settings.runtime_dir == (tmp_path / "var" / "runtime").resolve()
    assert settings.db_path == (tmp_path / "custom" / "meta.db").resolve()


def test_absolute_runtime_path_is_kept(tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere"
    settings = load_settings({"APP_RUNTIME_DIR": str(custom)}, project_root=tmp_path)

    assert settings.runtime_dir == custom.resolve()


@pytest.mark.parametrize("value", ["abc", "0", "65536", "-1", "80.5"])
def test_invalid_backend_port_fails(value: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings({"APP_BACKEND_PORT": value}, project_root=tmp_path)


def test_blank_port_uses_default(tmp_path: Path) -> None:
    settings = load_settings({"APP_BACKEND_PORT": "  "}, project_root=tmp_path)

    assert settings.backend_port == 8000


def test_invalid_frontend_port_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings({"APP_FRONTEND_PORT": "70000"}, project_root=tmp_path)


def test_empty_host_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings({"APP_BACKEND_HOST": "   "}, project_root=tmp_path)
