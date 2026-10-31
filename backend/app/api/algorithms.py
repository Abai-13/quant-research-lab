"""Read-only catalog of explicitly registered practice algorithms."""
from fastapi import APIRouter

from app.practice.algorithms import executable_algorithms


def create_algorithms_router() -> APIRouter:
    router = APIRouter(prefix="/api/practice", tags=["practice-algorithms"])

    @router.get("/algorithms")
    def list_algorithms() -> dict:
        return {"algorithms": executable_algorithms()}

    return router
