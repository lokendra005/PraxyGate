"""Budget service: correctness, TTL, concurrency, and fail-open behavior."""

from __future__ import annotations

import asyncio

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.budget import BudgetService


async def test_requests_under_limit_are_allowed(budget_service):
    for i in range(1, 4):  # limit is 3
        decision = await budget_service.consume("user-a")
        assert decision.allowed is True
        assert decision.count == i
        assert decision.degraded is False


async def test_request_over_limit_is_rejected(budget_service):
    for _ in range(3):
        await budget_service.consume("user-b")
    decision = await budget_service.consume("user-b")
    assert decision.allowed is False
    assert decision.count == 4
    assert decision.retry_after is not None and decision.retry_after > 0


async def test_ttl_is_set_on_first_request(budget_service, fake_redis):
    await budget_service.consume("user-c")
    keys = await fake_redis.keys("budget:user-c:*")
    assert len(keys) == 1
    ttl = await fake_redis.ttl(keys[0])
    # TTL should be positive and never exceed a full day.
    assert 0 < ttl <= 86400


async def test_concurrent_requests_cannot_exceed_limit(fake_redis):
    # Fresh service with a small limit; fire many requests at once.
    service = BudgetService(fake_redis, daily_limit=5)
    results = await asyncio.gather(*[service.consume("user-race") for _ in range(20)])
    allowed = [r for r in results if r.allowed]
    assert len(allowed) == 5  # exactly the limit, never more


async def test_redis_failure_fails_open():
    class BoomRedis:
        def pipeline(self, transaction=True):
            raise RedisConnectionError("redis down")

        async def ping(self):
            raise RedisConnectionError("redis down")

    service = BudgetService(BoomRedis(), daily_limit=3)
    decision = await service.consume("user-d")
    assert decision.allowed is True
    assert decision.degraded is True
    assert decision.enforcement == "degraded"


async def test_ping_reports_readiness(budget_service):
    assert await budget_service.ping() is True


async def test_ping_false_when_no_redis():
    service = BudgetService(None, daily_limit=3)
    assert await service.ping() is False
    decision = await service.consume("user-e")
    assert decision.allowed is True and decision.degraded is True
