from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class JobStatus(StrEnum):
    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    PROCESSING = "PROCESSING"
    WAITING_RATE_LIMIT = "WAITING_RATE_LIMIT"
    RETRY = "RETRY"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DRY_RUN = "DRY_RUN"


ACTIVE_JOB_STATUSES = {
    JobStatus.PENDING,
    JobStatus.SCHEDULED,
    JobStatus.PROCESSING,
    JobStatus.WAITING_RATE_LIMIT,
    JobStatus.RETRY,
    JobStatus.SENT,
}


ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.PROCESSING, JobStatus.CANCELLED},
    JobStatus.SCHEDULED: {JobStatus.PENDING, JobStatus.CANCELLED},
    JobStatus.PROCESSING: {
        JobStatus.SENT,
        JobStatus.RETRY,
        JobStatus.WAITING_RATE_LIMIT,
        JobStatus.FAILED,
        JobStatus.REVIEW_REQUIRED,
    },
    JobStatus.WAITING_RATE_LIMIT: {JobStatus.PROCESSING, JobStatus.CANCELLED},
    JobStatus.RETRY: {JobStatus.PROCESSING, JobStatus.CANCELLED},
    JobStatus.FAILED: {JobStatus.RETRY, JobStatus.CANCELLED},
    JobStatus.REVIEW_REQUIRED: {JobStatus.RETRY, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.SENT: set(),
    JobStatus.CANCELLED: set(),
    JobStatus.DRY_RUN: set(),
}


class InvalidStateTransition(ValueError):
    pass


def validate_transition(current: JobStatus, target: JobStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransition(f"Illegal job transition: {current} -> {target}")


@dataclass(slots=True, frozen=True)
class Destination:
    telegram_chat_id: int
    title: str
    username: str | None
    alias: str | None
    chat_type: str
    enabled: bool
    can_send: bool
    last_seen_at: str


@dataclass(slots=True, frozen=True)
class NewDestination:
    telegram_chat_id: int
    title: str
    username: str | None
    chat_type: str
    can_send: bool


@dataclass(slots=True, frozen=True)
class NewJob:
    uuid: str
    destination_chat_id: int
    text: str | None
    media_path: Path | None
    parse_mode: str | None
    disable_link_preview: bool
    scheduled_at: str
    status: JobStatus
    max_attempts: int
    idempotency_key: str
    requested_by: str


@dataclass(slots=True, frozen=True)
class MessageJob:
    id: int
    uuid: str
    destination_chat_id: int
    text: str | None
    media_path: Path | None
    parse_mode: str | None
    disable_link_preview: bool
    scheduled_at: str
    next_attempt_at: str | None
    status: JobStatus
    attempt_count: int
    max_attempts: int
    telegram_message_id: int | None
    created_at: str
    updated_at: str
    started_at: str | None
    sent_at: str | None
    last_error_type: str | None
    last_error_message: str | None
    idempotency_key: str
    requested_by: str


@dataclass(slots=True, frozen=True)
class TelegramAccount:
    telegram_user_id: int
    username: str | None
    display_name: str
    last_successful_connection_at: str
