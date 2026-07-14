"""Chat orchestration: turns an LLM stream into a safe SSE byte stream.

This is where streaming failure semantics live (PHASE 9). Once the first byte
is sent the HTTP status is already ``200`` and cannot change, so any later
failure is surfaced as an in-band SSE ``error`` event followed by ``done`` and
the stream is terminated. Nothing provider-specific ever reaches the client.

The final structured request log is emitted here (in a ``finally``) so it
records the *complete* lifecycle including total streaming latency, even if the
client disconnects mid-stream.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from app.core.logging import get_logger
from app.services.budget import BudgetDecision
from app.services.llm import LLMError, LLMGateway

logger = get_logger("chat")

_FALLBACK_MESSAGE = "I'm having trouble responding right now. Please try again shortly."


def _sse(event: str, data: dict) -> bytes:
    """Serialize one SSE event. Payload is always JSON — never string-interpolated."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


def _estimate_tokens(text: str) -> int:
    """Provider-agnostic estimate (~4 chars/token). Documented as an estimate."""
    return max(0, len(text) // 4)


class ChatService:
    def __init__(self, llm: LLMGateway) -> None:
        self._llm = llm

    async def stream_sse(
        self,
        *,
        request_id: str,
        user_id: str,
        message: str,
        budget: BudgetDecision,
        started_at: float,
    ) -> AsyncIterator[bytes]:
        input_tokens = _estimate_tokens(message)
        output_chars = 0
        first_token_at: float | None = None
        provider_started_at = time.perf_counter()
        llm_outcome = "success"
        http_status = 200

        try:
            async for chunk in self._llm.stream(message):
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                output_chars += len(chunk)
                yield _sse("token", {"content": chunk})

            if first_token_at is None:
                # Stream completed but produced nothing usable.
                llm_outcome = "empty"

        except LLMError as exc:
            llm_outcome = f"error:{exc.kind}"
            yield _sse("error", {"message": _FALLBACK_MESSAGE})
        except Exception:  # defensive: never leak an unexpected error to client
            llm_outcome = "error:unexpected"
            logger.exception("chat_unexpected_error", extra={"request_id": request_id})
            yield _sse("error", {"message": _FALLBACK_MESSAGE})
        finally:
            yield _sse("done", {"request_id": request_id})

            now = time.perf_counter()
            latency_ms = round((now - started_at) * 1000, 2)
            ttft_ms = round((first_token_at - provider_started_at) * 1000, 2) if first_token_at else None
            provider_latency_ms = round((now - provider_started_at) * 1000, 2)

            logger.info(
                "chat_request_completed",
                extra={
                    "request_id": request_id,
                    "user_id": user_id,
                    "method": "POST",
                    "path": "/chat",
                    "http_status": http_status,
                    "latency_ms": latency_ms,
                    "time_to_first_token_ms": ttft_ms,
                    "provider_latency_ms": provider_latency_ms,
                    "estimated_tokens": input_tokens + output_chars // 4,
                    "throttled": False,
                    "budget_enforcement": budget.enforcement,
                    "budget_count": budget.count,
                    "llm_outcome": llm_outcome,
                },
            )
