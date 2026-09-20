from __future__ import annotations

from typing import Protocol

from telethon import TelegramClient

from app.models import MessageJob
from app.telegram.errors import ClassifiedTelegramError, classify_telegram_error

CONTROL_MESSAGE_LIMIT = 3800


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


class TelegramSender:
    """The only application component allowed to invoke Telegram send APIs."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def send(self, job: MessageJob) -> int:
        try:
            if job.media_path is not None:
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
        try:
            message = None
            for chunk in _control_chunks(text):
                message = await self._client.send_message("me", chunk, parse_mode=None)
        except Exception as exc:
            raise classify_telegram_error(exc) from exc
        assert message is not None
        return int(message.id)

    async def edit_control_message(self, message_id: int, text: str) -> int:
        try:
            message = await self._client.edit_message("me", message_id, text, parse_mode=None)
        except Exception as exc:
            raise classify_telegram_error(exc) from exc
        return int(message.id)
