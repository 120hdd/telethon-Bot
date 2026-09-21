from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from app.config import Settings
from app.db.database import Database
from app.db.repositories import Repository
from app.models import NewDestination


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        TELEGRAM_API_MODE="custom",
        TELEGRAM_API_ID=12345,
        TELEGRAM_API_HASH="test-api-hash",
        TELEGRAM_PHONE="+10000000000",
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        TELEGRAM_SESSION_DIR=tmp_path / "sessions",
        UPLOADS_DIR=tmp_path / "uploads",
        LOG_PATH=tmp_path / "logs" / "app.log",
        LOCAL_TIMEZONE="UTC",
        DEFAULT_SEND_INTERVAL_SECONDS=0,
        WORKER_POLL_SECONDS=0.01,
    )


@pytest_asyncio.fixture
async def repository(tmp_path: Path) -> AsyncIterator[Repository]:
    database = Database(tmp_path / "app.db")
    await database.connect()
    repo = Repository(database)
    await repo.initialize()
    try:
        yield repo
    finally:
        await database.close()


async def add_destination(
    repository: Repository,
    *,
    chat_id: int = -1001234567890,
    enabled: bool = True,
    can_send: bool = True,
    alias: str = "work",
) -> int:
    await repository.upsert_destinations(
        [
            NewDestination(
                telegram_chat_id=chat_id,
                title="Work Group",
                username="workgroup",
                chat_type="supergroup",
                can_send=can_send,
            )
        ]
    )
    if alias:
        await repository.set_destination_alias(chat_id, alias)
    if enabled:
        await repository.set_destination_enabled(chat_id, True)
    return chat_id
