from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer

from app.config import Settings, get_settings
from app.db.database import Database, DatabaseLockedError
from app.db.repositories import NotFoundError, Repository
from app.main import run_application
from app.messaging.bulk import BulkEnqueueResult, BulkMessageService
from app.messaging.group_sets import GroupSetService
from app.messaging.scheduler import parse_schedule
from app.messaging.service import MessageService
from app.messaging.targets import TargetResolver
from app.models import JobStatus
from app.services.health import format_health, get_health
from app.telegram.client import (
    AuthenticationRequiredError,
    SessionInUseError,
    TelegramConnection,
    account_summary,
    create_client,
    protect_session_storage,
    session_exists,
    session_file_path,
)
from app.telegram.dialogs import refresh_dialogs
from app.telegram.errors import TelegramAuthorizationError, authorization_error_message
from app.utils.locks import AlreadyRunningError, ProcessLock

app = typer.Typer(help="Conservative Telegram personal-account messaging client.")
groups_app = typer.Typer(help="Discover and manage allowed Telegram groups.")
queue_app = typer.Typer(help="Inspect and manage persistent outgoing jobs.")
auth_app = typer.Typer(help="Authenticate and inspect the Telegram user session.")
group_sets_app = typer.Typer(help="Manage persistent named sets of Telegram groups.")
app.add_typer(groups_app, name="groups")
app.add_typer(queue_app, name="queue")
app.add_typer(auth_app, name="auth")
app.add_typer(group_sets_app, name="groupset")


def _run[T](operation: Awaitable[T]) -> T:
    try:
        return asyncio.run(operation)
    except (
        ValueError,
        NotFoundError,
        DuplicateError,
        AlreadyRunningError,
        SessionInUseError,
        AuthenticationRequiredError,
        TelegramAuthorizationError,
        DatabaseLockedError,
    ) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


class DuplicateError(ValueError):
    pass


def _get_settings_or_exit() -> Settings:
    try:
        return get_settings()
    except ValueError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def _safe_preview(text: str | None, *, limit: int = 160) -> str:
    if not text:
        return "[media only]"
    preview = " ".join(text.split())
    return preview if len(preview) <= limit else f"{preview[: limit - 1]}…"


def _split_cli_targets(values: list[str]) -> tuple[str, ...]:
    targets = tuple(
        target.strip() for value in values for target in value.split(",") if target.strip()
    )
    if not targets:
        raise ValueError("At least one group target is required.")
    return targets


def _bulk_output(heading: str, result: BulkEnqueueResult, *, dry_run: bool) -> str:
    prefix = "[DRY-RUN] " if dry_run else ""
    return (
        f"{prefix}{heading}\n"
        f"Targets: {result.requested}\n"
        f"Eligible: {result.eligible}\n"
        f"Queued: {result.queued}\n"
        f"Duplicates: {result.duplicates}\n"
        f"Skipped: {result.skipped}\n"
        f"Failed: {result.failed}\n"
        f"Batch: {result.batch_id}"
    )


async def _with_repository[T](
    operation: Callable[[Repository, Settings], Awaitable[T]],
) -> T:
    settings = _get_settings_or_exit()
    settings.ensure_directories()
    database = Database(settings.database_path)
    await database.connect()
    try:
        repository = Repository(database)
        await repository.initialize()
        return await operation(repository, settings)
    finally:
        await database.close()


@app.command()
def start(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Resolve metadata without any Telegram mutation")
    ] = False,
) -> None:
    """Start the Telegram connection, queue worker, and Saved Messages controller."""
    settings = _get_settings_or_exit()
    if dry_run:
        settings = settings.model_copy(update={"dry_run": True})
    _run(run_application(settings))


@auth_app.command("login")
def auth_login(
    qr: Annotated[bool, typer.Option("--qr", help="Log in by scanning Telegram's QR URL")] = False,
) -> None:
    """Explicitly authorize the configured Telegram user account."""

    async def operation(repository: Repository, settings: Settings) -> dict[str, str | int]:
        credentials = settings.resolve_api_credentials()
        session_path = settings.session_path_for(credentials.profile)
        lock = ProcessLock(session_path.with_suffix(".lock"))
        lock.acquire()
        connection: TelegramConnection | None = None
        try:
            connection = TelegramConnection(
                create_client(settings), settings, repository, notice=typer.echo
            )
            await connection.connect_and_authorize(interactive=True, qr=qr)
            me = await connection.client.get_me()
            if me is None:
                raise AuthenticationRequiredError("Telegram did not return an authorized user.")
            return account_summary(me, settings.telegram_phone)
        finally:
            if connection is not None:
                await connection.disconnect()
            lock.release()

    summary = _run(_with_repository(operation))
    typer.echo("Telegram authorization successful")
    typer.echo(f"User ID: {summary['user_id']}")
    typer.echo(f"Username: {summary['username']}")
    typer.echo(f"Phone: {summary['phone']}")


async def _telegram_status(settings: Settings) -> tuple[list[str], bool, bool]:
    credentials = settings.resolve_api_credentials()
    session_path = settings.session_path_for(credentials.profile)
    existed = session_exists(session_path)
    lines = [
        f"API profile: {credentials.profile}",
        f"API ID: {credentials.api_id}",
        "API hash: configured (hidden)",
        f"Session path: {session_file_path(session_path)}",
        f"Session exists: {'yes' if existed else 'no'}",
    ]
    client = create_client(settings)
    connected = False
    authorized = False
    try:
        await client.connect()
        connected = True
        lines.append("Telegram connection: ok")
        authorized = bool(await client.is_user_authorized())
        lines.append(f"Authorized: {'yes' if authorized else 'no'}")
        if authorized:
            me = await client.get_me()
            if me is not None:
                summary = account_summary(me, settings.telegram_phone)
                lines.extend(
                    [
                        f"User ID: {summary['user_id']}",
                        f"Username: {summary['username']}",
                        f"Phone: {summary['phone']}",
                    ]
                )
    except Exception as exc:
        safe_error = authorization_error_message(exc) or type(exc).__name__
        lines.append(f"Telegram connection: error ({safe_error})")
        lines.append("Authorized: unknown")
    finally:
        if client.is_connected():
            await client.disconnect()
        protect_session_storage(session_path)
    return lines, connected, authorized


@auth_app.command("status")
def auth_status() -> None:
    """Inspect the session without requesting a login code."""
    settings = _get_settings_or_exit()
    settings.ensure_directories()
    lines, _, _ = _run(_telegram_status(settings))
    typer.echo("\n".join(lines))


@auth_app.command("reset")
def auth_reset(
    yes: Annotated[bool, typer.Option("--yes", help="Confirm deletion without a prompt")] = False,
) -> None:
    """Delete only the selected profile's session after explicit confirmation."""
    settings = _get_settings_or_exit()
    settings.ensure_directories()
    credentials = settings.resolve_api_credentials()
    session_path = settings.session_path_for(credentials.profile)
    session_file = session_file_path(session_path)
    if not yes and not typer.confirm(
        f"Delete Telegram session for profile '{credentials.profile}' at {session_file}?"
    ):
        raise typer.Abort()
    lock = ProcessLock(session_path.with_suffix(".lock"))
    lock.acquire()
    try:
        removed = False
        for path in (
            session_file,
            Path(f"{session_file}-journal"),
            Path(f"{session_file}-shm"),
            Path(f"{session_file}-wal"),
        ):
            if path.exists():
                path.unlink()
                removed = True
    finally:
        lock.release()
    typer.echo("Telegram session removed." if removed else "No Telegram session file existed.")


@app.command("doctor")
def doctor() -> None:
    """Check configuration, storage, Telethon, network, and authorization without mutations."""
    settings = _get_settings_or_exit()
    checks = [
        f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        f"Python >= 3.12: {'ok' if sys.version_info >= (3, 12) else 'error'}",
        f"Telethon: {version('telethon')}",
    ]
    try:
        settings.ensure_directories()
        credentials = settings.resolve_api_credentials()
        settings.require_phone()
        checks.append(f"Configuration: ok (API profile: {credentials.profile})")
        writable = os.access(settings.session_directory, os.W_OK)
        checks.append(f"Session directory writable: {'yes' if writable else 'no'}")
        status_lines, connected, authorized = _run(_telegram_status(settings))
        checks.extend(status_lines)
        doctor_result = "ok" if connected and authorized and writable else "attention required"
        checks.append(f"Doctor result: {doctor_result}")
    except (ValueError, TelegramAuthorizationError) as exc:
        checks.append(f"Configuration: error ({exc})")
        checks.append("Doctor result: attention required")
    typer.echo("\n".join(checks))


@app.command()
def status() -> None:
    """Show database, last-known Telegram, and queue health."""

    async def operation(repository: Repository, _: Settings) -> str:
        return format_health(await get_health(repository))

    typer.echo(_run(_with_repository(operation)))


@groups_app.command("list")
def groups_list() -> None:
    """List cached groups. Use refresh to query Telegram."""

    async def operation(repository: Repository, _: Settings) -> list[object]:
        return await repository.list_destinations()

    destinations = _run(_with_repository(operation))
    typer.echo(f"{'ID':<22} {'TYPE':<12} {'ALLOWED':<8} {'SEND':<6} TITLE")
    for item in destinations:
        typer.echo(
            f"{item.telegram_chat_id:<22} {item.chat_type:<12} "
            f"{str(item.enabled):<8} {str(item.can_send):<6} {item.title}"
        )


@groups_app.command("refresh")
def groups_refresh() -> None:
    """Connect to Telegram and refresh the cached group list."""

    async def operation(repository: Repository, settings: Settings) -> int:
        lock = ProcessLock(settings.tg_session_path.with_suffix(".lock"))
        lock.acquire()
        connection: TelegramConnection | None = None
        try:
            connection = TelegramConnection(create_client(settings), settings, repository)
            await connection.connect_and_authorize(interactive=False)
            return await refresh_dialogs(connection.client, repository)
        finally:
            if connection is not None:
                await connection.disconnect()
            lock.release()

    count = _run(_with_repository(operation))
    typer.echo(f"Refreshed {count} groups. Newly discovered groups remain disabled.")


@groups_app.command("allowed")
def groups_allowed() -> None:
    """List whitelisted groups."""

    async def operation(repository: Repository, _: Settings) -> list[object]:
        return await repository.list_destinations(allowed_only=True)

    destinations = _run(_with_repository(operation))
    if not destinations:
        typer.echo("No groups are whitelisted.")
        return
    for item in destinations:
        typer.echo(f"{item.telegram_chat_id}  {item.alias or '-':<20} {item.title}")


@groups_app.command("allow", context_settings={"ignore_unknown_options": True})
def groups_allow(group: str) -> None:
    """Whitelist a cached group ID or alias."""

    async def operation(repository: Repository, _: Settings) -> object:
        return await repository.set_destination_enabled(group, True)

    destination = _run(_with_repository(operation))
    typer.echo(f"Allowed: {destination.title} ({destination.telegram_chat_id})")


@groups_app.command("deny", context_settings={"ignore_unknown_options": True})
def groups_deny(group: str) -> None:
    """Disable a destination and cancel all unsent jobs for it."""

    async def operation(repository: Repository, _: Settings) -> object:
        return await repository.set_destination_enabled(group, False)

    destination = _run(_with_repository(operation))
    typer.echo(f"Denied: {destination.title} ({destination.telegram_chat_id})")


@groups_app.command("alias", context_settings={"ignore_unknown_options": True})
def groups_alias(group: str, alias: str) -> None:
    """Assign a local alias to a cached group."""

    async def operation(repository: Repository, _: Settings) -> object:
        return await repository.set_destination_alias(group, alias)

    destination = _run(_with_repository(operation))
    typer.echo(f"Alias {destination.alias} -> {destination.telegram_chat_id}")


@group_sets_app.command("create")
def group_set_create(name: str) -> None:
    """Create a persistent, case-insensitively named group set."""

    async def operation(repository: Repository, _: Settings) -> object:
        resolver = TargetResolver(repository)
        return await GroupSetService(repository, resolver).create(name, actor="cli")

    group_set = _run(_with_repository(operation))
    typer.echo(f"Group set created: {group_set.name}")


@group_sets_app.command("add")
def group_set_add(
    name: str,
    groups: Annotated[
        list[str],
        typer.Option(
            "--group",
            "-g",
            help="Cached alias or peer ID; repeat the option or separate values with commas.",
        ),
    ],
) -> None:
    """Atomically add one or more cached Telegram groups to a set."""
    targets = _split_cli_targets(groups)

    async def operation(repository: Repository, _: Settings) -> object:
        resolver = TargetResolver(repository)
        return await GroupSetService(repository, resolver).add(name, targets, actor="cli")

    result = _run(_with_repository(operation))
    typer.echo(
        f"Group set updated: {result.name}\n"
        f"Added: {result.changed}\nAlready present: {result.unchanged}"
    )


@group_sets_app.command("remove")
def group_set_remove(
    name: str,
    groups: Annotated[
        list[str],
        typer.Option(
            "--group",
            "-g",
            help="Cached alias or peer ID; repeat the option or separate values with commas.",
        ),
    ],
) -> None:
    """Atomically remove one or more groups from a set."""
    targets = _split_cli_targets(groups)

    async def operation(repository: Repository, _: Settings) -> object:
        resolver = TargetResolver(repository)
        return await GroupSetService(repository, resolver).remove(name, targets, actor="cli")

    result = _run(_with_repository(operation))
    typer.echo(
        f"Group set updated: {result.name}\n"
        f"Removed: {result.changed}\nNot present: {result.unchanged}"
    )


@group_sets_app.command("list")
def group_set_list() -> None:
    """List persistent Group Sets and their member counts."""

    async def operation(repository: Repository, _: Settings) -> list[tuple[object, int]]:
        resolver = TargetResolver(repository)
        return await GroupSetService(repository, resolver).list()

    group_sets = _run(_with_repository(operation))
    if not group_sets:
        typer.echo("No group sets found.")
        return
    for group_set, count in group_sets:
        typer.echo(f"{group_set.name}  {count} groups")


@group_sets_app.command("show")
def group_set_show(name: str) -> None:
    """Show a set's canonical members and current eligibility."""

    async def operation(repository: Repository, _: Settings) -> object:
        resolver = TargetResolver(repository)
        return await GroupSetService(repository, resolver).snapshot(name)

    snapshot = _run(_with_repository(operation))
    typer.echo(f"Group set: {snapshot.group_set.name} ({len(snapshot.members)} groups)")
    for view in snapshot.members:
        member = view.member
        destination = member.destination
        label = (
            destination.alias or destination.title
            if destination is not None
            else str(member.destination_peer_id)
        )
        typer.echo(f"{view.status:<9} {member.destination_peer_id:<22} {label}")
    typer.echo(
        f"Allowed: {snapshot.allowed}\nDisabled: {snapshot.disabled}\n"
        f"Missing: {snapshot.missing}\nDuplicates: {snapshot.duplicates}"
    )


@group_sets_app.command("delete")
def group_set_delete(name: str) -> None:
    """Delete a Group Set and its memberships, but not destinations."""

    async def operation(repository: Repository, _: Settings) -> str:
        resolver = TargetResolver(repository)
        service = GroupSetService(repository, resolver)
        normalized = service.normalize_name(name)
        await service.delete(normalized, actor="cli")
        return normalized

    deleted_name = _run(_with_repository(operation))
    typer.echo(f"Group set deleted: {deleted_name}")


@app.command("send")
def send_command(
    group: Annotated[str, typer.Option("--group", help="Whitelisted ID or alias")],
    text: Annotated[str | None, typer.Option("--text")] = None,
    file: Annotated[Path | None, typer.Option("--file", exists=True, dir_okay=False)] = None,
    at: Annotated[
        str | None, typer.Option("--at", help="Local or offset-aware ISO datetime")
    ] = None,
    parse_mode: Annotated[str | None, typer.Option("--parse-mode")] = None,
    disable_link_preview: Annotated[bool, typer.Option("--disable-link-preview")] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Explicitly allow an identical send")
    ] = False,
) -> None:
    """Queue text or media for a whitelisted group."""

    async def operation(repository: Repository, settings: Settings) -> object:
        scheduled_at = parse_schedule(at, settings.timezone) if at else None
        return await MessageService(repository, settings).queue_message(
            group,
            text=text,
            media_path=file,
            scheduled_at=scheduled_at,
            parse_mode=parse_mode,
            disable_link_preview=disable_link_preview,
            force=force,
        )

    result = _run(_with_repository(operation))
    if result.duplicate:
        typer.secho(
            f"Duplicate suppressed. Existing job: {result.job.uuid}", fg=typer.colors.YELLOW
        )
    elif result.job.status == JobStatus.DRY_RUN:
        typer.echo(
            f"[DRY-RUN] target resolved: {result.job.destination_chat_id}\n"
            f"[DRY-RUN] would send message: {_safe_preview(result.job.text)}\n"
            "[DRY-RUN] send skipped"
        )
    else:
        typer.echo(f"Queued job {result.job.uuid} ({result.job.status})")


@app.command("sendall")
def send_all_command(
    text: Annotated[str, typer.Option("--text", "-t", help="Message text")],
) -> None:
    """Queue one independent job for every currently allowed Telegram group."""

    async def operation(
        repository: Repository, settings: Settings
    ) -> tuple[BulkEnqueueResult | None, bool]:
        resolver = TargetResolver(repository)
        resolution = await resolver.allowed_groups()
        if not resolution.destinations:
            return None, settings.dry_run
        result = await BulkMessageService(
            repository, MessageService(repository, settings)
        ).enqueue_bulk(
            resolution.destinations,
            text=text,
            batch_type="sendall",
            requested=(
                len(resolution.destinations) + resolution.duplicates + len(resolution.errors)
            ),
            duplicates=resolution.duplicates,
            skipped=len(resolution.errors),
            actor="cli",
        )
        return result, settings.dry_run

    result, dry_run = _run(_with_repository(operation))
    if result is None:
        typer.echo("No allowed groups found. Nothing was queued.")
        return
    typer.echo(_bulk_output("Send-all queued", result, dry_run=dry_run))


@app.command("sendmulti")
def send_multi_command(
    groups: Annotated[
        list[str],
        typer.Option(
            "--group",
            "-g",
            help="Allowed alias or peer ID; repeat the option or separate values with commas.",
        ),
    ],
    text: Annotated[str, typer.Option("--text", "-t", help="Message text")],
) -> None:
    """Atomically validate selected groups, then queue one job per canonical peer."""
    targets = _split_cli_targets(groups)

    async def operation(
        repository: Repository, settings: Settings
    ) -> tuple[BulkEnqueueResult, bool]:
        resolver = TargetResolver(repository)
        resolution = await resolver.resolve_targets(targets)
        if resolution.errors:
            details = "; ".join(f"{item.reference} - {item.reason}" for item in resolution.errors)
            raise ValueError(
                f"Multi-send aborted. Invalid targets: {details}. No messages were queued."
            )
        result = await BulkMessageService(
            repository, MessageService(repository, settings)
        ).enqueue_bulk(
            resolution.destinations,
            text=text,
            batch_type="sendmulti",
            requested=len(targets),
            duplicates=resolution.duplicates,
            actor="cli",
        )
        return result, settings.dry_run

    result, dry_run = _run(_with_repository(operation))
    typer.echo(_bulk_output("Multi-send queued", result, dry_run=dry_run))


@app.command("sendset")
def send_set_command(
    name: str,
    text: Annotated[str, typer.Option("--text", "-t", help="Message text")],
) -> None:
    """Queue one job for every currently eligible member of a Group Set."""

    async def operation(
        repository: Repository, settings: Settings
    ) -> tuple[object, BulkEnqueueResult | None, bool]:
        resolver = TargetResolver(repository)
        group_sets = GroupSetService(repository, resolver)
        snapshot = await group_sets.snapshot(name)
        if not snapshot.eligible:
            return snapshot, None, settings.dry_run
        result = await BulkMessageService(
            repository, MessageService(repository, settings)
        ).enqueue_bulk(
            snapshot.eligible,
            text=text,
            batch_type="sendset",
            requested=len(snapshot.members),
            duplicates=snapshot.duplicates,
            skipped=snapshot.disabled + snapshot.missing,
            actor="cli",
        )
        return snapshot, result, settings.dry_run

    snapshot, result, dry_run = _run(_with_repository(operation))
    if result is None:
        typer.echo(f"No eligible groups in set: {snapshot.group_set.name}. Nothing was queued.")
        return
    typer.echo(
        _bulk_output(f"Group set queued: {snapshot.group_set.name}", result, dry_run=dry_run)
    )
    typer.echo(f"Disabled: {snapshot.disabled}\nMissing: {snapshot.missing}")


@app.command("batch")
def batch_status_command(batch_id: str) -> None:
    """Show aggregate queue status for a bulk-send batch UUID."""

    async def operation(
        repository: Repository, settings: Settings
    ) -> tuple[object, dict[str, int]]:
        return await BulkMessageService(
            repository, MessageService(repository, settings)
        ).batch_status(batch_id)

    batch, counts = _run(_with_repository(operation))
    pending_statuses = {
        JobStatus.PENDING,
        JobStatus.SCHEDULED,
        JobStatus.PROCESSING,
        JobStatus.RETRY,
        JobStatus.WAITING_RATE_LIMIT,
        JobStatus.REVIEW_REQUIRED,
    }
    pending = sum(counts.get(status.value, 0) for status in pending_statuses)
    typer.echo(
        f"Batch: {batch.id}\n"
        f"Type: {batch.batch_type}\n"
        f"Targets: {batch.requested_count}\n"
        f"Jobs: {sum(counts.values())}\n"
        f"Pending: {pending}\n"
        f"Sent: {counts.get(JobStatus.SENT.value, 0)}\n"
        f"Failed: {counts.get(JobStatus.FAILED.value, 0)}\n"
        f"Cancelled: {counts.get(JobStatus.CANCELLED.value, 0)}\n"
        f"Dry run: {counts.get(JobStatus.DRY_RUN.value, 0)}"
    )


@queue_app.command("list")
def queue_list() -> None:
    """List recent queue jobs."""

    async def operation(repository: Repository, _: Settings) -> list[object]:
        return await repository.list_jobs(limit=100)

    jobs = _run(_with_repository(operation))
    typer.echo(f"{'ID':<38} {'STATUS':<20} {'CHAT':<22} ATTEMPTS")
    for job in jobs:
        typer.echo(
            f"{job.uuid:<38} {job.status:<20} {job.destination_chat_id:<22} "
            f"{job.attempt_count}/{job.max_attempts}"
        )


@queue_app.command("failed")
def queue_failed() -> None:
    """List failed and crash-uncertain jobs."""

    async def operation(repository: Repository, _: Settings) -> list[object]:
        return await repository.list_jobs({JobStatus.FAILED, JobStatus.REVIEW_REQUIRED})

    jobs = _run(_with_repository(operation))
    for job in jobs:
        typer.echo(f"{job.uuid}  {job.status}  {job.last_error_type or '-'}")


@queue_app.command("cancel")
def queue_cancel(job_id: str) -> None:
    """Cancel an unsent job."""

    async def operation(repository: Repository, _: Settings) -> object:
        return await repository.cancel_job(job_id)

    job = _run(_with_repository(operation))
    typer.echo(f"Cancelled job {job.uuid}")


@queue_app.command("retry")
def queue_retry(job_id: str) -> None:
    """Explicitly retry a failed or review-required job."""

    async def operation(repository: Repository, _: Settings) -> object:
        return await repository.retry_job(job_id)

    job = _run(_with_repository(operation))
    typer.echo(f"Retry queued for {job.uuid}")
