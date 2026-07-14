"""Per-user daily request budget backed by Redis.

Design decisions (see README "Decisions & Trade-offs"):

* **Atomicity**: increment + first-write TTL run inside a single MULTI/EXEC
  transaction. A naive client-side ``INCR`` then ``EXPIRE`` has a crash window:
  if the process dies between the two round-trips the key can live forever with
  no TTL, permanently locking a user out. Queuing both commands in one
  transaction makes Redis execute them together server-side, so there is no
  window. ``EXPIRE ... NX`` additionally sets the TTL only on first creation, so
  we never push the daily reset forward and any (theoretically) missing TTL is
  self-healed by the next request.

* **Concurrency**: ``INCR`` is atomic on the Redis server, so two simultaneous
  requests from the same user get distinct, ordered counter values. The request
  that pushes the counter past the limit is the one rejected. There is no
  read-then-write race because we never read-modify-write on the client.

* **Fail-open**: any Redis error (down, timeout, command failure) is caught and
  the request is *allowed* with ``degraded=True`` so a Redis outage never takes
  ``/chat`` offline. The trade-off (temporary loss of spend enforcement) is
  documented and made visible in logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger

logger = get_logger("budget")


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    count: int          # requests used today (0 when we could not read Redis)
    limit: int
    degraded: bool      # True => Redis was unavailable and we failed open
    retry_after: int | None = None  # seconds until reset, for HTTP 429

    @property
    def enforcement(self) -> str:
        return "degraded" if self.degraded else "enforced"


def _seconds_until_utc_midnight(now: datetime) -> int:
    """Seconds remaining in the current UTC day (min 1 to avoid a 0 TTL)."""
    tomorrow = now.date().toordinal() + 1
    midnight = datetime.fromordinal(tomorrow).replace(tzinfo=timezone.utc)
    return max(1, int((midnight - now).total_seconds()))


class BudgetService:
    def __init__(self, redis: Redis | None, daily_limit: int) -> None:
        self._redis = redis
        self._limit = daily_limit

    @staticmethod
    def _key(user_id: str, day: str) -> str:
        return f"budget:{user_id}:{day}"

    async def consume(self, user_id: str) -> BudgetDecision:
        """Atomically consume one unit of the user's daily budget.

        Budget is consumed *before* the LLM call, so failed LLM requests still
        count. This prevents provider failures from becoming a free retry storm.
        """
        if self._redis is None:
            # Redis was never configured/connected: fail open.
            logger.warning(
                "budget_check_degraded",
                extra={"user_id": user_id, "redis_error": "not_configured"},
            )
            return BudgetDecision(allowed=True, count=0, limit=self._limit, degraded=True)

        now = datetime.now(timezone.utc)
        key = self._key(user_id, now.strftime("%Y-%m-%d"))
        ttl = _seconds_until_utc_midnight(now)

        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.incr(key)
            pipe.expire(key, ttl, nx=True)  # set TTL only on first creation
            results = await pipe.execute()
            count = int(results[0])
        except RedisError as exc:
            # Fail open, but make the degradation loud and classifiable.
            logger.warning(
                "budget_check_degraded",
                extra={"user_id": user_id, "redis_error": type(exc).__name__},
            )
            return BudgetDecision(allowed=True, count=0, limit=self._limit, degraded=True)

        allowed = count <= self._limit
        return BudgetDecision(
            allowed=allowed,
            count=count,
            limit=self._limit,
            degraded=False,
            retry_after=None if allowed else ttl,
        )

    async def ping(self) -> bool:
        """Readiness probe: is Redis reachable right now?"""
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.ping())
        except RedisError:
            return False
