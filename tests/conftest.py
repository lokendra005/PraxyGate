"""Shared test fixtures.

Tests never touch a real Redis or a real LLM provider:
* Redis is replaced with an in-memory ``fakeredis`` instance.
* The LLM is replaced with a ``FakeLLMGateway`` whose behavior each test picks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.services.budget import BudgetService
from app.services.chat import ChatService
from app.services.llm import LLMGateway


class FakeLLMGateway(LLMGateway):
    """Configurable stand-in for the real gateway. No network, no cost."""

    def __init__(self, chunks=None, error: Exception | None = None) -> None:
        self._chunks = chunks or []
        self._error = error

    async def stream(self, message: str) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


@pytest_asyncio.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def budget_service(fake_redis):
    return BudgetService(fake_redis, daily_limit=3)


@pytest.fixture
def make_chat_service():
    def _factory(chunks=None, error: Exception | None = None) -> ChatService:
        return ChatService(FakeLLMGateway(chunks=chunks, error=error))

    return _factory
