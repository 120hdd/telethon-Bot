from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta

from app.config import Settings
from app.db.repositories import Repository
from app.models import JobStatus, MessageJob
from app.telegram.errors import ClassifiedTelegramError, ErrorCategory
from app.telegram.sender import MessageSender
from app.utils.time import from_db_time, to_db_time, utc_now

logger = logging.getLogger(__name__)
RETRY_DELAYS = (5, 15, 45, 120, 300)


def retry_delay(attempt_count: int, *, jitter: float | None = None) -> float:
    base = RETRY_DELAYS[min(max(attempt_count - 1, 0), len(RETRY_DELAYS) - 1)]
    variation = random.uniform(0, min(base * 0.2, 10)) if jitter is None else jitter
    return base + variation


class QueueWorker:
    def __init__(self, repository: Repository, sender: MessageSender, settings: Settings) -> None:
        self.repository = repository
        self.sender = sender
        self.settings = settings
        self.stop_event = asyncio.Event()
        self._last_send_finished: float | None = None

    def request_stop(self) -> None:
        self.stop_event.set()

    async def run(self) -> None:
        logger.info("queue_worker_started")
        while not self.stop_event.is_set():
            processed = await self.process_once()
            if not processed:
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=self.settings.worker_poll_seconds
                    )
                except TimeoutError:
                    pass
        logger.info("queue_worker_stopped")

    async def process_once(self) -> bool:
        if await self._is_globally_paused():
            return False
        now = to_db_time(utc_now())
        await self.repository.promote_due_scheduled(now)
        await self.repository.cancel_disabled_jobs()
        job = await self.repository.claim_next_job(now)
        if job is None:
            return False

        logger.info(
            "job_claimed",
            extra={
                "job_id": job.uuid,
                "chat_id": job.destination_chat_id,
                "attempt": job.attempt_count,
            },
        )
        if not await self._pace():
            await self.repository.transition_job(
                job.id,
                JobStatus.RETRY,
                next_attempt_at=to_db_time(utc_now()),
                error_type="SHUTDOWN_BEFORE_SEND",
                error_message="Worker stopped before the Telegram request began",
            )
            return True
        destination = await self.repository.resolve_destination(job.destination_chat_id)
        if destination is None or not destination.enabled:
            await self.repository.transition_job(
                job.id,
                JobStatus.FAILED,
                error_type="DESTINATION_DISABLED",
                error_message="Destination was disabled before delivery",
            )
            await self._notify_saved_messages(
                job,
                f"failed\njob: {job.uuid}\nreason: destination was disabled before delivery",
            )
            return True
        if job.media_path is not None and not job.media_path.is_file():
            await self.repository.transition_job(
                job.id,
                JobStatus.FAILED,
                error_type="MEDIA_MISSING",
                error_message="Queued media file no longer exists",
            )
            await self._notify_saved_messages(
                job,
                f"failed\njob: {job.uuid}\nreason: queued media file no longer exists",
            )
            return True

        started = time.monotonic()
        logger.info(
            "message_send_started",
            extra={
                "job_id": job.uuid,
                "chat_id": job.destination_chat_id,
                "attempt": job.attempt_count,
            },
        )
        try:
            telegram_message_id = await self.sender.send(job)
        except ClassifiedTelegramError as exc:
            await self._handle_error(job, exc)
        else:
            await self.repository.transition_job(
                job.id, JobStatus.SENT, telegram_message_id=telegram_message_id
            )
            await self.repository.set_state("connectivity", "CONNECTED")
            logger.info(
                "message_sent",
                extra={
                    "job_id": job.uuid,
                    "chat_id": job.destination_chat_id,
                    "telegram_message_id": telegram_message_id,
                    "attempt": job.attempt_count,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            await self._notify_saved_messages(
                job,
                f"sent\njob: {job.uuid}\ntelegram_message_id: {telegram_message_id}",
            )
        finally:
            self._last_send_finished = time.monotonic()
        return True

    async def _pace(self) -> bool:
        if self._last_send_finished is None:
            return True
        elapsed = time.monotonic() - self._last_send_finished
        remaining = self.settings.default_send_interval_seconds - elapsed
        if remaining > 0:
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=remaining)
            except TimeoutError:
                pass
        return not self.stop_event.is_set()

    async def _is_globally_paused(self) -> bool:
        reason = await self.repository.get_state("outgoing_pause_reason")
        if not reason:
            return False
        until = await self.repository.get_state("outgoing_pause_until")
        if until and from_db_time(until) <= utc_now():
            await self.repository.set_state("outgoing_pause_reason", None)
            await self.repository.set_state("outgoing_pause_until", None)
            await self.repository.set_state("connectivity", "CONNECTED")
            return False
        if reason == "TELEGRAM_FLOOD_WAIT":
            await self.repository.set_state("connectivity", "RATE_LIMITED")
        return True

    async def _handle_error(self, job: MessageJob, exc: ClassifiedTelegramError) -> None:
        fields = {
            "job_id": job.uuid,
            "chat_id": job.destination_chat_id,
            "attempt": job.attempt_count,
            "exception_type": exc.error_type,
        }
        if exc.category == ErrorCategory.RATE_LIMITED:
            wait = max(exc.wait_seconds or 1, 1)
            is_slow_mode = exc.error_type == "SlowModeWaitError"
            safety_margin = 0 if is_slow_mode else 2
            next_attempt = to_db_time(utc_now() + timedelta(seconds=wait + safety_margin))
            logger.warning(
                "telegram_slow_mode" if is_slow_mode else "telegram_flood_wait",
                extra={**fields, "wait_seconds": wait},
            )
            if not is_slow_mode and wait > self.settings.max_automatic_flood_wait_seconds:
                await self.repository.set_state("outgoing_pause_reason", "TELEGRAM_FLOOD_WAIT")
                await self.repository.set_state("outgoing_pause_until", next_attempt)
                await self.repository.set_state("connectivity", "RATE_LIMITED")
            if job.attempt_count >= job.max_attempts:
                await self.repository.transition_job(
                    job.id,
                    JobStatus.FAILED,
                    error_type=exc.error_type,
                    error_message=f"Retry limit reached. {exc.safe_message}",
                )
                await self._notify_saved_messages(
                    job, f"failed\njob: {job.uuid}\nreason: retry limit reached"
                )
                return
            await self.repository.transition_job(
                job.id,
                JobStatus.WAITING_RATE_LIMIT,
                next_attempt_at=next_attempt,
                error_type=exc.error_type,
                error_message=exc.safe_message,
            )
            return

        if exc.category == ErrorCategory.RETRYABLE_NETWORK and job.attempt_count < job.max_attempts:
            await self.repository.set_state("connectivity", "RECONNECTING")
            delay = retry_delay(job.attempt_count)
            await self.repository.transition_job(
                job.id,
                JobStatus.RETRY,
                next_attempt_at=to_db_time(utc_now() + timedelta(seconds=delay)),
                error_type=exc.error_type,
                error_message=exc.safe_message,
            )
            logger.warning("message_retry_scheduled", extra={**fields, "wait_seconds": delay})
            return

        if exc.category == ErrorCategory.AUTH_ERROR:
            await self.repository.set_state("connectivity", "AUTH_REQUIRED")
            await self.repository.set_state("outgoing_pause_reason", "AUTH_REQUIRED")
            self.request_stop()

        if exc.category in {ErrorCategory.PERMISSION_ERROR, ErrorCategory.DESTINATION_ERROR}:
            await self.repository.set_destination_can_send(job.destination_chat_id, False)

        await self.repository.transition_job(
            job.id,
            JobStatus.FAILED,
            error_type=exc.error_type,
            error_message=exc.safe_message,
        )
        logger.error("message_failed", extra=fields)
        await self._notify_saved_messages(
            job, f"failed\njob: {job.uuid}\nreason: {exc.safe_message}"
        )

    async def _notify_saved_messages(self, job: MessageJob, text: str) -> None:
        if job.requested_by != "saved_messages":
            return
        try:
            await self.sender.send_control_reply(text)
        except ClassifiedTelegramError as exc:
            logger.warning(
                "control_reply_failed",
                extra={"job_id": job.uuid, "exception_type": exc.error_type},
            )
