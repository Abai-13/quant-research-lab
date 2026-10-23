"""五年工作窗口、引用保护、清理预览/应用与压缩归档。

安全边界：

- 预览以只读连接读取业务库，只把预览写入 ``maintenance.db``；
- 应用绑定 ``instance_id``、规范化数据库路径、政策版本与 ``generation``；
- 应用前必须在写锁内核对已验证的操作前完整恢复点；
- 归档先写临时文件、fsync、完整读回校验，再原子改名，最后在数据库事务中回收；
- 仍被任何在线版本、hold 或股票池快照引用的数据不得删除；
- 优先回收窗口外且未被引用的记录；每证券保留窗口前最后一个锚点。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .backup import (
    MANIFEST_NAME,
    create_backup,
    directory_bytes,
    validate_recovery_point,
    verify_registered_recovery_point,
)
from .chunks import VersionView, version_refs
from .digests import canonical_json_bytes, sha256_of_object
from .errors import DataError
from .locking import DataWriteLock
from .maintenance import (
    get_preview,
    list_actions,
    list_pending_files,
    list_quality_summaries,
    mark_pending_file_state,
    mark_preview_pending_rolled_back,
    open_maintenance,
    record_action,
    record_pending_file,
    record_preview,
    record_quality_summary,
    record_recovery_point,
    release_previous_recovery_points,
    set_recovery_point_state,
    unfinished_cleanup_summary,
    update_action,
    update_preview_state,
    update_quality_summary_state,
)
from .records import ALL_CATEGORIES, spec_for
from .timeutil import require_yyyymmdd, utc_now_iso, yyyymmdd

__all__ = [
    "POLICY_VERSION",
    "DEFAULT_POLICY",
    "current_policy",
    "ensure_policy",
    "add_hold",
    "list_holds",
    "remove_hold",
    "state_digest",
    "preview_cleanup",
    "apply_cleanup",
    "archive_version",
    "restore_version",
    "cleanup_status",
]

POLICY_VERSION = "ret-2026-10-22.1"
DEFAULT_POLICY: dict[str, Any] = {
    "policy_version": POLICY_VERSION,
    "working_window_years": 5,
    "warmup_trading_days": 20,
    "version_retention_days": 56,
    "raw_archive_days": 30,
    "request_log_days": 90,
    "quality_issue_days": 400,
    "backup_retain_count": 3,
    "preview_valid_minutes": 30,
    "anchor_rows_per_security": 1,
}

# 单次预览/删除的有界上限；重复执行可继续清剩余对象。
_RETENTION_ACTION_LIMIT = 5000
_TERMINAL_BATCH_STATUSES = ("completed", "failed", "partial")

_BASE_TABLES = {
    "calendar": "trade_calendar",
    "security": "security_info",
    "daily": "daily_price",
    "adj_factor": "adj_factor",
    "suspension": "suspension_event",
    "corporate_action": "corporate_action",
}
_DATE_FIELDS = {
    "calendar": "cal_date",
    "security": None,
    "daily": "trade_date",
    "adj_factor": "trade_date",
    "suspension": "trade_date",
    "corporate_action": "end_date",
}


def _now_iso() -> str:
    return utc_now_iso()


def ensure_policy(conn: sqlite3.Connection, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = dict(policy or DEFAULT_POLICY)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO retention_policy(policy_version, config_json, created_at)"
            " VALUES (?, ?, ?)",
            (
                str(config["policy_version"]),
                json.dumps(config, ensure_ascii=False, sort_keys=True),
                _now_iso(),
            ),
        )
    return config


def current_policy(conn: sqlite3.Connection, policy_version: str | None = None) -> dict[str, Any]:
    if policy_version:
        row = conn.execute(
            "SELECT config_json FROM retention_policy WHERE policy_version=?",
            (policy_version,),
        ).fetchone()
        if row is None:
            raise DataError("policy_not_found", f"保留政策 {policy_version} 不存在")
        return json.loads(row[0])
    row = conn.execute(
        "SELECT config_json FROM retention_policy ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return json.loads(row[0]) if row else dict(DEFAULT_POLICY)


def _bump_generation(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE maintenance_generation SET generation=generation+1, updated_at=? WHERE id=1",
        (_now_iso(),),
    )


def _generation(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT generation FROM maintenance_generation WHERE id=1").fetchone()
    return int(row[0]) if row else 0


def _instance(conn: sqlite3.Connection) -> tuple[str, str]:
    rows = dict(conn.execute("SELECT key, value FROM app_metadata").fetchall())
    return str(rows.get("instance_id") or ""), str(rows.get("created_at") or "")


def state_digest(conn: sqlite3.Connection) -> str:
    """完整状态证人：generation + 关键表 max(id)/计数 + 版本/持有摘要。"""
    details: dict[str, Any] = {"generation": _generation(conn)}
    details["versions"] = [
        [row[0], row[1], row[2]]
        for row in conn.execute(
            "SELECT version_id, retention_state, archive_id FROM data_version"
            " ORDER BY version_id"
        ).fetchall()
    ]
    details["holds"] = [
        [row[0], row[1], row[2]]
        for row in conn.execute(
            "SELECT hold_id, target_type, target_id FROM retention_hold ORDER BY hold_id"
        ).fetchall()
    ]
    details["tables"] = {}
    for table in set(_BASE_TABLES.values()) | {
        "data_chunk",
        "data_chunk_member",
        "data_version_chunk",
        "coverage_stats",
        "quality_issue",
        "pool_snapshot",
        "pool_member",
        "pool_exclusion",
        "retention_hold",
        "retention_policy",
    }:
        try:
            row = conn.execute(f"SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM {table}").fetchone()
            details["tables"][table] = [int(row[0]), int(row[1])]
        except sqlite3.Error:
            details["tables"][table] = None
    return sha256_of_object(details)


def add_hold(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    reason: str,
    expires_at: str | None = None,
) -> str:
    hold_id = "hold_" + uuid.uuid4().hex[:12]
    with conn:
        conn.execute(
            "INSERT INTO retention_hold(hold_id, target_type, target_id, reason, created_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (hold_id, target_type, target_id, reason, _now_iso(), expires_at),
        )
        _bump_generation(conn)
    return hold_id


def list_holds(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT hold_id, target_type, target_id, reason, created_at, expires_at"
        " FROM retention_hold ORDER BY created_at, hold_id"
    ).fetchall()
    return [
        {
            "hold_id": row[0],
            "target_type": row[1],
            "target_id": row[2],
            "reason": row[3],
            "created_at": row[4],
            "expires_at": row[5],
        }
        for row in rows
    ]


def remove_hold(conn: sqlite3.Connection, hold_id: str) -> bool:
    with conn:
        cursor = conn.execute("DELETE FROM retention_hold WHERE hold_id=?", (hold_id,))
        _bump_generation(conn)
    return bool(cursor.rowcount)


def _protection(
    conn: sqlite3.Connection,
) -> tuple[set[str], set[str], list[tuple[str, str, str]]]:
    """返回 (受保护版本, 受保护分块, 记录范围保护)。

    所有对外声明的 hold 类型都必须有明确语义；未知类型直接拒绝，不能静默忽略。
    """
    versions: set[str] = set()
    chunks: set[str] = set()
    ranges: list[tuple[str, str, str]] = []
    for hold in conn.execute(
        "SELECT target_type, target_id FROM retention_hold"
    ).fetchall():
        target_type = str(hold[0])
        target_id = str(hold[1])
        if target_type == "version":
            versions.add(target_id)
        elif target_type == "snapshot":
            row = conn.execute(
                "SELECT data_version_id FROM pool_snapshot WHERE snapshot_id=?",
                (target_id,),
            ).fetchone()
            if row and row[0]:
                versions.add(str(row[0]))
            else:
                raise DataError(
                    "hold_target_not_found", f"hold 引用的股票池快照不存在: {target_id}"
                )
        elif target_type == "chunk":
            if not conn.execute(
                "SELECT 1 FROM data_chunk WHERE chunk_id=?", (target_id,)
            ).fetchone():
                raise DataError("hold_target_not_found", f"hold 引用的分块不存在: {target_id}")
            chunks.add(target_id)
        elif target_type == "record_range":
            parts = target_id.split("|")
            if len(parts) != 3 or parts[0] not in _BASE_TABLES:
                raise DataError(
                    "unsupported_hold_type",
                    "record_range 的 target_id 必须为 category|start|end 且类别有效",
                )
            ranges.append((parts[0], parts[1], parts[2]))
        elif target_type == "raw_archive":
            if not conn.execute(
                "SELECT 1 FROM raw_archive WHERE digest=?", (target_id,)
            ).fetchone():
                raise DataError(
                    "hold_target_not_found", f"hold 引用的原始档案不存在: {target_id}"
                )
        else:
            raise DataError(
                "unsupported_hold_type", f"不支持的 hold 类型: {target_type}"
            )
    for row in conn.execute("SELECT DISTINCT data_version_id FROM pool_snapshot").fetchall():
        if row[0]:
            versions.add(str(row[0]))
    for row in conn.execute(
        "SELECT dv.version_id FROM data_version dv WHERE dv.retention_state='active'"
        " AND NOT EXISTS (SELECT 1 FROM data_version d2 WHERE d2.source_code=dv.source_code"
        "   AND d2.retention_state='active' AND d2.rowid > dv.rowid)"
    ).fetchall():
        versions.add(str(row[0]))
    return versions, chunks, ranges


def _version_chunks(conn: sqlite3.Connection, version_id: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT chunk_id FROM data_version_chunk WHERE version_id=?", (version_id,)
        ).fetchall()
    }


def _version_member_ids(conn: sqlite3.Connection, version_id: str) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for category, record_id, _hash, _key in _iter_members(conn, version_id):
        result.setdefault(category, set()).add(record_id)
    return result


def _iter_members(conn, version_id):
    from .chunks import iter_version_members

    yield from iter_version_members(conn, version_id)


def _summary_view(plan: Mapping[str, Any], *, drop: Sequence[str] = ()) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key not in drop}


def _iso_cutoff(now: datetime, days: int) -> str:
    moment = now.astimezone(timezone.utc) - timedelta(days=int(days))
    return moment.isoformat(timespec="seconds")


def _incomplete_batch_ids(conn: sqlite3.Connection) -> set[str]:
    placeholders = ", ".join("?" for _ in _TERMINAL_BATCH_STATUSES)
    return {
        str(row[0])
        for row in conn.execute(
            f"SELECT batch_id FROM import_batch WHERE status NOT IN ({placeholders})",
            list(_TERMINAL_BATCH_STATUSES),
        ).fetchall()
    }


def _raw_hold_digests(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT target_id FROM retention_hold WHERE target_type='raw_archive'"
        ).fetchall()
    }


def _managed_raw_path(settings, stored: str) -> Path | None:
    """只接受本库原始档案目录内的相对路径；绝对路径或 ``..`` 一律跳过。"""
    candidate = Path(str(stored))
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    root = Path(settings.raw_archive_dir).resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _plan_request_log_retention(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    policy: Mapping[str, Any],
    incomplete_batches: set[str],
) -> dict[str, Any]:
    cutoff = _iso_cutoff(now, int(policy["request_log_days"]))
    where = "datetime(started_at) < datetime(?)"
    params: list[Any] = [cutoff]
    if incomplete_batches:
        placeholders = ", ".join("?" for _ in incomplete_batches)
        where += f" AND batch_id NOT IN ({placeholders})"
        params.extend(sorted(incomplete_batches))
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM import_request_log WHERE {where}", params
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT request_id, batch_id, interface, page_no, started_at"
        f" FROM import_request_log WHERE {where} ORDER BY request_id LIMIT ?",
        [*params, _RETENTION_ACTION_LIMIT],
    ).fetchall()
    return {
        "cutoff": cutoff,
        "eligible": total,
        "truncated": total > len(rows),
        "protected_batches": sorted(incomplete_batches),
        "request_ids": [int(row[0]) for row in rows],
        "items": [
            {
                "request_id": int(row[0]),
                "batch_id": str(row[1]),
                "interface": str(row[2]),
                "page_no": int(row[3]),
                "started_at": str(row[4]),
            }
            for row in rows
        ],
    }


def _plan_raw_archive_retention(
    settings,
    conn: sqlite3.Connection,
    *,
    now: datetime,
    policy: Mapping[str, Any],
    log_candidate_ids: Sequence[int],
    incomplete_batches: set[str],
) -> dict[str, Any]:
    cutoff = _iso_cutoff(now, int(policy["raw_archive_days"]))
    held = _raw_hold_digests(conn)
    where = "datetime(r.fetched_at) < datetime(?)"
    params: list[Any] = [cutoff]
    # 无论是否存在到期日志候选，都要排除“仍保留的请求日志”所引用的原始档案。
    log_exclusion = ""
    log_params: list[Any] = []
    if log_candidate_ids:
        placeholders = ", ".join("?" for _ in log_candidate_ids)
        log_exclusion = f" AND l.request_id NOT IN ({placeholders})"
        log_params = [int(item) for item in log_candidate_ids]
    where += (
        " AND NOT EXISTS (SELECT 1 FROM import_request_log l"
        f" WHERE l.raw_digest=r.digest{log_exclusion})"
    )
    params.extend(log_params)
    if incomplete_batches:
        placeholders = ", ".join("?" for _ in incomplete_batches)
        where += f" AND r.first_batch_id NOT IN ({placeholders})"
        params.extend(sorted(incomplete_batches))
    if held:
        placeholders = ", ".join("?" for _ in held)
        where += f" AND r.digest NOT IN ({placeholders})"
        params.extend(sorted(held))
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM raw_archive r WHERE {where}", params
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT r.digest, r.path, r.content_bytes, r.fetched_at, r.first_batch_id"
        f" FROM raw_archive r WHERE {where} ORDER BY r.fetched_at, r.digest LIMIT ?",
        [*params, _RETENTION_ACTION_LIMIT],
    ).fetchall()
    items: list[dict[str, Any]] = []
    unsafe = 0
    for row in rows:
        stored = str(row[1])
        if _managed_raw_path(settings, stored) is None:
            unsafe += 1
            continue
        items.append(
            {
                "digest": str(row[0]),
                "path": stored,
                "bytes": int(row[2]),
                "fetched_at": str(row[3]),
                "first_batch_id": str(row[4]),
            }
        )
    return {
        "cutoff": cutoff,
        "eligible": total,
        "truncated": total > len(rows),
        "unsafe_skipped": unsafe,
        "held": sorted(held),
        "items": items,
    }


def _pending_file_id(preview_id: str, stored: str) -> str:
    return "pf_" + hashlib.sha256(f"{preview_id}:{stored}".encode("utf-8")).hexdigest()[:16]


def _plan_pending_raw_files(
    settings,
    conn: sqlite3.Connection,
    side: sqlite3.Connection,
    *,
    preview_id: str,
) -> dict[str, Any]:
    """只重试本 preview 登记过、且业务行已消失的受管原始文件。

    不接管其他未完成清理的待办；不扫描未知目录、不凭位置或时间删除任何文件；
    越界路径（含符号链接逃逸）只标记失败，绝不删除。
    """
    rows = list_pending_files(
        side, limit=_RETENTION_ACTION_LIMIT, preview_id=preview_id
    )
    items: list[dict[str, Any]] = []
    unsafe: list[str] = []
    stale: list[str] = []
    for row in rows:
        stored = str(row["path"])
        if _managed_raw_path(settings, stored) is None:
            unsafe.append(str(row["file_id"]))
            continue
        # 业务库仍有该路径索引说明上次事务实际未提交，不能删除文件。
        if conn.execute(
            "SELECT 1 FROM raw_archive WHERE path=? LIMIT 1", (stored,)
        ).fetchone():
            stale.append(str(row["file_id"]))
            continue
        items.append(
            {
                "file_id": str(row["file_id"]),
                "preview_id": preview_id,
                "path": stored,
                "bytes": int(row["expected_bytes"] or 0),
                "state": str(row["state"]),
            }
        )
    if unsafe:
        mark_pending_file_state(side, unsafe, state="failed", detail="unsafe_path")
    if stale:
        mark_pending_file_state(side, stale, state="aborted", detail="business_row_still_present")
    return {
        "items": items,
        "truncated": len(rows) >= _RETENTION_ACTION_LIMIT,
        "unsafe": unsafe,
        "aborted": stale,
    }


def _plan_quality_issue_retention(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    policy: Mapping[str, Any],
    protected_versions: set[str],
    incomplete_batches: set[str],
) -> dict[str, Any]:
    cutoff = _iso_cutoff(now, int(policy["quality_issue_days"]))
    active_versions = {
        str(row[0])
        for row in conn.execute(
            "SELECT version_id FROM data_version WHERE retention_state='active'"
        ).fetchall()
    }
    protected = sorted(active_versions | protected_versions)
    # 用子查询表达版本保护，避免活动版本数量增长时参数列表无界。
    where = (
        "datetime(detected_at) < datetime(?)"
        " AND (data_version_id IS NULL OR NOT EXISTS ("
        "   SELECT 1 FROM data_version dv WHERE dv.version_id=quality_issue.data_version_id"
        "   AND dv.retention_state='active'))"
        " AND (data_version_id IS NULL OR data_version_id NOT IN ("
        "   SELECT target_id FROM retention_hold WHERE target_type='version'))"
        " AND (data_version_id IS NULL OR data_version_id NOT IN ("
        "   SELECT data_version_id FROM pool_snapshot WHERE data_version_id IS NOT NULL))"
    )
    params: list[Any] = [cutoff]
    if incomplete_batches:
        placeholders = ", ".join("?" for _ in incomplete_batches)
        where += f" AND (batch_id IS NULL OR batch_id NOT IN ({placeholders}))"
        params.extend(sorted(incomplete_batches))
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM quality_issue WHERE {where}", params
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT issue_id, rule_code, severity FROM quality_issue WHERE {where}"
        f" ORDER BY issue_id LIMIT ?",
        [*params, _RETENTION_ACTION_LIMIT],
    ).fetchall()
    by_rule: dict[str, int] = {}
    by_severity = {"info": 0, "warn": 0, "high": 0}
    for _issue_id, rule_code, severity in rows:
        by_rule[str(rule_code)] = by_rule.get(str(rule_code), 0) + 1
        by_severity[str(severity)] = by_severity.get(str(severity), 0) + 1
    return {
        "cutoff": cutoff,
        "eligible": total,
        "truncated": total > len(rows),
        "protected_versions": protected,
        "issue_ids": [int(row[0]) for row in rows],
        # 汇总只覆盖本次实际删除的记录；截断后由后续预览继续汇总与删除。
        "summary": {
            "total": len(rows),
            "eligible": total,
            "by_rule": by_rule,
            "by_severity": by_severity,
        },
    }


def _scan_backup_packages(settings) -> list[dict[str, Any]]:
    """只扫描本库备份目录中的备份包；不触碰任意用户目录。"""
    root = Path(settings.backup_dir)
    packages: list[dict[str, Any]] = []
    if not root.is_dir():
        return packages
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_NAME
        if not manifest_path.is_file():
            continue  # 不是本应用的备份包，绝不删除
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            stat = child.stat()
        except (OSError, ValueError):
            packages.append(
                {
                    "name": child.name,
                    "path": str(child.resolve()),
                    "kind": "unrecognized",
                    "verified": False,
                }
            )
            continue
        database = manifest.get("database") or {}
        packages.append(
            {
                "name": child.name,
                "path": str(child.resolve()),
                "kind": "candidate",
                "verified": bool(manifest.get("verified_at")),
                "instance_id": str(database.get("instance_id") or ""),
                "database_path": database.get("path"),
                "created_at": str(manifest.get("created_at") or ""),
                "generation": database.get("generation"),
                "manifest_sha256": _file_sha256(manifest_path),
                "bytes": directory_bytes(child),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    return packages


def _protected_backup_paths(side: sqlite3.Connection) -> dict[str, str]:
    """未完成清理/当前清理恢复点、未完成迁移引用的包路径 → 保护原因。

    ``recorded`` 表示业务已提交但中断收尾未完成，属于不可回收的对象；
    ``active`` 是最近一次已完成清理的恢复点，可在新恢复点登记后释放。
    """
    protected: dict[str, str] = {}
    for row in side.execute(
        "SELECT rp.preview_id, rp.path, rp.state,"
        " (SELECT COUNT(*) FROM cleanup_pending_file pf"
        "   WHERE pf.preview_id=rp.preview_id AND pf.state IN ('pending','failed'))"
        " FROM cleanup_recovery_point rp"
        " WHERE rp.state IN ('recorded','active')"
        " ORDER BY rp.created_at, rp.preview_id"
    ).fetchall():
        state = str(row[2])
        unfinished_files = int(row[3] or 0)
        reason = (
            "unfinished_cleanup_recovery_point"
            if state == "recorded" or unfinished_files
            else "active_recovery_point"
        )
        protected[_normalized_path(row[1])] = f"{reason}:{row[0]}"
    placeholders = ", ".join("?" for _ in _TERMINAL_BATCH_STATUSES)
    for row in side.execute(
        "SELECT run_id, backup_path FROM maintenance_run"
        f" WHERE backup_path IS NOT NULL AND state NOT IN ({placeholders})",
        list(_TERMINAL_BATCH_STATUSES),
    ).fetchall():
        protected[_normalized_path(row[1])] = f"incomplete_maintenance_run:{row[0]}"
    return protected


def _plan_backup_retention(
    settings,
    conn: sqlite3.Connection,
    side: sqlite3.Connection,
    *,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    retain = max(1, int(policy["backup_retain_count"]))
    instance_id, _created = _instance(conn)
    db_path = _normalized_path(settings.db_path)
    packages = _scan_backup_packages(settings)
    ordinary: list[dict[str, Any]] = []
    foreign: list[str] = []
    unverified: list[str] = []
    unrecognized: list[str] = []
    for package in packages:
        if package["kind"] == "unrecognized":
            unrecognized.append(package["name"])
            continue
        if not package["verified"]:
            unverified.append(package["name"])
            continue
        if package["instance_id"] != instance_id:
            foreign.append(package["name"])
            continue
        recorded = package.get("database_path")
        if recorded and _normalized_path(recorded) != db_path:
            foreign.append(package["name"])
            continue
        ordinary.append(package)
    ordinary.sort(
        key=lambda item: (item["created_at"], item["mtime_ns"], item["name"]), reverse=True
    )
    protected_paths = _protected_backup_paths(side)
    candidates: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    released_recovery_points: list[dict[str, Any]] = []
    # apply 至少会保障一个恢复点（默认新建；显式包则复用一个）。保留数量包含
    # 本次恢复点，因此现有普通包只保留 retain-1 个，预览如实说明新增点影响。
    existing_keep = max(0, retain - 1)
    kept = ordinary[:existing_keep]
    for package in ordinary[existing_keep:]:
        reason = protected_paths.get(_normalized_path(package["path"]))
        releasable = bool(reason and reason.startswith("active_recovery_point:"))
        if reason and not releasable:
            protected.append(
                {
                    "name": package["name"],
                    "path": package["path"],
                    "reason": reason,
                    "bytes": package["bytes"],
                }
            )
            continue
        item = {
            "name": package["name"],
            "path": package["path"],
            "manifest_sha256": package["manifest_sha256"],
            "created_at": package["created_at"],
            "bytes": package["bytes"],
        }
        if releasable:
            # 上一轮清理恢复点在新恢复点验证后释放，仍按保留数量参与本轮回收；
            # 若 apply 显式复用它，则在 apply 内再次受保护并如实报告超出数量。
            item["releasable_recovery_point"] = reason
            released_recovery_points.append(
                {"name": package["name"], "reason": reason}
            )
        candidates.append(item)
    kept_count = len(ordinary) - len(candidates)
    return {
        "retain_count": retain,
        "verified_total": len(ordinary),
        "kept": kept_count,
        "existing_kept": existing_keep,
        "new_recovery_point_expected": True,
        "candidates": candidates,
        "candidate_bytes": sum(int(item["bytes"]) for item in candidates),
        "protected": protected,
        "protected_bytes": sum(int(item["bytes"]) for item in protected),
        "released_recovery_points": released_recovery_points,
        "over_limit_protected": max(0, kept_count + 1 - retain),
        "foreign": foreign,
        "unverified": unverified,
        "unrecognized": unrecognized,
        "newest": [item["name"] for item in kept],
    }


def preview_cleanup(
    settings,
    *,
    source_code: str,
    as_of: str,
    policy_version: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """只读业务库，生成清理预览并写入 maintenance.db。"""
    as_of = require_yyyymmdd(as_of)
    business = sqlite3.connect(
        Path(settings.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0
    )
    business.row_factory = sqlite3.Row
    side = open_maintenance(settings)
    try:
        # 新清理不得接管其他未完成清理的待删文件或恢复保护。
        unfinished = unfinished_cleanup_summary(side)
        if unfinished["unfinished"]:
            details: list[str] = []
            for item in unfinished["pending"]:
                details.append(
                    f"{item['preview_id']} 尚有 {item['pending_files']} 个待删文件"
                )
            for item in unfinished["recorded_recovery_points"]:
                details.append(f"{item['preview_id']} 的恢复点尚未完成中断收尾")
            raise DataError(
                "pending_cleanup_unresolved",
                "存在未完成的清理（"
                + "；".join(details)
                + "）。请先对原预览完成中断收尾（修复其精确登记的恢复点后重试），"
                "或人工恢复原恢复点；新清理不会接管其待删文件、释放其保护或删除其备份。",
            )
        # 一致性只读事务：generation、计划与计数来自同一快照。
        business.execute("BEGIN")
        policy = current_policy(business, policy_version)
        instance_id, _created = _instance(business)
        generation = _generation(business)
        digest = state_digest(business)
        now_dt = now or datetime.now(timezone.utc)
        cutoff = now_dt - timedelta(days=int(policy["version_retention_days"]))
        protected_versions, protected_chunks, record_ranges = _protection(business)
        candidates = []
        for row in business.execute(
            "SELECT version_id, source_code, published_at, retention_state, window_mode"
            " FROM data_version WHERE retention_state='active' ORDER BY published_at, version_id"
        ).fetchall():
            version_id = str(row["version_id"])
            if version_id in protected_versions or str(row["source_code"]) != source_code:
                continue
            published = str(row["published_at"] or row["version_id"])
            try:
                published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                continue
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            if published_at > cutoff:
                continue
            candidates.append(
                {
                    "version_id": version_id,
                    "published_at": published,
                    "window_mode": row["window_mode"],
                }
            )

        candidate_ids = {item["version_id"] for item in candidates}
        candidate_chunks = {
            item["version_id"]: _version_chunks(business, item["version_id"])
            for item in candidates
        }
        retained_active_chunks: set[str] = set()
        for row in business.execute(
            "SELECT version_id FROM data_version WHERE retention_state='active'"
        ).fetchall():
            version_id = str(row[0])
            if version_id in candidate_ids:
                continue
            retained_active_chunks |= _version_chunks(business, version_id)
        all_candidate_chunks: set[str] = set()
        for chunks in candidate_chunks.values():
            all_candidate_chunks |= chunks
        reclaimable_chunks = all_candidate_chunks - retained_active_chunks - protected_chunks

        actions: list[dict[str, Any]] = []
        total_bytes = 0
        total_records = 0
        for candidate in candidates:
            version_id = candidate["version_id"]
            chunks = candidate_chunks[version_id]
            reclaimable = sorted(chunks & reclaimable_chunks)
            rows = _candidate_deletable_records(
                business,
                candidate_ids,
                reclaimable,
                as_of,
                policy,
                record_ranges,
            )
            bytes_estimate = sum(
                int(
                    business.execute(
                        "SELECT byte_size FROM data_chunk WHERE chunk_id=?", (chunk_id,)
                    ).fetchone()[0]
                )
                for chunk_id in reclaimable
            )
            record_count = sum(len(item["record_ids"]) for item in rows)
            actions.append(
                {
                    "action": "archive_version",
                    "version_id": version_id,
                    "published_at": candidate["published_at"],
                    "chunk_ids": sorted(chunks),
                    "reclaimable_chunk_ids": reclaimable,
                    "records": rows,
                    "record_count": record_count,
                    "estimated_bytes": bytes_estimate,
                }
            )
            total_bytes += bytes_estimate
            total_records += record_count

        # 有限保留：原始档案、请求日志、质量问题与普通备份按政策进入同一预览。
        incomplete_batches = _incomplete_batch_ids(business)
        log_plan = _plan_request_log_retention(
            business, now=now_dt, policy=policy, incomplete_batches=incomplete_batches
        )
        raw_plan = _plan_raw_archive_retention(
            settings,
            business,
            now=now_dt,
            policy=policy,
            log_candidate_ids=log_plan["request_ids"],
            incomplete_batches=incomplete_batches,
        )
        quality_plan = _plan_quality_issue_retention(
            business,
            now=now_dt,
            policy=policy,
            protected_versions=protected_versions,
            incomplete_batches=incomplete_batches,
        )
        backup_plan = _plan_backup_retention(settings, business, side, policy=policy)

        if log_plan["request_ids"]:
            actions.append({"action": "delete_request_log", **log_plan})
        if raw_plan["items"]:
            actions.append({"action": "delete_raw_archive", **raw_plan})
        if quality_plan["issue_ids"]:
            actions.append({"action": "delete_quality_issue", **quality_plan})
        if backup_plan["candidates"]:
            actions.append({"action": "prune_backup", **backup_plan})

        preview_id = "cp_" + hashlib.sha256(
            canonical_json_bytes(
                {
                    "instance_id": instance_id,
                    "db": str(Path(settings.db_path).resolve()),
                    "generation": generation,
                    "digest": digest,
                    "actions": actions,
                    "nonce": uuid.uuid4().hex,
                }
            )
        ).hexdigest()[:16]
        created_at = _now_iso()
        expires = (
            datetime.now(timezone.utc)
            + timedelta(minutes=int(policy["preview_valid_minutes"]))
        ).isoformat(timespec="seconds")
        summary = {
            "policy_version": policy["policy_version"],
            "candidate_versions": len(candidates),
            "record_count": total_records,
            "estimated_bytes": total_bytes,
            "protected_versions": sorted(protected_versions),
            "as_of": as_of,
            "window_years": int(policy["working_window_years"]),
            "request_log": _summary_view(log_plan, drop=("request_ids", "items")),
            "raw_archive": _summary_view(raw_plan, drop=("items",))
            | {
                "candidate_bytes": sum(int(item["bytes"]) for item in raw_plan["items"]),
            },
            "quality_issue": _summary_view(quality_plan, drop=("issue_ids",)),
            "backup": backup_plan,
        }
        record_preview(
            side,
            preview_id=preview_id,
            policy_version=str(policy["policy_version"]),
            source_code=source_code,
            as_of=as_of,
            instance_id=instance_id,
            database_path=str(Path(settings.db_path).resolve()),
            generation=generation,
            state_digest=digest,
            plan={"actions": actions, "summary": summary},
            summary=summary,
            created_at=created_at,
            expires_at=expires,
        )
        return {
            "preview_id": preview_id,
            "created_at": created_at,
            "expires_at": expires,
            "plan": {"actions": actions, "summary": summary},
            "summary": summary,
            "generation": generation,
        }
    finally:
        business.close()
        side.close()


def _candidate_deletable_records(
    conn: sqlite3.Connection,
    candidate_ids: set[str],
    chunk_ids: Sequence[str],
    as_of: str,
    policy: Mapping[str, Any],
    record_ranges: Sequence[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """按分块流式收集候选记录；不复制其它版本的完整成员集合。"""
    if not chunk_ids:
        return []
    window_start = _window_start(as_of, int(policy["working_window_years"]))
    calendar_start = f"{int(window_start[:4]) - 1}0101"
    ids_by_category: dict[str, set[int]] = {}
    for start in range(0, len(chunk_ids), 400):
        batch = chunk_ids[start : start + 400]
        placeholders = ", ".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT c.category, m.record_id, m.biz_date FROM data_chunk c"
            f" JOIN data_chunk_member m ON m.chunk_id=c.chunk_id"
            f" WHERE c.chunk_id IN ({placeholders})",
            batch,
        ).fetchall()
        for category, record_id, biz_date in rows:
            category = str(category)
            if category in {"daily", "adj_factor", "suspension"}:
                if not biz_date or str(biz_date) >= window_start:
                    continue
            elif category in {"calendar", "corporate_action"}:
                if not biz_date or str(biz_date) >= calendar_start:
                    continue
            ids_by_category.setdefault(category, set()).add(int(record_id))

    result: list[dict[str, Any]] = []
    for category, ids in sorted(ids_by_category.items()):
        if category not in _BASE_TABLES:
            continue
        # 旧全量成员表中的引用（未被转换的版本）同样保护记录。
        protected_ids: set[int] = set()
        id_list = sorted(ids)
        for start in range(0, len(id_list), 400):
            batch = id_list[start : start + 400]
            placeholders = ", ".join("?" for _ in batch)
            candidate_list = list(candidate_ids) or [""]
            candidate_placeholders = ", ".join("?" for _ in candidate_list)
            rows = conn.execute(
                f"SELECT DISTINCT dm.record_id FROM data_version_member dm"
                f" JOIN data_version dv ON dv.version_id=dm.version_id"
                f" WHERE dm.category=? AND dm.record_id IN ({placeholders})"
                f" AND dv.version_id NOT IN ({candidate_placeholders})",
                [category, *batch, *candidate_list],
            ).fetchall()
            protected_ids.update(int(row[0]) for row in rows)
        id_list = [record_id for record_id in id_list if record_id not in protected_ids]
        date_field = _DATE_FIELDS.get(category)
        if date_field:
            filtered: list[int] = []
            for start in range(0, len(id_list), 400):
                filtered.extend(
                    _filter_window_old(
                        conn, category, date_field, id_list[start : start + 400], as_of, policy
                    )
                )
            id_list = filtered
        for range_category, range_start, range_end in record_ranges:
            if range_category != category or not date_field:
                continue
            kept: list[int] = []
            for start in range(0, len(id_list), 400):
                batch = id_list[start : start + 400]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT id FROM {_BASE_TABLES[category]} WHERE id IN ({placeholders})"
                    f" AND {date_field} BETWEEN ? AND ?",
                    [*batch, range_start, range_end],
                ).fetchall()
                blocked = {int(row[0]) for row in rows}
                kept.extend(record_id for record_id in batch if record_id not in blocked)
            id_list = kept
        if id_list:
            result.append({"category": category, "record_ids": sorted(id_list)})
    return result


def _filter_window_old(
    conn: sqlite3.Connection,
    category: str,
    date_field: str,
    record_ids: Sequence[int],
    as_of: str,
    policy: Mapping[str, Any],
) -> list[int]:
    """只挑窗口外、且不是每证券窗口前锚点的记录。"""
    if not record_ids:
        return []
    window_start = _window_start(as_of, int(policy["working_window_years"]))
    spec = spec_for(category)
    keys = ", ".join(spec.key_fields)
    keep_anchor: set[int] = set()
    if date_field in {"trade_date", "end_date"} and "ts_code" in spec.key_fields:
        for row in conn.execute(
            f"SELECT ts_code, MAX({date_field}) FROM {spec.table}"
            f" WHERE {date_field} < ? GROUP BY ts_code",
            (window_start,),
        ).fetchall():
            anchor = conn.execute(
                f"SELECT id FROM {spec.table} WHERE ts_code=? AND {date_field}=?"
                f" AND revision_no=(SELECT MAX(revision_no) FROM {spec.table} t2"
                f" WHERE t2.ts_code={spec.table}.ts_code AND t2.{date_field}={spec.table}.{date_field})",
                (row[0], row[1]),
            ).fetchone()
            if anchor:
                keep_anchor.add(int(anchor[0]))
    result = []
    placeholders = ", ".join("?" for _ in record_ids)
    rows = conn.execute(
        f"SELECT id, {keys}, {date_field} FROM {spec.table} WHERE id IN ({placeholders})",
        list(record_ids),
    ).fetchall()
    for row in rows:
        record_id = int(row[0])
        value = str(row[-1] or "")
        if value and value < window_start and record_id not in keep_anchor:
            result.append(record_id)
    return sorted(result)


def _window_start(as_of: str, years: int) -> str:
    date = datetime.strptime(as_of, "%Y%m%d").date()
    try:
        start = date.replace(year=date.year - years)
    except ValueError:  # 2 月 29 日
        start = date.replace(year=date.year - years, day=28)
    return yyyymmdd(start)


def apply_cleanup(
    settings,
    *,
    preview_id: str,
    create_recovery_point: bool = True,
    recovery_point: Path | None = None,
) -> dict[str, Any]:
    side = open_maintenance(settings)
    try:
        preview = get_preview(side, preview_id)
        if preview is None:
            raise DataError("preview_not_found", f"清理预览 {preview_id} 不存在")
        if preview["state"] == "applied":
            has_pending = side.execute(
                "SELECT 1 FROM cleanup_pending_file WHERE preview_id=?"
                " AND state IN ('pending','failed') LIMIT 1",
                (preview_id,),
            ).fetchone()
            if has_pending is None:
                return {"preview_id": preview_id, "status": "already_applied"}
            # 业务已完成但外部文件收尾失败：不受预览 TTL 限制，仍须在锁内
            # 完整复验原恢复点后重试本 preview 自己的待删文件。
            if str(Path(settings.db_path).resolve()) != preview["database_path"]:
                raise DataError("preview_database_mismatch", "预览不属于当前数据库")
            lock = DataWriteLock(settings.lock_dir / "data-write.lock", wait_seconds=30.0)
            lock.acquire()
            try:
                conn = sqlite3.connect(str(settings.db_path), timeout=30.0)
                conn.row_factory = sqlite3.Row
                try:
                    instance_id, _created = _instance(conn)
                    if instance_id != preview["instance_id"]:
                        raise DataError(
                            "preview_instance_mismatch", "预览实例标识不匹配"
                        )
                    return _finalize_applied_pending(settings, side, conn, preview)
                finally:
                    conn.close()
            finally:
                lock.release()

        if preview["state"] != "open":
            raise DataError("preview_not_open", f"预览状态为 {preview['state']}")
        if str(Path(settings.db_path).resolve()) != preview["database_path"]:
            raise DataError("preview_database_mismatch", "预览不属于当前数据库")

        lock = DataWriteLock(settings.lock_dir / "data-write.lock", wait_seconds=30.0)
        lock.acquire()
        try:
            conn = sqlite3.connect(str(settings.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            instance_id, _created = _instance(conn)
            if instance_id != preview["instance_id"]:
                update_preview_state(side, preview_id, "rejected")
                raise DataError("preview_instance_mismatch", "预览实例标识不匹配")
            journal = _journal_state(conn, preview_id)
            expected_targets = _journaled_targets(preview["plan"]["actions"])
            if expected_targets and all(
                journal.get(target) == "applied" for target in expected_targets
            ):
                # 业务事务已提交但侧库未完成：按 journal 核对事实并收尾。
                # 已提交的恢复不受预览 TTL 限制；此处位于失败标记 try 之外，
                # 恢复点完整验证失败时不得改动待删文件、旧保护或动作状态。
                return _reconcile_committed_cleanup(
                    settings, side, conn, preview, journal
                )
            # 未实际执行的过期预览仍然拒绝。
            expires_at = datetime.fromisoformat(
                str(preview["expires_at"]).replace("Z", "+00:00")
            )
            if expires_at < datetime.now(timezone.utc):
                update_preview_state(side, preview_id, "expired")
                raise DataError("preview_expired", "清理预览已过期，请重新生成")

            quality_summaries: list[str] = []
            committed = False
            try:
                conn.execute("BEGIN IMMEDIATE")
                if _generation(conn) != int(preview["generation"]):
                    conn.rollback()
                    update_preview_state(side, preview_id, "rejected")
                    raise DataError("preview_stale", "数据库状态已变化，旧预览失效")
                if state_digest(conn) != preview["state_digest"]:
                    conn.rollback()
                    update_preview_state(side, preview_id, "rejected")
                    raise DataError("preview_stale", "数据库状态摘要已变化，旧预览失效")

                actions = preview["plan"]["actions"]
                # 在写锁内、创建/登记新恢复点之前检查其他未完成清理与旧计划
                # foreign item；拒绝时不会留下新 recorded 孤儿。
                unfinished = unfinished_cleanup_summary(side)
                foreign_previews = [
                    item for item in unfinished["preview_ids"] if item != preview_id
                ]
                if foreign_previews:
                    raise DataError(
                        "pending_cleanup_unresolved",
                        "存在其他未完成清理（"
                        + "、".join(foreign_previews)
                        + "）；拒绝创建新恢复点。请先对原预览完成中断收尾或人工恢复。",
                    )
                for planned in actions:
                    if str(planned.get("action")) != "retry_pending_raw_files":
                        continue
                    foreign_items = [
                        item
                        for item in planned.get("items") or []
                        if str(item.get("preview_id")) != preview_id
                    ]
                    if foreign_items:
                        raise DataError(
                            "pending_cleanup_unresolved",
                            "预览计划包含其他未完成清理的待删文件，拒绝执行；"
                            "请先完成原预览的中断收尾",
                        )

                # 恢复保障先于任何删除：默认新建并验证；显式替代恢复点必须能证明
                # 它就是本库操作前的状态（实例 + 路径 + 完整内容指纹）。
                if create_recovery_point:
                    backup = create_backup(settings, verify=True)
                    recovery = validate_recovery_point(
                        settings, backup.path, current_conn=conn
                    )
                else:
                    if recovery_point is None:
                        raise DataError(
                            "recovery_point_required",
                            "apply 需要已验证的操作前恢复点；请提供 --recovery-point 或允许创建",
                        )
                    recovery = validate_recovery_point(
                        settings, Path(recovery_point), current_conn=conn
                    )
                # 任何删除/提交之前先精确持久登记恢复点；登记失败即中止。
                record_recovery_point(
                    side,
                    preview_id=preview_id,
                    path=str(recovery["path"]),
                    manifest_sha256=recovery.get("manifest_sha256"),
                    instance_id=recovery.get("instance_id"),
                    binding=recovery.get("binding"),
                    content_fingerprint=recovery.get("content_fingerprint"),
                    state="recorded",
                )

                freed = 0
                archived = 0
                deleted = {
                    "request_logs": 0,
                    "quality_issues": 0,
                    "raw_archive_rows": 0,
                    "raw_files": 0,
                    "pending_files": 0,
                    "backups": 0,
                }
                external_bytes = 0
                results: list[dict[str, Any]] = []
                raw_paths_by_seq: dict[int, list[str]] = {}
                pending_by_seq: dict[int, dict[str, str]] = {}
                for seq, action in enumerate(actions):
                    kind = str(action["action"])
                    target_type, target_id = _action_identity(kind, action)
                    record_action(
                        side,
                        preview_id=preview_id,
                        seq=seq,
                        action=kind,
                        target_type=target_type,
                        target_id=target_id,
                        state="prepared",
                    )
                    detail = ""
                    freed_action = 0
                    if kind == "archive_version":
                        result = _archive_and_reclaim(
                            conn, settings, action, preview["summary"]["as_of"], side, preview_id, seq
                        )
                        freed_action = int(result.get("freed_bytes") or 0)
                        archived += 1
                        detail = f"archive={result['archive_id']}"
                    elif kind == "delete_request_log":
                        count = _delete_rows_by_ids(
                            conn, "import_request_log", "request_id", action["request_ids"]
                        )
                        deleted["request_logs"] += count
                        detail = f"deleted_request_logs={count}"
                        _record_journal(conn, preview_id, kind, target_type, target_id, detail)
                    elif kind == "delete_quality_issue":
                        summary_id = (
                            "qs_"
                            + hashlib.sha256(f"{preview_id}:{seq}".encode("utf-8")).hexdigest()[:16]
                        )
                        record_quality_summary(
                            side,
                            summary_id=summary_id,
                            preview_id=preview_id,
                            source_code=preview["source_code"],
                            cutoff=str(action["cutoff"]),
                            issue_count=len(action["issue_ids"]),
                            summary=action["summary"],
                        )
                        quality_summaries.append(summary_id)
                        count = _delete_rows_by_ids(
                            conn, "quality_issue", "issue_id", action["issue_ids"]
                        )
                        deleted["quality_issues"] += count
                        detail = f"summary={summary_id};deleted_quality_issues={count}"
                        _record_journal(conn, preview_id, kind, target_type, target_id, detail)
                    elif kind == "delete_raw_archive":
                        # 先登记待删文件（侧库），再做业务事务删除；中断后只重试这里登记过的路径。
                        pending_map: dict[str, str] = {}
                        for item in action["items"]:
                            file_id = _pending_file_id(preview_id, str(item["path"]))
                            record_pending_file(
                                side,
                                file_id=file_id,
                                preview_id=preview_id,
                                path=str(item["path"]),
                                expected_bytes=int(item.get("bytes") or 0),
                            )
                            pending_map[str(item["path"])] = file_id
                        count, paths = _delete_raw_archive_rows(conn, action["items"])
                        deleted["raw_archive_rows"] += count
                        raw_paths_by_seq[seq] = paths
                        pending_by_seq[seq] = pending_map
                        detail = f"deleted_raw_archive_rows={count};files_pending={len(paths)}"
                        _record_journal(conn, preview_id, kind, target_type, target_id, detail)
                    elif kind == "retry_pending_raw_files":
                        raw_paths_by_seq[seq] = [
                            str(item["path"]) for item in action["items"]
                        ]
                        pending_by_seq[seq] = {
                            str(item["path"]): str(item["file_id"])
                            for item in action["items"]
                        }
                        detail = f"pending_files_pending={len(action['items'])}"
                        _record_journal(conn, preview_id, kind, target_type, target_id, detail)
                    elif kind == "prune_backup":
                        # 即使只删外部备份包，也在业务事务内留下 journal 事实，
                        # 保证提交后中断仍可按同一预览核对收尾。
                        _record_journal(
                            conn,
                            preview_id,
                            kind,
                            target_type,
                            target_id,
                            "prepared_for_post_commit",
                        )
                        detail = "backup_prune_deferred_to_post_commit"
                    else:
                        raise DataError(
                            "unknown_cleanup_action", f"未知清理动作: {kind}"
                        )
                    results.append(
                        {
                            "seq": seq,
                            "kind": kind,
                            "detail": detail,
                            "bytes": freed_action,
                        }
                    )
                    freed += freed_action

                _bump_generation(conn)
                conn.commit()
                committed = True

                # ---- 业务事务已提交：完成外部文件删除与侧库状态 ----
                external_bytes, backup_result, failed_notes = _post_commit_cleanup(
                    settings,
                    side,
                    conn,
                    preview_id=preview_id,
                    actions=actions,
                    results=results,
                    raw_paths_by_seq=raw_paths_by_seq,
                    pending_by_seq=pending_by_seq,
                    recovery=recovery,
                    deleted=deleted,
                    quality_summaries=quality_summaries,
                )
                return {
                    "preview_id": preview_id,
                    "status": "applied",
                    "archived_versions": archived,
                    "freed_bytes": freed,
                    "external_bytes_freed": external_bytes,
                    "deleted": deleted,
                    "recovery_point": recovery["path"],
                    "recovery_point_binding": recovery.get("binding"),
                    "backup_retention": backup_result,
                    "notes": failed_notes,
                }
            except Exception:
                if committed:
                    # 业务事务已经提交：不能再回滚或把动作伪报为失败；保留真实状态。
                    raise
                conn.rollback()
                try:
                    mark_preview_pending_rolled_back(side, preview_id)
                    set_recovery_point_state(side, preview_id, "aborted")
                except Exception:
                    pass
                for summary_id in quality_summaries:
                    try:
                        update_quality_summary_state(
                            side, summary_id, state="rolled_back"
                        )
                    except Exception:
                        pass
                for seq, _action in enumerate(preview.get("plan", {}).get("actions", [])):
                    try:
                        update_action(side, preview_id=preview_id, seq=seq, state="failed")
                    except Exception:
                        pass
                raise
            finally:
                conn.close()
        finally:
            lock.release()
    finally:
        side.close()


def _journaled_targets(actions: Sequence[Mapping[str, Any]]) -> list[str]:
    targets: list[str] = []
    for action in actions:
        kind = str(action.get("action"))
        if kind in {
            "archive_version",
            "delete_request_log",
            "delete_raw_archive",
            "delete_quality_issue",
            "retry_pending_raw_files",
            "prune_backup",
        }:
            targets.append(_action_identity(kind, action)[1])
    return targets


def _journal_state(conn: sqlite3.Connection, preview_id: str) -> dict[str, str]:
    try:
        return {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT target_id, state FROM cleanup_journal WHERE preview_id=?",
                (preview_id,),
            ).fetchall()
        }
    except sqlite3.Error:
        return {}


def _validate_registered_point(
    settings,
    side: sqlite3.Connection,
    conn: sqlite3.Connection,
    preview_id: str,
) -> dict[str, Any]:
    """完整验证本 preview 提交前精确登记的恢复点；失败时零副作用抛出。"""
    recorded_row = side.execute(
        "SELECT path, manifest_sha256, instance_id, binding, content_fingerprint, state"
        " FROM cleanup_recovery_point WHERE preview_id=?",
        (preview_id,),
    ).fetchone()
    if recorded_row is None:
        raise DataError(
            "recovery_point_revalidation_failed",
            "缺少首次清理前的精确恢复点登记；拒绝收尾，保留全部保护与待删文件",
        )
    recorded = dict(recorded_row)
    if str(recorded["state"]) not in {"recorded", "active"}:
        raise DataError(
            "recovery_point_revalidation_failed",
            f"恢复点登记状态为 {recorded['state']}，无法证明可恢复；拒绝收尾",
        )
    current_instance, _created = _instance(conn)
    try:
        verified = verify_registered_recovery_point(
            settings,
            package=str(recorded["path"]),
            manifest_sha256=recorded.get("manifest_sha256"),
            instance_id=recorded.get("instance_id"),
            content_fingerprint=recorded.get("content_fingerprint"),
            current_instance_id=current_instance,
        )
    except DataError as exc:
        raise DataError(
            "recovery_point_revalidation_failed",
            f"恢复前完整验证未通过（{exc.reason}）: {exc}；"
            "保留待删文件、旧恢复点保护与其他备份，未改动预览状态",
        ) from exc
    return {**recorded, **verified}


def _retry_own_pending_files(
    settings,
    conn: sqlite3.Connection,
    side: sqlite3.Connection,
    *,
    preview_id: str,
) -> dict[str, Any]:
    """只删除本 preview 登记且业务索引已消失的受管文件；其他待办保持不动。"""
    notes: list[str] = []
    foreign_pending = [
        row
        for row in list_pending_files(side, limit=_RETENTION_ACTION_LIMIT)
        if str(row["preview_id"]) != preview_id
    ]
    if foreign_pending:
        notes.append(
            "保留其他未完成清理的待删文件 "
            f"{len(foreign_pending)} 个（preview: "
            + "、".join(sorted({str(row['preview_id']) for row in foreign_pending}))
            + "）"
        )
    pending_plan = _plan_pending_raw_files(
        settings, conn, side, preview_id=preview_id
    )
    file_result = _delete_managed_files(
        settings, [str(item["path"]) for item in pending_plan["items"]]
    )
    pending_map = {str(item["path"]): str(item["file_id"]) for item in pending_plan["items"]}
    done_paths = set(file_result["deleted"]) | set(file_result["missing"])
    bad_paths = set(file_result["unsafe"]) | {
        item.split(":", 1)[0] for item in file_result["failed"]
    }
    mark_pending_file_state(
        side,
        [pending_map[path] for path in pending_map if path in done_paths],
        state="deleted",
        detail="file_absent_or_removed",
    )
    mark_pending_file_state(
        side,
        [pending_map[path] for path in pending_map if path in bad_paths],
        state="failed",
        detail="unlink_failed_or_unsafe",
    )
    if file_result["failed"] or file_result["unsafe"]:
        notes.append("部分登记文件仍未删除，保留 pending/failed 供后续重试")
    return {"plan": pending_plan, "result": file_result, "notes": notes}


def _finalize_applied_pending(
    settings,
    side: sqlite3.Connection,
    conn: sqlite3.Connection,
    preview: Mapping[str, Any],
) -> dict[str, Any]:
    """预览已 applied 但仍有待删文件：完整复验原恢复点后只重试本 preview 的文件。"""
    preview_id = str(preview["preview_id"])
    _validate_registered_point(settings, side, conn, preview_id)
    retried = _retry_own_pending_files(
        settings, conn, side, preview_id=preview_id
    )
    file_result = retried["result"]
    failed = len(file_result["failed"]) + len(file_result["unsafe"])
    for row in side.execute(
        "SELECT seq, action, state FROM cleanup_action WHERE preview_id=?",
        (preview_id,),
    ).fetchall():
        if str(row[1]) not in {"delete_raw_archive", "retry_pending_raw_files"}:
            continue
        update_action(
            side,
            preview_id=preview_id,
            seq=int(row[0]),
            state="failed" if failed else "applied",
            bytes_freed=int(file_result["bytes"]),
            detail=(
                f"finalized_files_deleted={len(file_result['deleted'])};"
                f"files_failed={failed}"
            ),
        )
    return {
        "preview_id": preview_id,
        "status": "finalized_pending_files",
        "deleted_files": len(file_result["deleted"]),
        "failed_files": failed,
        "notes": retried["notes"],
    }


def _reconcile_committed_cleanup(
    settings,
    side: sqlite3.Connection,
    conn: sqlite3.Connection,
    preview: Mapping[str, Any],
    journal: Mapping[str, str],
) -> dict[str, Any]:
    """业务事务已提交但侧库未标记 applied 时，按业务 journal 收尾，不重复删除。"""
    preview_id = str(preview["preview_id"])
    actions = preview["plan"]["actions"]
    notes: list[str] = []

    # 1) 在任何重试删除/保护释放/备份回收之前，完整验证提交前精确登记的恢复点。
    #    验证失败时零副作用，保留待删文件、全部旧恢复点保护与其他备份。
    recovery_ok = _validate_registered_point(settings, side, conn, preview_id)

    # 2) 验证通过后才重试本 preview 登记、业务行已消失的受管文件。
    #    其他未完成清理的待办一律不动，其恢复点/备份保护继续保留。
    retried = _retry_own_pending_files(
        settings, conn, side, preview_id=preview_id
    )
    file_result = retried["result"]
    notes.extend(retried["notes"])

    backup_action = next(
        (item for item in actions if str(item.get("action")) == "prune_backup"), None
    )
    backup_result = None
    set_recovery_point_state(side, preview_id, "active")
    release_previous_recovery_points(side, keep_preview_id=preview_id)
    if backup_action is not None:
        backup_result = _prune_backups(
            settings,
            side,
            conn,
            backup_action,
            current_recovery=str(recovery_ok["path"]),
        )
        if backup_result["failed"]:
            notes.append("备份包删除失败，保留可重试记录")

    # 3) 本次预览的质量汇总标记为已应用。
    for row in side.execute(
        "SELECT summary_id FROM quality_issue_summary WHERE preview_id=?", (preview_id,)
    ).fetchall():
        update_quality_summary_state(side, str(row[0]), state="applied")

    # 5) 动作状态以业务 journal 事实为准。
    for seq, action in enumerate(actions):
        kind = str(action.get("action"))
        target_type, target_id = _action_identity(kind, action)
        if kind == "prune_backup":
            detail = (
                None
                if backup_result is None
                else f"deleted={len(backup_result['deleted'])};"
                f"skipped={len(backup_result['skipped'])};"
                f"failed={len(backup_result['failed'])}"
            )
            state = "applied"
            if backup_result and backup_result["failed"]:
                state = "failed"
            bytes_freed = int((backup_result or {}).get("bytes_freed") or 0)
        elif kind == "retry_pending_raw_files":
            detail = f"files_deleted={len(file_result['deleted'])}"
            state = "applied"
            if file_result["failed"] or file_result["unsafe"]:
                state = "failed"
            bytes_freed = int(file_result["bytes"])
        else:
            detail = f"reconciled_from_journal:{journal.get(target_id, 'missing')}"
            state = "applied"
            bytes_freed = 0
        update_action(
            side,
            preview_id=preview_id,
            seq=seq,
            state=state,
            bytes_freed=bytes_freed,
            detail=detail,
        )
    update_preview_state(side, preview_id, "applied")
    return {
        "preview_id": preview_id,
        "status": "reconciled_after_interruption",
        "archived_versions": sum(
            1 for action in actions if action.get("action") == "archive_version"
        ),
        "recovered_from_journal": sorted(journal),
        "recovery_point": recovery_ok["path"] if recovery_ok else None,
        "recovery_point_binding": recovery_ok.get("binding"),
        "backup_retention": backup_result,
        "pending_files": {
            "deleted": len(file_result["deleted"]),
            "failed": len(file_result["failed"]) + len(file_result["unsafe"]),
        },
        "notes": notes,
    }


def _post_commit_cleanup(
    settings,
    side: sqlite3.Connection,
    conn: sqlite3.Connection,
    *,
    preview_id: str,
    actions: Sequence[Mapping[str, Any]],
    results: list[dict[str, Any]],
    raw_paths_by_seq: Mapping[int, Sequence[str]],
    pending_by_seq: Mapping[int, Mapping[str, str]],
    recovery: Mapping[str, Any],
    deleted: dict[str, int],
    quality_summaries: Sequence[str],
) -> tuple[int, dict[str, Any] | None, list[str]]:
    """业务事务提交后的外部文件与侧库收尾；失败保留可解释状态，不回滚已提交事实。"""
    external_bytes = 0
    failed_notes: list[str] = []
    for result in results:
        seq = int(result["seq"])
        if seq not in raw_paths_by_seq:
            continue
        file_result = _delete_managed_files(settings, raw_paths_by_seq[seq])
        if result["kind"] == "retry_pending_raw_files":
            deleted["pending_files"] += len(file_result["deleted"])
        else:
            deleted["raw_files"] += len(file_result["deleted"])
        external_bytes += int(file_result["bytes"])
        pending_map = pending_by_seq.get(seq, {})
        done_paths = set(file_result["deleted"]) | set(file_result["missing"])
        bad_paths = set(file_result["unsafe"]) | {
            item.split(":", 1)[0] for item in file_result["failed"]
        }
        done_ids = [
            pending_map[path] for path in raw_paths_by_seq[seq] if path in done_paths
            and path in pending_map
        ]
        bad_ids = [
            pending_map[path] for path in raw_paths_by_seq[seq] if path in bad_paths
            and path in pending_map
        ]
        mark_pending_file_state(side, done_ids, state="deleted", detail="file_absent_or_removed")
        mark_pending_file_state(side, bad_ids, state="failed", detail="unlink_failed_or_unsafe")
        if file_result["failed"] or file_result["unsafe"]:
            failed_notes.append(
                f"seq={seq}: 文件删除失败 {len(file_result['failed']) + len(file_result['unsafe'])} 个；"
                "数据库行已删除，登记记录保留供下次清理重试"
            )
            result["state"] = "failed"
            result["detail"] += (
                f";failed_files={len(file_result['failed']) + len(file_result['unsafe'])}"
            )
        else:
            result["detail"] += f";files_deleted={len(file_result['deleted'])}"

    # 当前恢复点先登记为 active，再把上一轮恢复点释放为 released：
    # 新点已包含清理前状态，旧点不再需要，可参与本轮数量保留。
    try:
        recorded = side.execute(
            "SELECT 1 FROM cleanup_recovery_point WHERE preview_id=?", (preview_id,)
        ).fetchone()
        if recorded is None:
            raise DataError(
                "recovery_point_registration_missing",
                "缺少提交前的恢复点登记，拒绝释放旧保护",
            )
        set_recovery_point_state(side, preview_id, "active")
        release_previous_recovery_points(side, keep_preview_id=preview_id)
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            "cleanup_post_commit_failed",
            f"业务清理已提交，但恢复点状态更新失败: {exc}；请用 cleanup status 核对",
        ) from exc

    backup_result = None
    prune_action = next(
        (item for item in actions if str(item.get("action")) == "prune_backup"), None
    )
    if prune_action is not None:
        try:
            backup_result = _prune_backups(
                settings, side, conn, prune_action, current_recovery=str(recovery["path"])
            )
            deleted["backups"] += len(backup_result["deleted"])
            external_bytes += int(backup_result["bytes_freed"])
            result = next(
                item for item in results if item["kind"] == "prune_backup"
            )
            result["detail"] = (
                f"deleted={len(backup_result['deleted'])};"
                f"skipped={len(backup_result['skipped'])};"
                f"failed={len(backup_result['failed'])};"
                f"over_limit_protected={backup_result['over_limit_protected']}"
            )
            if backup_result["failed"]:
                result["state"] = "failed"
                failed_notes.append(f"备份包删除失败 {len(backup_result['failed'])} 个")
        except Exception as exc:  # noqa: BLE001 - 保留审计并继续完成侧库状态
            failed_notes.append(f"备份保留执行失败: {exc}")
            result = next(
                (item for item in results if item["kind"] == "prune_backup"), None
            )
            if result is not None:
                result["state"] = "failed"
                result["detail"] += f";error={exc}"

    try:
        for summary_id in quality_summaries:
            update_quality_summary_state(side, summary_id, state="applied")
        for result in results:
            update_action(
                side,
                preview_id=preview_id,
                seq=int(result["seq"]),
                state=str(result.get("state") or "applied"),
                bytes_freed=int(result["bytes"]),
                detail=str(result["detail"]),
            )
        update_preview_state(side, preview_id, "applied")
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            "cleanup_post_commit_failed",
            f"业务清理已提交，但外部文件或侧库状态收尾失败: {exc}；请用 cleanup status 核对",
        ) from exc
    return external_bytes, backup_result, failed_notes


def _action_identity(kind: str, action: Mapping[str, Any]) -> tuple[str, str]:
    if kind == "archive_version":
        return "version", str(action["version_id"])
    if kind == "delete_request_log":
        return "request_log", "request_log_set"
    if kind == "delete_raw_archive":
        return "raw_archive", "raw_archive_set"
    if kind == "retry_pending_raw_files":
        return "raw_file", "pending_file_set"
    if kind == "delete_quality_issue":
        return "quality_issue", "quality_issue_set"
    if kind == "prune_backup":
        return "backup", "backup_set"
    raise DataError("unknown_cleanup_action", f"未知清理动作: {kind}")


def _record_journal(
    conn: sqlite3.Connection,
    preview_id: str,
    action: str,
    target_type: str,
    target_id: str,
    detail: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cleanup_journal(preview_id, action, target_type, target_id,"
        " state, archive_id, detail, updated_at) VALUES (?, ?, ?, ?, 'applied', NULL, ?, ?)",
        (preview_id, action, target_type, target_id, detail, _now_iso()),
    )


def _delete_rows_by_ids(
    conn: sqlite3.Connection, table: str, column: str, ids: Sequence[int]
) -> int:
    deleted = 0
    for start in range(0, len(ids), 400):
        batch = list(ids[start : start + 400])
        if not batch:
            continue
        placeholders = ", ".join("?" for _ in batch)
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})", batch
        )
        deleted += int(cursor.rowcount or 0)
    return deleted


def _delete_raw_archive_rows(
    conn: sqlite3.Connection, items: Sequence[Mapping[str, Any]]
) -> tuple[int, list[str]]:
    # 只删除仍与预览路径一致的索引行；路径在预览后变化时不删除。
    paths: list[str] = []
    count = 0
    for item in items:
        cursor = conn.execute(
            "DELETE FROM raw_archive WHERE digest=? AND path=?",
            (str(item["digest"]), str(item["path"])),
        )
        if cursor.rowcount:
            count += 1
            paths.append(str(item["path"]))
    return count, paths


def _delete_managed_files(settings, stored_paths: Sequence[str]) -> dict[str, Any]:
    """只删除本库原始档案目录内的文件；越界路径与缺失文件都如实记录。"""
    deleted: list[str] = []
    bytes_freed = 0
    failed: list[str] = []
    missing: list[str] = []
    unsafe: list[str] = []
    for stored in stored_paths:
        target = _managed_raw_path(settings, stored)
        if target is None:
            unsafe.append(str(stored))
            continue
        if not target.is_file():
            missing.append(str(stored))
            continue
        try:
            size = target.stat().st_size
            target.unlink()
            deleted.append(str(stored))
            bytes_freed += int(size)
        except OSError as exc:
            failed.append(f"{stored}: {exc}")
    return {
        "deleted": deleted,
        "bytes": bytes_freed,
        "failed": failed,
        "missing": missing,
        "unsafe": unsafe,
    }


def _prune_backups(
    settings,
    side: sqlite3.Connection,
    conn: sqlite3.Connection,
    action: Mapping[str, Any],
    *,
    current_recovery: str | None,
    protect_after: str | None = None,
) -> dict[str, Any]:
    """只删除预览记录过、仍然匹配且不受保护的普通备份包。"""
    root = Path(settings.backup_dir).resolve()
    instance_id, _created = _instance(conn)
    db_path = _normalized_path(settings.db_path)
    current = {
        package["name"]: package
        for package in _scan_backup_packages(settings)
        if package.get("kind") == "candidate"
    }
    ordinary = [
        package
        for package in current.values()
        if package.get("verified")
        and package.get("instance_id") == instance_id
        and (
            not package.get("database_path")
            or _normalized_path(package["database_path"]) == db_path
        )
    ]
    ordinary.sort(
        key=lambda item: (item["created_at"], item["mtime_ns"], item["name"]), reverse=True
    )
    retain = max(1, int(action.get("retain_count") or 3))
    retained_names = {package["name"] for package in ordinary[:retain]}
    protected = _protected_backup_paths(side)
    if current_recovery:
        protected[_normalized_path(current_recovery)] = "current_cleanup_recovery_point"
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[str] = []
    bytes_freed = 0
    for item in action.get("candidates") or []:
        name = str(item.get("name") or "")
        package = current.get(name)
        if package is None:
            skipped.append({"name": name, "reason": "missing"})
            continue
        if package.get("manifest_sha256") != item.get("manifest_sha256"):
            skipped.append({"name": name, "reason": "changed_since_preview"})
            continue
        if name in retained_names:
            skipped.append({"name": name, "reason": "now_within_retention"})
            continue
        if protect_after and str(package.get("created_at") or "") >= protect_after:
            skipped.append({"name": name, "reason": "created_after_preview_recovery_point"})
            continue
        reason = protected.get(_normalized_path(package["path"]))
        if reason:
            skipped.append({"name": name, "reason": f"protected:{reason}"})
            continue
        target = Path(package["path"]).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            skipped.append({"name": name, "reason": "outside_managed_directory"})
            continue
        if not (target / MANIFEST_NAME).is_file():
            skipped.append({"name": name, "reason": "manifest_missing"})
            continue
        try:
            shutil.rmtree(target)
            deleted.append(name)
            bytes_freed += int(item.get("bytes") or 0)
        except OSError as exc:
            failed.append(f"{name}: {exc}")
    kept = len(ordinary) - len(deleted)
    return {
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
        "bytes_freed": bytes_freed,
        "retain_count": retain,
        "kept": kept,
        "over_limit_protected": max(0, kept - retain),
    }


def _archive_and_reclaim(
    conn: sqlite3.Connection,
    settings,
    action: Mapping[str, Any],
    as_of: str,
    side: sqlite3.Connection,
    preview_id: str,
    seq: int,
) -> dict[str, Any]:
    version_id = str(action["version_id"])
    archive = archive_version(settings, conn, version_id, commit=False)
    update_action(
        side,
        preview_id=preview_id,
        seq=seq,
        state="prepared",
        detail=f"archive={archive['archive_id']}",
    )
    # 回收：已完整归档的退休版本释放其全部在线引用；底层仅全局无引用才可删。
    conn.execute(
        "UPDATE data_version SET retention_state='archived', archive_id=? WHERE version_id=?",
        (archive["archive_id"], version_id),
    )
    conn.execute("DELETE FROM data_version_chunk WHERE version_id=?", (version_id,))
    freed = 0
    all_chunks = list(action.get("chunk_ids") or action.get("reclaimable_chunk_ids") or [])
    for chunk_id in all_chunks:
        held = conn.execute(
            "SELECT 1 FROM retention_hold WHERE target_type='chunk' AND target_id=?",
            (chunk_id,),
        ).fetchone()
        if held:
            continue
        still = conn.execute(
            "SELECT 1 FROM data_version_chunk WHERE chunk_id=? LIMIT 1", (chunk_id,)
        ).fetchone()
        if still:
            continue
        freed += int(
            conn.execute(
                "SELECT byte_size FROM data_chunk WHERE chunk_id=?", (chunk_id,)
            ).fetchone()[0]
        )
        conn.execute("DELETE FROM data_chunk_member WHERE chunk_id=?", (chunk_id,))
        conn.execute("DELETE FROM data_chunk WHERE chunk_id=?", (chunk_id,))
    for item in action.get("records") or []:
        ids = list(item.get("record_ids") or [])
        if ids:
            freed += _delete_records(conn, str(item["category"]), ids)
    conn.execute(
        "INSERT OR REPLACE INTO cleanup_journal(preview_id, action, target_type, target_id,"
        " state, archive_id, detail, updated_at) VALUES (?, 'archive_version', 'version', ?,"
        " 'applied', ?, ?, ?)",
        (preview_id, version_id, archive["archive_id"], archive["path"], _now_iso()),
    )
    return {"freed_bytes": freed, "archive_id": archive["archive_id"]}


def _delete_records(conn: sqlite3.Connection, category: str, record_ids: Sequence[int]) -> int:
    table = _BASE_TABLES[category]
    deleted = 0
    for start in range(0, len(record_ids), 400):
        batch = record_ids[start : start + 400]
        placeholders = ", ".join("?" for _ in batch)
        referenced = {
            int(row[0])
            for row in conn.execute(
                f"SELECT m.record_id FROM data_chunk_member m"
                f" JOIN data_chunk c ON c.chunk_id=m.chunk_id"
                f" JOIN data_version_chunk vc ON vc.chunk_id=m.chunk_id"
                f" WHERE c.category=? AND m.record_id IN ({placeholders})",
                [category, *batch],
            ).fetchall()
        }
        legacy_refs = {
            int(row[0])
            for row in conn.execute(
                f"SELECT record_id FROM data_version_member WHERE category=? AND record_id IN"
                f" ({placeholders})",
                [category, *batch],
            ).fetchall()
        }
        deletable = [record_id for record_id in batch if record_id not in referenced | legacy_refs]
        if not deletable:
            continue
        dplaceholders = ", ".join("?" for _ in deletable)
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE id IN ({dplaceholders})", deletable
        )
        deleted += int(cursor.rowcount or 0)
    return deleted


def archive_version(
    settings, conn: sqlite3.Connection, version_id: str, *, commit: bool = True
) -> dict[str, Any]:
    from .records import logical_key as make_logical_key, record_content_hash

    row = conn.execute(
        "SELECT version_id, source_code, scope, categories, quality_summary, manifest_sha256,"
        " rule_version, status, created_at, published_at, manifest_version, window_mode,"
        " window_start FROM data_version WHERE version_id=?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise DataError("version_not_found", f"数据版本 {version_id} 不存在")
    category = "mixed"
    archive_id = "ar_" + hashlib.sha256(f"{version_id}:{uuid.uuid4().hex}".encode()).hexdigest()[:16]
    relative = Path(str(row[1])) / f"{version_id}-{archive_id}.jsonl.gz"
    target = Path(settings.runtime_dir) / "archives" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    digest = hashlib.sha256()
    content_digest = hashlib.sha256()
    content_digest.update(b"[")
    content_first = True
    count = 0
    with gzip.open(temp, "wb", compresslevel=6) as handle:
        for category_name, record_id, content_hash, key in _iter_members(conn, version_id):
            table = _BASE_TABLES.get(category_name)
            if table is None:
                continue
            record = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            if record is None:
                raise DataError(
                    "archive_record_missing",
                    f"归档失败：版本 {version_id} 的 {category_name} 记录 {record_id} 不存在",
                )
            record_dict = dict(record)
            # 归档前逐条重算摘要与逻辑键；不一致说明在线数据与版本清单已脱节。
            recomputed = record_content_hash(category_name, record_dict)
            logical = make_logical_key(category_name, record_dict, str(row[1]))
            if recomputed != str(content_hash) or logical != str(key):
                raise DataError(
                    "archive_integrity_error",
                    f"归档失败：{category_name}/{record_id} 内容摘要或逻辑键与版本清单不一致",
                )
            payload = {
                "category": category_name,
                "logical_key": logical,
                "content_hash": recomputed,
                "record": record_dict,
            }
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
            handle.write(line)
            digest.update(line)
            if not content_first:
                content_digest.update(b",")
            content_first = False
            content_digest.update(canonical_json_bytes([category_name, logical, recomputed]))
            count += 1
        if not content_first:
            content_digest.update(b"]")
        else:
            content_digest.update(b"]")
        handle.flush()
        os.fsync(handle.fileno())
    file_digest = hashlib.sha256()
    with temp.open("rb") as raw_handle:
        for block in iter(lambda: raw_handle.read(1024 * 1024), b""):
            file_digest.update(block)
    # 完整读回校验：解压、逐行摘要、计数与内容摘要。
    verify_digest = hashlib.sha256()
    verify_content = hashlib.sha256()
    verify_content.update(b"[")
    verify_first = True
    verify_count = 0
    with gzip.open(temp, "rb") as handle:
        for line in handle:
            verify_digest.update(line)
            payload = json.loads(line)
            recomputed = record_content_hash(payload["category"], payload["record"])
            logical = make_logical_key(payload["category"], payload["record"], str(row[1]))
            if recomputed != payload["content_hash"] or logical != payload["logical_key"]:
                temp.unlink(missing_ok=True)
                raise DataError("archive_verification_failed", "归档读回时内容摘要不一致")
            if not verify_first:
                verify_content.update(b",")
            verify_first = False
            verify_content.update(
                canonical_json_bytes([payload["category"], logical, recomputed])
            )
            verify_count += 1
        verify_content.update(b"]")
    if (
        verify_digest.hexdigest() != digest.hexdigest()
        or verify_count != count
        or verify_content.hexdigest() != content_digest.hexdigest()
    ):
        temp.unlink(missing_ok=True)
        raise DataError("archive_verification_failed", "归档读回校验失败，拒绝回收在线数据")
    os.replace(temp, target)
    manifest = {
        "format": "archive-v1",
        "version": {column: row[index] for index, column in enumerate(row.keys())},
        "record_count": count,
        "content_digest": content_digest.hexdigest(),
        "uncompressed_sha256": digest.hexdigest(),
        "file_sha256": file_digest.hexdigest(),
        "restore": {
            "method": "restore_version",
            "partition_algorithm": "part-1",
            "members_source": "derive from jsonl line fields",
        },
    }
    manifest_path = target.with_suffix(".manifest.json")
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    statement = (
        "INSERT INTO archive_manifest(archive_id, version_id, source_code, category, path,"
        " sha256, manifest_path, manifest_sha256, content_digest, bytes, member_count,"
        " record_count, restore_meta, verified_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = (
        archive_id,
        version_id,
        str(row[1]),
        category,
        str(relative),
        file_digest.hexdigest(),
        str(manifest_path.relative_to(Path(settings.runtime_dir) / "archives")),
        manifest_sha,
        content_digest.hexdigest(),
        target.stat().st_size,
        count,
        count,
        json.dumps(manifest["restore"], ensure_ascii=False),
        _now_iso(),
        _now_iso(),
    )
    if commit:
        with conn:
            conn.execute(statement, params)
    else:
        conn.execute(statement, params)
    return {
        "archive_id": archive_id,
        "path": str(target),
        "relative_path": str(relative),
        "manifest_path": str(manifest_path),
        "sha256": file_digest.hexdigest(),
        "manifest_sha256": manifest_sha,
        "content_digest": content_digest.hexdigest(),
        "record_count": count,
    }


def restore_version(settings, *, version_id: str, target_db: Path) -> dict[str, Any]:
    """把归档版本恢复到明确的新/空目标库，并逐条与版本摘要核对。"""
    from .chunks import legacy_manifest_digest
    from .records import logical_key as make_logical_key, record_content_hash

    target_db = Path(target_db)
    if not target_db.exists():
        raise DataError("database_missing", f"目标数据库不存在: {target_db}")
    source = sqlite3.connect(
        Path(settings.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0
    )
    try:
        archive = source.execute(
            "SELECT archive_id, path, sha256, manifest_path, manifest_sha256, content_digest,"
            " record_count FROM archive_manifest WHERE version_id=?"
            " ORDER BY created_at DESC LIMIT 1",
            (version_id,),
        ).fetchone()
    finally:
        source.close()
    if archive is None:
        raise DataError("archive_not_found", f"版本 {version_id} 没有归档")
    archive_root = Path(settings.runtime_dir) / "archives"
    path = archive_root / archive[1]
    manifest_path = archive_root / archive[3] if archive[3] else path.with_suffix(".manifest.json")
    if not path.is_file():
        raise DataError("archive_corrupt", "归档文件缺失")
    file_digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            file_digest.update(block)
    if file_digest.hexdigest() != archive[2]:
        raise DataError("archive_corrupt", "归档文件摘要不符")
    if not manifest_path.is_file():
        raise DataError("archive_corrupt", "归档 manifest 缺失")
    manifest_bytes = manifest_path.read_bytes()
    if archive[4] and hashlib.sha256(manifest_bytes).hexdigest() != archive[4]:
        raise DataError("archive_corrupt", "归档 manifest 摘要不符")
    manifest = json.loads(manifest_bytes)
    version = manifest["version"]
    source_code = str(version["source_code"])
    categories = sorted(json.loads(version["categories"]))
    conn = sqlite3.connect(str(target_db), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        # 目标冲突检查：只恢复到明确的新/空目标，绝不覆盖已有记录。
        existing_version = conn.execute(
            "SELECT 1 FROM data_version WHERE version_id=?", (version_id,)
        ).fetchone()
        if existing_version:
            raise DataError("restore_target_exists", f"目标库已存在版本 {version_id}")
        for category in categories:
            table = _BASE_TABLES.get(category)
            if table is None:
                continue
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count:
                raise DataError(
                    "restore_target_not_empty",
                    f"目标库 {table} 非空，拒绝恢复到已有数据（避免 record_id 碰撞）",
                )
        content_digest = hashlib.sha256()
        content_digest.update(b"[")
        first = True
        count = 0
        with conn:
            conn.execute(
                "INSERT INTO data_version(version_id, source_code, scope, categories,"
                " quality_summary, manifest_sha256, rule_version, status, created_at, published_at,"
                " manifest_version, member_storage, member_count, chunk_count, content_digest,"
                " retention_state, window_mode, window_start, archive_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy', ?, 0, ?, 'active', ?, ?, ?)",
                (
                    version["version_id"],
                    source_code,
                    version["scope"],
                    version["categories"],
                    version["quality_summary"],
                    version["manifest_sha256"],
                    version["rule_version"],
                    version["status"],
                    version["created_at"],
                    version["published_at"],
                    version["manifest_version"],
                    manifest["record_count"],
                    manifest.get("content_digest"),
                    version["window_mode"],
                    version["window_start"],
                    archive[0],
                ),
            )
            with gzip.open(path, "rb") as handle:
                for line in handle:
                    payload = json.loads(line)
                    category = str(payload["category"])
                    record = payload["record"]
                    recomputed = record_content_hash(category, record)
                    logical = make_logical_key(category, record, source_code)
                    if recomputed != payload["content_hash"] or logical != payload["logical_key"]:
                        raise DataError(
                            "archive_integrity_error", "归档记录内容摘要或逻辑键不一致"
                        )
                    table = _BASE_TABLES[category]
                    columns = list(record.keys())
                    placeholders = ", ".join("?" for _ in columns)
                    conn.execute(
                        f"INSERT INTO {table}({', '.join(columns)}) VALUES ({placeholders})",
                        [record[column] for column in columns],
                    )
                    conn.execute(
                        "INSERT INTO data_version_member(version_id, category, record_id,"
                        " content_hash, logical_key) VALUES (?, ?, ?, ?, ?)",
                        (
                            version["version_id"],
                            category,
                            int(record["id"]),
                            recomputed,
                            logical,
                        ),
                    )
                    if not first:
                        content_digest.update(b",")
                    first = False
                    content_digest.update(
                        canonical_json_bytes([category, logical, recomputed])
                    )
                    count += 1
            content_digest.update(b"]")
            if count != int(manifest["record_count"]):
                raise DataError("archive_integrity_error", "归档记录数与 manifest 不一致")
            if manifest.get("content_digest") and (
                content_digest.hexdigest() != manifest["content_digest"]
            ):
                raise DataError("archive_integrity_error", "归档内容摘要与 manifest 不一致")
            if int(version["manifest_version"]) == 1:
                if legacy_manifest_digest(conn, version_id) != version["manifest_sha256"]:
                    raise DataError("archive_integrity_error", "恢复后 v1 版本摘要不一致")
        return {
            "status": "restored",
            "version_id": version["version_id"],
            "record_count": count,
            "target_db": str(target_db),
        }
    finally:
        conn.close()


def cleanup_status(settings, *, preview_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    side = open_maintenance(settings)
    try:
        if preview_id:
            preview = get_preview(side, preview_id)
            if preview is None:
                raise DataError("preview_not_found", f"预览 {preview_id} 不存在")
            actions = list_actions(side, preview_id)
            business = sqlite3.connect(
                Path(settings.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0
            )
            try:
                journal = {
                    str(row[0]): str(row[1])
                    for row in business.execute(
                        "SELECT target_id, state FROM cleanup_journal WHERE preview_id=?",
                        (preview_id,),
                    ).fetchall()
                }
            finally:
                business.close()
            for action in actions:
                if action["state"] == "prepared":
                    journal_state = journal.get(str(action["target_id"]))
                    if journal_state == "applied":
                        action["state"] = "applied"
                    elif preview["state"] in {"applied", "rejected", "expired"}:
                        action["state"] = "failed"
            recovery_points = [
                dict(row)
                for row in side.execute(
                    "SELECT preview_id, path, binding, state, created_at"
                    " FROM cleanup_recovery_point ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
            return {
                "preview": preview,
                "actions": actions,
                "recovery_points": recovery_points,
                "pending_files": list_pending_files(
                    side, states=("pending", "failed"), limit=limit
                ),
                "quality_summaries": [
                    row
                    for row in list_quality_summaries(side, limit=max(limit, 20))
                    if row["preview_id"] == preview_id
                ],
                "unfinished_cleanups": unfinished_cleanup_summary(side),
            }
        rows = side.execute(
            "SELECT preview_id, policy_version, source_code, as_of, generation, created_at,"
            " expires_at, state FROM cleanup_preview ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {
            "previews": [dict(row) for row in rows],
            "recovery_points": [
                dict(row)
                for row in side.execute(
                    "SELECT preview_id, path, binding, state, created_at"
                    " FROM cleanup_recovery_point ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ],
            "pending_files": list_pending_files(
                side, states=("pending", "failed"), limit=limit
            ),
            "quality_summaries": list_quality_summaries(side, limit=limit),
            "unfinished_cleanups": unfinished_cleanup_summary(side),
        }
    finally:
        side.close()
