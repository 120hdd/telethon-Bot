import asyncio

import pytest

from app.telegram.sender import TelegramSender


class CancellingClient:
    async def send_message(self, *args: object, **kwargs: object) -> object:
        raise asyncio.CancelledError


class EditingClient:
    def __init__(self) -> None:
        self.call: tuple[object, ...] | None = None

    async def edit_message(self, *args: object, **kwargs: object) -> object:
        self.call = (*args, kwargs)
        return type("Message", (), {"id": 42})()


class RecordingClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, _: str, text: str, **__: object) -> object:
        self.messages.append(text)
        return type("Message", (), {"id": len(self.messages)})()


@pytest.mark.asyncio
async def test_sender_does_not_translate_task_cancellation() -> None:
    sender = TelegramSender(CancellingClient())  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await sender.send_control_reply("test")


@pytest.mark.asyncio
async def test_sender_edits_saved_message_without_parse_mode() -> None:
    client = EditingClient()
    sender = TelegramSender(client)  # type: ignore[arg-type]

    message_id = await sender.edit_control_message(12, "live logs")

    assert message_id == 42
    assert client.call == ("me", 12, "live logs", {"parse_mode": None})


@pytest.mark.asyncio
async def test_sender_splits_long_control_replies_on_line_boundaries() -> None:
    client = RecordingClient()
    sender = TelegramSender(client)  # type: ignore[arg-type]

    message_id = await sender.send_control_reply(("group entry\n" * 400).strip())

    assert message_id == 2
    assert len(client.messages) == 2
    assert all(len(message) <= 3800 for message in client.messages)
