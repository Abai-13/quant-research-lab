"""FastAPI 应用工厂与阶段 1 状态接口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import DISPLAY_NAME, MODE, SERVICE_NAME, __version__
from .config import Settings, load_settings
from .db import DatabaseStatus, check_database, initialize_database

logger = logging.getLogger("app.main")

LIMITATIONS = [
    "本阶段仅提供本地基础服务，不连接行情、不创建模拟账户，也不执行策略或交易。",
    "未实现登录与公网访问保护，服务默认只监听本机回环地址。",
    "页面中的功能状态来自后端接口，不代表策略有效或能够盈利。",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _database_payload(status: DatabaseStatus) -> dict:
    if status.ready and status.metadata is not None:
        return {
            "status": "ready",
            "schema_version": status.metadata.schema_version,
            "instance_id": status.metadata.instance_id,
            "created_at": status.metadata.created_at,
        }
    return {
        "status": "not_ready",
        "reason": status.reason,
        "message": status.message,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = resolved
        app.state.startup_database_error = None
        try:
            initialize_database(resolved)
        except Exception as exc:  # noqa: BLE001 - 启动失败必须可诊断且不伪造就绪
            reason = getattr(exc, "reason", "database_unavailable")
            app.state.startup_database_error = reason
            logger.warning("数据库初始化未完成（%s），服务继续启动但 readiness 将返回未就绪", reason)
        yield

    app = FastAPI(
        title=DISPLAY_NAME,
        version=__version__,
        description="阶段 1 本地基础状态服务",
        lifespan=lifespan,
    )

    @app.get("/api/health")
    def health() -> dict:
        """进程存活检查；不代表数据库或业务功能就绪。"""
        return {
            "service": SERVICE_NAME,
            "name": DISPLAY_NAME,
            "version": __version__,
            "mode": MODE,
            "status": "ok",
        }

    @app.get("/api/ready")
    def ready(request: Request):
        """数据库就绪检查；每次请求都执行真实的只读检查。"""
        status = check_database(request.app.state.settings)
        payload = {"status": "ready" if status.ready else "not_ready", "database": _database_payload(status)}
        if not status.ready:
            return JSONResponse(status_code=503, content=payload)
        return payload

    @app.get("/api/system/status")
    def system_status() -> dict:
        """功能准备状态；未实现或未接入的项目不使用虚构数值。"""
        return {
            "service": SERVICE_NAME,
            "name": DISPLAY_NAME,
            "version": __version__,
            "mode": MODE,
            "generated_at": _utc_now(),
            "market_data": {"status": "not_connected"},
            "account": {
                "status": "not_initialized",
                "net_value": None,
                "cumulative_return": None,
            },
            "strategy": {"status": "not_implemented"},
            "scheduler": {"status": "not_implemented"},
            "ai": {"status": "not_configured"},
            "limitations": LIMITATIONS,
        }

    return app
