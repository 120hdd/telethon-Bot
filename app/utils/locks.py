from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TextIO

import portalocker

from app.utils.time import to_db_time, utc_now


class AlreadyRunningError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock: portalocker.Lock | None = None
        self._handle: TextIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(str(self.path), mode="a+", timeout=0, fail_when_locked=True)
        try:
            handle = lock.acquire()
        except portalocker.exceptions.LockException as exc:
            owner = self._read_owner()
            detail = f" ({owner})" if owner else ""
            raise AlreadyRunningError(
                f"Another process is using this Telegram session{detail}."
            ) from exc
        handle.seek(0)
        handle.truncate()
        json.dump({"pid": os.getpid(), "acquired_at": to_db_time(utc_now())}, handle)
        handle.flush()
        self._lock = lock
        self._handle = handle

    def _read_owner(self) -> str | None:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return raw or None

    def release(self) -> None:
        if self._lock is not None:
            self._lock.release()
        self._lock = None
        self._handle = None

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
