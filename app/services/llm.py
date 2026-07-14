"""LLM gateway: the *only* module that knows about LiteLLM / OpenRouter.

Responsibilities:
* call the provider asynchronously with streaming enabled,
* yield normalized text chunks (empty chunks dropped),
* enforce bounded waiting with two guarantees (see PHASE 8):
    - a total-operation deadline (``llm_timeout_seconds``), and
    - a per-chunk idle timeout (``llm_chunk_timeout_seconds``),
* translate any provider-specific error into ``LLMError`` so callers never see
  provider internals or stack traces.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import litellm

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger("llm")

# LiteLLM by default logs verbosely and can retry on its own; we keep control.
litellm.drop_params = True
litellm.suppress_debug_info = True


class LLMError(Exception):
    """Raised for any provider failure. Carries a classifiable label only."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class LLMTimeoutError(LLMError):
    def __init__(self, message: str = "llm operation timed out") -> None:
        super().__init__(message, kind="timeout")


class LLMProviderError(LLMError):
    def __init__(self, message: str = "llm provider error", *, kind: str = "provider_error") -> None:
        super().__init__(message, kind=kind)


class LLMGateway:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def stream(self, message: str) -> AsyncIterator[str]:
        """Yield text chunks from the model. Raises ``LLMError`` on failure.

        The first chunk yielded also marks time-to-first-token for the caller.
        """
        settings = self._settings
        total_deadline = asyncio.get_event_loop().time() + settings.llm_timeout_seconds

        try:
            response = await asyncio.wait_for(
                litellm.acompletion(
                    model=settings.llm_model,
                    api_key=settings.llm_api_key or None,
                    messages=[{"role": "user", "content": message}],
                    stream=True,
                    timeout=settings.llm_timeout_seconds,
                ),
                timeout=settings.llm_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError("timed out establishing llm stream") from exc
        except Exception as exc:  # provider boundary: normalize everything
            logger.warning("llm_start_failed", extra={"llm_error_class": type(exc).__name__})
            raise LLMProviderError(kind=type(exc).__name__) from exc

        stream_iter = response.__aiter__()
        while True:
            remaining = total_deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise LLMTimeoutError("llm stream exceeded total timeout")

            per_chunk = self._settings.llm_chunk_timeout_seconds
            chunk_timeout = min(per_chunk, remaining) if per_chunk > 0 else remaining

            try:
                chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=chunk_timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as exc:
                raise LLMTimeoutError("llm stream stalled between chunks") from exc
            except Exception as exc:  # provider boundary
                logger.warning("llm_stream_failed", extra={"llm_error_class": type(exc).__name__})
                raise LLMProviderError(kind=type(exc).__name__) from exc

            content = _extract_content(chunk)
            if content:
                yield content


def _extract_content(chunk: object) -> str:
    """Pull text out of a LiteLLM stream chunk, tolerating shape differences."""
    try:
        choices = getattr(chunk, "choices", None)
        if not choices:
            return ""
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            return ""
        content = getattr(delta, "content", None)
        return content or ""
    except (AttributeError, IndexError, TypeError):
        return ""
