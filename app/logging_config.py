from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

STANDARD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
SENSITIVE_KEYS = {
    "api_hash",
    "authorization_code",
    "otp",
    "password",
    "session",
    "session_auth_key",
}


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key in SENSITIVE_KEYS:
            if hasattr(record, key):
                setattr(record, key, "[REDACTED]")
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        for key, value in record.__dict__.items():
            if key not in STANDARD_FIELDS and key not in SENSITIVE_KEYS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        details = []
        for key, value in record.__dict__.items():
            if key not in STANDARD_FIELDS and key not in SENSITIVE_KEYS:
                details.append(f"{key}={value}")
        suffix = f" {' '.join(details)}" if details else ""
        return f"{self.formatTime(record)} {record.levelname:<8} {record.getMessage()}{suffix}"


def configure_logging(level: str, log_format: str, log_path: Path) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    redaction = RedactionFilter()
    console = logging.StreamHandler(sys.stderr)
    console.addFilter(redaction)
    console.setFormatter(ConsoleFormatter())

    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.addFilter(redaction)
    file_handler.setFormatter(JsonFormatter() if log_format == "json" else ConsoleFormatter())

    root.addHandler(console)
    root.addHandler(file_handler)
