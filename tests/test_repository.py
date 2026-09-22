from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.database import Database
from app.db.repositories import Repository
from app.messaging.service import MessageService
from app.models import JobStatus
from app.utils.time import to_db_time, utc_now
from tests.conftest import add_destination


@pytest.mark.asyncio
async def test_deny_cancels_unsent_jobs(repository: Repository, settings) -> None:
    chat_id = await add_destination(repository)
    service = MessageService(repository, settings)
    result = await service.queue_message(chat_id, text="hello")

    await repository.set_destination_enabled(chat_id, False)

    job = await repository.get_job(result.job.uuid)
    assert job is not None
    assert job.status == JobStatus.CANCELLED
    assert job.last_error_type == "CANCELLED_DESTINATION_DISABLED"


@pytest.mark.asyncio
async def test_queue_claim_is_atomic_and_increments_attempt(
    repository: Repository, settings
) -> None:
    await add_destination(repository)
    result = await MessageService(repository, settings).queue_message("work", text="hello")

    claimed = await repository.claim_next_job(to_db_time(utc_now()))
    second_claim = await repository.claim_next_job(to_db_time(utc_now()))

    assert claimed is not None
    assert claimed.uuid == result.job.uuid
    assert claimed.status == JobStatus.PROCESSING
    assert claimed.attempt_count == 1
    assert second_claim is None


@pytest.mark.asyncio
async def test_scheduled_job_promotes_only_when_due(repository: Repository, settings) -> None:
    await add_destination(repository)
    due = utc_now() + timedelta(hours=1)
    result = await MessageService(repository, settings).queue_message(
        "work", text="later", scheduled_at=due
    )

    assert await repository.promote_due_scheduled(to_db_time(utc_now())) == 0
    assert await repository.promote_due_scheduled(to_db_time(due + timedelta(seconds=1))) == 1
    job = await repository.get_job(result.job.uuid)
    assert job is not None and job.status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_restart_moves_processing_to_review(repository: Repository, settings) -> None:
    await add_destination(repository)
    result = await MessageService(repository, settings).queue_message("work", text="uncertain")
    await repository.claim_next_job(to_db_time(utc_now()))

    assert await repository.recover_stale_processing() == 1
    job = await repository.get_job(result.job.uuid)
    assert job is not None
    assert job.status == JobStatus.REVIEW_REQUIRED
    assert job.last_error_type == "DELIVERY_UNCERTAIN_AFTER_RESTART"


@pytest.mark.asyncio
async def test_manual_retry_resets_attempt_budget(repository: Repository, settings) -> None:
    await add_destination(repository)
    result = await MessageService(repository, settings).queue_message("work", text="retry me")
    claimed = await repository.claim_next_job(to_db_time(utc_now()))
    assert claimed is not None
    await repository.transition_job(
        claimed.id,
        JobStatus.FAILED,
        error_type="TEST_FAILURE",
        error_message="test",
    )

    retried = await repository.retry_job(result.job.uuid)

    assert retried.status == JobStatus.RETRY
    assert retried.attempt_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting_status", [JobStatus.RETRY, JobStatus.WAITING_RATE_LIMIT])
async def test_retryable_state_can_be_claimed_and_sent(
    repository: Repository, settings, waiting_status: JobStatus
) -> None:
    await add_destination(repository)
    result = await MessageService(repository, settings).queue_message(
        "work", text=f"transition {waiting_status}"
    )
    first_claim = await repository.claim_next_job(to_db_time(utc_now()))
    assert first_claim is not None
    await repository.transition_job(
        first_claim.id,
        waiting_status,
        next_attempt_at=to_db_time(utc_now()),
        error_type="TEST_RETRY",
        error_message="test",
    )

    second_claim = await repository.claim_next_job(to_db_time(utc_now()))
    assert second_claim is not None
    sent = await repository.transition_job(second_claim.id, JobStatus.SENT, telegram_message_id=123)

    assert sent.uuid == result.job.uuid
    assert sent.status == JobStatus.SENT
    assert sent.attempt_count == 2


@pytest.mark.asyncio
async def test_initialize_migrates_existing_jobs_without_data_loss(tmp_path) -> None:
    database = Database(tmp_path / "old.db")
    await database.connect()
    connection = database.require_connection()
    await connection.executescript(
        """
        CREATE TABLE destinations (
            telegram_chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT,
            alias TEXT UNIQUE, chat_type TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
            can_send INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
        );
        CREATE TABLE message_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
            destination_chat_id INTEGER NOT NULL REFERENCES destinations(telegram_chat_id),
            text TEXT, media_path TEXT, parse_mode TEXT,
            disable_link_preview INTEGER NOT NULL DEFAULT 0, scheduled_at TEXT NOT NULL,
            next_attempt_at TEXT, status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL, telegram_message_id INTEGER, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, started_at TEXT, sent_at TEXT, last_error_type TEXT,
            last_error_message TEXT, idempotency_key TEXT NOT NULL,
            requested_by TEXT NOT NULL DEFAULT 'cli'
        );
        INSERT INTO destinations VALUES(
            -1001, 'Existing', NULL, 'existing', 'supergroup', 1, 1,
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00'
        );
        INSERT INTO message_jobs(
            uuid, destination_chat_id, text, scheduled_at, status, max_attempts,
            created_at, updated_at, idempotency_key, requested_by
        ) VALUES(
            'existing-job', -1001, 'preserve me', '2026-01-01T00:00:00+00:00',
            'PENDING', 3, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
            'existing-key', 'cli'
        );
        """
    )
    await connection.commit()

    try:
        repository = Repository(database)
        await repository.initialize()
        existing = await repository.get_job("existing-job")
        columns = await connection.execute("PRAGMA table_info(message_jobs)")

        assert existing is not None
        assert existing.text == "preserve me"
        assert existing.batch_id is None
        assert "batch_id" in {row["name"] for row in await columns.fetchall()}
        assert await repository.get_group_set("anything") is None
    finally:
        await database.close()
