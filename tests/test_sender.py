import asyncio

import pytest

from app.telegram.sender import TelegramSender


class CancellingClient:
    async def send_message(self, *args: object, **kwargs: object) -> object:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_sender_does_not_translate_task_cancellation() -> None:
    sender = TelegramSender(CancellingClient())  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await sender.send_control_reply("test")
