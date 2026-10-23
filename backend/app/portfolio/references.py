"""阶段三结果对数据资产的引用保护（S1）。

阶段三 §9 决定：

- 建议、模拟与必要证据引用数据版本/股票池快照；复用既有 hold/引用保护；
- "保存结果与所需引用保护具有一致事务边界：不能先公开结果再异步加保护"。
  本模块提供的 hold 登记在同一调用方事务内执行并推进 ``maintenance_generation``，
  使既有清理预览立即失效；
- hold 目标必须存在：不产生悬空 hold，也不静默忽略未知目标。

阶段三只使用既有 ``version`` / ``snapshot`` 两类 hold 目标；新增 hold 语义必须
留在阶段二 ``retention._protection`` 的既有分类中，不发明新类型。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..data.digests import sha256_of_object
from ..data.errors import DataError
from ..data.timeutil import utc_now_iso

__all__ = [
    "ReferenceTarget",
    "reference_hold",
    "register_reference_holds",
    "release_reference_holds",
    "list_reference_holds",
    "analysis_input_sha256",
    "reference_hold_id",
]

_HOLD_TYPES = ("version", "snapshot")


@dataclass(frozen=True)
class ReferenceTarget:
    """一个需要保住的阶段三引用（数据版本或股票池快照）。"""

    target_type: str
    target_id: str
    reason: str

    def __post_init__(self) -> None:
        if self.target_type not in _HOLD_TYPES:
            raise DataError(
                "unsupported_hold_type",
                f"阶段三引用目标类型只能是 {'/'.join(_HOLD_TYPES)}: {self.target_type}",
            )


def _hold_target_exists(conn: sqlite3.Connection, target: ReferenceTarget) -> None:
    if target.target_type == "version":
        row = conn.execute(
            "SELECT 1 FROM data_version WHERE version_id=?", (target.target_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM pool_snapshot WHERE snapshot_id=?", (target.target_id,)
        ).fetchone()
    label = "数据版本" if target.target_type == "version" else "股票池快照"
    if row is None:
        raise DataError(
            "hold_target_not_found",
            f"hold 引用的{label}不存在: {target.target_id}",
        )


def reference_hold_id(target: ReferenceTarget) -> str:
    """确定性 hold_id：同 (类型, 目标, 原因) 重复登记得到同一行（幂等）。"""
    return "hold_" + sha256_of_object(
        ["phase3-reference", target.target_type, target.target_id, target.reason]
    )[:16]


def _bump_generation(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE maintenance_generation SET generation=generation+1, updated_at=? WHERE id=1",
        (utc_now_iso(),),
    )


def reference_hold(conn: sqlite3.Connection, target: ReferenceTarget) -> tuple[str, bool]:
    """在同一事务内登记一条引用保护 hold 并推进 generation；不提交。

    同 (target_type, target_id, reason) 重复登记返回既有 hold_id（幂等，
    不重复推进 generation 时只跳过重复行）。
    """
    _hold_target_exists(conn, target)
    hold_id = reference_hold_id(target)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO retention_hold(hold_id, target_type, target_id, reason,"
        " created_at, expires_at) VALUES (?, ?, ?, ?, ?, NULL)",
        (hold_id, target.target_type, target.target_id, target.reason, utc_now_iso()),
    )
    created = bool(cursor.rowcount)
    if created:
        _bump_generation(conn)
    return hold_id, created


def register_reference_holds(
    conn: sqlite3.Connection, targets: Sequence[ReferenceTarget], *, reason_prefix: str
) -> list[dict[str, Any]]:
    """在调用方事务内登记全部引用保护（不提交）。

    ``reason_prefix`` 说明保护归属于哪条结果（如 ``analysis_run:an_xxx``），
    与具体目标一起组成稳定的幂等键；任何目标不存在都直接拒绝且不写入任何行。
    """
    holds: list[dict[str, Any]] = []
    for target in targets:
        resolved = ReferenceTarget(
            target_type=target.target_type,
            target_id=target.target_id,
            reason=f"{reason_prefix}:{target.reason}",
        )
        hold_id, created = reference_hold(conn, resolved)
        holds.append(
            {
                "hold_id": hold_id,
                "created": created,
                "target_type": resolved.target_type,
                "target_id": resolved.target_id,
                "reason": resolved.reason,
            }
        )
    return holds


def release_reference_holds(conn: sqlite3.Connection, hold_ids: Sequence[str]) -> list[str]:
    """按 hold_id 解除引用保护（同事务、不提交）；返回实际解除的 hold_id。"""
    released: list[str] = []
    for hold_id in hold_ids:
        cursor = conn.execute(
            "DELETE FROM retention_hold WHERE hold_id=? AND hold_id LIKE 'hold_%'", (hold_id,)
        )
        if cursor.rowcount:
            _bump_generation(conn)
            released.append(hold_id)
    return released


def list_reference_holds(conn: sqlite3.Connection, *, reason_prefix: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT hold_id, target_type, target_id, reason FROM retention_hold"
        " WHERE reason LIKE ? ORDER BY hold_id",
        (f"{reason_prefix}%",),
    ).fetchall()
    return [
        {"hold_id": row[0], "target_type": row[1], "target_id": row[2], "reason": row[3]}
        for row in rows
    ]


def analysis_input_sha256(payload: Mapping[str, Any]) -> str:
    """规范化输入摘要：全量输入（含可缺席股票池快照的显式 None）确定可重复。

    幂等键必须来自调用方提供或本摘要；绝不含时间戳、随机数或进程状态。
    """
    return sha256_of_object(dict(payload))
