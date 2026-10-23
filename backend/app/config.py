"""本地运行配置。

配置来源优先级（从高到低）：
1. 进程环境变量；
2. 项目根目录下的 ``.env``（由 python-dotenv 读取，不修改进程环境）；
3. 代码内默认值。

所有相对路径都以项目根目录为基准，避免依赖调用命令时的当前目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173
DEFAULT_RUNTIME_DIR = PROJECT_ROOT / ".runtime"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

SUPPORTED_DATA_SOURCES = frozenset({"tushare", "tushare-fixture"})


class ConfigError(ValueError):
    """配置无效时抛出，消息可直接展示给本机用户。"""


@dataclass(frozen=True)
class ImportRetrySettings:
    """导入重试与限频边界（有界，可配置，不随结果放宽）。"""

    max_attempts_per_page: int = 3
    backoff_seconds_sequence: tuple[float, ...] = (1.0, 3.0)
    per_page_wait_budget_seconds: float = 60.0
    batch_wait_budget_seconds: float = 600.0
    min_interval_seconds: float = 0.35
    request_timeout_seconds: float = 30.0
    revision_window_days: int = 5
    max_pages_per_scope: int = 200
    lock_wait_seconds: float = 30.0
    working_window_years: int = 5
    warmup_trading_days: int = 20

    def __post_init__(self) -> None:
        if self.max_attempts_per_page < 1:
            raise ConfigError("导入重试次数不能少于 1")
        if self.per_page_wait_budget_seconds < 0 or self.batch_wait_budget_seconds < 0:
            raise ConfigError("导入等待预算不能为负")
        if self.min_interval_seconds < 0:
            raise ConfigError("限频间隔不能为负")
        if self.request_timeout_seconds <= 0:
            raise ConfigError("导入请求超时必须为正数")
        if self.revision_window_days < 0:
            raise ConfigError("修订重取窗口不能为负")
        if self.max_pages_per_scope < 1:
            raise ConfigError("单范围最大分页数不能少于 1")
        if self.lock_wait_seconds < 0:
            raise ConfigError("写入锁等待时间不能为负")
        if self.working_window_years < 1:
            raise ConfigError("工作窗口年数不能小于 1")
        if self.warmup_trading_days < 0:
            raise ConfigError("预热交易日数不能为负")


@dataclass(frozen=True)
class SourceSettings:
    """数据源配置；凭据不落入日志或报告。"""

    source_code: str
    api_base_url: str
    token_present: bool
    token: str = field(default="", repr=False)

    @property
    def is_real_source(self) -> bool:
        return self.source_code == "tushare"

    @property
    def is_fixture_source(self) -> bool:
        return self.source_code == "tushare-fixture"

    def public_summary(self) -> dict[str, object]:
        """可安全展示的来源摘要；绝不包含凭据内容。"""
        return {
            "source_code": self.source_code,
            "api_base_url": self.api_base_url if self.is_real_source else None,
            "credentials_present": self.token_present,
            "is_fixture": self.is_fixture_source,
        }


@dataclass(frozen=True)
class Settings:
    project_root: Path
    backend_host: str
    backend_port: int
    frontend_port: int
    runtime_dir: Path
    db_path: Path
    log_dir: Path
    backup_dir: Path
    raw_archive_dir: Path
    lock_dir: Path
    fixture_dir: Path
    data_source: SourceSettings
    import_retry: ImportRetrySettings

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


def _parse_backoff_sequence(raw: str | None) -> tuple[float, ...]:
    if raw is None or not raw.strip():
        return ImportRetrySettings.backoff_seconds_sequence[0:2]
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        return ImportRetrySettings.backoff_seconds_sequence[0:2]
    values: list[float] = []
    for part in parts:
        try:
            value = float(part)
        except ValueError as exc:
            raise ConfigError(f"APP_IMPORT_BACKOFF_SECONDS 必须为逗号分隔的数字: {raw!r}") from exc
        if value < 0:
            raise ConfigError("APP_IMPORT_BACKOFF_SECONDS 不能包含负数")
        values.append(value)
    return tuple(values)


def _resolve_path(raw: str | None, default: Path, project_root: Path) -> Path:
    if raw is None or not raw.strip():
        return default
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def _resolve_float(raw: str | None, default: float, name: str) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字: {raw!r}") from exc


def _resolve_positive_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数: {raw!r}") from exc
    if value < 1:
        raise ConfigError(f"{name} 不能小于 1")
    return value


def _resolve_non_negative_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数: {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{name} 不能为负")
    return value


_TOKEN_VARIABLES = ("APP_TUSHARE_TOKEN", "TUSHARE_TOKEN", "TUSHARE_PRO_TOKEN")


def _resolve_source_settings(source: Mapping[str, str]) -> SourceSettings:
    source_code = (source.get("APP_DATA_SOURCE") or "tushare").strip()
    if source_code not in SUPPORTED_DATA_SOURCES:
        raise ConfigError(
            f"APP_DATA_SOURCE 仅支持 {'、'.join(sorted(SUPPORTED_DATA_SOURCES))}，当前值 {source_code!r}"
        )
    api_base_url = (source.get("APP_TUSHARE_API_URL") or "https://api.tushare.pro").strip()
    token = ""
    for name in _TOKEN_VARIABLES:
        candidate = (source.get(name) or "").strip()
        if candidate:
            token = candidate
            break
    return SourceSettings(
        source_code=source_code,
        api_base_url=api_base_url,
        token_present=bool(token),
        token=token,
    )


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
    backup_dir = _resolve_path(source.get("APP_BACKUP_DIR"), runtime_dir / "backups", root)
    raw_archive_dir = _resolve_path(
        source.get("APP_DATA_RAW_DIR"), runtime_dir / "data" / "raw", root
    )
    lock_dir = _resolve_path(source.get("APP_LOCK_DIR"), runtime_dir / "locks", root)
    fixture_dir = _resolve_path(
        source.get("APP_DATA_FIXTURE_DIR"), root / "backend" / "tests" / "fixtures" / "tushare", root
    )
    data_source = _resolve_source_settings(source)
    import_retry = ImportRetrySettings(
        max_attempts_per_page=_resolve_positive_int(
            source.get("APP_IMPORT_MAX_ATTEMPTS"), 3, "APP_IMPORT_MAX_ATTEMPTS"
        ),
        backoff_seconds_sequence=_parse_backoff_sequence(source.get("APP_IMPORT_BACKOFF_SECONDS")),
        per_page_wait_budget_seconds=_resolve_float(
            source.get("APP_IMPORT_PAGE_WAIT_BUDGET"), 60.0, "APP_IMPORT_PAGE_WAIT_BUDGET"
        ),
        batch_wait_budget_seconds=_resolve_float(
            source.get("APP_IMPORT_BATCH_WAIT_BUDGET"), 600.0, "APP_IMPORT_BATCH_WAIT_BUDGET"
        ),
        min_interval_seconds=_resolve_float(
            source.get("APP_IMPORT_MIN_INTERVAL_SECONDS"), 0.35, "APP_IMPORT_MIN_INTERVAL_SECONDS"
        ),
        request_timeout_seconds=_resolve_float(
            source.get("APP_IMPORT_TIMEOUT_SECONDS"), 30.0, "APP_IMPORT_TIMEOUT_SECONDS"
        ),
        revision_window_days=_resolve_non_negative_int(
            source.get("APP_IMPORT_REVISION_WINDOW_DAYS"), 5, "APP_IMPORT_REVISION_WINDOW_DAYS"
        ),
        max_pages_per_scope=_resolve_positive_int(
            source.get("APP_IMPORT_MAX_PAGES"), 200, "APP_IMPORT_MAX_PAGES"
        ),
        lock_wait_seconds=_resolve_float(
            source.get("APP_IMPORT_LOCK_WAIT_SECONDS"), 30.0, "APP_IMPORT_LOCK_WAIT_SECONDS"
        ),
        working_window_years=_resolve_positive_int(
            source.get("APP_WORKING_WINDOW_YEARS"), 5, "APP_WORKING_WINDOW_YEARS"
        ),
        warmup_trading_days=_resolve_non_negative_int(
            source.get("APP_WARMUP_TRADING_DAYS"), 20, "APP_WARMUP_TRADING_DAYS"
        ),
    )

    return Settings(
        project_root=root,
        backend_host=backend_host,
        backend_port=backend_port,
        frontend_port=frontend_port,
        runtime_dir=runtime_dir,
        db_path=db_path,
        log_dir=log_dir,
        backup_dir=backup_dir,
        raw_archive_dir=raw_archive_dir,
        lock_dir=lock_dir,
        fixture_dir=fixture_dir,
        data_source=data_source,
        import_retry=import_retry,
    )
