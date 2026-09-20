from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from telethon import errors


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
    "UnauthorizedError",
    "AuthKeyError",
    "AuthKeyUnregisteredError",
    "SessionRevokedError",
    "UserDeactivatedError",
}


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
