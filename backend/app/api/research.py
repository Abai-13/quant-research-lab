"""Research record and frozen experiment-plan HTTP API.

The owning application may mount ``create_research_router`` under its API app;
this module never changes the application lifecycle or practice database.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.config import load_settings
from app.research.service import ResearchError, create_plan, create_record, idempotent_replay, reserve_idempotency, revise_record, save_idempotent_response
from app.research.store import research_connect


def _connect(request: Request) -> sqlite3.Connection:
    return research_connect(getattr(request.app.state, "settings", None) or load_settings())


def _error(exc: ResearchError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "reason": exc.reason, "detail": exc.detail})


def _replay(response: dict[str, Any]) -> Any:
    code = int(response.get("_http_status", 200))
    return JSONResponse(status_code=code, content={key: value for key, value in response.items() if key != "_http_status"}) if code != 200 else {key: value for key, value in response.items() if key != "_http_status"}


def create_research_router() -> APIRouter:
    router = APIRouter(prefix="/api/research", tags=["research"])

    @router.get("/records")
    def search_records(request: Request, q: str = "", kind: str | None = None, as_of: str | None = None,
                       practice_id: str | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        conn = _connect(request)
        try:
            if practice_id and (not as_of or not re.fullmatch(r"\d{8}", as_of)):
                return {"ok": True, "records": [], "total": 0, "limit": limit, "offset": offset}
            filters, filter_params = [], []
            if kind:
                filters.append("kind=?"); filter_params.append(kind)
            if q.strip():
                filters.append("body_json LIKE ?"); filter_params.append(f"%{q.strip()[:200]}%")
            if practice_id:
                cte = "WITH visible AS (SELECT r.record_id,r.kind,r.created_at record_created_at,v.revision_id,v.parent_revision_id,v.body_json,v.body_sha256,v.created_at revision_created_at,ROW_NUMBER() OVER(PARTITION BY r.record_id ORDER BY v.created_at DESC,v.rowid DESC) rn FROM research_record r JOIN research_revision v ON v.record_id=r.record_id WHERE json_extract(v.body_json,'$.evidence_as_of') IS NOT NULL AND json_extract(v.body_json,'$.evidence_as_of')<=? AND substr(replace(v.created_at,'-',''),1,8)<=? AND (json_extract(v.body_json,'$.scope')='global' OR (json_extract(v.body_json,'$.scope')='simulation' AND EXISTS(SELECT 1 FROM json_each(v.body_json,'$.source_refs') ref WHERE json_extract(ref.value,'$.owner_id')=?))))"
                base_params = [as_of, as_of, practice_id]
                where = " WHERE rn=1" + (" AND " + " AND ".join(filters) if filters else "")
                count = int(conn.execute(cte + " SELECT COUNT(*) FROM visible" + where, [*base_params, *filter_params]).fetchone()[0])
                rows = conn.execute(cte + " SELECT * FROM visible" + where + " ORDER BY record_created_at DESC,record_id LIMIT ? OFFSET ?", [*base_params, *filter_params, limit, offset]).fetchall()
            else:
                where = " WHERE v.revision_id=r.current_revision_id" + (" AND " + " AND ".join("r.kind=?" if f == "kind=?" else "v.body_json LIKE ?" for f in filters) if filters else "")
                base = " FROM research_record r JOIN research_revision v ON v.record_id=r.record_id"
                count = int(conn.execute("SELECT COUNT(*)" + base + where, filter_params).fetchone()[0])
                rows = conn.execute("SELECT r.record_id,r.kind,r.created_at record_created_at,v.revision_id,v.parent_revision_id,v.body_json,v.body_sha256,v.created_at revision_created_at" + base + where + " ORDER BY r.created_at DESC,r.record_id LIMIT ? OFFSET ?", [*filter_params, limit, offset]).fetchall()
            records = [{"record_id": row["record_id"], "revision_id": row["revision_id"],
                        "parent_revision_id": row["parent_revision_id"], "kind": row["kind"],
                        "body": json.loads(row["body_json"]), "body_sha256": row["body_sha256"],
                        "created_at": row["revision_created_at"]} for row in rows]
            return {"ok": True, "records": records, "total": count, "limit": limit, "offset": offset}
        finally:
            conn.close()

    @router.post("/records")
    def add_record(request: Request, body: dict[str, Any]):
        conn = _connect(request)
        try:
            replay = idempotent_replay(conn, "create_record", body)
            if replay is not None:
                return _replay(replay)
            payload = {key: value for key, value in body.items() if key != "idempotency_key"}
            response = {"ok": True, "record": create_record(conn, payload)}
            save_idempotent_response(conn, "create_record", body, response)
            return response
        except ResearchError as exc:
            return _error(exc)
        finally:
            conn.close()

    @router.get("/records/{record_id}")
    def read_record(request: Request, record_id: str, as_of: str | None = None, practice_id: str | None = None,
                    history_limit: int = Query(20, ge=1, le=100), history_offset: int = Query(0, ge=0)):
        conn = _connect(request)
        try:
            where = "r.record_id=?"
            params: list[Any] = [record_id]
            if practice_id:
                if not as_of or not re.fullmatch(r"\d{8}", as_of):
                    return JSONResponse(status_code=404, content={"ok": False, "reason": "record_not_visible", "detail": "模拟受限查询必须提供有效 as_of 日期"})
                where += " AND json_extract(v.body_json,'$.evidence_as_of') IS NOT NULL AND json_extract(v.body_json,'$.evidence_as_of')<=? AND substr(replace(v.created_at,'-',''),1,8)<=? AND (json_extract(v.body_json,'$.scope')='global' OR (json_extract(v.body_json,'$.scope')='simulation' AND EXISTS(SELECT 1 FROM json_each(v.body_json,'$.source_refs') ref WHERE json_extract(ref.value,'$.owner_id')=?)))"
                params.extend((as_of, as_of, practice_id))
            base = " FROM research_record r JOIN research_revision v ON v.record_id=r.record_id WHERE " + where
            latest = conn.execute("SELECT r.kind,r.created_at record_created_at,v.*" + base + " ORDER BY v.created_at DESC,v.rowid DESC LIMIT 1", params).fetchone()
            if latest is None:
                return JSONResponse(status_code=404, content={"ok": False, "reason": "record_not_found", "detail": "研究记录不存在"})
            history_rows = conn.execute("SELECT v.revision_id,v.parent_revision_id,v.body_sha256,v.created_at" + base + " ORDER BY v.rowid LIMIT ? OFFSET ?", [*params, history_limit, history_offset]).fetchall()
            history_total = int(conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0])
            body = json.loads(latest["body_json"])
            return {"ok": True, "record": {"record_id": record_id, "kind": latest["kind"], "body": body,
                "revision_id": latest["revision_id"], "parent_revision_id": latest["parent_revision_id"],
                "body_sha256": latest["body_sha256"], "created_at": latest["created_at"],
                "revision_history": [dict(item) for item in history_rows],
                "revision_history_total": history_total, "history_limit": history_limit, "history_offset": history_offset}}
        finally:
            conn.close()

    @router.post("/records/{record_id}/revisions")
    def append_revision(request: Request, record_id: str, body: dict[str, Any]):
        conn = _connect(request)
        try:
            operation = f"append_revision:{record_id}"
            replay = reserve_idempotency(conn, operation, body)
            if replay is not None and replay.get("pending"):
                return JSONResponse(status_code=202, content={"ok": True, "pending": True, "detail": "相同请求仍在处理中；请稍后重试相同幂等键。"})
            if replay is not None:
                return _replay(replay)
            payload = {key: value for key, value in body.items() if key != "idempotency_key"}
            response = {"ok": True, "revision": revise_record(conn, record_id, payload)}
            save_idempotent_response(conn, operation, body, response)
            return response
        except ResearchError as exc:
            return _error(exc)
        finally:
            conn.close()

    @router.post("/plans")
    def add_plan(request: Request, body: dict[str, Any]):
        body = dict(body)
        if "evaluation_dates" not in body:
            try:
                source = body.get("source_context") or {}
                practice_path = (getattr(request.app.state, "settings", None) or load_settings()).runtime_dir.resolve() / "practice" / "app.db"
                practice = sqlite3.connect(practice_path.as_uri() + "?mode=ro", uri=True, timeout=3.0)
                practice.row_factory = sqlite3.Row
                try:
                    from app.data.chunks import VersionView
                    from app.practice.core import require_session
                    session = require_session(practice, str(source.get("practice_id", "")))
                    view = VersionView(practice, session["data_version_id"], expected_source_code=session["source_code"])
                    dates = [day for day in view.calendar_days(on_or_before=session["end_date"]) if day > str((body.get("training_dates") or [""])[-1])]
                    if len(dates) < 120:
                        raise ResearchError("evaluation_window_unavailable", "默认评估需要120个独立交易日；可提供满足条件的显式评估区间")
                    body["evaluation_dates"] = dates[:120]
                finally:
                    practice.close()
            except ResearchError as exc:
                return _error(exc)
            except Exception as exc:  # fail closed; never substitute guessed dates
                return JSONResponse(status_code=422, content={"ok": False, "reason": "evaluation_window_unavailable", "detail": "无法从冻结模拟数据版本派生默认120日评估区间"})
        conn = _connect(request)
        try:
            replay = idempotent_replay(conn, "create_plan", body)
            if replay is not None:
                return _replay(replay)
            payload = {key: value for key, value in body.items() if key != "idempotency_key"}
            response = {"ok": True, "plan": create_plan(conn, payload)}
            save_idempotent_response(conn, "create_plan", body, response)
            return response
        except ResearchError as exc:
            return _error(exc)
        finally:
            conn.close()

    @router.get("/plans/{plan_id}")
    def get_plan(request: Request, plan_id: str):
        conn = _connect(request)
        try:
            row = conn.execute("SELECT plan_revision,status,plan_json,plan_sha256,created_at FROM research_plan WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                return JSONResponse(status_code=404, content={"ok": False, "reason": "plan_not_found", "detail": "研究方案不存在"})
            return {"ok": True, "plan": {"plan_id": plan_id, "plan_revision": row[0], "status": row[1],
                    **json.loads(row[2]), "plan_digest": row[3], "created_at": row[4]}}
        finally:
            conn.close()

    @router.post("/experiments")
    def start_experiment(request: Request, body: dict[str, Any]):
        plan_id = body.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            return JSONResponse(status_code=422, content={"ok": False, "reason": "plan_id_required", "detail": "plan_id required"})
        try:
            from app.research.executor import create_operation
            value = create_operation(getattr(request.app.state, "settings", None) or load_settings(), plan_id, body)
            return JSONResponse(status_code=202, content={"ok": True, "operation": value})
        except ResearchError as exc:
            return _error(exc)

    @router.get("/experiments")
    def list_experiments(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        conn = _connect(request)
        try:
            from app.research.executor import _view
            rows = conn.execute("SELECT * FROM research_operation ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM research_operation").fetchone()[0]
            return {"ok": True, "experiments": [_view(row) for row in rows], "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()

    @router.get("/experiments/{experiment_id}")
    def get_experiment(request: Request, experiment_id: str):
        try:
            from app.research.executor import get_operation
            settings = getattr(request.app.state, "settings", None) or load_settings()
            value = get_operation(settings, experiment_id)
            response = {"ok": True, "operation": value}
            if value.get("result") is not None:
                response["result"] = value["result"]
            return response
        except ResearchError as exc:
            return _error(exc)

    @router.post("/experiments/{experiment_id}/cancel")
    def cancel_experiment(request: Request, experiment_id: str, body: dict[str, Any]):
        try:
            from app.research.executor import cancel_operation
            expected = body.get("expected_generation")
            if type(expected) is not int:
                raise ResearchError("generation_required", "expected_generation must be an integer", 422)
            value = cancel_operation(getattr(request.app.state, "settings", None) or load_settings(), experiment_id, expected)
            return {"ok": True, "operation": value}
        except ResearchError as exc:
            return _error(exc)

    @router.post("/experiments/{experiment_id}/resume")
    def resume_experiment(request: Request, experiment_id: str, body: dict[str, Any]):
        try:
            from app.research.executor import resume_operation
            expected, key = body.get("expected_generation"), body.get("idempotency_key")
            if type(expected) is not int:
                raise ResearchError("generation_required", "expected_generation must be an integer", 422)
            value = resume_operation(getattr(request.app.state, "settings", None) or load_settings(), experiment_id, expected, key)
            return JSONResponse(status_code=202, content={"ok": True, "operation": value})
        except ResearchError as exc:
            return _error(exc)

    return router
