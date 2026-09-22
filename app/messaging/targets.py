from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.db.repositories import Repository
from app.models import Destination

GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})


@dataclass(slots=True, frozen=True)
class TargetError:
    reference: str
    reason: str


@dataclass(slots=True, frozen=True)
class TargetResolution:
    destinations: tuple[Destination, ...]
    errors: tuple[TargetError, ...]
    duplicates: int


class TargetResolver:
    """Resolve user-facing references to canonical, cached Telegram group identities."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def refresh_cutoff(self) -> str | None:
        return await self.repository.get_state("last_dialog_refresh_started_at")

    @staticmethod
    def is_current(destination: Destination, refresh_cutoff: str | None) -> bool:
        return refresh_cutoff is None or destination.last_seen_at >= refresh_cutoff

    async def resolve_target(
        self, reference: str | int, *, require_allowed: bool = True
    ) -> Destination | TargetError:
        display_reference = str(reference).strip()
        if not display_reference:
            return TargetError(display_reference, "empty target")
        try:
            destination = await self.repository.resolve_destination(reference)
        except (TypeError, ValueError, OverflowError):
            return TargetError(display_reference, "malformed Telegram ID")
        if destination is None:
            return TargetError(display_reference, "not found")
        if not self.is_current(destination, await self.refresh_cutoff()):
            return TargetError(display_reference, "not found in current Telegram groups")
        if destination.chat_type not in GROUP_CHAT_TYPES:
            return TargetError(display_reference, "not a Telegram group")
        if require_allowed and not destination.enabled:
            return TargetError(display_reference, "disabled")
        if require_allowed and not destination.can_send:
            return TargetError(display_reference, "sending unavailable")
        return destination

    async def resolve_targets(
        self, references: Iterable[str | int], *, require_allowed: bool = True
    ) -> TargetResolution:
        destinations: list[Destination] = []
        errors: list[TargetError] = []
        seen: set[int] = set()
        duplicates = 0
        for reference in references:
            resolved = await self.resolve_target(reference, require_allowed=require_allowed)
            if isinstance(resolved, TargetError):
                errors.append(resolved)
                continue
            if resolved.telegram_chat_id in seen:
                duplicates += 1
                continue
            seen.add(resolved.telegram_chat_id)
            destinations.append(resolved)
        return TargetResolution(tuple(destinations), tuple(errors), duplicates)

    async def allowed_groups(self) -> TargetResolution:
        destinations = await self.repository.list_destinations(allowed_only=True)
        refresh_cutoff = await self.refresh_cutoff()
        eligible: list[Destination] = []
        errors: list[TargetError] = []
        seen: set[int] = set()
        duplicates = 0
        for destination in destinations:
            reference = destination.alias or str(destination.telegram_chat_id)
            if not self.is_current(destination, refresh_cutoff):
                errors.append(TargetError(reference, "not found in current Telegram groups"))
                continue
            if destination.chat_type not in GROUP_CHAT_TYPES:
                errors.append(TargetError(reference, "not a Telegram group"))
                continue
            if not destination.can_send:
                errors.append(TargetError(reference, "sending unavailable"))
                continue
            if destination.telegram_chat_id in seen:
                duplicates += 1
                continue
            seen.add(destination.telegram_chat_id)
            eligible.append(destination)
        return TargetResolution(tuple(eligible), tuple(errors), duplicates)
