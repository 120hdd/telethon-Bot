import logging
from types import SimpleNamespace

import pytest

from app.commands.saved_messages import (
    HELP_TEXT,
    SavedCommandKind,
    SavedMessagesController,
    is_authorized_saved_message,
    parse_saved_command,
)
from app.logging_config import ConsoleFormatter, RecentLogHandler


class EditingSender:
    def __init__(self) -> None:
        self.edits: list[tuple[int, str]] = []

    async def edit_control_message(self, message_id: int, text: str) -> int:
        self.edits.append((message_id, text))
        return message_id


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
    with pytest.raises(ValueError, match="Send /help"):
        parse_saved_command(".send work")


def test_slash_help_is_supported() -> None:
    command = parse_saved_command("/help")
    assert command is not None
    assert command.kind == SavedCommandKind.HELP


def test_parses_multiple_groups_with_optional_aliases() -> None:
    command = parse_saved_command(
        "/groups add\n@public_group work\nhttps://t.me/team\n-100123 private_team"
    )
    assert command is not None
    assert command.kind == SavedCommandKind.GROUP_ADD
    assert [(item.reference, item.alias) for item in command.groups] == [
        ("@public_group", "work"),
        ("https://t.me/team", None),
        ("-100123", "private_team"),
    ]


@pytest.mark.parametrize("duration", ["4", "301"])
def test_rejects_out_of_range_log_stream_duration(duration: str) -> None:
    with pytest.raises(ValueError, match="between 5 and 300"):
        parse_saved_command(f"/logs {duration}")


def test_help_documents_every_saved_messages_command() -> None:
    for command in (
        "/status",
        "/groups",
        "/groups refresh",
        "/groups add",
        "/groups remove",
        "/send",
        "/schedule",
        "/queue",
        "/cancel",
        "/logs",
        "/logs stop",
        "/sendmulti",
        "/sendall",
        "/groupset create",
        "/groupset add",
        "/groupset remove",
        "/groupset list",
        "/groupset show",
        "/groupset delete",
        "/sendset",
        "/batch",
    ):
        assert command in HELP_TEXT


@pytest.mark.parametrize(
    ("raw", "kind", "argument", "targets"),
    [
        ("/groupset create Ads", SavedCommandKind.GROUP_SET_CREATE, "Ads", ()),
        (
            ".groupset add ads one,two three",
            SavedCommandKind.GROUP_SET_ADD,
            "ads",
            ("one", "two", "three"),
        ),
        (
            "/groupset remove ads one two",
            SavedCommandKind.GROUP_SET_REMOVE,
            "ads",
            ("one", "two"),
        ),
        ("/groupset list", SavedCommandKind.GROUP_SET_LIST, None, ()),
        ("/groupset show ads", SavedCommandKind.GROUP_SET_SHOW, "ads", ()),
        ("/groupset delete ads", SavedCommandKind.GROUP_SET_DELETE, "ads", ()),
    ],
)
def test_parses_group_set_commands(raw, kind, argument, targets) -> None:
    command = parse_saved_command(raw)
    assert command is not None
    assert command.kind == kind
    assert command.argument == argument
    assert command.targets == targets


async def test_log_stream_edits_the_command_message() -> None:
    logs = RecentLogHandler()
    logs.setFormatter(ConsoleFormatter())
    logs.handle(logging.LogRecord("test", logging.INFO, __file__, 1, "worker_ready", (), None))
    sender = EditingSender()
    controller = SavedMessagesController(
        None,  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        1,
        logs,
    )

    await controller._stream_logs(77, 0)

    assert sender.edits[0][0] == 77
    assert "Logs (finished, 0s)" in sender.edits[0][1]
    assert "worker_ready" in sender.edits[0][1]
