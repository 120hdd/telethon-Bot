from telethon import errors

from app.telegram.errors import ErrorCategory, classify_telegram_error


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
