from types import SimpleNamespace

import pytest
from telethon import errors

from app.db.repositories import Repository
from app.telegram.client import AuthenticationRequiredError, TelegramConnection
from app.telegram.errors import TelegramAuthorizationError


class AuthorizedClient:
    send_code_requests = 0

    async def connect(self) -> None:
        return None

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> object:
        return SimpleNamespace(
            id=77,
            username="owner",
            first_name="Test",
            last_name="Owner",
            phone="10000000000",
        )

    async def send_code_request(self, _: str) -> None:
        self.send_code_requests += 1


class UnauthorizedClient(AuthorizedClient):
    async def is_user_authorized(self) -> bool:
        return False


class TwoFactorClient(AuthorizedClient):
    def __init__(self) -> None:
        self.authorized = False
        self.send_code_requests = 0
        self.passwords: list[str] = []

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def sign_in(self, **kwargs: str) -> None:
        if "password" in kwargs:
            self.passwords.append(kwargs["password"])
            self.authorized = True
            return
        raise errors.SessionPasswordNeededError(None)


class RetryingCodeClient(TwoFactorClient):
    def __init__(self) -> None:
        super().__init__()
        self.codes: list[str] = []

    async def sign_in(self, **kwargs: str) -> None:
        code = kwargs["code"]
        self.codes.append(code)
        if len(self.codes) == 1:
            raise errors.PhoneCodeInvalidError(None)
        self.authorized = True


class ExpiredCodeClient(TwoFactorClient):
    async def sign_in(self, **kwargs: str) -> None:
        raise errors.PhoneCodeExpiredError(None)


@pytest.mark.asyncio
async def test_successful_login_clears_stale_auth_pause(repository: Repository, settings) -> None:
    await repository.set_state("outgoing_pause_reason", "AUTH_REQUIRED")
    await repository.set_state("outgoing_pause_until", None)
    connection = TelegramConnection(AuthorizedClient(), settings, repository)  # type: ignore[arg-type]

    account = await connection.connect_and_authorize(interactive=False)

    assert account.telegram_user_id == 77
    assert await repository.get_state("connectivity") == "CONNECTED"
    assert await repository.get_state("outgoing_pause_reason") is None
    assert connection.client.send_code_requests == 0


@pytest.mark.asyncio
async def test_normal_startup_never_requests_a_login_code(repository, settings) -> None:
    client = UnauthorizedClient()
    connection = TelegramConnection(client, settings, repository)  # type: ignore[arg-type]

    with pytest.raises(AuthenticationRequiredError, match="auth login"):
        await connection.connect_and_authorize()

    assert client.send_code_requests == 0


@pytest.mark.asyncio
async def test_two_factor_password_is_prompted_securely(repository, settings) -> None:
    client = TwoFactorClient()

    async def code_prompt(_: str) -> str:
        return "12345"

    async def password_prompt(_: str) -> str:
        return "test-password"

    connection = TelegramConnection(
        client,  # type: ignore[arg-type]
        settings,
        repository,
        code_prompt=code_prompt,
        two_factor_prompt=password_prompt,
        notice=lambda _: None,
    )

    account = await connection.connect_and_authorize(interactive=True)

    assert account.telegram_user_id == 77
    assert client.send_code_requests == 1
    assert client.passwords == ["test-password"]


@pytest.mark.asyncio
async def test_wrong_code_retries_input_without_requesting_another_code(
    repository, settings
) -> None:
    client = RetryingCodeClient()
    codes = iter(["wrong", "right"])

    async def code_prompt(_: str) -> str:
        return next(codes)

    connection = TelegramConnection(
        client,  # type: ignore[arg-type]
        settings,
        repository,
        code_prompt=code_prompt,
        notice=lambda _: None,
    )

    await connection.connect_and_authorize(interactive=True)

    assert client.send_code_requests == 1
    assert client.codes == ["wrong", "right"]


@pytest.mark.asyncio
async def test_expired_code_requires_a_new_explicit_login(repository, settings) -> None:
    client = ExpiredCodeClient()

    async def code_prompt(_: str) -> str:
        return "expired"

    connection = TelegramConnection(
        client,  # type: ignore[arg-type]
        settings,
        repository,
        code_prompt=code_prompt,
        notice=lambda _: None,
    )

    with pytest.raises(TelegramAuthorizationError, match="expired"):
        await connection.connect_and_authorize(interactive=True)

    assert client.send_code_requests == 1
