from __future__ import annotations

import logging

from telethon import TelegramClient, types, utils

from app.db.repositories import Repository
from app.models import Destination, NewDestination
from app.telegram.errors import ClassifiedTelegramError, classify_telegram_error

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


def _is_group(entity: object) -> bool:
    if isinstance(entity, types.Chat):
        return True
    return isinstance(entity, types.Channel) and bool(
        getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
    )


def _is_private_invite(reference: str) -> bool:
    normalized = reference.lower().replace("http://", "https://")
    return "t.me/+" in normalized or "t.me/joinchat/" in normalized


async def allow_group(
    client: TelegramClient,
    repository: Repository,
    reference: str,
    alias: str | None = None,
) -> Destination:
    """Resolve a joined group, cache it, and explicitly whitelist it."""

    cached = await repository.resolve_destination(reference)
    if cached is None:
        if _is_private_invite(reference):
            raise ValueError(
                "Private invite links are not joined automatically. Join the group first, "
                "then run /groups refresh and add its ID."
            )
        lookup: str | int = reference.strip()
        if str(lookup).lstrip("-").isdigit():
            lookup = int(lookup)
        try:
            entity = await client.get_entity(lookup)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f'Group "{reference}" was not found. Use a public link, @username, or cached ID.'
            ) from exc
        except ClassifiedTelegramError:
            raise
        except Exception as exc:
            raise classify_telegram_error(exc) from exc

        if not _is_group(entity):
            raise ValueError(f'"{reference}" is not a Telegram group or supergroup.')
        if bool(getattr(entity, "left", False)):
            raise ValueError(f'The account must join "{reference}" before it can be whitelisted.')
        if not _can_send(entity):
            raise ValueError(f'The account cannot send messages to "{reference}".')

        chat_id = int(utils.get_peer_id(entity))
        await repository.upsert_destinations(
            [
                NewDestination(
                    telegram_chat_id=chat_id,
                    title=getattr(entity, "title", None) or str(chat_id),
                    username=getattr(entity, "username", None),
                    chat_type=_chat_type(entity),
                    can_send=True,
                )
            ]
        )
        cached = await repository.resolve_destination(chat_id)
        assert cached is not None

    if not cached.can_send:
        raise ValueError(f'The account cannot send messages to "{cached.title}".')
    destination = await repository.set_destination_enabled(cached.telegram_chat_id, True)
    if alias is not None:
        destination = await repository.set_destination_alias(destination.telegram_chat_id, alias)
    return destination


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
