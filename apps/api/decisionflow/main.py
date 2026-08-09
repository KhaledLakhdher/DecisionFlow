"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from decisionflow import __version__
from decisionflow.api.v1.router import api_router
from decisionflow.core.config import settings
from decisionflow.core.errors import DecisionFlowError
from decisionflow.core.logging import configure_logging, get_logger
from decisionflow.db.session import dispose_engine

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings.duckdb_root.mkdir(parents=True, exist_ok=True)
    log.info(
        "api.startup",
        version=__version__,
        environment=settings.environment,
        llm_configured=settings.llm_configured,
    )
    if not settings.llm_configured:
        # Not fatal: everything except the AI endpoints works without a key.
        log.warning("api.llm_not_configured", hint="Set GEMINI_API_KEY to enable AI features.")

    yield

    await dispose_engine()
    log.info("api.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="DecisionFlow API",
        description="AI Business Analyst — turn your data into decisions.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def bind_request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Tag every log line emitted during a request with the same request id."""
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(DecisionFlowError)
    async def handle_app_error(request: Request, exc: DecisionFlowError) -> JSONResponse:
        # Client mistakes are noise at error level; server faults are not.
        if exc.status_code >= 500:
            log.error("request.failed", code=exc.code, message=exc.message, exc_info=exc)
        else:
            log.info("request.rejected", code=exc.code, message=exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic puts the originating exception object in `ctx`, which is not
        # JSON-serialisable and would leak internals anyway. Keep only the
        # fields a client can act on.
        errors = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())),
                "message": err.get("msg", "Invalid value."),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The request payload is invalid.",
                    "details": {"errors": errors},
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("request.unhandled_exception")
        # Never surface internal detail to the client in production.
        message = str(exc) if settings.debug else "An unexpected error occurred."
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": message}},
        )

    app.include_router(api_router, prefix="/api/v1")

    return app


app = create_app()
