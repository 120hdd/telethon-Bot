from __future__ import annotations

import logging
from typing import Protocol

from telethon import TelegramClient

from app.models import MessageJob
from app.telegram.errors import ClassifiedTelegramError, ErrorCategory, classify_telegram_error

CONTROL_MESSAGE_LIMIT = 3800
logger = logging.getLogger(__name__)


class DryRunMutationError(RuntimeError):
    pass


def _control_chunks(text: str) -> list[str]:
    remaining = text
    chunks: list[str] = []
    while len(remaining) > CONTROL_MESSAGE_LIMIT:
        split_at = remaining.rfind("\n", 0, CONTROL_MESSAGE_LIMIT + 1)
        if split_at <= 0:
            split_at = CONTROL_MESSAGE_LIMIT
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    chunks.append(remaining)
    return chunks


class MessageSender(Protocol):
    async def send(self, job: MessageJob) -> int: ...

    async def send_control_reply(self, text: str) -> int: ...

    async def edit_control_message(self, message_id: int, text: str) -> int: ...


def validate_forward_source(source: object | None) -> None:
    if source is None:
        raise ClassifiedTelegramError(
            ErrorCategory.SOURCE_ERROR,
            "ForwardSourceMissing",
            "Forward source is missing or was deleted from Saved Messages.",
        )
    if getattr(source, "fwd_from", None) is None:
        raise ClassifiedTelegramError(
            ErrorCategory.SOURCE_ERROR,
            "ForwardSourceNotForwarded",
            "Source must be a forwarded message in Saved Messages.",
        )
    if getattr(source, "noforwards", False):
        raise ClassifiedTelegramError(
            ErrorCategory.SOURCE_ERROR,
            "ForwardSourceProtected",
            "Forward source is protected and cannot be forwarded.",
        )
    media = getattr(source, "media", None)
    document = getattr(media, "document", None)
    mime = getattr(document, "mime_type", "") or ""
    special_attributes = {
        "DocumentAttributeSticker",
        "DocumentAttributeAnimated",
        "DocumentAttributeAudio",
    }
    has_special_attribute = any(
        type(attribute).__name__ in special_attributes
        or bool(getattr(attribute, "round_message", False))
        for attribute in (getattr(document, "attributes", None) or ())
    )
    supported = (
        (
            getattr(source, "message", None)
            and (media is None or type(media).__name__ == "MessageMediaWebPage")
        )
        or getattr(media, "photo", None)
        or (document is not None and not mime.startswith("audio/") and not has_special_attribute)
    )
    if not supported:
        raise ClassifiedTelegramError(
            ErrorCategory.SOURCE_ERROR,
            "ForwardSourceUnsupported",
            "Only forwarded text, photos, videos, and documents are supported.",
        )


class TelegramSender:
    """The only application component allowed to invoke Telegram send APIs."""

    def __init__(self, client: TelegramClient, *, dry_run: bool = False) -> None:
        self._client = client
        self._dry_run = dry_run

    def _ensure_mutation_allowed(self, operation: str) -> None:
        if self._dry_run:
            logger.info("dry_run_telegram_mutation_blocked", extra={"operation": operation})
            raise DryRunMutationError(
                f"[DRY-RUN] Telegram mutation blocked: {operation}; send skipped"
            )

    async def send(self, job: MessageJob) -> int:
        operation = (
            "forward_messages"
            if job.source_message_id is not None
            else ("send_file" if job.media_path is not None else "send_message")
        )
        self._ensure_mutation_allowed(operation)
        try:
            if job.source_message_id is not None:
                source = await self._client.get_messages(
                    job.source_chat_id, ids=job.source_message_id
                )
                validate_forward_source(source)
                message = await self._client.forward_messages(
                    job.destination_chat_id, job.source_message_id, from_peer=job.source_chat_id
                )
                if message is None:
                    raise ClassifiedTelegramError(
                        ErrorCategory.SOURCE_ERROR,
                        "ForwardSourceMissing",
                        "Telegram did not return a forwarded message; source may be unavailable.",
                    )
            elif job.media_path is not None:
                message = await self._client.send_file(
                    job.destination_chat_id,
                    str(job.media_path),
                    caption=job.text or None,
                    parse_mode=job.parse_mode,
                )
            else:
                message = await self._client.send_message(
                    job.destination_chat_id,
                    job.text or "",
                    parse_mode=job.parse_mode,
                    link_preview=not job.disable_link_preview,
                )
        except ClassifiedTelegramError:
            raise
        except Exception as exc:
            raise classify_telegram_error(exc) from exc
        return int(message.id)

    async def send_control_reply(self, text: str) -> int:
        self._ensure_mutation_allowed("send_message")
        try:
            message = None
            for chunk in _control_chunks(text):
                message = await self._client.send_message("me", chunk, parse_mode=None)
        except Exception as exc:
            raise classify_telegram_error(exc) from exc
        assert message is not None
        return int(message.id)

    async def edit_control_message(self, message_id: int, text: str) -> int:
        self._ensure_mutation_allowed("edit_message")
        try:
            message = await self._client.edit_message("me", message_id, text, parse_mode=None)
        except Exception as exc:
            raise classify_telegram_error(exc) from exc
        return int(message.id)
