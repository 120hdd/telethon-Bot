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
