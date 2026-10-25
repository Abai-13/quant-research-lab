"""Fail-closed runtime cache maintenance endpoints."""
from __future__ import annotations

import sqlite3
import importlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from ..agent_runtime.maintenance import MaintenanceError, MaintenanceService
from ..config import Settings


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    automatic: bool = False


def _unavailable_reference_source(name: str):
    def read_snapshot():
        raise RuntimeError(f"reference_snapshot_unavailable:{name}")
    return read_snapshot


def create_maintenance_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])
    service = MaintenanceService(Path(settings.runtime_dir) / "agent" / "maintenance.sqlite3", settings.runtime_dir)
    for module_name, domain in (("app.practice_data.service", "practice_data"),
                                ("app.research.service", "research"),
                                ("app.agent_tools.service", "agent_evidence")):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name in {module_name, module_name.rpartition(".")[0]}:
                service.register_reference_source(domain, _unavailable_reference_source(domain))
                continue
            raise
        reader = getattr(module, "maintenance_reference_snapshot", None)
        if callable(reader):
            service.register_reference_source(domain, lambda fn=reader: fn(settings))
        else:
            service.register_reference_source(domain, _unavailable_reference_source(domain))

    def source_instance_id() -> str:
        if not Path(settings.db_path).is_file():
            raise HTTPException(503, detail={"ok": False, "reason": "source_instance_unavailable", "detail": "The managed data instance is unavailable."})
        try:
            conn = sqlite3.connect(Path(settings.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
            try:
                row = conn.execute("SELECT value FROM app_metadata WHERE key='instance_id'").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            row = None
        if row is None or not row[0]:
            raise HTTPException(503, detail={"ok": False, "reason": "source_instance_unavailable", "detail": "The managed data instance is unavailable."})
        return str(row[0])

    @router.get("/previews")
    def list_previews(limit: int = Query(20, ge=1, le=100)):
        if not service.db_path.exists():
            return {"previews": [], "count": 0, "limit": limit}
        service.initialize()
        previews = service.list_previews(limit)
        return {"previews": previews, "count": len(previews), "limit": limit}

    @router.post("/preview")
    def preview_cache():
        service.initialize()
        try:
            # All cross-domain reference sources are fail-closed. They are
            # registered by integration adapters when the owning slices land.
            return service.preview_runtime_cache(instance_id=source_instance_id(), policy_version="cache-retention-v1")
        except MaintenanceError as exc:
            raise HTTPException(409, detail={"ok": False, "reason": exc.reason, "detail": "Maintenance preview could not be created safely."}) from None

    @router.post("/previews/{preview_id}/execute")
    def execute_cache_preview(preview_id: str, body: ApplyBody):
        service.initialize()
        try:
            return service.execute_saved(preview_id, instance_id=source_instance_id(), policy_version="cache-retention-v1", automatic=body.automatic)
        except MaintenanceError as exc:
            raise HTTPException(409, detail={"ok": False, "reason": exc.reason, "detail": "Maintenance preview changed or references could not be verified."}) from None

    return router
