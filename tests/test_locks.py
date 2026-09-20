from pathlib import Path

import pytest

from app.utils.locks import AlreadyRunningError, ProcessLock


def test_second_process_lock_is_rejected(tmp_path: Path) -> None:
    first = ProcessLock(tmp_path / "main.lock")
    second = ProcessLock(tmp_path / "main.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError, match="Another process"):
            second.acquire()
    finally:
        first.release()
