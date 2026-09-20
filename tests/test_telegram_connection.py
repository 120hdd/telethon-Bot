from types import SimpleNamespace

import pytest

from app.db.repositories import Repository
from app.telegram.client import TelegramConnection


class AuthorizedClient:
    async def connect(self) -> None:
        return None

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> object:
        return SimpleNamespace(id=77, username="owner", first_name="Test", last_name="Owner")


@pytest.mark.asyncio
async def test_successful_login_clears_stale_auth_pause(repository: Repository, settings) -> None:
    await repository.set_state("outgoing_pause_reason", "AUTH_REQUIRED")
    await repository.set_state("outgoing_pause_until", None)
    connection = TelegramConnection(AuthorizedClient(), settings, repository)  # type: ignore[arg-type]

    account = await connection.connect_and_authorize(interactive=False)

    assert account.telegram_user_id == 77
    assert await repository.get_state("connectivity") == "CONNECTED"
    assert await repository.get_state("outgoing_pause_reason") is None
