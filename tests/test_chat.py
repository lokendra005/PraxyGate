"""/chat behavior: streaming, throttling, fail-open, and safe fallbacks."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_budget_service, get_chat_service
from app.main import app
from app.services.budget import BudgetDecision
from app.services.chat import ChatService
from app.services.llm import LLMProviderError, LLMTimeoutError
from tests.conftest import FakeLLMGateway


class _FakeBudget:
    def __init__(self, decision: BudgetDecision, *, raise_on_consume: bool = False) -> None:
        self._decision = decision
        self.consumed = False

    async def consume(self, user_id: str) -> BudgetDecision:
        self.consumed = True
        return self._decision

    async def ping(self) -> bool:
        return True


def _allowed(degraded: bool = False) -> BudgetDecision:
    return BudgetDecision(allowed=True, count=1, limit=20, degraded=degraded)


def _blocked() -> BudgetDecision:
    return BudgetDecision(allowed=False, count=21, limit=20, degraded=False, retry_after=3600)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _override(budget, chat: ChatService):
    app.dependency_overrides[get_budget_service] = lambda: budget
    app.dependency_overrides[get_chat_service] = lambda: chat


def test_successful_response_streams_chunks(make_chat_service):
    chat = make_chat_service(chunks=["Hello", " ", "there"])
    _override(_FakeBudget(_allowed()), chat)
    with TestClient(app) as client:
        resp = client.post("/chat", json={"user_id": "u1", "message": "hi"})
    assert resp.status_code == 200
    body = resp.text
    assert 'event: token' in body
    assert '"content": "Hello"' in body
    assert '"content": "there"' in body
    assert 'event: done' in body


def test_over_budget_returns_429_before_llm(make_chat_service):
    # If the chat service were called it would raise; proving the LLM is skipped.
    chat = ChatService(FakeLLMGateway(error=RuntimeError("should not be called")))
    _override(_FakeBudget(_blocked()), chat)
    with TestClient(app) as client:
        resp = client.post("/chat", json={"user_id": "u2", "message": "hi"})
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "daily_budget_exceeded"
    assert resp.headers.get("Retry-After") == "3600"


def test_redis_failure_still_invokes_llm(make_chat_service):
    chat = make_chat_service(chunks=["ok"])
    _override(_FakeBudget(_allowed(degraded=True)), chat)
    with TestClient(app) as client:
        resp = client.post("/chat", json={"user_id": "u3", "message": "hi"})
    assert resp.status_code == 200
    assert '"content": "ok"' in resp.text


def test_llm_error_produces_clean_fallback(make_chat_service):
    chat = make_chat_service(chunks=["partial"], error=LLMProviderError(kind="AuthError"))
    _override(_FakeBudget(_allowed()), chat)
    with TestClient(app) as client:
        resp = client.post("/chat", json={"user_id": "u4", "message": "hi"})
    body = resp.text
    assert resp.status_code == 200
    assert 'event: error' in body
    assert "trouble responding" in body
    assert 'event: done' in body
    # No provider internals / stack traces leaked.
    assert "AuthError" not in body
    assert "Traceback" not in body


def test_llm_timeout_produces_clean_fallback(make_chat_service):
    chat = make_chat_service(error=LLMTimeoutError())
    _override(_FakeBudget(_allowed()), chat)
    with TestClient(app) as client:
        resp = client.post("/chat", json={"user_id": "u5", "message": "hi"})
    body = resp.text
    assert resp.status_code == 200
    assert 'event: error' in body
    assert "trouble responding" in body
    assert 'event: done' in body


def test_done_event_terminates_stream(make_chat_service):
    chat = make_chat_service(chunks=["a", "b"])
    _override(_FakeBudget(_allowed()), chat)
    with TestClient(app) as client:
        resp = client.post("/chat", json={"user_id": "u6", "message": "hi"})
    assert resp.text.strip().endswith('data: {"request_id": "' + resp.headers["X-Request-ID"] + '"}')


def test_invalid_input_is_rejected():
    with TestClient(app) as client:
        resp = client.post("/chat", json={"user_id": "  ", "message": "hi"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
