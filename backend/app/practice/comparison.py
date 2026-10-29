"""阶段四练习对照路径：将阶段三冻结的模拟段作为唯一账本引擎。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from ..data.chunks import VersionView
from ..data.errors import DataError
from ..data.prices import valuation_price
from ..portfolio.performance import compute_performance_point, performance_summary
from ..portfolio.rule_bundles import load_rule_bundle
from ..portfolio.service import DataSelectionContext, bind_data_context
from ..portfolio.simulation import (
    advance_simulation_segment,
    close_simulation_segment,
    create_simulation_segment,
    get_simulation_account_state,
    place_simulation_order,
)
from ..portfolio.targeting import compute_ideal_target

__all__ = ["initialize_paths", "advance_paths", "get_comparison", "finish_paths"]

PATHS = {
    "user": "用户",
    "incremental": "增量调仓",
    "independent": "独立组合",
    "hold": "持有",
}
def _settings(session: Mapping[str, Any]) -> Mapping[str, Any]:
    value = session.get("settings")
    return value if isinstance(value, Mapping) else {}


def _required(session: Mapping[str, Any], key: str) -> str:
    value = session.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DataError("practice_comparison_session_invalid", f"session 缺少必需字段 {key}")
    return value.strip()


def _get_next_trade_date(conn, session: Mapping[str, Any]) -> str | None:
    view = VersionView(conn, _required(session, "data_version_id"), expected_source_code=_required(session, "source_code"))
    days = view.calendar_days(on_or_before=str(session.get("end_date") or "99991231"))
    revealed = _required(session, "revealed_date")
    return next((d for d in days if d > revealed), None)


def _account_for_snapshot(conn, snapshot_id: str) -> tuple[str, int]:
    row = conn.execute(
        "SELECT account_id, account_revision FROM account_snapshot WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise DataError("practice_comparison_seed_missing", f"seed snapshot {snapshot_id} 不存在")
    return str(row[0]), int(row[1])


def initialize_paths(conn, session: dict[str, Any]) -> dict[str, str]:
    """创建四个互相独立、同源快照与费用/规则配置的模拟段。"""
    seed_id = _required(session, "seed_snapshot_id")
    source_code = _required(session, "source_code")
    version_id = _required(session, "data_version_id")
    settings = _settings(session)
    rule_id = settings.get("simulation_rule_bundle_id") or settings.get("rule_bundle_id")
    fee_id = settings.get("fee_bundle_id")
    if not isinstance(rule_id, str) or not rule_id.strip() or not isinstance(fee_id, str) or not fee_id.strip():
        raise DataError(
            "practice_comparison_bundles_missing",
            "练习必须显式提供 simulation_rule_bundle_id/rule_bundle_id 与 fee_bundle_id；缺证据不创建系统模拟段",
        )
    source_account_id, _ = _account_for_snapshot(conn, seed_id)
    start_date = _required(session, "start_date")
    practice_id = _required(session, "practice_id")
    effective_settings = dict(settings)
    effective_settings.setdefault("rebalance_every_n_days", 5)
    if "constraints" not in effective_settings:
        parameter_id = effective_settings.get("parameter_bundle_id")
        parameter = load_rule_bundle(conn, parameter_id) if isinstance(parameter_id, str) else None
        content = parameter.get("content") if parameter else None
        versioned_constraints = content.get("target_constraints") if isinstance(content, Mapping) else None
        if isinstance(versioned_constraints, Mapping):
            effective_settings["constraints"] = dict(versioned_constraints)
            effective_settings["constraints_source"] = parameter_id
        else:
            effective_settings["constraints"] = {
                "cash_reserve_bp": 500,
                "per_security_weight_cap_bp": 1_000,
                "assumption": "engineering_default_unverified",
            }
            effective_settings["constraints_source"] = "engineering_default_unverified"
    session["settings"] = effective_settings
    result: dict[str, str] = {}
    for key in PATHS:
        segment = create_simulation_segment(
            conn,
            source_account_id=source_account_id,
            base_account_snapshot_id=seed_id,
            rule_bundle_id=rule_id,
            fee_bundle_id=fee_id,
            source_code=source_code,
            data_version_id=version_id,
            as_of=start_date,
            simulation_run_key=f"practice:{practice_id}:{key}",
        )
        result[key] = str(segment["segment_id"])
    session["segments"] = result
    # Start-date state is the first performance point; it is common to all paths.
    for key, segment_id in result.items():
        compute_performance_point(
            conn,
            owner_type="simulation_segment",
            owner_id=segment_id,
            source_code=source_code,
            data_version_id=version_id,
            as_of=start_date,
        )
    return result


def _next_rebalance_is_due(conn, session: Mapping[str, Any], segment_id: str) -> bool:
    settings = _settings(session)
    cadence = settings.get("rebalance_every_n_days", 5)
    if isinstance(cadence, bool) or not isinstance(cadence, int) or cadence < 1:
        cadence = 5
    count = conn.execute(
        "SELECT COUNT(DISTINCT as_of) FROM valuation_snapshot WHERE owner_type='simulation_segment' AND owner_id=? AND as_of>=?",
        (segment_id, _required(session, "start_date")),
    ).fetchone()[0]
    # First schedule is from the start-date close; then every N revealed sessions.
    return int(count) == 1 or (int(count) - 1) % cadence == 0


def _create_system_orders(conn, session: Mapping[str, Any], key: str, segment_id: str, trade_date: str) -> list[str]:
    """Use the S3 deterministic target only when a matching, qualified daily pool exists."""
    if key == "hold":
        return []
    settings = _settings(session)
    pool_id = settings.get("pool_snapshot_id")
    parameter_id = settings.get("parameter_bundle_id")
    if not isinstance(pool_id, str) or not pool_id or not isinstance(parameter_id, str) or not parameter_id:
        return ["system_rebalance_blocked_missing_versioned_pool_or_parameters"]
    state = get_simulation_account_state(conn, segment_id)
    context = DataSelectionContext(
        source_code=_required(session, "source_code"),
        as_of=_required(session, "revealed_date"),
        purpose="signal_input",
        data_version_id=_required(session, "data_version_id"),
        account_revision=int(state["revision"]),
        rule_bundle_id=parameter_id,
        pool_snapshot_id=pool_id,
    )
    try:
        bound = bind_data_context(conn, context)
    except DataError as exc:
        # A stale or unqualified pool must constrain the path, never turn into
        # permission to reuse a future/unverified universe.
        return [f"system_rebalance_blocked:{exc.reason}"]
    if bound.restricted:
        return [str(bound.restriction_code or "system_rebalance_context_restricted")]
    try:
        report = compute_ideal_target(conn, bound, account_state=state)
    except DataError as exc:
        return [f"system_rebalance_blocked:{exc.reason}"]
    if report.get("restricted"):
        return [str(report.get("restriction_code") or "system_rebalance_target_restricted")]
    allocation = report.get("allocation") or {}
    targets = allocation.get("targets") or []
    if not allocation.get("ok") or not targets:
        return [str(allocation.get("reason_code") or "system_rebalance_no_valid_targets")]
    restrictions: list[str] = []
    nav_value = allocation.get("investable_cents")
    if isinstance(nav_value, bool) or not isinstance(nav_value, int) or nav_value <= 0:
        return ["system_rebalance_investable_value_unknown"]
    # Incremental follows the selected set while independent resets to the explicit S3 target.
    selected = {str(item["ts_code"]): int(item["target_value_cents"]) for item in targets}
    positions = {str(p["ts_code"]): p for p in state.get("positions", [])}
    codes = set(selected) | (set(positions) if key == "independent" else set())
    market = VersionView(conn, _required(session, "data_version_id"), expected_source_code=_required(session, "source_code"))
    for code in sorted(codes):
        pos = positions.get(code)
        current_shares = int((pos or {}).get("shares") or 0)
        price = valuation_price(
            conn,
            source_code=_required(session, "source_code"),
            ts_code=code,
            business_date=_required(session, "revealed_date"),
            as_of=_required(session, "revealed_date"),
            view=market,
        )
        price_value = price.price
        if price_value is None or Decimal(str(price_value)) <= 0:
            if current_shares or selected.get(code, 0):
                restrictions.append(f"price_unknown:{code}")
            continue
        price_cents = int((Decimal(str(price_value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        target_cents = selected.get(code, 0)
        target_shares = (target_cents // price_cents // 100) * 100
        delta = target_shares - current_shares
        if not delta:
            continue
        direction = "buy" if delta > 0 else "sell"
        if direction == "sell":
            sellable = (pos or {}).get("sellable_shares")
            if (pos or {}).get("locked") or sellable is None or int(sellable) < abs(delta):
                restrictions.append(f"sell_restricted:{code}")
                continue
        place_simulation_order(
            conn,
            segment_id,
            ts_code=code,
            direction=direction,
            planned_shares=abs(delta),
            trade_date=trade_date,
            idempotency_key=f"practice:{session['practice_id']}:{key}:{session['revealed_date']}:{trade_date}:{code}:{direction}",
        )
    return restrictions


def _practice_orders_due(conn, session: Mapping[str, Any], trade_date: str) -> list[dict[str, Any]]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='practice_order'"
    ).fetchone()
    if table is None:
        raise DataError("practice_order_table_missing", "core practice_order 表尚未初始化")
    return [
        dict(row)
        for row in conn.execute(
            "SELECT order_id, idempotency_key, trade_date, ts_code, direction, planned_shares,"
            " filled_shares, status, detail_json FROM practice_order"
            " WHERE practice_id=? AND trade_date=? AND status IN ('pending','partially_filled')"
            " ORDER BY trade_date, created_at, order_id",
            (_required(session, "practice_id"), trade_date),
        ).fetchall()
    ]


def _register_practice_orders(conn, session: Mapping[str, Any], segment_id: str, trade_date: str) -> list[str]:
    """Translate core-owned accepted orders into the isolated user simulation segment."""
    result: list[str] = []
    for order in _practice_orders_due(conn, session, trade_date):
        key = str(order["idempotency_key"])
        existing = conn.execute(
            "SELECT order_id FROM simulation_order WHERE segment_id=? AND idempotency_key=?",
            (segment_id, key),
        ).fetchone()
        if existing is not None:
            result.append(str(existing[0]))
            continue
        placed = place_simulation_order(
            conn,
            segment_id,
            ts_code=str(order["ts_code"]),
            direction=str(order["direction"]),
            planned_shares=int(order["planned_shares"]),
            trade_date=str(order["trade_date"]),
            idempotency_key=key,
        )
        result.append(str(placed["order_id"]))
    return result


def _sync_practice_orders(
    conn,
    session: Mapping[str, Any],
    segment_id: str,
    trade_date: str,
    processed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    orders = conn.execute(
        "SELECT o.order_id, o.idempotency_key, o.status, o.planned_shares, o.filled_shares,"
        " o.remaining_shares, o.reason_code, o.model_price_cents, o.fee_estimate_cents, p.detail_json, o.trade_date"
        " FROM simulation_order o JOIN practice_order p ON p.idempotency_key=o.idempotency_key"
        " WHERE o.segment_id=? AND p.practice_id=? AND o.trade_date<=?"
        " AND p.status IN ('pending','partially_filled') ORDER BY o.trade_date, o.order_id",
        (segment_id, _required(session, "practice_id"), trade_date),
    ).fetchall()
    updates: list[dict[str, Any]] = []
    for row in orders:
        engine_status = str(row[2])
        engine_result = next((item for item in processed if str(item.get("order_id")) == str(row[0])), None)
        if engine_result is not None:
            engine_status = str(engine_result.get("status") or engine_status)
        status = {
            "pending": "pending",
            "unfilled": "unfilled",
            "filled": "filled",
            "partially_filled": "partially_filled",
            "cancelled": "cancelled",
            "rejected": "rejected",
            "expired": "expired",
        }.get(engine_status, engine_status)
        try:
            prior_detail = json.loads(str(row[9]))
        except (TypeError, ValueError):
            prior_detail = {}
        detail = dict(prior_detail) if isinstance(prior_detail, dict) else {}
        if engine_result is not None:
            # Keep each engine response's cash flow scoped to the trade-date
            # attempt; the order-level fields below come from the cumulative DB row.
            attempt = {**engine_result, "trade_date": str(trade_date)}
            attempts = detail.get("attempts")
            attempts = list(attempts) if isinstance(attempts, list) else []
            attempts = [
                item for item in attempts
                if not isinstance(item, dict) or item.get("trade_date") != str(trade_date)
            ]
            attempts.append(attempt)
            detail.update(engine_result)
            detail["attempts"] = attempts
            detail["cash_flow_scope"] = "latest_attempt"
            detail["cash_spent_total_cents"] = sum(
                int(item.get("cash_spent_cents") or 0) for item in attempts if isinstance(item, dict)
            )
            detail["net_proceeds_total_cents"] = sum(
                int(item.get("net_proceeds_cents") or 0) for item in attempts if isinstance(item, dict)
            )
        else:
            detail["order_id"] = str(row[0])
        # These values represent the live cumulative order state and must not
        # inherit stale pending/partial values from a prior detail payload.
        detail.update({
            "status": engine_status,
            "filled_shares": int(row[4]),
            "reason_code": row[6],
            "remaining_shares": int(row[5]),
            "model_price_cents": row[7],
            "fee_estimate_cents": row[8],
        })
        # Core's order_id is distinct from the S5 engine id; match by its stable request key.
        conn.execute(
            "UPDATE practice_order SET filled_shares=?, status=?, detail_json=?"
            " WHERE practice_id=? AND idempotency_key=?",
            (
                int(row[4]),
                status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                _required(session, "practice_id"),
                str(row[1]),
            ),
        )
        updates.append({"idempotency_key": str(row[1]), "status": status, "filled_shares": int(row[4]), "detail": detail})
    return updates


def advance_paths(conn, session: dict[str, Any], next_trade_date: str) -> dict[str, Any]:
    segments = session.get("segments")
    if not isinstance(segments, Mapping) or set(PATHS) - set(segments):
        raise DataError("practice_comparison_segments_missing", "练习缺少完整四路径模拟段")
    expected = _get_next_trade_date(conn, session)
    if expected is None or expected != next_trade_date:
        raise DataError("practice_comparison_date_not_next", f"只能推进下一交易日 {expected}，请求为 {next_trade_date}")
    limitations: dict[str, list[str]] = {}
    user_segment = str(segments["user"])
    registered_user_orders = _register_practice_orders(conn, session, user_segment, next_trade_date)
    user_engine_result: dict[str, Any] = {"processed": []}
    for key, segment_id in segments.items():
        if key in ("incremental", "independent") and _next_rebalance_is_due(conn, session, str(segment_id)):
            limitations[key] = _create_system_orders(conn, session, key, str(segment_id), next_trade_date)
        engine_result = advance_simulation_segment(conn, str(segment_id), on_trade_date=next_trade_date)
        if key == "user":
            user_engine_result = engine_result
        compute_performance_point(
            conn,
            owner_type="simulation_segment",
            owner_id=str(segment_id),
            source_code=_required(session, "source_code"),
            data_version_id=_required(session, "data_version_id"),
            as_of=next_trade_date,
        )
        user_order_updates = _sync_practice_orders(
            conn, session, user_segment, next_trade_date, user_engine_result["processed"]
        )
    session["revealed_date"] = next_trade_date
    return {
        "revealed_date": next_trade_date,
        "limitations": limitations,
        "user_order_engine_ids": registered_user_orders,
        "user_order_updates": user_order_updates,
    }


def _path_result(conn, session: Mapping[str, Any], key: str, segment_id: str) -> dict[str, Any]:
    source = _required(session, "source_code")
    version = _required(session, "data_version_id")
    points = conn.execute(
        "SELECT v.as_of, v.nav_cents, p.period_return_bp, v.completeness, v.restriction_code"
        " FROM valuation_snapshot v LEFT JOIN performance_point p"
        " ON p.valuation_id=v.valuation_id AND p.owner_type=v.owner_type AND p.owner_id=v.owner_id"
        " WHERE v.owner_type='simulation_segment' AND v.owner_id=? AND v.as_of<=? ORDER BY v.as_of",
        (segment_id, _required(session, "revealed_date")),
    ).fetchall()
    navs = [row[1] for row in points]
    restrictions = sorted({str(row[4]) for row in points if row[4]})
    if key in ("incremental", "independent"):
        settings = _settings(session)
        if not settings.get("pool_snapshot_id") or not settings.get("parameter_bundle_id"):
            restrictions.append("system_rebalance_blocked_missing_versioned_pool_or_parameters")
        else:
            pool = conn.execute(
                "SELECT as_of, data_version_id FROM pool_snapshot WHERE snapshot_id=?",
                (settings["pool_snapshot_id"],),
            ).fetchone()
            if pool is None or str(pool[0]) != _required(session, "revealed_date") or str(pool[1]) != version:
                restrictions.append("system_rebalance_blocked_pool_not_as_of_revealed_date")
    order_reasons = conn.execute(
        "SELECT DISTINCT reason_code FROM simulation_order"
        " WHERE segment_id=? AND reason_code IS NOT NULL AND status IN"
        " ('blocked','rejected','expired','partially_filled')",
        (segment_id,),
    ).fetchall()
    restrictions.extend(str(row[0]) for row in order_reasons if row[0])
    if any(row[1] is None for row in points):
        restrictions.append("valuation_incomplete_nav_unknown")
    start_nav = navs[0] if navs else None
    end_nav = navs[-1] if navs else None
    return_bp = None
    if start_nav is not None and end_nav is not None and int(start_nav) > 0:
        return_bp = int(((Decimal(int(end_nav)) / Decimal(int(start_nav)) - 1) * 10_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    peak = None
    drawdown = None
    for nav in navs:
        if nav is None:
            drawdown = None
            break
        peak = max(int(nav), int(peak) if peak is not None else int(nav))
        current = int(((Decimal(int(nav)) / Decimal(int(peak)) - 1) * 10_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        drawdown = min(drawdown if drawdown is not None else 0, current)
    summary = performance_summary(conn, "simulation_segment", segment_id)
    fills = conn.execute(
        "SELECT COUNT(DISTINCT CASE WHEN f.filled_shares>0 THEN f.order_id END),"
        " COALESCE(SUM(fee_cents + slippage_cents), 0),"
        " COALESCE(SUM(ABS(f.filled_shares * f.price_cents)), 0) FROM simulation_fill f"
        " JOIN simulation_order o ON o.order_id=f.order_id WHERE o.segment_id=?",
        (segment_id,),
    ).fetchone()
    trade_count, cost, notional = map(int, fills)
    turnover = None if start_nav is None or int(start_nav) <= 0 else int((Decimal(notional) * 10_000 / Decimal(int(start_nav))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    state = get_simulation_account_state(conn, segment_id)
    concentration = None
    if end_nav is not None and int(end_nav) > 0 and not state["positions"]:
        concentration = 0
    elif end_nav is not None and int(end_nav) > 0:
        values: list[int] = []
        view = VersionView(conn, version, expected_source_code=source)
        for p in state["positions"]:
            price = valuation_price(
                conn,
                source_code=source,
                ts_code=str(p["ts_code"]),
                business_date=_required(session, "revealed_date"),
                as_of=_required(session, "revealed_date"),
                view=view,
            )
            if price.price is None:
                values = []
                break
            values.append(int((Decimal(str(price.price)) * int(p["shares"]) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        if values:
            concentration = int((Decimal(max(values)) * 10_000 / Decimal(int(end_nav))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "key": key,
        "label": PATHS[key],
        "nav_cents": end_nav,
        "return_bps": return_bp,
        "drawdown_bps": drawdown,
        "cost_cents": cost,
        "trade_count": trade_count,
        "turnover_bps": turnover,
        "concentration_bps": concentration,
        "points": [{"date": str(row[0]), "nav_cents": row[1], "return_bps": row[2]} for row in points],
        "restrictions": sorted(set(restrictions)),
        "segment_id": segment_id,
        "performance_method": summary["method_version"],
    }


def get_comparison(conn, session: dict[str, Any]) -> dict[str, Any]:
    segments = session.get("segments")
    if not isinstance(segments, Mapping):
        raise DataError("practice_comparison_segments_missing", "练习缺少模拟段映射")
    paths = [_path_result(conn, session, key, str(segments[key])) for key in PATHS if key in segments]
    contributions: list[dict[str, Any]] = []
    user = next((p for p in paths if p["key"] == "user"), None)
    for path in paths:
        if path["key"] == "user":
            continue
        difference = None if path["nav_cents"] is None or not user or user["nav_cents"] is None else int(path["nav_cents"]) - int(user["nav_cents"])
        contributions.append({"path": path["key"], "vs_user_nav_difference_cents": difference, "basis": "算术对照；不代表策略因果贡献"})
    limitations = sorted({r for p in paths for r in p["restrictions"]})
    view = VersionView(conn, _required(session, "data_version_id"), expected_source_code=_required(session, "source_code"))
    if view.corporate_action_count():
        limitations.append("corporate_actions_require_explicit_settlement_review")
    return {
        "as_of": _required(session, "revealed_date"),
        "paths": paths,
        "contributions": contributions,
        "limitations": sorted(set(limitations)),
    }


def finish_paths(conn, session: dict[str, Any]) -> dict[str, Any]:
    """关闭未完成订单并使用当前揭晓日的估值快照；不强制卖出。"""
    segments = session.get("segments")
    if not isinstance(segments, Mapping):
        raise DataError("practice_comparison_segments_missing", "练习缺少模拟段映射")
    for segment_id in segments.values():
        # close_simulation_segment expires any pending remainder atomically.
        close_simulation_segment(conn, str(segment_id), reason="practice_finished")
    return get_comparison(conn, session)
