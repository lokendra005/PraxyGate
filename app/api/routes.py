"""HTTP routes. Handlers stay thin: validate, decide, delegate, translate.

* ``POST /chat``  — budget check, then stream via SSE.
* ``GET /health`` — liveness, no dependency checks.
* ``GET /ready``  — readiness, pings Redis.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import get_budget_service, get_chat_service
from app.core.logging import get_logger
from app.schemas.chat import ChatRequest
from app.services.budget import BudgetService
from app.services.chat import ChatService

logger = get_logger("api")

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable proxy buffering so chunks flush live
}


@router.post("/chat")
async def chat(
    request: Request,
    payload: ChatRequest,
    budget_service: BudgetService = Depends(get_budget_service),
    chat_service: ChatService = Depends(get_chat_service),
):
    started_at = time.perf_counter()
    request_id = request.state.request_id

    decision = await budget_service.consume(payload.user_id)

    if not decision.allowed:
        # Throttled: reject *before* any LLM call so we never spend on it.
        logger.info(
            "chat_request_throttled",
            extra={
                "request_id": request_id,
                "user_id": payload.user_id,
                "method": "POST",
                "path": "/chat",
                "http_status": 429,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "throttled": True,
                "budget_enforcement": decision.enforcement,
                "budget_count": decision.count,
                "llm_outcome": "not_called",
            },
        )
        headers = {"Retry-After": str(decision.retry_after)} if decision.retry_after else {}
        return JSONResponse(
            status_code=429,
            headers=headers,
            content={
                "error": {
                    "code": "daily_budget_exceeded",
                    "message": "Daily request limit exceeded. Please try again tomorrow.",
                }
            },
        )

    generator = chat_service.stream_sse(
        request_id=request_id,
        user_id=payload.user_id,
        message=payload.message,
        budget=decision,
        started_at=started_at,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={**_SSE_HEADERS, "X-Request-ID": request_id},
    )


@router.get("/health")
async def health() -> dict:
    """Liveness: is the process up? Deliberately checks no dependencies."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    request: Request,
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Readiness: can we reach Redis? Reports 503 when we cannot."""
    redis_ok = await budget_service.ping()
    if redis_ok:
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "dependencies": {"redis": "ok"}},
        )
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "dependencies": {"redis": "unavailable"}},
    )
