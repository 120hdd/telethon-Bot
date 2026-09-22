from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from app.db.repositories import Repository
from app.messaging.service import MessageService
from app.messaging.targets import GROUP_CHAT_TYPES
from app.models import Destination, MessageBatch

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class BulkEnqueueResult:
    batch_id: str
    requested: int
    eligible: int
    queued: int
    duplicates: int
    skipped: int
    failed: int


class BulkMessageService:
    """Create destination-level jobs while leaving delivery to the normal queue worker."""

    def __init__(self, repository: Repository, message_service: MessageService) -> None:
        self.repository = repository
        self.message_service = message_service

    async def enqueue_bulk(
        self,
        destinations: Iterable[Destination],
        *,
        text: str,
        batch_type: str,
        requested: int,
        duplicates: int = 0,
        skipped: int = 0,
        actor: str = "saved_messages_bulk",
    ) -> BulkEnqueueResult:
        snapshot: list[Destination] = []
        seen: set[int] = set()
        defensive_duplicates = 0
        defensive_skipped = 0
        for destination in destinations:
            if destination.telegram_chat_id in seen:
                defensive_duplicates += 1
                continue
            seen.add(destination.telegram_chat_id)
            if (
                destination.chat_type not in GROUP_CHAT_TYPES
                or not destination.enabled
                or not destination.can_send
            ):
                defensive_skipped += 1
                continue
            snapshot.append(destination)

        batch_id = str(uuid.uuid4())
        await self.repository.create_batch(batch_id, batch_type, requested)
        queued = 0
        existing_duplicates = 0
        failed = 0
        for destination in snapshot:
            try:
                result = await self.message_service.queue_message(
                    destination.telegram_chat_id,
                    text=text,
                    actor=actor,
                    batch_id=batch_id,
                )
            except (ValueError, OSError):
                failed += 1
                logger.exception(
                    "bulk_destination_enqueue_failed",
                    extra={"batch_id": batch_id, "chat_id": destination.telegram_chat_id},
                )
            else:
                if result.duplicate:
                    existing_duplicates += 1
                else:
                    queued += 1

        result = BulkEnqueueResult(
            batch_id=batch_id,
            requested=requested,
            eligible=len(snapshot),
            queued=queued,
            duplicates=duplicates + defensive_duplicates + existing_duplicates,
            skipped=skipped + defensive_skipped,
            failed=failed,
        )
        logger.info(
            "bulk_queued",
            extra={
                "command_type": batch_type,
                "batch_id": batch_id,
                "requested": result.requested,
                "resolved": len(snapshot) + result.duplicates,
                "eligible": result.eligible,
                "queued": result.queued,
                "duplicates": result.duplicates,
                "skipped": result.skipped,
                "failed": result.failed,
            },
        )
        await self.repository.add_audit(
            "bulk_queued",
            actor=actor,
            entity_type="batch",
            entity_id=batch_id,
            details=(
                f"type={batch_type};requested={result.requested};eligible={result.eligible};"
                f"queued={result.queued};duplicates={result.duplicates};"
                f"skipped={result.skipped};failed={result.failed}"
            ),
        )
        return result

    async def batch_status(self, batch_id: str) -> tuple[MessageBatch, dict[str, int]]:
        batch = await self.repository.get_batch(batch_id)
        if batch is None:
            from app.db.repositories import NotFoundError

            raise NotFoundError(f"Batch not found: {batch_id}")
        return batch, await self.repository.batch_counts(batch.id)
