"""Storage budget, policy and fail-closed cleanup governance for isolated practice data."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

MAX_BUDGET = 10 * 1024**4


def _connect(request):
    from app.practice_data.api import _connect as connect
    return connect(request)


def _ensure(conn):
    # DDL is deliberately executed statement by statement: executescript commits
    # pending work in sqlite3 and would break callers' transaction boundaries.
    conn.execute("CREATE TABLE IF NOT EXISTS storage_policy(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,budget_bytes INTEGER NOT NULL,window_years INTEGER NOT NULL,pinned_json TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS storage_idempotency(key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, response_json TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS storage_preview(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, candidates_json TEXT NOT NULL, expires REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS storage_cleanup_result(id TEXT PRIMARY KEY, result_json TEXT NOT NULL)")


def _policy(conn):
    _ensure(conn)
    r = conn.execute("SELECT version,budget_bytes,window_years,pinned_json FROM storage_policy WHERE id=1").fetchone()
    if r is None:
        return {"policy_version": 1, "budget_bytes": 10737418240, "working_window_years": 5, "pinned_ranges": []}
    return {"policy_version": int(r[0]), "budget_bytes": int(r[1]), "working_window_years": int(r[2]), "pinned_ranges": json.loads(r[3])}


def _fingerprint(conn):
    rows = _version_rows(conn)
    refs = _session_version_ids(conn)
    return hashlib.sha256(json.dumps([ [tuple(r) for r in rows], refs ], sort_keys=True).encode()).hexdigest()


def _version_rows(conn):
    """Return prepared versions when that optional schema has been created."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='practice_data_version'"
    ).fetchone()
    if not exists:
        return []
    return conn.execute(
        "SELECT version_id,source_code,start_date,end_date,record_count "
        "FROM practice_data_version ORDER BY version_id"
    ).fetchall()


def _session_version_ids(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='practice_session'"
    ).fetchone()
    if not exists:
        return []
    return [str(row[0]) for row in conn.execute(
        "SELECT DISTINCT data_version_id FROM practice_session ORDER BY data_version_id"
    ).fetchall()]


def _usage(root: Path):
    root = root.resolve(strict=True)
    result = {"data": 0, "practice": 0, "backup": 0, "logs_cache": 0}
    roots = [(root / "data", "data"), (root / "backups", "backup"), (root / "logs", "logs_cache"), (root, "practice")]
    seen = set()
    for base, category in roots:
        if not base.exists():
            continue
        for p in base.rglob("*"):
            try:
                if p.is_symlink():
                    continue
                if p.is_file():
                    resolved = p.resolve(strict=True)
                    if not resolved.is_relative_to(root):
                        continue
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    size = resolved.stat().st_size
                    if category == "practice":
                        result["practice"] += size
                    else:
                        result[category] += size
            except OSError as exc:
                raise RuntimeError("practice_storage_usage_unknown") from exc
    return result


def _date(s):
    if not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d" if len(s) == 10 else "%Y%m%d").date()
    except ValueError:
        return None


def _references(conn, version_id):
    # Any failed lookup is handled by the caller as protected; known application
    # references include simulation versions and snapshots.
    checks = [
        ("practice_session", "SELECT 1 FROM practice_session WHERE data_version_id=? LIMIT 1"),
        ("practice_data_job", "SELECT 1 FROM practice_data_job WHERE result_json LIKE ? LIMIT 1"),
    ]
    found = []
    for label, sql in checks:
        try:
            arg = (f'%"data_version_id":"{version_id}"%',) if label == "practice_data_job" else (version_id,)
            if conn.execute(sql, arg).fetchone():
                found.append(label)
        except Exception:
            found.append("reference_check_unavailable")
    return found


def create_storage_router():
    router = APIRouter()

    @router.get("/storage")
    def get_storage(request: Request):
        from app.practice.runtime import practice_settings
        isolated = practice_settings(getattr(request.app.state, "settings", None))
        conn = _connect(request)
        try:
            _ensure(conn)
            if conn.in_transaction:
                conn.commit()
            p = _policy(conn)
            usage = _usage(isolated.runtime_dir)
            used = sum(usage.values())
            free = shutil.disk_usage(isolated.runtime_dir).free
            return {"ok": True, "categories": usage, "used_bytes": used, "budget_bytes": p["budget_bytes"], "available_bytes": max(0, p["budget_bytes"] - used), "can_prepare": used < p["budget_bytes"] and free > 0, "working_window_years": p["working_window_years"], "pinned_ranges": p["pinned_ranges"], "policy_version": p["policy_version"], "limitations": ["五年窗口不保证固定字节数", "磁盘剩余空间按运行目录所在卷估算"]}
        finally:
            conn.close()

    @router.put("/storage/policy")
    def put_policy(request: Request, body: dict[str, Any]):
        key, budget, ver = body.get("idempotency_key"), body.get("budget_bytes"), body.get("expected_version")
        pins, years = body.get("pinned_ranges", []), body.get("working_window_years", 5)
        valid_pins = isinstance(pins, list) and all(isinstance(x, dict) and _date(x.get("start_date")) and _date(x.get("end_date")) and x["start_date"] <= x["end_date"] for x in pins)
        if not isinstance(key, str) or not key.strip() or isinstance(budget, bool) or not isinstance(budget, int) or not 0 < budget <= MAX_BUDGET or isinstance(ver, bool) or not isinstance(ver, int) or years != 5 or not valid_pins:
            return JSONResponse(status_code=422, content={"ok": False, "reason": "invalid_policy"})
        conn = _connect(request)
        try:
            _ensure(conn)
            if not conn.in_transaction:
                conn.execute("INSERT OR IGNORE INTO storage_policy VALUES(1,1,10737418240,5,'[]')")
                conn.commit()
            request_obj = {"budget_bytes": budget, "working_window_years": years, "pinned_ranges": pins, "expected_version": ver}
            digest = hashlib.sha256(json.dumps(request_obj, sort_keys=True).encode()).hexdigest()
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT request_hash,response_json FROM storage_idempotency WHERE key=?", (key,)).fetchone()
            if old:
                conn.rollback()
                return JSONResponse(status_code=409, content={"ok": False, "reason": "idempotency_conflict"}) if old[0] != digest else json.loads(old[1])
            p = _policy(conn)
            if ver != p["policy_version"]:
                conn.rollback()
                return JSONResponse(status_code=409, content={"ok": False, "reason": "policy_version_conflict"})
            result = {"policy_version": ver + 1, "budget_bytes": budget, "working_window_years": 5, "pinned_ranges": pins}
            cur = conn.execute("UPDATE storage_policy SET version=?,budget_bytes=?,window_years=5,pinned_json=? WHERE id=1 AND version=?", (ver + 1, budget, json.dumps(pins), ver))
            if cur.rowcount != 1:
                conn.rollback()
                return JSONResponse(status_code=409, content={"ok": False, "reason": "policy_version_conflict"})
            conn.execute("INSERT INTO storage_idempotency VALUES(?,?,?)", (key, digest, json.dumps(result)))
            conn.commit()
            return result
        finally:
            conn.close()

    @router.post("/storage/cleanup-preview")
    def preview(request: Request, body: dict[str, Any]):
        from app.practice.runtime import practice_settings
        settings = getattr(request.app.state, "settings", None)
        isolated = practice_settings(settings)
        conn = _connect(request)
        try:
            _ensure(conn)
            if not conn.in_transaction:
                conn.execute("INSERT OR IGNORE INTO storage_policy VALUES(1,1,10737418240,5,'[]')")
                conn.commit()
            p = _policy(conn)
            today = datetime.utcnow().date()
            cutoff = today.replace(year=today.year - 5)
            rows = _version_rows(conn)
            candidates, protected = [], []
            listed = {str(row[0]) for row in rows}
            for version_id in _session_version_ids(conn):
                if version_id not in listed:
                    protected.append({"version_id": version_id, "reason": "referenced:practice_session"})
            for row in rows:
                vid, source, start, end, count = row
                refs = _references(conn, str(vid))
                pinned = any(not (str(end) < x["start_date"] or str(start) > x["end_date"]) for x in p["pinned_ranges"])
                old = _date(str(end)) and _date(str(end)) < cutoff
                if refs:
                    protected.append({"version_id": vid, "reason": "referenced:" + ",".join(refs)})
                elif pinned:
                    protected.append({"version_id": vid, "reason": "pinned_range"})
                elif not old:
                    protected.append({"version_id": vid, "reason": "within_working_window"})
                else:
                    size = int(conn.execute("SELECT COUNT(*) * 96 FROM practice_data_daily WHERE version_id=?", (vid,)).fetchone()[0])
                    candidates.append({"version_id": vid, "source_code": source, "start_date": start, "end_date": end, "estimated_bytes": size})
            ident, expiry = uuid.uuid4().hex, time.time() + 1800
            fp = hashlib.sha256(json.dumps([_fingerprint(conn), p["policy_version"], candidates], sort_keys=True).encode()).hexdigest()
            conn.execute("INSERT INTO storage_preview VALUES(?,?,?,?)", (ident, fp, json.dumps(candidates), expiry))
            conn.commit()
            return {"preview_id": ident, "candidates": candidates, "protected": protected, "estimated_reclaim_bytes": sum(x["estimated_bytes"] for x in candidates), "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expiry))}
        finally:
            conn.close()

    @router.post("/storage/cleanup-preview/{preview_id}/confirm")
    def confirm(request: Request, preview_id: str, body: dict[str, Any]):
        if body.get("confirm") is not True or not isinstance(body.get("idempotency_key"), str) or not body["idempotency_key"].strip():
            return JSONResponse(status_code=422, content={"ok": False, "reason": "confirmation_required"})
        conn = _connect(request)
        try:
            _ensure(conn)
            if not conn.in_transaction:
                conn.execute("INSERT OR IGNORE INTO storage_policy VALUES(1,1,10737418240,5,'[]')")
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            idem = conn.execute("SELECT result_json FROM storage_cleanup_result WHERE id=?", (body["idempotency_key"],)).fetchone()
            if idem:
                conn.rollback()
                return json.loads(idem[0])
            row = conn.execute("SELECT fingerprint,candidates_json,expires FROM storage_preview WHERE id=?", (preview_id,)).fetchone()
            policy = _policy(conn)
            expected = hashlib.sha256(json.dumps([_fingerprint(conn), policy["policy_version"], json.loads(row[1]) if row else []], sort_keys=True).encode()).hexdigest()
            if not row or row[2] < time.time() or row[0] != expected:
                conn.rollback()
                return JSONResponse(status_code=409, content={"ok": False, "reason": "preview_expired_or_changed"})
            candidates = json.loads(row[1]); deleted = []; reclaimed = 0
            for item in candidates:
                vid = item["version_id"]
                if _references(conn, vid):
                    conn.rollback()
                    return JSONResponse(status_code=409, content={"ok": False, "reason": "version_became_referenced"})
                exists = conn.execute("SELECT 1 FROM practice_data_version WHERE version_id=?", (vid,)).fetchone()
                if not exists:
                    conn.rollback()
                    return JSONResponse(status_code=409, content={"ok": False, "reason": "preview_expired_or_changed"})
                conn.execute("DELETE FROM practice_data_daily WHERE version_id=?", (vid,))
                conn.execute("DELETE FROM practice_data_version WHERE version_id=?", (vid,))
                deleted.append(vid); reclaimed += int(item["estimated_bytes"])
            result = {"ok": True, "deleted_versions": deleted, "estimated_reclaim_bytes": reclaimed}
            conn.execute("INSERT INTO storage_cleanup_result VALUES(?,?)", (body["idempotency_key"], json.dumps(result)))
            conn.commit()
            return result
        finally:
            conn.close()

    return router


def assert_prepare_capacity(settings, conn, estimate_bytes: int = 0):
    """Raise before prepare/publication when configured budget or free disk is exhausted."""
    from app.practice.runtime import practice_settings
    isolated = practice_settings(settings)
    policy = _policy(conn)
    used = sum(_usage(isolated.runtime_dir).values())
    disk_root = isolated.runtime_dir
    while not disk_root.exists() and disk_root != disk_root.parent:
        disk_root = disk_root.parent
    free = shutil.disk_usage(disk_root).free
    if used + max(0, estimate_bytes) > policy["budget_bytes"] or free < max(1, estimate_bytes):
        raise RuntimeError("practice_storage_budget_exceeded")
