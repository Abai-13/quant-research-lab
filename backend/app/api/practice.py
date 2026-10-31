"""历史练习 API：隔离练习库、冻结数据版本和显式单日推进。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

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


class ResearchSourceContext(BaseModel):
    """Exact source binding accepted by the frozen research-plan contract."""

    model_config = ConfigDict(extra="forbid")

    practice_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    revealed_date: str = Field(pattern=r"^\d{8}$")
    data_version_id: str = Field(min_length=1, max_length=200)
    source_code: str = Field(min_length=1, max_length=100)
    source_instance_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    settings_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    algorithm_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fee_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResearchContextAvailable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True]
    availability: Literal["available"]
    source_context: ResearchSourceContext


class ResearchContextSupplementationRequired(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True]
    availability: Literal["supplementation_required"]
    reason: str
    missing_fields: list[str]


ResearchContextResponse = ResearchContextAvailable | ResearchContextSupplementationRequired


_RESEARCH_SOURCE_FIELDS = (
    "practice_id", "revision", "revealed_date", "data_version_id", "source_code",
    "source_instance_id", "settings_fingerprint", "algorithm_fingerprint",
    "fee_fingerprint", "segment_fingerprint",
)


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
            scope={
                "kind": "explicit_practice_synthetic_demo",
                "seed_version_id": data_version_id,
                "simulation_rule_bundle_id": str(seeded["simulation_rule_bundle_id"]),
                "parameter_bundle_id": str(seeded["parameter_bundle_id"]),
            },
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


def _explicit_demo_rule_bundle(conn: sqlite3.Connection, version_id: str, source_code: str) -> str | None:
    """Return the bound rule only for the explicitly installed synthetic demo version."""
    if source_code != "tushare-fixture":
        return None
    row = conn.execute(
        "SELECT scope FROM data_version WHERE version_id=? AND source_code=? AND status='published'",
        (version_id, source_code),
    ).fetchone()
    if row is None:
        return None
    try:
        scope = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    if not isinstance(scope, dict) or scope.get("kind") != "explicit_practice_synthetic_demo":
        return None
    bundle_id = scope.get("simulation_rule_bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id.strip():
        return None
    bundle = load_rule_bundle(conn, bundle_id)
    if (
        bundle is None
        or bundle.get("kind") != "parameter"
        or bundle.get("assumption_scope") != "synthetic_simulation"
        or not isinstance(bundle.get("content"), dict)
        or not isinstance(bundle["content"].get("market_rules"), dict)
    ):
        return None
    return bundle_id


def _explicit_demo_parameter_bundle(conn, version_id, source_code):
    if source_code != "tushare-fixture": return None
    row = conn.execute("SELECT scope FROM data_version WHERE version_id=? AND source_code=?", (version_id, source_code)).fetchone()
    scope = decode_json(row[0]) if row else {}
    if scope.get("kind") != "explicit_practice_synthetic_demo": return None
    bundle_id = scope.get("parameter_bundle_id")
    bundle = load_rule_bundle(conn, bundle_id) if bundle_id else None
    if (bundle and bundle["kind"] == "parameter" and bundle["assumption_scope"] == "synthetic_simulation"
            and isinstance(bundle["content"].get("target_constraints"), dict)):
        return bundle_id
    return None


def create_practice_router() -> APIRouter:
    router = APIRouter(prefix="/api/practice", tags=["practice"])

    def place_order_transaction(conn: sqlite3.Connection, practice_id: str, body: dict[str, Any], settings: Any):
        session = require_session(conn, practice_id)
        replay, request_hash = idempotent_response(conn, operation=f"order:{practice_id}", key=body.get("idempotency_key"), request=body)
        if replay is not None: return replay
        check_revision(session, body.get("expected_revision"))
        trade_date = next_trade_date(conn, session)
        if trade_date is None: raise PracticeError("no_future_trade_date", "该练习区间没有下一个交易日", 409)
        preview = order_preview(conn, session, body)
        if preview["direction"] == "buy" and preview["cash_sufficient_at_known_price"] is False:
            raise PracticeError("insufficient_cash", "按已揭晓价格估算的金额超过可用现金", 409)
        order_id = str(uuid.uuid4())
        conn.execute("INSERT INTO practice_order(order_id,practice_id,idempotency_key,trade_date,ts_code,direction,planned_shares,status,detail_json,created_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                     (order_id, practice_id, body["idempotency_key"], trade_date, str(preview["ts_code"]), preview["direction"], preview["planned_shares"], "{}", utc_now()))
        user = dict(session["user_state"])
        user["orders"] = [*user.get("orders", []), {"order_id": order_id, "trade_date": trade_date, "ts_code": str(preview["ts_code"]), "direction": preview["direction"], "planned_shares": preview["planned_shares"], "status": "pending"}]
        compare_and_update(conn, session, user_state=user)
        response = {"ok": True, "session": view(conn, session, settings)}
        save_idempotency(conn, operation=f"order:{practice_id}", key=body["idempotency_key"], request_hash=request_hash, practice_id=practice_id, response=response)
        return response

    def advance_transaction(conn: sqlite3.Connection, practice_id: str, body: dict[str, Any], settings: Any):
        session = require_session(conn, practice_id)
        replay, request_hash = idempotent_response(conn, operation=f"advance:{practice_id}", key=body.get("idempotency_key"), request=body)
        if replay is not None: return replay
        check_revision(session, body.get("expected_revision"))
        date = next_trade_date(conn, session)
        if date is None: raise PracticeError("no_future_trade_date", "该练习区间没有下一个交易日", 409)
        try:
            from app.practice.comparison import advance_paths
        except ImportError as exc:
            raise PracticeError("comparison_unavailable", "对照引擎尚未就绪", 503) from exc
        result = advance_paths(conn, session, date)
        user_state = refreshed_user_state(conn, session)
        user_state["last_advance"] = result
        compare_and_update(conn, session, revealed_date=date, user_state=user_state)
        response = {"ok": True, "session": view(conn, session, settings)}
        save_idempotency(conn, operation=f"advance:{practice_id}", key=body["idempotency_key"], request_hash=request_hash, practice_id=practice_id, response=response)
        return response

    router.practice_actions = {"order": place_order_transaction, "advance": advance_transaction}

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
        coverage_start = coverage_end = None
        limitations = [
            "练习行情只读取所选数据版本；不会在普通读取请求中联网更新。",
            "历史股票池、停牌和公司行为能力取决于已发布数据版本的实际覆盖。",
        ]
        for version in versions:
            try:
                view = VersionView(conn, version["version_id"], expected_source_code=version["source_code"])
            except Exception:
                continue
            calendar = view.calendar_days()
            if view.chunked:
                daily_rows = conn.execute(
                    "SELECT DISTINCT m.ts_code,m.biz_date FROM data_version_chunk vc JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id WHERE vc.version_id=? AND vc.category='daily'",
                    (version["version_id"],),
                ).fetchall()
            else:
                daily_rows = conn.execute(
                    "SELECT DISTINCT ts_code,trade_date FROM daily_price WHERE source_code=?", (version["source_code"],)
                ).fetchall()
            visible_dates = sorted(set(calendar).intersection(str(row[1]) for row in daily_rows))
            version["start_date"] = visible_dates[0] if visible_dates else None
            version["end_date"] = visible_dates[-1] if visible_dates else None
        default_version = next((item for item in reversed(versions) if item.get("start_date") and item.get("end_date")), None)
        if default_version:
            selected = VersionView(conn, default_version["version_id"], expected_source_code=default_version["source_code"])
            daily_codes = {str(row[0]) for row in conn.execute(
                "SELECT DISTINCT m.ts_code FROM data_version_chunk vc JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id WHERE vc.version_id=? AND vc.category='daily'" if selected.chunked else
                "SELECT DISTINCT ts_code FROM daily_price WHERE source_code=?", (default_version["version_id"] if selected.chunked else default_version["source_code"],)
            ).fetchall()}
            symbols = [{"ts_code": str(row["ts_code"]), "name": row["name"]}
                       for row in selected.securities() if str(row["ts_code"]) in daily_codes]
            coverage_start, coverage_end = default_version["start_date"], default_version["end_date"]
        return {
            "sources": sources,
            "versions": versions,
            "symbols": symbols,
            "defaults": {
                "cash_cents": 1_000_000,
                "rebalance_every_n_days": 5,
                "data_version_id": default_version["version_id"] if default_version else (versions[-1]["version_id"] if versions else None),
                "source_code": default_version["source_code"] if default_version else (versions[-1]["source_code"] if versions else None),
                "start_date": coverage_start,
                "end_date": coverage_end,
            },
            "demo_available": True,
            "limitations": limitations,
        }

    def view(conn: sqlite3.Connection, session: dict[str, Any], settings=None) -> dict[str, Any]:
        if settings is not None:
            from app.research.exposure import capture_practice_exposure
            capture_practice_exposure(settings, conn, session)
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
        demo_rule_bundle_id = _explicit_demo_rule_bundle(conn, version_id, source_code)
        settings = {
            "rebalance_every_n_days": 5,
            "parameter_bundle_id": _explicit_demo_parameter_bundle(conn, version_id, source_code) or bundles["parameter"],
            "rule_bundle_id": supplied.get("rule_bundle_id", demo_rule_bundle_id or bundles["parameter"]),
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
        if type(settings.get("rebalance_every_n_days")) is not int or settings.get("rebalance_every_n_days") not in {5, 10, 20}:
            raise PracticeError("rebalance_schedule_invalid", "调仓间隔仅支持已验证的 5、10、20 个交易日")
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

    def plan_qualification(conn: sqlite3.Connection, payload: dict[str, Any], symbols: list[str]) -> dict[str, Any]:
        issues: list[dict[str, str]] = []
        limitations: list[str] = []
        start = end = None
        try:
            start = parse_date(payload.get("start_date"), "start_date")
            end = parse_date(payload.get("end_date"), "end_date")
            if end < start:
                raise PracticeError("date_range_invalid", "结束日期不能早于开始日期")
            if not symbols or len(symbols) > 20 or len(set(symbols)) != len(symbols):
                raise PracticeError("symbols_invalid", "请选择 1 至 20 只不重复的股票")
            if any(not isinstance(code, str) or not code.strip() for code in symbols):
                raise PracticeError("symbols_invalid", "股票代码无效")
            data = dict(payload)
            data["name"] = data.get("name") or "我的投资练习"
            data["positions"] = data.get("positions") or []
            data["settings"] = {**(data.get("settings") or {}), "watchlist_symbols": symbols}
            conn.execute("SAVEPOINT plan_qualification")
            try:
                session = create_session(conn, payload=data)
                market = VersionView(conn, session["data_version_id"], expected_source_code=session["source_code"])
                calendar = market.calendar_days()
                if market.chunked:
                    daily_dates = {str(row[0]) for row in conn.execute(
                        "SELECT DISTINCT m.biz_date FROM data_version_chunk vc JOIN data_chunk_member m ON m.chunk_id=vc.chunk_id WHERE vc.version_id=? AND vc.category='daily'",
                        (session["data_version_id"],),
                    ).fetchall()}
                else:
                    daily_dates = {str(row[0]) for row in conn.execute(
                        "SELECT DISTINCT trade_date FROM daily_price WHERE source_code=?", (session["source_code"],)
                    ).fetchall()}
                covered_dates = set(calendar).intersection(daily_dates)
                if start not in covered_dates or end not in covered_dates:
                    raise PracticeError("date_coverage_unavailable", "开始和结束日期必须同时位于开市日与日线实际覆盖范围内", 422)
                available = {str(item["ts_code"]) for item in market.securities()}
                missing = [code for code in symbols if code not in available]
                if missing:
                    raise PracticeError("symbol_unavailable", "所选股票不属于当前数据版本：" + ", ".join(missing), 422)
                for code in symbols:
                    daily_rows = market.daily_series(code, on_or_before=end)
                    if not any(start <= str(row["trade_date"]) <= end for row in daily_rows):
                        raise PracticeError("symbol_daily_unavailable", f"{code} 在所选日期范围没有日线", 422)
                if session["source_code"] in {"synthetic", "tushare-fixture"}:
                    limitations.append("当前数据为明确标注的合成演示数据。")
            except PracticeError:
                raise
            finally:
                conn.execute("ROLLBACK TO plan_qualification")
                conn.execute("RELEASE plan_qualification")
        except PracticeError as exc:
            issues.append({"code": exc.reason, "detail": str(exc)})
        except (TypeError, ValueError, sqlite3.Error) as exc:
            issues.append({"code": "qualification_unavailable", "detail": str(exc)[:200]})
        return {"ready": not issues, "issues": issues, "limitations": limitations,
                "start_date": start, "end_date": end}

    def plan_response(conn: sqlite3.Connection, plan_id: str, version: int | None = None) -> dict[str, Any]:
        head = conn.execute("SELECT current_version,status,practice_id,created_at,updated_at FROM practice_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if head is None:
            raise PracticeError("plan_not_found", "方案不存在", 404)
        selected = int(head[0]) if version is None else version
        row = conn.execute("SELECT status,payload_json,symbols_json,goal,practice_id,qualification_json,created_at FROM practice_plan_version WHERE plan_id=? AND version=?", (plan_id, selected)).fetchone()
        if row is None:
            raise PracticeError("plan_version_not_found", "方案版本不存在", 404)
        return {"plan_id": plan_id, "version": selected, "status": str(row[0]),
                "payload": decode_json(str(row[1])), "symbols": decode_json(str(row[2])), "goal": str(row[3]),
                "practice_id": row[4], "created_at": str(row[6]), "updated_at": str(head[4]),
                "qualification": decode_json(str(row[5]))}

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
            # Persist copied immutable rule bundles together with the installed demo version.
            with conn:
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

    @router.post("/plans")
    def create_plan(request: Request, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                key = body.get("idempotency_key")
                if not isinstance(key, str) or not 1 <= len(key.strip()) <= 160:
                    raise PracticeError("idempotency_key_required", "必须提供幂等键")
                request_hash = hashlib.sha256(encode_json(body).encode("utf-8")).hexdigest()
                old = conn.execute("SELECT request_sha256,response_json FROM practice_plan_idempotency WHERE operation='create_plan' AND idempotency_key=?", (key,)).fetchone()
                if old:
                    if old[0] != request_hash:
                        raise PracticeError("idempotency_conflict", "幂等键已用于不同请求", 409)
                    return decode_json(str(old[1]))
                payload = body.get("payload")
                symbols = body.get("symbols")
                goal = body.get("goal", "")
                if not isinstance(payload, dict) or not isinstance(symbols, list) or not isinstance(goal, str) or len(goal) > 8000 or len(encode_json(payload).encode("utf-8")) > 24_000 or len(symbols) > 20 or any(not isinstance(code, str) or len(code) > 40 for code in symbols):
                    raise PracticeError("plan_invalid", "方案内容无效")
                plan_id, now = str(uuid.uuid4()), utc_now()
                qualification = plan_qualification(conn, payload, symbols)
                conn.execute("INSERT INTO practice_plan(plan_id,current_version,status,created_at,updated_at) VALUES(?,1,'draft',?,?)", (plan_id, now, now))
                conn.execute("INSERT INTO practice_plan_version(plan_id,version,status,payload_json,symbols_json,goal,qualification_json,created_at) VALUES(?,1,'draft',?,?,?,?,?)",
                             (plan_id, encode_json(payload), encode_json(symbols), goal, encode_json(qualification), now))
                result = plan_response(conn, plan_id)
                conn.execute("INSERT INTO practice_plan_idempotency VALUES('create_plan',?,?,?,?)", (key, request_hash, encode_json(result), now))
                return result
        finally:
            conn.close()

    @router.get("/plans/{plan_id}")
    def get_plan(request: Request, plan_id: str, version: int | None = Query(None, ge=1)):
        with connect(request) as conn:
            return plan_response(conn, plan_id, version)

    @router.put("/plans/{plan_id}")
    def update_plan(request: Request, plan_id: str, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                expected, key = body.get("expected_version"), body.get("idempotency_key")
                if type(expected) is not int or not isinstance(key, str) or not 1 <= len(key.strip()) <= 160:
                    raise PracticeError("plan_invalid", "expected_version 和幂等键必填")
                request_hash = hashlib.sha256(encode_json({"plan_id": plan_id, **body}).encode("utf-8")).hexdigest()
                old = conn.execute("SELECT request_sha256,response_json FROM practice_plan_idempotency WHERE operation='update_plan' AND idempotency_key=?", (key,)).fetchone()
                if old:
                    if old[0] != request_hash: raise PracticeError("idempotency_conflict", "幂等键已用于不同请求", 409)
                    return decode_json(str(old[1]))
                head = conn.execute("SELECT current_version,status FROM practice_plan WHERE plan_id=?", (plan_id,)).fetchone()
                if head is None: raise PracticeError("plan_not_found", "方案不存在", 404)
                if head[0] != expected: raise PracticeError("plan_version_conflict", "方案已更新，请重新读取", 409)
                if head[1] != "draft": raise PracticeError("plan_confirmed", "已确认方案不可修改", 409)
                payload, symbols, goal = body.get("payload"), body.get("symbols"), body.get("goal", "")
                if not isinstance(payload, dict) or not isinstance(symbols, list) or not isinstance(goal, str) or len(goal) > 8000 or len(encode_json(payload).encode("utf-8")) > 24_000 or len(symbols) > 20 or any(not isinstance(code, str) or len(code) > 40 for code in symbols):
                    raise PracticeError("plan_invalid", "方案内容无效")
                qualification = plan_qualification(conn, payload, symbols)
                version, now = expected + 1, utc_now()
                conn.execute("UPDATE practice_plan SET current_version=?,updated_at=? WHERE plan_id=? AND current_version=? AND status='draft'", (version, now, plan_id, expected))
                conn.execute("INSERT INTO practice_plan_version(plan_id,version,status,payload_json,symbols_json,goal,qualification_json,created_at) VALUES(?,?,'draft',?,?,?,?,?)",
                             (plan_id, version, encode_json(payload), encode_json(symbols), goal, encode_json(qualification), now))
                result = plan_response(conn, plan_id)
                conn.execute("INSERT INTO practice_plan_idempotency VALUES('update_plan',?,?,?,?)", (key, request_hash, encode_json(result), now))
                return result
        finally:
            conn.close()

    @router.post("/plans/{plan_id}/confirm")
    def confirm_plan(request: Request, plan_id: str, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                expected, key = body.get("expected_version"), body.get("idempotency_key")
                if type(expected) is not int or not isinstance(key, str) or not 1 <= len(key.strip()) <= 160:
                    raise PracticeError("plan_invalid", "expected_version 和幂等键必填")
                request_hash = hashlib.sha256(encode_json({"plan_id": plan_id, **body}).encode("utf-8")).hexdigest()
                old = conn.execute("SELECT request_sha256,response_json FROM practice_plan_idempotency WHERE operation='confirm_plan' AND idempotency_key=?", (key,)).fetchone()
                if old:
                    if old[0] != request_hash: raise PracticeError("idempotency_conflict", "幂等键已用于不同请求", 409)
                    return decode_json(str(old[1]))
                head = conn.execute("SELECT current_version,status FROM practice_plan WHERE plan_id=?", (plan_id,)).fetchone()
                if head is None: raise PracticeError("plan_not_found", "方案不存在", 404)
                if head[0] != expected: raise PracticeError("plan_version_conflict", "方案已更新，请重新读取", 409)
                plan = plan_response(conn, plan_id)
                if plan["status"] == "confirmed":
                    session = view(conn, require_session(conn, plan["practice_id"]), request.app.state.settings)
                    response = {"plan": plan, "session": session}
                else:
                    qualification = plan_qualification(conn, plan["payload"], plan["symbols"])
                    conn.execute("UPDATE practice_plan_version SET qualification_json=? WHERE plan_id=? AND version=?", (encode_json(qualification), plan_id, expected))
                    if not qualification["ready"]:
                        raise PracticeError("plan_not_qualified", "方案尚未满足创建条件", 422)
                    payload = dict(plan["payload"])
                    payload["settings"] = {**(payload.get("settings") or {}), "watchlist_symbols": plan["symbols"]}
                    session_row = create_session(conn, payload=payload)
                    session = view(conn, session_row, request.app.state.settings)
                    now = utc_now()
                    conn.execute("UPDATE practice_plan SET status='confirmed',practice_id=?,updated_at=? WHERE plan_id=? AND current_version=? AND status='draft'", (session_row["practice_id"], now, plan_id, expected))
                    conn.execute("UPDATE practice_plan_version SET status='confirmed',practice_id=? WHERE plan_id=? AND version=?", (session_row["practice_id"], plan_id, expected))
                    response = {"plan": plan_response(conn, plan_id), "session": session}
                conn.execute("INSERT INTO practice_plan_idempotency VALUES('confirm_plan',?,?,?,?)", (key, request_hash, encode_json(response), utc_now()))
                return response
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
                response = {"ok": True, "session": view(conn, session, request.app.state.settings)}
                save_idempotency(conn, operation="create", key=body["idempotency_key"],
                                 request_hash=request_hash, practice_id=session["practice_id"], response=response)
                return response
        finally:
            conn.close()

    @router.get("/sessions/{practice_id}")
    def get_session(request: Request, practice_id: str):
        with connect(request) as conn:
            return {"session": view(conn, require_session(conn, practice_id), request.app.state.settings)}

    @router.get("/sessions/{practice_id}/workspace")
    def workspace(request: Request, practice_id: str):
        from decimal import Decimal, ROUND_HALF_UP
        with connect(request) as conn:
            session = require_session(conn, practice_id)
            state = refreshed_user_state(conn, session)
            market = VersionView(conn, session["data_version_id"], expected_source_code=session["source_code"])
            as_of = str(session["revealed_date"])
            calendar = market.calendar_days(on_or_before=as_of)
            securities = {str(item["ts_code"]): item for item in market.securities()}
            all_codes = list(dict.fromkeys(
                list(session["settings"].get("watchlist_symbols", []))
                + [str(item["ts_code"]) for item in state["positions"]]
                + [str(item["ts_code"]) for item in state["orders"]]
            ))
            codes = all_codes[:40]
            limitations: list[str] = []
            if len(all_codes) > len(codes):
                limitations.append(f"当前相关证券共 {len(all_codes)} 只，概览最多显示 40 只；其余请在订单或持仓详情查看。")
            agent_actions: dict[str, str] = {}
            store = getattr(request.app.state, "agent_store", None)
            if store is not None and getattr(store, "path", None) is not None and store.path.is_file():
                tasks, _ = store.list(practice_id, 100, 0, scope="simulation")
                for task in tasks:
                    context = task.get("context") or {}
                    task_date = context.get("revealed_date")
                    if (task.get("status") != "completed" or task.get("stale")
                            or context.get("practice_id") != practice_id
                            or not isinstance(task_date, str) or task_date > as_of
                            or context.get("revision", 0) > session["revision"]):
                        continue
                    for draft in ((task.get("result") or {}).get("drafts") or []):
                        draft_payload = draft.get("payload") or {}
                        code = draft_payload.get("ts_code")
                        if (draft.get("kind") != "trade" or code not in codes or code in agent_actions
                                or draft.get("revealed_date") != task_date or draft.get("practice_id") != practice_id):
                            continue
                        direction = draft_payload.get("direction")
                        verb = "买入" if direction == "buy" else "卖出" if direction == "sell" else "交易"
                        agent_actions[code] = f"建议{verb}，尚需你确认（{task_date}）"
            rows: list[dict[str, Any]] = []
            for code in codes:
                daily = market.daily_on(code, as_of)
                price = None
                if daily is not None and daily["close"] is not None:
                    price = int((Decimal(str(daily["close"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                prior_day = None
                prior = None
                if as_of in calendar:
                    index = calendar.index(as_of)
                    for candidate in reversed(calendar[max(0, index - 250):index]):
                        prior = market.daily_on(code, candidate)
                        if prior is not None and prior["close"] is not None and Decimal(str(prior["close"])) > 0:
                            prior_day = candidate
                            break
                change_bps = None
                if price is not None and prior is not None and prior["close"] is not None and Decimal(str(prior["close"])) != 0:
                    change_bps = int((((Decimal(str(daily["close"])) / Decimal(str(prior["close"]))) - 1) * 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                position = next((item for item in state["positions"] if item["ts_code"] == code), None)
                shares = int(position["shares"]) if position else 0
                sellable = int(position.get("sellable_shares", 0)) if position else 0
                value = shares * price if price is not None else None
                cost = position.get("total_cost_cents") if position else 0
                pnl = value - int(cost) if value is not None and cost is not None else None
                security = securities.get(code)
                rows.append({"ts_code": code, "name": None if security is None else security["name"],
                             "price_cents": price, "price_date": as_of if price is not None else None,
                             "change_bps": change_bps, "change_base_date": prior_day if change_bps is not None else None,
                             "shares": shares, "sellable_shares": sellable,
                             "market_value_cents": value, "pnl_cents": pnl,
                             "last_agent_action": agent_actions.get(code)})
                if price is None: limitations.append(f"{code} 在已揭晓日 {as_of} 没有可用收盘价。")
            return {"practice_id": practice_id, "revision": int(session["revision"]), "as_of": as_of,
                    "next_trade_date": next_trade_date(conn, session),
                    "source": {"code": session["source_code"], "version_id": session["data_version_id"]},
                    "rows": rows, "limitations": limitations}

    @router.get("/sessions/{practice_id}/research-context", response_model=ResearchContextResponse)
    def research_context(request: Request, practice_id: str, expected_revision: int = Query(..., ge=1)):
        """Return an exact frozen-plan source binding from the local practice snapshot.

        This endpoint deliberately opens an existing practice database read-only;
        it neither initializes storage nor asks an Agent/model for context.
        """
        from pathlib import Path

        from app.config import load_settings
        from app.data.errors import DataError
        from app.practice.core import PracticeError
        from app.practice.runtime import practice_settings

        def unavailable(reason: str, missing: list[str]) -> dict[str, Any]:
            return {
                "ok": True, "availability": "supplementation_required",
                "reason": reason, "missing_fields": missing,
            }
        settings = getattr(request.app.state, "settings", None) or load_settings()
        path = practice_settings(settings).db_path
        if not path.is_file():
            return JSONResponse(status_code=404, content={"ok": False, "reason": "practice_not_found"})
        try:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=3.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                session = require_session(conn, practice_id)
                if int(session["revision"]) != expected_revision:
                    return JSONResponse(status_code=409, content={"ok": False, "reason": "revision_conflict"})

                segment_id = (session.get("segments") or {}).get("user")
                segment = conn.execute(
                    "SELECT rule_bundle_id,fee_bundle_id,input_sha256 FROM simulation_segment WHERE segment_id=?",
                    (segment_id,),
                ).fetchone() if segment_id else None
                if segment is None:
                    return unavailable("historical_context_incomplete", [
                        "algorithm_fingerprint", "fee_fingerprint", "segment_fingerprint",
                    ])

                from app.portfolio.rule_bundles import load_rule_bundle

                rule = load_rule_bundle(conn, str(segment[0]))
                fee = load_rule_bundle(conn, str(segment[1]))
                if rule is None or rule.get("kind") != "parameter":
                    return unavailable("historical_context_incomplete", ["algorithm_fingerprint"])
                if fee is None or fee.get("kind") != "fee":
                    return unavailable("historical_context_incomplete", ["fee_fingerprint"])
                settings_value = session.get("settings")
                if not isinstance(settings_value, dict):
                    return unavailable("historical_context_incomplete", ["settings_fingerprint"])

                try:
                    market = VersionView(conn, str(session["data_version_id"]), expected_source_code=str(session["source_code"]))
                    calendar = market.calendar_days(on_or_before=str(session["revealed_date"]))
                    if str(session["revealed_date"]) not in calendar:
                        return unavailable("source_data_time_unavailable", ["revealed_date", "data_version_id"])
                    if not market.securities():
                        return unavailable("source_data_unavailable", ["data_version_id"])
                except (DataError, sqlite3.Error, ValueError, TypeError):
                    return unavailable("source_data_unavailable", ["data_version_id", "source_code"])

                settings_json = json.dumps(settings_value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                source_context = ResearchSourceContext(
                    practice_id=str(session["practice_id"]),
                    revision=int(session["revision"]),
                    revealed_date=str(session["revealed_date"]),
                    data_version_id=str(session["data_version_id"]),
                    source_code=str(session["source_code"]),
                    source_instance_id=hashlib.sha256(str(settings.runtime_dir.resolve()).encode("utf-8")).hexdigest(),
                    settings_fingerprint=hashlib.sha256(settings_json.encode("utf-8")).hexdigest(),
                    algorithm_fingerprint=str(rule["content_sha256"]),
                    fee_fingerprint=str(fee["content_sha256"]),
                    segment_fingerprint=str(segment[2]),
                )
                return {
                    "ok": True, "availability": "available",
                    "source_context": source_context.model_dump(),
                }
            finally:
                conn.close()
        except PracticeError as exc:
            if exc.reason == "session_not_found":
                return JSONResponse(status_code=404, content={"ok": False, "reason": "practice_not_found"})
            return unavailable("historical_context_incomplete", list(_RESEARCH_SOURCE_FIELDS))
        except (sqlite3.Error, OSError, DataError, ValueError, TypeError):
            return unavailable("practice_context_unavailable", list(_RESEARCH_SOURCE_FIELDS))

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
                response = {"ok": True, "session": view(conn, new, request.app.state.settings)}
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
                return place_order_transaction(conn, practice_id, body, request.app.state.settings)
        finally:
            conn.close()

    @router.post("/sessions/{practice_id}/advance")
    def advance(request: Request, practice_id: str, body: dict[str, Any]):
        conn = connect(request)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                return advance_transaction(conn, practice_id, body, request.app.state.settings)
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
                response = {"ok": True, "session": view(conn, session, request.app.state.settings), "report": report}
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
                response = {"ok": True, "session": view(conn, session, request.app.state.settings)}
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
            from app.research.exposure import capture_market_query
            capture_market_query(request.app.state.settings, session, code, [str(item["date"]) for item in points])
            return {"code": code, "as_of": session["revealed_date"], "points": points,
                    "limitations": [] if points else ["所选版本在揭晓区间没有该证券行情"]}

    @router.get("/sessions/{practice_id}/report")
    def report(request: Request, practice_id: str):
        with connect(request) as conn:
            session = require_session(conn, practice_id)
            comparison = view(conn, session, request.app.state.settings)["comparison"]
            return {"report": {"comparison": comparison, "decisions": decisions(conn, practice_id),
                               "summary": {"as_of": session["revealed_date"], "status": session["status"]},
                               "limitations": comparison.get("limitations", [])}}

    try:
        from app.practice_data.api import create_practice_data_router

        router.include_router(create_practice_data_router())
        from app.practice.governance import create_storage_router
        router.include_router(create_storage_router())
    except ImportError:
        pass
    return router
