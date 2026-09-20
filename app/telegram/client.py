from __future__ import annotations

import asyncio
import getpass
import logging
import sqlite3
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, errors

from app.config import Settings
from app.db.repositories import Repository
from app.models import TelegramAccount
from app.utils.time import to_db_time, utc_now

logger = logging.getLogger(__name__)
Prompt = Callable[[str], Awaitable[str]]


class AuthenticationRequiredError(RuntimeError):
    pass


class SessionInUseError(RuntimeError):
    pass


async def terminal_prompt(label: str) -> str:
    return await asyncio.to_thread(input, label)


async def password_prompt(label: str) -> str:
    return await asyncio.to_thread(getpass.getpass, label)


def create_client(settings: Settings) -> TelegramClient:
    api_id, api_hash, _ = settings.require_telegram_credentials()
    try:
        return TelegramClient(
            str(settings.tg_session_path),
            api_id,
            api_hash,
            auto_reconnect=True,
            connection_retries=5,
            request_retries=3,
            retry_delay=2,
            flood_sleep_threshold=0,
            sequential_updates=True,
        )
    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            raise SessionInUseError(
                "The Telethon session database is locked; another process may be using it."
            ) from exc
        raise


class TelegramConnection:
    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        repository: Repository,
        *,
        code_prompt: Prompt = terminal_prompt,
        two_factor_prompt: Prompt = password_prompt,
    ) -> None:
        self.client = client
        self.settings = settings
        self.repository = repository
        self.code_prompt = code_prompt
        self.two_factor_prompt = two_factor_prompt
        self.account: TelegramAccount | None = None

    async def connect_and_authorize(self, *, interactive: bool = True) -> TelegramAccount:
        logger.info("telegram_connecting")
        await self.repository.set_state("connectivity", "RECONNECTING")
        try:
            await self.client.connect()
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                raise SessionInUseError(
                    "The Telethon session database is locked; another process may be using it."
                ) from exc
            raise
        except OSError:
            await self.repository.set_state("connectivity", "DISCONNECTED")
            raise

        if not await self.client.is_user_authorized():
            await self.repository.set_state("connectivity", "AUTH_REQUIRED")
            if not interactive:
                raise AuthenticationRequiredError("Telegram login is required.")
            await self._authenticate()

        me = await self.client.get_me()
        if me is None:
            await self.repository.set_state("connectivity", "AUTH_REQUIRED")
            raise AuthenticationRequiredError("Telegram did not return an authorized account.")
        display_name = " ".join(
            part
            for part in (getattr(me, "first_name", None), getattr(me, "last_name", None))
            if part
        ) or str(me.id)
        account = TelegramAccount(
            telegram_user_id=int(me.id),
            username=getattr(me, "username", None),
            display_name=display_name,
            last_successful_connection_at=to_db_time(utc_now()),
        )
        await self.repository.set_account(account)
        await self.repository.set_state("connectivity", "CONNECTED")
        if await self.repository.get_state("outgoing_pause_reason") == "AUTH_REQUIRED":
            await self.repository.set_state("outgoing_pause_reason", None)
            await self.repository.set_state("outgoing_pause_until", None)
        self.account = account
        logger.info("telegram_connected", extra={"telegram_user_id": account.telegram_user_id})
        return account

    async def _authenticate(self) -> None:
        _, _, phone = self.settings.require_telegram_credentials()
        logger.warning("auth_required")
        await self.client.send_code_request(phone)
        code = (await self.code_prompt("Telegram login code: ")).strip()
        try:
            await self.client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            password = await self.two_factor_prompt("Telegram 2FA password: ")
            await self.client.sign_in(password=password)
        if not await self.client.is_user_authorized():
            raise AuthenticationRequiredError("Telegram authentication did not complete.")

    async def disconnect(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()
        await self.repository.set_state("connectivity", "DISCONNECTED")
        logger.info("telegram_disconnected")
