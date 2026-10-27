"""Validation and immutable write operations for research records and plans."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .store import encode

PARAMETER = "rebalance_every_n_days"
ALLOWED_CANDIDATES = {5, 10, 20}


class ResearchError(ValueError):
    def __init__(self, reason: str, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.reason, self.detail, self.status_code = reason, detail, status_code


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def idempotent_replay(conn: sqlite3.Connection, operation: str, body: dict[str, Any]) -> dict[str, Any] | None:
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not 1 <= len(key.strip()) <= 120:
        raise ResearchError("idempotency_key_required", "POST 请求必须提供 1..120 字符 idempotency_key", 400)
    request_hash = digest(body)
    row = conn.execute("SELECT request_sha256,response_json FROM research_idempotency WHERE operation=? AND idempotency_key=?", (operation, key)).fetchone()
    if row is None:
        return None
    if str(row[0]) != request_hash:
        raise ResearchError("idempotency_conflict", "该 idempotency_key 已被不同请求内容使用", 409)
    return json.loads(str(row[1]))


def reserve_idempotency(conn: sqlite3.Connection, operation: str, body: dict[str, Any]) -> dict[str, Any] | None:
    replay = idempotent_replay(conn, operation, body)
    if replay is not None:
        return replay
    with conn:
        conn.execute("INSERT INTO research_idempotency VALUES(?,?,?,?,?)", (operation, body["idempotency_key"], digest(body), encode({"ok": True, "pending": True}), now()))
    return None


def save_idempotent_response(
    conn: sqlite3.Connection,
    operation: str,
    body: dict[str, Any],
    response: dict[str, Any],
    *,
    commit: bool = True,
) -> None:
    def save() -> None:
        conn.execute("INSERT INTO research_idempotency VALUES(?,?,?,?,?) ON CONFLICT(operation,idempotency_key) DO UPDATE SET request_sha256=excluded.request_sha256,response_json=excluded.response_json,created_at=excluded.created_at", (operation, body["idempotency_key"], digest(body), encode(response), now()))

    if commit:
        with conn:
            save()
    else:
        save()


def create_record(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    required = ("kind", "goal", "hypothesis", "disconfirming_condition")
    if any(not isinstance(body.get(k), str) or not body[k].strip() for k in required):
        raise ResearchError("record_fields_required", "kind、goal、hypothesis 与 disconfirming_condition 均为必填文本")
    if len(encode(body)) > 64_000:
        raise ResearchError("record_too_large", "研究记录不得超过 64 KB")
    record_id, revision_id, timestamp = "rr_" + uuid.uuid4().hex, "rev_" + uuid.uuid4().hex, now()
    with conn:
        conn.execute("INSERT INTO research_record VALUES(?,?,?,?)", (record_id, body["kind"], timestamp, revision_id))
        conn.execute("INSERT INTO research_revision VALUES(?,?,?,?,?,?)", (revision_id, record_id, None, encode(body), digest(body), timestamp))
    return {"record_id": record_id, "revision_id": revision_id, "parent_revision_id": None, **body, "created_at": timestamp}


def revise_record(conn: sqlite3.Connection, record_id: str, body: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute("SELECT current_revision_id FROM research_record WHERE record_id=?", (record_id,)).fetchone()
    if row is None:
        raise ResearchError("record_not_found", "研究记录不存在", 404)
    parent = str(row[0])
    revision_id, timestamp = "rev_" + uuid.uuid4().hex, now()
    with conn:
        conn.execute("INSERT INTO research_revision VALUES(?,?,?,?,?,?)", (revision_id, record_id, parent, encode(body), digest(body), timestamp))
        conn.execute("UPDATE research_record SET current_revision_id=? WHERE record_id=?", (revision_id, record_id))
    return {"record_id": record_id, "revision_id": revision_id, "parent_revision_id": parent, **body, "created_at": timestamp}


def validate_plan(body: dict[str, Any]) -> dict[str, Any]:
    candidates = body.get("candidates")
    if body.get("parameter_name") != PARAMETER:
        raise ResearchError("parameter_not_supported", f"首版仅支持 {PARAMETER}")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 3 or any(type(v) is not int or v not in ALLOWED_CANDIDATES for v in candidates) or len(set(candidates)) != len(candidates):
        raise ResearchError("candidate_invalid", "候选必须是互不重复的 rebalance_every_n_days 5/10/20，最多三项")
    if type(body.get("baseline_value")) is not int or body["baseline_value"] not in ALLOWED_CANDIDATES:
        raise ResearchError("baseline_invalid", "基线必须是已支持的 5/10/20 之一")
    if body["baseline_value"] in candidates:
        raise ResearchError("candidate_is_baseline", "候选改进必须不同于冻结基线")
    training, evaluation = body.get("training_dates"), body.get("evaluation_dates")
    if not isinstance(training, list) or not 1 <= len(training) <= 252 or not isinstance(evaluation, list) or not 1 <= len(evaluation) <= 252:
        raise ResearchError("date_budget_invalid", "训练最多252个交易日，评估最多252个交易日")
    for label, dates in (("training", training), ("evaluation", evaluation)):
        if any(not isinstance(d, str) or not re.fullmatch(r"\d{8}", d) for d in dates) or dates != sorted(set(dates)):
            raise ResearchError("date_sequence_invalid", f"{label} 日期必须为升序、唯一的 YYYYMMDD 列表")
    if training[-1] >= evaluation[0]:
        raise ResearchError("interval_overlap", "训练区间必须早于评估区间")
    warmup = body.get("warmup_dates", [])
    if not isinstance(warmup, list) or len(warmup) > 60 or (warmup and (not all(isinstance(d, str) and re.fullmatch(r"\d{8}", d) for d in warmup) or warmup != sorted(set(warmup)))):
        raise ResearchError("warmup_invalid", "预热日期必须升序唯一且最多60个")
    if warmup and warmup[-1] >= training[0]:
        raise ResearchError("warmup_overlap", "预热日期必须早于训练区间")
    if body.get("primary_metric") not in {"net_return_bps", "max_drawdown_bps"}:
        raise ResearchError("metric_invalid", "primary_metric 不受支持")
    budget = body.get("budget") or {}
    if not isinstance(budget, Mapping):
        raise ResearchError("budget_invalid", "budget 必须是对象")
    steps = len(candidates) * len(training) + 2 * len(evaluation) + len(warmup) * (len(candidates) + 2)
    seconds = budget.get("wall_seconds", 180)
    bytes_limit = budget.get("max_bytes", 100 * 1024 * 1024)
    if steps > 1260 or type(seconds) is not int or not 1 <= seconds <= 300 or type(bytes_limit) is not int or not 1 <= bytes_limit <= 100 * 1024 * 1024:
        raise ResearchError("budget_exceeded", "逻辑步数上限1260、运行时间上限300秒、实验域上限100MB")
    source = body.get("source_context")
    required_source = {"practice_id", "revision", "revealed_date", "data_version_id", "source_code",
                       "source_instance_id", "settings_fingerprint", "algorithm_fingerprint",
                       "fee_fingerprint", "segment_fingerprint"}
    if body.get("engine_id") != "practice_deterministic_v1" or not isinstance(source, Mapping) or not required_source.issubset(source):
        raise ResearchError("source_context_required", "必须绑定已验证的练习确定性引擎与数据/版本上下文")
    if not isinstance(source.get("revision"), int) or not isinstance(source.get("revealed_date"), str) or training[0] <= source["revealed_date"]:
        raise ResearchError("source_context_invalid", "训练日期必须晚于冻结源模拟揭晓日，并绑定有效revision")
    constraints = body.get("risk_constraints", {})
    if not isinstance(constraints, Mapping):
        raise ResearchError("risk_constraints_invalid", "risk_constraints 必须是对象")
    max_drawdown = constraints.get("max_drawdown_bps")
    if max_drawdown is not None and (type(max_drawdown) is not int or not -10_000 <= max_drawdown <= 0):
        raise ResearchError("risk_constraints_invalid", "max_drawdown_bps 必须为 -10000..0 的整数基点")
    if body.get("selection_rule") not in {"highest_net_return_bps", "least_negative_drawdown_bps"}:
        raise ResearchError("selection_rule_invalid", "selection_rule 不受支持")
    if (body["primary_metric"], body["selection_rule"]) not in {
        ("net_return_bps", "highest_net_return_bps"), ("max_drawdown_bps", "least_negative_drawdown_bps")
    }:
        raise ResearchError("selection_rule_invalid", "选择规则必须与主指标一致")
    return {**body, "candidates": candidates, "training_dates": training, "evaluation_dates": evaluation,
            "warmup_dates": warmup, "budget": {"wall_seconds": seconds, "hard_wall_seconds": 300,
            "max_bytes": bytes_limit, "max_logical_steps": 1260, "estimated_logical_steps": steps},
            "limitations": ["未见仅表示本应用尚未将这些评估曝光用于选参；不表示人或模型从未见过行情。"]}


def create_plan(conn: sqlite3.Connection, body: dict[str, Any], *, commit: bool = True) -> dict[str, Any]:
    plan = validate_plan(body)
    plan_id, timestamp = "plan_" + uuid.uuid4().hex, now()
    plan_hash = digest(plan)
    def insert() -> None:
        conn.execute("INSERT INTO research_plan VALUES(?,?,?,?,?,?)", (plan_id, 1, "frozen", encode(plan), plan_hash, timestamp))

    if commit:
        with conn:
            insert()
    else:
        insert()
    return {"plan_id": plan_id, "plan_revision": 1, "status": "frozen", **plan, "plan_digest": plan_hash, "created_at": timestamp}


def maintenance_reference_snapshot(settings) -> dict[str, Any]:
    """C maintenance adapter; kept here as the service-domain entry point."""
    from .store import maintenance_reference_snapshot as read_snapshot
    return read_snapshot(settings)
