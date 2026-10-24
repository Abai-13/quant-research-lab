"""Read-only, date-bounded tools for simulation assistance.

Every simulation connection is opened with SQLite ``mode=ro``. The module never
bootstraps a database, mutates an order, advances a session, or exposes raw
database rows to the model.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from app.config import Settings
from app.data.chunks import VersionView
from app.practice.core import PracticeError, parse_date, require_session
from app.practice.storage import decode_json
from app.portfolio.rule_bundles import load_rule_bundle
from app.portfolio.simulation import (
    _fee_schedule,
    _load_market_rules,
    _max_affordable_shares,
    _order_fee_estimate,
    get_simulation_account_state,
)


_MAX_TOOL_ARGS = 16_384
_MAX_WINDOW = 60
_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "prepare_setup_draft", "description": "整理新模拟条件为待检查草稿，不读取行情或账户且不创建模拟。", "parameters": {"type": "object", "properties": {"name": {"type": "string", "maxLength": 120}, "start_date": {"type": "string", "pattern": "^\\d{8}$"}, "end_date": {"type": "string", "pattern": "^\\d{8}$"}, "cash_cents": {"type": "integer", "minimum": 0, "maximum": 100000000000}, "positions": {"type": "array", "maxItems": 100, "items": {"type": "object", "additionalProperties": False, "required": ["ts_code", "shares"], "properties": {"ts_code": {"type": "string", "maxLength": 20}, "shares": {"type": "integer", "minimum": 1, "maximum": 1000000000}, "sellable_shares": {"type": ["integer", "null"], "minimum": 0}, "total_cost_cents": {"type": ["integer", "null"], "minimum": 0}}}}, "preferences": {"type": "string", "maxLength": 1000}}, "required": ["name", "start_date", "end_date", "cash_cents"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_simulation_state", "description": "读取绑定模拟在已揭晓日的持仓、订单和可见决策记录。", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_source_quality", "description": "读取当前冻结数据版本的来源、覆盖质量和用途限制。", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "preview_trade", "description": "按当前已揭晓价格和现有费用规则做只读试算；不提交订单，未来成交价格保持未知。", "parameters": {"type": "object", "properties": {"ts_code": {"type": "string", "maxLength": 20}, "direction": {"type": "string", "enum": ["buy", "sell"]}, "planned_shares": {"type": "integer", "minimum": 1, "maximum": 10000000}}, "required": ["ts_code", "direction", "planned_shares"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "compare_review", "description": "读取程序计算的当前模拟与可见参考路径对照；隐藏系统提示时不返回系统路径。", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "bounded_analysis", "description": "在固定的最多60个已揭晓交易日窗口内，计算当前用户模拟净收益或最大回撤。", "parameters": {"type": "object", "properties": {"metric": {"type": "string", "enum": ["net_return_bps", "max_drawdown_bps"]}, "window_trading_days": {"type": "integer", "minimum": 2, "maximum": 60}}, "required": ["metric", "window_trading_days"], "additionalProperties": False}}},
]


def tool_definitions() -> list[dict[str, Any]]:
    """Return a JSON-safe copy of the strict OpenAI function schemas."""
    return json.loads(json.dumps(_TOOLS))


def _fingerprint(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _practice_path(settings: Settings) -> Path:
    return settings.runtime_dir.resolve() / "practice" / "app.db"


def _connect(settings: Settings) -> sqlite3.Connection:
    path = _practice_path(settings)
    if not path.is_file():
        raise PracticeError("practice_database_missing", "本机尚无已存在的模拟数据库；请先通过模拟界面创建或准备模拟。", 404)
    uri = path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _session_context(conn: sqlite3.Connection, session: Mapping[str, Any]) -> dict[str, Any]:
    settings = session.get("settings") or {}
    segment_id = (session.get("segments") or {}).get("user")
    segment = conn.execute(
        "SELECT rule_bundle_id,fee_bundle_id,input_sha256 FROM simulation_segment WHERE segment_id=?",
        (segment_id,),
    ).fetchone() if segment_id else None
    if segment is None:
        raise PracticeError("simulation_missing", "模拟缺少可绑定的用户模拟段。", 409)
    rule = load_rule_bundle(conn, str(segment[0]))
    fee = load_rule_bundle(conn, str(segment[1]))
    if rule is None or fee is None:
        raise PracticeError("simulation_rules_missing", "模拟规则或费用版本不可用。", 409)
    return {
        "practice_id": str(session["practice_id"]),
        "revision": int(session["revision"]),
        "revealed_date": str(session["revealed_date"]),
        "data_version_id": str(session["data_version_id"]),
        "source_code": str(session["source_code"]),
        "status": str(session["status"]),
        "settings_fingerprint": _fingerprint(settings),
        "algorithm_version": str(rule["version"]),
        "algorithm_fingerprint": str(rule["content_sha256"]),
        "fee_version": str(fee["version"]),
        "fee_fingerprint": str(fee["content_sha256"]),
        "segment_fingerprint": str(segment[2]),
    }


def capture_context(settings: Settings, practice_id: str | None, expected_revision: int | None = None) -> dict[str, Any]:
    """Bind setup-only or existing-session context without initializing storage."""
    if practice_id is None:
        return {
            "scope": "setup", "practice_id": None, "revision": None,
            "revealed_date": None, "data_version_id": None, "source_code": None,
            "context_fingerprint": _fingerprint({"scope": "setup"}),
            "model_context": {"scope": "setup", "permissions": ["setup_draft_only"]},
            "limitations": ["尚未绑定模拟；仅可整理创建表单草稿，不读取行情、账户或历史数据。"],
        }
    if not isinstance(practice_id, str) or not practice_id.strip() or len(practice_id) > 100:
        raise PracticeError("practice_id_invalid", "practice_id 无效。")
    conn = _connect(settings)
    try:
        session = require_session(conn, practice_id)
        if expected_revision is not None and (isinstance(expected_revision, bool) or expected_revision != session["revision"]):
            raise PracticeError("revision_conflict", "模拟版本已变化，请刷新后重新开始。", 409)
        binding = _session_context(conn, session)
        fp = _fingerprint(binding)
        return {
            "scope": "simulation", **binding, "context_fingerprint": fp,
            "model_context": {
                "scope": "simulation", "as_of": binding["revealed_date"],
                "source_code": binding["source_code"], "data_version_id": binding["data_version_id"],
                "status": binding["status"], "permissions": ["read_revealed_state", "deterministic_preview", "bounded_analysis", "draft_only"],
            },
            "limitations": ["只可读取当前已揭晓数据；Agent不能推进模拟或提交订单。", "模型上下文不包含姓名、隐藏系统提示或未来行情。"],
        }
    finally:
        conn.close()


def validate_context(settings: Settings, context: Mapping[str, Any]) -> None:
    if not isinstance(context, Mapping) or context.get("scope") not in {"setup", "simulation"}:
        raise PracticeError("context_invalid", "工具上下文无效。", 409)
    if context.get("scope") == "setup":
        expected = _fingerprint({"scope": "setup"})
        if context.get("context_fingerprint") != expected or context.get("practice_id") is not None:
            raise PracticeError("context_stale", "设置草稿上下文已变化。", 409)
        return
    live = capture_context(settings, context.get("practice_id"), context.get("revision"))
    for field in ("revision", "revealed_date", "data_version_id", "source_code", "algorithm_version", "algorithm_fingerprint", "fee_version", "fee_fingerprint", "segment_fingerprint", "context_fingerprint"):
        if live.get(field) != context.get(field):
            raise PracticeError("context_stale", "模拟日期或版本已变化，请重新查询。", 409)


def _output(context: Mapping[str, Any], name: str, data: dict[str, Any], summary: str, *, drafts: list[dict[str, Any]] | None = None, limitations: list[str] | None = None, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    # Re-read the binding after all work and before returning any result.
    if conn is not None:
        _live(conn, context)
    evidence_id = "ev_" + uuid.uuid4().hex
    result = {
        "data": data,
        "evidence": [{"evidence_id": evidence_id, "tool_name": name, "as_of": context.get("revealed_date"), "version": {"practice_id": context.get("practice_id"), "revision": context.get("revision"), "data_version_id": context.get("data_version_id"), "source_code": context.get("source_code"), "algorithm_version": context.get("algorithm_version"), "algorithm_fingerprint": context.get("algorithm_fingerprint"), "fee_version": context.get("fee_version"), "fee_fingerprint": context.get("fee_fingerprint")}, "summary": summary[:500], "data": data}],
        "drafts": drafts or [], "limitations": list(context.get("limitations") or []) + (limitations or []),
        "as_of": context.get("revealed_date"), "context_fingerprint": str(context["context_fingerprint"]),
    }
    if len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")) > 64_000:
        raise PracticeError("tool_output_too_large", "工具结果超出输出大小限制。", 413)
    return result


def _require_keys(arguments: Mapping[str, Any], allowed: set[str], required: set[str]) -> None:
    if not isinstance(arguments, Mapping) or set(arguments) - allowed or required - set(arguments):
        raise PracticeError("tool_arguments_invalid", "工具参数缺失或包含未支持字段。")
    encoded = json.dumps(arguments, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > _MAX_TOOL_ARGS:
        raise PracticeError("tool_arguments_too_large", "工具参数超出大小限制。", 413)


def _validate_setup(arguments: Mapping[str, Any]) -> None:
    name = arguments["name"]
    cash = arguments["cash_cents"]
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise PracticeError("tool_arguments_invalid", "模拟名称必须为1至120个字符。")
    if isinstance(cash, bool) or not isinstance(cash, int) or not 0 <= cash <= 100_000_000_000:
        raise PracticeError("tool_arguments_invalid", "本金必须为有界非负整数分。")
    start = parse_date(arguments["start_date"], "start_date")
    end = parse_date(arguments["end_date"], "end_date")
    if end < start:
        raise PracticeError("tool_arguments_invalid", "模拟结束日不能早于开始日。")
    positions = arguments.get("positions", [])
    if not isinstance(positions, list) or len(positions) > 100:
        raise PracticeError("tool_arguments_invalid", "初始持仓最多100项。")
    for position in positions:
        allowed = {"ts_code", "shares", "sellable_shares", "total_cost_cents"}
        if not isinstance(position, dict) or set(position) - allowed or not {"ts_code", "shares"} <= set(position):
            raise PracticeError("tool_arguments_invalid", "初始持仓字段无效。")
        if not isinstance(position["ts_code"], str) or not position["ts_code"] or len(position["ts_code"]) > 20:
            raise PracticeError("tool_arguments_invalid", "初始持仓证券代码无效。")
        shares = position["shares"]
        if isinstance(shares, bool) or not isinstance(shares, int) or not 1 <= shares <= 1_000_000_000:
            raise PracticeError("tool_arguments_invalid", "初始持仓数量无效。")
        for field in ("sellable_shares", "total_cost_cents"):
            value = position.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise PracticeError("tool_arguments_invalid", f"{field} 必须为非负整数或 null。")
        if position.get("sellable_shares") is not None and position["sellable_shares"] > shares:
            raise PracticeError("tool_arguments_invalid", "可卖数量不能超过持仓数量。")
    preferences = arguments.get("preferences", "")
    if not isinstance(preferences, str) or len(preferences) > 1000:
        raise PracticeError("tool_arguments_invalid", "偏好说明最多1000个字符。")


def _live(conn: sqlite3.Connection, context: Mapping[str, Any]) -> dict[str, Any]:
    session = require_session(conn, str(context["practice_id"]))
    live_binding = _session_context(conn, session)
    if live_binding != {key: context[key] for key in live_binding}:
        raise PracticeError("context_stale", "模拟日期或版本已变化，请重新查询。", 409)
    return session


def _get_state(session: Mapping[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
    segments = session.get("segments") or {}
    segment_id = segments.get("user")
    if not segment_id:
        raise PracticeError("simulation_missing", "模拟缺少用户模拟段。", 409)
    state = get_simulation_account_state(conn, str(segment_id))
    orders = conn.execute("SELECT trade_date,ts_code,direction,planned_shares,filled_shares,status,detail_json FROM practice_order WHERE practice_id=? ORDER BY created_at,order_id LIMIT 200", (session["practice_id"],)).fetchall()
    decisions = conn.execute("SELECT decision_date,recorded_at,judgment,evidence,exit_condition,is_backfill FROM practice_decision WHERE practice_id=? AND decision_date<=? ORDER BY decision_date,recorded_at LIMIT 200", (session["practice_id"], session["revealed_date"])).fetchall()
    return {
        "status": session["status"], "revealed_date": session["revealed_date"],
        "cash_cents": int(state["cash_cents"]),
        "positions": [{k: p.get(k) for k in ("ts_code", "shares", "total_cost_cents", "sellable_shares", "locked", "price_status")} for p in state.get("positions", []) if int(p.get("shares") or 0) > 0][:200],
        "orders": [{"trade_date": str(r[0]), "ts_code": str(r[1]), "direction": str(r[2]), "planned_shares": int(r[3]), "filled_shares": int(r[4]), "status": str(r[5])} for r in orders],
        "decisions": [{"decision_date": str(r[0]), "judgment": str(r[2])[:1000], "evidence": str(r[3])[:1000], "exit_condition": str(r[4])[:500], "is_backfill": bool(r[5])} for r in decisions],
    }


def _source_quality(conn: sqlite3.Connection, session: Mapping[str, Any]) -> dict[str, Any]:
    row = conn.execute("SELECT source_code,status,categories,rule_version,quality_summary FROM data_version WHERE version_id=?", (session["data_version_id"],)).fetchone()
    if row is None:
        raise PracticeError("data_version_missing", "冻结数据版本不存在。", 409)
    try:
        quality = decode_json(str(row[4]))
        limitations = quality.get("limitations", []) if isinstance(quality, dict) else []
    except (TypeError, ValueError):
        quality, limitations = {}, ["来源质量摘要无法解析。"]
    view = VersionView(conn, str(session["data_version_id"]), expected_source_code=str(session["source_code"]))
    days = [d for d in view.calendar_days() if d <= str(session["revealed_date"])]
    return {"source_code": str(row[0]), "data_version_id": str(session["data_version_id"]), "status": str(row[1]), "categories": decode_json(str(row[2])), "rule_version": str(row[3]), "quality": quality, "coverage": {"revealed_trade_days": len(days), "first_revealed_day": days[0] if days else None, "last_revealed_day": days[-1] if days else None}, "synthetic": str(row[0]) in {"synthetic", "tushare-fixture"}, "limitations": [str(v)[:300] for v in limitations[:30]] if isinstance(limitations, list) else []}


def execute_tool(settings: Settings, context: Mapping[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one allowlisted, bounded deterministic tool."""
    if name not in {"prepare_setup_draft", "get_simulation_state", "get_source_quality", "preview_trade", "compare_review", "bounded_analysis"}:
        raise PracticeError("tool_unknown", "未知工具。")
    if not isinstance(arguments, dict):
        raise PracticeError("tool_arguments_invalid", "工具参数必须为 JSON 对象。")
    validate_context(settings, context)
    if name == "prepare_setup_draft":
        if context.get("scope") != "setup":
            raise PracticeError("setup_scope_required", "设置草稿仅允许在未绑定模拟时生成。")
        allowed = {"name", "start_date", "end_date", "cash_cents", "positions", "preferences"}
        required = {"name", "start_date", "end_date", "cash_cents"}
        _require_keys(arguments, allowed, required)
        _validate_setup(arguments)
        draft_id = "dr_" + uuid.uuid4().hex
        payload = {k: arguments[k] for k in sorted(arguments)}
        draft = {"draft_id": draft_id, "kind": "setup", "practice_id": None, "expected_revision": None, "revealed_date": None, "context_fingerprint": context["context_fingerprint"], "payload": payload, "limitations": ["仅为新建表单草稿；需用户检查并通过既有模拟创建界面提交。"]}
        return _output(context, name, {"fields": payload}, "已整理模拟条件草稿，未读取行情或账户。", drafts=[draft])

    if context.get("scope") != "simulation":
        raise PracticeError("simulation_scope_required", "该工具需要绑定现有模拟。")
    conn = _connect(settings)
    try:
        session = _live(conn, context)
        if name == "get_simulation_state":
            _require_keys(arguments, set(), set())
            data = _get_state(session, conn)
            return _output(context, name, data, "已读取当前已揭晓日的用户持仓、订单和决策记录。", conn=conn)
        if name == "get_source_quality":
            _require_keys(arguments, set(), set())
            data = _source_quality(conn, session)
            return _output(context, name, data, "来源和覆盖质量来自绑定的数据版本记录。", limitations=data["limitations"], conn=conn)
        if name == "preview_trade":
            _require_keys(arguments, {"ts_code", "direction", "planned_shares"}, {"ts_code", "direction", "planned_shares"})
            code, side, shares = arguments["ts_code"], arguments["direction"], arguments["planned_shares"]
            if not isinstance(code, str) or len(code) > 20 or side not in {"buy", "sell"} or isinstance(shares, bool) or not isinstance(shares, int) or not 1 <= shares <= 10_000_000:
                raise PracticeError("tool_arguments_invalid", "证券、方向或数量无效。")
            market = VersionView(conn, str(session["data_version_id"]), expected_source_code=str(session["source_code"]))
            if code not in {str(item["ts_code"]) for item in market.securities()}:
                raise PracticeError("security_not_in_version", "证券不属于模拟冻结的数据版本。", 422)
            daily = market.latest_daily(code, on_or_before=str(session["revealed_date"]))
            close = daily["close"] if daily is not None else None
            price_cents = int(round(float(close) * 100)) if close is not None else None
            segment = conn.execute("SELECT fee_bundle_id,rule_bundle_id FROM simulation_segment WHERE segment_id=?", (session["segments"].get("user"),)).fetchone()
            bundle = load_rule_bundle(conn, str(segment[0])) if segment else None
            market_rules = _load_market_rules(conn, str(segment[1])) if segment else None
            schedule = _fee_schedule(bundle) if bundle else {}
            fee = _order_fee_estimate(schedule, side, price_cents * shares) if schedule and price_cents is not None else None
            state = _get_state(session, conn)
            position = next((p for p in state["positions"] if p["ts_code"] == code), None)
            next_day = next((d for d in market.calendar_days() if d > str(session["revealed_date"]) and d <= str(session["end_date"])), None)
            board_lot = market_rules.get("board_lot_shares") if market_rules else None
            valid_board_lot = isinstance(board_lot, int) and not isinstance(board_lot, bool) and board_lot > 0
            lot_aligned = bool(valid_board_lot and shares % board_lot == 0)
            cash_sufficient: bool | None = None
            if side == "buy" and price_cents is not None and schedule and valid_board_lot:
                cash_sufficient = _max_affordable_shares(
                    schedule,
                    price_cents=price_cents,
                    board_lot=int(board_lot),
                    planned_shares=shares,
                    cash_budget=int(state["cash_cents"]),
                    apply_minimum=True,
                ) >= shares
            elif side == "sell" and fee is not None and price_cents is not None:
                cash_sufficient = int(state["cash_cents"]) + price_cents * shares - int(fee["fee_cents"]) - int(fee["slippage_cents"]) >= 0
            checks: dict[str, bool | None] = {
                "known_price_available": price_cents is not None and price_cents > 0,
                "market_rules_available": market_rules is not None,
                "board_lot_aligned": lot_aligned if valid_board_lot else None,
                "fee_schedule_available": bool(schedule),
                "cash_sufficient_at_known_price": cash_sufficient,
                "execution_date_available": next_day is not None,
            }
            if side == "sell" and (position is None or position.get("locked") or position.get("sellable_shares") is None or shares > int(position["sellable_shares"])):
                checks["sellable_shares_available"] = False
            elif side == "sell":
                checks["sellable_shares_available"] = True
            reason = next((key for key, value in checks.items() if value is False), None)
            unknown = [key for key, value in checks.items() if value is None]
            known_pass = reason is None and not unknown
            data = {"ts_code": code, "direction": side, "planned_shares": shares, "as_of": session["revealed_date"], "known_price_cents": price_cents, "estimated_notional_cents": price_cents * shares if price_cents is not None else None, "fee_estimate": fee, "fee_version": bundle.get("version") if bundle else None, "fee_assumption_scope": bundle.get("assumption_scope") if bundle else None, "board_lot_shares": board_lot if valid_board_lot else None, "known_checks": checks, "known_constraints_pass": known_pass, "execution_date": next_day, "future_execution_price_cents": None, "future_execution_status": "unknown", "restriction": reason, "unknown_checks": unknown, "writes_order": False, "limitations": ["未来成交价格、停牌、涨跌停和开盘成交条件尚未知；已通过的已知条件不保证将来成交。"]}
            draft = {"draft_id": "dr_" + uuid.uuid4().hex, "kind": "trade", "practice_id": context["practice_id"], "expected_revision": context["revision"], "revealed_date": context["revealed_date"], "context_fingerprint": context["context_fingerprint"], "payload": {"ts_code": code, "direction": side, "planned_shares": shares}, "limitations": ["草稿不会下单；用户确认后必须由既有订单 API 重新核验版本、日期、规则与幂等键。", "成交日价格未知，费用是按已揭晓价格做的模拟估算。"]}
            return _output(context, name, data, "使用冻结版本的已揭晓收盘价、费用规则和买入整手规则完成只读试算；未写入订单。", drafts=[draft], limitations=draft["limitations"] + data["limitations"], conn=conn)
        if name == "compare_review":
            _require_keys(arguments, set(), set())
            from app.practice.comparison import get_comparison
            comparison = get_comparison(conn, session)
            hide = bool((session.get("settings") or {}).get("hide_system_hints"))
            paths = comparison.get("paths", [])
            if hide:
                paths = [p for p in paths if p.get("key") == "user"]
            data = {"as_of": session["revealed_date"], "paths": [{k: p.get(k) for k in ("key", "label", "nav_cents", "return_bps", "drawdown_bps", "cost_cents", "trade_count", "turnover_bps", "concentration_bps", "restrictions", "performance_method")} for p in paths], "contributions": [] if hide else comparison.get("contributions", []), "limitations": comparison.get("limitations", []) + (["hide_system_hints is enabled; system comparison results are withheld."] if hide else [])}
            return _output(context, name, data, "对照指标由现有确定性比较引擎计算，统计截止于当前已揭晓日。", limitations=data["limitations"], conn=conn)
        if name == "bounded_analysis":
            _require_keys(arguments, {"metric", "window_trading_days"}, {"metric", "window_trading_days"})
            metric, window = arguments["metric"], arguments["window_trading_days"]
            if metric not in {"net_return_bps", "max_drawdown_bps"} or isinstance(window, bool) or not isinstance(window, int) or not 2 <= window <= _MAX_WINDOW:
                raise PracticeError("tool_arguments_invalid", "有限分析参数超出允许范围。")
            segment = str(session["segments"].get("user") or "")
            rows = conn.execute("SELECT as_of,nav_cents FROM valuation_snapshot WHERE owner_type='simulation_segment' AND owner_id=? AND as_of<=? ORDER BY as_of DESC LIMIT ?", (segment, session["revealed_date"], window)).fetchall()
            points = list(reversed(rows))
            navs = [int(r[1]) if r[1] is not None else None for r in points]
            if len(navs) < 2 or any(n is None for n in navs) or navs[0] <= 0:
                value = None
                limitation = "窗口中的净值不完整，无法计算该指标。"
            elif metric == "net_return_bps":
                value = int(((Decimal(navs[-1]) / Decimal(navs[0]) - 1) * Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                limitation = None
            else:
                peak = navs[0]
                value = 0
                for nav in navs:
                    peak = max(peak, nav)
                    drawdown_bps = int(((Decimal(nav) / Decimal(peak) - 1) * Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    value = min(value, drawdown_bps)
                limitation = None
            refs = [str(r[0]) for r in points]
            data = {"metric": metric, "value_bps": value, "window_trading_days": len(points), "window_start": refs[0] if refs else None, "window_end": refs[-1] if refs else None, "calculation": "净收益=(期末净值/期初净值-1)*10000；最大回撤=max(期内净值/此前峰值-1)*10000", "limitations": [limitation] if limitation else []}
            return _output(context, name, data, f"已从固定已揭晓窗口计算 {metric}，共 {len(points)} 个交易日。", limitations=data["limitations"], conn=conn)
        raise PracticeError("tool_unknown", "未知工具。")
    finally:
        conn.close()
