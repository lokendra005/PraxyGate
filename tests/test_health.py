"""Health/readiness endpoint behavior."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_budget_service
from app.main import app


class _FakeBudget:
    def __init__(self, ready: bool) -> None:
        self._ready = ready

    async def ping(self) -> bool:
        return self._ready


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_health_returns_200_without_dependency_check():
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_returns_200_when_redis_ok():
    app.dependency_overrides[get_budget_service] = lambda: _FakeBudget(ready=True)
    with TestClient(app) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["dependencies"]["redis"] == "ok"


def test_ready_returns_503_when_redis_unavailable():
    app.dependency_overrides[get_budget_service] = lambda: _FakeBudget(ready=False)
    with TestClient(app) as client:
        resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["dependencies"]["redis"] == "unavailable"
