from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.messaging.scheduler import parse_schedule


def test_naive_schedule_uses_configured_timezone() -> None:
    now = datetime(2026, 9, 15, 8, tzinfo=UTC)
    result = parse_schedule("2026-09-16 14:30", ZoneInfo("Asia/Tehran"), now=now)
    assert result.tzinfo == UTC
    assert result.hour == 11


def test_past_schedule_is_rejected() -> None:
    now = datetime(2026, 9, 15, 8, tzinfo=UTC)
    with pytest.raises(ValueError, match="future"):
        parse_schedule("2026-09-14 14:30", ZoneInfo("UTC"), now=now)
