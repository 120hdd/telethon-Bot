from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class TelegramApiCredentials:
    profile: str
    api_id: int
    api_hash: str


def normalize_phone(value: str) -> str:
    normalized = re.sub(r"[\s().-]", "", value.strip())
    if normalized.startswith("00"):
        normalized = f"+{normalized[2:]}"
    if not re.fullmatch(r"\+[1-9]\d{6,14}", normalized):
        raise ValueError("TELEGRAM_PHONE must use international format, for example +10000000000")
    return normalized


def mask_phone(value: str | None) -> str:
    if not value:
        return "unavailable"
    try:
        normalized = normalize_phone(value)
    except ValueError:
        return "[invalid]"
    visible_prefix = min(4, max(2, len(normalized) - 5))
    hidden = max(3, len(normalized) - visible_prefix - 3)
    return f"{normalized[:visible_prefix]}{'*' * hidden}{normalized[-3:]}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    telegram_api_mode: str = Field(default="auto", validation_alias="TELEGRAM_API_MODE")
    telegram_api_id: int | None = Field(
        default=None, validation_alias=AliasChoices("TELEGRAM_API_ID", "TG_API_ID")
    )
    telegram_api_hash: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("TELEGRAM_API_HASH", "TG_API_HASH")
    )
    telegram_public_api_id: int | None = Field(
        default=None, validation_alias="TELEGRAM_PUBLIC_API_ID"
    )
    telegram_public_api_hash: SecretStr | None = Field(
        default=None, validation_alias="TELEGRAM_PUBLIC_API_HASH"
    )
    telegram_phone: str | None = Field(
        default=None, validation_alias=AliasChoices("TELEGRAM_PHONE", "TG_PHONE")
    )
    telegram_session_dir: Path | None = Field(default=None, validation_alias="TELEGRAM_SESSION_DIR")
    legacy_session_path: Path | None = Field(default=None, validation_alias="TG_SESSION_PATH")

    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/app.db", validation_alias="DATABASE_URL"
    )
    uploads_dir: Path = Field(default=Path("./data/uploads"), validation_alias="UPLOADS_DIR")
    log_path: Path = Field(default=Path("./logs/app.log"), validation_alias="LOG_PATH")

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_format: str = Field(default="console", validation_alias="LOG_FORMAT")
    log_message_bodies: bool = Field(default=False, validation_alias="LOG_MESSAGE_BODIES")
    control_saved_messages: bool = Field(default=True, validation_alias="CONTROL_SAVED_MESSAGES")
    dry_run: bool = Field(default=False, validation_alias="DRY_RUN")
    local_timezone: str = Field(default="UTC", validation_alias="LOCAL_TIMEZONE")

    default_send_interval_seconds: float = Field(
        default=8.0, ge=0, validation_alias="DEFAULT_SEND_INTERVAL_SECONDS"
    )
    max_queue_attempts: int = Field(default=5, ge=1, validation_alias="MAX_QUEUE_ATTEMPTS")
    max_automatic_flood_wait_seconds: int = Field(
        default=300, ge=0, validation_alias="MAX_AUTOMATIC_FLOOD_WAIT_SECONDS"
    )
    worker_poll_seconds: float = Field(default=1.0, gt=0, validation_alias="WORKER_POLL_SECONDS")
    max_media_bytes: int = Field(default=2_147_483_648, gt=0, validation_alias="MAX_MEDIA_BYTES")

    @field_validator("telegram_api_mode")
    @classmethod
    def validate_api_mode(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"auto", "custom", "public"}:
            raise ValueError("TELEGRAM_API_MODE must be 'auto', 'custom', or 'public'")
        return normalized

    @field_validator("telegram_phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return normalize_phone(value) if value else None

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"console", "json"}:
            raise ValueError("LOG_FORMAT must be 'console' or 'json'")
        return normalized

    @field_validator("local_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value

    def resolve_api_credentials(self) -> TelegramApiCredentials:
        custom_hash = (
            self.telegram_api_hash.get_secret_value() if self.telegram_api_hash is not None else ""
        )
        public_hash = (
            self.telegram_public_api_hash.get_secret_value()
            if self.telegram_public_api_hash is not None
            else ""
        )
        custom_present = self.telegram_api_id is not None or bool(custom_hash)
        custom_complete = self.telegram_api_id is not None and bool(custom_hash)
        public_present = self.telegram_public_api_id is not None or bool(public_hash)
        public_complete = self.telegram_public_api_id is not None and bool(public_hash)

        if custom_present and not custom_complete:
            raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured together.")
        if public_present and not public_complete:
            raise ValueError(
                "TELEGRAM_PUBLIC_API_ID and TELEGRAM_PUBLIC_API_HASH must be configured together."
            )

        profile = self.telegram_api_mode
        if profile == "auto":
            profile = "custom" if custom_complete else "public"
        if profile == "custom":
            if not custom_complete:
                raise ValueError(
                    "TELEGRAM_API_MODE=custom requires TELEGRAM_API_ID and TELEGRAM_API_HASH."
                )
            assert self.telegram_api_id is not None
            if self.telegram_api_id <= 0:
                raise ValueError("TELEGRAM_API_ID must be a positive integer.")
            return TelegramApiCredentials("custom", self.telegram_api_id, custom_hash)
        if not public_complete:
            raise ValueError(
                "TELEGRAM_API_MODE=public (or auto fallback) requires "
                "TELEGRAM_PUBLIC_API_ID and TELEGRAM_PUBLIC_API_HASH."
            )
        assert self.telegram_public_api_id is not None
        if self.telegram_public_api_id <= 0:
            raise ValueError("TELEGRAM_PUBLIC_API_ID must be a positive integer.")
        return TelegramApiCredentials("public", self.telegram_public_api_id, public_hash)

    def require_phone(self) -> str:
        if not self.telegram_phone:
            raise ValueError("Missing Telegram configuration: TELEGRAM_PHONE")
        return self.telegram_phone

    def require_telegram_credentials(self) -> tuple[int, str, str]:
        """Backward-compatible combined accessor for older integrations."""

        credentials = self.resolve_api_credentials()
        return credentials.api_id, credentials.api_hash, self.require_phone()

    def session_path_for(self, profile: str) -> Path:
        if profile not in {"custom", "public"}:
            raise ValueError(f"Unknown Telegram API profile: {profile}")
        if self.telegram_session_dir is not None:
            return self.telegram_session_dir / f"account_{profile}"
        if self.legacy_session_path is not None:
            if profile == "custom":
                return self.legacy_session_path
            return self.legacy_session_path.with_name(f"{self.legacy_session_path.name}_public")
        return Path("./data/sessions") / f"account_{profile}"

    @property
    def tg_session_path(self) -> Path:
        """Compatibility property used by existing code and third-party integrations."""

        return self.session_path_for(self.resolve_api_credentials().profile)

    @property
    def session_directory(self) -> Path:
        if self.telegram_session_dir is not None:
            return self.telegram_session_dir
        if self.legacy_session_path is not None:
            return self.legacy_session_path.parent
        return Path("./data/sessions")

    @property
    def database_path(self) -> Path:
        prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
        for prefix in prefixes:
            if self.database_url.startswith(prefix):
                raw_path = self.database_url.removeprefix(prefix)
                if raw_path == ":memory:":
                    return Path(":memory:")
                return Path(raw_path)
        raise ValueError("DATABASE_URL must use sqlite+aiosqlite:/// or sqlite:///")

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.local_timezone)

    def ensure_directories(self) -> None:
        directories = {self.session_directory, self.uploads_dir, self.log_path.parent}
        if self.database_path != Path(":memory:"):
            directories.add(self.database_path.parent)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    legacy_keys = {
        key
        for key in ("TG_API_ID", "TG_API_HASH", "TG_PHONE", "TG_SESSION_PATH")
        if os.getenv(key) is not None
    }
    env_path = Path(".env")
    if env_path.is_file():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                key = line.split("=", 1)[0].strip()
                if key in {"TG_API_ID", "TG_API_HASH", "TG_PHONE", "TG_SESSION_PATH"}:
                    legacy_keys.add(key)
        except OSError:
            pass
    if legacy_keys:
        logger.warning(
            "Deprecated Telegram environment names are in use; migrate to TELEGRAM_* names",
            extra={"legacy_keys": ",".join(sorted(legacy_keys))},
        )
    return settings
