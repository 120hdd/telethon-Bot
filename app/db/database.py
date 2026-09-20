from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


class DatabaseLockedError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(str(self.path), timeout=5)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != Path(":memory:"):
            await self.connection.execute("PRAGMA journal_mode = WAL")
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def require_connection(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Database is not connected")
        return self.connection

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        connection = self.require_connection()
        async with self.lock:
            try:
                await connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                await connection.commit()
            except aiosqlite.OperationalError as exc:
                await connection.rollback()
                if "database is locked" in str(exc).lower():
                    raise DatabaseLockedError(
                        "Application database is locked; another process may be running."
                    ) from exc
                raise
            except BaseException:
                await connection.rollback()
                raise
