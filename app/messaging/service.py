from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.db.repositories import DuplicateJobError, NotFoundError, Repository
from app.messaging.idempotency import build_idempotency_key, hash_file, normalize_message
from app.models import JobStatus, MessageJob, NewJob
from app.utils.time import to_db_time, utc_now

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class QueueResult:
    job: MessageJob
    duplicate: bool = False


class MessageService:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    async def queue_message(
        self,
        destination_reference: str | int,
        *,
        text: str | None = None,
        media_path: Path | None = None,
        scheduled_at: datetime | None = None,
        parse_mode: str | None = None,
        disable_link_preview: bool = False,
        force: bool = False,
        actor: str = "cli",
    ) -> QueueResult:
        destination = await self.repository.resolve_destination(destination_reference)
        if destination is None:
            raise NotFoundError(
                f"Unknown destination: {destination_reference}. Refresh groups first."
            )
        if not destination.enabled:
            raise ValueError(f'Destination "{destination.title}" is not whitelisted.')
        if not destination.can_send:
            raise ValueError(f'Destination "{destination.title}" does not allow sending.')

        normalized_text = normalize_message(text)
        try:
            normalized_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("Message contains malformed Unicode data.") from exc
        source_media = (
            await asyncio.to_thread(_resolve_media_path, media_path) if media_path else None
        )
        media_hash = ""
        if source_media is not None:
            self._validate_media(source_media)
            media_hash = await asyncio.to_thread(hash_file, source_media)
        if not normalized_text and source_media is None:
            raise ValueError("A message must contain text or a media file.")
        text_limit = 1024 if source_media else 4096
        if len(normalized_text) > text_limit:
            kind = "Media caption" if source_media else "Message"
            raise ValueError(f"{kind} exceeds the {text_limit}-character limit.")
        if parse_mode not in {None, "md", "markdown", "html"}:
            raise ValueError("Parse mode must be md, markdown, html, or omitted.")

        if scheduled_at is not None and scheduled_at.tzinfo is None:
            raise ValueError("Scheduled datetime must include timezone information.")
        now = utc_now()
        due = scheduled_at.astimezone(now.tzinfo) if scheduled_at else now
        schedule_identity = to_db_time(scheduled_at) if scheduled_at else "IMMEDIATE"
        key = build_idempotency_key(
            destination.telegram_chat_id, normalized_text, media_hash, schedule_identity
        )
        job_uuid = str(uuid.uuid4())
        if force:
            key = build_idempotency_key(
                destination.telegram_chat_id,
                normalized_text,
                media_hash,
                f"{schedule_identity}:{job_uuid}",
            )

        staged_media: Path | None = None
        if source_media is not None:
            target_dir = self.settings.uploads_dir.resolve() / job_uuid
            target_dir.mkdir(parents=True, exist_ok=False)
            staged_media = target_dir / source_media.name
            await asyncio.to_thread(shutil.copy2, source_media, staged_media)

        status = (
            JobStatus.DRY_RUN
            if self.settings.dry_run
            else JobStatus.SCHEDULED
            if scheduled_at
            else JobStatus.PENDING
        )
        new_job = NewJob(
            uuid=job_uuid,
            destination_chat_id=destination.telegram_chat_id,
            text=normalized_text or None,
            media_path=staged_media,
            parse_mode=parse_mode,
            disable_link_preview=disable_link_preview,
            scheduled_at=to_db_time(due),
            status=status,
            max_attempts=self.settings.max_queue_attempts,
            idempotency_key=key,
            requested_by=actor,
        )
        try:
            job = await self.repository.create_job(new_job, actor=actor)
        except DuplicateJobError as exc:
            if staged_media is not None:
                await asyncio.to_thread(shutil.rmtree, staged_media.parent, True)
            assert exc.existing is not None
            return QueueResult(exc.existing, duplicate=True)

        logger.info(
            "job_created",
            extra={
                "job_id": job.uuid,
                "chat_id": job.destination_chat_id,
                "message_length": len(normalized_text),
                "message_hash": key,
                "status": job.status.value,
            },
        )
        if self.settings.log_message_bodies and self.settings.log_level.upper() == "DEBUG":
            logger.debug("message_body_debug", extra={"job_id": job.uuid, "body": normalized_text})
        return QueueResult(job)

    def _validate_media(self, path: Path) -> None:
        if not path.is_file():
            raise ValueError(f"Media file does not exist: {path}")
        try:
            size = path.stat().st_size
            with path.open("rb") as source:
                source.read(1)
        except OSError as exc:
            raise ValueError(f"Media file is not readable: {path}") from exc
        if size == 0:
            raise ValueError("Media file is empty.")
        if size > self.settings.max_media_bytes:
            raise ValueError(
                f"Media file is too large ({size} bytes; maximum {self.settings.max_media_bytes})."
            )


def _resolve_media_path(path: Path) -> Path:
    return path.expanduser().resolve()
