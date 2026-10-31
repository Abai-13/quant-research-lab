"""FastAPI 应用工厂与状态接口。

阶段 1 的 ``/api/health``、``/api/ready`` 与 ``/api/system/status`` 语义保持不变；
阶段 2 只在状态页补充**数据状态摘要**，并新增 ``/api/data/status``。
"""

from __future__ import annotations

import logging
import importlib
import inspect
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from . import DISPLAY_NAME, MODE, SERVICE_NAME, __version__
from .api.portfolio import create_portfolio_router
from .api.practice import create_practice_router
from .api.observation import create_observation_router
from .api.algorithms import create_algorithms_router
from .api.agent import create_agent_router
from .api.maintenance import create_maintenance_router
from .agent_runtime.manager import AgentTaskManager
from .agent_runtime.store import AgentStore
from .config import Settings, load_settings
from .data.errors import DataError
from .data.status import data_status
from .db import DatabaseStatus, check_database, initialize_database
from .practice.core import PracticeError
from .practice.observation_runner import ObservationRunner

logger = logging.getLogger("app.main")

LIMITATIONS = [
    "阶段三已实现本地个人持仓、组合建议、模拟成交、公司行为与 NAV/收益；不接券商、不自动下单。",
    "阶段四提供隔离的历史练习、显式逐日推进、四路对照与复盘；练习写入 <运行目录>/practice，与个人主库分离。",
    "练习行情刷新只由显式 POST 请求触发；BaoStock 商用/再分发许可、完整股票池、历史交易资格与复杂公司行为仍未确认。",
    "离线演示使用独立合成数据库，页面始终标注合成数据；不会写入个人账户或真实行情。",
    "未实现登录与公网访问保护，服务默认只监听本机回环地址。",
    "页面中的功能状态来自后端接口，不代表策略有效或能够盈利；费用/滑点/涨跌停均为版本化模拟假设。",
    "行情可用资格由数据资格接口判断，缺失或陈旧的数据不会被当作合格输入。",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _optional_router(module_name: str, factory_name: str, settings: Settings):
    """Load phase-6 domain routers when their owning slice is integrated."""
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name in {module_name, module_name.rpartition(".")[0]}:
            return None
        raise
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        return None
    parameters = inspect.signature(factory).parameters
    return factory(settings) if parameters else factory()


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


def create_app(settings: Settings | None = None, *, agent_gateway=None, agent_tools=None) -> FastAPI:
    resolved = settings or load_settings()
    agent_store = AgentStore(resolved.runtime_dir / "agent" / "tasks.sqlite3")
    agent_manager = AgentTaskManager(resolved, agent_store, agent_gateway, agent_tools)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        data_operations = None
        data_operations_started = False
        research_executor = None
        research_executor_started = False
        app.state.settings = resolved
        app.state.startup_database_error = None
        try:
            initialize_database(resolved)
        except Exception as exc:  # noqa: BLE001 - 启动失败必须可诊断且不伪造就绪
            reason = getattr(exc, "reason", "database_unavailable")
            app.state.startup_database_error = reason
            logger.warning("数据库初始化未完成（%s），服务继续启动但 readiness 将返回未就绪", reason)
        try:
            agent_store.initialize()
        except Exception as exc:  # Agent 存储故障不应中断既有手动模拟API
            app.state.agent_storage_error = getattr(exc, "reason", "agent_storage_unavailable")
            logger.warning("Agent任务存储不可用（%s）", app.state.agent_storage_error)
        try:
            data_operations = importlib.import_module("app.practice_data.operations")
            start_operations = getattr(data_operations, "startup_operations", None)
            if callable(start_operations):
                start_operations(resolved)
                data_operations_started = True
        except ModuleNotFoundError as exc:
            if exc.name not in {"app.practice_data.operations", "app.practice_data", "app"}:
                logger.warning("练习数据任务生命周期不可用（%s）", getattr(exc, "name", "unknown"))
        try:
            research_executor = importlib.import_module("app.research.executor")
            start_research = getattr(research_executor, "startup_research_executor", None)
            if callable(start_research):
                start_research(resolved)
                research_executor_started = True
        except ModuleNotFoundError as exc:
            if exc.name not in {"app.research.executor", "app.research", "app"}:
                logger.warning("Research executor lifecycle unavailable (%s)", getattr(exc, "name", "unknown"))
        app.state.observation_runner.recover()
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.observation_runner.shutdown)
            try:
                await agent_manager.shutdown()
            finally:
                try:
                    if data_operations_started and data_operations is not None:
                        close_operations = getattr(data_operations, "shutdown_operations", None)
                        if callable(close_operations):
                            await asyncio.to_thread(close_operations, wait=True)
                finally:
                    try:
                        if research_executor_started and research_executor is not None:
                            close_research = getattr(research_executor, "shutdown_research_executor", None)
                            if callable(close_research):
                                await asyncio.to_thread(close_research, wait=True)
                    finally:
                        try:
                            credentials = importlib.import_module("app.credentials")
                            close_process = getattr(credentials, "close_process_credentials", None)
                            if callable(close_process):
                                close_process(resolved.runtime_dir)
                        except ModuleNotFoundError as exc:
                            if exc.name not in {"app.credentials", "app"}:
                                logger.warning("Credential cleanup module unavailable (%s)", getattr(exc, "name", "unknown"))
                        except Exception:
                            logger.warning("Credential cleanup could not be completed")

    app = FastAPI(
        title=DISPLAY_NAME,
        version=__version__,
        description="阶段 1 本地基础状态服务",
        lifespan=lifespan,
    )
    app.state.agent_store = agent_store
    app.state.agent_manager = agent_manager
    app.state.agent_gateway = agent_gateway

    @app.exception_handler(DataError)
    async def data_error_handler(request: Request, exc: DataError) -> JSONResponse:
        """业务校验失败返回稳定原因码，不吞掉为 500。"""
        return JSONResponse(
            status_code=400,
            content={"ok": False, "reason": exc.reason, "detail": str(exc)},
        )

    @app.exception_handler(PracticeError)
    async def practice_error_handler(request: Request, exc: PracticeError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "reason": exc.reason, "detail": str(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "reason": "request_invalid", "detail": "Invalid Agent request."}
            if request.url.path.startswith("/api/agent")
            else {"ok": False, "reason": "request_invalid", "detail": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if request.url.path.startswith("/api/agent") and isinstance(exc.detail, dict) and exc.detail.get("ok") is False:
            return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

    app.include_router(create_portfolio_router())
    practice_router = create_practice_router()
    app.state.practice_actions = practice_router.practice_actions
    app.state.observation_runner = ObservationRunner(resolved, practice_router.practice_actions)
    app.include_router(practice_router)
    app.include_router(create_observation_router())
    app.include_router(create_algorithms_router())
    app.include_router(create_agent_router(resolved, agent_store, agent_manager, agent_gateway, agent_tools))
    app.include_router(create_maintenance_router(resolved))
    for module_name, factory_name in (("app.api.research", "create_research_router"),
                                      ("app.api.credentials", "create_credentials_router")):
        optional = _optional_router(module_name, factory_name, resolved)
        if optional is not None:
            app.include_router(optional)

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
                "status": "available",
                "net_value": None,
                "cumulative_return": None,
            },
            "strategy": {"status": "available"},
            "scheduler": {"status": "not_implemented"},
            "ai": _ai_status(request.app.state.settings, request.app.state.agent_gateway),
            "limitations": LIMITATIONS,
        }

    return app


def _ai_status(settings: Settings, gateway=None) -> dict:
    """Expose only real public provider configuration state; never secret material."""
    try:
        if gateway is None:
            from .agent_gateway import get_public_config
        else:
            get_public_config = gateway.get_public_config
        value = get_public_config(settings)
        return {"status": "configured" if value.get("configured") else "not_configured"}
    except ImportError:
        return {"status": "unavailable"}
    except Exception:
        return {"status": "unavailable"}
