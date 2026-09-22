from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.db.repositories import NotFoundError, Repository
from app.messaging.targets import GROUP_CHAT_TYPES, TargetError, TargetResolver
from app.models import Destination, GroupSet, GroupSetMember

logger = logging.getLogger(__name__)

_GROUP_SET_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(slots=True, frozen=True)
class GroupSetMutation:
    name: str
    changed: int
    unchanged: int


@dataclass(slots=True, frozen=True)
class GroupSetMemberView:
    member: GroupSetMember
    status: str


@dataclass(slots=True, frozen=True)
class GroupSetSnapshot:
    group_set: GroupSet
    members: tuple[GroupSetMemberView, ...]
    eligible: tuple[Destination, ...]
    allowed: int
    disabled: int
    missing: int
    duplicates: int


class GroupSetService:
    def __init__(self, repository: Repository, resolver: TargetResolver) -> None:
        self.repository = repository
        self.resolver = resolver

    @staticmethod
    def normalize_name(name: str) -> str:
        normalized = name.strip().lower()
        if not _GROUP_SET_NAME.fullmatch(normalized):
            raise ValueError(
                "Group set name must be 1-64 characters using letters, numbers, _ or -."
            )
        return normalized

    async def create(self, name: str, *, actor: str = "saved_messages") -> GroupSet:
        normalized = self.normalize_name(name)
        group_set = await self.repository.create_group_set(normalized, actor=actor)
        logger.info("group_set_mutated", extra={"set_name": normalized, "operation": "create"})
        return group_set

    async def delete(self, name: str, *, actor: str = "saved_messages") -> None:
        normalized = self.normalize_name(name)
        if not await self.repository.delete_group_set(normalized, actor=actor):
            raise NotFoundError(f"Group set not found: {normalized}")
        logger.info("group_set_mutated", extra={"set_name": normalized, "operation": "delete"})

    async def get(self, name: str) -> GroupSet:
        normalized = self.normalize_name(name)
        group_set = await self.repository.get_group_set(normalized)
        if group_set is None:
            raise NotFoundError(f"Group set not found: {normalized}")
        return group_set

    async def list(self) -> list[tuple[GroupSet, int]]:
        return await self.repository.list_group_sets()

    async def members(self, name: str) -> tuple[GroupSet, list[GroupSetMember]]:
        group_set = await self.get(name)
        return group_set, await self.repository.list_group_set_members(group_set.id)

    async def snapshot(self, name: str) -> GroupSetSnapshot:
        group_set, members = await self.members(name)
        refresh_cutoff = await self.resolver.refresh_cutoff()
        views: list[GroupSetMemberView] = []
        eligible: list[Destination] = []
        seen: set[int] = set()
        allowed = disabled = missing = duplicates = 0
        for member in members:
            if member.destination_peer_id in seen:
                duplicates += 1
                views.append(GroupSetMemberView(member, "duplicate"))
                continue
            seen.add(member.destination_peer_id)
            destination = member.destination
            if destination is None or not self.resolver.is_current(destination, refresh_cutoff):
                missing += 1
                views.append(GroupSetMemberView(member, "missing"))
            elif (
                destination.chat_type not in GROUP_CHAT_TYPES
                or not destination.enabled
                or not destination.can_send
            ):
                disabled += 1
                views.append(GroupSetMemberView(member, "disabled"))
            else:
                allowed += 1
                eligible.append(destination)
                views.append(GroupSetMemberView(member, "allowed"))
        return GroupSetSnapshot(
            group_set=group_set,
            members=tuple(views),
            eligible=tuple(eligible),
            allowed=allowed,
            disabled=disabled,
            missing=missing,
            duplicates=duplicates,
        )

    async def add(
        self, name: str, references: Iterable[str], *, actor: str = "saved_messages"
    ) -> GroupSetMutation:
        group_set = await self.get(name)
        resolution = await self.resolver.resolve_targets(references, require_allowed=False)
        if resolution.errors:
            raise ValueError(self._validation_message(resolution.errors))
        added, present = await self.repository.add_group_set_members(
            group_set.id,
            (item.telegram_chat_id for item in resolution.destinations),
            actor=actor,
        )
        unchanged = present + resolution.duplicates
        logger.info(
            "group_set_mutated",
            extra={
                "set_name": group_set.name,
                "operation": "add",
                "changed": added,
                "unchanged": unchanged,
            },
        )
        return GroupSetMutation(group_set.name, added, unchanged)

    async def remove(
        self, name: str, references: Iterable[str], *, actor: str = "saved_messages"
    ) -> GroupSetMutation:
        group_set = await self.get(name)
        destination_ids: list[int] = []
        errors: list[TargetError] = []
        seen: set[int] = set()
        duplicates = 0
        for reference in references:
            resolved = await self.resolver.resolve_target(reference, require_allowed=False)
            if isinstance(resolved, TargetError):
                stripped = reference.strip()
                if stripped.lstrip("-").isdigit():
                    destination_id = int(stripped)
                else:
                    errors.append(resolved)
                    continue
            else:
                destination_id = resolved.telegram_chat_id
            if destination_id in seen:
                duplicates += 1
                continue
            seen.add(destination_id)
            destination_ids.append(destination_id)
        if errors:
            raise ValueError(self._validation_message(errors))
        removed, absent = await self.repository.remove_group_set_members(
            group_set.id, destination_ids, actor=actor
        )
        unchanged = absent + duplicates
        logger.info(
            "group_set_mutated",
            extra={
                "set_name": group_set.name,
                "operation": "remove",
                "changed": removed,
                "unchanged": unchanged,
            },
        )
        return GroupSetMutation(group_set.name, removed, unchanged)

    @staticmethod
    def _validation_message(errors: Iterable[TargetError]) -> str:
        details = "; ".join(f"{item.reference} - {item.reason}" for item in errors)
        return f"Group set update aborted. Invalid targets: {details}. No changes were made."
