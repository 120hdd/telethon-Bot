from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer

from app.config import Settings, get_settings
from app.db.database import Database, DatabaseLockedError
from app.db.repositories import NotFoundError, Repository
from app.main import run_application
from app.messaging.scheduler import parse_schedule
from app.messaging.service import MessageService
from app.models import JobStatus
from app.services.health import format_health, get_health
from app.telegram.client import (
    AuthenticationRequiredError,
    SessionInUseError,
    TelegramConnection,
    create_client,
)
from app.telegram.dialogs import refresh_dialogs
from app.utils.locks import AlreadyRunningError, ProcessLock

app = typer.Typer(help="Conservative Telegram personal-account messaging client.")
groups_app = typer.Typer(help="Discover and manage allowed Telegram groups.")
queue_app = typer.Typer(help="Inspect and manage persistent outgoing jobs.")
app.add_typer(groups_app, name="groups")
app.add_typer(queue_app, name="queue")


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
        DatabaseLockedError,
    ) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


class DuplicateError(ValueError):
    pass


async def _with_repository[T](
    operation: Callable[[Repository, Settings], Awaitable[T]],
) -> T:
    settings = get_settings()
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
def start() -> None:
    """Start the Telegram connection, queue worker, and Saved Messages controller."""
    settings = get_settings()
    _run(run_application(settings))


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
        connection = TelegramConnection(create_client(settings), settings, repository)
        try:
            await connection.connect_and_authorize()
            return await refresh_dialogs(connection.client, repository)
        finally:
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
            f"DRY RUN\nDestination: {result.job.destination_chat_id}\n"
            f"Message:\n{result.job.text or '[media only]'}"
        )
    else:
        typer.echo(f"Queued job {result.job.uuid} ({result.job.status})")


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
