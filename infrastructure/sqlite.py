from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


class SQLitePool:
    """Small WAL-aware connection pool for the bot's production event loop.

    ``aiosqlite`` owns one worker thread per connection. Reusing a bounded set
    avoids creating hundreds of short-lived threads under button-heavy load,
    while multiple connections still allow WAL readers to run alongside a
    writer.
    """

    def __init__(self, *, size: int = 4, busy_timeout_ms: int = 30_000) -> None:
        self.size = max(1, min(int(size), 16))
        self.busy_timeout_ms = max(1_000, int(busy_timeout_ms))
        self._path: Path | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._available: asyncio.LifoQueue[aiosqlite.Connection] | None = None
        self._connections: set[aiosqlite.Connection] = set()
        self._guard: asyncio.Lock | None = None

    async def _open(self, path: Path) -> aiosqlite.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(
            path,
            timeout=self.busy_timeout_ms / 1_000,
        )
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        await connection.execute("PRAGMA journal_mode = WAL")
        await connection.execute("PRAGMA synchronous = NORMAL")
        await connection.execute("PRAGMA temp_store = MEMORY")
        return connection

    async def _ensure_runtime(self, path: Path) -> None:
        loop = asyncio.get_running_loop()
        resolved = path.resolve()
        if self._loop is loop and self._path == resolved:
            return

        # A DB path change is expected in tests and during explicit reconfigure.
        # Close the previous pool before attaching locks/queues to the new loop.
        await self.close()
        self._loop = loop
        self._path = resolved
        self._available = asyncio.LifoQueue(maxsize=self.size)
        self._guard = asyncio.Lock()

    async def acquire(self, path: Path) -> aiosqlite.Connection:
        await self._ensure_runtime(path)
        assert self._available is not None
        assert self._guard is not None

        try:
            return self._available.get_nowait()
        except asyncio.QueueEmpty:
            pass

        async with self._guard:
            try:
                return self._available.get_nowait()
            except asyncio.QueueEmpty:
                if len(self._connections) < self.size:
                    connection = await self._open(path)
                    self._connections.add(connection)
                    return connection

        return await self._available.get()

    async def release(self, connection: aiosqlite.Connection, *, broken: bool = False) -> None:
        if connection not in self._connections:
            return
        if not broken:
            try:
                if connection.in_transaction:
                    await connection.rollback()
            except (sqlite3.Error, ValueError):
                broken = True
        if broken or self._available is None:
            self._connections.discard(connection)
            try:
                await connection.close()
            except (sqlite3.Error, ValueError):
                pass
            return
        await self._available.put(connection)

    @asynccontextmanager
    async def connection(self, path: Path):
        connection = await self.acquire(path)
        broken = False
        try:
            yield connection
        except (sqlite3.Error, ValueError):
            try:
                await connection.rollback()
            except (sqlite3.Error, ValueError):
                broken = True
            raise
        except BaseException:
            try:
                await connection.rollback()
            except (sqlite3.Error, ValueError):
                broken = True
            raise
        finally:
            await self.release(connection, broken=broken)

    async def checkpoint(self, *, truncate: bool = False) -> None:
        if self._path is None:
            return
        connection = await self.acquire(self._path)
        try:
            mode = "TRUNCATE" if truncate else "PASSIVE"
            await connection.execute(f"PRAGMA wal_checkpoint({mode})")
            await connection.execute("PRAGMA optimize")
        finally:
            await self.release(connection)

    async def close(self) -> None:
        connections = tuple(self._connections)
        self._connections.clear()
        self._available = None
        self._guard = None
        self._path = None
        self._loop = None
        for connection in connections:
            try:
                await connection.close()
            except (sqlite3.Error, ValueError, RuntimeError):
                pass

    def status(self) -> dict[str, int | bool]:
        available = self._available.qsize() if self._available is not None else 0
        return {
            "enabled": self._available is not None,
            "size": self.size,
            "opened": len(self._connections),
            "available": available,
            "in_use": max(0, len(self._connections) - available),
        }
