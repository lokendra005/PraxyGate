"""Environment-driven application configuration.

All tunables live here so the rest of the code never reads ``os.environ``
directly. Validation happens once at import/startup so misconfiguration fails
fast and loudly instead of surfacing as a confusing runtime error mid-request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a float, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of runtime configuration."""

    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "local"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())

    # Redis
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    redis_connect_timeout: float = field(
        default_factory=lambda: _get_float("REDIS_CONNECT_TIMEOUT_SECONDS", 0.5)
    )
    redis_socket_timeout: float = field(
        default_factory=lambda: _get_float("REDIS_SOCKET_TIMEOUT_SECONDS", 0.5)
    )

    # Budget
    daily_request_limit: int = field(default_factory=lambda: _get_int("DAILY_REQUEST_LIMIT", 20))

    # LLM
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "openrouter/openai/gpt-4o-mini"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_timeout_seconds: float = field(default_factory=lambda: _get_float("LLM_TIMEOUT_SECONDS", 30.0))
    # Idle timeout between streamed chunks; guards against a stream that begins
    # then hangs forever. 0 disables the per-chunk guard.
    llm_chunk_timeout_seconds: float = field(
        default_factory=lambda: _get_float("LLM_CHUNK_TIMEOUT_SECONDS", 15.0)
    )

    # Input validation limits
    max_user_id_length: int = field(default_factory=lambda: _get_int("MAX_USER_ID_LENGTH", 128))
    max_message_length: int = field(default_factory=lambda: _get_int("MAX_MESSAGE_LENGTH", 8000))

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}

    def validate(self) -> None:
        """Fail fast on invalid or unsafe production configuration."""
        errors: list[str] = []

        if self.daily_request_limit <= 0:
            errors.append("DAILY_REQUEST_LIMIT must be > 0")
        if self.llm_timeout_seconds <= 0:
            errors.append("LLM_TIMEOUT_SECONDS must be > 0")
        if self.max_message_length <= 0:
            errors.append("MAX_MESSAGE_LENGTH must be > 0")
        if self.max_user_id_length <= 0:
            errors.append("MAX_USER_ID_LENGTH must be > 0")

        # In production a missing LLM key would make every /chat degrade to the
        # fallback path silently, so we refuse to boot without it.
        if self.is_production and not self.llm_api_key:
            errors.append("LLM_API_KEY is required when APP_ENV is production")

        if errors:
            raise ValueError("Invalid configuration: " + "; ".join(errors))


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
