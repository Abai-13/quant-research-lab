"""数据库初始化、只读检查与显式 schema 升级。

阶段 1 行为保持兼容：初始化幂等且保留 ``schema_version``/``instance_id``/``created_at``；
schema 1/2 的既有库必须显式 ``migrate_database`` 才升级，readiness 只读并提示
``migration_required``，不修改任何表。

迁移遵循 C1 冻结约束：

- 维护运行记录 ``prepared`` 写在独立 ``maintenance.db``，业务库在备份验证前不写入；
- 备份包包含数据库与全部被引用的外部文件，并在临时目录独立验证；
- 结构变更、``schema_version`` 与准确的 ``schema_history(from,to)`` 在同一业务事务提交；
- schema 1 按 1→2、2→3 两个准确事务执行；新库直接建 schema 3，不写虚假迁移历史；
- 旧版本成员可转换为分块存储，version_id/manifest_version/manifest_sha256 不变。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import schema as schema_mod
from .backup import create_backup
from .chunks import convert_legacy_version, version_members
from .locking import DataWriteLock
from .maintenance import latest_run, open_maintenance, start_run, update_run
from .schema import METADATA_TABLE, SCHEMA_VERSION, table_names

__all__ = [
    "SCHEMA_VERSION",
    "DatabaseError",
    "MigrationInjectedFailure",
    "Metadata",
    "DatabaseStatus",
    "initialize_database",
    "check_database",
    "migrate_database",
    "convert_legacy_members",
    "reclaim_legacy_members",
]

_REQUIRED_KEYS = ("schema_version", "created_at", "instance_id")

_REASON_MESSAGES = {
    "database_missing": "数据库尚未初始化",
    "database_corrupt": "数据库文件已损坏或不是有效的 SQLite 数据库",
    "schema_incompatible": "数据库 schema 与本应用要求不兼容",
    "database_unavailable": "数据库当前不可访问",
    "migration_required": "数据库需要显式迁移到目标 schema",
    "migration_backup_unavailable": "迁移备份未通过一致性核对",
    "migration_inconsistent": "迁移后的结构与历史记录核对不一致",
}


class SettingsLike(Protocol):
    @property
    def db_path(self) -> Path: ...

    @property
    def backup_dir(self) -> Path: ...

    @property
    def runtime_dir(self) -> Path: ...


class DatabaseError(RuntimeError):
    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or _REASON_MESSAGES.get(reason, "数据库错误"))


class MigrationInjectedFailure(RuntimeError):
    """测试注入的迁移中途失败。"""


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


def _connect_read_write(path: Path) -> sqlite3.Connection:
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


def _check_integrity(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        raise DatabaseError("database_corrupt", "SQLite integrity_check 未通过")


class MetadataEntryStore:
    @staticmethod
    def create_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"CREATE TABLE {METADATA_TABLE} ("
            "key TEXT PRIMARY KEY NOT NULL, "
            "value TEXT NOT NULL)"
        )


def _create_fresh_schema(conn: sqlite3.Connection) -> Metadata:
    """空库直接创建 schema 3；不写 schema_history（没有发生迁移）。"""
    schema_mod.create_schema(conn)
    MetadataEntryStore.create_table(conn)
    metadata = Metadata(
        schema_version=SCHEMA_VERSION,
        instance_id=str(uuid.uuid4()),
        created_at=_utc_now(),
    )
    conn.executemany(
        f"INSERT INTO {METADATA_TABLE}(key, value) VALUES (?, ?)",
        [
            ("schema_version", str(metadata.schema_version)),
            ("instance_id", metadata.instance_id),
            ("created_at", metadata.created_at),
        ],
    )
    return metadata


def initialize_database(settings: SettingsLike) -> Metadata:
    db_path = settings.db_path
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DatabaseError("database_unavailable", f"无法创建数据库目录: {exc}") from exc

    try:
        conn = _connect_read_write(db_path)
    except sqlite3.Error as exc:
        raise DatabaseError("database_unavailable", f"无法打开数据库: {exc}") from exc

    try:
        _check_integrity(conn)
        tables = table_names(conn)
        if METADATA_TABLE in tables:
            metadata = _read_metadata(conn)
            if metadata.schema_version in (1, 2, SCHEMA_VERSION):
                return metadata
            raise DatabaseError(
                "schema_incompatible",
                f"数据库 schema 版本为 {metadata.schema_version}，本应用支持 1/2（待迁移）或 {SCHEMA_VERSION}",
            )
        if tables:
            raise DatabaseError(
                "schema_incompatible",
                "数据库包含非本应用的表，拒绝自动改写: " + ", ".join(sorted(tables)[:5]),
            )
        try:
            with conn:
                metadata = _create_fresh_schema(conn)
        except sqlite3.Error as exc:
            raise DatabaseError("database_unavailable", f"创建数据库结构失败: {exc}") from exc
        return metadata
    except sqlite3.DatabaseError as exc:
        if _is_corruption_error(exc):
            raise DatabaseError("database_corrupt", str(exc)) from exc
        raise DatabaseError("database_unavailable", f"读写数据库失败: {exc}") from exc
    finally:
        conn.close()


def check_database(settings: SettingsLike) -> DatabaseStatus:
    db_path = settings.db_path
    if not db_path.exists():
        parent = db_path.parent
        if parent.exists() and not parent.is_dir():
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
        if METADATA_TABLE not in table_names(conn):
            raise DatabaseError("database_missing", "数据库存在但尚未初始化")
        metadata = _read_metadata(conn)
        if metadata.schema_version == SCHEMA_VERSION:
            return DatabaseStatus(True, None, None, metadata)
        if metadata.schema_version in (1, 2):
            return DatabaseStatus(
                False,
                "migration_required",
                f"数据库仍是 schema {metadata.schema_version}，需要先显式迁移到 schema {SCHEMA_VERSION}",
                metadata,
            )
        raise DatabaseError(
            "schema_incompatible",
            f"数据库 schema 版本为 {metadata.schema_version}，本应用支持 1/2（待迁移）或 {SCHEMA_VERSION}",
        )
    except DatabaseError as exc:
        return DatabaseStatus(False, exc.reason, str(exc), None)
    except sqlite3.DatabaseError as exc:
        reason = "database_corrupt" if _is_corruption_error(exc) else "database_unavailable"
        return DatabaseStatus(False, reason, _REASON_MESSAGES[reason], None)
    finally:
        conn.close()


def _history_exists(conn: sqlite3.Connection, from_version: int, to_version: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM schema_history WHERE from_version=? AND to_version=?"
        " AND result='success'",
        (from_version, to_version),
    ).fetchone()
    return bool(row and int(row[0]) > 0)


def _migrate_1_to_2(conn: sqlite3.Connection, backup_path: str | None) -> None:
    with conn:
        schema_mod.create_schema_2(conn)
        conn.execute(
            f"UPDATE {METADATA_TABLE} SET value='2' WHERE key='schema_version'"
        )
        conn.execute(
            "INSERT INTO schema_history(from_version, to_version, applied_at, backup_path,"
            " result, notes) VALUES (1, 2, ?, ?, 'success', 'explicit migration 1->2')",
            (_utc_now(), backup_path),
        )


def _migrate_2_to_3(conn: sqlite3.Connection, backup_path: str | None) -> None:
    with conn:
        schema_mod.create_schema_3(conn)
        # 旧版本保持 v1 摘要，成员存储暂时标记 legacy（随后可显式转换）。
        conn.execute(
            "UPDATE data_version SET manifest_version=1, member_storage='legacy',"
            " member_count=(SELECT COUNT(*) FROM data_version_member m"
            "   WHERE m.version_id=data_version.version_id), chunk_count=0"
        )
        conn.execute(f"UPDATE {METADATA_TABLE} SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
        conn.execute(
            "INSERT INTO schema_history(from_version, to_version, applied_at, backup_path,"
            " result, notes) VALUES (2, ?, ?, ?, 'success', 'explicit migration 2->3')",
            (SCHEMA_VERSION, _utc_now(), backup_path),
        )


def migrate_database(
    settings: SettingsLike,
    *,
    fail_after_step: str | None = None,
    conn_factory=_connect_read_write,
    convert_legacy: bool = True,
    reclaim_legacy: bool = False,
) -> dict:
    """把 schema 1/2 显式升级为 schema 3；与导入共用写入锁。"""
    lock = DataWriteLock(settings.lock_dir / "data-write.lock", wait_seconds=30.0)
    lock.acquire()
    try:
        return _migrate_locked(
            settings,
            fail_after_step=fail_after_step,
            conn_factory=conn_factory,
            convert_legacy=convert_legacy,
            reclaim_legacy=reclaim_legacy,
        )
    finally:
        lock.release()


def _migrate_locked(
    settings: SettingsLike,
    *,
    fail_after_step: str | None = None,
    conn_factory=_connect_read_write,
    convert_legacy: bool = True,
    reclaim_legacy: bool = False,
) -> dict:
    """已持锁的迁移实现（内部 helper，避免嵌套自锁）。"""
    db_path = settings.db_path
    if not db_path.exists():
        raise DatabaseError("database_missing", "数据库文件不存在：迁移只作用于已存在的数据库")
    if fail_after_step == "before_integrity":
        raise MigrationInjectedFailure("injected failure before integrity check")

    side = open_maintenance(settings)
    try:
        run_id = start_run(
            side, kind="migrate", from_version=None, to_version=SCHEMA_VERSION, detail="prepared"
        )
        conn = conn_factory(db_path)
        try:
            _check_integrity(conn)
            tables = table_names(conn)
            if METADATA_TABLE not in tables:
                raise DatabaseError(
                    "schema_incompatible", "数据库缺少 app_metadata，不是本应用管理的库"
                )
            metadata = _read_metadata(conn)
            if metadata.schema_version == SCHEMA_VERSION:
                if fail_after_step == "after_schema":
                    raise MigrationInjectedFailure("injected failure after schema transaction")
                update_run(side, run_id, state="completed", detail="already_migrated")
                return {"status": "already_migrated", "schema_version": SCHEMA_VERSION}
            if metadata.schema_version not in (1, 2):
                raise DatabaseError(
                    "schema_incompatible",
                    f"数据库 schema 版本为 {metadata.schema_version}，只支持从 1/2 迁移",
                )
            original_version = metadata.schema_version
            allowed_tables = {METADATA_TABLE}
            if metadata.schema_version >= 2:
                allowed_tables.update(schema_mod.table_names(conn))
            foreign_tables = [
                name for name in tables if name not in allowed_tables and name != "schema_history"
            ]
            if metadata.schema_version == 1 and [t for t in tables if t != METADATA_TABLE]:
                raise DatabaseError(
                    "schema_incompatible",
                    "数据库包含非本应用的表，拒绝自动改写: " + ", ".join(sorted(foreign_tables)[:5]),
                )
            if metadata.schema_version == 2:
                expected = set(_schema2_table_names())
                unknown = [name for name in tables if name not in expected and name != "schema_history"]
                if unknown:
                    raise DatabaseError(
                        "schema_incompatible",
                        "数据库包含非 schema 2 的表，拒绝自动改写: " + ", ".join(sorted(unknown)[:5]),
                    )
            if fail_after_step == "after_metadata":
                raise MigrationInjectedFailure("injected failure after metadata check")

            backup = create_backup(settings, verify=True)
            backup_db = backup.path / "app.db"
            update_run(
                side, run_id, state="backup_verified", backup_path=str(backup.path)
            )
            if fail_after_step == "after_backup":
                raise MigrationInjectedFailure("injected failure after backup")
            if fail_after_step == "after_verify_backup":
                raise MigrationInjectedFailure("injected failure after backup verification")

            if metadata.schema_version == 1:
                _migrate_1_to_2(conn, str(backup_db))
                metadata = _read_metadata(conn)
            _migrate_2_to_3(conn, str(backup_db))
            if fail_after_step == "after_schema":
                raise MigrationInjectedFailure("injected failure after schema transaction")

            # 提交后恢复核对：结构、history、成员必须一致。
            if original_version == 1 and not _history_exists(conn, 1, 2):
                raise DatabaseError("migration_inconsistent", "缺少 1->2 迁移历史")
            if not _history_exists(conn, 2, SCHEMA_VERSION):
                raise DatabaseError("migration_inconsistent", "缺少 2->3 迁移历史")
            _check_integrity(conn)
            update_run(side, run_id, state="schema_committed")
        except MigrationInjectedFailure:
            conn.rollback()
            update_run(side, run_id, state="failed", detail="injected failure")
            raise
        except sqlite3.DatabaseError as exc:
            conn.rollback()
            update_run(side, run_id, state="failed", detail=str(exc))
            if _is_corruption_error(exc):
                raise DatabaseError("database_corrupt", str(exc)) from exc
            raise DatabaseError("database_unavailable", f"迁移失败（原库保留）: {exc}") from exc
        finally:
            conn.close()

        converted: list[dict] = []
        if convert_legacy:
            converted = _convert_legacy_members_locked(settings)
        reclaimed = 0
        if reclaim_legacy:
            reclaimed = _reclaim_legacy_members_locked(settings)
        update_run(
            side,
            run_id,
            state="completed",
            detail=f"converted={len(converted)};reclaimed={reclaimed}",
        )
        return {
            "status": "migrated",
            "schema_version": SCHEMA_VERSION,
            "backup_path": str(backup.path / "app.db"),
            "backup_package": str(backup.path),
            "verified": backup.verified,
            "converted_versions": len(converted),
            "reclaimed_member_rows": reclaimed,
        }
    finally:
        side.close()


def _schema2_table_names() -> list[str]:
    names = ["app_metadata", "schema_history"]
    for statement in schema_mod.SCHEMA_2_STATEMENTS:
        text = statement.strip()
        if text.upper().startswith("CREATE TABLE "):
            names.append(text.split()[2])
    return names


def convert_legacy_members(settings: SettingsLike) -> list[dict]:
    """公开入口：持写入锁后转换 legacy 成员。"""
    lock = DataWriteLock(settings.lock_dir / "data-write.lock", wait_seconds=30.0)
    lock.acquire()
    try:
        return _convert_legacy_members_locked(settings)
    finally:
        lock.release()


def _convert_legacy_members_locked(settings: SettingsLike) -> list[dict]:
    conn = _connect_read_write(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT version_id FROM data_version WHERE member_storage='legacy'"
            " ORDER BY created_at, version_id"
        ).fetchall()
        results = []
        for (version_id,) in rows:
            results.append(convert_legacy_version(conn, str(version_id)))
        return results
    finally:
        conn.close()


def reclaim_legacy_members(settings: SettingsLike) -> int:
    """公开入口：持写入锁后回收旧成员行。"""
    lock = DataWriteLock(settings.lock_dir / "data-write.lock", wait_seconds=30.0)
    lock.acquire()
    try:
        return _reclaim_legacy_members_locked(settings)
    finally:
        lock.release()


def _reclaim_legacy_members_locked(settings: SettingsLike) -> int:
    conn = _connect_read_write(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT version_id FROM data_version WHERE member_storage='chunked'"
        ).fetchall()
        deleted = 0
        for (version_id,) in rows:
            legacy = conn.execute(
                "SELECT category, record_id, content_hash, logical_key FROM data_version_member"
                " WHERE version_id=? ORDER BY category, logical_key",
                (version_id,),
            ).fetchall()
            if not legacy:
                continue
            chunked = version_members(conn, str(version_id))
            if [tuple(row) for row in legacy] != chunked:
                raise DatabaseError(
                    "migration_inconsistent",
                    f"版本 {version_id} 的分块成员与旧成员不一致，拒绝回收",
                )
            with conn:
                cursor = conn.execute(
                    "DELETE FROM data_version_member WHERE version_id=?", (version_id,)
                )
                deleted += int(cursor.rowcount or 0)
        return deleted
    finally:
        conn.close()
