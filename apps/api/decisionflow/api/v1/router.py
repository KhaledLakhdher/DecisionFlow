"""Aggregates the v1 endpoint modules into one router."""

from __future__ import annotations

from fastapi import APIRouter

from decisionflow.api.v1 import auth, health, orgs

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(orgs.router)
