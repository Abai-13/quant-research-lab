"""一致性备份包：数据库 + 全部被引用的外部文件。

备份不是“只写文件清单”：包内包含可独立恢复的数据库副本、每个外部文件的
独立副本、以及记录受检映射的 manifest。可写 SQLite（含 maintenance.db）一律用
SQLite backup API 取一致性快照，绝不对活动数据库做硬链接。

路径安全：数据库里保存的路径与包内路径分开映射；包内路径只由序号与摘要生成，
拒绝绝对路径/``..`` 逃逸；任何目标解析后必须仍在新包内。恢复使用 manifest 中
受检的包内映射，不按数据库原路径直接写盘。

硬链接只表示“同一仍存在文件不重复分配数据块”，不表示包永远零字节：在线源被
清理后，备份目录项仍持有数据。空间计量分别报告逻辑字节与按文件身份去重的实际
字节。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .digests import sha256_of_object
from .errors import DataError

__all__ = [
    "BackupResult",
    "create_backup",
    "verify_backup",
    "restore_package",
    "directory_bytes",
    "file_identity_bytes",
    "database_fingerprint",
    "validate_recovery_point",
    "verify_registered_recovery_point",
]

BACKUP_FORMAT = "backup-package-v2"
LEGACY_BACKUP_FORMATS = {"backup-package-v1"}
MANIFEST_NAME = "backup-manifest.json"
SNAPSHOT_LIMITATION = (
    "Full-runtime backups require the application and background jobs to be stopped and drained. "
    "SQLite writer reservations serialize the snapshot window but do not coordinate multi-database workflows."
)


@dataclass
class BackupResult:
    path: Path
    manifest_path: Path
    db_sha256: str
    files: list[dict[str, Any]] = field(default_factory=list)
    verified: bool = False
    logical_bytes: int = 0
    physical_bytes: int = 0
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "manifest_path": str(self.manifest_path),
            "db_sha256": self.db_sha256,
            "files": self.files,
            "verified": self.verified,
            "logical_bytes": self.logical_bytes,
            "physical_bytes": self.physical_bytes,
            "limitations": self.limitations,
        }


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# 清理会改动版本引用、分块、原始档案、请求日志、质量记录与股票池引用；
# 这些表的实际行值（而非调用方维护的 content_hash 或 COUNT/MAX）全部进入状态证明。
_FINGERPRINT_TABLES: dict[str, str] = {
    "trade_calendar": "id",
    "security_info": "id",
    "daily_price": "id",
    "adj_factor": "id",
    "suspension_event": "id",
    "corporate_action": "id",
    "data_version": "version_id",
    "data_version_member": "version_id, category, record_id",
    "quality_issue": "issue_id",
    "pool_snapshot": "snapshot_id",
    "pool_member": "snapshot_id, ts_code",
    "pool_exclusion": "snapshot_id, ts_code, seq",
    "data_chunk": "chunk_id",
    "data_chunk_member": "chunk_id, logical_key",
    "data_version_chunk": "version_id, category, chunk_id",
    "coverage_stats": "source_code, category",
    "retention_policy": "policy_version",
    "retention_hold": "hold_id",
    "archive_manifest": "archive_id",
    "raw_archive": "digest",
    "import_batch": "batch_id",
    "import_request_log": "request_id",
    "source_registry": "source_code, interface",
    "schema_history": "id",
    "cleanup_journal": "journal_id",
}


def _table_present(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _stream_fingerprint(conn: sqlite3.Connection, table: str, order_by: str) -> list[Any]:
    """对表内全部列的实际行值做流式摘要（不依赖调用方维护的 content_hash）。"""
    columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
    digest = hashlib.sha256()
    digest.update(("|".join(columns) + "\x1e").encode("utf-8"))
    count = 0
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}"):
        count += 1
        parts = []
        for value in row:
            if value is None:
                parts.append("\x00")
            else:
                parts.append(str(value))
        digest.update(("\x1f".join(parts) + "\x1e").encode("utf-8"))
    return [count, digest.hexdigest()]


def database_fingerprint(conn: sqlite3.Connection) -> str:
    """数据库内容证明：逐表流式摘要 + 元信息，不依赖文件路径或实例 UUID。

    用于核对“替代恢复点是否就是本库操作前的状态”。仅比较实例 UUID、
    generation 或文件路径都不够：同身份克隆分叉后这些值可能仍然相同。
    """
    details: dict[str, Any] = {"metadata": {}, "generation": None, "tables": {}}
    if _table_present(conn, "app_metadata"):
        details["metadata"] = dict(
            conn.execute("SELECT key, value FROM app_metadata ORDER BY key").fetchall()
        )
    if _table_present(conn, "maintenance_generation"):
        row = conn.execute("SELECT generation FROM maintenance_generation WHERE id=1").fetchone()
        details["generation"] = int(row[0]) if row else None
    for table, order_by in _FINGERPRINT_TABLES.items():
        if _table_present(conn, table):
            details["tables"][table] = _stream_fingerprint(conn, table, order_by)
    return sha256_of_object(details)


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0
    )


def _current_instance_id(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute(
            "SELECT value FROM app_metadata WHERE key='instance_id'"
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row else ""


def validate_recovery_point(
    settings,
    package: Path,
    *,
    current_conn: sqlite3.Connection,
) -> dict[str, Any]:
    """核对显式恢复点确实能恢复“本库操作前状态”。

    核对项：包内独立验证、实例标识、规范化数据库路径（新包）、包内数据库内容
    指纹与 manifest 一致、包内内容指纹与当前业务库完全一致。仅有路径字符串或
    ``verified`` 标记不构成通过；过旧、克隆分叉、被篡改或不完整的包都会被拒绝。
    """
    package = Path(package)
    verification = verify_backup(package)
    if not verification.get("verified"):
        raise DataError(
            "recovery_point_invalid",
            f"提供的恢复点未通过独立验证: {verification.get('reason')}",
        )
    manifest_path = package / MANIFEST_NAME
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    database = manifest.get("database") or {}
    package_instance = str(database.get("instance_id") or "")
    current_instance = _current_instance_id(current_conn)
    if not package_instance or not current_instance or package_instance != current_instance:
        raise DataError(
            "recovery_point_instance_mismatch",
            f"恢复点属于实例 {package_instance or '未知'}，当前数据库实例为"
            f" {current_instance or '未知'}",
        )
    recorded_path = database.get("path")
    if recorded_path:
        if _normalized_path(recorded_path) != _normalized_path(settings.db_path):
            raise DataError(
                "recovery_point_database_mismatch",
                f"恢复点属于数据库 {recorded_path}，不是当前数据库"
                f" {Path(settings.db_path).resolve()}",
            )
        binding = "instance+path+content"
    else:
        # 旧包没有路径字段：仍允许作为恢复点，但必须以完整内容证明兜底，
        # 不能仅凭实例 UUID 或 verified 标记。
        binding = "instance+content"
    package_db = package / str(database.get("relative_path") or "app.db")
    if not package_db.is_file():
        raise DataError("recovery_point_invalid", "恢复点内缺少数据库文件")
    try:
        package_conn = _readonly_connection(package_db)
    except sqlite3.Error as exc:
        raise DataError("recovery_point_invalid", f"恢复点数据库无法读取: {exc}") from exc
    try:
        package_fingerprint = database_fingerprint(package_conn)
    finally:
        package_conn.close()
    expected_fingerprint = database.get("content_fingerprint")
    if expected_fingerprint and package_fingerprint != expected_fingerprint:
        raise DataError(
            "recovery_point_tampered",
            "恢复点内容与自身 manifest 记录不一致（不完整或被篡改）",
        )
    current_fingerprint = database_fingerprint(current_conn)
    if package_fingerprint != current_fingerprint:
        raise DataError(
            "recovery_point_state_mismatch",
            "恢复点内容不是当前数据库操作前的状态：备份过旧、来自其他库或已分叉",
        )
    return {
        "path": str(package.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "instance_id": package_instance,
        "binding": binding,
        "content_fingerprint": current_fingerprint,
        "created_at": manifest.get("created_at"),
        "generation": database.get("generation"),
    }


def verify_registered_recovery_point(
    settings,
    *,
    package: str | Path,
    manifest_sha256: str | None,
    instance_id: str | None,
    content_fingerprint: str | None,
    current_instance_id: str | None = None,
) -> dict[str, Any]:
    """在任何重试删除/释放保护之前，完整复核中断前精确登记的恢复点。

    与 ``validate_recovery_point`` 不同：业务库可能已经完成清理，因此这里
    **不**比较当前业务库内容；而是核对登记时的 manifest 字节、实例、路径，
    重新执行包内 DB 与全部外部文件的独立恢复验证，并重算包内数据库内容指纹
    与登记证明/manifest 对比。仅 ``verified_at``、文件存在或 manifest 哈希不足。
    """
    if not manifest_sha256:
        raise DataError(
            "recovery_point_registration_incomplete",
            "恢复点登记缺少 manifest 哈希，无法证明包未被替换",
        )
    if not content_fingerprint:
        raise DataError(
            "recovery_point_registration_incomplete",
            "恢复点登记缺少内容指纹，无法证明包内数据库状态",
        )
    package = Path(package)
    manifest_path = package / MANIFEST_NAME
    if not package.is_dir() or not manifest_path.is_file():
        raise DataError(
            "recovery_point_registration_missing",
            f"登记的恢复点不存在或缺少 manifest: {package}",
        )
    manifest_bytes = manifest_path.read_bytes()
    actual_manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_sha != manifest_sha256:
        raise DataError(
            "recovery_point_manifest_mismatch",
            "恢复点 manifest 与首次清理前的登记哈希不一致（包被替换或篡改）",
        )
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise DataError("recovery_point_package_invalid", f"manifest 无法解析: {exc}") from exc
    database = manifest.get("database") or {}
    package_instance = str(database.get("instance_id") or "")
    if instance_id and package_instance != instance_id:
        raise DataError(
            "recovery_point_instance_mismatch",
            f"恢复点实例 {package_instance or '未知'} 与登记实例 {instance_id} 不一致",
        )
    if current_instance_id and package_instance != current_instance_id:
        raise DataError(
            "recovery_point_instance_mismatch",
            "恢复点实例与当前业务库实例不一致",
        )
    recorded_path = database.get("path")
    if recorded_path and _normalized_path(recorded_path) != _normalized_path(settings.db_path):
        raise DataError(
            "recovery_point_database_mismatch",
            "恢复点属于其他数据库路径，不能作为本库中断恢复依据",
        )
    # 完整独立恢复验证：包内 app.db 摘要/完整性 + 每个外部文件复制后摘要。
    verification = verify_backup(package)
    if not verification.get("verified"):
        raise DataError(
            "recovery_point_package_invalid",
            f"恢复点包内数据库或外部文件损坏: {verification.get('reason')}",
        )
    package_db = package / str(database.get("relative_path") or "app.db")
    try:
        package_conn = _readonly_connection(package_db)
    except sqlite3.Error as exc:
        raise DataError("recovery_point_package_invalid", f"恢复点数据库无法读取: {exc}") from exc
    try:
        package_fingerprint = database_fingerprint(package_conn)
    finally:
        package_conn.close()
    if package_fingerprint != content_fingerprint:
        raise DataError(
            "recovery_point_fingerprint_mismatch",
            "恢复点包内数据库内容指纹与首次清理前的登记证明不一致",
        )
    manifest_fingerprint = database.get("content_fingerprint")
    if manifest_fingerprint and package_fingerprint != manifest_fingerprint:
        raise DataError(
            "recovery_point_fingerprint_mismatch",
            "恢复点包内数据库内容指纹与自身 manifest 记录不一致",
        )
    return {
        "path": str(package.resolve()),
        "manifest_sha256": actual_manifest_sha,
        "instance_id": package_instance,
        "content_fingerprint": package_fingerprint,
        "verified_at": verification.get("verified_at"),
        "files": verification.get("files"),
    }


def _external_paths(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """返回 ``(数据库保存路径, 期望摘要, 种类)``；按实际 schema 枚举允许表。

    schema 1 没有 raw_archive/archive_manifest，返回空列表；
    其他 sqlite 错误不吞掉。
    """
    entries: list[tuple[str, str, str]] = []
    if _table_exists(conn, "raw_archive"):
        for path, digest in conn.execute("SELECT path, digest FROM raw_archive ORDER BY path"):
            entries.append((str(path), str(digest), "raw"))
    if _table_exists(conn, "archive_manifest"):
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(archive_manifest)")
        }
        if {"manifest_path", "manifest_sha256"} <= columns:
            rows = conn.execute(
                "SELECT path, sha256, manifest_path, manifest_sha256 FROM archive_manifest"
                " ORDER BY path"
            ).fetchall()
            for path, digest, manifest_path, manifest_digest in rows:
                entries.append((str(path), str(digest), "archive"))
                if manifest_path and manifest_digest:
                    entries.append((str(manifest_path), str(manifest_digest), "archive"))
        else:
            for path, digest in conn.execute(
                "SELECT path, sha256 FROM archive_manifest ORDER BY path"
            ):
                entries.append((str(path), str(digest), "archive"))
    return entries


def _source_path(settings, stored: str, kind: str) -> Path:
    candidate = Path(stored)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise DataError("backup_unsafe_source", f"拒绝不安全的数据库文件路径: {stored}")
    root = Path(settings.raw_archive_dir) if kind == "raw" else Path(settings.runtime_dir) / "archives"
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise DataError("backup_unsafe_source", f"外部文件路径逃逸: {stored}") from exc
    return resolved


def _package_path(index: int, stored: str) -> str:
    """由序号/摘要生成受检包内相对路径；不接受数据库路径中的目录成分。"""
    name = Path(stored).name or "file"
    digest = hashlib.sha256(stored.encode("utf-8")).hexdigest()[:12]
    return f"external/{index:04d}-{digest}/{name}"


def _ensure_within(package: Path, target: Path) -> Path:
    package_resolved = package.resolve()
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(package_resolved)
    except ValueError as exc:
        raise DataError("backup_path_escape", f"包内路径逃逸: {target}") from exc
    return target_resolved


def _safe_relative(value: Any) -> Path:
    raw = str(value or "")
    path = Path(raw)
    if (not raw or path.is_absolute() or ".." in path.parts or
            (len(raw) >= 2 and raw[1] == ":") or raw.startswith(("\\\\", "//"))):
        raise DataError("backup_path_escape", f"不安全的相对路径: {raw}")
    return path


def _copy_file(source: Path, target: Path, *, allow_hardlink: bool) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.resolve() == source.resolve():
            raise DataError("backup_source_target_same", f"源与目标相同: {source}")
        if _sha256_file(target) == _sha256_file(source):
            return "reused"
        raise DataError("backup_target_exists", f"包内目标已存在且内容不同: {target}")
    if allow_hardlink:
        try:
            os.link(source, target)
            return "hardlink"
        except OSError:
            pass
    shutil.copy2(source, target)
    return "copy"


def _sqlite_snapshot(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(source_path), timeout=30.0)
    try:
        dest = sqlite3.connect(str(target_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _runtime_databases(settings) -> list[tuple[Path, str]]:
    """Every persistent SQLite store that must move with a runtime directory."""
    runtime = Path(settings.runtime_dir)
    return [
        (Path(settings.db_path), "data/app.db"),
        (runtime / "maintenance.db", "maintenance.db"),
        (runtime / "practice" / "app.db", "practice/app.db"),
        (runtime / "research" / "research.sqlite3", "research/research.sqlite3"),
        (runtime / "agent" / "tasks.sqlite3", "agent/tasks.sqlite3"),
        (runtime / "agent" / "maintenance.sqlite3", "agent/maintenance.sqlite3"),
    ]


def _runtime_external_files(settings) -> list[tuple[Path, str]]:
    """Return non-database research artifacts with safe runtime-relative paths."""
    root = Path(settings.runtime_dir).resolve()
    experiments = root / "research" / "experiments"
    if not experiments.exists():
        return []
    result: list[tuple[Path, str]] = []
    for current, dirs, names in os.walk(experiments, followlinks=False):
        base = Path(current)
        for name in list(dirs):
            path = base / name
            if path.is_symlink() or (getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise DataError("backup_unsafe_source", f"研究工件包含重解析目录: {path}")
        for name in names:
            path = base / name
            if path.is_symlink() or (getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise DataError("backup_unsafe_source", f"研究工件包含重解析文件: {path}")
            if not path.is_file():
                raise DataError("backup_unsafe_source", f"研究工件不是普通文件: {path}")
            try:
                relative = path.resolve(strict=True).relative_to(root).as_posix()
            except (OSError, ValueError) as exc:
                raise DataError("backup_unsafe_source", f"研究工件路径逃逸: {path}") from exc
            result.append((path, relative))
    return sorted(result, key=lambda item: item[1])


def _create_backup_impl(
    settings,
    *,
    out_dir: Path | None = None,
    verify: bool = True,
) -> BackupResult:
    """在写锁保护下创建一致性备份包；调用方负责持有写锁。"""
    db_path = Path(settings.db_path)
    if not db_path.exists():
        raise DataError("database_missing", "数据库不存在，无法创建备份")
    base = Path(out_dir) if out_dir else Path(settings.backup_dir)
    package = base / f"{_utc_stamp()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    package.mkdir(parents=True, exist_ok=False)
    backup_db = package / "app.db"
    _sqlite_snapshot(db_path, backup_db)

    # 外部文件清单、元信息与内容指纹都从同一个数据库快照读取，避免两代混合。
    snapshot_conn = _readonly_connection(backup_db)
    try:
        external = _external_paths(snapshot_conn)
        metadata = dict(snapshot_conn.execute("SELECT key, value FROM app_metadata").fetchall())
        fingerprint = database_fingerprint(snapshot_conn)
        generation = None
        if _table_present(snapshot_conn, "maintenance_generation"):
            generation_row = snapshot_conn.execute(
                "SELECT generation FROM maintenance_generation WHERE id=1"
            ).fetchone()
            generation = int(generation_row[0]) if generation_row else None
    finally:
        snapshot_conn.close()

    db_sha = _sha256_file(backup_db)
    files: list[dict[str, Any]] = []
    logical = backup_db.stat().st_size
    external_refs = [(stored, expected, kind, settings, "")
                     for stored, expected, kind in external]
    practice_path = Path(settings.runtime_dir) / "practice" / "app.db"
    if practice_path.is_file():
        from app.practice.runtime import practice_settings
        practice_config = practice_settings(settings)
        practice_conn = _readonly_connection(practice_path)
        try:
            external_refs.extend((stored, expected, kind, practice_config, "practice/")
                                 for stored, expected, kind in _external_paths(practice_conn))
        finally:
            practice_conn.close()

    for index, (stored, expected, kind, source_settings, restore_prefix) in enumerate(external_refs):
        source = _source_path(source_settings, stored, kind)
        if not source.is_file():
            raise DataError("backup_missing_file", f"备份失败：外部文件缺失 {stored}")
        actual = _sha256_file(source)
        if actual != expected:
            raise DataError("backup_digest_mismatch", f"备份失败：外部文件摘要不符 {stored}")
        package_path = _package_path(index, stored)
        target = _ensure_within(package, package / package_path)
        # raw 与 archive 文件不可变，可硬链接；若源可写（理论上不应进入此表）仍会校验摘要。
        mode = _copy_file(source, target, allow_hardlink=True)
        if _sha256_file(target) != expected:
            raise DataError("backup_copy_mismatch", f"备份失败：复制后摘要不符 {stored}")
        size = target.stat().st_size
        logical += size
        files.append(
            {
                "path": f"{restore_prefix}{stored}",
                "kind": kind,
                "package_path": package_path,
                "restore_path": f"{restore_prefix}{'data/raw' if kind == 'raw' else 'archives'}/{stored}",
                "sha256": expected,
                "bytes": size,
                "storage": mode,
            }
        )

    # Product databases are copied while the wrapper holds all writer locks.
    side_databases = _runtime_databases(settings)[1:]
    for side_db, relative in side_databases:
        if not side_db.is_file():
            continue
        side_target = package / relative
        _sqlite_snapshot(side_db, side_target)
        side_entry = {
            "path": relative,
            "kind": "side_db",
            "package_path": relative,
            "restore_path": relative,
            "sha256": _sha256_file(side_target),
            "bytes": side_target.stat().st_size,
            "storage": "snapshot",
        }
        files.append(side_entry)
        logical += side_entry["bytes"]

    research_copy = package / "research" / "research.sqlite3"
    if research_copy.is_file():
        with sqlite3.connect(research_copy) as research_conn:
            tables = {str(row[0]) for row in research_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("research_operation", "research_experiment"):
                if table in tables and research_conn.execute(
                    f"SELECT 1 FROM {table} WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone():
                    raise DataError("backup_active_research", "研究任务或实验仍在运行，无法生成全域一致备份")

    for index, (source, relative) in enumerate(_runtime_external_files(settings)):
        package_path = f"runtime-files/{index:04d}/{Path(relative).name}"
        target = _ensure_within(package, package / package_path)
        digest = _sha256_file(source)
        mode = _copy_file(source, target, allow_hardlink=False)
        if _sha256_file(target) != digest:
            raise DataError("backup_copy_mismatch", f"研究工件复制后摘要不符: {relative}")
        files.append({"path": relative, "kind": "runtime_file", "package_path": package_path,
                      "restore_path": relative, "sha256": digest, "bytes": target.stat().st_size,
                      "storage": mode})
        logical += target.stat().st_size

    manifest = {
        "format": BACKUP_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database": {
            "relative_path": "app.db",
            "restore_path": "data/app.db",
            "sha256": db_sha,
            "bytes": backup_db.stat().st_size,
            "schema_version": metadata.get("schema_version"),
            "instance_id": metadata.get("instance_id"),
            "created_at": metadata.get("created_at"),
            "path": str(db_path.resolve()),
            "generation": generation,
            "content_fingerprint": fingerprint,
            "fingerprint_algorithm": "db-fingerprint-v1",
        },
        "files": files,
        "external_expected": len(external_refs),
        "logical_bytes": logical,
        "consistency_policy": "offline-quiescent-v1",
        "path_mapping": "package_path is generated and validated; database paths are never used as targets",
    }
    manifest_path = package / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    physical, limitations = file_identity_bytes([package])
    limitations.append(SNAPSHOT_LIMITATION)
    result = BackupResult(
        path=package,
        manifest_path=manifest_path,
        db_sha256=db_sha,
        files=files,
        logical_bytes=logical,
        physical_bytes=physical,
        limitations=limitations,
    )
    if verify:
        verification = verify_backup(package)
        result.verified = bool(verification["verified"])
        if not result.verified:
            raise DataError(
                "backup_verification_failed",
                f"备份独立恢复验证未通过: {verification.get('reason')}",
            )
        manifest["verified_at"] = verification["verified_at"]
        manifest["physical_bytes"] = physical
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
    return result


def create_backup(
    settings,
    *,
    out_dir: Path | None = None,
    verify: bool = True,
    held_writer_locks: set[Path] | None = None,
) -> BackupResult:
    """Create one quiescent snapshot of all extant runtime databases."""
    databases = _runtime_databases(settings)
    with ExitStack() as locks:
        held = {Path(path).resolve() for path in (held_writer_locks or set())}
        present = {path.resolve() for path, _ in databases if path.is_file()}
        for path in sorted(present - held, key=lambda item: os.path.normcase(str(item))):
            conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
            locks.callback(conn.close)
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise DataError("backup_quiescence_unavailable", f"无法冻结数据库写入 {path}: {exc}") from exc
        result = _create_backup_impl(settings, out_dir=out_dir, verify=verify)
        if {path.resolve() for path, _ in databases if path.is_file()} != present:
            raise DataError("backup_quiescence_unavailable", "备份期间产品数据库集合发生变化")
        return result


def _manifest_files(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw_files = manifest.get("files", [])
    if not isinstance(raw_files, list):
        raise DataError("backup_manifest_mismatch", "manifest files 必须为列表")
    files = list(raw_files)
    external = [entry for entry in files if entry.get("kind") in {"raw", "archive"}]
    try:
        expected = int(manifest.get("external_expected", len(external)))
    except (TypeError, ValueError) as exc:
        raise DataError("backup_manifest_mismatch", "manifest 外部文件数量无效") from exc
    if len(external) != expected:
        raise DataError("backup_manifest_mismatch", "manifest 外部文件数量与登记不一致")
    return files


def verify_backup(package: Path) -> dict[str, Any]:
    """真实独立恢复验证：在临时目录重建数据库与外部文件，再逐项核对。"""
    package = Path(package)
    manifest_path = package / MANIFEST_NAME
    if not manifest_path.is_file():
        return {"verified": False, "reason": "manifest_missing"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") not in ({BACKUP_FORMAT} | LEGACY_BACKUP_FORMATS):
            return {"verified": False, "reason": "backup_format_unsupported"}
        raw_files = manifest.get("files", [])
        if not isinstance(raw_files, list) or any(not isinstance(entry, dict) for entry in raw_files):
            return {"verified": False, "reason": "manifest_invalid:files"}
        files = _manifest_files(manifest)
        allowed_kinds = {"raw", "archive", "side_db", "runtime_file"}
        if any(entry.get("kind") not in allowed_kinds for entry in files):
            return {"verified": False, "reason": "manifest_invalid:kind"}
    except (OSError, TypeError, ValueError, DataError) as exc:
        return {"verified": False, "reason": f"manifest_invalid:{exc}"}
    database = manifest.get("database")
    if not isinstance(database, dict):
        return {"verified": False, "reason": "manifest_invalid:database"}
    if not isinstance(database.get("sha256"), str) or not isinstance(database.get("instance_id"), str):
        return {"verified": False, "reason": "manifest_invalid:database_fields"}
    try:
        db = _ensure_within(package, package / _safe_relative(database.get("relative_path")))
    except DataError as exc:
        return {"verified": False, "reason": f"manifest_invalid:{exc.reason}"}
    if not db.is_file():
        return {"verified": False, "reason": "database_missing"}
    if _sha256_file(db) != manifest["database"]["sha256"]:
        return {"verified": False, "reason": "database_digest_mismatch"}

    with tempfile.TemporaryDirectory(prefix="backup-verify-") as tmp:
        tmp_root = Path(tmp)
        restored_db = tmp_root / "app.db"
        shutil.copy2(db, restored_db)
        try:
            conn = sqlite3.connect(restored_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            return {"verified": False, "reason": "database_unreadable"}
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                return {"verified": False, "reason": "integrity_check_failed"}
            metadata = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
            if metadata.get("instance_id") != manifest["database"]["instance_id"]:
                return {"verified": False, "reason": "instance_id_mismatch"}
        except sqlite3.Error:
            return {"verified": False, "reason": "database_unreadable"}
        finally:
            conn.close()
        seen_restore_paths = {"data/app.db"}
        for entry in files:
            package_path = str(entry.get("package_path") or "")
            try:
                relative_package = _safe_relative(package_path)
                source = _ensure_within(package, package / relative_package)
                target = _ensure_within(tmp_root, tmp_root / relative_package)
                if manifest.get("format") == BACKUP_FORMAT:
                    restore_relative = _safe_relative(entry.get("restore_path"))
                elif entry.get("kind") in {"raw", "archive"}:
                    stored = str(entry.get("path") or "")
                    _safe_relative(stored)
                    restore_relative = Path("data/raw" if entry["kind"] == "raw" else "archives") / stored
                else:
                    restore_relative = _safe_relative(entry.get("path") or package_path)
                if restore_relative.as_posix().casefold() in seen_restore_paths:
                    return {"verified": False, "reason": "manifest_invalid:restore_path_duplicate"}
                seen_restore_paths.add(restore_relative.as_posix().casefold())
            except DataError:
                return {"verified": False, "reason": f"unsafe_package_path:{package_path}"}
            expected_sha = entry.get("sha256")
            if not isinstance(expected_sha, str) or len(expected_sha) != 64:
                return {"verified": False, "reason": f"manifest_invalid:sha256:{package_path}"}
            target.parent.mkdir(parents=True, exist_ok=True)
            if not source.is_file():
                return {"verified": False, "reason": f"file_missing:{package_path}"}
            shutil.copy2(source, target)
            if _sha256_file(target) != expected_sha:
                return {"verified": False, "reason": f"file_mismatch:{package_path}"}
            if entry.get("kind") == "side_db":
                try:
                    check = sqlite3.connect(target)
                    try:
                        integrity = check.execute("PRAGMA integrity_check").fetchone()
                        if not integrity or integrity[0] != "ok":
                            return {"verified": False, "reason": f"side_db_integrity_failed:{package_path}"}
                    finally:
                        check.close()
                except sqlite3.Error:
                    return {"verified": False, "reason": f"side_db_unreadable:{package_path}"}
    return {
        "verified": True,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": len(files),
        "independent_restore": True,
    }


def restore_package(package: Path, target_dir: Path) -> dict[str, Any]:
    """Restore a verified package as a complete, default-configured runtime directory."""
    package = Path(package)
    target_dir = Path(target_dir)
    verification = verify_backup(package)
    if not verification["verified"]:
        raise DataError("backup_verification_failed", f"备份包未通过验证: {verification.get('reason')}")
    manifest = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
    files = _manifest_files(manifest)
    database = manifest["database"]
    if manifest.get("format") == BACKUP_FORMAT:
        db_restore = _safe_relative(database.get("restore_path"))
    else:
        # v1 bundles were valid snapshots but restored into a non-runnable layout.
        # Preserve their data while mapping them into the current default runtime.
        db_restore = Path("data/app.db")
    restore_rows: list[tuple[dict[str, Any], Path, Path]] = []
    seen_targets = {db_restore.as_posix()}
    for entry in files:
        package_rel = _safe_relative(entry.get("package_path"))
        if manifest.get("format") == BACKUP_FORMAT:
            restore_rel = _safe_relative(entry.get("restore_path"))
        elif entry.get("kind") in {"raw", "archive"}:
            stored = str(entry.get("path") or "")
            _safe_relative(stored)
            restore_rel = Path("data/raw" if entry["kind"] == "raw" else "archives") / stored
        else:
            restore_rel = _safe_relative(entry.get("path") or entry.get("package_path"))
        key = restore_rel.as_posix().casefold()
        if key in seen_targets:
            raise DataError("backup_manifest_mismatch", f"多个备份项写入同一恢复路径: {restore_rel}")
        seen_targets.add(key)
        restore_rows.append((entry, package_rel, restore_rel))

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        raise DataError("restore_target_exists", f"拒绝覆盖已有恢复目录: {target_dir}")
    # Keep the temporary sibling short for Windows MAX_PATH-limited copies.
    stage = target_dir.parent / f".restore-{uuid.uuid4().hex[:12]}"
    stage.mkdir(parents=False, exist_ok=False)
    target_db = stage / db_restore
    try:
        source_db = _ensure_within(package, package / _safe_relative(database.get("relative_path")))
        target_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_db, target_db)
        if _sha256_file(target_db) != database["sha256"]:
            raise DataError("restore_copy_mismatch", "恢复主数据库摘要不符")
        restored = 0
        for entry, package_rel, restore_rel in restore_rows:
            source = _ensure_within(package, package / package_rel)
            target = _ensure_within(stage, stage / restore_rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if _sha256_file(target) != entry["sha256"]:
                raise DataError("restore_copy_mismatch", f"恢复后摘要不符: {restore_rel}")
            if entry.get("kind") in {"raw", "archive", "runtime_file"}:
                restored += 1
        os.replace(stage, target_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {
        "status": "restored",
        "database": str(target_dir / db_restore),
        "external_files": restored,
        "target_dir": str(target_dir),
    }


def directory_bytes(path: Path) -> int:
    """文件返回自身大小；目录递归求和；不存在返回 0。"""
    path = Path(path)
    if not path.exists():
        return 0
    if path.is_file():
        return int(path.stat().st_size)
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def file_identity_bytes(paths: list[Path]) -> tuple[int, list[str]]:
    """按 (device, inode) 去重的实际占用字节；不可测量时给保守上界。"""
    seen: dict[tuple[int, int], int] = {}
    limitations: list[str] = []
    for root in paths:
        root = Path(root)
        if not root.exists():
            continue
        for item in root.rglob("*"):
            if not item.is_file():
                continue
            try:
                stat = item.stat()
                if int(stat.st_ino) == 0:
                    raise OSError("inode unavailable")
                key = (int(stat.st_dev), int(stat.st_ino))
            except OSError:
                limitations.append(f"无法按文件身份去重，按逻辑字节保守上界计: {item}")
                seen[(0, len(seen) + 1)] = int(item.stat().st_size)
                continue
            seen[key] = int(stat.st_size)
    return sum(seen.values()), limitations
