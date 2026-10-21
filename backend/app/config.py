"""本地运行配置。

配置来源优先级（从高到低）：
1. 进程环境变量；
2. 项目根目录下的 ``.env``（由 python-dotenv 读取，不修改进程环境）；
3. 代码内默认值。

所有相对路径都以项目根目录为基准，避免依赖调用命令时的当前目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173
DEFAULT_RUNTIME_DIR = PROJECT_ROOT / ".runtime"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(ValueError):
    """配置无效时抛出，消息可直接展示给本机用户。"""


@dataclass(frozen=True)
class Settings:
    project_root: Path
    backend_host: str
    backend_port: int
    frontend_port: int
    runtime_dir: Path
    db_path: Path
    log_dir: Path

    @property
    def loops_back_only(self) -> bool:
        return self.backend_host in LOOPBACK_HOSTS


def _read_environment(env_file: Path | None = None) -> dict[str, str]:
    """合并 .env 与进程环境；进程环境优先。"""
    values: dict[str, str] = {}
    path = env_file or (PROJECT_ROOT / ".env")
    if path.is_file():
        for key, value in dotenv_values(path).items():
            if value is not None:
                values[key] = value
    for key, value in os.environ.items():
        values[key] = value
    return values


def _parse_port(name: str, raw: str | None) -> int:
    if raw is None or not raw.strip():
        return {"APP_BACKEND_PORT": DEFAULT_BACKEND_PORT, "APP_FRONTEND_PORT": DEFAULT_FRONTEND_PORT}[name]
    text = raw.strip()
    try:
        port = int(text)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是 1-65535 之间的整数，当前值为 {text!r}") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name} 必须在 1-65535 之间，当前值为 {port}")
    return port


def _resolve_path(raw: str | None, default: Path, project_root: Path) -> Path:
    if raw is None or not raw.strip():
        return default
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def load_settings(values: Mapping[str, str] | None = None, project_root: Path | None = None) -> Settings:
    """加载并校验配置。

    ``values`` 为显式传入的配置映射时不再读取 .env 或进程环境，便于测试。
    """
    root = (project_root or PROJECT_ROOT).resolve()
    source: Mapping[str, str] = _read_environment() if values is None else values

    backend_host = (source.get("APP_BACKEND_HOST") or DEFAULT_BACKEND_HOST).strip()
    if not backend_host:
        raise ConfigError("APP_BACKEND_HOST 不能为空")

    backend_port = _parse_port("APP_BACKEND_PORT", source.get("APP_BACKEND_PORT"))
    frontend_port = _parse_port("APP_FRONTEND_PORT", source.get("APP_FRONTEND_PORT"))

    runtime_dir = _resolve_path(source.get("APP_RUNTIME_DIR"), root / ".runtime", root)
    db_path = _resolve_path(source.get("APP_DB_PATH"), runtime_dir / "data" / "app.db", root)
    log_dir = _resolve_path(source.get("APP_LOG_DIR"), runtime_dir / "logs", root)

    return Settings(
        project_root=root,
        backend_host=backend_host,
        backend_port=backend_port,
        frontend_port=frontend_port,
        runtime_dir=runtime_dir,
        db_path=db_path,
        log_dir=log_dir,
    )
