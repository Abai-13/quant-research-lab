"""SQLite 数据库入口（阶段 2 起由数据层承担实现）。

阶段 1 的公开 API 保持不变：``initialize_database`` / ``check_database`` / ``DatabaseError``
与 ``SCHEMA_VERSION`` 仍从这里导入；schema 2 的表结构与显式迁移见 ``app.data.schema``
和 ``app.data.migration``。
"""

from __future__ import annotations

from .data.migration import (
    SCHEMA_VERSION,
    DatabaseError,
    DatabaseStatus,
    Metadata,
    check_database,
    initialize_database,
    migrate_database,
)

__all__ = [
    "SCHEMA_VERSION",
    "DatabaseError",
    "DatabaseStatus",
    "Metadata",
    "check_database",
    "initialize_database",
    "migrate_database",
]
