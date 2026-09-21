from __future__ import annotations

import asyncio
import logging

from app.commands.saved_messages import SavedMessagesController
from app.config import Settings
from app.db.database import Database
from app.db.repositories import Repository
from app.logging_config import configure_logging
from app.messaging.queue import QueueWorker
from app.messaging.service import MessageService
from app.services.shutdown import install_shutdown_handlers
from app.telegram.client import TelegramConnection, create_client
from app.telegram.dialogs import refresh_dialogs
from app.telegram.sender import TelegramSender
from app.utils.locks import ProcessLock

logger = logging.getLogger(__name__)


async def run_application(settings: Settings) -> None:
    settings.ensure_directories()
    recent_logs = configure_logging(settings.log_level, settings.log_format, settings.log_path)
    process_lock = ProcessLock(settings.tg_session_path.with_suffix(".lock"))
    process_lock.acquire()
    database = Database(settings.database_path)
    connection: TelegramConnection | None = None
    worker: QueueWorker | None = None
    worker_task: asyncio.Task[None] | None = None
    controller: SavedMessagesController | None = None
    try:
        logger.info("app_started")
        await database.connect()
        repository = Repository(database)
        await repository.initialize()
        recovered = await repository.recover_stale_processing()
        if recovered:
            logger.warning("stale_jobs_require_review", extra={"job_count": recovered})

        client = create_client(settings)
        connection = TelegramConnection(client, settings, repository)
        account = await connection.connect_and_authorize(interactive=False)
        destination_count = await refresh_dialogs(client, repository)

        sender = TelegramSender(client, dry_run=settings.dry_run)
        message_service = MessageService(repository, settings)
        worker = QueueWorker(repository, sender, settings)
        if settings.dry_run:
            logger.info(
                "[DRY-RUN] account resolved",
                extra={"user_id": account.telegram_user_id, "username": account.username},
            )
            logger.info(
                "[DRY-RUN] target metadata refreshed; send skipped",
                extra={"destination_count": destination_count},
            )
            return
        if settings.control_saved_messages:
            controller = SavedMessagesController(
                client,
                sender,
                repository,
                message_service,
                account.telegram_user_id,
                recent_logs,
            )
            controller.register()

        stop_event = asyncio.Event()
        install_shutdown_handlers(stop_event)
        worker_task = asyncio.create_task(worker.run(), name="outgoing-queue-worker")
        disconnected_task = asyncio.ensure_future(client.disconnected)
        stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
        done, pending = await asyncio.wait(
            {worker_task, disconnected_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            if task is not worker_task:
                task.cancel()
        for task in done:
            if task not in {worker_task, stop_task} and not task.cancelled():
                task.result()
    finally:
        logger.info("app_stopping")
        try:
            try:
                if controller is not None:
                    controller.unregister()
                if worker is not None:
                    worker.request_stop()
                if worker_task is not None and not worker_task.done():
                    await asyncio.wait_for(worker_task, timeout=30)
            except TimeoutError:
                assert worker_task is not None
                worker_task.cancel()
                logger.warning("queue_worker_shutdown_timeout")
            except Exception:
                logger.exception("queue_worker_shutdown_failed")
        finally:
            try:
                try:
                    if connection is not None:
                        await connection.disconnect()
                except Exception:
                    logger.exception("telegram_disconnect_failed")
            finally:
                try:
                    try:
                        await database.close()
                    except Exception:
                        logger.exception("database_close_failed")
                finally:
                    process_lock.release()
                    logger.info("app_stopped")
                    logging.shutdown()
