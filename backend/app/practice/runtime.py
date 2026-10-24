"""练习库的隔离运行配置与连接入口。

练习库使用应用当前的 schema 4 作为只读兼容底座，并在该文件中追加
practice 专属表。它永远位于 runtime/practice/app.db，不会复用个人主库。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields

from app.config import Settings
from app.db import SCHEMA_VERSION, initialize_database

from .storage import ensure_practice_schema


@dataclass(frozen=True)
class PracticeSettings(Settings):
    """显式标记的练习配置，避免依赖用户自定义目录名称判定隔离状态。"""


def practice_settings(settings: Settings) -> PracticeSettings:
    """返回指向隔离练习运行目录的配置副本。

    重复调用保持同一路径，所有伴随文件也落在隔离目录内。
    """
    if isinstance(settings, PracticeSettings):
        return settings
    runtime_dir = settings.runtime_dir.resolve() / "practice"
    values = {item.name: getattr(settings, item.name) for item in fields(Settings)}
    values.update(
        runtime_dir=runtime_dir,
        db_path=runtime_dir / "app.db",
        log_dir=runtime_dir / "logs",
        backup_dir=runtime_dir / "backups",
        raw_archive_dir=runtime_dir / "data" / "raw",
        lock_dir=runtime_dir / "locks",
        fixture_dir=runtime_dir / "fixtures",
    )
    return PracticeSettings(**values)


def practice_connect(settings: Settings) -> sqlite3.Connection:
    """初始化隔离 schema 4 练习库并返回可由调用方管理事务的连接。"""
    isolated = practice_settings(settings)
    metadata = initialize_database(isolated)
    if metadata.schema_version != SCHEMA_VERSION:
        raise RuntimeError(
            f"practice runtime requires schema {SCHEMA_VERSION}; found {metadata.schema_version}"
        )
    conn = sqlite3.connect(isolated.db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        ensure_practice_schema(conn)
        # This connection is fresh; this commits only the schema bootstrap.
        if conn.in_transaction:
            conn.commit()
    except Exception:
        conn.close()
        raise
    return conn
