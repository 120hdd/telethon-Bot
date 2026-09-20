from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def parse_schedule(value: str, timezone: ZoneInfo, *, now: datetime | None = None) -> datetime:
    normalized = value.strip().replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("Schedule must be an ISO datetime, for example 2026-09-16 14:30") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    result = parsed.astimezone(UTC)
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    if result <= reference:
        raise ValueError("Scheduled time must be in the future")
    return result
