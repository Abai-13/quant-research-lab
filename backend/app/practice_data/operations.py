"""Persistent, bounded practice-data preparation operations.

The single-worker executor is deliberately shared by all requests: BaoStock's
client maintains process-wide session/rate-limit state, so two jobs must never
query it at once. Job generation is checked immediately before publication.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import multiprocessing
import queue
import sqlite3
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from . import store
from .fetch import SourceDataError, SourceUnavailable, baostock_fetch_daily
from .service import RefreshError, perform_refresh, request_sha256

def _presets() -> list[dict[str, Any]]:
    today = dt.date.today()
    common = {"symbols": ["sh.600000", "sz.000001"], "source_code": "baostock", "limitations": ["最多20证券、366自然日、20000日线行。"]}
    return [
        {"preset_id": "recent-1y", "name": "近一年", "start_date": (today - dt.timedelta(days=365)).isoformat(), "end_date": today.isoformat(), **common},
        {"preset_id": "recent-6m", "name": "近六个月", "start_date": (today - dt.timedelta(days=182)).isoformat(), "end_date": today.isoformat(), **common},
    ]


PRESETS = _presets()
DEFAULT_TIMEOUT_SECONDS = 300
HARD_TIMEOUT_SECONDS = 900
MAX_DAILY_ROWS = 20_000
_executor_lock = threading.RLock()
_executor: concurrent.futures.ThreadPoolExecutor | None = None
_executor_users = 0
_recovered_runtimes: set[str] = set()


def _active_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="practice-data")
        return _executor


def _submit(function: Callable[..., Any], *args: Any) -> concurrent.futures.Future[Any]:
    # Submit while holding the lifecycle lock so the last app shutdown cannot
    # close this executor between lookup and enqueue.
    with _executor_lock:
        return _active_executor().submit(function, *args)


def startup_operations(settings: Any = None) -> None:
    """Acquire one app-lifespan reference and requeue durable in-flight jobs."""
    global _executor_users
    with _executor_lock:
        _active_executor()
        _executor_users += 1
    # Startup recovery is intentionally idempotent: claim each persisted
    # queued/running/publishing row with a generation bump before submission.
    # A killed publisher may have committed its immutable version already;
    # perform_refresh and the bridge are content-addressed/idempotent, while
    # the generation fence prevents an old worker from publishing afterward.
    if settings is None:
        from app.config import load_settings
        settings = load_settings()
    runtime_key = str(settings.runtime_dir.resolve())
    # Multiple FastAPI app instances can share this process and database. Only
    # the first startup for a runtime may claim durable in-flight generations.
    with _executor_lock:
        if runtime_key in _recovered_runtimes:
            return
        try:
            from app.practice.runtime import practice_connect, practice_settings
            # The practice database is lazy. Startup recovery only has work
            # to do when a prior process created durable operation state.
            if not practice_settings(settings).db_path.exists():
                _recovered_runtimes.add(runtime_key)
                return
            conn = practice_connect(settings)
            try:
                ensure_operation_schema(conn)
                rows = conn.execute("SELECT job_id,request_json,timeout_seconds,generation FROM practice_data_job WHERE status IN ('queued','running','publishing') ORDER BY created_at,job_id").fetchall()
                for row in rows:
                    conn.execute("BEGIN IMMEDIATE")
                    changed = conn.execute("UPDATE practice_data_job SET status='queued',generation=generation+1,updated_at=? WHERE job_id=? AND generation=? AND status IN ('queued','running','publishing')", (_now(), row["job_id"], row["generation"])).rowcount
                    conn.commit()
                    if changed:
                        _submit(_run_job, lambda s=settings: practice_connect(s), str(row["job_id"]), store.decode_json(row["request_json"]), int(row["timeout_seconds"]), int(row["generation"])+1, settings)
            finally:
                conn.close()
            _recovered_runtimes.add(runtime_key)
        except Exception:
            # Keep startup fail-closed: unresolved recovery must not fabricate a
            # successful operation; the durable row remains queryable for resume.
            raise


def ensure_operation_schema(conn: sqlite3.Connection) -> None:
    store.ensure_practice_data_schema(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS practice_data_job (
        job_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
        request_json TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
        completed_units INTEGER NOT NULL DEFAULT 0, total_units INTEGER NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1, attempt INTEGER NOT NULL DEFAULT 1,
        timeout_seconds INTEGER NOT NULL DEFAULT 300, result_json TEXT, error_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_practice_data_job_sha ON practice_data_job(request_sha256, status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS practice_data_idempotency (
        idempotency_key TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES practice_data_job(job_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS practice_data_job_unit (
        job_id TEXT NOT NULL REFERENCES practice_data_job(job_id), symbol TEXT NOT NULL,
        rows_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, completed_at TEXT NOT NULL,
        PRIMARY KEY(job_id,symbol))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS practice_data_job_action (
        idempotency_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, action TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL)""")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _baostock_child(result_queue: Any, symbol: str, start_date: str, end_date: str) -> None:
    try:
        result_queue.put(("ok", baostock_fetch_daily(symbol, start_date, end_date)))
    except SourceUnavailable as exc:
        result_queue.put(("unavailable", exc.reason, exc.detail))
    except SourceDataError as exc:
        result_queue.put(("data_error", exc.reason, exc.detail))
    except BaseException as exc:  # child failures become explicit source errors
        result_queue.put(("unavailable", "baostock_worker_failed", str(exc)))


def _isolated_baostock_fetch(symbol: str, start_date: str, end_date: str, timeout: float) -> list[dict[str, Any]]:
    """Run the SDK in a killable subprocess so socket stalls have a hard bound."""
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=_baostock_child, args=(result_queue, symbol, start_date, end_date), daemon=True)
    process.start()
    process.join(max(0.1, timeout))
    if process.is_alive():
        process.terminate()
        process.join(2.0)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(2.0)
        result_queue.close()
        raise SourceUnavailable("baostock_timeout", f"{symbol} 查询超过 {timeout:.1f} 秒，已终止隔离工作进程")
    try:
        outcome = result_queue.get(timeout=1.0)
    except queue.Empty as exc:
        raise SourceUnavailable("baostock_worker_failed", f"{symbol} 查询进程退出且未返回结果") from exc
    finally:
        result_queue.close()
    if outcome[0] == "ok":
        return outcome[1]
    if outcome[0] == "data_error":
        raise SourceDataError(outcome[1], outcome[2])
    raise SourceUnavailable(outcome[1], outcome[2])


def _job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM practice_data_job WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    if result["status"] == "publishing":
        result["status"] = "running"
        result["phase"] = "publishing"
    result["request"] = store.decode_json(result.pop("request_json"))
    for field in ("result_json", "error_json"):
        raw = result.pop(field)
        result[field.removesuffix("_json")] = store.decode_json(raw) if raw else None
    result["operation_id"] = result["job_id"]
    result["kind"] = "data_prepare"
    completed_checkpoints = conn.execute("SELECT COUNT(*) FROM practice_data_job_unit WHERE job_id=?", (job_id,)).fetchone()[0]
    result["progress"] = {
        "phase": result.get("phase", result["status"]),
        "completed_units": int(result["completed_units"]),
        "total_units": int(result["total_units"]),
    }
    result["checkpoint"] = {
        "sequence": int(completed_checkpoints), "steps": [],
        "validation": "checkpointed" if result["status"] == "completed" or int(completed_checkpoints) == int(result["completed_units"]) else "partial",
        "summary": f"已提交 {int(completed_checkpoints)} 个证券单元检查点",
        "validation_status": "validated" if result["status"] == "completed" or int(completed_checkpoints) == int(result["completed_units"]) else "partial",
    }
    version_result = result.get("result") or {}
    quality = version_result.get("quality") or {}
    checkpoint_rows = 0
    for checkpoint in conn.execute("SELECT rows_json FROM practice_data_job_unit WHERE job_id=?", (job_id,)).fetchall():
        try:
            checkpoint_rows += len(store.decode_json(str(checkpoint[0])))
        except (TypeError, ValueError):
            pass
    result["cumulative_usage"] = {"daily_rows": int(quality.get("daily_rows", checkpoint_rows)), "attempt": int(result["attempt"])}
    result["limits"] = {"timeout_seconds": int(result["timeout_seconds"]), "hard_timeout_seconds": HARD_TIMEOUT_SECONDS, "max_daily_rows": MAX_DAILY_ROWS}
    result["resumable"] = result["status"] in {"failed", "interrupted", "cancelled"}
    result["stop_reason"] = (result.get("error") or {}).get("reason")
    return result


def _operation_view(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    from app.agent_runtime.operation import operation_view
    public = operation_view({**job, "task_id": job["job_id"], "usage": job["cumulative_usage"]})
    public["checkpoint"]["summary"] = job["checkpoint"]["summary"]
    public["checkpoint"]["validation_status"] = job["checkpoint"]["validation_status"]
    public["result"] = job.get("result")
    return public


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    ensure_operation_schema(conn)
    return _operation_view(_job(conn, job_id))


def submit_job(
    conn_factory: Callable[[], sqlite3.Connection],
    normalized: dict[str, Any],
    *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, settings=None,
) -> dict[str, Any]:
    if timeout_seconds < 1 or timeout_seconds > HARD_TIMEOUT_SECONDS:
        raise RefreshError("invalid_timeout", f"timeout_seconds 必须为 1..{HARD_TIMEOUT_SECONDS}")
    sha = request_sha256({k: v for k, v in normalized.items() if k != "idempotency_key"})
    conn = conn_factory()
    try:
        ensure_operation_schema(conn)
        conn.commit()
        if settings is not None:
            from app.practice.governance import assert_prepare_capacity
            assert_prepare_capacity(settings, conn)
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT j.* FROM practice_data_idempotency i JOIN practice_data_job j USING(job_id) WHERE i.idempotency_key=?", (normalized["idempotency_key"],)).fetchone()
        if old:
            mapped_sha = conn.execute("SELECT request_sha256 FROM practice_data_idempotency WHERE idempotency_key=?", (normalized["idempotency_key"],)).fetchone()[0]
            if mapped_sha != sha:
                raise RefreshError("idempotency_conflict", "相同 idempotency_key 已绑定不同请求内容", 409)
            return _operation_view(_job(conn, old["job_id"])) or {}
        active = conn.execute("SELECT * FROM practice_data_job WHERE request_sha256=? AND status IN ('queued','running') ORDER BY created_at LIMIT 1", (sha,)).fetchone()
        if active:
            conn.execute("INSERT INTO practice_data_idempotency(idempotency_key,request_sha256,job_id) VALUES(?,?,?)", (normalized["idempotency_key"],sha,active["job_id"]))
            conn.commit()
            return _operation_view(_job(conn, active["job_id"])) or {}
        # Reuse only an intact prior version with identical source and range.
        cache = None if normalized.get("refresh_mode") == "force_refresh" else conn.execute("SELECT version_id FROM practice_data_version WHERE source_code=? AND symbols_json=? AND start_date=? AND end_date=? ORDER BY created_at DESC LIMIT 1", (normalized["source_code"], store.encode_json(normalized["symbols"]), normalized["start_date"], normalized["end_date"])).fetchone()
        job_id = str(uuid4())
        now = _now()
        if cache:
            version = store.get_version(conn, str(cache[0]))
            count = conn.execute("SELECT COUNT(*) FROM practice_data_daily WHERE version_id=?", (cache[0],)).fetchone()[0]
            if version and int(count) == int(version["record_count"]) and version["coverage"].get("content_sha256") == store.version_content_digest(conn, str(cache[0])):
                from .bridge import publish_prepared_dataset
                bridge = publish_prepared_dataset(conn, str(cache[0]))
                response = {**_prepared(version, True), **bridge}
                conn.execute("INSERT INTO practice_data_job(job_id,request_sha256,idempotency_key,request_json,status,progress,completed_units,total_units,generation,timeout_seconds,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id,sha,normalized["idempotency_key"],store.encode_json(normalized),"completed",1,len(normalized["symbols"]),len(normalized["symbols"]),1,timeout_seconds,store.encode_json(response),now,now))
                conn.execute("INSERT INTO practice_data_idempotency(idempotency_key,request_sha256,job_id) VALUES(?,?,?)", (normalized["idempotency_key"],sha,job_id))
                conn.commit()
                return _operation_view(_job(conn, job_id)) or {}
        conn.execute("INSERT INTO practice_data_job(job_id,request_sha256,idempotency_key,request_json,status,progress,completed_units,total_units,generation,timeout_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (job_id,sha,normalized["idempotency_key"],store.encode_json(normalized),"queued",0,0,len(normalized["symbols"]),1,timeout_seconds,now,now))
        conn.execute("INSERT INTO practice_data_idempotency(idempotency_key,request_sha256,job_id) VALUES(?,?,?)", (normalized["idempotency_key"],sha,job_id))
        conn.commit()
    finally:
        conn.close()
    _submit(_run_job, conn_factory, job_id, normalized, timeout_seconds, 1, settings)
    conn = conn_factory()
    try:
        return _operation_view(_job(conn, job_id)) or {}
    finally:
        conn.close()


def _prepared(version: dict[str, Any], cache_hit: bool) -> dict[str, Any]:
    return {
        "source_code": version["source_code"], "synthetic": version["synthetic"],
        "data_version_id": version.get("data_version_id", version["version_id"]),
        "pool_snapshot_id": None, "symbols": version["symbols"],
        "start_date": version["start_date"], "end_date": version["end_date"],
        "content_digest": version.get("content_digest") or version.get("coverage", {}).get("content_sha256"), "quality": version["coverage"],
        "eligibility": version.get("eligibility", "restricted_analysis"),
        "limitations": version["limitations"], "cache_hit": cache_hit,
    }


def _set_progress(conn_factory: Callable[[], sqlite3.Connection], job_id: str, status: str, completed: int, total: int, *, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None, generation: int | None = None) -> None:
    conn = conn_factory()
    try:
        fraction = 1.0 if status == "completed" else min(0.95, completed / max(1, total) * 0.95)
        values = [status, fraction, completed, _now(), store.encode_json(result) if result is not None else None, store.encode_json(error) if error is not None else None, job_id]
        sql = "UPDATE practice_data_job SET status=?,progress=?,completed_units=?,updated_at=?,result_json=COALESCE(?,result_json),error_json=COALESCE(?,error_json) WHERE job_id=?"
        if generation is not None:
            sql += " AND generation=?"
            values.append(generation)
        conn.execute(sql, values)
        conn.commit()
    finally:
        conn.close()


def _load_checkpoints(conn: sqlite3.Connection, job_id: str) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute("SELECT symbol,rows_json,content_sha256 FROM practice_data_job_unit WHERE job_id=?", (job_id,)).fetchall()
    result = {}
    for symbol, raw, digest in rows:
        if hashlib.sha256(str(raw).encode("utf-8")).hexdigest() != str(digest):
            conn.execute("DELETE FROM practice_data_job_unit WHERE job_id=? AND symbol=?", (job_id,symbol))
            continue
        decoded = store.decode_json(str(raw))
        if isinstance(decoded, list) and all(isinstance(item, dict) for item in decoded):
            result[str(symbol)] = decoded
        else:
            conn.execute("DELETE FROM practice_data_job_unit WHERE job_id=? AND symbol=?", (job_id,symbol))
    conn.commit()
    return result


def _run_job(conn_factory: Callable[[], sqlite3.Connection], job_id: str, normalized: dict[str, Any], timeout_seconds: int, generation: int, settings: Any = None) -> None:
    started = time.monotonic()
    total = len(normalized["symbols"])
    _set_progress(conn_factory, job_id, "running", 0, total, generation=generation)
    rows_done = 0

    def progress(done: int, _count: int) -> None:
        nonlocal rows_done
        rows_done = done
        _set_progress(conn_factory, job_id, "running", done, total, generation=generation)

    def publication_allowed() -> bool:
        if time.monotonic() - started >= min(timeout_seconds, HARD_TIMEOUT_SECONDS):
            return False
        check = conn_factory()
        try:
            check.execute("BEGIN IMMEDIATE")
            cursor = check.execute(
                "UPDATE practice_data_job SET status='publishing',updated_at=?"
                " WHERE job_id=? AND generation=? AND status='running'",
                (_now(),job_id,generation),
            )
            if cursor.rowcount != 1:
                current = check.execute("SELECT status,generation FROM practice_data_job WHERE job_id=?", (job_id,)).fetchone()
            check.commit()
            return cursor.rowcount == 1 or bool(current and current[0] == "publishing" and int(current[1]) == generation)
        finally:
            check.close()

    def fetcher(symbol: str, start_date: str, end_date: str):
        if normalized["source_code"] != "baostock":
            from .service import PROVIDERS
            return PROVIDERS[normalized["source_code"]](symbol, start_date, end_date)
        remaining = min(timeout_seconds, HARD_TIMEOUT_SECONDS) - (time.monotonic() - started)
        if remaining <= 0:
            raise SourceUnavailable("prepare_timeout", "历史准备超过设置的任务超时")
        return _isolated_baostock_fetch(symbol, start_date, end_date, min(remaining, 60.0))

    def save_checkpoint(symbol: str, rows: list[dict[str, Any]]) -> None:
        encoded = store.encode_json(rows)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        checkpoint_conn = conn_factory()
        try:
            checkpoint_conn.execute("BEGIN IMMEDIATE")
            current = checkpoint_conn.execute(
                "SELECT status,generation FROM practice_data_job WHERE job_id=?", (job_id,)
            ).fetchone()
            if not current or current[0] != "running" or int(current[1]) != generation:
                checkpoint_conn.rollback()
                return
            checkpoint_conn.execute(
                "INSERT INTO practice_data_job_unit(job_id,symbol,rows_json,content_sha256,completed_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(job_id,symbol) DO UPDATE SET rows_json=excluded.rows_json,content_sha256=excluded.content_sha256,completed_at=excluded.completed_at",
                (job_id,symbol,encoded,digest,_now()),
            )
            checkpoint_conn.commit()
        finally:
            checkpoint_conn.close()

    conn = conn_factory()
    try:
        current = _job(conn, job_id)
        if not current or int(current["generation"]) != generation or current["status"] == "cancelled":
            return
        cached_units = _load_checkpoints(conn, job_id)
        if cached_units:
            rows_done = len(cached_units)
            _set_progress(conn_factory, job_id, "running", rows_done, total, generation=generation)
        response, _reused = perform_refresh(
            conn, normalized, progress_callback=progress,
            publish_guard=publication_allowed, fetcher=fetcher,
            cached_units=cached_units, checkpoint_callback=save_checkpoint,
        )
        if not publication_allowed():
            raise RefreshError("publication_fenced", "任务已取消或超时，拒绝桥接发布", 409)
        from app.practice.governance import assert_prepare_capacity
        if settings is not None:
            assert_prepare_capacity(settings, conn)
        from .bridge import publish_prepared_dataset
        bridge = publish_prepared_dataset(conn, response["version"]["version_id"])
        prepared_dataset = {
            **_prepared(response["version"], bool(response.get("reused"))),
            **bridge,
            "cache_hit": bool(response.get("reused")),
        }
        current = _job(conn, job_id)
        if not current or int(current["generation"]) != generation or current["status"] == "cancelled":
            return
        _set_progress(conn_factory, job_id, "completed", total, total, result=prepared_dataset, generation=generation)
    except RefreshError as exc:
        status = "interrupted" if exc.reason == "publication_fenced" and time.monotonic() - started >= min(timeout_seconds, HARD_TIMEOUT_SECONDS) else "cancelled" if exc.reason == "publication_fenced" else "failed"
        _set_progress(conn_factory, job_id, status, rows_done, total, error={"reason": exc.reason, "detail": exc.detail}, generation=generation)
    except Exception as exc:  # noqa: BLE001
        _set_progress(conn_factory, job_id, "failed", rows_done, total, error={"reason": "prepare_failed", "detail": str(exc)}, generation=generation)
    finally:
        conn.close()


def _action_digest(job_id: str, action: str, expected_generation: int) -> str:
    payload = store.encode_json({"job_id": job_id, "action": action, "expected_generation": expected_generation})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cancel_job(conn_factory: Callable[[], sqlite3.Connection], job_id: str, expected_generation: int, idempotency_key: str) -> dict[str, Any] | None:
    conn = conn_factory()
    try:
        ensure_operation_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        digest = _action_digest(job_id, "cancel", expected_generation)
        previous = conn.execute("SELECT request_sha256,response_json FROM practice_data_job_action WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if previous:
            if previous[0] != digest:
                raise RefreshError("idempotency_conflict", "该 idempotency_key 已绑定不同操作", 409)
            conn.commit()
            return store.decode_json(previous[1])
        cursor = conn.execute("UPDATE practice_data_job SET status='cancelled',generation=generation+1,updated_at=? WHERE job_id=? AND generation=? AND status IN ('queued','running','interrupted','failed')", (_now(),job_id,expected_generation))
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        response = _operation_view(_job(conn, job_id))
        conn.execute("INSERT INTO practice_data_job_action(idempotency_key,job_id,action,request_sha256,response_json,created_at) VALUES(?,?,?,?,?,?)", (idempotency_key,job_id,"cancel",digest,store.encode_json(response),_now()))
        conn.commit()
        return response
    finally:
        conn.close()


def resume_job(conn_factory: Callable[[], sqlite3.Connection], job_id: str, expected_generation: int, idempotency_key: str) -> dict[str, Any] | None:
    conn = conn_factory()
    timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    start_runner = False
    try:
        ensure_operation_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        digest = _action_digest(job_id, "resume", expected_generation)
        previous = conn.execute("SELECT request_sha256,response_json FROM practice_data_job_action WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if previous:
            if previous[0] != digest:
                raise RefreshError("idempotency_conflict", "该 idempotency_key 已绑定不同操作", 409)
            conn.commit()
            return store.decode_json(previous[1])
        current = _job(conn, job_id)
        if not current or int(current["generation"]) != expected_generation or current["status"] not in {"interrupted", "failed", "cancelled"}:
            conn.rollback()
            return None
        generation = expected_generation + 1
        normalized = current["request"]
        timeout_seconds = int(current["limits"]["timeout_seconds"])
        conn.execute("UPDATE practice_data_job SET status='queued',generation=?,attempt=attempt+1,progress=0,completed_units=0,error_json=NULL,updated_at=? WHERE job_id=? AND generation=?", (generation,_now(),job_id,expected_generation))
        response = _operation_view(_job(conn, job_id))
        conn.execute("INSERT INTO practice_data_job_action(idempotency_key,job_id,action,request_sha256,response_json,created_at) VALUES(?,?,?,?,?,?)", (idempotency_key,job_id,"resume",digest,store.encode_json(response),_now()))
        conn.commit()
        start_runner = True
    finally:
        conn.close()
    if start_runner:
        _submit(_run_job, conn_factory, job_id, normalized, timeout_seconds, generation, None)
    return response


def startup_check(settings: Any, policy: dict[str, Any], conn_factory: Callable[[], sqlite3.Connection]) -> dict[str, Any]:
    """Optional non-blocking startup warmup; only a saved, enabled scope runs."""
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        return {"started": False, "reason": "startup_prepare_disabled"}
    request = policy.get("request")
    if not isinstance(request, dict):
        return {"started": False, "reason": "saved_scope_missing"}
    try:
        from .service import validate_refresh_payload
        normalized = validate_refresh_payload(request)
        operation = submit_job(conn_factory, normalized)
        return {"started": True, "job_id": operation.get("job_id"), "status": operation.get("status")}
    except RefreshError as exc:
        return {"started": False, "reason": exc.reason}


def shutdown_operations(*, wait: bool = False) -> None:
    """Release one app reference; close the serial worker after the last app.

    A later app in the same process creates a fresh single-worker executor.
    The last app always drains accepted work before the executor is replaced,
    even when the legacy ``wait`` argument is false. This keeps BaoStock's
    process-wide session serial across executor generations and avoids leaving
    persisted queued rows without a runner.
    """
    global _executor, _executor_users
    with _executor_lock:
        if _executor_users > 0:
            _executor_users -= 1
        if _executor_users == 0:
            executor, _executor = _executor, None
            if executor is not None:
                # Keep the lock until old work drains. A new app cannot create
                # another BaoStock executor beside the retiring one.
                executor.shutdown(wait=True, cancel_futures=False)
