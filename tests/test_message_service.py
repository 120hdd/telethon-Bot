from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.db.repositories import Repository
from app.messaging.service import MessageService
from app.models import JobStatus
from tests.conftest import add_destination


@pytest.mark.asyncio
async def test_whitelist_is_enforced(repository: Repository, settings) -> None:
    await add_destination(repository, enabled=False)
    with pytest.raises(ValueError, match="not whitelisted"):
        await MessageService(repository, settings).queue_message("work", text="blocked")


@pytest.mark.asyncio
async def test_idempotency_suppresses_duplicate_and_force_overrides(
    repository: Repository, settings
) -> None:
    await add_destination(repository)
    service = MessageService(repository, settings)

    first = await service.queue_message("work", text="same message")
    duplicate = await service.queue_message("work", text="same message")
    forced = await service.queue_message("work", text="same message", force=True)

    assert duplicate.duplicate is True
    assert duplicate.job.uuid == first.job.uuid
    assert forced.duplicate is False
    assert forced.job.uuid != first.job.uuid


@pytest.mark.asyncio
async def test_media_is_staged_for_later_delivery(
    repository: Repository, settings, tmp_path: Path
) -> None:
    await add_destination(repository)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"not-a-real-jpeg-but-readable")

    result = await MessageService(repository, settings).queue_message(
        "work", text="caption", media_path=source
    )
    source.unlink()

    assert result.job.media_path is not None
    assert result.job.media_path.read_bytes() == b"not-a-real-jpeg-but-readable"


@pytest.mark.asyncio
async def test_dry_run_is_persisted_but_not_eligible(repository: Repository, settings) -> None:
    await add_destination(repository)
    dry_settings = settings.model_copy(update={"dry_run": True})

    result = await MessageService(repository, dry_settings).queue_message("work", text="preview")

    assert result.job.status == JobStatus.DRY_RUN


@pytest.mark.asyncio
async def test_malformed_unicode_is_rejected(repository: Repository, settings) -> None:
    await add_destination(repository)
    with pytest.raises(ValueError, match="malformed Unicode"):
        await MessageService(repository, settings).queue_message("work", text="bad\ud800")


@pytest.mark.asyncio
async def test_naive_service_schedule_is_rejected(repository: Repository, settings) -> None:
    await add_destination(repository)
    with pytest.raises(ValueError, match="timezone"):
        await MessageService(repository, settings).queue_message(
            "work", text="later", scheduled_at=datetime(2026, 9, 16, 14, 30)
        )
