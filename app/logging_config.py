from __future__ import annotations

import json
import logging
import sys
from collections import deque
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

STANDARD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
SENSITIVE_KEYS = {
    "api_hash",
    "authorization_code",
    "body",
    "message_body",
    "otp",
    "password",
    "session",
    "session_auth_key",
}


class RecentLogHandler(logging.Handler):
    """Keep a bounded, redacted log view for the Saved Messages controller."""

    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        self._records: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._records.append(self.format(record))
        except Exception:
            self.handleError(record)

    def snapshot(self, *, max_chars: int = 3200) -> str:
        self.acquire()
        try:
            selected: list[str] = []
            used = 0
            for line in reversed(self._records):
                line_size = len(line) + 1
                if selected and used + line_size > max_chars:
                    break
                if line_size > max_chars:
                    line = line[-max_chars:]
                    line_size = len(line)
                selected.append(line)
                used += line_size
            return "\n".join(reversed(selected))
        finally:
            self.release()


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


def configure_logging(level: str, log_format: str, log_path: Path) -> RecentLogHandler:
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

    recent_handler = RecentLogHandler()
    recent_handler.addFilter(redaction)
    recent_handler.setFormatter(ConsoleFormatter())

    root.addHandler(console)
    root.addHandler(file_handler)
    root.addHandler(recent_handler)
    return recent_handler
