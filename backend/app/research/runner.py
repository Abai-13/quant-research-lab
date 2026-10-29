"""Bounded experiment runner using the existing practice comparison engine.

Every variant runs against a disposable SQLite backup in its own experiment
directory. The source practice database is opened read-only and never advanced.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.config import Settings
from app.practice.comparison import _get_next_trade_date, advance_paths, finish_paths, get_comparison
from app.practice.core import require_session
from app.practice.runtime import practice_settings
from .exposure import assess_exposure, register_exposure
from .service import ResearchError, now
from .store import encode, research_connect


class ExperimentBlocked(ResearchError):
    def __init__(self, reason: str, detail: str):
        super().__init__(reason, detail, 409)


def _backup(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(target_path, timeout=5.0)
    try:
        source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise ExperimentBlocked("snapshot_integrity_failed", "隔离实验快照未通过 SQLite 完整性检查")
    finally:
        target.close()
        source.close()


def _summarize_variant_metrics(nav_values: list[int], fills: tuple[int, int, int],
                               restrictions: Any = None, performance_method: Any = None) -> dict[str, Any]:
    """Summarize integer-cent outputs exactly; basis points use ROUND_HALF_UP."""
    if not nav_values or int(nav_values[0]) <= 0:
        raise ValueError("initial NAV must be positive")
    nav = [int(value) for value in nav_values]
    trade_count, costs_cents, notional_cents = map(int, fills)
    ret = int(((Decimal(nav[-1]) / Decimal(nav[0]) - 1) * 10_000).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ))
    peak, drawdown = nav[0], 0
    for value in nav[1:]:
        peak = max(peak, value)
        drawdown = min(drawdown, int(((Decimal(value) / Decimal(peak) - 1) * 10_000).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )))
    turnover = int((Decimal(notional_cents) * Decimal(10_000) / Decimal(nav[0])).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ))
    return {"return_bps": ret, "drawdown_bps": drawdown, "cost_cents": costs_cents,
            "trade_count": trade_count, "turnover_bps": turnover,
            "restrictions": restrictions, "performance_method": performance_method}


def _run_variant(source_path: Path, directory: Path, practice_id: str, cadence: int,
                 dates: list[str], metric_dates: list[str], deadline: float,
                 *, finish: bool = True) -> dict[str, Any]:
    db_path = directory / "app.db"
    _backup(source_path, db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT settings_json FROM practice_session WHERE practice_id=?", (practice_id,)).fetchone()
        if row is None:
            raise ExperimentBlocked("source_session_missing", "源模拟在快照中不存在")
        settings = json.loads(row[0])
        settings["rebalance_every_n_days"] = cadence
        conn.execute("UPDATE practice_session SET settings_json=? WHERE practice_id=?", (encode(settings), practice_id))
        conn.commit()
        session = require_session(conn, practice_id)
        if dates and session["revealed_date"] >= dates[0]:
            raise ExperimentBlocked("training_not_independent", "方案日期必须晚于冻结源模拟的已揭晓日期")
        for day in dates:
            if time.monotonic() > deadline:
                raise ExperimentBlocked("wall_budget_exceeded", "实验达到运行时预算，结果保持封存")
            expected = _get_next_trade_date(conn, session)
            if expected != day:
                raise ExperimentBlocked("trading_calendar_mismatch", f"方案日期 {day} 不是冻结数据中的下一交易日 {expected}")
            advance_paths(conn, session, day)
            conn.execute("UPDATE practice_session SET revealed_date=?,updated_at=? WHERE practice_id=?", (day, now(), practice_id))
            conn.commit()
        report = finish_paths(conn, session) if finish else get_comparison(conn, session)
        conn.commit()
        path = next((item for item in report["paths"] if item["key"] == "independent"), None)
        if path is None:
            raise ExperimentBlocked("comparison_path_missing", "确定性引擎未返回独立组合路径")
        points = path["points"]
        first = metric_dates[0]
        base_points = [point for point in points if point["date"] < first]
        metric_points = [point for point in points if first <= point["date"] <= metric_dates[-1]]
        if not base_points or not metric_points or [item["date"] for item in metric_points] != metric_dates:
            raise ExperimentBlocked("valuation_points_missing", "训练/评估区间缺少可复算的每日估值点")
        nav_values = [point["nav_cents"] for point in base_points[-1:] + metric_points]
        if any(value is None for value in nav_values) or int(nav_values[0]) <= 0:
            raise ExperimentBlocked("valuation_incomplete", "训练/评估区间估值不完整")
        fills = conn.execute("SELECT COUNT(DISTINCT CASE WHEN f.filled_shares>0 THEN f.order_id END),COALESCE(SUM(f.fee_cents+f.slippage_cents),0),COALESCE(SUM(ABS(f.filled_shares*f.price_cents)),0) FROM simulation_fill f JOIN simulation_order o ON o.order_id=f.order_id WHERE o.segment_id=? AND o.trade_date BETWEEN ? AND ?", (path["segment_id"], metric_dates[0], metric_dates[-1])).fetchone()
        return _summarize_variant_metrics(
            list(map(int, nav_values)), (int(fills[0]), int(fills[1]), int(fills[2])),
            path.get("restrictions"), path.get("performance_method"),
        )
    finally:
        conn.close()


def _metric(result: dict[str, Any], metric: str) -> int | None:
    mapping = {"net_return_bps": "return_bps", "max_drawdown_bps": "drawdown_bps"}
    return result.get(mapping[metric])


def _directory_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file()) if directory.exists() else 0


def run_experiment(settings: Settings, plan_id: str, *, experiment_id: str | None = None,
                   generation: int = 1, progress_callback=None, remaining_wall_seconds: int | None = None,
                   remaining_logical_steps: int | None = None) -> dict[str, Any]:
    research = research_connect(settings)
    timestamp = now()
    experiment_id = experiment_id or "exp_" + __import__("uuid").uuid4().hex
    directory = settings.runtime_dir.resolve() / "research" / "experiments" / experiment_id
    try:
        row = research.execute("SELECT status,plan_json,plan_sha256 FROM research_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None or row[0] != "frozen":
            raise ExperimentBlocked("plan_unavailable", "只能启动已冻结的研究方案")
        plan, plan_sha = json.loads(row[1]), str(row[2])
        research.execute("BEGIN IMMEDIATE")
        if experiment_id.startswith("exp_"):
            op = research.execute("SELECT generation,status FROM research_operation WHERE operation_id=?", (experiment_id,)).fetchone()
            if op is not None and (int(op["generation"]) != generation or op["status"] != "running"):
                raise ExperimentBlocked("generation_conflict", "operation generation changed")
        active = research.execute("SELECT COUNT(*) FROM research_experiment WHERE status IN ('queued','running') AND experiment_id<>?", (experiment_id,)).fetchone()[0]
        if active:
            raise ExperimentBlocked("active_experiment_exists", "同时只允许一个活动实验")
        existing_exp = research.execute("SELECT generation FROM research_experiment WHERE experiment_id=?", (experiment_id,)).fetchone()
        if existing_exp is None:
            research.execute("INSERT INTO research_experiment(experiment_id,plan_id,status,status_json,created_at,updated_at,generation) VALUES(?,?,?,?,?,?,?)", (experiment_id, plan_id, "queued", encode({"progress": {"phase": "preflight", "completed_units": 0, "total_units": plan["budget"]["estimated_logical_steps"]}, "plan_digest": plan_sha}), timestamp, timestamp, generation))
        elif int(existing_exp[0]) != generation:
            raise ExperimentBlocked("generation_conflict", "operation generation changed")
        research.commit()
        src = plan["source_context"]
        # Persist provenance from the frozen input itself. The practice API
        # exposes context_fingerprint (not context_digest), so hashing the
        # validated source mapping is the stable, non-disclosing digest.
        from .service import digest as _digest
        source_context_digest = _digest(src)
        practice_id = src.get("practice_id")
        if not isinstance(practice_id, str) or not practice_id:
            raise ExperimentBlocked("source_session_required", "方案必须绑定已有练习模拟")
        from app.agent_tools.service import capture_context
        live_context = capture_context(settings, practice_id)
        for key in ("practice_id", "revision", "revealed_date", "data_version_id", "source_code",
                    "settings_fingerprint", "algorithm_fingerprint", "fee_fingerprint", "segment_fingerprint"):
            if src.get(key) != live_context.get(key):
                raise ExperimentBlocked("source_context_changed", "方案冻结后源模拟、数据或费用版本已变化")
        expected_instance = __import__("hashlib").sha256(str(settings.runtime_dir.resolve()).encode("utf-8")).hexdigest()
        if src.get("source_instance_id") != expected_instance:
            raise ExperimentBlocked("source_instance_mismatch", "方案绑定的本地模拟实例不匹配")
        source_path = practice_settings(settings).db_path
        if not source_path.is_file():
            raise ExperimentBlocked("practice_database_missing", "隔离练习数据库不存在")
        db_bytes = source_path.stat().st_size
        branch_count = len(plan["candidates"]) + 2
        if db_bytes * branch_count > plan["budget"]["max_bytes"]:
            raise ExperimentBlocked("storage_budget_exceeded", "实验分支数据库预计总大小超过100MB预算")
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=5.0) as src_conn:
            src_conn.row_factory = sqlite3.Row
            session = require_session(src_conn, practice_id)
            if src.get("data_version_id") != session["data_version_id"] or src.get("source_code") != session["source_code"]:
                raise ExperimentBlocked("source_context_changed", "冻结方案的数据版本与源模拟不一致")
            if session["settings"].get("hide_system_hints"):
                raise ExperimentBlocked("system_path_hidden", "系统对照路径被隐藏，不能作为训练或评估输入")
            snapshot = session["settings"].get("pool_snapshot_id")
            symbols = [str(item[0]) for item in src_conn.execute("SELECT ts_code FROM pool_member WHERE snapshot_id=? ORDER BY ts_code", (snapshot,))] if snapshot else []
        if not symbols:
            raise ExperimentBlocked("universe_unknown", "无法从冻结模拟取得规范化证券集合")
        all_dates = plan["evaluation_dates"]
        eligibility = assess_exposure(research, symbols=symbols, dates=all_dates)
        with research:
            changed = research.execute("UPDATE research_experiment SET status='running',updated_at=? WHERE experiment_id=? AND generation=? AND EXISTS(SELECT 1 FROM research_operation WHERE operation_id=? AND generation=? AND status='running')", (now(), experiment_id, generation, experiment_id, generation)).rowcount
            if changed != 1:
                raise ExperimentBlocked("generation_conflict", "operation generation changed")
            register_exposure(research, owner_type="research_experiment", owner_id=experiment_id,
                source_instance_id=src.get("source_instance_id", "local"), source_code=session["source_code"],
                data_version_id=session["data_version_id"], symbols=symbols, dates=all_dates,
                purpose="evaluation_reserved", status=eligibility["eligibility"])
            register_exposure(research, owner_type="research_experiment", owner_id=experiment_id,
                source_instance_id=src.get("source_instance_id", "local"), source_code=session["source_code"],
                data_version_id=session["data_version_id"], symbols=symbols, dates=plan["training_dates"],
                purpose="training")
        started = time.monotonic()
        default_seconds = min(plan["budget"]["wall_seconds"], remaining_wall_seconds or plan["budget"]["wall_seconds"])
        deadline = started + min(default_seconds, 300)
        previous_exp = research.execute("SELECT status_json FROM research_experiment WHERE experiment_id=?", (experiment_id,)).fetchone()
        training_results = json.loads(previous_exp[0]).get("training_results", []) if previous_exp else []
        completed_candidates = {int(item["candidate"]) for item in training_results}
        logical_steps_this_attempt = 0
        for idx, candidate in enumerate(plan["candidates"]):
            if candidate in completed_candidates:
                continue
            if remaining_logical_steps is not None and logical_steps_this_attempt + len(plan["training_dates"]) > remaining_logical_steps:
                raise ExperimentBlocked("logical_budget_exceeded", "累计逻辑步数预算已用尽")
            if progress_callback:
                prior_steps = [{"step_id": f"training:{item['candidate']}", "state": "committed"} for item in training_results]
                progress_callback({"phase": "training", "completed_units": len(training_results) * len(plan["training_dates"]), "total_units": plan["budget"]["estimated_logical_steps"]},
                                  {"sequence": len(prior_steps), "steps": prior_steps, "validation": "checkpointed"}, len(plan["training_dates"]))
            result = _run_variant(source_path, directory / f"training-{candidate}", practice_id, candidate, plan["training_dates"], plan["training_dates"], deadline, finish=False)
            logical_steps_this_attempt += len(plan["training_dates"])
            if _directory_bytes(directory) > plan["budget"]["max_bytes"]:
                raise ExperimentBlocked("storage_budget_exceeded", "实验目录超过100MB；评估结果保持封存")
            training_results.append({"candidate": candidate, **result})
            progress = {"phase": "training", "completed_units": len(training_results) * len(plan["training_dates"]), "total_units": plan["budget"]["estimated_logical_steps"]}
            changed = research.execute("UPDATE research_experiment SET status_json=?,updated_at=? WHERE experiment_id=? AND generation=? AND EXISTS(SELECT 1 FROM research_operation WHERE operation_id=? AND generation=? AND status='running')",
                (encode({"progress": progress, "plan_digest": plan_sha, "source_context_digest": source_context_digest, "training_results": training_results, "evaluation_metrics": None}), now(), experiment_id, generation, experiment_id, generation)).rowcount
            if changed != 1: raise ExperimentBlocked("generation_conflict", "operation generation changed")
            research.commit()
            if progress_callback:
                progress_callback(progress, {"sequence": len(training_results), "steps": [{"step_id": f"training:{item['candidate']}", "state": "committed"} for item in training_results], "validation": "checkpointed"}, 0)
            if time.monotonic() > deadline:
                raise ExperimentBlocked("wall_budget_exceeded", "训练阶段达到运行时预算")
        metric = plan["primary_metric"]
        constraints = plan.get("risk_constraints", {})
        eligible = [item for item in training_results if item["candidate"] != plan["baseline_value"]
                    and _metric(item, metric) is not None
                    and (constraints.get("max_drawdown_bps") is None or item["drawdown_bps"] is not None
                         and item["drawdown_bps"] >= int(constraints["max_drawdown_bps"]))]
        if not eligible:
            raise ExperimentBlocked("no_training_improvement", "训练期没有满足风险约束的改进候选；未揭晓评估结果")
        selected = sorted(eligible, key=lambda item: (int(_metric(item, metric)), -int(item["candidate"])), reverse=True)[0]["candidate"]
        # Persist the frozen choice before any evaluation value is computed.
        changed = research.execute("UPDATE research_experiment SET status_json=?,updated_at=? WHERE experiment_id=? AND generation=? AND EXISTS(SELECT 1 FROM research_operation WHERE operation_id=? AND generation=? AND status='running')",
            (encode({"progress": {"phase": "evaluation", "completed_units": len(training_results) * len(plan["training_dates"]), "total_units": plan["budget"]["estimated_logical_steps"]}, "plan_digest": plan_sha, "source_context_digest": source_context_digest, "selected_candidate": selected, "selection_rule": plan.get("selection_rule"), "training_results": training_results, "evaluation_metrics": None}), now(), experiment_id, generation, experiment_id, generation)).rowcount
        if changed != 1: raise ExperimentBlocked("generation_conflict", "operation generation changed")
        research.commit()
        if progress_callback:
            committed_steps = [{"step_id": f"training:{item['candidate']}", "state": "committed"} for item in training_results]
            committed_steps.append({"step_id": "selection", "state": "committed"})
            progress_callback({"phase": "evaluation", "completed_units": len(training_results) * len(plan["training_dates"]), "total_units": plan["budget"]["estimated_logical_steps"]},
                              {"sequence": len(committed_steps), "steps": committed_steps, "validation": "checkpointed"},
                              0)
        before = assess_exposure(research, symbols=symbols, dates=all_dates)
        if before["eligibility"] != "eligible_unseen_in_application":
            raise ExperimentBlocked("evaluation_exposure_changed", "锁定方案后评估日期已被其他来源揭晓，未运行或发布评估结果")
        training_seed = directory / f"training-{selected}" / "app.db"
        if remaining_logical_steps is not None and logical_steps_this_attempt + 2 * len(plan["evaluation_dates"]) > remaining_logical_steps:
            raise ExperimentBlocked("logical_budget_exceeded", "累计逻辑步数预算已用尽")
        if progress_callback:
            progress_callback({"phase": "evaluation", "completed_units": len(training_results) * len(plan["training_dates"]), "total_units": plan["budget"]["estimated_logical_steps"]},
                              {"sequence": len(committed_steps), "steps": committed_steps, "validation": "checkpointed"}, len(plan["evaluation_dates"]))
        baseline = _run_variant(training_seed, directory / "evaluation-baseline", practice_id, plan["baseline_value"], plan["evaluation_dates"], plan["evaluation_dates"], deadline)
        logical_steps_this_attempt += len(plan["evaluation_dates"])
        if progress_callback:
            progress_callback({"phase": "evaluation", "completed_units": len(training_results) * len(plan["training_dates"]) + len(plan["evaluation_dates"]), "total_units": plan["budget"]["estimated_logical_steps"]},
                              {"sequence": len(committed_steps) + 1, "steps": committed_steps + [{"step_id": "evaluation-baseline", "state": "committed"}], "validation": "checkpointed"}, 0)
        if progress_callback:
            progress_callback({"phase": "evaluation", "completed_units": len(training_results) * len(plan["training_dates"]) + len(plan["evaluation_dates"]), "total_units": plan["budget"]["estimated_logical_steps"]},
                              {"sequence": len(committed_steps) + 1, "steps": committed_steps + [{"step_id": "evaluation-baseline", "state": "committed"}], "validation": "checkpointed"}, len(plan["evaluation_dates"]))
        improved = _run_variant(training_seed, directory / f"evaluation-selected-{selected}", practice_id, selected, plan["evaluation_dates"], plan["evaluation_dates"], deadline)
        logical_steps_this_attempt += len(plan["evaluation_dates"])
        if progress_callback:
            progress_callback({"phase": "evaluation", "completed_units": len(training_results) * len(plan["training_dates"]) + 2 * len(plan["evaluation_dates"]), "total_units": plan["budget"]["estimated_logical_steps"]},
                              {"sequence": len(committed_steps) + 2, "steps": committed_steps + [{"step_id": "evaluation-baseline", "state": "committed"}, {"step_id": "evaluation-selected", "state": "committed"}], "validation": "checkpointed"}, 0)
        if _directory_bytes(directory) > plan["budget"]["max_bytes"]:
            raise ExperimentBlocked("storage_budget_exceeded", "实验目录超过100MB；评估结果保持封存")
        after = assess_exposure(research, symbols=symbols, dates=all_dates)
        if after["eligibility"] != "eligible_unseen_in_application":
            raise ExperimentBlocked("evaluation_exposure_changed", "评估期间出现重叠曝光；中间结果已封存")
        with research:
            for purpose in ("evaluation_reserved",):
                research.execute("UPDATE exposure_ledger SET purpose='evaluation_revealed',status='revealed' WHERE owner_type='research_experiment' AND owner_id=? AND purpose=? AND EXISTS(SELECT 1 FROM research_operation WHERE operation_id=? AND generation=? AND status='running')", (experiment_id, purpose, experiment_id, generation))
            final = {"training_candidates": training_results, "selected_candidate": selected,
                     "baseline_value": plan["baseline_value"], "evaluation": {"baseline": baseline, "selected": improved},
                     "evaluation_eligibility": eligibility, "limitations": ["未见只表示本应用未用于选参；不表示人或模型历史上没见过。"],
                     "plan_digest": plan_sha, "source_context_digest": source_context_digest}
            changed = research.execute("UPDATE research_experiment SET status='completed',status_json=?,updated_at=? WHERE experiment_id=? AND generation=? AND EXISTS(SELECT 1 FROM research_operation WHERE operation_id=? AND generation=? AND status='running')", (encode({"progress": {"phase": "completed", "completed_units": plan["budget"]["estimated_logical_steps"], "total_units": plan["budget"]["estimated_logical_steps"]}, "final_summary": final}), now(), experiment_id, generation, experiment_id, generation)).rowcount
            if changed != 1: raise ExperimentBlocked("generation_conflict", "operation generation changed")
        return {"experiment_id": experiment_id, "status": "completed", **final}
    except Exception as exc:
        existing = research.execute("SELECT status_json FROM research_experiment WHERE experiment_id=?", (experiment_id,)).fetchone()
        if not existing:
            research.execute("INSERT INTO research_experiment(experiment_id,plan_id,status,status_json,created_at,updated_at,generation) VALUES(?,?,?,?,?,?,?)", (experiment_id, plan_id, "blocked", encode({"progress": {"phase": "preflight", "completed_units": 0, "total_units": None}, "stop_reason": getattr(exc, "reason", "preflight_failed")}), timestamp, now(), generation))
        else:
            previous = json.loads(existing[0])
            training_attempts = previous.get("training_results", [])
            research.execute("UPDATE research_experiment SET status=?,status_json=?,updated_at=? WHERE experiment_id=? AND generation=? AND EXISTS(SELECT 1 FROM research_operation WHERE operation_id=? AND generation=? AND status='running')",
                ("blocked" if isinstance(exc, ResearchError) else "failed", encode({"progress": {"phase": "stopped", "completed_units": previous.get("progress", {}).get("completed_units", 0), "total_units": previous.get("progress", {}).get("total_units")}, "plan_digest": previous.get("plan_digest"), "source_context_digest": previous.get("source_context_digest"), "stop_reason": getattr(exc, "reason", "experiment_failed"), "final_summary": {"completed_training_candidates": training_attempts, "selected_candidate": previous.get("selected_candidate"), "evaluation": None}}), now(), experiment_id, generation, experiment_id, generation))
        research.commit()
        if isinstance(exc, ResearchError):
            raise
        raise ResearchError("experiment_failed", "实验停止；中间评估指标不会返回。", 409) from exc
    finally:
        research.close()
