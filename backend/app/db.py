"""SQLite 元信息存储。

阶段 1 只保存 schema 版本、创建时间和实例标识。约束：

- 初始化是事务化的，可重复执行且不重置已有元信息；
- 发现损坏或不兼容的数据库时明确失败，绝不删除、重建或覆盖文件；
- readiness 检查以只读方式打开数据库，不承担隐式建库。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings

SCHEMA_VERSION = 1
METADATA_TABLE = "app_metadata"
_REQUIRED_KEYS = ("schema_version", "created_at", "instance_id")

_REASON_MESSAGES = {
    "database_missing": "数据库尚未初始化",
    "database_corrupt": "数据库文件已损坏或不是有效的 SQLite 数据库",
    "schema_incompatible": "数据库 schema 与本阶段要求不兼容",
    "database_unavailable": "数据库当前不可访问",
}


class DatabaseError(RuntimeError):
    """带稳定原因类别的数据库错误。"""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or _REASON_MESSAGES.get(reason, "数据库错误"))


@dataclass(frozen=True)
class Metadata:
    schema_version: int
    instance_id: str
    created_at: str


@dataclass(frozen=True)
class DatabaseStatus:
    ready: bool
    reason: str | None
    message: str | None
    metadata: Metadata | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_corruption_error(exc: sqlite3.DatabaseError) -> bool:
    text = str(exc).lower()
    return "not a database" in text or "malformed" in text or "file is encrypted" in text


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=5.0)


def _read_metadata(conn: sqlite3.Connection) -> Metadata:
    try:
        rows = dict(conn.execute(f"SELECT key, value FROM {METADATA_TABLE}").fetchall())
    except sqlite3.Error as exc:
        raise DatabaseError("schema_incompatible", f"无法读取元信息表: {exc}") from exc

    missing = [key for key in _REQUIRED_KEYS if key not in rows]
    if missing:
        raise DatabaseError("schema_incompatible", "元信息表缺少必要字段: " + ", ".join(missing))

    try:
        schema_version = int(rows["schema_version"])
    except (TypeError, ValueError) as exc:
        raise DatabaseError("schema_incompatible", "schema_version 不是有效整数") from exc

    instance_id = (rows["instance_id"] or "").strip()
    created_at = (rows["created_at"] or "").strip()
    if not instance_id or not created_at:
        raise DatabaseError("schema_incompatible", "实例标识或创建时间为空")
    return Metadata(schema_version=schema_version, instance_id=instance_id, created_at=created_at)


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [row[0] for row in rows]


def _check_integrity(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        raise DatabaseError("database_corrupt", "SQLite integrity_check 未通过")


def initialize_database(settings: Settings) -> Metadata:
    """初始化数据库并返回元信息。

    已存在且兼容时原样返回，不更新时间或实例标识。
    """
    db_path = settings.db_path
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DatabaseError("database_unavailable", f"无法创建数据库目录: {exc}") from exc

    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        raise DatabaseError("database_unavailable", f"无法打开数据库: {exc}") from exc

    try:
        _check_integrity(conn)
        tables = _table_names(conn)

        if METADATA_TABLE in tables:
            metadata = _read_metadata(conn)
            if metadata.schema_version != SCHEMA_VERSION:
                raise DatabaseError(
                    "schema_incompatible",
                    f"数据库 schema 版本为 {metadata.schema_version}，本应用要求 {SCHEMA_VERSION}",
                )
            return metadata

        if tables:
            raise DatabaseError(
                "schema_incompatible",
                "数据库包含非本应用的表，拒绝自动改写: " + ", ".join(sorted(tables)[:5]),
            )

        metadata = Metadata(
            schema_version=SCHEMA_VERSION,
            instance_id=str(uuid.uuid4()),
            created_at=_utc_now(),
        )
        try:
            with conn:  # 单个事务：建表与写入元信息要么全部成功，要么全部回滚。
                conn.execute(
                    f"CREATE TABLE {METADATA_TABLE} ("
                    "key TEXT PRIMARY KEY NOT NULL, "
                    "value TEXT NOT NULL)"
                )
                conn.executemany(
                    f"INSERT INTO {METADATA_TABLE}(key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(metadata.schema_version)),
                        ("instance_id", metadata.instance_id),
                        ("created_at", metadata.created_at),
                    ],
                )
        except sqlite3.Error as exc:
            raise DatabaseError("database_unavailable", f"写入元信息失败: {exc}") from exc
        return metadata

    except sqlite3.DatabaseError as exc:
        if _is_corruption_error(exc):
            raise DatabaseError("database_corrupt", str(exc)) from exc
        raise DatabaseError("database_unavailable", f"读取数据库失败: {exc}") from exc
    finally:
        conn.close()


def check_database(settings: Settings) -> DatabaseStatus:
    """以只读方式检查数据库；不创建文件、不修复 schema。"""
    db_path = settings.db_path
    if not db_path.exists():
        parent = db_path.parent
        if parent.exists() and not parent.is_dir():
            # 父路径被文件占用时，数据库目录不可写，而不是“尚未初始化”。
            return DatabaseStatus(False, "database_unavailable", "数据库父路径不是目录", None)
        return DatabaseStatus(False, "database_missing", _REASON_MESSAGES["database_missing"], None)
    if not db_path.is_file():
        return DatabaseStatus(False, "database_unavailable", "数据库路径不是文件", None)

    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        reason = "database_corrupt" if _is_corruption_error(exc) else "database_unavailable"
        return DatabaseStatus(False, reason, _REASON_MESSAGES[reason], None)

    try:
        _check_integrity(conn)
        if METADATA_TABLE not in _table_names(conn):
            raise DatabaseError("database_missing", "数据库存在但尚未初始化")
        metadata = _read_metadata(conn)
        if metadata.schema_version != SCHEMA_VERSION:
            raise DatabaseError(
                "schema_incompatible",
                f"数据库 schema 版本为 {metadata.schema_version}，本应用要求 {SCHEMA_VERSION}",
            )
        return DatabaseStatus(True, None, None, metadata)
    except DatabaseError as exc:
        return DatabaseStatus(False, exc.reason, str(exc), None)
    except sqlite3.DatabaseError as exc:
        reason = "database_corrupt" if _is_corruption_error(exc) else "database_unavailable"
        return DatabaseStatus(False, reason, _REASON_MESSAGES[reason], None)
    finally:
        conn.close()
