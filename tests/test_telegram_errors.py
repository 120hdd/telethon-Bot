from telethon import errors

from app.telegram.errors import (
    ErrorCategory,
    authorization_error_message,
    classify_telegram_error,
)


def test_flood_wait_preserves_requested_delay() -> None:
    classified = classify_telegram_error(errors.FloodWaitError(None, capture=42))
    assert classified.category == ErrorCategory.RATE_LIMITED
    assert classified.wait_seconds == 42
    assert classified.retryable is True


def test_slow_mode_is_rate_limited() -> None:
    classified = classify_telegram_error(errors.SlowModeWaitError(None, capture=9))
    assert classified.category == ErrorCategory.RATE_LIMITED
    assert classified.wait_seconds == 9
    assert classified.error_type == "SlowModeWaitError"


def test_permission_error_is_permanent() -> None:
    classified = classify_telegram_error(errors.ChatWriteForbiddenError(None))
    assert classified.category == ErrorCategory.PERMISSION_ERROR
    assert classified.retryable is False


def test_network_error_is_retryable() -> None:
    classified = classify_telegram_error(TimeoutError("socket timeout"))
    assert classified.category == ErrorCategory.RETRYABLE_NETWORK
    assert classified.retryable is True


def test_api_id_published_flood_is_never_described_as_retryable() -> None:
    message = authorization_error_message(errors.ApiIdPublishedFloodError(None))
    assert message is not None
    assert "Do not retry automatically" in message
    assert "another valid API credential profile" in message


def test_auth_key_not_found_preserves_session_for_explicit_reset() -> None:
    message = authorization_error_message(errors.AuthKeyNotFound())
    assert message is not None
    assert "session was not deleted" in message.lower()
    assert "explicit auth reset" in message.lower()


def test_auth_flood_wait_reports_duration_without_sleeping() -> None:
    message = authorization_error_message(errors.FloodWaitError(None, capture=7200))
    assert message is not None
    assert "7200-second wait" in message
