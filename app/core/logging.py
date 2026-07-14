"""Structured JSON logging.

We use the stdlib ``logging`` module with a tiny JSON formatter rather than a
heavy telemetry dependency. Every log line is a single JSON object so it can be
ingested by any log platform (Railway logs, Loki, Datadog, etc.) without a
custom parser.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# Attributes that already exist on a standard LogRecord. Anything set via
# ``logger.info(..., extra={...})`` that is NOT in this set is treated as a
# structured field and merged into the JSON output.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            # Keep the exception type/message but never the full traceback in a
            # way that could leak secrets from request payloads.
            payload["error_class"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["error_message"] = str(record.exc_info[1]) if record.exc_info[1] else None

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger exactly once."""
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any pre-existing handlers (e.g. uvicorn's default) so we do not
    # emit duplicate, non-JSON lines.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    # Route uvicorn's loggers through the root handler for consistent output.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
