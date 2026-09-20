from __future__ import annotations

import logging

from telethon import TelegramClient, types, utils

from app.db.repositories import Repository
from app.models import NewDestination

logger = logging.getLogger(__name__)


def _can_send(entity: object) -> bool:
    if bool(getattr(entity, "left", False)) or bool(getattr(entity, "broadcast", False)):
        return False
    admin_rights = getattr(entity, "admin_rights", None)
    if admin_rights is not None:
        return True
    banned = getattr(entity, "default_banned_rights", None)
    return not bool(getattr(banned, "send_messages", False))


def _chat_type(entity: object) -> str:
    if isinstance(entity, types.Channel):
        return "supergroup" if entity.megagroup else "channel"
    return "group"


async def refresh_dialogs(client: TelegramClient, repository: Repository) -> int:
    discovered: list[NewDestination] = []
    async for dialog in client.iter_dialogs():
        if not dialog.is_group:
            continue
        entity = dialog.entity
        discovered.append(
            NewDestination(
                telegram_chat_id=int(utils.get_peer_id(entity)),
                title=dialog.name or str(utils.get_peer_id(entity)),
                username=getattr(entity, "username", None),
                chat_type=_chat_type(entity),
                can_send=_can_send(entity),
            )
        )
    count = await repository.upsert_destinations(discovered)
    logger.info("dialogs_refreshed", extra={"destination_count": count})
    return count
