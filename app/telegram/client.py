from __future__ import annotations

import asyncio
import getpass
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors

from app.config import Settings, mask_phone
from app.db.repositories import Repository
from app.models import TelegramAccount
from app.telegram.errors import TelegramAuthorizationError, authorization_error_message
from app.utils.time import to_db_time, utc_now

logger = logging.getLogger(__name__)
Prompt = Callable[[str], Awaitable[str]]
Notice = Callable[[str], None]


class AuthenticationRequiredError(RuntimeError):
    pass


class SessionInUseError(RuntimeError):
    pass


async def terminal_prompt(label: str) -> str:
    return await asyncio.to_thread(input, label)


async def password_prompt(label: str) -> str:
    return await asyncio.to_thread(getpass.getpass, label)


def session_file_path(session_path: Path) -> Path:
    return Path(f"{session_path}.session")


def session_exists(session_path: Path) -> bool:
    return session_file_path(session_path).is_file()


def protect_session_storage(session_path: Path) -> None:
    try:
        session_path.parent.chmod(0o700)
    except OSError:
        pass
    session_file = session_file_path(session_path)
    if session_file.exists():
        try:
            session_file.chmod(0o600)
        except OSError:
            pass


def create_client(settings: Settings) -> TelegramClient:
    credentials = settings.resolve_api_credentials()
    session_path = settings.session_path_for(credentials.profile)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    protect_session_storage(session_path)
    logger.info("Telegram API profile: %s", credentials.profile)
    logger.info(
        "telegram_session_selected",
        extra={
            "session_path": str(session_file_path(session_path)),
            "session_exists": session_exists(session_path),
        },
    )
    try:
        client = TelegramClient(
            str(session_path),
            credentials.api_id,
            credentials.api_hash,
            timeout=10,
            auto_reconnect=True,
            connection_retries=5,
            request_retries=3,
            retry_delay=2,
            flood_sleep_threshold=0,
            sequential_updates=True,
        )
        protect_session_storage(session_path)
        return client
    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            raise SessionInUseError(
                "The Telethon session database is locked; another process may be using it."
            ) from exc
        raise


def account_summary(me: Any, configured_phone: str | None = None) -> dict[str, str | int]:
    user_id = int(me.id)
    username = getattr(me, "username", None) or "unavailable"
    phone = getattr(me, "phone", None) or configured_phone
    if phone and not str(phone).startswith("+"):
        phone = f"+{phone}"
    return {"user_id": user_id, "username": username, "phone": mask_phone(phone)}


class TelegramConnection:
    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        repository: Repository,
        *,
        code_prompt: Prompt = terminal_prompt,
        two_factor_prompt: Prompt = password_prompt,
        notice: Notice = print,
    ) -> None:
        self.client = client
        self.settings = settings
        self.repository = repository
        self.code_prompt = code_prompt
        self.two_factor_prompt = two_factor_prompt
        self.notice = notice
        self.account: TelegramAccount | None = None

    async def connect_and_authorize(
        self, *, interactive: bool = False, qr: bool = False
    ) -> TelegramAccount:
        logger.info("telegram_connecting")
        await self.repository.set_state("connectivity", "RECONNECTING")
        try:
            await self._connect()
            if not await self.client.is_user_authorized():
                await self.repository.set_state("connectivity", "AUTH_REQUIRED")
                if not interactive:
                    raise AuthenticationRequiredError(
                        "Telegram session is not authorized. Run: python -m app auth login"
                    )
                await self._authenticate(qr=qr)

            me = await self.client.get_me()
            if me is None:
                await self.repository.set_state("connectivity", "AUTH_REQUIRED")
                raise AuthenticationRequiredError(
                    "Telegram did not return an authorized user. Run: python -m app auth login"
                )
        except (AuthenticationRequiredError, SessionInUseError, asyncio.CancelledError):
            raise
        except Exception as exc:
            message = authorization_error_message(exc)
            if message is None:
                raise
            logger.error(
                "telegram_authorization_failed",
                extra={
                    "exception_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", None),
                    "error_message": message,
                },
            )
            raise TelegramAuthorizationError(message) from exc

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
        protect_session_storage(self.settings.tg_session_path)
        summary = account_summary(me, self.settings.telegram_phone)
        logger.info("Telegram authorization successful", extra=summary)
        return account

    async def _connect(self) -> None:
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
        finally:
            protect_session_storage(self.settings.tg_session_path)

    async def _authenticate(self, *, qr: bool = False) -> None:
        logger.warning("auth_required")
        if qr:
            await self._authenticate_qr()
        else:
            await self._authenticate_phone()
        if not await self.client.is_user_authorized():
            raise AuthenticationRequiredError("Telegram authentication did not complete.")

    async def _authenticate_phone(self) -> None:
        phone = self.settings.require_phone()
        self.notice(
            "Telegram may deliver the code in an existing Telegram app, by email, or through "
            "another Telegram-supported mechanism."
        )
        await self.client.send_code_request(phone)
        while True:
            code = (await self.code_prompt("Telegram login code: ")).strip()
            try:
                await self.client.sign_in(phone=phone, code=code)
                return
            except errors.PhoneCodeInvalidError:
                self.notice("Incorrect Telegram login code. Try entering the code again.")
            except errors.SessionPasswordNeededError:
                await self._sign_in_with_password()
                return

    async def _authenticate_qr(self) -> None:
        qr_login = await self.client.qr_login()
        self.notice("Open Telegram on an authorized device and scan this login URL:")
        self.notice(str(qr_login.url))
        try:
            await qr_login.wait()
        except errors.SessionPasswordNeededError:
            await self._sign_in_with_password()

    async def _sign_in_with_password(self) -> None:
        password = await self.two_factor_prompt("Telegram 2FA password: ")
        await self.client.sign_in(password=password)

    async def disconnect(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()
        protect_session_storage(self.settings.tg_session_path)
        await self.repository.set_state("connectivity", "DISCONNECTED")
        logger.info("telegram_disconnected")
