"""Bounded local simulation runner. Every effect and checkpoint commits together."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
from threading import Event, Lock
from time import monotonic

from app.data.chunks import VersionView
from app.data.universe import PoolRule, generate_pool_snapshot
from app.portfolio.service import DataSelectionContext, bind_data_context
from app.portfolio.simulation import get_simulation_account_state
from . import observation as plans
from .algorithms import calculate_registered_algorithm
from .core import PracticeError, next_trade_date, require_session, utc_now
from .runtime import practice_connect, practice_settings
from .storage import decode_json, encode_json


def _connection(settings):
    conn = practice_connect(settings)
    plans.ensure_schema(conn)
    conn.commit()
    return conn


def _targets(conn, row, session, account):
    """Use current visible history and the pinned registered production components."""
    meta = conn.execute("SELECT scope FROM data_version WHERE version_id=?", (session["data_version_id"],)).fetchone()
    scope = decode_json(meta[0]) if meta else {}
    synthetic = (scope.get("kind") == "explicit_practice_synthetic_demo" and
                 session["source_code"] == "tushare-fixture")
    symbols = decode_json(row["symbols_json"])
    pool = generate_pool_snapshot(
        conn, as_of=session["revealed_date"], data_version_id=session["data_version_id"],
        source_code=session["source_code"], ts_codes=symbols,
        rule=PoolRule(min_avg_amount_cny=0.0, min_history_trade_days=120) if synthetic else PoolRule(),
    )
    parameter_id = session["settings"].get("parameter_bundle_id")
    if not parameter_id: raise PracticeError("parameter_bundle_required", "缺少冻结的算法参数规则", 409)
    context = bind_data_context(conn, DataSelectionContext(
        source_code=session["source_code"], as_of=session["revealed_date"], purpose="signal_input",
        data_version_id=session["data_version_id"], account_revision=account["revision"],
        rule_bundle_id=parameter_id, pool_snapshot_id=pool["snapshot_id"],
    ))
    if synthetic and context.restriction_code == "universe_unverified":
        # An explicit, generated toy universe has no claim to market completeness.
        # Keep its per-security history/price eligibility checks and report the assumption.
        context = replace(context, restricted=False, restriction_code=None, restriction_detail=None,
                          limits=[*context.limits, "explicit_synthetic_universe_only"])
    if context.restricted:
        raise PracticeError("observation_data_restricted", "行情资格或市场范围尚未核验，执行已停止", 409)
    algorithm = decode_json(row["algorithm_json"])
    report = calculate_registered_algorithm(conn, context, account,
        algorithm_id=algorithm["algorithm_id"], algorithm_version=algorithm["algorithm_version"],
        parameters=algorithm["parameters"])
    if report.get("restricted"):
        raise PracticeError("observation_target_restricted", "算法目标受限，执行已停止", 409)
    return report, synthetic


def execute_step(conn, execution_id, run_token, actions, settings):
    """Caller owns BEGIN IMMEDIATE: authorization, effects and checkpoint are atomic."""
    row = conn.execute("SELECT * FROM observation_execution WHERE execution_id=?", (execution_id,)).fetchone()
    if row is None or row["state"] != "running" or row["run_token"] != run_token:
        return False
    session = plans.validate_frozen(conn, row)
    limits, permissions = decode_json(row["limits_json"]), decode_json(row["permissions_json"])
    if not permissions["observe"]:
        plans.stop(conn, row, "paused", "observation_permission_disabled")
        return False
    day = next_trade_date(conn, session)
    if row["completed_steps"] >= limits["steps"] or session["revealed_date"] >= row["deadline"]:
        plans.stop(conn, row, "completed", "authorized_boundary_reached")
        return False
    if (permissions["advance"] or permissions["orders"]) and (not day or day > row["deadline"]):
        plans.stop(conn, row, "completed", "no_authorized_next_date")
        return False
    # Advancing must never incidentally execute someone else's manually queued orders.
    if (permissions["advance"] or permissions["orders"]) and conn.execute(
        "SELECT 1 FROM practice_order WHERE practice_id=? AND status IN ('pending','partially_filled')",
        (session["practice_id"],),
    ).fetchone():
        raise PracticeError("pending_orders_require_review", "已有待执行订单，请先人工处理后重新授权", 409)
    account = get_simulation_account_state(conn, session["segments"]["user"])
    report, synthetic = _targets(conn, row, session, account)
    if permissions["orders"] and (report.get("allocation") or {}).get("restricted"):
        raise PracticeError("allocation_rules_restricted", "分配规则尚未绑定有效约束，请重新检查方案", 409)
    symbols = set(decode_json(row["symbols_json"]))
    targets = (report.get("allocation") or {}).get("targets", [])
    seen = set()
    if not isinstance(targets, list):
        raise PracticeError("algorithm_output_invalid", "算法目标格式不受支持，执行已停止", 409)
    for target in targets:
        if (not isinstance(target, dict) or not isinstance(target.get("ts_code"), str)
                or target["ts_code"] not in symbols or target["ts_code"] in seen
                or type(target.get("target_value_cents")) is not int or target["target_value_cents"] < 0):
            raise PracticeError("algorithm_output_invalid", "算法目标超出范围或金额无效，执行已停止", 409)
        seen.add(target["ts_code"])
    holdings = {p["ts_code"]: p for p in account["positions"]}
    total_shares = sum(int(p["shares"]) for p in holdings.values())
    if total_shares > limits["max_position_shares"]:
        raise PracticeError("position_limit_exceeded", "当前持仓已超过授权股数上限", 409)
    prefix = f"obs:{execution_id}:{row['completed_steps'] + 1}:"
    order_count = 0
    if permissions["orders"]:
        market = VersionView(conn, session["data_version_id"], expected_source_code=session["source_code"])
        allocation = report.get("allocation") or {}
        remaining_cash = int(account["cash_cents"])
        # Reserve is kept intact; same-batch sale proceeds are not spent speculatively.
        remaining_cash = min(remaining_cash, int(allocation.get("investable_cents") or 0))
        from app.portfolio.rule_bundles import load_rule_bundle
        rule_id = session["settings"].get("simulation_rule_bundle_id") or session["settings"].get("rule_bundle_id")
        rule = load_rule_bundle(conn, rule_id)
        lot = (rule or {}).get("content", {}).get("market_rules", {}).get("board_lot_shares")
        if type(lot) is not int or lot <= 0:
            raise PracticeError("board_lot_unknown", "冻结规则缺少每手股数", 409)
        for target in allocation.get("targets", []) if allocation.get("ok") else []:
            code = target["ts_code"]
            if code not in symbols: raise PracticeError("algorithm_scope_violation", "算法目标超出授权证券", 409)
            price = market.latest_daily(code, on_or_before=session["revealed_date"])
            if not price or price["close"] is None or price["trade_date"] != session["revealed_date"]:
                raise PracticeError("observation_price_unknown", "缺少当前可见日的目标价格，执行已停止", 409)
            cents = int((Decimal(str(price["close"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if cents <= 0: raise PracticeError("observation_price_unknown", "目标价格无效，执行已停止", 409)
            position = holdings.get(code, {})
            current = int(position.get("shares") or 0)
            desired = min(int(target["target_value_cents"]) // cents // lot * lot,
                          (limits["max_position_shares"] - total_shares + current) // lot * lot)
            delta = desired - current
            direction = "buy" if delta > 0 else "sell"
            shares = min(abs(delta), limits["max_order_value_cents"] // cents) // lot * lot
            if direction == "buy":
                shares = min(shares, remaining_cash // cents // lot * lot)
            else:
                shares = min(shares, int(position.get("sellable_shares") or 0) // lot * lot)
                if position.get("locked"): shares = 0
            if not shares: continue
            session = require_session(conn, row["practice_id"])
            actions["order"](conn, row["practice_id"], {
                "expected_revision": session["revision"], "idempotency_key": prefix + code + ":" + direction,
                "ts_code": code, "direction": direction, "planned_shares": shares,
            }, settings)
            if direction == "buy": remaining_cash -= shares * cents
            total_shares += shares if direction == "buy" else -shares
            order_count += 1
    advanced = False
    if permissions["advance"]:
        session = require_session(conn, row["practice_id"])
        actions["advance"](conn, row["practice_id"], {
            "expected_revision": session["revision"], "idempotency_key": prefix + "advance",
        }, settings)
        advanced = True
        # Unknown next-day prices cannot be used in sizing. If a gap violates the
        # monetary cap, roll back the WHOLE step before marking it blocked.
        fills = conn.execute(
            "SELECT o.ts_code,SUM(f.filled_shares*f.price_cents) AS notional FROM simulation_order o "
            "JOIN simulation_fill f ON f.order_id=o.order_id WHERE o.segment_id=? AND o.idempotency_key LIKE ? GROUP BY o.order_id",
            (session["segments"]["user"], prefix + "%"),
        ).fetchall()
        if any(int(f["notional"] or 0) > limits["max_order_value_cents"] for f in fills):
            raise PracticeError("execution_price_exceeds_limit", "下一日成交超出授权金额，整步已回滚", 409)
        after = get_simulation_account_state(conn, session["segments"]["user"])
        if sum(int(p["shares"]) for p in after["positions"]) > limits["max_position_shares"]:
            raise PracticeError("execution_position_exceeds_limit", "成交后持仓超出授权，整步已回滚", 409)
    session = require_session(conn, row["practice_id"])
    frozen = decode_json(row["frozen_json"])
    frozen["practice_revision"] = session["revision"]
    count = row["completed_steps"] + 1
    conn.execute("UPDATE observation_execution SET completed_steps=?,frozen_json=?,updated_at=? WHERE execution_id=?",
                 (count, encode_json(frozen), utc_now(), execution_id))
    plans._event(conn, execution_id, row["version"], "step_completed", {
        "step": count, "as_of": report["as_of"], "revealed_date": session["revealed_date"],
        "input_digest": report["input_digest"], "synthetic": synthetic,
        "orders": order_count, "advanced": advanced,
        "selected": report["selection"]["selected"],
    })
    if not advanced or count >= limits["steps"] or session["revealed_date"] >= row["deadline"] or not next_trade_date(conn, session):
        plans.stop(conn, row, "completed", "awaiting_manual_advance" if order_count and not advanced else "authorized_boundary_reached")
        return False
    return True


class ObservationRunner:
    def __init__(self, settings, actions):
        self.settings, self.actions = settings, actions
        self._stop = Event()
        self._lock = Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="practice-observe")
        self._jobs = set()

    def recover(self):
        # Opening an unused application must not create a practice database.
        if not practice_settings(self.settings).db_path.exists(): return
        conn = _connection(self.settings)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                for row in conn.execute("SELECT * FROM observation_execution WHERE state='running'").fetchall():
                    plans.stop(conn, row, "paused", "process_restarted_review_required")
        finally: conn.close()

    def start(self, execution_id, body, *, resume=False):
        with self._lock:
            if self._stop.is_set(): raise PracticeError("runner_stopping", "服务正在停止", 503)
            conn = _connection(self.settings)
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    result = plans.begin_run(conn, execution_id, body, resume=resume)
                token = result["run_token"]
                if token:
                    future = self._pool.submit(self._run, execution_id, token)
                    self._jobs.add(future)
                self._jobs = {job for job in self._jobs if not job.done()}
                return result["execution"]
            finally: conn.close()

    def _run(self, execution_id, token):
        started = monotonic()
        for _ in range(20):
            if self._stop.is_set() or monotonic() - started >= 120:
                self._halt(execution_id, token, "paused", "runner_stopped_review_required")
                return
            conn = _connection(self.settings)
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    more = execute_step(conn, execution_id, token, self.actions, self.settings)
            except Exception as exc:
                conn.rollback()
                self._halt(execution_id, token, "blocked", getattr(exc, "reason", "execution_failed_review_required"))
                return
            finally: conn.close()
            if not more: return
            self._stop.wait(0.05)
        self._halt(execution_id, token, "paused", "runner_step_bound_reached")

    def _halt(self, execution_id, token, state, reason):
        conn = _connection(self.settings)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM observation_execution WHERE execution_id=?", (execution_id,)).fetchone()
                if row and row["state"] == "running" and row["run_token"] == token:
                    plans.stop(conn, row, state, reason)
        finally: conn.close()

    def shutdown(self):
        with self._lock: self._stop.set()
        self._pool.shutdown(wait=True, cancel_futures=False)
        self.recover()
