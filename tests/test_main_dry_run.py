from __future__ import annotations

from pathlib import Path

import app.main as main_module
from app.config import Settings
from app.models import TelegramAccount


class NonMutatingClient:
    async def send_message(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("dry-run attempted send_message")

    async def send_file(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("dry-run attempted send_file")


class FakeConnection:
    def __init__(self, client, settings, repository) -> None:
        self.client = client

    async def connect_and_authorize(self, *, interactive: bool = False) -> TelegramAccount:
        assert interactive is False
        return TelegramAccount(77, "owner", "Owner", "2026-01-01T00:00:00+00:00")

    async def disconnect(self) -> None:
        return None


async def test_offline_application_dry_run_starts_no_sender_or_worker(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings(
        _env_file=None,
        TELEGRAM_API_MODE="custom",
        TELEGRAM_API_ID=11,
        TELEGRAM_API_HASH="test-secret",
        TELEGRAM_PHONE="+10000000000",
        TELEGRAM_SESSION_DIR=tmp_path / "sessions",
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        UPLOADS_DIR=tmp_path / "uploads",
        LOG_PATH=tmp_path / "logs" / "app.log",
        LOCAL_TIMEZONE="UTC",
        DRY_RUN=True,
    )
    client = NonMutatingClient()
    monkeypatch.setattr(main_module, "create_client", lambda _: client)
    monkeypatch.setattr(main_module, "TelegramConnection", FakeConnection)

    async def fake_refresh(client_arg, repository) -> int:
        assert client_arg is client
        return 3

    monkeypatch.setattr(main_module, "refresh_dialogs", fake_refresh)

    await main_module.run_application(settings)
