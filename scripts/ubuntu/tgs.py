#!/usr/bin/env python3
"""System management CLI for the Ubuntu SajadBot installation."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

APP_USER = "sajadbot"
SERVICE = "sajadbot"
APP_DIR = Path("/opt/sajadbot")
STATE_DIR = Path("/var/lib/sajadbot")
CONFIG_FILE = Path("/etc/sajadbot/sajadbot.env")
SERVICE_FILE = Path("/etc/systemd/system/sajadbot.service")
APP_CLI = Path("/usr/local/bin/sajadbot")
TGS_CLI = Path("/usr/local/bin/tgs")
MINIMUM_PYTHON = (3, 12)


class CommandError(RuntimeError):
    pass


def run(command: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        text=True,
        capture_output=capture,
    )


def require_root() -> None:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        raise CommandError("This command must run as root. Prefix it with sudo.")


def is_source_root(path: Path) -> bool:
    return all(
        candidate.exists()
        for candidate in (
            path / "pyproject.toml",
            path / "app",
            path / "scripts" / "ubuntu" / "install.sh",
            path / "deploy" / "ubuntu" / "sajadbot.service",
        )
    )


def resolve_source(value: str | None) -> Path:
    candidates: list[Path] = []
    if value:
        candidates.append(Path(value))
    candidates.append(Path.cwd())
    script_repo = Path(__file__).resolve().parents[2]
    candidates.append(script_repo)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if is_source_root(resolved):
            return resolved
    raise CommandError(
        "Could not find a SajadBot source checkout. Run from the repository or pass SOURCE."
    )


def service_is_active() -> bool:
    if shutil.which("systemctl") is None:
        return False
    result = run(("systemctl", "is-active", "--quiet", SERVICE))
    return result.returncode == 0


def run_installer(source: Path) -> int:
    installer = source / "scripts" / "ubuntu" / "install.sh"
    result = run(("bash", str(installer)))
    return result.returncode


def command_install(args: argparse.Namespace) -> int:
    require_root()
    source = resolve_source(args.source)
    print(f"Installing SajadBot from {source}")
    return run_installer(source)


def command_update(args: argparse.Namespace) -> int:
    require_root()
    if not APP_DIR.is_dir():
        raise CommandError("SajadBot is not installed. Run: sudo tgs install SOURCE")
    source = resolve_source(args.source)
    was_active = service_is_active()
    if was_active:
        print("Stopping the active sajadbot service for a safe update")
        stopped = run(("systemctl", "stop", SERVICE))
        if stopped.returncode != 0:
            return stopped.returncode
    print(f"Updating SajadBot from {source}")
    result = run_installer(source)
    if result != 0:
        if was_active:
            print("Update failed; the service remains stopped.", file=sys.stderr)
        return result
    if was_active and not args.no_restart:
        print("Starting the updated sajadbot service")
        return run(("systemctl", "start", SERVICE)).returncode
    if was_active:
        print("Service was left stopped (--no-restart).")
    else:
        print("Service was inactive and remains inactive.")
    return 0


def read_environment_keys(path: Path) -> dict[str, bool]:
    result: dict[str, bool] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        result[key.strip()] = bool(value.strip())
    return result


def check_line(label: str, ok: bool, detail: str) -> bool:
    state = "OK" if ok else "ERROR"
    print(f"[{state}] {label}: {detail}")
    return ok


def source_check(source: str | None) -> bool:
    if source is None:
        return True
    path = Path(source).expanduser().resolve()
    return check_line("source tree", is_source_root(path), str(path))


def local_checks(source: str | None) -> bool:
    checks: list[bool] = []
    checks.append(
        check_line(
            "Python",
            sys.version_info >= MINIMUM_PYTHON,
            platform.python_version(),
        )
    )
    os_release = Path("/etc/os-release")
    ubuntu = False
    if os_release.is_file():
        try:
            ubuntu = any(
                line.strip() == "ID=ubuntu" for line in os_release.read_text().splitlines()
            )
        except OSError:
            ubuntu = False
    checks.append(check_line("Ubuntu", ubuntu, "required deployment platform"))
    checks.append(check_line("application", APP_DIR.is_dir(), str(APP_DIR)))
    checks.append(check_line("service unit", SERVICE_FILE.is_file(), str(SERVICE_FILE)))
    checks.append(check_line("application CLI", os.access(APP_CLI, os.X_OK), str(APP_CLI)))
    checks.append(check_line("tgs CLI", os.access(TGS_CLI, os.X_OK), str(TGS_CLI)))
    checks.append(check_line("configuration", CONFIG_FILE.is_file(), str(CONFIG_FILE)))

    environment = read_environment_keys(CONFIG_FILE)
    phone_ok = environment.get("TELEGRAM_PHONE", False) or environment.get("TG_PHONE", False)
    custom_ok = (
        environment.get("TELEGRAM_API_ID", False) and environment.get("TELEGRAM_API_HASH", False)
    ) or (environment.get("TG_API_ID", False) and environment.get("TG_API_HASH", False))
    public_ok = environment.get("TELEGRAM_PUBLIC_API_ID", False) and environment.get(
        "TELEGRAM_PUBLIC_API_HASH", False
    )
    checks.append(check_line("Telegram phone", phone_ok, "configured without displaying it"))
    checks.append(
        check_line(
            "Telegram API profile",
            bool(custom_ok or public_ok),
            "at least one complete credential pair is present",
        )
    )

    session_dir = STATE_DIR / "sessions"
    checks.append(
        check_line(
            "session directory",
            session_dir.is_dir() and os.access(session_dir, os.W_OK),
            str(session_dir),
        )
    )
    if session_dir.exists():
        mode = stat.S_IMODE(session_dir.stat().st_mode)
        checks.append(
            check_line(
                "session permissions",
                mode & 0o077 == 0,
                f"mode {mode:04o}; owner-only access is required",
            )
        )
    checks.append(source_check(source))
    return all(checks)


def run_as_app_user(arguments: Sequence[str]) -> int:
    if not APP_CLI.is_file():
        raise CommandError("The application CLI is missing. Run: sudo tgs install SOURCE")
    command = [str(APP_CLI), *arguments]
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        if shutil.which("runuser") is None:
            raise CommandError("runuser is required to execute commands as the sajadbot user.")
        command = ["runuser", "-u", APP_USER, "--", *command]
    return run(command).returncode


def with_session_access(stop_service: bool, operation: Callable[[], int]) -> int:
    was_active = service_is_active()
    if was_active and not stop_service:
        raise CommandError(
            "The service is using the Telegram session. Re-run with --stop-service, "
            "or stop it first with: sudo tgs stop"
        )
    if was_active:
        require_root()
        stopped = run(("systemctl", "stop", SERVICE))
        if stopped.returncode != 0:
            return stopped.returncode
    operation_result = 1
    try:
        operation_result = operation()
    finally:
        if was_active:
            print("Restarting the sajadbot service")
            restart_result = run(("systemctl", "start", SERVICE)).returncode
            if operation_result == 0:
                operation_result = restart_result
    return operation_result


def command_check(args: argparse.Namespace) -> int:
    require_root()
    local_ok = local_checks(args.source)
    if not args.online:
        print("Online Telegram checks skipped. Use --online to include them.")
        return 0 if local_ok else 1
    online_result = with_session_access(
        args.stop_service,
        lambda: run_as_app_user(("doctor",)),
    )
    return 0 if local_ok and online_result == 0 else 1


def command_service(args: argparse.Namespace) -> int:
    if args.action != "status":
        require_root()
    command = ["systemctl"]
    if args.action == "status":
        command.extend(("--no-pager", "--full", "status", SERVICE))
    elif args.action in {"enable", "disable"}:
        command.append(args.action)
        if args.now:
            command.append("--now")
        command.append(SERVICE)
    else:
        command.extend((args.action, SERVICE))
    return run(command).returncode


def command_logs(args: argparse.Namespace) -> int:
    require_root()
    command = ["journalctl", "-u", SERVICE, "-n", str(args.lines), "--no-pager"]
    if args.follow:
        command.append("--follow")
    return run(command).returncode


def command_status(_: argparse.Namespace) -> int:
    require_root()
    service_result = run(("systemctl", "is-active", SERVICE), capture=True)
    service_state = (service_result.stdout or service_result.stderr).strip() or "unknown"
    print(f"Service: {service_state}")
    if APP_CLI.is_file():
        return run_as_app_user(("status",))
    return 1


def command_doctor(args: argparse.Namespace) -> int:
    require_root()
    return with_session_access(args.stop_service, lambda: run_as_app_user(("doctor",)))


def command_auth(args: argparse.Namespace) -> int:
    require_root()
    delegated = ["auth", args.auth_action]
    if args.auth_action == "login" and args.qr:
        delegated.append("--qr")
    if args.auth_action == "reset" and args.yes:
        delegated.append("--yes")
    return with_session_access(
        args.stop_service,
        lambda: run_as_app_user(tuple(delegated)),
    )


def command_send_all(args: argparse.Namespace) -> int:
    return run_as_app_user(("sendall", "--text", args.text))


def command_send_multi(args: argparse.Namespace) -> int:
    delegated = ["sendmulti"]
    for group in args.groups:
        delegated.extend(("--group", group))
    delegated.extend(("--text", args.text))
    return run_as_app_user(tuple(delegated))


def command_send_set(args: argparse.Namespace) -> int:
    return run_as_app_user(("sendset", args.name, "--text", args.text))


def command_batch(args: argparse.Namespace) -> int:
    return run_as_app_user(("batch", args.batch_id))


def command_group_set(args: argparse.Namespace) -> int:
    delegated = ["groupset", args.group_set_action]
    if args.group_set_action != "list":
        delegated.append(args.name)
    if args.group_set_action in {"add", "remove"}:
        for group in args.groups:
            delegated.extend(("--group", group))
    return run_as_app_user(tuple(delegated))


def command_version(_: argparse.Namespace) -> int:
    pyproject = APP_DIR / "pyproject.toml"
    if not pyproject.is_file():
        raise CommandError("SajadBot is not installed.")
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        app_version = data["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise CommandError(f"Could not read the installed version: {type(exc).__name__}") from exc
    print(f"tgs 1.0\nSajadBot {app_version}")
    return 0


def add_stop_service_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stop-service",
        action="store_true",
        help="temporarily stop an active service and restart it afterward",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgs",
        description="Install, update, inspect, and operate the Ubuntu SajadBot service.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    install = subcommands.add_parser("install", help="install SajadBot from a source checkout")
    install.add_argument("source", nargs="?", help="repository path; defaults to the current tree")
    install.set_defaults(handler=command_install)

    update = subcommands.add_parser(
        "update", help="update from a source checkout while preserving config and state"
    )
    update.add_argument("source", nargs="?", help="updated repository path")
    update.add_argument(
        "--no-restart",
        action="store_true",
        help="leave the service stopped when it was active before the update",
    )
    update.set_defaults(handler=command_update)

    check = subcommands.add_parser("check", help="check installation and configuration safely")
    check.add_argument("--source", help="also validate this source checkout")
    check.add_argument(
        "--online",
        action="store_true",
        help="also run the application's non-mutating Telegram doctor",
    )
    add_stop_service_option(check)
    check.set_defaults(handler=command_check)

    status = subcommands.add_parser("status", help="show service and application queue status")
    status.set_defaults(handler=command_status)

    for action in ("start", "stop", "restart"):
        service_parser = subcommands.add_parser(action, help=f"{action} the sajadbot service")
        service_parser.set_defaults(handler=command_service, action=action, now=False)

    for action in ("enable", "disable"):
        service_parser = subcommands.add_parser(action, help=f"{action} service autostart")
        service_parser.add_argument(
            "--now", action="store_true", help=f"also {action} the running service now"
        )
        service_parser.set_defaults(handler=command_service, action=action)

    logs = subcommands.add_parser("logs", help="show systemd logs")
    logs.add_argument("-n", "--lines", type=int, default=100, help="number of lines (default: 100)")
    logs.add_argument("-f", "--follow", action="store_true", help="follow new log records")
    logs.set_defaults(handler=command_logs)

    doctor = subcommands.add_parser("doctor", help="run non-mutating application diagnostics")
    add_stop_service_option(doctor)
    doctor.set_defaults(handler=command_doctor)

    auth = subcommands.add_parser("auth", help="manage Telegram user authorization")
    auth_commands = auth.add_subparsers(dest="auth_action", required=True, metavar="ACTION")
    auth_login = auth_commands.add_parser("login", help="perform explicit interactive login")
    auth_login.add_argument("--qr", action="store_true", help="use Telegram QR login")
    add_stop_service_option(auth_login)
    auth_login.set_defaults(handler=command_auth)
    auth_status = auth_commands.add_parser("status", help="inspect authorization without a code")
    add_stop_service_option(auth_status)
    auth_status.set_defaults(handler=command_auth, qr=False, yes=False)
    auth_reset = auth_commands.add_parser("reset", help="explicitly delete the selected session")
    auth_reset.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    add_stop_service_option(auth_reset)
    auth_reset.set_defaults(handler=command_auth, qr=False)

    send_all = subcommands.add_parser(
        "sendall", help="queue a message for every currently allowed group"
    )
    send_all.add_argument("-t", "--text", required=True, help="message text")
    send_all.set_defaults(handler=command_send_all)

    send_multi = subcommands.add_parser(
        "sendmulti", help="atomically queue a message for selected allowed groups"
    )
    send_multi.add_argument(
        "-g",
        "--group",
        dest="groups",
        action="append",
        required=True,
        help="alias or peer ID; repeat this option or use comma-separated values",
    )
    send_multi.add_argument("-t", "--text", required=True, help="message text")
    send_multi.set_defaults(handler=command_send_multi)

    send_set = subcommands.add_parser(
        "sendset", help="queue a message for eligible members of a Group Set"
    )
    send_set.add_argument("name", help="Group Set name")
    send_set.add_argument("-t", "--text", required=True, help="message text")
    send_set.set_defaults(handler=command_send_set)

    batch = subcommands.add_parser("batch", help="show aggregate status for a bulk batch")
    batch.add_argument("batch_id", help="batch UUID")
    batch.set_defaults(handler=command_batch)

    group_set = subcommands.add_parser("groupset", help="manage persistent Group Sets")
    group_set_commands = group_set.add_subparsers(
        dest="group_set_action", required=True, metavar="ACTION"
    )
    for action in ("create", "show", "delete"):
        action_parser = group_set_commands.add_parser(action, help=f"{action} a Group Set")
        action_parser.add_argument("name", help="Group Set name")
        action_parser.set_defaults(handler=command_group_set, groups=[])
    for action in ("add", "remove"):
        action_parser = group_set_commands.add_parser(action, help=f"{action} Group Set members")
        action_parser.add_argument("name", help="Group Set name")
        action_parser.add_argument(
            "-g",
            "--group",
            dest="groups",
            action="append",
            required=True,
            help="alias or peer ID; repeat this option or use comma-separated values",
        )
        action_parser.set_defaults(handler=command_group_set)
    group_set_list = group_set_commands.add_parser("list", help="list Group Sets")
    group_set_list.set_defaults(handler=command_group_set, name=None, groups=[])

    version_parser = subcommands.add_parser("version", help="show installed versions")
    version_parser.set_defaults(handler=command_version)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except CommandError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
