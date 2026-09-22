from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config import Settings, mask_phone, normalize_phone
from app.telegram.client import create_client


def configured(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


def test_custom_credential_selection() -> None:
    credentials = configured(
        TELEGRAM_API_MODE="custom",
        TELEGRAM_API_ID=11,
        TELEGRAM_API_HASH="custom-secret",
    ).resolve_api_credentials()
    assert (credentials.profile, credentials.api_id, credentials.api_hash) == (
        "custom",
        11,
        "custom-secret",
    )


def test_public_credential_selection() -> None:
    credentials = configured(
        TELEGRAM_API_MODE="public",
        TELEGRAM_PUBLIC_API_ID=22,
        TELEGRAM_PUBLIC_API_HASH="public-secret",
    ).resolve_api_credentials()
    assert (credentials.profile, credentials.api_id, credentials.api_hash) == (
        "public",
        22,
        "public-secret",
    )


def test_auto_prefers_custom() -> None:
    credentials = configured(
        TELEGRAM_API_MODE="auto",
        TELEGRAM_API_ID=11,
        TELEGRAM_API_HASH="custom-secret",
        TELEGRAM_PUBLIC_API_ID=22,
        TELEGRAM_PUBLIC_API_HASH="public-secret",
    ).resolve_api_credentials()
    assert credentials.profile == "custom"


def test_auto_falls_back_to_public() -> None:
    credentials = configured(
        TELEGRAM_API_MODE="auto",
        TELEGRAM_PUBLIC_API_ID=22,
        TELEGRAM_PUBLIC_API_HASH="public-secret",
    ).resolve_api_credentials()
    assert credentials.profile == "public"


def test_blank_optional_api_ids_from_env_are_treated_as_unset(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_API_MODE=auto\n"
        "TELEGRAM_API_ID=\n"
        "TELEGRAM_API_HASH=\n"
        "TELEGRAM_PUBLIC_API_ID=\n"
        "TELEGRAM_PUBLIC_API_HASH=\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.telegram_api_id is None
    assert settings.telegram_public_api_id is None
    with pytest.raises(ValueError, match="auto fallback"):
        settings.resolve_api_credentials()


@pytest.mark.parametrize(
    "values",
    [
        {"TELEGRAM_API_ID": 11},
        {"TELEGRAM_API_HASH": "orphan-hash"},
    ],
)
def test_partial_custom_pair_is_rejected(values: dict[str, object]) -> None:
    settings = configured(
        TELEGRAM_API_MODE="auto",
        TELEGRAM_PUBLIC_API_ID=22,
        TELEGRAM_PUBLIC_API_HASH="public-secret",
        **values,
    )
    with pytest.raises(ValueError, match="configured together"):
        settings.resolve_api_credentials()


def test_profile_session_paths_are_deterministic_and_separate(tmp_path: Path) -> None:
    settings = configured(TELEGRAM_SESSION_DIR=tmp_path / "sessions")
    assert settings.session_path_for("custom") == tmp_path / "sessions" / "account_custom"
    assert settings.session_path_for("public") == tmp_path / "sessions" / "account_public"
    assert settings.session_path_for("custom") != settings.session_path_for("public")


def test_legacy_custom_session_is_preserved_and_public_is_separate(tmp_path: Path) -> None:
    settings = configured(TG_SESSION_PATH=tmp_path / "sessions" / "main")
    assert settings.session_path_for("custom") == tmp_path / "sessions" / "main"
    assert settings.session_path_for("public") == tmp_path / "sessions" / "main_public"


def test_api_hash_never_appears_in_factory_logs(tmp_path: Path, caplog) -> None:
    secret = "never-log-this-hash"
    settings = configured(
        TELEGRAM_API_MODE="custom",
        TELEGRAM_API_ID=11,
        TELEGRAM_API_HASH=secret,
        TELEGRAM_SESSION_DIR=tmp_path,
    )
    with caplog.at_level(logging.INFO):
        client = create_client(settings)
    assert secret not in caplog.text
    assert "Telegram API profile: custom" in caplog.text
    client.session.close()


def test_phone_is_normalized_and_masked() -> None:
    assert normalize_phone("00 98-912-345-6789") == "+989123456789"
    masked = mask_phone("+989123456789")
    assert masked.startswith("+989")
    assert masked.endswith("789")
    assert "123456" not in masked
