"""Shared operation lifecycle DTOs and checkpoint helpers owned by C.

Domain stores keep their own transactions; this module defines the stable shape
and a small adapter contract rather than a cross-domain scheduler.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping


OPERATION_KINDS = frozenset({"data_prepare", "research_experiment", "agent_task", "startup_update", "maintenance"})
OPERATION_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled", "interrupted"})
STEP_STATES = frozenset({"pending", "committed", "unknown"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checkpoint_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def checkpoint_step(step_id: str, state: str, *, result: Any = None, result_digest: str | None = None) -> dict[str, Any]:
    if not step_id or len(step_id) > 160 or state not in STEP_STATES:
        raise ValueError("checkpoint_invalid")
    item: dict[str, Any] = {"step_id": step_id, "state": state}
    if state == "committed":
        if result_digest is None:
            result_digest = checkpoint_digest({"result": result})
        item["result_digest"] = result_digest
    if state == "unknown":
        item["reason"] = "external_result_unknown"
    return item


@dataclass(frozen=True)
class OperationView:
    operation_id: str
    kind: str
    status: str
    attempt: int
    generation: int
    progress: dict[str, Any]
    checkpoint: dict[str, Any]
    cumulative_usage: dict[str, Any]
    limits: dict[str, Any]
    resumable: bool
    stop_reason: str | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def operation_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a task-like domain row to the public operation contract."""
    usage = row.get("usage") or {}
    checkpoint = row.get("checkpoint") or {"sequence": 0, "steps": [], "validation": "not_checked"}
    status = str(row.get("status") or "failed")
    return OperationView(
        operation_id=str(row.get("task_id") or row.get("operation_id") or ""),
        kind=str(row.get("kind") or "agent_task"), status=status,
        attempt=max(1, int(row.get("attempt") or 1)), generation=max(1, int(row.get("generation") or 1)),
        progress=row.get("progress") or {"phase": status, "completed_units": 0, "total_units": None},
        checkpoint={k: checkpoint.get(k) for k in ("sequence", "steps", "validation")},
        cumulative_usage=usage, limits=row.get("limits") or {},
        resumable=bool(row.get("resumable", False)), stop_reason=row.get("stop_reason"),
        created_at=str(row.get("created_at") or ""), updated_at=str(row.get("updated_at") or ""),
    ).as_dict()
