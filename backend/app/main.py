"""FastAPI 应用工厂与状态接口。

阶段 1 的 ``/api/health``、``/api/ready`` 与 ``/api/system/status`` 语义保持不变；
阶段 2 只在状态页补充**数据状态摘要**，并新增 ``/api/data/status``。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import DISPLAY_NAME, MODE, SERVICE_NAME, __version__
from .config import Settings, load_settings
from .data.status import data_status
from .db import DatabaseStatus, check_database, initialize_database

logger = logging.getLogger("app.main")

LIMITATIONS = [
    "本阶段建立数据接入与质量检查基础，不实现策略、回测、模拟账户或调度。",
    "未实现登录与公网访问保护，服务默认只监听本机回环地址。",
    "页面中的功能状态来自后端接口，不代表策略有效或能够盈利。",
    "行情可用资格由数据资格接口判断，缺失或陈旧的数据不会被当作合格输入。",
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

    @app.get("/api/data/status")
    def data_status_endpoint(request: Request) -> dict:
        """数据准备程度摘要；只读真实检查结果，不展示假价格或假收益。"""
        return data_status(request.app.state.settings)

    @app.get("/api/data/versions")
    def data_versions_endpoint(request: Request, limit: int = 20, offset: int = 0) -> dict:
        """有界版本列表；完整列表用分页读取，不在状态页一次返回历史全量。"""
        import sqlite3

        from .data.store import published_versions, version_count

        settings = request.app.state.settings
        if not settings.db_path.exists():
            return {"versions": [], "count": 0, "total": 0, "limit": limit, "offset": offset}
        conn = sqlite3.connect(
            settings.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0
        )
        try:
            safe_limit = max(1, min(int(limit), 200))
            safe_offset = max(0, int(offset))
            return {
                "versions": published_versions(conn, limit=safe_limit, offset=safe_offset),
                "count": safe_limit,
                "total": version_count(conn),
                "limit": safe_limit,
                "offset": safe_offset,
            }
        finally:
            conn.close()

    @app.get("/api/data/cleanup/previews")
    def cleanup_previews_endpoint(request: Request, limit: int = 20) -> dict:
        """只读列出维护库中的清理预览；维护库不存在时返回空列表，不创建文件。"""
        import sqlite3

        from .data.maintenance import maintenance_db_path

        settings = request.app.state.settings
        path = maintenance_db_path(settings)
        if not path.exists():
            return {"previews": [], "count": 0, "total": 0, "limit": limit}
        safe_limit = max(1, min(int(limit), 100))
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT preview_id, policy_version, source_code, as_of, generation,"
                " created_at, expires_at, state FROM cleanup_preview"
                " ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM cleanup_preview").fetchone()[0]
        finally:
            conn.close()
        return {
            "previews": [
                {
                    "preview_id": row[0],
                    "policy_version": row[1],
                    "source_code": row[2],
                    "as_of": row[3],
                    "generation": row[4],
                    "created_at": row[5],
                    "expires_at": row[6],
                    "state": row[7],
                }
                for row in rows
            ],
            "count": len(rows),
            "total": int(total),
            "limit": safe_limit,
        }

    @app.get("/api/system/status")
    def system_status(request: Request) -> dict:
        """功能准备状态；未实现或未接入的项目不使用虚构数值。"""
        data = data_status(request.app.state.settings)
        return {
            "service": SERVICE_NAME,
            "name": DISPLAY_NAME,
            "version": __version__,
            "mode": MODE,
            "generated_at": _utc_now(),
            "market_data": {
                "status": data["status"],
                "detail": data.get("detail"),
                "source": data["source"],
                "coverage": data.get("coverage"),
                "last_import": data.get("last_import"),
                "quality": data.get("quality"),
                "data_versions": data.get("data_versions"),
                "pool_snapshots": data.get("pool_snapshots"),
            },
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
