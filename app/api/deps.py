"""Dependency accessors.

Services are created once at startup (see ``app.main`` lifespan) and stored on
``app.state``. These helpers expose them to routes via FastAPI's ``Depends`` so
handlers stay thin and tests can override them.
"""

from __future__ import annotations

from fastapi import Request

from app.services.budget import BudgetService
from app.services.chat import ChatService


def get_budget_service(request: Request) -> BudgetService:
    return request.app.state.budget_service


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service
