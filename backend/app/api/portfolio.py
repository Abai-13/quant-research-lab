"""阶段三最小业务 API：持仓、建议、模拟、公司行为与绩效。

所有写命令复用阶段三服务/账本/模拟域函数，不在这里重写业务规则。响应中的
``synthetic_demo`` 明确标识离线演示数据；演示使用独立数据库目录，不污染用户库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from ..config import Settings
from ..data.errors import DataError
from ..portfolio.corporate_actions import (
    list_corporate_actions,
    record_corporate_action,
    settle_corporate_action,
)
from ..portfolio.db import portfolio_connect
from ..portfolio.demo import DEMO_ACCOUNT_ID, ensure_demo_database
from ..portfolio.ledger import (
    AccountEventRequest,
    create_account,
    get_account_state,
    get_account_snapshot_state,
    list_account_events,
    list_account_snapshots,
    post_account_event,
    preview_account_event,
)
from ..portfolio.performance import (
    compute_performance_point,
    performance_summary,
)
from ..portfolio.recommendation import (
    create_recommendation_run,
    get_recommendation_run,
    invalidate_recommendation_run,
    list_recommendation_runs,
)
from ..portfolio.rule_bundles import ensure_default_bundles
from ..portfolio.service import DataSelectionContext, bind_data_context
from ..portfolio.simulation import (
    advance_simulation_segment,
    cancel_simulation_order,
    close_simulation_segment,
    create_simulation_segment,
    get_simulation_account_state,
    get_simulation_segment,
    list_simulation_events,
    list_simulation_segments,
    place_simulation_order,
)

__all__ = ["create_portfolio_router"]


def _db_path(settings: Settings, demo: bool) -> Path:
    if demo:
        return Path(str(ensure_demo_database(settings)["db_path"]))
    return settings.db_path


def _conn(request: Request, demo: bool = False):
    settings = request.app.state.settings
    path = _db_path(settings, demo)
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail={"reason": "database_not_initialized", "message": "数据库尚未初始化"},
        )
    return portfolio_connect(path)


def _body_demo(body: dict[str, Any]) -> bool:
    return bool(body.pop("demo", False))


def _event_request(account_id: str, body: dict[str, Any]) -> AccountEventRequest:
    return AccountEventRequest(
        account_id=account_id,
        event_type=str(body.get("event_type", "")),
        business_date=str(body.get("business_date", "")),
        idempotency_key=str(body.get("idempotency_key", "")),
        payload=dict(body.get("payload") or {}),
        supersedes_event_id=body.get("supersedes_event_id"),
        reason=body.get("reason"),
        recorded_at=body.get("recorded_at"),
        source=body.get("source", "manual_entry"),
    )


def create_portfolio_router() -> APIRouter:
    router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

    # ---------------------------------------------------------------- 账户
    @router.post("/accounts")
    def create_account_endpoint(request: Request, body: dict = Body(...)) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = create_account(
                    conn,
                    str(body.get("account_id", "")),
                    str(body.get("display_name", "")),
                )
            return {"ok": True, "account": result, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.get("/accounts/{account_id}")
    def account_state_endpoint(
        request: Request, account_id: str, demo: bool = Query(False)
    ) -> dict:
        conn = _conn(request, demo)
        try:
            return {
                "ok": True,
                "account_id": account_id,
                "state": get_account_state(conn, account_id),
                "synthetic_demo": demo,
            }
        finally:
            conn.close()

    @router.get("/accounts/{account_id}/events")
    def account_events_endpoint(
        request: Request, account_id: str, limit: int = 200, offset: int = 0, demo: bool = Query(False)
    ) -> dict:
        conn = _conn(request, demo)
        try:
            safe_limit = max(1, min(int(limit), 1000))
            safe_offset = max(0, int(offset))
            events = list_account_events(
                conn, account_id, limit=safe_limit, offset=safe_offset
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM account_event WHERE account_id=?",
                (account_id,),
            ).fetchone()[0]
            return {
                "ok": True,
                "events": events,
                "count": int(total),
                "limit": safe_limit,
                "offset": safe_offset,
                "synthetic_demo": demo,
            }
        finally:
            conn.close()

    @router.get("/accounts/{account_id}/snapshots")
    def account_snapshots_endpoint(
        request: Request, account_id: str, limit: int = 50, demo: bool = Query(False)
    ) -> dict:
        conn = _conn(request, demo)
        try:
            snapshots = list_account_snapshots(conn, account_id, limit=max(1, min(limit, 200)))
            return {"ok": True, "snapshots": snapshots, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.get("/accounts/{account_id}/snapshots/{revision}")
    def account_snapshot_state_endpoint(
        request: Request, account_id: str, revision: int, demo: bool = Query(False)
    ) -> dict:
        conn = _conn(request, demo)
        try:
            state = get_account_snapshot_state(conn, account_id, revision=revision)
            if state is None:
                raise HTTPException(status_code=404, detail="snapshot_not_found")
            return {"ok": True, "state": state, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.post("/accounts/{account_id}/events/preview")
    def preview_event_endpoint(
        request: Request, account_id: str, body: dict = Body(...)
    ) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            preview = preview_account_event(conn, _event_request(account_id, body))
            return {"ok": True, "preview": preview.as_dict(), "synthetic_demo": demo}
        finally:
            conn.close()

    @router.post("/accounts/{account_id}/events")
    def post_event_endpoint(
        request: Request, account_id: str, body: dict = Body(...)
    ) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            expected = body.get("expected_account_revision")
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                raise HTTPException(
                    status_code=422,
                    detail={"reason": "expected_account_revision_required"},
                )
            with conn:
                result = post_account_event(
                    conn,
                    _event_request(account_id, body),
                    expected_account_revision=expected,
                )
            return {"ok": True, "result": result.as_dict(), "synthetic_demo": demo}
        finally:
            conn.close()

    # ---------------------------------------------------------------- 建议
    @router.post("/recommendation/runs")
    def recommendation_create_endpoint(request: Request, body: dict = Body(...)) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            account_id = str(body.get("account_id", ""))
            state = get_account_state(conn, account_id)
            context = DataSelectionContext(
                source_code=str(body.get("source_code", "")),
                as_of=str(body.get("as_of", "")),
                purpose="signal_input",
                data_version_id=str(body.get("data_version_id", "")),
                account_revision=int(state["revision"]),
                rule_bundle_id=str(body.get("rule_bundle_id", "")),
                pool_snapshot_id=(
                    None if body.get("pool_snapshot_id") is None else str(body.get("pool_snapshot_id"))
                ),
            )
            bound = bind_data_context(conn, context)
            if demo:
                # 仅离线合成演示：股票池范围旗标未核验，但演示库是独立合成数据，
                # 明确标记 synthetic_demo 后展示完整编排；绝不用于个人主库。
                import dataclasses

                bound = dataclasses.replace(
                    bound, restricted=False, restriction_code=None, restriction_detail=None
                )
            with conn:
                result = create_recommendation_run(
                    conn,
                    bound,
                    account_id=account_id,
                    account_state=state,
                    fee_bundle_id=str(body.get("fee_bundle_id", "")),
                    mode=str(body.get("mode", "incremental")),
                    note=body.get("note"),
                    valid_until=body.get("valid_until"),
                )
            return {"ok": True, **result, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.get("/recommendation/runs")
    def recommendation_list_endpoint(
        request: Request,
        account_id: str,
        mode: str | None = None,
        limit: int = 50,
        offset: int = 0,
        demo: bool = Query(False),
    ) -> dict:
        conn = _conn(request, demo)
        try:
            runs = list_recommendation_runs(
                conn, account_id, mode=mode, limit=limit, offset=offset
            )
            return {"ok": True, "runs": runs, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.get("/recommendation/runs/{run_id}")
    def recommendation_get_endpoint(
        request: Request, run_id: str, demo: bool = Query(False)
    ) -> dict:
        conn = _conn(request, demo)
        try:
            return {"ok": True, "run": get_recommendation_run(conn, run_id), "synthetic_demo": demo}
        finally:
            conn.close()

    @router.post("/recommendation/runs/{run_id}/invalidate")
    def recommendation_invalidate_endpoint(
        request: Request, run_id: str, body: dict = Body(...)
    ) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = invalidate_recommendation_run(
                    conn,
                    run_id,
                    reason=str(body.get("reason", "")),
                    expiry_time=body.get("valid_until"),
                )
            return {"ok": True, **result, "synthetic_demo": demo}
        finally:
            conn.close()

    # ---------------------------------------------------------------- 模拟
    @router.post("/simulation/segments")
    def simulation_create_endpoint(request: Request, body: dict = Body(...)) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = create_simulation_segment(
                    conn,
                    source_account_id=str(body.get("source_account_id", "")),
                    base_account_snapshot_id=str(body.get("base_account_snapshot_id", "")),
                    rule_bundle_id=str(body.get("rule_bundle_id", "")),
                    fee_bundle_id=str(body.get("fee_bundle_id", "")),
                    source_code=str(body.get("source_code", "")),
                    data_version_id=str(body.get("data_version_id", "")),
                    as_of=str(body.get("as_of", "")),
                    simulation_run_key=body.get("simulation_run_key"),
                )
            return {"ok": True, **result, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.get("/simulation/segments")
    def simulation_list_endpoint(
        request: Request,
        source_account_id: str,
        limit: int = 50,
        offset: int = 0,
        demo: bool = Query(False),
    ) -> dict:
        conn = _conn(request, demo)
        try:
            segments = list_simulation_segments(
                conn, source_account_id, limit=limit, offset=offset
            )
            return {
                "ok": True,
                "segments": segments,
                "count": len(segments),
                "limit": min(max(limit, 1), 200),
                "offset": max(offset, 0),
                "synthetic_demo": demo,
            }
        finally:
            conn.close()

    @router.get("/simulation/segments/{segment_id}")
    def simulation_get_endpoint(
        request: Request, segment_id: str, demo: bool = Query(False)
    ) -> dict:
        conn = _conn(request, demo)
        try:
            return {"ok": True, "segment": get_simulation_segment(conn, segment_id), "synthetic_demo": demo}
        finally:
            conn.close()

    @router.get("/simulation/segments/{segment_id}/state")
    def simulation_state_endpoint(
        request: Request, segment_id: str, demo: bool = Query(False)
    ) -> dict:
        conn = _conn(request, demo)
        try:
            return {
                "ok": True,
                "state": get_simulation_account_state(conn, segment_id),
                "events": list_simulation_events(conn, segment_id),
                "synthetic_demo": demo,
            }
        finally:
            conn.close()

    @router.post("/simulation/segments/{segment_id}/orders")
    def simulation_order_endpoint(
        request: Request, segment_id: str, body: dict = Body(...)
    ) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = place_simulation_order(
                    conn,
                    segment_id,
                    ts_code=str(body.get("ts_code", "")),
                    direction=str(body.get("direction", "")),
                    planned_shares=int(body.get("planned_shares", 0)),
                    trade_date=str(body.get("trade_date", "")),
                    idempotency_key=str(body.get("idempotency_key", "")),
                )
            return {"ok": True, **result, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.post("/simulation/segments/{segment_id}/advance")
    def simulation_advance_endpoint(
        request: Request, segment_id: str, body: dict = Body(...)
    ) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = advance_simulation_segment(
                    conn, segment_id, on_trade_date=str(body.get("on_trade_date", ""))
                )
            return {"ok": True, **result, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.post("/simulation/segments/{segment_id}/close")
    def simulation_close_endpoint(
        request: Request, segment_id: str, body: dict = Body(...)
    ) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = close_simulation_segment(
                    conn, segment_id, reason=str(body.get("reason", ""))
                )
            return {"ok": True, **result, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.post("/simulation/orders/{order_id}/cancel")
    def simulation_cancel_endpoint(request: Request, order_id: str, body: dict = Body(...)) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = cancel_simulation_order(
                    conn, order_id, reason=str(body.get("reason", ""))
                )
            return {"ok": True, **result, "synthetic_demo": demo}
        finally:
            conn.close()

    # ---------------------------------------------------------------- 公司行为
    @router.post("/corporate-actions")
    def corporate_action_record_endpoint(request: Request, body: dict = Body(...)) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = record_corporate_action(
                    conn,
                    owner_type=str(body.get("owner_type", "account")),
                    owner_id=str(body.get("owner_id", "")),
                    ts_code=str(body.get("ts_code", "")),
                    action_kind=str(body.get("action_kind", "")),
                    record_date=str(body.get("record_date", "")),
                    entitlement_shares=int(body.get("entitlement_shares", 0)),
                    idempotency_key=str(body.get("idempotency_key", "")),
                    ex_date=body.get("ex_date"),
                    pay_date=body.get("pay_date"),
                    list_date=body.get("list_date"),
                    cash_receivable_cents=body.get("cash_receivable_cents"),
                    tax_status=str(body.get("tax_status", "unknown")),
                    data_version_id=body.get("data_version_id"),
                    note=body.get("note"),
                )
            return {"ok": True, **result, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.post("/corporate-actions/{application_id}/settle")
    def corporate_action_settle_endpoint(
        request: Request, application_id: str, body: dict = Body(...)
    ) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = settle_corporate_action(
                    conn, application_id, as_of=str(body.get("as_of", ""))
                )
            return {"ok": True, **result, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.get("/corporate-actions")
    def corporate_action_list_endpoint(
        request: Request, account_id: str, limit: int = 50, demo: bool = Query(False)
    ) -> dict:
        conn = _conn(request, demo)
        try:
            return {
                "ok": True,
                "applications": list_corporate_actions(conn, account_id, limit=limit),
                "synthetic_demo": demo,
            }
        finally:
            conn.close()

    # ---------------------------------------------------------------- 绩效
    @router.post("/performance/points")
    def performance_point_endpoint(request: Request, body: dict = Body(...)) -> dict:
        demo = _body_demo(body)
        conn = _conn(request, demo)
        try:
            with conn:
                result = compute_performance_point(
                    conn,
                    owner_type=str(body.get("owner_type", "account")),
                    owner_id=str(body.get("owner_id", "")),
                    source_code=str(body.get("source_code", "")),
                    data_version_id=str(body.get("data_version_id", "")),
                    as_of=str(body.get("as_of", "")),
                )
            return {"ok": True, "point": result, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.get("/performance/summary")
    def performance_summary_endpoint(
        request: Request,
        owner_type: str,
        owner_id: str,
        limit: int = 500,
        demo: bool = Query(False),
    ) -> dict:
        conn = _conn(request, demo)
        try:
            return {
                "ok": True,
                "summary": performance_summary(conn, owner_type, owner_id, limit=limit),
                "synthetic_demo": demo,
            }
        finally:
            conn.close()

    # ---------------------------------------------------------------- 默认组合/演示
    @router.get("/defaults")
    def defaults_endpoint(request: Request, demo: bool = Query(False)) -> dict:
        conn = _conn(request, demo)
        try:
            with conn:
                bundles = ensure_default_bundles(conn)
            return {"ok": True, "bundles": bundles, "synthetic_demo": demo}
        finally:
            conn.close()

    @router.get("/demo/bootstrap")
    def demo_bootstrap_endpoint(request: Request) -> dict:
        info = ensure_demo_database(request.app.state.settings)
        return {"ok": True, **info, "synthetic_demo": True, "demo_account_id": DEMO_ACCOUNT_ID}

    return router
