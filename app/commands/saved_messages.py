from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from enum import StrEnum

from telethon import TelegramClient, events

from app.db.repositories import NotFoundError, Repository
from app.logging_config import RecentLogHandler
from app.messaging.bulk import BulkEnqueueResult, BulkMessageService
from app.messaging.group_sets import GroupSetService
from app.messaging.scheduler import parse_schedule
from app.messaging.service import MessageService
from app.messaging.targets import TargetResolver
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
    SEND_ALL = "sendall"
    SEND_MULTI = "sendmulti"
    GROUP_SET_CREATE = "groupset_create"
    GROUP_SET_ADD = "groupset_add"
    GROUP_SET_REMOVE = "groupset_remove"
    GROUP_SET_LIST = "groupset_list"
    GROUP_SET_SHOW = "groupset_show"
    GROUP_SET_DELETE = "groupset_delete"
    SEND_SET = "sendset"
    BATCH = "batch"


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
    targets: tuple[str, ...] = ()


def _group_spec(raw: str) -> GroupSpec:
    parts = raw.split(maxsplit=1)
    if not parts:
        raise ValueError("Each group line must contain a link, @username, ID, or cached alias.")
    return GroupSpec(parts[0], parts[1] if len(parts) == 2 else None)


def _message_from_tail(inline: str, body: str) -> str:
    message = "\n".join(part for part in (inline.strip(), body) if part).strip()
    if not message:
        raise ValueError("Message text must not be empty.")
    return message


def _split_multi_send(tail: str, body: str) -> tuple[tuple[str, ...], str]:
    match = re.match(r"^\s*([^\s,]+(?:\s*,\s*[^\s,]+)*)\s+(.+)$", tail, re.DOTALL)
    if match is None:
        if not body:
            raise ValueError("Use /sendmulti GROUP1,GROUP2 MESSAGE.")
        target_expression = tail
        message = body
    else:
        target_expression = match.group(1)
        message = _message_from_tail(match.group(2), body)
    targets = tuple(item.strip() for item in target_expression.split(",") if item.strip())
    if not targets:
        raise ValueError("Add at least one target.")
    return targets, message.strip()


def _split_target_list(raw: str) -> tuple[str, ...]:
    return tuple(item for item in re.split(r"[\s,]+", raw.strip()) if item)


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
    if name == "/sendall":
        inline = header[len(parts[0]) :]
        return SavedCommand(SavedCommandKind.SEND_ALL, text=_message_from_tail(inline, body))
    if name == "/sendmulti":
        tail = header[len(parts[0]) :].strip()
        targets, message = _split_multi_send(tail, body)
        return SavedCommand(SavedCommandKind.SEND_MULTI, text=message, targets=targets)
    if name == "/sendset":
        command_parts = header.split(maxsplit=2)
        if len(command_parts) < 2:
            raise ValueError("Use /sendset NAME MESSAGE.")
        inline_message = command_parts[2] if len(command_parts) == 3 else ""
        return SavedCommand(
            SavedCommandKind.SEND_SET,
            argument=command_parts[1],
            text=_message_from_tail(inline_message, body),
        )
    if name == "/groupset":
        command_parts = header.split(maxsplit=3)
        if len(command_parts) < 2:
            raise ValueError("Use /groupset create|add|remove|list|show|delete.")
        action = command_parts[1].lower()
        if action == "list" and len(command_parts) == 2 and not body:
            return SavedCommand(SavedCommandKind.GROUP_SET_LIST)
        if action in {"create", "show", "delete"} and len(command_parts) == 3 and not body:
            kind = {
                "create": SavedCommandKind.GROUP_SET_CREATE,
                "show": SavedCommandKind.GROUP_SET_SHOW,
                "delete": SavedCommandKind.GROUP_SET_DELETE,
            }[action]
            return SavedCommand(kind, argument=command_parts[2])
        if action in {"add", "remove"} and len(command_parts) >= 4 and not body:
            targets = _split_target_list(command_parts[3])
            if not targets:
                raise ValueError("Add at least one target.")
            kind = (
                SavedCommandKind.GROUP_SET_ADD
                if action == "add"
                else SavedCommandKind.GROUP_SET_REMOVE
            )
            return SavedCommand(kind, argument=command_parts[2], targets=targets)
        raise ValueError("Invalid /groupset syntax. Send /help for examples.")
    if name == "/batch" and len(parts) == 2 and not body:
        return SavedCommand(SavedCommandKind.BATCH, argument=parts[1])
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


HELP_TEXT += """

Bulk and Group Sets:
/sendmulti GROUP1,GROUP2 MESSAGE - queue one job per selected allowed group
/sendall MESSAGE - queue one job for every currently allowed group
/groupset create NAME - create a persistent set
/groupset add NAME GROUP... - add cached groups (spaces or commas)
/groupset remove NAME GROUP... - remove groups
/groupset list - list saved sets
/groupset show NAME - show members and current eligibility
/groupset delete NAME - delete a set
/sendset NAME MESSAGE - queue all currently eligible members
/batch BATCH_ID - show delivery status for a bulk batch

Dot-prefixed forms such as .sendall remain supported. Bulk delivery uses the normal
durable queue, sequential worker, rate limiter, retries, and dry-run behavior.
"""


class SavedMessagesController:
    def __init__(
        self,
        client: TelegramClient,
        sender: TelegramSender,
        repository: Repository,
        message_service: MessageService,
        account_id: int,
        recent_logs: RecentLogHandler | None = None,
        target_resolver: TargetResolver | None = None,
        bulk_service: BulkMessageService | None = None,
        group_set_service: GroupSetService | None = None,
    ) -> None:
        self.client = client
        self.sender = sender
        self.repository = repository
        self.message_service = message_service
        self.account_id = account_id
        self.recent_logs = recent_logs
        self.target_resolver = target_resolver or TargetResolver(repository)
        self.bulk_service = bulk_service or BulkMessageService(repository, message_service)
        self.group_set_service = group_set_service or GroupSetService(
            repository, self.target_resolver
        )
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
        if command.kind == SavedCommandKind.GROUP_SET_CREATE:
            assert command.argument is not None
            try:
                group_set = await self.group_set_service.create(command.argument)
            except ValueError as exc:
                if "already exists" in str(exc):
                    normalized = self.group_set_service.normalize_name(command.argument)
                    return f"❌ Group set already exists: {normalized}"
                raise
            return f"✅ Group set created: {group_set.name}"
        if command.kind == SavedCommandKind.GROUP_SET_ADD:
            assert command.argument is not None
            result = await self.group_set_service.add(command.argument, command.targets)
            return (
                f"✅ Group set updated: {result.name}\n\n"
                f"Added: {result.changed}\nAlready present: {result.unchanged}"
            )
        if command.kind == SavedCommandKind.GROUP_SET_REMOVE:
            assert command.argument is not None
            result = await self.group_set_service.remove(command.argument, command.targets)
            return (
                f"✅ Group set updated: {result.name}\n\n"
                f"Removed: {result.changed}\nNot present: {result.unchanged}"
            )
        if command.kind == SavedCommandKind.GROUP_SET_LIST:
            group_sets = await self.group_set_service.list()
            if not group_sets:
                return "No group sets found."
            return "📁 Group Sets\n\n" + "\n".join(
                f"{group_set.name} — {count} groups" for group_set, count in group_sets
            )
        if command.kind == SavedCommandKind.GROUP_SET_SHOW:
            assert command.argument is not None
            snapshot = await self.group_set_service.snapshot(command.argument)
            lines: list[str] = []
            for view in snapshot.members:
                member = view.member
                destination = member.destination
                if view.status == "missing":
                    lines.append(f"⚠️ {member.destination_peer_id} — destination missing")
                elif view.status == "allowed" and destination is not None:
                    lines.append(
                        f"✅ {destination.alias or destination.telegram_chat_id} — "
                        f"{destination.title}"
                    )
                elif destination is not None:
                    lines.append(
                        f"⛔ {destination.alias or destination.telegram_chat_id} — "
                        f"{destination.title}"
                    )
            heading = f"📁 {snapshot.group_set.name}\n\n{len(snapshot.members)} groups"
            details = "\n".join(lines)
            summary = (
                f"Allowed: {snapshot.allowed}\nDisabled: {snapshot.disabled}\n"
                f"Missing: {snapshot.missing}"
            )
            return "\n\n".join(part for part in (heading, details, summary) if part)
        if command.kind == SavedCommandKind.GROUP_SET_DELETE:
            assert command.argument is not None
            normalized = self.group_set_service.normalize_name(command.argument)
            try:
                await self.group_set_service.delete(normalized)
            except NotFoundError:
                return f"❌ Group set not found: {normalized}"
            return f"✅ Group set deleted: {normalized}"
        if command.kind == SavedCommandKind.SEND_ALL:
            assert command.text is not None
            resolution = await self.target_resolver.allowed_groups()
            if not resolution.destinations:
                return "⚠️ No allowed groups found.\nNothing was queued."
            result = await self.bulk_service.enqueue_bulk(
                resolution.destinations,
                text=command.text,
                batch_type="sendall",
                requested=len(resolution.destinations)
                + resolution.duplicates
                + len(resolution.errors),
                duplicates=resolution.duplicates,
                skipped=len(resolution.errors),
            )
            return self._bulk_summary("✅ Send-all queued", result, targets=True)
        if command.kind == SavedCommandKind.SEND_MULTI:
            assert command.text is not None
            resolution = await self.target_resolver.resolve_targets(command.targets)
            if resolution.errors:
                invalid = "\n".join(
                    f"- {item.reference} — {item.reason}" for item in resolution.errors
                )
                logger.info(
                    "bulk_validation_failed",
                    extra={
                        "command_type": "sendmulti",
                        "requested": len(command.targets),
                        "failure_count": len(resolution.errors),
                    },
                )
                return (
                    f"❌ Multi-send aborted.\n\nInvalid targets:\n{invalid}\n\n"
                    "No messages were queued."
                )
            result = await self.bulk_service.enqueue_bulk(
                resolution.destinations,
                text=command.text,
                batch_type="sendmulti",
                requested=len(command.targets),
                duplicates=resolution.duplicates,
            )
            return self._bulk_summary("✅ Multi-send queued", result, targets=True)
        if command.kind == SavedCommandKind.SEND_SET:
            assert command.argument is not None and command.text is not None
            snapshot = await self.group_set_service.snapshot(command.argument)
            if not snapshot.eligible:
                return (
                    f"⚠️ No eligible groups in set: {snapshot.group_set.name}.\nNothing was queued."
                )
            result = await self.bulk_service.enqueue_bulk(
                snapshot.eligible,
                text=command.text,
                batch_type="sendset",
                requested=len(snapshot.members),
                duplicates=snapshot.duplicates,
                skipped=snapshot.disabled + snapshot.missing,
            )
            return (
                f"✅ Group set queued: {snapshot.group_set.name}\n\n"
                f"Members: {len(snapshot.members)}\nEligible: {result.eligible}\n"
                f"Queued: {result.queued}\nDisabled: {snapshot.disabled}\n"
                f"Missing: {snapshot.missing}\n"
                f"Duplicates: {result.duplicates}\nFailed: {result.failed}\n"
                f"Batch: {result.batch_id}"
            )
        if command.kind == SavedCommandKind.BATCH:
            assert command.argument is not None
            batch, counts = await self.bulk_service.batch_status(command.argument)
            pending_statuses = {
                JobStatus.PENDING,
                JobStatus.SCHEDULED,
                JobStatus.PROCESSING,
                JobStatus.RETRY,
                JobStatus.WAITING_RATE_LIMIT,
                JobStatus.REVIEW_REQUIRED,
            }
            pending = sum(counts.get(status.value, 0) for status in pending_statuses)
            return (
                f"Batch: {batch.id}\n\nTargets: {batch.requested_count}\n"
                f"Queued: {sum(counts.values())}\nPending: {pending}\n"
                f"Sent: {counts.get(JobStatus.SENT.value, 0)}\n"
                f"Failed: {counts.get(JobStatus.FAILED.value, 0)}\n"
                f"Cancelled: {counts.get(JobStatus.CANCELLED.value, 0)}\n"
                f"Dry run: {counts.get(JobStatus.DRY_RUN.value, 0)}"
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

    @staticmethod
    def _bulk_summary(heading: str, result: BulkEnqueueResult, *, targets: bool = False) -> str:
        first_count = f"Targets: {result.requested}\n" if targets else ""
        return (
            f"{heading}\n\n{first_count}Queued: {result.queued}\n"
            f"Duplicates: {result.duplicates}\nSkipped: {result.skipped}\n"
            f"Failed: {result.failed}\nBatch: {result.batch_id}"
        )

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
