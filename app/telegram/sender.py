from __future__ import annotations

from typing import Protocol

from telethon import TelegramClient

from app.models import MessageJob
from app.telegram.errors import ClassifiedTelegramError, classify_telegram_error


class MessageSender(Protocol):
    async def send(self, job: MessageJob) -> int: ...

    async def send_control_reply(self, text: str) -> int: ...


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
            message = await self._client.send_message("me", text, parse_mode=None)
        except Exception as exc:
            raise classify_telegram_error(exc) from exc
        return int(message.id)
