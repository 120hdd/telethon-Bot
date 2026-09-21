from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from telethon import errors


class TelegramAuthorizationError(RuntimeError):
    pass


class ErrorCategory(StrEnum):
    RETRYABLE_NETWORK = "RETRYABLE_NETWORK"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_ERROR = "AUTH_ERROR"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    DESTINATION_ERROR = "DESTINATION_ERROR"
    CONTENT_ERROR = "CONTENT_ERROR"
    UNKNOWN_RPC_ERROR = "UNKNOWN_RPC_ERROR"


@dataclass(slots=True)
class ClassifiedTelegramError(Exception):
    category: ErrorCategory
    error_type: str
    safe_message: str
    wait_seconds: int | None = None
    retryable: bool = False

    def __str__(self) -> str:
        return self.safe_message


PERMISSION_ERRORS = {
    "ChatWriteForbiddenError",
    "ChatAdminRequiredError",
    "UserBannedInChannelError",
    "UserNotParticipantError",
}
DESTINATION_ERRORS = {
    "ChannelInvalidError",
    "ChannelPrivateError",
    "PeerIdInvalidError",
    "ChatIdInvalidError",
}
CONTENT_ERRORS = {
    "MessageTooLongError",
    "EntityBoundsInvalidError",
    "MediaInvalidError",
    "MediaEmptyError",
    "FilePartsInvalidError",
}
AUTH_ERRORS = {
    "AuthKeyNotFound",
    "UnauthorizedError",
    "AuthKeyError",
    "AuthKeyUnregisteredError",
    "SessionRevokedError",
    "UserDeactivatedError",
}


AUTHORIZATION_MESSAGES = {
    "ApiIdInvalidError": "Configured Telegram API credentials are invalid.",
    "ApiIdPublishedFloodError": (
        "The selected public/shared Telegram API ID has been server-side limited by Telegram. "
        "Do not retry automatically. Configure another valid API credential profile."
    ),
    "PhoneNumberInvalidError": "Telegram rejected the configured phone number.",
    "PhoneNumberBannedError": "The Telegram account/phone number is banned.",
    "PhoneNumberFloodError": "Too many login-code requests. Stop and wait before retrying.",
    "PhonePasswordFloodError": "Too many password/login attempts. Stop and wait.",
    "PhoneCodeInvalidError": "Incorrect Telegram login code.",
    "PhoneCodeExpiredError": (
        "The Telegram login code expired. Explicitly request a new login attempt."
    ),
    "SessionPasswordNeededError": "Telegram 2FA password is required.",
    "AuthRestartError": "Telegram requested that the explicit login flow be restarted.",
    "UpdateAppToLoginError": (
        "Telegram rejected this login flow because the client library must be updated."
    ),
    "AuthKeyNotFound": (
        "The session authorization key is no longer recognized. The session was not deleted. "
        "Run the explicit auth reset command, then log in again."
    ),
    "UnauthorizedError": (
        "The Telegram session is unauthorized. The session was not deleted; log in explicitly."
    ),
}


def authorization_error_message(exc: BaseException) -> str | None:
    error_type = type(exc).__name__
    if error_type in AUTHORIZATION_MESSAGES:
        return AUTHORIZATION_MESSAGES[error_type]
    if isinstance(exc, errors.FloodWaitError):
        return (
            f"Telegram requires a {exc.seconds}-second wait. Do not retry until that wait expires."
        )
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return "Unable to connect to Telegram due to a network error. Retry later."
    if isinstance(exc, errors.RPCError):
        code = getattr(exc, "code", "unknown")
        message = " ".join(str(exc).split())[:300]
        return f"Unexpected Telegram RPC error ({error_type}, code={code}): {message}"
    return None


def classify_telegram_error(exc: BaseException) -> ClassifiedTelegramError:
    error_type = type(exc).__name__
    if isinstance(exc, errors.SlowModeWaitError):
        return ClassifiedTelegramError(
            ErrorCategory.RATE_LIMITED,
            error_type,
            f"Telegram slow mode requires a {exc.seconds}-second wait.",
            wait_seconds=exc.seconds,
            retryable=True,
        )
    if isinstance(exc, errors.FloodWaitError):
        return ClassifiedTelegramError(
            ErrorCategory.RATE_LIMITED,
            error_type,
            f"Telegram requires a {exc.seconds}-second wait.",
            wait_seconds=exc.seconds,
            retryable=True,
        )
    if error_type in AUTH_ERRORS:
        return ClassifiedTelegramError(
            ErrorCategory.AUTH_ERROR,
            error_type,
            "Telegram authorization is no longer valid; operator login is required.",
        )
    if error_type in PERMISSION_ERRORS:
        return ClassifiedTelegramError(
            ErrorCategory.PERMISSION_ERROR,
            error_type,
            "The account does not have permission to post in this destination.",
        )
    if error_type in DESTINATION_ERRORS:
        return ClassifiedTelegramError(
            ErrorCategory.DESTINATION_ERROR,
            error_type,
            "The Telegram destination is unavailable or no longer accessible.",
        )
    if error_type in CONTENT_ERRORS:
        return ClassifiedTelegramError(
            ErrorCategory.CONTENT_ERROR,
            error_type,
            "Telegram rejected the message or media content.",
        )
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return ClassifiedTelegramError(
            ErrorCategory.RETRYABLE_NETWORK,
            error_type,
            "A temporary network error interrupted the Telegram request.",
            retryable=True,
        )
    if isinstance(exc, errors.RPCError):
        code = getattr(exc, "code", "unknown")
        request = type(getattr(exc, "request", None)).__name__
        return ClassifiedTelegramError(
            ErrorCategory.UNKNOWN_RPC_ERROR,
            error_type,
            f"Unexpected Telegram RPC error (code={code}, request={request}).",
        )
    return ClassifiedTelegramError(
        ErrorCategory.UNKNOWN_RPC_ERROR,
        error_type,
        "Unexpected error while communicating with Telegram.",
    )
