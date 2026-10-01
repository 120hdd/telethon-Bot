from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from app.commands import cli
from app.db.database import Database
from app.db.repositories import Repository
from app.models import JobStatus, NewDestination, TelegramAccount
from app.utils.time import to_db_time, utc_now


def test_cli_forward_commands_queue_persistent_jobs(tmp_path: Path, settings, monkeypatch) -> None:
    database_path = tmp_path / "cli-forward.db"
    cli_settings = settings.model_copy(update={"database_path": database_path, "dry_run": False})

    async def with_repository(operation):
        database = Database(database_path)
        await database.connect()
        try:
            repository = Repository(database)
            await repository.initialize()
            return await operation(repository, cli_settings)
        finally:
            await database.close()

    async def seed() -> None:
        async def operation(repository, _):
            await repository.set_account(TelegramAccount(1, None, "Owner", to_db_time(utc_now())))
            for chat_id, alias, allowed in (
                (-1001, "family", True),
                (-1002, "work", True),
                (-1003, "off", False),
            ):
                await repository.upsert_destinations(
                    [NewDestination(chat_id, alias.title(), None, "supergroup", True)]
                )
                await repository.set_destination_alias(chat_id, alias)
                if allowed:
                    await repository.set_destination_enabled(chat_id, True)

        await with_repository(operation)

    asyncio.run(seed())
    monkeypatch.setattr(cli, "_with_repository", with_repository)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["groupset", "create", "customers"]).exit_code == 0
    assert (
        runner.invoke(cli.app, ["groupset", "add", "customers", "-g", "family,work"]).exit_code == 0
    )

    commands = [
        ["forward", "-g", "family", "--source-id", "77"],
        ["forwardmulti", "-g", "family,work", "--source-id", "78"],
        ["forwardset", "customers", "--source-id", "79"],
        ["forwardall", "--source-id", "80"],
        ["forward", "-g", "family", "--source-id", "81", "--at", "2030-01-01T10:00:00Z"],
    ]
    results = [runner.invoke(cli.app, command) for command in commands]
    assert all(result.exit_code == 0 for result in results), [result.output for result in results]
    assert "Queued: 1" in results[0].stdout
    assert all("Queued: 2" in result.stdout for result in results[1:4])
    assert "Batch:" in results[3].stdout

    async def inspect():
        async def operation(repository, _):
            return await repository.list_jobs()

        return await with_repository(operation)

    jobs = asyncio.run(inspect())
    assert len(jobs) == 8
    assert {job.source_message_id for job in jobs} == {77, 78, 79, 80, 81}
    assert all(job.source_chat_id == 1 and job.text is None for job in jobs)
    assert next(job for job in jobs if job.source_message_id == 81).status == JobStatus.SCHEDULED

    duplicate = runner.invoke(cli.app, commands[0])
    invalid = runner.invoke(cli.app, ["forwardmulti", "-g", "family,missing", "--source-id", "82"])
    assert "Queued: 0" in duplicate.stdout
    assert invalid.exit_code == 1
    assert len(asyncio.run(inspect())) == 8
