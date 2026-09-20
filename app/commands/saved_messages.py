from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from telethon import TelegramClient, events

from app.db.repositories import NotFoundError, Repository
from app.messaging.scheduler import parse_schedule
from app.messaging.service import MessageService
from app.models import JobStatus
from app.services.health import format_health, get_health
from app.telegram.errors import ClassifiedTelegramError
from app.telegram.sender import TelegramSender

logger = logging.getLogger(__name__)


class SavedCommandKind(StrEnum):
    HELP = "help"
    STATUS = "status"
    GROUPS = "groups"
    SEND = "send"
    SCHEDULE = "schedule"
    QUEUE = "queue"
    CANCEL = "cancel"


@dataclass(slots=True, frozen=True)
class SavedCommand:
    kind: SavedCommandKind
    argument: str | None = None
    text: str | None = None
    schedule: str | None = None


def parse_saved_command(raw_text: str) -> SavedCommand | None:
    lines = raw_text.replace("\r\n", "\n").split("\n")
    header = lines[0].strip()
    if not header.startswith("."):
        return None
    body = "\n".join(lines[1:]).strip()
    parts = header.split(maxsplit=2)
    name = parts[0].lower()
    if name in {".help", ".status", ".groups", ".queue"} and len(parts) == 1:
        return SavedCommand(SavedCommandKind(name[1:]))
    if name == ".send" and len(parts) == 2 and body:
        return SavedCommand(SavedCommandKind.SEND, argument=parts[1], text=body)
    if name == ".schedule" and len(parts) == 3 and body:
        return SavedCommand(
            SavedCommandKind.SCHEDULE,
            argument=parts[1],
            schedule=parts[2],
            text=body,
        )
    if name == ".cancel" and len(parts) == 2:
        return SavedCommand(SavedCommandKind.CANCEL, argument=parts[1])
    raise ValueError("Invalid Saved Messages command. Send .help for syntax.")


def is_authorized_saved_message(event: object, account_id: int) -> bool:
    message = getattr(event, "message", None)
    return bool(
        getattr(event, "chat_id", None) == account_id
        and getattr(event, "sender_id", None) == account_id
        and getattr(event, "out", False)
        and getattr(message, "fwd_from", None) is None
    )


HELP_TEXT = """Commands:
.status
.groups
.send <alias>
message text
.schedule <alias> <ISO datetime>
message text
.queue
.cancel <job-id>"""


class SavedMessagesController:
    def __init__(
        self,
        client: TelegramClient,
        sender: TelegramSender,
        repository: Repository,
        message_service: MessageService,
        account_id: int,
    ) -> None:
        self.client = client
        self.sender = sender
        self.repository = repository
        self.message_service = message_service
        self.account_id = account_id

    def register(self) -> None:
        self.client.add_event_handler(self.handle, events.NewMessage(outgoing=True))
        logger.info("saved_messages_control_registered")

    def unregister(self) -> None:
        self.client.remove_event_handler(self.handle)

    async def handle(self, event: object) -> None:
        if not is_authorized_saved_message(event, self.account_id):
            return
        raw_text = getattr(event, "raw_text", "")
        try:
            command = parse_saved_command(raw_text)
            if command is None:
                return
            response = await self.execute(command)
        except (ValueError, NotFoundError) as exc:
            response = f"failed\nreason: {exc}"
        except ClassifiedTelegramError:
            logger.exception("saved_messages_command_failed")
            return
        try:
            await self.sender.send_control_reply(response)
        except ClassifiedTelegramError as exc:
            logger.warning("control_reply_failed", extra={"exception_type": exc.error_type})

    async def execute(self, command: SavedCommand) -> str:
        if command.kind == SavedCommandKind.HELP:
            return HELP_TEXT
        if command.kind == SavedCommandKind.STATUS:
            return format_health(await get_health(self.repository))
        if command.kind == SavedCommandKind.GROUPS:
            destinations = await self.repository.list_destinations(allowed_only=True)
            if not destinations:
                return "No whitelisted groups."
            return "\n".join(
                f"{item.alias or '-'} | {item.title} | {item.telegram_chat_id}"
                for item in destinations
            )
        if command.kind == SavedCommandKind.QUEUE:
            jobs = await self.repository.list_jobs(
                {
                    JobStatus.PENDING,
                    JobStatus.SCHEDULED,
                    JobStatus.PROCESSING,
                    JobStatus.RETRY,
                    JobStatus.WAITING_RATE_LIMIT,
                    JobStatus.REVIEW_REQUIRED,
                },
                limit=20,
            )
            if not jobs:
                return "Queue is empty."
            return "\n".join(f"{job.uuid} | {job.status}" for job in jobs)
        if command.kind == SavedCommandKind.CANCEL:
            assert command.argument is not None
            job = await self.repository.cancel_job(command.argument, actor="saved_messages")
            return f"cancelled\njob: {job.uuid}"
        if command.kind in {SavedCommandKind.SEND, SavedCommandKind.SCHEDULE}:
            assert command.argument is not None and command.text is not None
            scheduled_at = None
            if command.kind == SavedCommandKind.SCHEDULE:
                assert command.schedule is not None
                scheduled_at = parse_schedule(
                    command.schedule, self.message_service.settings.timezone
                )
            result = await self.message_service.queue_message(
                command.argument,
                text=command.text,
                scheduled_at=scheduled_at,
                actor="saved_messages",
            )
            label = (
                "already queued"
                if result.duplicate
                else "dry run"
                if result.job.status == JobStatus.DRY_RUN
                else "queued"
            )
            destination = await self.repository.resolve_destination(result.job.destination_chat_id)
            return (
                f"{label}\njob: {result.job.uuid}\n"
                f"destination: {destination.alias or destination.title}"
            )
        raise AssertionError(f"Unhandled command: {command.kind}")
