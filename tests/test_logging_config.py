from __future__ import annotations

import logging

from app.logging_config import ConsoleFormatter, RecentLogHandler, RedactionFilter


def test_recent_log_handler_keeps_redacted_snapshot() -> None:
    handler = RecentLogHandler(capacity=2)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(ConsoleFormatter())
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "job_created", (), None)
    record.body = "private message"
    record.job_id = "job-1"

    handler.handle(record)
    snapshot = handler.snapshot()

    assert "job_created" in snapshot
    assert "job_id=job-1" in snapshot
    assert "private message" not in snapshot
