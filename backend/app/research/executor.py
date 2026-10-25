"""Single-process, bounded executor for deterministic research experiments."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

try:
    from app.agent_runtime.operation import operation_view
except ModuleNotFoundError as exc:  # C's contract module is merged by the integration branch.
    if exc.name != "app.agent_runtime.operation":
        raise
    def operation_view(row):
        return {"operation_id": str(row.get("task_id") or ""), "kind": str(row.get("kind") or "research_experiment"),
                "status": str(row.get("status") or "failed"), "attempt": int(row.get("attempt") or 1),
                "generation": int(row.get("generation") or 1), "progress": row.get("progress") or {},
                "checkpoint": row.get("checkpoint") or {"sequence": 0, "steps": [], "validation": "not_checked"},
                "cumulative_usage": row.get("usage") or {}, "limits": row.get("limits") or {},
                "resumable": bool(row.get("resumable")), "stop_reason": row.get("stop_reason"),
                "created_at": str(row.get("created_at") or ""), "updated_at": str(row.get("updated_at") or "")}
from app.config import Settings
from .service import ResearchError, now
from .store import encode, research_connect

_lock = threading.RLock()
_executor: ThreadPoolExecutor | None = None
_settings: Settings | None = None
_active: set[str] = set()
_stopping = False


def _view(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value.update({"task_id": value["operation_id"], "kind": "research_experiment",
                  "progress": json.loads(value.pop("progress_json")),
                  "checkpoint": json.loads(value.pop("checkpoint_json")),
                  "usage": json.loads(value.pop("usage_json")),
                  "limits": json.loads(value.pop("limits_json")),
                  "resumable": bool(value["resumable"])})
    value.pop("request_sha256", None)
    return operation_view(value)


def _read(conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM research_operation WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise ResearchError("operation_not_found", "研究 Operation 不存在", 404)
    return row


def startup_research_executor(settings: Settings | None = None) -> None:
    """Initialize the single worker and conservatively mark crash leftovers."""
    global _executor, _settings, _stopping
    if settings is None:
        from app.config import load_settings
        settings = load_settings()
    with _lock:
        if _executor is not None:
            return
        _settings, _stopping = settings, False
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research-experiment")
    try:
        conn = research_connect(settings)
    except Exception:
        shutdown_research_executor(wait=False)
        raise
    try:
        rows = conn.execute("SELECT operation_id,generation,progress_json,checkpoint_json,usage_json,limits_json,attempt FROM research_operation WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            progress, checkpoint = json.loads(row["progress_json"]), json.loads(row["checkpoint_json"])
            # Only candidate-level committed checkpoints are deterministic and safe.
            steps = checkpoint.get("steps", [])
            safe = (checkpoint.get("validation") == "not_checked" and not steps) or (
                checkpoint.get("validation") == "checkpointed" and all(step.get("state") == "committed" for step in steps))
            status = "interrupted"
            reason = "process_restart"
            with conn:
                conn.execute("UPDATE research_operation SET status=?,resumable=?,stop_reason=?,updated_at=? WHERE operation_id=? AND generation=?",
                             (status, int(safe), reason, now(), row["operation_id"], row["generation"]))
                conn.execute("INSERT OR IGNORE INTO research_operation_attempt(operation_id,attempt,generation,status,checkpoint_json,created_at) VALUES(?,?,?,?,?,?)",
                             (row["operation_id"], row["attempt"], row["generation"], status, row["checkpoint_json"], now()))
                conn.execute("UPDATE research_operation_attempt SET status=?,checkpoint_json=? WHERE operation_id=? AND attempt=?",
                             (status, row["checkpoint_json"], row["operation_id"], row["attempt"]))
    finally:
        conn.close()


def shutdown_research_executor(*, wait: bool = True) -> None:
    global _executor, _settings, _stopping
    with _lock:
        executor, _executor, _settings = _executor, None, None
        _stopping = True
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=False)


def create_operation(settings: Settings, plan_id: str, body: dict[str, Any]) -> dict[str, Any]:
    startup_research_executor(settings)
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not key.strip() or len(key.strip()) > 120:
        raise ResearchError("idempotency_key_required", "POST 必须提供 idempotency_key", 400)
    request_hash = hashlib.sha256(encode(body).encode()).hexdigest()
    conn = research_connect(settings)
    try:
        prior = conn.execute("SELECT operation_id,request_sha256 FROM research_operation WHERE idempotency_key=?", (key,)).fetchone()
        if prior:
            if prior["request_sha256"] != request_hash:
                raise ResearchError("idempotency_conflict", "idempotency_key 已用于不同请求", 409)
            return _view(_read(conn, prior["operation_id"]))
        plan = conn.execute("SELECT status,plan_json FROM research_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None or plan["status"] != "frozen":
            raise ResearchError("plan_unavailable", "只能启动已冻结的研究方案", 409)
        limits = json.loads(plan["plan_json"])["budget"]
        operation_id, timestamp = "exp_" + uuid.uuid4().hex, now()
        progress = {"phase": "queued", "completed_units": 0, "total_units": limits["estimated_logical_steps"]}
        checkpoint = {"sequence": 0, "steps": [], "validation": "not_checked"}
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            raced = conn.execute("SELECT operation_id,request_sha256 FROM research_operation WHERE idempotency_key=?", (key,)).fetchone()
            if raced:
                if raced["request_sha256"] != request_hash:
                    raise ResearchError("idempotency_conflict", "idempotency_key 已用于不同请求", 409)
                return _view(_read(conn, raced["operation_id"]))
            active = conn.execute("SELECT 1 FROM research_operation WHERE status IN ('queued','running') LIMIT 1").fetchone()
            if active:
                raise ResearchError("active_experiment_exists", "同一实例同时只允许一个活动实验", 409)
            conn.execute("INSERT INTO research_operation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (operation_id, operation_id, plan_id, "queued", 1, 1, encode(progress), encode(checkpoint), encode({"wall_seconds": 0, "logical_steps": 0}), encode(limits), 0, None, key, request_hash, timestamp, timestamp))
            conn.execute("INSERT INTO research_operation_attempt(operation_id,attempt,generation,status,checkpoint_json,created_at) VALUES(?,?,?,?,?,?)",
                         (operation_id, 1, 1, "queued", encode(checkpoint), timestamp))
        result = _view(_read(conn, operation_id))
        with _lock:
            executor = _executor
            if executor is None or _stopping:
                with conn:
                    conn.execute("UPDATE research_operation SET status='interrupted',resumable=0,stop_reason='executor_unavailable',updated_at=? WHERE operation_id=?", (now(), operation_id))
                    conn.execute("UPDATE research_operation_attempt SET status='interrupted' WHERE operation_id=? AND attempt=1", (operation_id,))
                raise ResearchError("executor_unavailable", "研究 executor 不可用", 503)
            _active.add(operation_id)
            executor.submit(_run, settings, operation_id, 1, 1)
        return result
    finally:
        conn.close()


def get_operation(settings: Settings, operation_id: str) -> dict[str, Any]:
    conn = research_connect(settings)
    try:
        result = _view(_read(conn, operation_id))
        attempts = conn.execute("SELECT attempt,generation,status,checkpoint_json,created_at FROM research_operation_attempt WHERE operation_id=? ORDER BY attempt", (operation_id,)).fetchall()
        result["attempts"] = [{"attempt": int(row[0]), "generation": int(row[1]), "status": str(row[2]),
                               "checkpoint": json.loads(row[3]), "created_at": str(row[4])} for row in attempts]
        experiment = conn.execute("SELECT status,status_json FROM research_experiment WHERE experiment_id=?", (operation_id,)).fetchone()
        if experiment:
            status_json = json.loads(experiment["status_json"])
            summary = status_json.get("final_summary")
            if isinstance(summary, dict):
                # Failed runs expose completed training evidence only. Their
                # evaluation member is always null by the runner contract.
                result["result"] = summary
            result["source_context_digest"] = status_json.get("source_context_digest") or (
                summary.get("source_context_digest") if isinstance(summary, dict) else None
            )
        return result
    finally:
        conn.close()


def cancel_operation(settings: Settings, operation_id: str, expected_generation: int) -> dict[str, Any]:
    conn = research_connect(settings)
    try:
        row = _read(conn, operation_id)
        if row["status"] == "cancelled" and int(row["generation"]) == expected_generation + 1:
            return _view(row)
        if int(row["generation"]) != expected_generation:
            raise ResearchError("generation_conflict", "Operation generation 已变化", 409)
        if row["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            return _view(row)
        checkpoint = json.loads(row["checkpoint_json"])
        safe = checkpoint.get("validation") in {"not_checked", "checkpointed"} and all(s.get("state") == "committed" for s in checkpoint.get("steps", []))
        with conn:
            changed = conn.execute("UPDATE research_operation SET status='cancelled',generation=generation+1,resumable=?,stop_reason='cancelled',updated_at=? WHERE operation_id=? AND generation=? AND status IN ('queued','running')",
                                   (int(safe), now(), operation_id, expected_generation)).rowcount
            if changed != 1:
                raise ResearchError("generation_conflict", "Operation 状态已变化", 409)
            conn.execute("UPDATE research_experiment SET generation=generation+1,status='cancelled',updated_at=? WHERE experiment_id=? AND generation=?", (now(), operation_id, expected_generation))
            conn.execute("INSERT INTO research_operation_attempt(operation_id,attempt,generation,status,checkpoint_json,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(operation_id,attempt) DO UPDATE SET status=excluded.status,checkpoint_json=excluded.checkpoint_json",
                         (operation_id, row["attempt"], expected_generation + 1, "cancelled", row["checkpoint_json"], now()))
        return _view(_read(conn, operation_id))
    finally:
        conn.close()


def resume_operation(settings: Settings, operation_id: str, expected_generation: int, idempotency_key: str) -> dict[str, Any]:
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key.strip()) > 120:
        raise ResearchError("idempotency_key_required", "恢复必须提供 idempotency_key", 400)
    startup_research_executor(settings)
    conn = research_connect(settings)
    try:
        req_hash = hashlib.sha256(f"{operation_id}|{expected_generation}|{idempotency_key}".encode()).hexdigest()
        replay = conn.execute("SELECT operation_id,request_sha256 FROM research_operation_resume_key WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if replay:
            if replay["operation_id"] != operation_id or replay["request_sha256"] != req_hash:
                raise ResearchError("idempotency_conflict", "idempotency_key 已用于不同恢复请求", 409)
            return _view(_read(conn, operation_id))
        row = _read(conn, operation_id)
        if int(row["generation"]) != expected_generation:
            raise ResearchError("generation_conflict", "Operation generation 已变化", 409)
        if row["status"] not in {"cancelled", "interrupted", "failed"} or not row["resumable"]:
            raise ResearchError("operation_not_resumable", "Operation 尚无安全恢复检查点", 409)
        used = json.loads(row["usage_json"])
        limits = json.loads(row["limits_json"])
        if int(used.get("wall_seconds", 0)) >= int(limits["wall_seconds"]):
            raise ResearchError("budget_exhausted", "累计运行时间预算已用尽，不能恢复", 409)
        if int(used.get("logical_steps", 0)) >= int(limits.get("max_logical_steps", 1260)):
            raise ResearchError("budget_exhausted", "累计逻辑步数预算已用尽，不能恢复", 409)
        with _lock:
            if _executor is None or _stopping or _active:
                raise ResearchError("executor_unavailable", "研究 executor 忙碌或不可用", 503)
            with conn:
                conn.execute("INSERT INTO research_operation_resume_key VALUES(?,?,?,?,?)", (operation_id, idempotency_key, req_hash, expected_generation, now()))
                changed = conn.execute("UPDATE research_operation SET status='queued',attempt=attempt+1,generation=generation+1,stop_reason=NULL,updated_at=? WHERE operation_id=? AND generation=? AND status IN ('cancelled','interrupted','failed')",
                             (now(), operation_id, expected_generation)).rowcount
                if changed != 1:
                    raise ResearchError("generation_conflict", "Operation 状态已变化", 409)
                conn.execute("UPDATE research_experiment SET status='queued',generation=generation+1,updated_at=? WHERE experiment_id=? AND generation=?", (now(), operation_id, expected_generation))
                current = _read(conn, operation_id)
                conn.execute("INSERT INTO research_operation_attempt(operation_id,attempt,generation,status,checkpoint_json,created_at) VALUES(?,?,?,?,?,?)",
                             (operation_id, current["attempt"], current["generation"], "queued", current["checkpoint_json"], now()))
            _active.add(operation_id)
            _executor.submit(_run, settings, operation_id, int(current["attempt"]), int(current["generation"]))
        return _view(current)
    finally:
        conn.close()


def _run(settings: Settings, operation_id: str, attempt: int, generation: int) -> None:
    started = time.monotonic()
    charged_wall = 0
    def elapsed_delta() -> int:
        nonlocal charged_wall
        total = math.ceil(time.monotonic() - started)
        delta = max(0, total - charged_wall)
        charged_wall = total
        return delta
    conn = None
    try:
        conn = research_connect(settings)
        row = _read(conn, operation_id)
        limits = json.loads(row["limits_json"])
        usage = json.loads(row["usage_json"])
        remaining = max(1, int(limits["wall_seconds"]) - int(usage.get("wall_seconds", 0)))
        remaining_steps = max(0, int(limits.get("max_logical_steps", 1260)) - int(usage.get("logical_steps", 0)))
        if remaining_steps == 0:
            with conn:
                conn.execute("UPDATE research_operation SET status='failed',resumable=0,stop_reason='logical_budget_exceeded',updated_at=? WHERE operation_id=? AND generation=? AND status='queued'", (now(), operation_id, generation))
                conn.execute("UPDATE research_operation_attempt SET status='failed' WHERE operation_id=? AND attempt=?", (operation_id, attempt))
            return
        with conn:
            conn.execute("UPDATE research_operation SET status='running',progress_json=?,updated_at=? WHERE operation_id=? AND generation=? AND status='queued'",
                         (encode({**json.loads(row["progress_json"]), "phase": "preflight"}), now(), operation_id, generation))
            conn.execute("UPDATE research_experiment SET generation=?,status='running',updated_at=? WHERE experiment_id=? AND generation=?", (generation, now(), operation_id, generation))
        def progress(value, checkpoint, logical_steps):
            current = research_connect(settings)
            try:
                with current:
                    fresh = _read(current, operation_id)
                    if int(fresh["generation"]) != generation or fresh["status"] != "running":
                        raise ResearchError("generation_fenced", "迟到的实验写入被 generation fence 拒绝", 409)
                    used = json.loads(fresh["usage_json"])
                    used["wall_seconds"] = int(used.get("wall_seconds", 0)) + elapsed_delta()
                    updated_steps = int(used.get("logical_steps", 0)) + int(logical_steps)
                    if updated_steps > int(json.loads(fresh["limits_json"]).get("max_logical_steps", 1260)):
                        raise ResearchError("logical_budget_exceeded", "累计逻辑步数预算已用尽", 409)
                    used["logical_steps"] = updated_steps
                    changed = current.execute("UPDATE research_operation SET progress_json=?,checkpoint_json=?,usage_json=?,resumable=1,updated_at=? WHERE operation_id=? AND generation=? AND status='running'",
                                    (encode(value), encode(checkpoint), encode(used), now(), operation_id, generation)).rowcount
                    if changed != 1:
                        raise ResearchError("generation_fenced", "迟到的实验写入被 generation fence 拒绝", 409)
                    current.execute("UPDATE research_operation_attempt SET status='running',checkpoint_json=? WHERE operation_id=? AND attempt=?",
                                    (encode(checkpoint), operation_id, attempt))
            finally:
                current.close()
        from .runner import run_experiment
        result = run_experiment(settings, row["plan_id"], experiment_id=operation_id, generation=generation,
                                progress_callback=progress, remaining_wall_seconds=remaining,
                                remaining_logical_steps=remaining_steps)
        fresh = research_connect(settings)
        try:
            with fresh:
                row = _read(fresh, operation_id)
                if int(row["generation"]) != generation or row["status"] != "running":
                    return
                used = json.loads(row["usage_json"])
                used["wall_seconds"] = int(used.get("wall_seconds", 0)) + elapsed_delta()
                progress = {"phase": "completed", "completed_units": int(json.loads(row["limits_json"])["estimated_logical_steps"]), "total_units": int(json.loads(row["limits_json"])["estimated_logical_steps"])}
                checkpoint = {"sequence": int(json.loads(row["checkpoint_json"]).get("sequence", 0)) + 1, "steps": json.loads(row["checkpoint_json"]).get("steps", []) + [{"step_id": "evaluation", "state": "committed"}], "validation": "complete"}
                changed = fresh.execute("UPDATE research_operation SET status='completed',progress_json=?,checkpoint_json=?,usage_json=?,resumable=0,stop_reason=NULL,updated_at=? WHERE operation_id=? AND generation=? AND status='running'",
                              (encode(progress), encode(checkpoint), encode(used), now(), operation_id, generation))
                if changed.rowcount != 1:
                    raise ResearchError("generation_fenced", "迟到的完成写入被 generation fence 拒绝", 409)
                fresh.execute("UPDATE research_operation_attempt SET status='completed',checkpoint_json=? WHERE operation_id=? AND attempt=?", (encode(checkpoint), operation_id, attempt))
        finally:
            fresh.close()
    except Exception as exc:
        try:
            current = research_connect(settings)
            with current:
                row = _read(current, operation_id)
                if int(row["generation"]) == generation and row["status"] == "running":
                    used = json.loads(row["usage_json"])
                    used["wall_seconds"] = int(used.get("wall_seconds", 0)) + elapsed_delta()
                    resumable = bool(row["resumable"])
                    status = "interrupted" if resumable else "failed"
                    changed = current.execute("UPDATE research_operation SET status=?,usage_json=?,resumable=?,stop_reason=?,updated_at=? WHERE operation_id=? AND generation=? AND status='running'",
                                    (status, encode(used), int(resumable), getattr(exc, "reason", "experiment_failed"), now(), operation_id, generation)).rowcount
                    if changed != 1:
                        return
                    current.execute("UPDATE research_operation_attempt SET status=? WHERE operation_id=? AND attempt=?", (status, operation_id, attempt))
        finally:
            current.close()
    finally:
        if conn is not None:
            conn.close()
        with _lock:
            _active.discard(operation_id)

