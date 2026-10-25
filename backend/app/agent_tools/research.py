"""Strict, read-only and draft-only research tools for the Agent boundary."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from app.config import Settings
from app.research.service import ResearchError, validate_plan
from app.research.store import research_connect

_TOOLS = [
    {"type": "function", "function": {"name": "search_research_records", "description": "检索当前模拟可见范围内的研究记录；不会返回超出 as-of 日期的证据。", "parameters": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 200}, "kind": {"type": ["string", "null"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["query", "limit"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_research_record", "description": "读取当前上下文可见的单条研究及追加修订历史。", "parameters": {"type": "object", "properties": {"record_id": {"type": "string", "maxLength": 100}}, "required": ["record_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "prepare_experiment_draft", "description": "按已实现的单参数白名单校验方案草稿；不会启动实验或揭晓评估结果。", "parameters": {"type": "object", "properties": {"plan": {"type": "object"}}, "required": ["plan"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_experiment_status", "description": "查看授权实验状态；运行中不泄漏评估收益、候选对比或中间指标。", "parameters": {"type": "object", "properties": {"experiment_id": {"type": "string", "maxLength": 100}}, "required": ["experiment_id"], "additionalProperties": False}}},
]


def research_tool_definitions() -> list[dict[str, Any]]:
    return json.loads(json.dumps(_TOOLS))


def invoke_research_tool(settings: Settings, context: Mapping[str, Any], name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one whitelisted tool with a date and source scoped context."""
    if context.get("scope") != "simulation" or not context.get("practice_id") or not context.get("revealed_date"):
        raise ResearchError("research_context_required", "研究工具必须绑定本地模拟及已揭晓日期", 409)
    conn = research_connect(settings)
    try:
        if name == "search_research_records":
            query = arguments.get("query", "")
            if not isinstance(query, str) or len(query) > 200:
                raise ResearchError("tool_arguments_invalid", "query 无效")
            limit = max(1, min(int(arguments.get("limit", 20)), 50))
            rows = conn.execute("WITH visible AS (SELECT r.record_id,r.kind,v.body_json,v.created_at,ROW_NUMBER() OVER(PARTITION BY r.record_id ORDER BY v.created_at DESC,v.rowid DESC) rn FROM research_record r JOIN research_revision v ON v.record_id=r.record_id WHERE json_extract(v.body_json,'$.evidence_as_of') IS NOT NULL AND json_extract(v.body_json,'$.evidence_as_of')<=? AND substr(replace(v.created_at,'-',''),1,8)<=? AND (json_extract(v.body_json,'$.scope')='global' OR (json_extract(v.body_json,'$.scope')='simulation' AND EXISTS(SELECT 1 FROM json_each(v.body_json,'$.source_refs') ref WHERE json_extract(ref.value,'$.owner_id')=?)))) SELECT record_id,kind,body_json,created_at FROM visible WHERE rn=1 AND body_json LIKE ? AND (? IS NULL OR kind=?) ORDER BY created_at DESC,record_id LIMIT ?", (str(context["revealed_date"]), str(context["revealed_date"]), str(context["practice_id"]), f"%{query[:200]}%", arguments.get("kind"), arguments.get("kind"), limit)).fetchall()
            records = []
            seen = set()
            for row in rows:
                if row["record_id"] in seen:
                    continue
                body = json.loads(row["body_json"])
                refs = body.get("source_refs", [])
                own = body.get("scope") == "simulation" and any(isinstance(ref, dict) and ref.get("owner_id") == context["practice_id"] for ref in refs)
                globally_dated = body.get("scope") == "global" and bool(body.get("evidence_as_of") and body["evidence_as_of"] <= context["revealed_date"])
                if not own and not globally_dated:
                    continue
                records.append({"record_id": row["record_id"], "kind": row["kind"], "body": body, "created_at": row["created_at"]})
                seen.add(row["record_id"])
            return {"records": records, "as_of": context["revealed_date"], "scope": "application_research_only"}
        if name == "read_research_record":
            rows = conn.execute("SELECT r.kind,v.body_json,v.revision_id,v.parent_revision_id,v.created_at FROM research_record r JOIN research_revision v ON v.record_id=r.record_id WHERE r.record_id=? AND json_extract(v.body_json,'$.evidence_as_of') IS NOT NULL AND json_extract(v.body_json,'$.evidence_as_of')<=? AND substr(replace(v.created_at,'-',''),1,8)<=? AND (json_extract(v.body_json,'$.scope')='global' OR (json_extract(v.body_json,'$.scope')='simulation' AND EXISTS(SELECT 1 FROM json_each(v.body_json,'$.source_refs') ref WHERE json_extract(ref.value,'$.owner_id')=?))) ORDER BY v.created_at DESC,v.rowid DESC LIMIT 100", (arguments.get("record_id"), str(context["revealed_date"]), str(context["revealed_date"]), str(context["practice_id"]))).fetchall()
            row = None
            body = None
            for candidate in rows:
                candidate_body = json.loads(candidate["body_json"])
                refs = candidate_body.get("source_refs", [])
                own = candidate_body.get("scope") == "simulation" and any(isinstance(ref, dict) and ref.get("owner_id") == context["practice_id"] for ref in refs)
                globally_dated = candidate_body.get("scope") == "global" and bool(candidate_body.get("evidence_as_of") and candidate_body["evidence_as_of"] <= context["revealed_date"])
                if own or globally_dated:
                    row, body = candidate, candidate_body
                    break
            if row is None:
                raise ResearchError("record_not_found", "研究记录不存在或当前模拟不可见", 404)
            return {"record_id": arguments["record_id"], "kind": row["kind"], "body": body, "revision_id": row["revision_id"], "parent_revision_id": row["parent_revision_id"]}
        if name == "prepare_experiment_draft":
            plan = validate_plan(dict(arguments.get("plan") or {}))
            return {"draft": plan, "starts_experiment": False, "discloses_evaluation": False}
        if name == "get_experiment_status":
            row = conn.execute("SELECT o.status,o.progress_json,o.checkpoint_json,o.usage_json,o.limits_json,o.attempt,o.generation,o.resumable,o.stop_reason,o.created_at,o.updated_at,p.plan_json FROM research_operation o JOIN research_plan p ON p.plan_id=o.plan_id WHERE o.operation_id=?", (arguments.get("experiment_id"),)).fetchone()
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
                stored = conn.execute("SELECT status_json FROM research_experiment WHERE experiment_id=? AND status='completed'", (arguments.get("experiment_id"),)).fetchone()
                if stored:
                    result["final_summary"] = json.loads(stored["status_json"]).get("final_summary")
            else:
                result["evaluation_metrics"] = None
            return result
        raise ResearchError("tool_unknown", "未授权研究工具", 400)
    finally:
        conn.close()
