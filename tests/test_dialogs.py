from __future__ import annotations

import pytest
from telethon import types

from app.telegram.dialogs import allow_group


class GroupClient:
    def __init__(self, entity: object) -> None:
        self.entity = entity
        self.references: list[object] = []

    async def get_entity(self, reference: object) -> object:
        self.references.append(reference)
        return self.entity


def telegram_group() -> types.Chat:
    return types.Chat(
        id=123,
        title="Telegram Work Group",
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=None,
        version=1,
        left=False,
    )


async def test_allow_group_resolves_and_whitelists_public_reference(repository) -> None:
    client = GroupClient(telegram_group())

    destination = await allow_group(  # type: ignore[arg-type]
        client, repository, "@work_group", "work"
    )

    assert client.references == ["@work_group"]
    assert destination.enabled is True
    assert destination.can_send is True
    assert destination.alias == "work"
    assert destination.title == "Telegram Work Group"


async def test_allow_group_uses_cached_id_without_telegram_lookup(repository) -> None:
    client = GroupClient(telegram_group())
    first = await allow_group(client, repository, "@work_group")  # type: ignore[arg-type]
    client.references.clear()

    destination = await allow_group(  # type: ignore[arg-type]
        client, repository, str(first.telegram_chat_id), "cached"
    )

    assert client.references == []
    assert destination.alias == "cached"


async def test_allow_group_does_not_auto_join_private_invite(repository) -> None:
    client = GroupClient(telegram_group())

    with pytest.raises(ValueError, match="not joined automatically"):
        await allow_group(  # type: ignore[arg-type]
            client, repository, "https://t.me/+private-invite"
        )

    assert client.references == []
