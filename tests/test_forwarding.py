from types import SimpleNamespace

import pytest

from app.commands.saved_messages import (
    SavedCommandKind,
    SavedMessagesController,
    parse_saved_command,
)
from app.messaging.service import MessageService
from app.models import NewDestination
from app.telegram.sender import TelegramSender


class ForwardClient:
    def __init__(self, source=None):
        self.source = source
        self.forwards = []

    async def get_messages(self, _chat, *, ids):
        return self.source if ids == 77 else None

    async def forward_messages(self, destination, message_id, *, from_peer):
        self.forwards.append((destination, message_id, from_peer))
        return SimpleNamespace(id=987)


class ControlSender:
    def __init__(self):
        self.replies = []

    async def send_control_reply(self, text):
        self.replies.append(text)
        return len(self.replies)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("/forward family", SavedCommandKind.FORWARD),
        ("/forwardmulti family,work", SavedCommandKind.FORWARD_MULTI),
        ("/forwardset customers", SavedCommandKind.FORWARD_SET),
        ("/forwardall", SavedCommandKind.FORWARD_ALL),
    ],
)
def test_forward_command_parses_without_content(raw, kind):
    command = parse_saved_command(raw)
    assert command is not None
    assert command.kind == kind
    assert command.text is None


@pytest.mark.asyncio
async def test_forward_jobs_preserve_source_and_use_native_api(repository, settings):
    await repository.upsert_destinations(
        [NewDestination(-1001, "Family", None, "supergroup", True)]
    )
    await repository.set_destination_alias(-1001, "family")
    await repository.set_destination_enabled(-1001, True)
    service = MessageService(repository, settings)
    first = await service.queue_forward("family", source_chat_id=1, source_message_id=77)
    duplicate = await service.queue_forward("family", source_chat_id=1, source_message_id=77)
    assert duplicate.duplicate
    assert first.job.source_chat_id == 1
    assert first.job.source_message_id == 77
    client = ForwardClient(
        SimpleNamespace(id=77, noforwards=False, fwd_from=object(), message="hello", media=None)
    )
    assert await TelegramSender(client).send(first.job) == 987
    assert client.forwards == [(-1001, 77, 1)]


@pytest.mark.asyncio
async def test_missing_or_protected_source_fails_before_send(repository, settings):
    await repository.upsert_destinations(
        [NewDestination(-1001, "Family", None, "supergroup", True)]
    )
    await repository.set_destination_enabled(-1001, True)
    job = (
        await MessageService(repository, settings).queue_forward(
            -1001, source_chat_id=1, source_message_id=77
        )
    ).job
    for source in (
        None,
        SimpleNamespace(id=77, noforwards=True, fwd_from=object(), message="hello", media=None),
    ):
        client = ForwardClient(source)
        with pytest.raises(Exception, match="source|protected"):
            await TelegramSender(client).send(job)
        assert client.forwards == []


@pytest.mark.asyncio
async def test_only_fresh_reply_command_queues_forward(repository, settings):
    await repository.upsert_destinations(
        [NewDestination(-1001, "Family", None, "supergroup", True)]
    )
    await repository.set_destination_alias(-1001, "family")
    await repository.set_destination_enabled(-1001, True)
    source = SimpleNamespace(
        id=77, fwd_from=object(), message="forwarded content", media=None, noforwards=False
    )
    client = ForwardClient(source)
    sender = ControlSender()
    controller = SavedMessagesController(
        client, sender, repository, MessageService(repository, settings), 1
    )
    forwarded_command = SimpleNamespace(
        chat_id=1,
        sender_id=1,
        out=True,
        raw_text="/forward family",
        message=SimpleNamespace(fwd_from=object(), reply_to_msg_id=77),
    )
    await controller.handle(forwarded_command)
    assert await repository.list_jobs() == []
    fresh_reply = SimpleNamespace(
        chat_id=1,
        sender_id=1,
        out=True,
        raw_text="/forward family",
        message=SimpleNamespace(fwd_from=None, reply_to_msg_id=77),
    )
    await controller.handle(fresh_reply)
    assert len(await repository.list_jobs()) == 1
    assert "forward queued: 1" in sender.replies[0]
