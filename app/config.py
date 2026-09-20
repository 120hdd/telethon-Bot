from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    tg_api_id: int | None = Field(default=None, validation_alias="TG_API_ID")
    tg_api_hash: SecretStr | None = Field(default=None, validation_alias="TG_API_HASH")
    tg_phone: str | None = Field(default=None, validation_alias="TG_PHONE")
    tg_session_path: Path = Field(
        default=Path("./data/sessions/main"), validation_alias="TG_SESSION_PATH"
    )
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

    def require_telegram_credentials(self) -> tuple[int, str, str]:
        missing: list[str] = []
        if self.tg_api_id is None:
            missing.append("TG_API_ID")
        if self.tg_api_hash is None or not self.tg_api_hash.get_secret_value():
            missing.append("TG_API_HASH")
        if not self.tg_phone:
            missing.append("TG_PHONE")
        if missing:
            raise ValueError(f"Missing Telegram configuration: {', '.join(missing)}")
        return self.tg_api_id, self.tg_api_hash.get_secret_value(), self.tg_phone

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
        directories = {
            self.tg_session_path.parent,
            self.uploads_dir,
            self.log_path.parent,
        }
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
    return Settings()
