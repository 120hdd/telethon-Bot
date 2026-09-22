from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from app.commands import cli
from app.db.database import Database
from app.db.repositories import Repository
from app.models import JobStatus, NewDestination


def test_terminal_bulk_and_group_set_commands_use_the_persistent_queue(
    tmp_path: Path, settings, monkeypatch
) -> None:
    database_path = tmp_path / "cli.db"
    dry_settings = settings.model_copy(update={"database_path": database_path, "dry_run": True})

    async def seed() -> None:
        database = Database(database_path)
        await database.connect()
        try:
            repository = Repository(database)
            await repository.initialize()
            for chat_id, alias, enabled in (
                (-1001, "one", True),
                (-1002, "two", True),
                (-1003, "denied", False),
            ):
                await repository.upsert_destinations(
                    [
                        NewDestination(
                            telegram_chat_id=chat_id,
                            title=alias.title(),
                            username=None,
                            chat_type="supergroup",
                            can_send=True,
                        )
                    ]
                )
                await repository.set_destination_alias(chat_id, alias)
                if enabled:
                    await repository.set_destination_enabled(chat_id, True)
        finally:
            await database.close()

    async def with_repository(operation):
        database = Database(database_path)
        await database.connect()
        try:
            repository = Repository(database)
            await repository.initialize()
            return await operation(repository, dry_settings)
        finally:
            await database.close()

    asyncio.run(seed())
    monkeypatch.setattr(cli, "_with_repository", with_repository)
    runner = CliRunner()

    created = runner.invoke(cli.app, ["groupset", "create", "ads"])
    added = runner.invoke(cli.app, ["groupset", "add", "ads", "-g", "one,two"])
    listed = runner.invoke(cli.app, ["groupset", "list"])
    shown = runner.invoke(cli.app, ["groupset", "show", "ads"])
    multi = runner.invoke(cli.app, ["sendmulti", "-g", "one", "-g", "-1002", "-t", "سلام"])
    send_all = runner.invoke(cli.app, ["sendall", "-t", "همه"])
    send_set = runner.invoke(cli.app, ["sendset", "ads", "-t", "مجموعه"])

    assert created.exit_code == 0
    assert "Group set created: ads" in created.stdout
    assert added.exit_code == 0
    assert "Added: 2" in added.stdout
    assert listed.exit_code == 0
    assert "ads  2 groups" in listed.stdout
    assert shown.exit_code == 0
    assert "Allowed: 2" in shown.stdout
    for result in (multi, send_all, send_set):
        assert result.exit_code == 0, result.output
        assert "[DRY-RUN]" in result.stdout
        assert "Queued: 2" in result.stdout
        assert "Batch:" in result.stdout

    batch_id = multi.stdout.rsplit("Batch: ", 1)[1].strip()
    batch = runner.invoke(cli.app, ["batch", batch_id])
    assert batch.exit_code == 0
    assert "Targets: 2" in batch.stdout
    assert "Dry run: 2" in batch.stdout

    async def inspect() -> tuple[int, set[JobStatus], set[int]]:
        database = Database(database_path)
        await database.connect()
        try:
            repository = Repository(database)
            jobs = await repository.list_jobs()
            return (
                len(jobs),
                {job.status for job in jobs},
                {job.destination_chat_id for job in jobs},
            )
        finally:
            await database.close()

    count, statuses, destinations = asyncio.run(inspect())
    assert count == 6
    assert statuses == {JobStatus.DRY_RUN}
    assert destinations == {-1001, -1002}

    removed = runner.invoke(cli.app, ["groupset", "remove", "ads", "-g", "two"])
    deleted = runner.invoke(cli.app, ["groupset", "delete", "ads"])
    assert removed.exit_code == 0
    assert "Removed: 1" in removed.stdout
    assert deleted.exit_code == 0
    assert "Group set deleted: ads" in deleted.stdout


def test_terminal_multi_send_aborts_atomically(tmp_path: Path, settings, monkeypatch) -> None:
    database_path = tmp_path / "atomic.db"

    async def with_repository(operation):
        database = Database(database_path)
        await database.connect()
        try:
            repository = Repository(database)
            await repository.initialize()
            if not await repository.list_destinations():
                await repository.upsert_destinations(
                    [NewDestination(-1001, "One", None, "supergroup", True)]
                )
                await repository.set_destination_alias(-1001, "one")
                await repository.set_destination_enabled(-1001, True)
            return await operation(repository, settings)
        finally:
            await database.close()

    monkeypatch.setattr(cli, "_with_repository", with_repository)
    result = CliRunner().invoke(cli.app, ["sendmulti", "-g", "one,missing", "-t", "hello"])

    assert result.exit_code == 1
    assert "Multi-send aborted" in result.output

    async def job_count() -> int:
        database = Database(database_path)
        await database.connect()
        try:
            return len(await Repository(database).list_jobs())
        finally:
            await database.close()

    assert asyncio.run(job_count()) == 0
