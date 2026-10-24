"""练习数据 HTTP 路由：/data/sources、/data/versions、/data/refresh。

- ``create_practice_data_router()`` 被 core 统一 include（前缀 /api/practice 下
  组装为子路由）；本模块不自行注册 FastAPI app。
- GET 是纯只读；只有 POST /data/refresh 触发挥源取数（baostock 需联网，
  synthetic 完全离线）。
- 练习隔离库连接默认经 core 的 ``app.practice.runtime.practice_connect``；
  core 未合入或测试环境可用 ``set_practice_connect_provider`` 注入连接工厂。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import load_settings
from . import store
from .service import RefreshError, perform_refresh, validate_refresh_payload
from .sources import SOURCE_LIMITATIONS, source_definitions

__all__ = [
    "create_practice_data_router",
    "set_practice_connect_provider",
    "reset_practice_connect_provider",
    "PracticeRuntimeUnavailable",
]

_CONNECT_PROVIDER: Callable[[Any], sqlite3.Connection] | None = None


def set_practice_connect_provider(
    provider: Callable[[Any], sqlite3.Connection] | None,
) -> None:
    """注入练习库连接工厂（核心集成或测试用）；None 恢复默认。"""
    global _CONNECT_PROVIDER
    _CONNECT_PROVIDER = provider


def reset_practice_connect_provider() -> None:
    set_practice_connect_provider(None)


class PracticeRuntimeUnavailable(RuntimeError):
    """core 的练习库运行时不可用（未合入或导入失败）时的显式降级。"""

    reason = "practice_core_runtime_not_installed"


def _default_connect_provider():
    def _provider(settings: Any) -> sqlite3.Connection:
        try:
            from app.practice.runtime import practice_connect
        except Exception as exc:  # noqa: BLE001 - core runtime 未合入即显式降级
            raise PracticeRuntimeUnavailable(
                "需要核心 owner 的 app.practice.runtime.practice_connect；"
                f"导入失败：{exc}"
            ) from exc
        return practice_connect(settings)

    return _provider


def _connect(request: Request) -> sqlite3.Connection:
    settings = getattr(request.app.state, "settings", None) or load_settings()
    provider = _CONNECT_PROVIDER
    if provider is None:
        provider = _default_connect_provider()
    return provider(settings)


def _error(exc: RefreshError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "reason": exc.reason, "detail": exc.detail},
    )


def _svc_error(exc: RuntimeError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "reason": getattr(exc, "reason", "practice_runtime_unavailable"),
            "detail": str(exc),
        },
    )


class RefreshBody(BaseModel):
    source_code: str
    symbols: list[str]
    start_date: str
    end_date: str
    idempotency_key: str


def create_practice_data_router() -> APIRouter:
    router = APIRouter()

    @router.get("/data/sources")
    def data_sources() -> dict:
        """只读：声明练习可用数据源、许可限定与覆盖。任何情况下不联网。"""
        return {
            "ok": True,
            "sources": source_definitions(),
            "limitations": SOURCE_LIMITATIONS,
        }

    @router.get("/data/versions")
    def data_versions(request: Request, limit: int = 20, offset: int = 0):
        """只读：练习库中共享的数据版本（分页有界）。"""
        try:
            conn = _connect(request)
        except RuntimeError as exc:
            return _svc_error(exc)
        try:
            if limit < 1:
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "reason": "invalid_request",
                        "detail": "limit 至少为 1",
                    },
                )
            safe_limit = min(int(limit), 100)
            safe_offset = max(0, int(offset))
            versions, total = store.list_versions(conn, safe_limit, safe_offset)
            return {
                "ok": True,
                "versions": versions,
                "total": total,
                "limit": safe_limit,
                "offset": safe_offset,
            }
        finally:
            conn.close()

    @router.post("/data/refresh")
    def data_refresh(request: Request, body: RefreshBody):
        """显式刷新：唯一会触发网络取数的入口（仅 baostock 源）。"""
        try:
            normalized = validate_refresh_payload(body.model_dump())
        except RefreshError as exc:
            return _error(exc)
        try:
            conn = _connect(request)
        except RuntimeError as exc:
            return _svc_error(exc)
        try:
            response, reused = perform_refresh(conn, normalized)
        except RefreshError as exc:
            return _error(exc)
        finally:
            conn.close()
        response["reused"] = reused or bool(response.get("reused"))
        return response

    return router
