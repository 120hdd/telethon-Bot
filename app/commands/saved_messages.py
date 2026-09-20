from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from enum import StrEnum

from telethon import TelegramClient, events

from app.db.repositories import NotFoundError, Repository
from app.logging_config import RecentLogHandler
from app.messaging.scheduler import parse_schedule
from app.messaging.service import MessageService
from app.models import JobStatus
from app.services.health import format_health, get_health
from app.telegram.dialogs import allow_group, refresh_dialogs
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
    GROUPS_ALL = "groups_all"
    GROUPS_REFRESH = "groups_refresh"
    GROUP_ADD = "group_add"
    GROUP_REMOVE = "group_remove"
    LOGS = "logs"
    LOGS_STOP = "logs_stop"


@dataclass(slots=True, frozen=True)
class GroupSpec:
    reference: str
    alias: str | None = None


@dataclass(slots=True, frozen=True)
class SavedCommand:
    kind: SavedCommandKind
    argument: str | None = None
    text: str | None = None
    schedule: str | None = None
    groups: tuple[GroupSpec, ...] = ()


def _group_spec(raw: str) -> GroupSpec:
    parts = raw.split(maxsplit=1)
    if not parts:
        raise ValueError("Each group line must contain a link, @username, ID, or cached alias.")
    return GroupSpec(parts[0], parts[1] if len(parts) == 2 else None)


def parse_saved_command(raw_text: str) -> SavedCommand | None:
    lines = raw_text.replace("\r\n", "\n").split("\n")
    header = lines[0].strip()
    if not header.startswith((".", "/")):
        return None
    body = "\n".join(lines[1:]).strip()
    parts = header.split(maxsplit=2)
    name = parts[0].lower().replace(".", "/", 1)
    if name in {"/help", "/status", "/groups", "/group", "/queue"} and len(parts) == 1:
        kind = SavedCommandKind.GROUPS if name == "/group" else SavedCommandKind(name[1:])
        return SavedCommand(kind)
    if name in {"/groups", "/group"} and len(parts) >= 2:
        action = parts[1].lower()
        tail = parts[2] if len(parts) == 3 else ""
        if action == "all" and not tail and not body:
            return SavedCommand(SavedCommandKind.GROUPS_ALL)
        if action == "refresh" and not tail and not body:
            return SavedCommand(SavedCommandKind.GROUPS_REFRESH)
        if action in {"remove", "deny"} and tail and not body:
            return SavedCommand(SavedCommandKind.GROUP_REMOVE, argument=tail)
        if action in {"add", "allow"}:
            if tail and body:
                raise ValueError("Use either one group in the command or a list in the body.")
            raw_groups = (
                [tail] if tail else [line.strip() for line in body.splitlines() if line.strip()]
            )
            if not raw_groups:
                raise ValueError("Add at least one group. Send /help for examples.")
            return SavedCommand(
                SavedCommandKind.GROUP_ADD,
                groups=tuple(_group_spec(item) for item in raw_groups),
            )
    if name == "/send" and len(parts) == 2 and body:
        return SavedCommand(SavedCommandKind.SEND, argument=parts[1], text=body)
    if name == "/schedule" and len(parts) == 3 and body:
        return SavedCommand(
            SavedCommandKind.SCHEDULE,
            argument=parts[1],
            schedule=parts[2],
            text=body,
        )
    if name == "/cancel" and len(parts) == 2:
        return SavedCommand(SavedCommandKind.CANCEL, argument=parts[1])
    if name == "/logs":
        if len(parts) == 1:
            return SavedCommand(SavedCommandKind.LOGS, argument="60")
        if len(parts) == 2 and parts[1].lower() == "stop":
            return SavedCommand(SavedCommandKind.LOGS_STOP)
        if len(parts) == 2 and parts[1].isdigit():
            duration = int(parts[1])
            if 5 <= duration <= 300:
                return SavedCommand(SavedCommandKind.LOGS, argument=str(duration))
            raise ValueError("Log stream duration must be between 5 and 300 seconds.")
    raise ValueError("Invalid Saved Messages command. Send /help for syntax.")


def is_authorized_saved_message(event: object, account_id: int) -> bool:
    message = getattr(event, "message", None)
    return bool(
        getattr(event, "chat_id", None) == account_id
        and getattr(event, "sender_id", None) == account_id
        and getattr(event, "out", False)
        and getattr(message, "fwd_from", None) is None
    )


HELP_TEXT = """راهنمای کنترل SajadBot از Saved Messages

این برنامه با اکانت شخصی شما کار می‌کند، نه Bot Token.
فرمان‌ها را فقط از Saved Messages خودتان می‌پذیرد. شکل /command پیشنهاد می‌شود؛
فرمان‌های قدیمی با نقطه هم هنوز کار می‌کنند.

وضعیت و صف:
/status — سلامت اتصال و وضعیت صف
/queue — حداکثر ۲۰ کار فعال
/cancel <job-id> — لغو کار صف‌شده

گروه‌ها:
/groups — گروه‌های مجاز
/groups all — تمام گروه‌های کشف‌شده، مجاز و غیرفعال
/groups refresh — اسکن دوباره گروه‌هایی که اکانت عضو آن‌هاست
/groups add <link|@username|chat-id|alias> [alias]
/groups remove <chat-id|alias>

افزودن چند گروه در یک پیام:
/groups add
@public_group work
https://t.me/another_group sales
-1001234567890 private_team

لینک عمومی، @username یا chat ID پذیرفته می‌شود.
برنامه با لینک دعوت خصوصی عضو گروه نمی‌شود؛ اول با خود اکانت وارد گروه شوید،
سپس /groups refresh و بعد /groups add را بزنید.
alias اختیاری است و فقط حرف، عدد و _ می‌پذیرد.

ارسال:
/send <alias-or-id>
متن پیام در خط بعد

زمان‌بندی:
/schedule <alias-or-id> <ISO datetime>
متن پیام در خط بعد
مثال: /schedule work 2026-09-20 14:30
زمان بدون offset با LOCAL_TIMEZONE تنظیم‌شده تفسیر می‌شود. زمان offsetدار هم پذیرفته است.
ارسال تکراری معادل، برای جلوگیری از دوباره‌فرستی، دوباره وارد صف نمی‌شود.

لاگ زنده:
/logs [seconds] — نمایش ۵ تا ۳۰۰ ثانیه (پیش‌فرض ۶۰) با ادیت همان پیام
/logs stop — توقف استریم فعال
لاگ نمایشی محدود و redacted است و متن پیام‌ها یا اطلاعات ورود را نشان نمی‌دهد.

نکته: فقط یک worker ارسال می‌کند، گروه باید whitelist و قابل ارسال باشد،
و محدودیت‌های FloodWait/Slow Mode دور زده نمی‌شوند.
چون این یک کلاینت اکانت شخصی است، منوی BotFather ندارد؛ /help و بقیه فرمان‌ها
با eventهای Saved Messages اجرا می‌شوند."""


class SavedMessagesController:
    def __init__(
        self,
        client: TelegramClient,
        sender: TelegramSender,
        repository: Repository,
        message_service: MessageService,
        account_id: int,
        recent_logs: RecentLogHandler | None = None,
    ) -> None:
        self.client = client
        self.sender = sender
        self.repository = repository
        self.message_service = message_service
        self.account_id = account_id
        self.recent_logs = recent_logs
        self._log_stream_task: asyncio.Task[None] | None = None

    def register(self) -> None:
        self.client.add_event_handler(self.handle, events.NewMessage(outgoing=True))
        logger.info("saved_messages_control_registered")

    def unregister(self) -> None:
        self.client.remove_event_handler(self.handle)
        if self._log_stream_task is not None:
            self._log_stream_task.cancel()

    async def handle(self, event: object) -> None:
        if not is_authorized_saved_message(event, self.account_id):
            return
        raw_text = getattr(event, "raw_text", "")
        try:
            command = parse_saved_command(raw_text)
            if command is None:
                return
            if command.kind == SavedCommandKind.LOGS:
                assert command.argument is not None
                await self._start_log_stream(event, int(command.argument))
                return
            if command.kind == SavedCommandKind.LOGS_STOP:
                response = await self._stop_log_stream()
            else:
                response = await self.execute(command)
        except (ValueError, NotFoundError) as exc:
            response = f"failed\nreason: {exc}"
        except ClassifiedTelegramError as exc:
            logger.warning(
                "saved_messages_command_failed", extra={"exception_type": exc.error_type}
            )
            response = f"failed\nreason: {exc.safe_message}"
        except Exception:
            logger.exception("saved_messages_command_unexpected_failure")
            response = "failed\nreason: internal command error; inspect /logs"
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
        if command.kind == SavedCommandKind.GROUPS_ALL:
            destinations = await self.repository.list_destinations()
            if not destinations:
                return "No discovered groups. Run /groups refresh."
            return "\n".join(
                f"{'allowed' if item.enabled else 'disabled'} | {item.alias or '-'} | "
                f"{item.title} | {item.telegram_chat_id}"
                for item in destinations
            )
        if command.kind == SavedCommandKind.GROUPS_REFRESH:
            count = await refresh_dialogs(self.client, self.repository)
            return f"refreshed\ngroups found: {count}\nnew groups remain disabled"
        if command.kind == SavedCommandKind.GROUP_ADD:
            results: list[str] = []
            for item in command.groups:
                try:
                    destination = await allow_group(
                        self.client, self.repository, item.reference, item.alias
                    )
                except (ValueError, NotFoundError) as exc:
                    results.append(f"failed | {item.reference} | {exc}")
                else:
                    results.append(
                        f"allowed | {destination.alias or '-'} | {destination.title} | "
                        f"{destination.telegram_chat_id}"
                    )
            return "\n".join(results)
        if command.kind == SavedCommandKind.GROUP_REMOVE:
            assert command.argument is not None
            destination = await self.repository.set_destination_enabled(command.argument, False)
            return f"disabled\n{destination.title} | {destination.telegram_chat_id}"
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

    async def _start_log_stream(self, event: object, duration: int) -> None:
        if self.recent_logs is None:
            await self.sender.send_control_reply("Log stream is unavailable.")
            return
        message_id = getattr(event, "id", None)
        if message_id is None:
            message_id = getattr(getattr(event, "message", None), "id", None)
        if message_id is None:
            await self.sender.send_control_reply("Could not edit the command message.")
            return
        if self._log_stream_task is not None and not self._log_stream_task.done():
            self._log_stream_task.cancel()
            await asyncio.gather(self._log_stream_task, return_exceptions=True)
        self._log_stream_task = asyncio.create_task(
            self._stream_logs(int(message_id), duration), name="saved-messages-log-stream"
        )

    async def _stop_log_stream(self) -> str:
        if self._log_stream_task is None or self._log_stream_task.done():
            return "No active log stream."
        self._log_stream_task.cancel()
        await asyncio.gather(self._log_stream_task, return_exceptions=True)
        self._log_stream_task = None
        return "Log stream stopped."

    async def _stream_logs(self, message_id: int, duration: int) -> None:
        assert self.recent_logs is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration
        try:
            while True:
                remaining = max(0, math.ceil(deadline - loop.time()))
                state = "live" if remaining > 0 else "finished"
                content = self.recent_logs.snapshot() or "No log records yet."
                text = f"Logs ({state}, {remaining}s)\n\n{content}"
                await self.sender.edit_control_message(message_id, text)
                if remaining <= 0:
                    return
                await asyncio.sleep(min(5, remaining))
        except ClassifiedTelegramError as exc:
            logger.warning("log_stream_edit_failed", extra={"exception_type": exc.error_type})
        finally:
            if self._log_stream_task is asyncio.current_task():
                self._log_stream_task = None
