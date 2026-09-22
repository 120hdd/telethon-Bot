from __future__ import annotations

from datetime import timedelta

import pytest

from app.commands.saved_messages import SavedMessagesController, parse_saved_command
from app.db.database import Database
from app.db.repositories import NotFoundError, Repository
from app.messaging.group_sets import GroupSetService
from app.messaging.service import MessageService
from app.messaging.targets import TargetResolver
from app.models import NewDestination
from app.utils.time import to_db_time, utc_now


async def add_group(repository: Repository, chat_id: int, alias: str, enabled: bool = True) -> None:
    await repository.upsert_destinations(
        [
            NewDestination(
                telegram_chat_id=chat_id,
                title=f"Title {alias}",
                username=alias,
                chat_type="supergroup",
                can_send=True,
            )
        ]
    )
    await repository.set_destination_alias(chat_id, alias)
    if enabled:
        await repository.set_destination_enabled(chat_id, True)


def services(repository: Repository, settings):
    resolver = TargetResolver(repository)
    group_sets = GroupSetService(repository, resolver)
    controller = SavedMessagesController(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        repository,
        MessageService(repository, settings),
        1,
        target_resolver=resolver,
        group_set_service=group_sets,
    )
    return group_sets, controller


@pytest.mark.asyncio
async def test_group_set_crud_is_atomic_and_case_insensitive(repository, settings) -> None:
    await add_group(repository, -1001, "one")
    await add_group(repository, -1002, "two")
    group_sets, _ = services(repository, settings)

    created = await group_sets.create("VIP")
    assert created.name == "vip"
    with pytest.raises(ValueError, match="already exists"):
        await group_sets.create("vip")

    added = await group_sets.add("VIP", ["one", "two", "one"])
    assert (added.changed, added.unchanged) == (2, 1)

    with pytest.raises(ValueError, match="No changes were made"):
        await group_sets.add("vip", ["one", "missing"])
    _, members = await group_sets.members("vip")
    assert {member.destination_peer_id for member in members} == {-1001, -1002}

    removed = await group_sets.remove("vip", ["one", "-9999"])
    assert (removed.changed, removed.unchanged) == (1, 1)
    await group_sets.delete("vip")
    assert await repository.list_group_sets() == []


@pytest.mark.asyncio
async def test_group_set_membership_survives_metadata_and_alias_changes(
    repository, settings
) -> None:
    await add_group(repository, -1001, "old_alias")
    group_sets, _ = services(repository, settings)
    await group_sets.create("work")
    await group_sets.add("work", ["old_alias"])

    await repository.set_destination_alias(-1001, "new_alias")
    await repository.upsert_destinations(
        [NewDestination(-1001, "Renamed Title", "renamed_user", "supergroup", True)]
    )
    _, members = await group_sets.members("work")

    assert members[0].destination_peer_id == -1001
    assert members[0].destination is not None
    assert members[0].destination.alias == "new_alias"
    assert members[0].destination.title == "Renamed Title"


@pytest.mark.asyncio
async def test_group_sets_persist_after_repository_recreation(tmp_path, settings) -> None:
    path = tmp_path / "persistent.db"
    database = Database(path)
    await database.connect()
    first = Repository(database)
    await first.initialize()
    await add_group(first, -1001, "one")
    first_service = GroupSetService(first, TargetResolver(first))
    await first_service.create("persisted")
    await first_service.add("persisted", ["one"])
    await database.close()

    reopened_database = Database(path)
    await reopened_database.connect()
    reopened = Repository(reopened_database)
    await reopened.initialize()
    try:
        group_set = await reopened.get_group_set("PERSISTED")
        assert group_set is not None
        members = await reopened.list_group_set_members(group_set.id)
        assert [member.destination_peer_id for member in members] == [-1001]
    finally:
        await reopened_database.close()


@pytest.mark.asyncio
async def test_sendset_skips_disabled_and_missing_members(repository, settings) -> None:
    await add_group(repository, -1001, "allowed")
    await add_group(repository, -1002, "disabled", enabled=False)
    group_sets, ctl = services(repository, settings)
    group_set = await group_sets.create("mixed")
    await group_sets.add("mixed", ["allowed", "disabled"])
    await repository.add_group_set_members(group_set.id, [-1003])

    response = await ctl.execute(
        parse_saved_command("/sendset mixed سلام")  # type: ignore[arg-type]
    )
    jobs = await repository.list_jobs()

    assert len(jobs) == 1
    assert jobs[0].destination_chat_id == -1001
    assert jobs[0].batch_id is not None
    assert "Eligible: 1" in response
    assert "Disabled: 1" in response
    assert "Missing: 1" in response


@pytest.mark.asyncio
async def test_disabled_group_remains_in_set_and_cascade_delete_removes_members(
    repository, settings
) -> None:
    await add_group(repository, -1001, "one")
    group_sets, _ = services(repository, settings)
    group_set = await group_sets.create("work")
    await group_sets.add("work", ["one"])

    await repository.set_destination_enabled(-1001, False)
    assert len(await repository.list_group_set_members(group_set.id)) == 1
    await group_sets.delete("work")
    assert await repository.list_group_set_members(group_set.id) == []


@pytest.mark.asyncio
async def test_show_lists_current_statuses_and_missing_identity(repository, settings) -> None:
    await add_group(repository, -1001, "allowed")
    await add_group(repository, -1002, "disabled", enabled=False)
    group_sets, ctl = services(repository, settings)
    group_set = await group_sets.create("showcase")
    await group_sets.add("showcase", ["allowed", "disabled"])
    await repository.add_group_set_members(group_set.id, [-1003])

    response = await ctl.execute(
        parse_saved_command("/groupset show showcase")  # type: ignore[arg-type]
    )

    assert "Allowed: 1" in response
    assert "Disabled: 1" in response
    assert "Missing: 1" in response
    assert "-1003" in response


@pytest.mark.asyncio
async def test_group_absent_from_latest_dialog_refresh_is_missing(repository, settings) -> None:
    await add_group(repository, -1001, "stale")
    group_sets, ctl = services(repository, settings)
    await group_sets.create("old")
    await group_sets.add("old", ["stale"])
    await repository.set_state(
        "last_dialog_refresh_started_at", to_db_time(utc_now() + timedelta(seconds=1))
    )

    shown = await ctl.execute(
        parse_saved_command("/groupset show old")  # type: ignore[arg-type]
    )
    sent = await ctl.execute(
        parse_saved_command("/sendset old hello")  # type: ignore[arg-type]
    )

    assert "Missing: 1" in shown
    assert "No eligible groups" in sent
    assert await repository.list_jobs() == []


@pytest.mark.asyncio
async def test_sendset_handles_empty_and_unknown_sets(repository, settings) -> None:
    group_sets, ctl = services(repository, settings)
    await group_sets.create("empty")

    empty = await ctl.execute(
        parse_saved_command("/sendset empty hello")  # type: ignore[arg-type]
    )

    assert "No eligible groups" in empty
    assert await repository.list_jobs() == []
    with pytest.raises(NotFoundError, match="not found"):
        await ctl.execute(
            parse_saved_command("/sendset unknown hello")  # type: ignore[arg-type]
        )
