from types import SimpleNamespace

import pytest

from app.commands.saved_messages import (
    SavedCommandKind,
    is_authorized_saved_message,
    parse_saved_command,
)


def test_parses_send_command_body() -> None:
    command = parse_saved_command(".send work\nDeployment complete.")
    assert command is not None
    assert command.kind == SavedCommandKind.SEND
    assert command.argument == "work"
    assert command.text == "Deployment complete."


def test_parses_schedule_with_local_datetime() -> None:
    command = parse_saved_command(".schedule work 2026-09-16 14:30\nReminder")
    assert command is not None
    assert command.kind == SavedCommandKind.SCHEDULE
    assert command.schedule == "2026-09-16 14:30"


@pytest.mark.parametrize(
    "event",
    [
        SimpleNamespace(chat_id=9, sender_id=8, out=True, message=SimpleNamespace(fwd_from=None)),
        SimpleNamespace(chat_id=8, sender_id=8, out=False, message=SimpleNamespace(fwd_from=None)),
        SimpleNamespace(
            chat_id=8, sender_id=8, out=True, message=SimpleNamespace(fwd_from=object())
        ),
    ],
)
def test_rejects_unauthorized_control_events(event: object) -> None:
    assert is_authorized_saved_message(event, 8) is False


def test_accepts_own_non_forwarded_saved_message() -> None:
    event = SimpleNamespace(
        chat_id=8, sender_id=8, out=True, message=SimpleNamespace(fwd_from=None)
    )
    assert is_authorized_saved_message(event, 8) is True


def test_invalid_command_has_clear_error() -> None:
    with pytest.raises(ValueError, match="Send .help"):
        parse_saved_command(".send work")
