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
from . import operations

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

    @router.get("/data/presets")
    def data_presets() -> dict:
        """可编辑的历史范围样例；只读且不连接数据源。"""
        return {
            "ok": True,
            "presets": operations.PRESETS,
            "limits": {"max_symbols": 20, "max_calendar_days": 366, "max_daily_rows": 20000,
                       "default_timeout_seconds": 300, "hard_timeout_seconds": 900},
        }

    @router.post("/data/prepare")
    def data_prepare(request: Request, body: dict[str, Any]):
        """排入异步历史准备任务；真实来源失败保持失败，不换合成源。"""
        try:
            payload = dict(body)
            preset_id = payload.get("preset_id")
            if preset_id is not None:
                preset = next((item for item in operations.PRESETS if item["preset_id"] == preset_id), None)
                if preset is None:
                    raise RefreshError("unknown_preset", f"未知历史范围预设 {preset_id!r}")
                for key in ("symbols", "start_date", "end_date", "source_code"):
                    payload.setdefault(key, preset[key])
            payload.setdefault("source_code", "baostock")
            refresh_mode = payload.get("refresh_mode", "use_cache")
            refresh_mode = {"reuse_cache": "use_cache", "refresh": "force_refresh"}.get(refresh_mode, refresh_mode)
            if refresh_mode == "synthetic" and payload.get("source_code") == "synthetic":
                refresh_mode = "use_cache"
            if refresh_mode not in {"use_cache", "force_refresh"}:
                raise RefreshError("invalid_refresh_mode", "refresh_mode 须为 reuse_cache/use_cache 或 refresh/force_refresh")
            normalized = validate_refresh_payload(payload)
            normalized["refresh_mode"] = refresh_mode
            timeout = int(body.get("timeout_seconds", operations.DEFAULT_TIMEOUT_SECONDS))
        except (RefreshError, TypeError, ValueError) as exc:
            if isinstance(exc, RefreshError):
                return _error(exc)
            return JSONResponse(status_code=400, content={"ok": False, "reason": "invalid_timeout", "detail": str(exc)})
        try:
            # Capture the selected isolated practice connection factory. The worker
            # opens a fresh connection for each progress update and publication.
            settings = getattr(request.app.state, "settings", None) or load_settings()
            provider = _CONNECT_PROVIDER or _default_connect_provider()
            conn_factory = lambda: provider(settings)
            result = operations.submit_job(conn_factory, normalized, timeout_seconds=timeout, settings=settings)
        except RefreshError as exc:
            return _error(exc)
        except RuntimeError as exc:
            return _svc_error(exc)
        return JSONResponse(status_code=202, content={"ok": True, **result})

    @router.get("/data/jobs/{job_id}")
    def data_job(request: Request, job_id: str):
        try:
            conn = _connect(request)
        except RuntimeError as exc:
            return _svc_error(exc)
        try:
            job = operations.get_job(conn, job_id)
            if job is None:
                return JSONResponse(status_code=404, content={"ok": False, "reason": "job_not_found"})
            return {"ok": True, **job}
        finally:
            conn.close()

    @router.post("/data/jobs/{job_id}/cancel")
    def data_job_cancel(request: Request, job_id: str, body: dict[str, Any]):
        try:
            generation = int(body.get("expected_generation", body.get("generation")))
            if not isinstance(body.get("idempotency_key"), str) or not body["idempotency_key"].strip():
                return JSONResponse(status_code=400, content={"ok": False, "reason": "idempotency_key_required"})
            settings = getattr(request.app.state, "settings", None) or load_settings()
            provider = _CONNECT_PROVIDER or _default_connect_provider()
            job = operations.cancel_job(lambda: provider(settings), job_id, generation, body["idempotency_key"].strip())
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"ok": False, "reason": "generation_required"})
        except RefreshError as exc:
            return _error(exc)
        except RuntimeError as exc:
            return _svc_error(exc)
        if job is None:
            return JSONResponse(status_code=409, content={"ok": False, "reason": "generation_conflict"})
        return {"ok": True, **job}

    @router.post("/data/jobs/{job_id}/resume")
    def data_job_resume(request: Request, job_id: str, body: dict[str, Any]):
        try:
            generation = int(body.get("expected_generation", body.get("generation")))
            if not isinstance(body.get("idempotency_key"), str) or not body["idempotency_key"].strip():
                return JSONResponse(status_code=400, content={"ok": False, "reason": "idempotency_key_required"})
            settings = getattr(request.app.state, "settings", None) or load_settings()
            provider = _CONNECT_PROVIDER or _default_connect_provider()
            job = operations.resume_job(lambda: provider(settings), job_id, generation, body["idempotency_key"].strip())
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"ok": False, "reason": "generation_required"})
        except RefreshError as exc:
            return _error(exc)
        except RuntimeError as exc:
            return _svc_error(exc)
        if job is None:
            return JSONResponse(status_code=409, content={"ok": False, "reason": "not_resumable_or_generation_conflict"})
        return JSONResponse(status_code=202, content={"ok": True, **job})

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

    @router.get("/data/cache")
    def data_cache(request: Request, limit: int = 20, offset: int = 0):
        """只读列出可复用的练习数据版本与资格限制。"""
        try:
            conn = _connect(request)
        except RuntimeError as exc:
            return _svc_error(exc)
        try:
            if limit < 1 or offset < 0:
                return JSONResponse(status_code=400, content={"ok": False, "reason": "invalid_request", "detail": "limit/offset 无效"})
            versions, total = store.list_versions(conn, min(int(limit), 100), int(offset))
            for version in versions:
                version["eligibility"] = "restricted_analysis" if version["synthetic"] else "blocked"
                version["content_valid"] = bool(version["coverage"].get("content_sha256") == store.version_content_digest(conn, version["version_id"]))
            return {"ok": True, "versions": versions, "total": total, "limit": min(int(limit),100), "offset": int(offset)}
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
