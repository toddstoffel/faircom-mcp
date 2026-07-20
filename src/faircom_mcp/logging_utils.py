from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

REDACT_KEYS = {
    "authorization",
    "query",
    "password",
    "secret",
    "sql",
    "token",
    "api_key",
    "apikey",
    "statement",
}


def redact_sensitive(value: Any, key: str | None = None) -> Any:
    if key is not None and any(marker in key.lower() for marker in REDACT_KEYS):
        return "***REDACTED***"

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            redacted[key] = redact_sensitive(item, key=key)
        return redacted

    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]

    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key
            not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "message",
                "asctime",
            }
        }
        if extras:
            payload["context"] = redact_sensitive(extras)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(redact_sensitive(payload), separators=(",", ":"))


def configure_structured_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("faircom_mcp")
    logger.setLevel(level)
    logger.handlers.clear()

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger
