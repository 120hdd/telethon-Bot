from __future__ import annotations

import pytest

from app.commands.saved_messages import (
    SavedCommandKind,
    SavedMessagesController,
    parse_saved_command,
)
from app.db.repositories import Repository
from app.messaging.service import MessageService
from app.models import JobStatus, NewDestination
from tests.conftest import add_destination


def controller(repository: Repository, settings) -> SavedMessagesController:
    service = MessageService(repository, settings)
    return SavedMessagesController(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        repository,
        service,
        1,
    )


async def add_group(
    repository: Repository,
    chat_id: int,
    alias: str,
    *,
    enabled: bool = True,
    can_send: bool = True,
    chat_type: str = "supergroup",
) -> None:
    await repository.upsert_destinations(
        [
            NewDestination(
                telegram_chat_id=chat_id,
                title=f"Group {alias}",
                username=f"{alias}_username",
                chat_type=chat_type,
                can_send=can_send,
            )
        ]
    )
    await repository.set_destination_alias(chat_id, alias)
    if enabled:
        await repository.set_destination_enabled(chat_id, True)


@pytest.mark.parametrize(
    ("raw", "kind", "targets", "text"),
    [
        (".sendall سلام دوستان", SavedCommandKind.SEND_ALL, (), "سلام دوستان"),
        (
            ".sendmulti one, two,three سلام دوستان، امروز",
            SavedCommandKind.SEND_MULTI,
            ("one", "two", "three"),
            "سلام دوستان، امروز",
        ),
        ("/sendset vip hello team", SavedCommandKind.SEND_SET, (), "hello team"),
    ],
)
def test_parses_bulk_commands(raw: str, kind: SavedCommandKind, targets, text: str) -> None:
    command = parse_saved_command(raw)
    assert command is not None
    assert command.kind == kind
    assert command.targets == targets
    assert command.text == text


@pytest.mark.asyncio
async def test_sendall_with_no_allowed_groups_queues_nothing(repository, settings) -> None:
    response = await controller(repository, settings).execute(
        parse_saved_command("/sendall hello")  # type: ignore[arg-type]
    )

    assert "No allowed groups" in response
    assert await repository.list_jobs() == []


@pytest.mark.asyncio
async def test_sendall_queues_one_job_per_eligible_group_with_one_batch(
    repository, settings
) -> None:
    await add_group(repository, -1001, "one")
    await add_group(repository, -1002, "two")
    await add_group(repository, -1003, "denied", enabled=False)
    await add_group(repository, 44, "person", chat_type="user")

    response = await controller(repository, settings).execute(
        parse_saved_command("/sendall پیام همگانی")  # type: ignore[arg-type]
    )
    jobs = await repository.list_jobs()

    assert len(jobs) == 2
    assert {job.destination_chat_id for job in jobs} == {-1001, -1002}
    assert len({job.batch_id for job in jobs}) == 1
    assert next(iter({job.batch_id for job in jobs})) is not None
    assert "Targets: 3" in response
    assert "Queued: 2" in response
    assert "Skipped: 1" in response


@pytest.mark.asyncio
async def test_sendmulti_deduplicates_alias_and_numeric_identity(repository, settings) -> None:
    await add_group(repository, -1001, "one")
    await add_group(repository, -1002, "two")

    response = await controller(repository, settings).execute(
        parse_saved_command("/sendmulti one,-1001,two سلام")  # type: ignore[arg-type]
    )
    jobs = await repository.list_jobs()

    assert len(jobs) == 2
    assert len({job.batch_id for job in jobs}) == 1
    assert all(job.text == "سلام" for job in jobs)
    assert "Duplicates: 1" in response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_target", "reason"),
    [
        ("missing", "not found"),
        ("999999999999999999999999999999", "malformed Telegram ID"),
        ("disabled", "disabled"),
        ("person", "not a Telegram group"),
    ],
)
async def test_sendmulti_validation_is_atomic(
    repository, settings, bad_target: str, reason: str
) -> None:
    await add_group(repository, -1001, "one")
    await add_group(repository, -1002, "disabled", enabled=False)
    await add_group(repository, 44, "person", chat_type="user")

    response = await controller(repository, settings).execute(
        parse_saved_command(f"/sendmulti one,{bad_target} hello")  # type: ignore[arg-type]
    )

    assert "Multi-send aborted" in response
    assert reason in response
    assert await repository.list_jobs() == []
    assert await repository.get_batch(response) is None


@pytest.mark.asyncio
async def test_bulk_idempotency_is_destination_specific(repository, settings) -> None:
    await add_group(repository, -1001, "one")
    await add_group(repository, -1002, "two")

    await controller(repository, settings).execute(
        parse_saved_command("/sendmulti one,two same")  # type: ignore[arg-type]
    )

    jobs = await repository.list_jobs()
    assert len(jobs) == 2
    assert len({job.idempotency_key for job in jobs}) == 2


@pytest.mark.asyncio
async def test_bulk_dry_run_persists_separate_jobs_and_never_claims_them(
    repository, settings
) -> None:
    await add_group(repository, -1001, "one")
    await add_group(repository, -1002, "two")
    dry_settings = settings.model_copy(update={"dry_run": True})

    await controller(repository, dry_settings).execute(
        parse_saved_command("/sendall preview")  # type: ignore[arg-type]
    )

    jobs = await repository.list_jobs()
    assert len(jobs) == 2
    assert {job.status for job in jobs} == {JobStatus.DRY_RUN}


@pytest.mark.asyncio
async def test_batch_command_reports_current_job_states(repository, settings) -> None:
    await add_destination(repository)
    ctl = controller(repository, settings)
    response = await ctl.execute(parse_saved_command("/sendall hello"))  # type: ignore[arg-type]
    batch_id = response.rsplit("Batch: ", 1)[1]

    status = await ctl.execute(parse_saved_command(f"/batch {batch_id}"))  # type: ignore[arg-type]

    assert f"Batch: {batch_id}" in status
    assert "Targets: 1" in status
    assert "Pending: 1" in status
