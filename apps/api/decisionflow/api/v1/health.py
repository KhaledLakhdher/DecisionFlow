"""Liveness and readiness probes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import text

from decisionflow import __version__
from decisionflow.core.config import settings
from decisionflow.db.session import untenanted_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness: the process is up. Deliberately touches no dependency."""
    return {"status": "ok", "version": __version__, "environment": settings.environment}


@router.get("/health/ready")
async def readiness() -> dict[str, Any]:
    """Readiness: the dependencies this process needs are actually reachable.

    Returns 200 with per-check detail rather than failing the whole probe on a
    degraded optional dependency — the LLM being unconfigured should not take
    the API out of a load balancer rotation.
    """
    checks: dict[str, Any] = {}

    try:
        async with untenanted_session() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"

    checks["llm"] = "configured" if settings.llm_configured else "not_configured"

    healthy = checks["postgres"] == "ok"
    return {"status": "ready" if healthy else "degraded", "checks": checks}
