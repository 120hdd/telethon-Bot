from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.db.repositories import Repository
from app.messaging.idempotency import build_idempotency_key
from app.messaging.service import MessageService
from app.models import JobStatus, NewJob
from app.utils.time import to_db_time, utc_now
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
async def test_markdown_send_is_not_a_duplicate_of_a_plain_send(
    repository: Repository, settings
) -> None:
    await add_destination(repository)
    service = MessageService(repository, settings)

    plain = await service.queue_message("work", text="*bold intent*")
    markdown = await service.queue_message("work", text="*bold intent*", parse_mode="md")

    assert markdown.duplicate is False
    assert markdown.job.uuid != plain.job.uuid
    assert markdown.job.parse_mode == "md"


@pytest.mark.asyncio
async def test_equivalent_parse_mode_spellings_still_deduplicate(
    repository: Repository, settings
) -> None:
    await add_destination(repository)
    service = MessageService(repository, settings)

    first = await service.queue_message("work", text="*same*", parse_mode="md")
    again = await service.queue_message("work", text="*same*", parse_mode="markdown")

    assert again.duplicate is True
    assert again.job.uuid == first.job.uuid


@pytest.mark.asyncio
async def test_suppressing_the_link_preview_is_not_a_duplicate(
    repository: Repository, settings
) -> None:
    await add_destination(repository)
    service = MessageService(repository, settings)

    previewing = await service.queue_message("work", text="https://example.com")
    suppressed = await service.queue_message(
        "work", text="https://example.com", disable_link_preview=True
    )

    assert suppressed.duplicate is False
    assert suppressed.job.uuid != previewing.job.uuid
    assert suppressed.job.disable_link_preview is True


@pytest.mark.asyncio
async def test_caption_parse_mode_is_not_a_duplicate(
    repository: Repository, settings, tmp_path: Path
) -> None:
    await add_destination(repository)
    service = MessageService(repository, settings)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"not-a-real-jpeg-but-readable")

    plain = await service.queue_message("work", text="caption", media_path=source)
    markdown = await service.queue_message(
        "work", text="caption", media_path=source, parse_mode="md"
    )

    assert markdown.duplicate is False
    assert markdown.job.uuid != plain.job.uuid


@pytest.mark.asyncio
async def test_legacy_formatted_send_keeps_its_duplicate_history(
    repository: Repository, settings
) -> None:
    chat_id = await add_destination(repository)
    old = await repository.create_job(
        NewJob(
            uuid=str(uuid4()),
            destination_chat_id=chat_id,
            text="*same*",
            media_path=None,
            parse_mode="md",
            disable_link_preview=False,
            scheduled_at=to_db_time(utc_now()),
            status=JobStatus.SENT,
            max_attempts=3,
            idempotency_key=build_idempotency_key(chat_id, "*same*", "", "IMMEDIATE"),
            requested_by="cli",
        )
    )
    service = MessageService(repository, settings)

    repeated = await service.queue_message(chat_id, text="*same*", parse_mode="markdown")
    plain = await service.queue_message(chat_id, text="*same*")

    assert repeated.duplicate is True
    assert repeated.job.uuid == old.uuid
    assert plain.duplicate is False
    assert plain.job.uuid != old.uuid


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
async def test_repeated_dry_run_is_suppressed(repository: Repository, settings) -> None:
    await add_destination(repository)
    dry_settings = settings.model_copy(update={"dry_run": True})
    service = MessageService(repository, dry_settings)

    first = await service.queue_message("work", text="preview")
    again = await service.queue_message("work", text="preview")
    forced = await service.queue_message("work", text="preview", force=True)

    assert again.duplicate is True
    assert again.job.uuid == first.job.uuid
    assert forced.duplicate is False


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
