from __future__ import annotations

import time

from app.db.repositories import Repository
from app.messaging.queue import QueueWorker, retry_delay
from app.messaging.service import MessageService
from app.models import JobStatus, MessageJob
from app.telegram.errors import ClassifiedTelegramError, ErrorCategory
from tests.conftest import add_destination


class FakeSender:
    def __init__(self, result: int | ClassifiedTelegramError = 321) -> None:
        self.result = result
        self.sent: list[MessageJob] = []
        self.replies: list[str] = []

    async def send(self, job: MessageJob) -> int:
        self.sent.append(job)
        if isinstance(self.result, ClassifiedTelegramError):
            raise self.result
        return self.result

    async def send_control_reply(self, text: str) -> int:
        self.replies.append(text)
        return 1


async def queued_job(repository: Repository, settings):
    await add_destination(repository)
    return await MessageService(repository, settings).queue_message("work", text="hello")


async def process_with(repository: Repository, settings, outcome):
    result = await queued_job(repository, settings)
    sender = FakeSender(outcome)
    await QueueWorker(repository, sender, settings).process_once()
    job = await repository.get_job(result.job.uuid)
    assert job is not None
    return job, sender


async def test_pending_processing_sent(repository: Repository, settings) -> None:
    job, sender = await process_with(repository, settings, 987)
    assert job.status == JobStatus.SENT
    assert job.telegram_message_id == 987
    assert len(sender.sent) == 1


async def test_network_failure_schedules_retry(repository: Repository, settings) -> None:
    error = ClassifiedTelegramError(
        ErrorCategory.RETRYABLE_NETWORK, "TimeoutError", "temporary", retryable=True
    )
    job, _ = await process_with(repository, settings, error)
    assert job.status == JobStatus.RETRY
    assert job.next_attempt_at is not None
    assert await repository.get_state("connectivity") == "RECONNECTING"
    assert retry_delay(1, jitter=0) == 5
    assert retry_delay(5, jitter=0) == 300


async def test_flood_wait_is_persisted(repository: Repository, settings) -> None:
    error = ClassifiedTelegramError(
        ErrorCategory.RATE_LIMITED,
        "FloodWaitError",
        "wait",
        wait_seconds=30,
        retryable=True,
    )
    job, _ = await process_with(repository, settings, error)
    assert job.status == JobStatus.WAITING_RATE_LIMIT
    assert job.next_attempt_at is not None


async def test_long_flood_wait_pauses_all_sends(repository: Repository, settings) -> None:
    error = ClassifiedTelegramError(
        ErrorCategory.RATE_LIMITED,
        "FloodWaitError",
        "wait",
        wait_seconds=settings.max_automatic_flood_wait_seconds + 1,
        retryable=True,
    )
    job, _ = await process_with(repository, settings, error)
    assert job.status == JobStatus.WAITING_RATE_LIMIT
    assert await repository.get_state("outgoing_pause_reason") == "TELEGRAM_FLOOD_WAIT"


async def test_rate_limit_stops_after_configured_attempts(repository: Repository, settings) -> None:
    one_attempt = settings.model_copy(update={"max_queue_attempts": 1})
    error = ClassifiedTelegramError(
        ErrorCategory.RATE_LIMITED,
        "SlowModeWaitError",
        "wait",
        wait_seconds=10,
        retryable=True,
    )
    job, _ = await process_with(repository, one_attempt, error)
    assert job.status == JobStatus.FAILED
    assert job.next_attempt_at is None


async def test_permission_error_fails_and_disables_destination(
    repository: Repository, settings
) -> None:
    error = ClassifiedTelegramError(
        ErrorCategory.PERMISSION_ERROR, "ChatWriteForbiddenError", "cannot send"
    )
    job, _ = await process_with(repository, settings, error)
    destination = await repository.resolve_destination("work")
    assert job.status == JobStatus.FAILED
    assert destination is not None and destination.can_send is False


async def test_permission_error_cancels_other_jobs_for_destination(
    repository: Repository, settings
) -> None:
    await add_destination(repository)
    service = MessageService(repository, settings)
    first = await service.queue_message("work", text="first")
    second = await service.queue_message("work", text="second")
    error = ClassifiedTelegramError(
        ErrorCategory.PERMISSION_ERROR, "ChatWriteForbiddenError", "cannot send"
    )

    await QueueWorker(repository, FakeSender(error), settings).process_once()

    failed = await repository.get_job(first.job.uuid)
    cancelled = await repository.get_job(second.job.uuid)
    assert failed is not None and failed.status == JobStatus.FAILED
    assert cancelled is not None and cancelled.status == JobStatus.CANCELLED
    assert cancelled.last_error_type == "CANCELLED_DESTINATION_UNAVAILABLE"


async def test_auth_error_stops_worker(repository: Repository, settings) -> None:
    await add_destination(repository)
    result = await MessageService(repository, settings).queue_message("work", text="hello")
    error = ClassifiedTelegramError(ErrorCategory.AUTH_ERROR, "UnauthorizedError", "login required")
    worker = QueueWorker(repository, FakeSender(error), settings)
    await worker.process_once()
    job = await repository.get_job(result.job.uuid)
    assert job is not None and job.status == JobStatus.FAILED
    assert worker.stop_event.is_set()
    assert await repository.get_state("connectivity") == "AUTH_REQUIRED"


async def test_shutdown_during_pacing_safely_releases_claim(
    repository: Repository, settings
) -> None:
    result = await queued_job(repository, settings)
    slow_settings = settings.model_copy(update={"default_send_interval_seconds": 100})
    sender = FakeSender()
    worker = QueueWorker(repository, sender, slow_settings)
    worker._last_send_finished = time.monotonic()
    worker.request_stop()

    await worker.process_once()

    job = await repository.get_job(result.job.uuid)
    assert job is not None
    assert job.status == JobStatus.RETRY
    assert job.last_error_type == "SHUTDOWN_BEFORE_SEND"
    assert sender.sent == []
