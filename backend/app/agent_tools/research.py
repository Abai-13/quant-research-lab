"""Strict, read-only and draft-only research tools for the Agent boundary."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from app.config import Settings
from app.research.service import ResearchError, validate_plan
from app.research.store import research_path

_TOOLS = [
    {"type": "function", "function": {"name": "search_research_records", "description": "检索当前模拟可见范围内的研究记录；不会返回超出 as-of 日期的证据。", "parameters": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 200}, "kind": {"type": ["string", "null"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["query", "limit"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_research_record", "description": "读取当前上下文可见的单条研究及追加修订历史。", "parameters": {"type": "object", "properties": {"record_id": {"type": "string", "maxLength": 100}}, "required": ["record_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "prepare_experiment_draft", "description": "按已实现的单参数白名单校验方案草稿；不会启动实验或揭晓评估结果。", "parameters": {"type": "object", "properties": {"plan": {"type": "object"}}, "required": ["plan"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_experiment_status", "description": "查看授权实验状态；运行中不泄漏评估收益、候选对比或中间指标。", "parameters": {"type": "object", "properties": {"experiment_id": {"type": "string", "maxLength": 100}}, "required": ["experiment_id"], "additionalProperties": False}}},
]


def research_tool_definitions() -> list[dict[str, Any]]:
    return json.loads(json.dumps(_TOOLS))


def _readonly_research_connect(settings: Settings) -> sqlite3.Connection:
    """Open the research database without creating schema or migrating it."""
    path = research_path(settings)
    if not path.is_file():
        raise ResearchError("research_database_missing", "研究数据库不存在", 503)
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise ResearchError("research_database_unavailable", "研究数据库当前不可读取", 503) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _record_visibility_filter(conn: sqlite3.Connection, context: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    """Build one revision predicate shared by search and detail reads."""
    has_visibility = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_revision_visibility'"
    ).fetchone() is not None
    as_of = str(context["revealed_date"])
    owner_id = str(context["practice_id"])
    evidence = "json_extract(v.body_json,'$.evidence_as_of')"
    scope = "json_extract(v.body_json,'$.scope')"
    created_as_of = "substr(replace(v.created_at,'-',''),1,8)"
    same_owner = (
        "EXISTS(SELECT 1 FROM json_each(v.body_json,'$.source_refs') ref "
        "WHERE json_extract(ref.value,'$.owner_id')=?)"
    )
    if has_visibility:
        join = " LEFT JOIN research_revision_visibility vis ON vis.revision_id=v.revision_id"
        predicate = (
            f"{evidence} IS NOT NULL AND {evidence}<=? AND ("
            f"({scope}='global' AND {created_as_of}<=?) OR "
            f"({scope}='simulation' AND ("
            "(vis.revision_id IS NOT NULL AND vis.owner_type='practice' "
            "AND vis.owner_id=? AND vis.visible_as_of<=?) OR "
            f"(vis.revision_id IS NULL AND {same_owner} AND {created_as_of}<=?)"
            "))"
            ")"
        )
        params = [as_of, as_of, owner_id, as_of, owner_id, as_of]
    else:
        # Legacy database fallback retains both date cutoffs and source ownership.
        join = ""
        predicate = (
            f"{evidence} IS NOT NULL AND {evidence}<=? AND ("
            f"({scope}='global' AND {created_as_of}<=?) OR "
            f"({scope}='simulation' AND {same_owner} AND {created_as_of}<=?)"
            ")"
        )
        params = [as_of, as_of, owner_id, as_of]
    return join, predicate, params


def _visible_record(record: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the selected revision with the exact simulation snapshot that admitted it."""
    body = json.loads(record["body_json"])
    scope = body.get("scope")
    practice_id = str(context["practice_id"])
    return {
        "record_id": record["record_id"],
        "revision_id": record["revision_id"],
        "parent_revision_id": record["parent_revision_id"],
        "kind": record["kind"],
        "body": body,
        "evidence_as_of": body.get("evidence_as_of"),
        "created_at": record["created_at"],
        "scope": scope,
        "owner_id": practice_id if scope == "simulation" else None,
        "as_of": str(context["revealed_date"]),
    }


def _literal_like_pattern(value: str) -> str:
    """Match a literal substring without interpreting SQL LIKE metacharacters."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def invoke_research_tool(settings: Settings, context: Mapping[str, Any], name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one whitelisted tool against the captured simulation time."""
    if context.get("scope") != "simulation" or not context.get("practice_id") or not context.get("revealed_date"):
        raise ResearchError("research_context_required", "研究工具必须绑定本地模拟及已揭晓日期", 409)
    if name == "prepare_experiment_draft":
        plan = validate_plan(dict(arguments.get("plan") or {}))
        return {"draft": plan, "starts_experiment": False, "discloses_evaluation": False}
    conn = _readonly_research_connect(settings)
    try:
        join, predicate, params = _record_visibility_filter(conn, context)
        if name == "search_research_records":
            query = arguments.get("query", "")
            if not isinstance(query, str) or len(query) > 200:
                raise ResearchError("tool_arguments_invalid", "query 无效")
            limit = max(1, min(int(arguments.get("limit", 20)), 50))
            rows = conn.execute(
                "WITH visible AS ("
                "SELECT r.record_id,r.kind,v.body_json,v.revision_id,v.parent_revision_id,v.created_at,"
                "ROW_NUMBER() OVER(PARTITION BY r.record_id ORDER BY v.created_at DESC,v.rowid DESC) rn "
                "FROM research_record r JOIN research_revision v ON v.record_id=r.record_id"
                + join + " WHERE " + predicate + ") "
                "SELECT record_id,kind,body_json,revision_id,parent_revision_id,created_at FROM visible WHERE rn=1 "
                "AND body_json LIKE ? ESCAPE '\\' AND (? IS NULL OR kind=?) "
                "ORDER BY created_at DESC,record_id LIMIT ?",
                (*params, _literal_like_pattern(query[:200]), arguments.get("kind"), arguments.get("kind"), limit),
            ).fetchall()
            records = [_visible_record(row, context) for row in rows]
            result = {"records": records, "as_of": context["revealed_date"],
                      "practice_id": str(context["practice_id"]), "scope": "application_research_only"}
            if not records:
                result["notice"] = "当前可见范围内没有找到匹配记录。可以换个关键词或说法再试。"
            return result
        if name == "read_research_record":
            row = conn.execute(
                "WITH visible AS ("
                "SELECT r.kind,v.body_json,v.revision_id,v.parent_revision_id,v.created_at,"
                "ROW_NUMBER() OVER(ORDER BY v.created_at DESC,v.rowid DESC) rn "
                "FROM research_record r JOIN research_revision v ON v.record_id=r.record_id"
                + join + " WHERE r.record_id=? AND " + predicate + ") "
                "SELECT kind,body_json,revision_id,parent_revision_id,created_at FROM visible WHERE rn=1",
                (arguments.get("record_id"), *params),
            ).fetchone()
            if row is None:
                raise ResearchError("record_not_found", "研究记录不存在或当前模拟不可见", 404)
            return _visible_record({"record_id": arguments["record_id"], **dict(row)}, context) | {
                "practice_id": str(context["practice_id"]),
            }
        if name == "get_experiment_status":
            row = conn.execute(
                "SELECT o.status,o.progress_json,o.checkpoint_json,o.usage_json,o.limits_json,"
                "o.attempt,o.generation,o.resumable,o.stop_reason,o.created_at,o.updated_at,p.plan_json "
                "FROM research_operation o JOIN research_plan p ON p.plan_id=o.plan_id "
                "WHERE o.operation_id=?", (arguments.get("experiment_id"),)
            ).fetchone()
            if row is None:
                raise ResearchError("experiment_not_found", "授权实验不存在", 404)
            plan = json.loads(row["plan_json"])
            source = plan.get("source_context", {})
            if source.get("practice_id") != context["practice_id"] or row["created_at"].replace("-", "")[:8] > str(context["revealed_date"]):
                raise ResearchError("experiment_not_visible", "该实验超出当前模拟可见范围", 404)
            operation = {"task_id": arguments["experiment_id"], "kind": "research_experiment", "status": row["status"],
                         "attempt": row["attempt"], "generation": row["generation"], "progress": json.loads(row["progress_json"]),
                         "checkpoint": json.loads(row["checkpoint_json"]), "usage": json.loads(row["usage_json"]),
                         "limits": json.loads(row["limits_json"]), "resumable": bool(row["resumable"]),
                         "stop_reason": row["stop_reason"], "created_at": row["created_at"], "updated_at": row["updated_at"]}
            from app.research.executor import operation_view
            result = operation_view(operation)
            evaluation_dates = plan.get("evaluation_dates") or []
            if row["status"] == "completed" and evaluation_dates and evaluation_dates[-1] <= str(context["revealed_date"]):
                stored = conn.execute(
                    "SELECT status_json FROM research_experiment WHERE experiment_id=? AND status='completed'",
                    (arguments.get("experiment_id"),)
                ).fetchone()
                if stored:
                    result["final_summary"] = json.loads(stored["status_json"]).get("final_summary")
            else:
                result["evaluation_metrics"] = None
            return result
        raise ResearchError("tool_unknown", "未授权研究工具", 400)
    finally:
        conn.close()
