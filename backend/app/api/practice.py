"""历史练习 API：隔离练习库、冻结数据版本和显式单日推进。"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request

from app.data.chunks import VersionView
from app.data.chunks import version_members
from app.data.store import publish_data_version, published_versions, version_count, write_records
from app.data.universe import PoolRule, generate_pool_snapshot
from app.portfolio.demo import ensure_demo_database
from app.portfolio.ledger import AccountEventRequest, create_account, post_account_event
from app.portfolio.rule_bundles import create_rule_bundle, ensure_default_bundles, load_rule_bundle
from app.practice.core import (
    PracticeError,
    check_revision,
    compare_and_update,
    idempotent_response,
    next_trade_date,
    parse_date,
    require_session,
    save_idempotency,
    utc_now,
)
from app.practice.runtime import practice_connect, practice_settings
from app.practice.storage import decode_json, encode_json


_RECORD_TABLES = {
    "calendar": "trade_calendar",
    "security": "security_info",
    "daily": "daily_price",
    "adj_factor": "adj_factor",
    "suspension": "suspension_event",
    "corporate_action": "corporate_action",
}


def _install_demo_seed(target: sqlite3.Connection, seeded: dict[str, Any]) -> dict[str, Any]:
    """把隔离 phase3 demo 的已发布合成样本重新固化到 practice app.db。"""
    from pathlib import Path

    source_uri = Path(str(seeded["db_path"])).resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=5.0)
    source.row_factory = sqlite3.Row
    try:
        data_version_id = str(seeded["version_id"])
        grouped: dict[str, list[dict[str, Any]]] = {}
        for category, record_id, _content_hash, _logical_key in version_members(source, data_version_id):
            table = _RECORD_TABLES.get(category)
            if not table:
                continue
            row = source.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            if row is not None:
                grouped.setdefault(category, []).append(dict(row))
        members: dict[str, list[tuple[int, str, str]]] = {}
        for category, rows in grouped.items():
            written = write_records(target, category, rows, source_code=str(seeded["source_code"]))
            member_rows = []
            for record_id, logical_key in zip(written.record_ids, written.logical_keys):
                content_hash = target.execute(
                    f"SELECT content_hash FROM {_RECORD_TABLES[category]} WHERE id=?", (record_id,)
                ).fetchone()[0]
                member_rows.append((record_id, logical_key, str(content_hash)))
            members[category] = member_rows
        source_meta = source.execute(
            "SELECT categories,rule_version,quality_summary FROM data_version WHERE version_id=?",
            (data_version_id,),
        ).fetchone()
        import json

        categories = json.loads(source_meta[0])
        target_version = publish_data_version(
            target,
            source_code=str(seeded["source_code"]),
            scope={"kind": "explicit_practice_synthetic_demo", "seed_version_id": data_version_id},
            categories=categories,
            rule_version=str(source_meta[1]),
            quality_summary={"synthetic": True, "limitations": ["离线合成示例，仅用于练习流程验证。"]},
            members=members,
        )
        pool_members = [
            str(row[0])
            for row in source.execute(
                "SELECT ts_code FROM pool_member WHERE snapshot_id=? ORDER BY seq",
                (seeded["pool_snapshot_id"],),
            ).fetchall()
        ]
        source_as_of = str(source.execute(
            "SELECT as_of FROM pool_snapshot WHERE snapshot_id=?", (seeded["pool_snapshot_id"],)
        ).fetchone()[0])
        pool = generate_pool_snapshot(
            target,
            as_of=source_as_of,
            data_version_id=str(target_version["version_id"]),
            source_code=str(seeded["source_code"]),
            rule=PoolRule(min_avg_amount_cny=0.0, min_history_trade_days=120),
            ts_codes=pool_members,
        )
        default_bundles = ensure_default_bundles(target)
        copied_bundles: dict[str, str] = {}
        for key, seeded_key in (
            ("parameter", "parameter_bundle_id"),
            ("fee", "fee_bundle_id"),
            ("rule", "simulation_rule_bundle_id"),
        ):
            bundle = load_rule_bundle(source, str(seeded[seeded_key]))
            if bundle is None:
                raise PracticeError("demo_seed_invalid", f"合成示例缺少 {key} 版本", 500)
            copied = create_rule_bundle(
                target,
                kind=str(bundle["kind"]),
                version=str(bundle["version"]),
                assumption_scope=str(bundle["assumption_scope"]),
                content=bundle["content"],
                notes=bundle.get("notes"),
            )
            copied_bundles[key] = str(copied["bundle_id"])
        copied_bundles.setdefault("parameter", default_bundles["parameter"])
        copied_bundles.setdefault("fee", default_bundles["fee"])
        return {
            "source_code": str(seeded["source_code"]),
            "version_id": str(target_version["version_id"]),
            "pool_snapshot_id": str(pool["snapshot_id"]),
            "parameter_bundle_id": copied_bundles["parameter"],
            "fee_bundle_id": copied_bundles["fee"],
            "rule_bundle_id": copied_bundles["rule"],
            "start_date": seeded.get("coverage_start"),
            "end_date": seeded.get("coverage_end"),
            "synthetic": True,
        }
    finally:
        source.close()


def create_practice_router() -> APIRouter:
    router = APIRouter(prefix="/api/practice", tags=["practice"])

    def connect(request: Request) -> sqlite3.Connection:
        return practice_connect(request.app.state.settings)

    def options(conn: sqlite3.Connection) -> dict[str, Any]:
        total_versions = version_count(conn, include_retired=False)
        versions = published_versions(conn, limit=100, offset=max(0, total_versions - 100))
        source_rows = conn.execute(
            "SELECT DISTINCT source_code FROM data_version WHERE status='published' ORDER BY source_code"
        ).fetchall()
        sources = [
            {"code": str(row[0]), "synthetic": str(row[0]) in {"synthetic", "tushare-fixture"}}
            for row in source_rows
        ]
        symbols: list[dict[str, Any]] = []
        limitations = [
            "练习行情只读取所选数据版本；不会在普通读取请求中联网更新。",
            "历史股票池、停牌和公司行为能力取决于已发布数据版本的实际覆盖。",
        ]
        for version in reversed(versions):
            try:
                view = VersionView(conn, version["version_id"], expected_source_code=version["source_code"])
            except Exception:
                continue
            symbols = [
                {"ts_code": str(row["ts_code"]), "name": row["name"]}
                for row in view.securities()
            ]
            break
        return {
            "sources": sources,
            "versions": versions,
            "symbols": symbols,
            "defaults": {
                "cash_cents": 1_000_000,
                "rebalance_every_n_days": 5,
                "data_version_id": versions[-1]["version_id"] if versions else None,
                "source_code": versions[-1]["source_code"] if versions else None,
            },
            "demo_available": True,
            "limitations": limitations,
        }

    def view(conn: sqlite3.Connection, session: dict[str, Any]) -> dict[str, Any]:
        try:
            from app.practice.comparison import get_comparison
        except ImportError as exc:
            raise PracticeError("comparison_unavailable", "对照引擎尚未就绪", 503) from exc
        comparison = get_comparison(conn, session)
        version = conn.execute(
            "SELECT source_code, quality_summary FROM data_version WHERE version_id=?",
            (session["data_version_id"],),
        ).fetchone()
        source_limitations: list[str] = []
        if version:
            try:
                quality = decode_json(str(version[1]))
                if isinstance(quality, dict):
                    source_limitations.extend(str(item) for item in quality.get("limitations", []))
            except (ValueError, TypeError):
                pass
        market = VersionView(conn, session["data_version_id"], expected_source_code=session["source_code"])
        return {
            "practice_id": session["practice_id"],
            "name": session["name"],
            "status": session["status"],
            "revision": session["revision"],
            "start_date": session["start_date"],
            "end_date": session["end_date"],
            "revealed_date": session["revealed_date"],
            "next_trade_date": next_trade_date(conn, session),
            "settings": session["settings"],
            "source": {
                "code": session["source_code"],
                "version_id": session["data_version_id"],
                "synthetic": session["source_code"] in {"synthetic", "tushare-fixture"},
                "limitations": source_limitations,
            },
            "user_state": session["user_state"],
            "comparison": comparison,
            "decisions": decisions(conn, session["practice_id"]),
        }

    def decisions(conn: sqlite3.Connection, practice_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT decision_id,decision_date,recorded_at,judgment,evidence,exit_condition,is_backfill,corrects_decision_id"
            " FROM practice_decision WHERE practice_id=? ORDER BY decision_date,recorded_at,decision_id",
            (practice_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def refreshed_user_state(conn: sqlite3.Connection, session: dict[str, Any]) -> dict[str, Any]:
        from app.portfolio.simulation import get_simulation_account_state

        segment_id = session["segments"].get("user")
        if not segment_id:
            raise PracticeError("user_simulation_missing", "练习缺少用户模拟段", 500)
        state = get_simulation_account_state(conn, str(segment_id))
        order_rows = conn.execute(
            "SELECT order_id,trade_date,ts_code,direction,planned_shares,filled_shares,status,detail_json"
            " FROM practice_order WHERE practice_id=? ORDER BY created_at,order_id",
            (session["practice_id"],),
        ).fetchall()
        return {
            "cash_cents": int(state["cash_cents"]),
            "positions": [position for position in state["positions"] if int(position["shares"]) > 0],
            "orders": [
                {"order_id": str(row[0]), "trade_date": str(row[1]), "ts_code": str(row[2]),
                 "direction": str(row[3]), "planned_shares": int(row[4]), "filled_shares": int(row[5]),
                 "status": str(row[6]), "detail": decode_json(str(row[7]))}
                for row in order_rows
            ],
        }

    def create_session(
        conn: sqlite3.Connection,
        *,
        payload: dict[str, Any],
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise PracticeError("name_required", "name 必须为 1..120 字符")
        start = parse_date(payload.get("start_date"), "start_date")
        end = parse_date(payload.get("end_date"), "end_date")
        if end < start:
            raise PracticeError("date_range_invalid", "end_date 不能早于 start_date")
        cash = payload.get("cash_cents")
        if isinstance(cash, bool) or not isinstance(cash, int) or cash < 0:
            raise PracticeError("cash_invalid", "cash_cents 必须为非负整数")
        version_id = payload.get("data_version_id")
        source_code = payload.get("source_code")
        if not isinstance(version_id, str) or not version_id.strip():
            raise PracticeError("data_version_required", "必须从 options 中选择数据版本")
        version = conn.execute(
            "SELECT source_code,status FROM data_version WHERE version_id=?", (version_id,)
        ).fetchone()
        if version is None or str(version[1]) != "published":
            raise PracticeError("data_version_unavailable", "所选数据版本不存在或尚未发布", 422)
        if source_code != str(version[0]):
            raise PracticeError("source_version_mismatch", "数据来源与所选版本不一致", 422)
        market = VersionView(conn, version_id, expected_source_code=source_code)
        calendar = market.calendar_days()
        if start not in calendar:
            raise PracticeError("start_date_unavailable", "start_date 必须位于已选版本覆盖的交易日", 422)
        if not any(start <= day <= end for day in calendar):
            raise PracticeError("date_range_unavailable", "所选日期范围没有已发布交易日", 422)
        pool = conn.execute(
            "SELECT snapshot_id FROM pool_snapshot WHERE data_version_id=? ORDER BY generated_at DESC LIMIT 1",
            (version_id,),
        ).fetchone()
        pool_snapshot_id = str(pool[0]) if pool else None
        supplied = payload.get("settings") or {}
        if not isinstance(supplied, dict):
            raise PracticeError("settings_invalid", "settings 必须为 JSON 对象")
        bundles = ensure_default_bundles(conn)
        settings = {
            "rebalance_every_n_days": 5,
            "parameter_bundle_id": bundles["parameter"],
            "rule_bundle_id": supplied.get("rule_bundle_id", bundles["parameter"]),
            "fee_bundle_id": bundles["fee"],
            "hide_system_hints": False,
            "max_single_name_weight_bp": 2500,
            "min_cash_reserve_bp": 1000,
            "constraints": {},
            **supplied,
        }
        settings["stock_scope"] = {
            **(settings.get("stock_scope") if isinstance(settings.get("stock_scope"), dict) else {}),
            "seed_snapshot_id": pool_snapshot_id,
            "pool_snapshot_id": pool_snapshot_id,
            "data_version_id": version_id,
        }
        settings["pool_snapshot_id"] = pool_snapshot_id
        if settings.get("rebalance_every_n_days") != 5:
            raise PracticeError("rebalance_schedule_frozen", "首版系统路径固定每5个已揭晓交易日检查")
        if not isinstance(settings.get("hide_system_hints"), bool):
            raise PracticeError("settings_invalid", "hide_system_hints 必须为布尔值")
        for field, low, high in (("max_single_name_weight_bp", 1, 10_000), ("min_cash_reserve_bp", 0, 10_000)):
            value = settings.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise PracticeError("settings_invalid", f"{field} 必须为 {low}..{high} 的整数基点")
        if not isinstance(settings.get("constraints"), dict):
            raise PracticeError("settings_invalid", "constraints 必须为 JSON 对象")
        for field, expected_kind in (
            ("parameter_bundle_id", "parameter"),
            ("rule_bundle_id", "parameter"),
            ("fee_bundle_id", "fee"),
        ):
            bundle = load_rule_bundle(conn, settings.get(field, ""))
            if bundle is None or bundle.get("kind") != expected_kind:
                raise PracticeError("rule_bundle_unavailable", f"{field} 未绑定可用的 {expected_kind} 版本", 422)
        positions = payload.get("positions") or []
        if not isinstance(positions, list):
            raise PracticeError("positions_invalid", "positions 必须为列表")
        allowed_codes = {str(row["ts_code"]) for row in market.securities()}
        seen_codes: set[str] = set()
        for position in positions:
            if not isinstance(position, dict):
                raise PracticeError("positions_invalid", "每项初始持仓必须为对象")
            code = position.get("ts_code")
            shares = position.get("shares")
            sellable = position.get("sellable_shares")
            if code not in allowed_codes or code in seen_codes:
                raise PracticeError("positions_invalid", "初始持仓证券须属于数据版本且不得重复", 422)
            if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
                raise PracticeError("positions_invalid", "初始持仓 shares 必须为正整数")
            if sellable is not None and (isinstance(sellable, bool) or not isinstance(sellable, int) or not 0 <= sellable <= shares):
                raise PracticeError("positions_invalid", "sellable_shares 必须在 0..shares 之间")
            if "total_cost_cents" in position and position["total_cost_cents"] is not None and (
                isinstance(position["total_cost_cents"], bool) or not isinstance(position["total_cost_cents"], int) or position["total_cost_cents"] < 0
            ):
                raise PracticeError("positions_invalid", "total_cost_cents 必须为非负整数或 null")
            seen_codes.add(str(code))
        if parent_id:
            parent = require_session(conn, parent_id)
            positions = parent["settings"].get("initial_positions", [])
            cash = int(parent["settings"].get("initial_cash_cents", cash))
        practice_id = str(uuid.uuid4())
        if pool_snapshot_id is None:
            raise PracticeError("seed_snapshot_unavailable", "该数据版本没有可用历史股票池快照", 422)
        # The simulation engine requires a persisted account snapshot as its
        # seed. Keep this seed account inside the isolated practice database;
        # it is never created in the user's real runtime database.
        seed_account_id = "practice_seed_" + uuid.uuid4().hex
        create_account(conn, seed_account_id, f"历史练习期初 {practice_id[:8]}")
        opening = post_account_event(
            conn,
            AccountEventRequest(
                account_id=seed_account_id,
                event_type="opening",
                business_date=start,
                idempotency_key=f"practice-seed:{practice_id}",
                payload={
                    "cash_cents": cash,
                    "positions": positions,
                    "note": "隔离历史练习期初快照",
                },
            ),
            expected_account_revision=0,
        )
        seed_snapshot_id = opening.snapshot_id
        if not seed_snapshot_id:
            raise PracticeError("seed_snapshot_unavailable", "未能固化练习期初账户快照", 500)
        settings["practice_seed_account_id"] = seed_account_id
        now = utc_now()
        user_state = {"cash_cents": cash, "positions": positions, "orders": []}
        settings["initial_cash_cents"] = cash
        settings["initial_positions"] = positions
        session = {
            "practice_id": practice_id,
            "name": name.strip(),
            "status": "active",
            "revision": 1,
            "start_date": start,
            "end_date": end,
            "revealed_date": start,
            "data_version_id": version_id,
            "source_code": source_code,
            "seed_snapshot_id": seed_snapshot_id,
            "segments": {},
            "settings": settings,
            "user_state": user_state,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(
            "INSERT INTO practice_session(practice_id,name,status,revision,start_date,end_date,revealed_date,"
            "data_version_id,source_code,seed_snapshot_id,segments_json,settings_json,user_state_json,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (practice_id, session["name"], "active", 1, start, end, start, version_id,
             source_code, seed_snapshot_id, "{}", encode_json(settings), encode_json(user_state), now, now),
        )
        try:
            from app.practice.comparison import initialize_paths

            session["segments"] = initialize_paths(conn, session)
        except ImportError as exc:
            raise PracticeError("comparison_unavailable", "对照引擎尚未就绪", 503) from exc
        conn.execute(
            "UPDATE practice_session SET segments_json=? WHERE practice_id=?",
            (encode_json(session["segments"]), practice_id),
        )
        return session

    def order_preview(conn: sqlite3.Connection, session: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        code = body.get("ts_code")
        direction = body.get("direction")
        shares = body.get("planned_shares")
        if not isinstance(code, str) or direction not in {"buy", "sell"} or isinstance(shares, bool) or not isinstance(shares, int) or shares < 1:
            raise PracticeError("order_invalid", "ts_code、direction 或 planned_shares 无效")
        version = VersionView(conn, session["data_version_id"], expected_source_code=session["source_code"])
        if code not in {str(row["ts_code"]) for row in version.securities()}:
            raise PracticeError("security_not_in_version", "证券不属于本练习冻结的数据版本", 422)
        price = version.latest_daily(code, on_or_before=session["revealed_date"])
        known_price = int(round(float(price["close"]) * 100)) if price and price["close"] is not None else None
        estimated = known_price * shares if known_price is not None else None
        position = next(
            (item for item in session["user_state"].get("positions", []) if item.get("ts_code") == code),
            None,
        )
        if direction == "sell":
            sellable = int(position.get("sellable_shares") or 0) if position else 0
            if not position or position.get("locked") or shares > sellable:
                raise PracticeError("insufficient_sellable_shares", "卖出数量超过已知可卖持仓或持仓受限", 409)
        cash = int(session["user_state"].get("cash_cents", 0))
        return {
            "ts_code": code, "direction": direction, "planned_shares": shares,
            "as_of": session["revealed_date"], "known_price_cents": known_price,
            "estimated_notional_cents": estimated,
            "cash_sufficient_at_known_price": None if estimated is None or direction != "buy" else cash >= estimated,
            "execution_date": next_trade_date(conn, session),
            "future_execution_price_cents": None, "writes_order": False,
        }

    @router.get("/options")
    def get_options(request: Request) -> dict[str, Any]:
        with connect(request) as conn:
            return options(conn)

    @router.post("/demo")
    def prepare_demo(request: Request) -> dict[str, Any]:
        # The reusable phase-3 seed is redirected beneath runtime/practice;
        # it never writes into the user's actual runtime database.
        seeded = ensure_demo_database(practice_settings(request.app.state.settings))
        conn = connect(request)
        try:
            demo = _install_demo_seed(conn, seeded)
            result = options(conn)
            result["defaults"].update({
                "data_version_id": demo["version_id"],
                "source_code": demo["source_code"],
                "parameter_bundle_id": demo["parameter_bundle_id"],
                "rule_bundle_id": demo["rule_bundle_id"],
                "fee_bundle_id": demo["fee_bundle_id"],
            })
            result["demo"] = demo
            return result
        finally:
            conn.close()

    @router.get("/sessions")
    def list_sessions(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        with connect(request) as conn:
            rows = conn.execute(
                "SELECT practice_id,name,status,revision,start_date,end_date,revealed_date,data_version_id,source_code,updated_at"
                " FROM practice_session ORDER BY updated_at DESC LIMIT ? OFFSET ?", (limit, offset),
            ).fetchall()
            total = int(conn.execute("SELECT COUNT(*) FROM practice_session").fetchone()[0])
            summaries = []
            for row in rows:
                item = dict(row)
                item["source"] = {
                    "code": item["source_code"],
                    "version_id": item["data_version_id"],
                    "synthetic": item["source_code"] in {"synthetic", "tushare-fixture"},
                }
                summaries.append(item)
            return {"sessions": summaries, "total": total}

    @router.post("/sessions")
    def create(request: Request, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                replay, request_hash = idempotent_response(
                    conn, operation="create", key=body.get("idempotency_key"), request=body
                )
                if replay is not None:
                    return replay
                session = create_session(conn, payload=body)
                response = {"ok": True, "session": view(conn, session)}
                save_idempotency(conn, operation="create", key=body["idempotency_key"],
                                 request_hash=request_hash, practice_id=session["practice_id"], response=response)
                return response
        finally:
            conn.close()

    @router.get("/sessions/{practice_id}")
    def get_session(request: Request, practice_id: str):
        with connect(request) as conn:
            return {"session": view(conn, require_session(conn, practice_id))}

    @router.post("/sessions/{practice_id}/restart")
    def restart(request: Request, practice_id: str, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                replay, request_hash = idempotent_response(conn, operation=f"restart:{practice_id}",
                                                           key=body.get("idempotency_key"), request=body)
                if replay is not None:
                    return replay
                old = require_session(conn, practice_id)
                payload = {"name": old["name"], "start_date": old["start_date"],
                           "end_date": old["end_date"], "cash_cents": old["settings"]["initial_cash_cents"],
                           "positions": old["settings"]["initial_positions"], "data_version_id": old["data_version_id"],
                           "source_code": old["source_code"], "settings": old["settings"]}
                new = create_session(conn, payload=payload, parent_id=practice_id)
                response = {"ok": True, "session": view(conn, new)}
                save_idempotency(conn, operation=f"restart:{practice_id}", key=body["idempotency_key"],
                                 request_hash=request_hash, practice_id=new["practice_id"], response=response)
                return response
        finally:
            conn.close()

    @router.post("/sessions/{practice_id}/orders/preview")
    def preview_order(request: Request, practice_id: str, body: dict[str, Any]):
        with connect(request) as conn:
            session = require_session(conn, practice_id)
            return {"preview": order_preview(conn, session, body)}

    @router.post("/sessions/{practice_id}/orders")
    def place_order(request: Request, practice_id: str, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                session = require_session(conn, practice_id)
                replay, request_hash = idempotent_response(conn, operation=f"order:{practice_id}",
                                                           key=body.get("idempotency_key"), request=body)
                if replay is not None:
                    return replay
                check_revision(session, body.get("expected_revision"))
                trade_date = next_trade_date(conn, session)
                if trade_date is None:
                    raise PracticeError("no_future_trade_date", "该练习区间没有下一交易日", 409)
                preview = order_preview(conn, session, body)
                code = str(preview["ts_code"])
                direction, shares = preview["direction"], preview["planned_shares"]
                if direction == "buy" and preview["cash_sufficient_at_known_price"] is False:
                    raise PracticeError("insufficient_cash", "按已揭晓价格估算的金额超过可用现金", 409)
                order_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO practice_order(order_id,practice_id,idempotency_key,trade_date,ts_code,direction,planned_shares,status,detail_json,created_at)"
                    " VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                    (order_id, practice_id, body["idempotency_key"], trade_date, code, direction,
                     shares, "{}", utc_now()),
                )
                user = dict(session["user_state"])
                user["orders"] = [*user.get("orders", []), {"order_id": order_id, "trade_date": trade_date,
                    "ts_code": code, "direction": direction, "planned_shares": shares, "status": "pending"}]
                compare_and_update(conn, session, user_state=user)
                response = {"ok": True, "session": view(conn, session)}
                save_idempotency(conn, operation=f"order:{practice_id}", key=body["idempotency_key"],
                                 request_hash=request_hash, practice_id=practice_id, response=response)
                return response
        finally:
            conn.close()

    @router.post("/sessions/{practice_id}/advance")
    def advance(request: Request, practice_id: str, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                session = require_session(conn, practice_id)
                replay, request_hash = idempotent_response(conn, operation=f"advance:{practice_id}",
                                                           key=body.get("idempotency_key"), request=body)
                if replay is not None:
                    return replay
                check_revision(session, body.get("expected_revision"))
                date = next_trade_date(conn, session)
                if date is None:
                    raise PracticeError("no_future_trade_date", "该练习区间没有下一交易日", 409)
                try:
                    from app.practice.comparison import advance_paths
                except ImportError as exc:
                    raise PracticeError("comparison_unavailable", "对照引擎尚未就绪", 503) from exc
                advance_result = advance_paths(conn, session, date)
                user_state = refreshed_user_state(conn, session)
                user_state["last_advance"] = advance_result
                compare_and_update(conn, session, revealed_date=date, user_state=user_state)
                response = {"ok": True, "session": view(conn, session)}
                save_idempotency(conn, operation=f"advance:{practice_id}", key=body["idempotency_key"],
                                 request_hash=request_hash, practice_id=practice_id, response=response)
                return response
        finally:
            conn.close()

    @router.post("/sessions/{practice_id}/finish")
    def finish(request: Request, practice_id: str, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                session = require_session(conn, practice_id)
                replay, request_hash = idempotent_response(conn, operation=f"finish:{practice_id}",
                                                           key=body.get("idempotency_key"), request=body)
                if replay is not None:
                    return replay
                check_revision(session, body.get("expected_revision"))
                try:
                    from app.practice.comparison import finish_paths
                except ImportError as exc:
                    raise PracticeError("comparison_unavailable", "对照引擎尚未就绪", 503) from exc
                report = finish_paths(conn, session)
                conn.execute(
                    "UPDATE practice_order SET status='expired',detail_json=? WHERE practice_id=?"
                    " AND status IN ('pending','partially_filled')",
                    (encode_json({"reason": "practice_finished"}), practice_id),
                )
                user_state = refreshed_user_state(conn, session)
                compare_and_update(conn, session, status="finished", user_state=user_state)
                response = {"ok": True, "session": view(conn, session), "report": report}
                save_idempotency(conn, operation=f"finish:{practice_id}", key=body["idempotency_key"],
                                 request_hash=request_hash, practice_id=practice_id, response=response)
                return response
        finally:
            conn.close()

    @router.get("/sessions/{practice_id}/decisions")
    def get_decisions(request: Request, practice_id: str):
        with connect(request) as conn:
            require_session(conn, practice_id)
            return {"decisions": decisions(conn, practice_id)}

    @router.post("/sessions/{practice_id}/decisions")
    def add_decision(request: Request, practice_id: str, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                session = require_session(conn, practice_id)
                replay, request_hash = idempotent_response(conn, operation=f"decision:{practice_id}",
                                                           key=body.get("idempotency_key"), request=body)
                if replay is not None:
                    return replay
                if session["status"] != "active":
                    raise PracticeError("session_finished", "已结束练习只读", 409)
                judgment, evidence, exit_condition = (body.get(k) for k in ("judgment", "evidence", "exit_condition"))
                if any(not isinstance(x, str) or not x.strip() for x in (judgment, evidence, exit_condition)):
                    raise PracticeError("decision_text_required", "判断、依据和退出条件均须填写原文")
                decision_date = parse_date(body.get("decision_date", session["revealed_date"]), "decision_date")
                if decision_date > session["revealed_date"]:
                    raise PracticeError("decision_date_future", "决策记录不能引用尚未揭晓的日期")
                corrects_id = body.get("corrects_decision_id")
                if corrects_id is not None:
                    target = conn.execute(
                        "SELECT 1 FROM practice_decision WHERE decision_id=? AND practice_id=?",
                        (str(corrects_id), practice_id),
                    ).fetchone()
                    if target is None:
                        raise PracticeError("decision_correction_target_missing", "更正目标不属于该练习", 404)
                decision_id, now = str(uuid.uuid4()), utc_now()
                conn.execute(
                    "INSERT INTO practice_decision(decision_id,practice_id,idempotency_key,decision_date,recorded_at,judgment,evidence,exit_condition,is_backfill,corrects_decision_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, practice_id, body["idempotency_key"], decision_date, now,
                     judgment, evidence, exit_condition, int(decision_date < session["revealed_date"]), corrects_id),
                )
                compare_and_update(conn, session)
                response = {"ok": True, "session": view(conn, session)}
                save_idempotency(conn, operation=f"decision:{practice_id}", key=body["idempotency_key"],
                                 request_hash=request_hash, practice_id=practice_id, response=response)
                return response
        finally:
            conn.close()

    @router.get("/sessions/{practice_id}/market")
    def market(request: Request, practice_id: str, code: str):
        with connect(request) as conn:
            session = require_session(conn, practice_id)
            version = VersionView(conn, session["data_version_id"], expected_source_code=session["source_code"])
            points = [
                {"date": str(row["trade_date"]), "open": row["open"], "high": row["high"],
                 "low": row["low"], "close": row["close"], "volume": row["volume"]}
                for row in version.daily_series(code, on_or_before=session["revealed_date"])
            ]
            return {"code": code, "as_of": session["revealed_date"], "points": points,
                    "limitations": [] if points else ["所选版本在揭晓区间没有该证券行情"]}

    @router.get("/sessions/{practice_id}/report")
    def report(request: Request, practice_id: str):
        with connect(request) as conn:
            session = require_session(conn, practice_id)
            comparison = view(conn, session)["comparison"]
            return {"report": {"comparison": comparison, "decisions": decisions(conn, practice_id),
                               "summary": {"as_of": session["revealed_date"], "status": session["status"]},
                               "limitations": comparison.get("limitations", [])}}

    try:
        from app.practice_data.api import create_practice_data_router

        router.include_router(create_practice_data_router())
    except ImportError:
        pass
    return router
