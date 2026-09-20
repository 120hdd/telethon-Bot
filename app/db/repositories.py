from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import aiosqlite

from app.db.database import Database
from app.models import (
    Destination,
    JobStatus,
    MessageJob,
    NewDestination,
    NewJob,
    TelegramAccount,
    validate_transition,
)
from app.utils.time import to_db_time, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    telegram_user_id INTEGER NOT NULL,
    username TEXT,
    display_name TEXT NOT NULL,
    last_successful_connection_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS destinations (
    telegram_chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    alias TEXT UNIQUE,
    chat_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    can_send INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    destination_chat_id INTEGER NOT NULL REFERENCES destinations(telegram_chat_id),
    text TEXT,
    media_path TEXT,
    parse_mode TEXT,
    disable_link_preview INTEGER NOT NULL DEFAULT 0,
    scheduled_at TEXT NOT NULL,
    next_attempt_at TEXT,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    telegram_message_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    sent_at TEXT,
    last_error_type TEXT,
    last_error_message TEXT,
    idempotency_key TEXT NOT NULL,
    requested_by TEXT NOT NULL DEFAULT 'cli'
);

CREATE INDEX IF NOT EXISTS ix_jobs_status ON message_jobs(status);
CREATE INDEX IF NOT EXISTS ix_jobs_scheduled_at ON message_jobs(scheduled_at);
CREATE INDEX IF NOT EXISTS ix_jobs_next_attempt_at ON message_jobs(next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_jobs_destination ON message_jobs(destination_chat_id);
CREATE INDEX IF NOT EXISTS ix_jobs_idempotency ON message_jobs(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_idempotency
ON message_jobs(idempotency_key)
WHERE status IN ('PENDING', 'SCHEDULED', 'PROCESSING', 'WAITING_RATE_LIMIT', 'RETRY', 'SENT');

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
"""


class DuplicateJobError(ValueError):
    def __init__(self, existing: MessageJob | None = None) -> None:
        super().__init__("An equivalent active or sent job already exists")
        self.existing = existing


class NotFoundError(ValueError):
    pass


class Repository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def initialize(self) -> None:
        async with self.db.transaction(immediate=True) as connection:
            await connection.executescript(SCHEMA)
            await connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (to_db_time(utc_now()),),
            )

    async def set_account(self, account: TelegramAccount) -> None:
        async with self.db.transaction(immediate=True) as connection:
            await connection.execute(
                """
                INSERT INTO account(singleton_id, telegram_user_id, username, display_name,
                                    last_successful_connection_at)
                VALUES(1, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    telegram_user_id=excluded.telegram_user_id,
                    username=excluded.username,
                    display_name=excluded.display_name,
                    last_successful_connection_at=excluded.last_successful_connection_at
                """,
                (
                    account.telegram_user_id,
                    account.username,
                    account.display_name,
                    account.last_successful_connection_at,
                ),
            )

    async def get_account(self) -> TelegramAccount | None:
        row = await self._fetchone("SELECT * FROM account WHERE singleton_id = 1")
        if row is None:
            return None
        return TelegramAccount(
            telegram_user_id=row["telegram_user_id"],
            username=row["username"],
            display_name=row["display_name"],
            last_successful_connection_at=row["last_successful_connection_at"],
        )

    async def upsert_destinations(self, destinations: Iterable[NewDestination]) -> int:
        now = to_db_time(utc_now())
        values = [
            (
                item.telegram_chat_id,
                item.title,
                item.username,
                item.chat_type,
                int(item.can_send),
                now,
                now,
                now,
            )
            for item in destinations
        ]
        if not values:
            return 0
        async with self.db.transaction(immediate=True) as connection:
            await connection.executemany(
                """
                INSERT INTO destinations(
                    telegram_chat_id, title, username, chat_type, can_send,
                    created_at, updated_at, last_seen_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    title=excluded.title,
                    username=excluded.username,
                    chat_type=excluded.chat_type,
                    can_send=excluded.can_send,
                    updated_at=excluded.updated_at,
                    last_seen_at=excluded.last_seen_at
                """,
                values,
            )
        return len(values)

    async def list_destinations(self, *, allowed_only: bool = False) -> list[Destination]:
        sql = "SELECT * FROM destinations"
        if allowed_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY title COLLATE NOCASE, telegram_chat_id"
        return [self._destination(row) for row in await self._fetchall(sql)]

    async def resolve_destination(self, reference: str | int) -> Destination | None:
        if isinstance(reference, int) or str(reference).lstrip("-").isdigit():
            row = await self._fetchone(
                "SELECT * FROM destinations WHERE telegram_chat_id = ?", (int(reference),)
            )
        else:
            row = await self._fetchone(
                "SELECT * FROM destinations WHERE alias = ? COLLATE NOCASE", (str(reference),)
            )
        return self._destination(row) if row else None

    async def set_destination_enabled(self, reference: str | int, enabled: bool) -> Destination:
        destination = await self.resolve_destination(reference)
        if destination is None:
            raise NotFoundError(f"Unknown destination: {reference}")
        now = to_db_time(utc_now())
        async with self.db.transaction(immediate=True) as connection:
            await connection.execute(
                "UPDATE destinations SET enabled = ?, updated_at = ? WHERE telegram_chat_id = ?",
                (int(enabled), now, destination.telegram_chat_id),
            )
            if not enabled:
                await connection.execute(
                    """
                    UPDATE message_jobs
                    SET status = 'CANCELLED', updated_at = ?,
                        last_error_type = 'CANCELLED_DESTINATION_DISABLED',
                        last_error_message = 'Destination was removed from the whitelist'
                    WHERE destination_chat_id = ?
                      AND status IN ('PENDING', 'SCHEDULED', 'RETRY', 'WAITING_RATE_LIMIT')
                    """,
                    (now, destination.telegram_chat_id),
                )
        await self.add_audit(
            "group_allowed" if enabled else "group_denied",
            entity_type="destination",
            entity_id=str(destination.telegram_chat_id),
        )
        refreshed = await self.resolve_destination(destination.telegram_chat_id)
        assert refreshed is not None
        return refreshed

    async def set_destination_alias(self, reference: str | int, alias: str) -> Destination:
        destination = await self.resolve_destination(reference)
        if destination is None:
            raise NotFoundError(f"Unknown destination: {reference}")
        normalized = alias.strip().lower()
        if not normalized or not normalized.replace("_", "").isalnum():
            raise ValueError("Alias must contain only letters, numbers, and underscores")
        try:
            async with self.db.transaction(immediate=True) as connection:
                await connection.execute(
                    "UPDATE destinations SET alias = ?, updated_at = ? WHERE telegram_chat_id = ?",
                    (normalized, to_db_time(utc_now()), destination.telegram_chat_id),
                )
        except aiosqlite.IntegrityError as exc:
            raise ValueError(f"Alias is already in use: {normalized}") from exc
        await self.add_audit(
            "alias_changed",
            entity_type="destination",
            entity_id=str(destination.telegram_chat_id),
            details=normalized,
        )
        refreshed = await self.resolve_destination(destination.telegram_chat_id)
        assert refreshed is not None
        return refreshed

    async def set_destination_can_send(self, chat_id: int, can_send: bool) -> None:
        now = to_db_time(utc_now())
        async with self.db.transaction(immediate=True) as connection:
            await connection.execute(
                "UPDATE destinations SET can_send = ?, updated_at = ? WHERE telegram_chat_id = ?",
                (int(can_send), now, chat_id),
            )
            if not can_send:
                await connection.execute(
                    """
                    UPDATE message_jobs
                    SET status = 'CANCELLED', updated_at = ?,
                        last_error_type = 'CANCELLED_DESTINATION_UNAVAILABLE',
                        last_error_message = 'Destination no longer allows sending'
                    WHERE destination_chat_id = ?
                      AND status IN ('PENDING', 'SCHEDULED', 'RETRY', 'WAITING_RATE_LIMIT')
                    """,
                    (now, chat_id),
                )

    async def create_job(self, job: NewJob, *, actor: str = "cli") -> MessageJob:
        now = to_db_time(utc_now())
        try:
            async with self.db.transaction(immediate=True) as connection:
                cursor = await connection.execute(
                    """
                    INSERT INTO message_jobs(
                        uuid, destination_chat_id, text, media_path, parse_mode,
                        disable_link_preview, scheduled_at, status, max_attempts,
                        created_at, updated_at, idempotency_key, requested_by
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.uuid,
                        job.destination_chat_id,
                        job.text,
                        str(job.media_path) if job.media_path else None,
                        job.parse_mode,
                        int(job.disable_link_preview),
                        job.scheduled_at,
                        job.status.value,
                        job.max_attempts,
                        now,
                        now,
                        job.idempotency_key,
                        job.requested_by,
                    ),
                )
                job_id = cursor.lastrowid
        except aiosqlite.IntegrityError as exc:
            existing = await self.find_duplicate(job.idempotency_key)
            if existing is not None:
                raise DuplicateJobError(existing) from exc
            raise
        assert job_id is not None
        await self.add_audit(
            "dry_run_executed" if job.status == JobStatus.DRY_RUN else "job_created",
            actor=actor,
            entity_type="job",
            entity_id=job.uuid,
        )
        created = await self.get_job(str(job_id))
        assert created is not None
        return created

    async def find_duplicate(self, idempotency_key: str) -> MessageJob | None:
        statuses = tuple(
            status.value
            for status in (
                JobStatus.PENDING,
                JobStatus.SCHEDULED,
                JobStatus.PROCESSING,
                JobStatus.WAITING_RATE_LIMIT,
                JobStatus.RETRY,
                JobStatus.SENT,
            )
        )
        placeholders = ",".join("?" for _ in statuses)
        row = await self._fetchone(
            f"""SELECT * FROM message_jobs
                WHERE idempotency_key = ? AND status IN ({placeholders})
                ORDER BY id DESC LIMIT 1""",
            (idempotency_key, *statuses),
        )
        return self._job(row) if row else None

    async def promote_due_scheduled(self, now: str) -> int:
        async with self.db.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE message_jobs SET status = 'PENDING', updated_at = ?
                WHERE status = 'SCHEDULED' AND scheduled_at <= ?
                """,
                (now, now),
            )
            return cursor.rowcount

    async def cancel_disabled_jobs(self) -> int:
        now = to_db_time(utc_now())
        async with self.db.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE message_jobs
                SET status = 'CANCELLED', updated_at = ?,
                    last_error_type = 'CANCELLED_DESTINATION_DISABLED',
                    last_error_message = 'Destination is not whitelisted'
                WHERE status IN ('PENDING', 'SCHEDULED', 'RETRY', 'WAITING_RATE_LIMIT')
                  AND EXISTS (
                    SELECT 1 FROM destinations d
                    WHERE d.telegram_chat_id = message_jobs.destination_chat_id
                      AND d.enabled = 0
                  )
                """,
                (now,),
            )
            return cursor.rowcount

    async def claim_next_job(self, now: str) -> MessageJob | None:
        async with self.db.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                SELECT j.* FROM message_jobs j
                JOIN destinations d ON d.telegram_chat_id = j.destination_chat_id
                WHERE d.enabled = 1 AND d.can_send = 1
                  AND (
                    j.status = 'PENDING'
                    OR (j.status IN ('RETRY', 'WAITING_RATE_LIMIT')
                        AND j.next_attempt_at IS NOT NULL AND j.next_attempt_at <= ?)
                  )
                ORDER BY COALESCE(j.next_attempt_at, j.scheduled_at), j.id
                LIMIT 1
                """,
                (now,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            current = JobStatus(row["status"])
            validate_transition(current, JobStatus.PROCESSING)
            update = await connection.execute(
                """
                UPDATE message_jobs
                SET status = 'PROCESSING', attempt_count = attempt_count + 1,
                    started_at = ?, updated_at = ?, next_attempt_at = NULL
                WHERE id = ? AND status = ?
                """,
                (now, now, row["id"], current.value),
            )
            if update.rowcount != 1:
                return None
            refreshed = await connection.execute(
                "SELECT * FROM message_jobs WHERE id = ?", (row["id"],)
            )
            claimed = await refreshed.fetchone()
            return self._job(claimed)

    async def transition_job(
        self,
        job_id: int,
        target: JobStatus,
        *,
        telegram_message_id: int | None = None,
        next_attempt_at: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> MessageJob:
        now = to_db_time(utc_now())
        async with self.db.transaction(immediate=True) as connection:
            cursor = await connection.execute("SELECT * FROM message_jobs WHERE id = ?", (job_id,))
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Unknown job: {job_id}")
            validate_transition(JobStatus(row["status"]), target)
            sent_at = now if target == JobStatus.SENT else row["sent_at"]
            await connection.execute(
                """
                UPDATE message_jobs
                SET status = ?, telegram_message_id = COALESCE(?, telegram_message_id),
                    next_attempt_at = ?, updated_at = ?, sent_at = ?,
                    last_error_type = ?, last_error_message = ?
                WHERE id = ?
                """,
                (
                    target.value,
                    telegram_message_id,
                    next_attempt_at,
                    now,
                    sent_at,
                    error_type,
                    error_message,
                    job_id,
                ),
            )
        updated = await self.get_job(str(job_id))
        assert updated is not None
        return updated

    async def recover_stale_processing(self) -> int:
        now = to_db_time(utc_now())
        async with self.db.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE message_jobs
                SET status = 'REVIEW_REQUIRED', updated_at = ?,
                    last_error_type = 'DELIVERY_UNCERTAIN_AFTER_RESTART',
                    last_error_message =
                        'Stopped during send; review before retrying to avoid a duplicate'
                WHERE status = 'PROCESSING'
                """,
                (now,),
            )
            return cursor.rowcount

    async def get_job(self, reference: str) -> MessageJob | None:
        if reference.isdigit():
            row = await self._fetchone("SELECT * FROM message_jobs WHERE id = ?", (int(reference),))
        else:
            row = await self._fetchone("SELECT * FROM message_jobs WHERE uuid = ?", (reference,))
        return self._job(row) if row else None

    async def list_jobs(
        self, statuses: Iterable[JobStatus] | None = None, *, limit: int = 100
    ) -> list[MessageJob]:
        parameters: tuple[object, ...] = ()
        sql = "SELECT * FROM message_jobs"
        if statuses:
            values = tuple(status.value for status in statuses)
            sql += f" WHERE status IN ({','.join('?' for _ in values)})"
            parameters = values
        sql += " ORDER BY id DESC LIMIT ?"
        return [self._job(row) for row in await self._fetchall(sql, (*parameters, limit))]

    async def cancel_job(self, reference: str, *, actor: str = "cli") -> MessageJob:
        job = await self.get_job(reference)
        if job is None:
            raise NotFoundError(f"Unknown job: {reference}")
        updated = await self.transition_job(job.id, JobStatus.CANCELLED)
        await self.add_audit("job_cancelled", actor=actor, entity_type="job", entity_id=job.uuid)
        return updated

    async def retry_job(self, reference: str, *, actor: str = "cli") -> MessageJob:
        job = await self.get_job(reference)
        if job is None:
            raise NotFoundError(f"Unknown job: {reference}")
        updated = await self.transition_job(
            job.id, JobStatus.RETRY, next_attempt_at=to_db_time(utc_now())
        )
        async with self.db.transaction(immediate=True) as connection:
            await connection.execute(
                "UPDATE message_jobs SET attempt_count = 0 WHERE id = ?", (job.id,)
            )
        await self.add_audit(
            "job_manually_retried", actor=actor, entity_type="job", entity_id=job.uuid
        )
        refreshed = await self.get_job(updated.uuid)
        assert refreshed is not None
        return refreshed

    async def queue_counts(self) -> dict[str, int]:
        rows = await self._fetchall(
            "SELECT status, COUNT(*) AS count FROM message_jobs GROUP BY status"
        )
        return {row["status"]: row["count"] for row in rows}

    async def last_successful_send(self) -> str | None:
        row = await self._fetchone(
            "SELECT MAX(sent_at) AS sent_at FROM message_jobs WHERE status='SENT'"
        )
        return row["sent_at"] if row else None

    async def set_state(self, key: str, value: str | None) -> None:
        async with self.db.transaction(immediate=True) as connection:
            await connection.execute(
                """
                INSERT INTO app_state(key, value, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, to_db_time(utc_now())),
            )

    async def get_state(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM app_state WHERE key = ?", (key,))
        return row["value"] if row else None

    async def add_audit(
        self,
        event_type: str,
        *,
        actor: str = "cli",
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: str | None = None,
    ) -> None:
        async with self.db.transaction(immediate=True) as connection:
            await connection.execute(
                """
                INSERT INTO audit_events(
                    event_type, actor, entity_type, entity_id, details, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    actor,
                    entity_type,
                    entity_id,
                    details,
                    to_db_time(utc_now()),
                ),
            )

    async def _fetchone(
        self, sql: str, parameters: tuple[object, ...] = ()
    ) -> aiosqlite.Row | None:
        async with self.db.lock:
            cursor = await self.db.require_connection().execute(sql, parameters)
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, parameters: tuple[object, ...] = ()) -> list[aiosqlite.Row]:
        async with self.db.lock:
            cursor = await self.db.require_connection().execute(sql, parameters)
            return await cursor.fetchall()

    @staticmethod
    def _destination(row: aiosqlite.Row) -> Destination:
        return Destination(
            telegram_chat_id=row["telegram_chat_id"],
            title=row["title"],
            username=row["username"],
            alias=row["alias"],
            chat_type=row["chat_type"],
            enabled=bool(row["enabled"]),
            can_send=bool(row["can_send"]),
            last_seen_at=row["last_seen_at"],
        )

    @staticmethod
    def _job(row: aiosqlite.Row) -> MessageJob:
        return MessageJob(
            id=row["id"],
            uuid=row["uuid"],
            destination_chat_id=row["destination_chat_id"],
            text=row["text"],
            media_path=Path(row["media_path"]) if row["media_path"] else None,
            parse_mode=row["parse_mode"],
            disable_link_preview=bool(row["disable_link_preview"]),
            scheduled_at=row["scheduled_at"],
            next_attempt_at=row["next_attempt_at"],
            status=JobStatus(row["status"]),
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            telegram_message_id=row["telegram_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            sent_at=row["sent_at"],
            last_error_type=row["last_error_type"],
            last_error_message=row["last_error_message"],
            idempotency_key=row["idempotency_key"],
            requested_by=row["requested_by"],
        )
