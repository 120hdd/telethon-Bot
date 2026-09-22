from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "ubuntu" / "tgs.py"


def load_tgs() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tgs_test_module", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "arguments",
    [
        ["-h"],
        ["install", "-h"],
        ["update", "-h"],
        ["check", "-h"],
        ["status", "-h"],
        ["start", "-h"],
        ["stop", "-h"],
        ["restart", "-h"],
        ["enable", "-h"],
        ["disable", "-h"],
        ["logs", "-h"],
        ["doctor", "-h"],
        ["auth", "-h"],
        ["auth", "login", "-h"],
        ["auth", "status", "-h"],
        ["auth", "reset", "-h"],
        ["sendall", "-h"],
        ["sendmulti", "-h"],
        ["sendset", "-h"],
        ["batch", "-h"],
        ["groups", "-h"],
        ["groups", "list", "-h"],
        ["groups", "refresh", "-h"],
        ["groups", "allowed", "-h"],
        ["groups", "allow", "-h"],
        ["groups", "deny", "-h"],
        ["groups", "alias", "-h"],
        ["groupset", "-h"],
        ["groupset", "create", "-h"],
        ["groupset", "add", "-h"],
        ["groupset", "remove", "-h"],
        ["groupset", "list", "-h"],
        ["groupset", "show", "-h"],
        ["groupset", "delete", "-h"],
        ["version", "-h"],
    ],
)
def test_tgs_and_every_subcommand_support_short_help(arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    assert "-h" in result.stdout
    assert "--help" in result.stdout


def test_tgs_requires_a_subcommand() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "required" in result.stderr.lower()


def test_update_stops_active_service_before_install_and_starts_afterward(
    tmp_path: Path, monkeypatch
) -> None:
    tgs = load_tgs()
    source = tmp_path / "source"
    source.mkdir()
    installed = tmp_path / "installed"
    installed.mkdir()
    events: list[str] = []

    monkeypatch.setattr(tgs, "APP_DIR", installed)
    monkeypatch.setattr(tgs, "require_root", lambda: None)
    monkeypatch.setattr(tgs, "resolve_source", lambda _: source)
    monkeypatch.setattr(tgs, "service_is_active", lambda: True)

    def fake_run(command, *, capture=False):
        events.append(" ".join(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    def fake_installer(path: Path) -> int:
        events.append(f"install {path}")
        return 0

    monkeypatch.setattr(tgs, "run", fake_run)
    monkeypatch.setattr(tgs, "run_installer", fake_installer)

    result = tgs.command_update(argparse.Namespace(source=None, no_restart=False))

    assert result == 0
    assert events == [
        "systemctl stop sajadbot",
        f"install {source}",
        "systemctl start sajadbot",
    ]


def test_session_command_refuses_to_compete_with_active_service(monkeypatch) -> None:
    tgs = load_tgs()
    monkeypatch.setattr(tgs, "service_is_active", lambda: True)

    with pytest.raises(tgs.CommandError, match="--stop-service"):
        tgs.with_session_access(False, lambda: 0)


def test_bulk_commands_delegate_to_installed_application_cli(monkeypatch) -> None:
    tgs = load_tgs()
    delegated: list[tuple[str, ...]] = []

    def fake_run_as_app_user(arguments) -> int:
        delegated.append(tuple(arguments))
        return 0

    monkeypatch.setattr(tgs, "run_as_app_user", fake_run_as_app_user)

    assert tgs.main(["sendall", "--text", "hello all"]) == 0
    assert tgs.main(["sendmulti", "-g", "one,two", "-g", "-1003", "-t", "hello"]) == 0
    assert tgs.main(["sendset", "vip", "--text", "hello set"]) == 0
    assert tgs.main(["batch", "batch-uuid"]) == 0

    assert delegated == [
        ("sendall", "--text", "hello all"),
        (
            "sendmulti",
            "--group",
            "one,two",
            "--group",
            "-1003",
            "--text",
            "hello",
        ),
        ("sendset", "vip", "--text", "hello set"),
        ("batch", "batch-uuid"),
    ]


def test_group_set_commands_delegate_to_installed_application_cli(monkeypatch) -> None:
    tgs = load_tgs()
    delegated: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        tgs,
        "run_as_app_user",
        lambda arguments: delegated.append(tuple(arguments)) or 0,
    )

    assert tgs.main(["groupset", "create", "ads"]) == 0
    assert tgs.main(["groupset", "add", "ads", "-g", "one,two"]) == 0
    assert tgs.main(["groupset", "remove", "ads", "-g", "two"]) == 0
    assert tgs.main(["groupset", "list"]) == 0
    assert tgs.main(["groupset", "show", "ads"]) == 0
    assert tgs.main(["groupset", "delete", "ads"]) == 0

    assert delegated == [
        ("groupset", "create", "ads"),
        ("groupset", "add", "ads", "--group", "one,two"),
        ("groupset", "remove", "ads", "--group", "two"),
        ("groupset", "list"),
        ("groupset", "show", "ads"),
        ("groupset", "delete", "ads"),
    ]


def test_group_commands_delegate_to_installed_application_cli(monkeypatch) -> None:
    tgs = load_tgs()
    delegated: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        tgs,
        "run_as_app_user",
        lambda arguments: delegated.append(tuple(arguments)) or 0,
    )

    assert tgs.main(["groups", "list"]) == 0
    assert tgs.main(["groups", "allowed"]) == 0
    assert tgs.main(["groups", "allow", "sales"]) == 0
    assert tgs.main(["groups", "deny", "-1003"]) == 0
    assert tgs.main(["groups", "alias", "-1003", "customers"]) == 0

    assert delegated == [
        ("groups", "list"),
        ("groups", "allowed"),
        ("groups", "allow", "sales"),
        ("groups", "deny", "-1003"),
        ("groups", "alias", "-1003", "customers"),
    ]


@pytest.mark.parametrize("arguments, stop_service", [([], False), (["--stop-service"], True)])
def test_groups_refresh_uses_session_access(
    arguments: list[str], stop_service: bool, monkeypatch
) -> None:
    tgs = load_tgs()
    delegated: list[tuple[str, ...]] = []
    stop_service_values: list[bool] = []
    monkeypatch.setattr(
        tgs,
        "run_as_app_user",
        lambda command: delegated.append(tuple(command)) or 0,
    )

    def fake_with_session_access(stop: bool, operation) -> int:
        stop_service_values.append(stop)
        return operation()

    monkeypatch.setattr(tgs, "with_session_access", fake_with_session_access)

    assert tgs.main(["groups", "refresh", *arguments]) == 0
    assert stop_service_values == [stop_service]
    assert delegated == [("groups", "refresh")]
