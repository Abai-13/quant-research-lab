"""维护侧库：迁移运行记录、清理预览与动作。

侧库（``runtime/maintenance.db``）只保存可恢复的维护编排状态，不属于业务数据；
预览只写这里，业务库始终以只读方式取状态。迁移的 ``prepared`` 记录在备份前写入，
业务库结构变更前不写任何业务/结构数据。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "maintenance_db_path",
    "open_maintenance",
    "start_run",
    "update_run",
    "get_run",
    "latest_run",
    "record_preview",
    "get_preview",
    "list_previews",
    "update_preview_state",
    "record_action",
    "update_action",
    "list_actions",
    "record_recovery_point",
    "set_recovery_point_state",
    "release_previous_recovery_points",
    "active_recovery_points",
    "record_quality_summary",
    "update_quality_summary_state",
    "list_quality_summaries",
    "record_pending_file",
    "mark_pending_file_state",
    "mark_preview_pending_rolled_back",
    "list_pending_files",
    "unfinished_cleanup_summary",
]

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS maintenance_run (
        run_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        from_version INTEGER,
        to_version INTEGER,
        backup_path TEXT,
        state TEXT NOT NULL,
        detail TEXT,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cleanup_preview (
        preview_id TEXT PRIMARY KEY,
        policy_version TEXT NOT NULL,
        source_code TEXT NOT NULL,
        as_of TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        database_path TEXT NOT NULL,
        generation INTEGER NOT NULL,
        state_digest TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        state TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cleanup_action (
        preview_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        state TEXT NOT NULL,
        archive_id TEXT,
        bytes_freed INTEGER,
        detail TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(preview_id, seq)
    )
    """,
    # 有限保留所需的显式侧库升级：当前清理恢复点与质量问题汇总。
    """
    CREATE TABLE IF NOT EXISTS cleanup_recovery_point (
        preview_id TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        manifest_sha256 TEXT,
        instance_id TEXT,
        binding TEXT,
        content_fingerprint TEXT,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # 外部文件删除的持久待办：只有这里登记过的路径才允许在中断后重试删除。
    """
    CREATE TABLE IF NOT EXISTS cleanup_pending_file (
        file_id TEXT PRIMARY KEY,
        preview_id TEXT NOT NULL,
        path TEXT NOT NULL,
        expected_bytes INTEGER,
        state TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quality_issue_summary (
        summary_id TEXT PRIMARY KEY,
        preview_id TEXT NOT NULL,
        source_code TEXT NOT NULL,
        cutoff TEXT NOT NULL,
        issue_count INTEGER NOT NULL,
        summary_json TEXT NOT NULL,
        state TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL
    )
    """,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def maintenance_db_path(settings) -> Path:
    return Path(settings.runtime_dir) / "maintenance.db"


def open_maintenance(settings) -> sqlite3.Connection:
    path = maintenance_db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    for statement in _SCHEMA:
        conn.execute(statement)
    conn.commit()
    return conn


def start_run(
    conn: sqlite3.Connection,
    *,
    kind: str,
    from_version: int | None,
    to_version: int | None,
    backup_path: str | None = None,
    detail: str | None = None,
) -> str:
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO maintenance_run(run_id, kind, from_version, to_version, backup_path,"
            " state, detail, started_at, updated_at) VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?, ?)",
            (run_id, kind, from_version, to_version, backup_path, detail, now, now),
        )
    return run_id


def update_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    state: str,
    backup_path: str | None = None,
    detail: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE maintenance_run SET state=?, backup_path=COALESCE(?, backup_path),"
            " detail=COALESCE(?, detail), updated_at=? WHERE run_id=?",
            (state, backup_path, detail, _now(), run_id),
        )


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM maintenance_run WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row is not None else None


def latest_run(conn: sqlite3.Connection, kind: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM maintenance_run WHERE kind=? ORDER BY started_at DESC LIMIT 1", (kind,)
    ).fetchone()
    return dict(row) if row is not None else None


def record_preview(
    conn: sqlite3.Connection,
    *,
    preview_id: str,
    policy_version: str,
    source_code: str,
    as_of: str,
    instance_id: str,
    database_path: str,
    generation: int,
    state_digest: str,
    plan: dict[str, Any],
    summary: dict[str, Any],
    created_at: str,
    expires_at: str,
) -> None:
    import json

    with conn:
        conn.execute(
            "INSERT INTO cleanup_preview(preview_id, policy_version, source_code, as_of,"
            " instance_id, database_path, generation, state_digest, plan_json, summary_json,"
            " created_at, expires_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (
                preview_id,
                policy_version,
                source_code,
                as_of,
                instance_id,
                database_path,
                generation,
                state_digest,
                json.dumps(plan, ensure_ascii=False, sort_keys=True),
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                created_at,
                expires_at,
            ),
        )


def get_preview(conn: sqlite3.Connection, preview_id: str) -> dict[str, Any] | None:
    import json

    row = conn.execute("SELECT * FROM cleanup_preview WHERE preview_id=?", (preview_id,)).fetchone()
    if row is None:
        return None
    payload = dict(row)
    payload["plan"] = json.loads(payload.pop("plan_json"))
    payload["summary"] = json.loads(payload.pop("summary_json"))
    return payload


def list_previews(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT preview_id, policy_version, source_code, as_of, generation, created_at,"
        " expires_at, state FROM cleanup_preview ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def update_preview_state(conn: sqlite3.Connection, preview_id: str, state: str) -> None:
    with conn:
        conn.execute("UPDATE cleanup_preview SET state=? WHERE preview_id=?", (state, preview_id))


def record_action(
    conn: sqlite3.Connection,
    *,
    preview_id: str,
    seq: int,
    action: str,
    target_type: str,
    target_id: str,
    state: str,
    archive_id: str | None = None,
    bytes_freed: int | None = None,
    detail: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO cleanup_action(preview_id, seq, action, target_type, target_id, state,"
            " archive_id, bytes_freed, detail, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                preview_id,
                seq,
                action,
                target_type,
                target_id,
                state,
                archive_id,
                bytes_freed,
                detail,
                _now(),
            ),
        )


def update_action(
    conn: sqlite3.Connection,
    *,
    preview_id: str,
    seq: int,
    state: str,
    bytes_freed: int | None = None,
    detail: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE cleanup_action SET state=?, bytes_freed=COALESCE(?, bytes_freed),"
            " detail=COALESCE(?, detail), updated_at=? WHERE preview_id=? AND seq=?",
            (state, bytes_freed, detail, _now(), preview_id, seq),
        )


def list_actions(conn: sqlite3.Connection, preview_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM cleanup_action WHERE preview_id=? ORDER BY seq", (preview_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def record_recovery_point(
    conn: sqlite3.Connection,
    *,
    preview_id: str,
    path: str,
    manifest_sha256: str | None,
    instance_id: str | None,
    binding: str | None,
    content_fingerprint: str | None,
    state: str = "recorded",
) -> None:
    """在删除/提交前精确登记操作前恢复点（侧库）。

    ``recorded`` 表示“已验证但在业务事务提交前”；提交成功后改为 active。
    中断恢复只能使用这条精确记录，不能按时间或 generation 扫描猜测。
    """
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO cleanup_recovery_point(preview_id, path, manifest_sha256,"
            " instance_id, binding, content_fingerprint, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                preview_id,
                path,
                manifest_sha256,
                instance_id,
                binding,
                content_fingerprint,
                state,
                _now(),
            ),
        )


def set_recovery_point_state(conn: sqlite3.Connection, preview_id: str, state: str) -> None:
    with conn:
        conn.execute(
            "UPDATE cleanup_recovery_point SET state=? WHERE preview_id=?",
            (state, preview_id),
        )


def release_previous_recovery_points(conn: sqlite3.Connection, *, keep_preview_id: str) -> None:
    """新清理成功后，旧清理恢复点退为 released；新恢复点已包含其前状态。

    仍有 pending/failed 待删文件的旧清理不能释放：它的恢复点是该任务完成前
    最后可恢复副本，必须保留到该任务自己的收尾通过。
    """
    with conn:
        conn.execute(
            "UPDATE cleanup_recovery_point SET state='released'"
            " WHERE state='active' AND preview_id<>?"
            " AND preview_id NOT IN ("
            "   SELECT preview_id FROM cleanup_pending_file"
            "   WHERE state IN ('pending','failed'))",
            (keep_preview_id,),
        )


def active_recovery_points(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT preview_id, path, manifest_sha256, instance_id, binding,"
        " content_fingerprint, created_at FROM cleanup_recovery_point WHERE state='active'"
        " ORDER BY created_at"
    ).fetchall()
    return [dict(row) for row in rows]


def record_quality_summary(
    conn: sqlite3.Connection,
    *,
    summary_id: str,
    preview_id: str,
    source_code: str,
    cutoff: str,
    issue_count: int,
    summary: dict[str, Any],
) -> None:
    import json

    with conn:
        conn.execute(
            "INSERT INTO quality_issue_summary(summary_id, preview_id, source_code, cutoff,"
            " issue_count, summary_json, state, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?)",
            (
                summary_id,
                preview_id,
                source_code,
                cutoff,
                issue_count,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )


def update_quality_summary_state(
    conn: sqlite3.Connection,
    summary_id: str,
    *,
    state: str,
    detail: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE quality_issue_summary SET state=?, detail=COALESCE(?, detail)"
            " WHERE summary_id=?",
            (state, detail, summary_id),
        )


def list_quality_summaries(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT summary_id, preview_id, source_code, cutoff, issue_count, state, detail,"
        " created_at FROM quality_issue_summary ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def record_pending_file(
    conn: sqlite3.Connection,
    *,
    file_id: str,
    preview_id: str,
    path: str,
    expected_bytes: int | None,
) -> None:
    now = _now()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO cleanup_pending_file(file_id, preview_id, path,"
            " expected_bytes, state, detail, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)",
            (file_id, preview_id, path, expected_bytes, now, now),
        )


def mark_pending_file_state(
    conn: sqlite3.Connection,
    file_ids: list[str],
    *,
    state: str,
    detail: str | None = None,
) -> None:
    if not file_ids:
        return
    now = _now()
    with conn:
        for file_id in file_ids:
            conn.execute(
                "UPDATE cleanup_pending_file SET state=?, detail=COALESCE(?, detail),"
                " updated_at=? WHERE file_id=?",
                (state, detail, now, file_id),
            )


def mark_preview_pending_rolled_back(conn: sqlite3.Connection, preview_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE cleanup_pending_file SET state='rolled_back', updated_at=?"
            " WHERE preview_id=? AND state='pending'",
            (_now(), preview_id),
        )


def list_pending_files(
    conn: sqlite3.Connection,
    *,
    states: tuple[str, ...] = ("pending", "failed"),
    limit: int = 5000,
    preview_id: str | None = None,
) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in states)
    where = f"state IN ({placeholders})"
    params: list[Any] = [*states]
    if preview_id is not None:
        where += " AND preview_id=?"
        params.append(preview_id)
    rows = conn.execute(
        "SELECT file_id, preview_id, path, expected_bytes, state, detail, created_at"
        f" FROM cleanup_pending_file WHERE {where}"
        " ORDER BY created_at, file_id LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [dict(row) for row in rows]


def unfinished_cleanup_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """未完成清理：仍待删的文件或提交前登记但尚未完成收尾的恢复点。

    新清理不得接管这些任务；必须先让原预览完成中断收尾，或人工恢复其
    精确登记的恢复点后重试。
    """
    pending = [
        {
            "preview_id": str(row[0]),
            "pending_files": int(row[1]),
            "last_updated": row[2],
        }
        for row in conn.execute(
            "SELECT preview_id, COUNT(*), MAX(updated_at) FROM cleanup_pending_file"
            " WHERE state IN ('pending','failed') GROUP BY preview_id ORDER BY preview_id"
        ).fetchall()
    ]
    recorded = [
        {
            "preview_id": str(row[0]),
            "path": str(row[1]),
            "created_at": row[2],
        }
        for row in conn.execute(
            "SELECT preview_id, path, created_at FROM cleanup_recovery_point"
            " WHERE state='recorded' ORDER BY created_at, preview_id"
        ).fetchall()
    ]
    preview_ids = sorted(
        {item["preview_id"] for item in pending}
        | {item["preview_id"] for item in recorded}
    )
    return {
        "unfinished": bool(preview_ids),
        "preview_ids": preview_ids,
        "pending": pending,
        "recorded_recovery_points": recorded,
    }
