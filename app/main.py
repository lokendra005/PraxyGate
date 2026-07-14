"""FastAPI application factory, lifecycle, middleware, and error handling."""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("app")

# Only allow a conservative charset for client-supplied request IDs so a header
# can never inject control characters into our structured logs.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _build_redis(settings) -> Redis | None:
    if not settings.redis_url:
        logger.warning("redis_not_configured")
        return None
    # Short timeouts: a slow/unreachable Redis must fail fast so /chat can fail
    # open instead of blocking the request path.
    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_connect_timeout,
        socket_timeout=settings.redis_socket_timeout,
        decode_responses=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.validate()

    # Import here so services pick up the configured logging.
    from app.services.budget import BudgetService
    from app.services.chat import ChatService
    from app.services.llm import LLMGateway

    redis = _build_redis(settings)
    app.state.redis = redis
    app.state.budget_service = BudgetService(redis, settings.daily_request_limit)
    app.state.chat_service = ChatService(LLMGateway(settings))

    logger.info(
        "app_startup",
        extra={
            "app_env": settings.app_env,
            "daily_request_limit": settings.daily_request_limit,
            "llm_model": settings.llm_model,
            "redis_configured": redis is not None,
        },
    )
    try:
        yield
    finally:
        if redis is not None:
            await redis.aclose()
        logger.info("app_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="Guardrailed LLM Gateway", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        # Surface a stable, user-safe error shape; never echo raw internals.
        logger.info(
            "request_validation_failed",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Request validation failed. Check user_id and message.",
                }
            },
        )

    app.include_router(router)
    return app


app = create_app()
