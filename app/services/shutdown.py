from __future__ import annotations

import asyncio
import logging
import signal

logger = logging.getLogger(__name__)


def install_shutdown_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        logger.info("shutdown_requested")
        stop_event.set()

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows' default event loop may not support asynchronous signal handlers.
            signal.signal(signal_name, lambda *_: loop.call_soon_threadsafe(request_stop))
