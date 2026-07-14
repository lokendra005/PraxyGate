"""Pydantic schemas for the chat API.

Validation limits are injected from settings at import time so the API contract
and the configured limits never drift apart.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings

_settings = get_settings()


class ChatRequest(BaseModel):
    user_id: str = Field(..., description="Stable identifier for the caller.")
    message: str = Field(..., description="User prompt to forward to the LLM.")

    @field_validator("user_id")
    @classmethod
    def _validate_user_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("user_id must not be empty or whitespace")
        if len(stripped) > _settings.max_user_id_length:
            raise ValueError(f"user_id must be <= {_settings.max_user_id_length} characters")
        return stripped

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be empty or whitespace")
        if len(stripped) > _settings.max_message_length:
            raise ValueError(f"message must be <= {_settings.max_message_length} characters")
        return stripped


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
