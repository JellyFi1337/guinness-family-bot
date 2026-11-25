from __future__ import annotations

import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from psycopg import IntegrityError as PostgresIntegrityError
from psycopg import OperationalError as PostgresOperationalError

from config import DATABASE_URL, DB_PATH, DB_POOL_SIZE
from services.metrics import metrics

from .postgresql import PostgresPool, safe_database_target
from .sqlite import SQLitePool


DB_BUSY_TIMEOUT_MS = 30_000
DATABASE_INTEGRITY_ERRORS = (sqlite3.IntegrityError, PostgresIntegrityError)
DATABASE_OPERATIONAL_ERRORS = (sqlite3.OperationalError, PostgresOperationalError)
_sqlite_pool = SQLitePool(size=DB_POOL_SIZE, busy_timeout_ms=DB_BUSY_TIMEOUT_MS)
_postgres_pool = PostgresPool(size=DB_POOL_SIZE)
_path = DB_PATH
_database_url = DATABASE_URL
_persistent = False


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def configure_database(
    path: Path,
    *,
    database_url: str | None = None,
    persistent: bool | None = None,
) -> None:
    global _path, _database_url, _persistent
    _path = Path(path)
    if database_url is not None:
        _database_url = database_url.strip()
    if persistent is not None:
        _persistent = persistent


def database_backend() -> str:
    return "postgresql" if _database_url else "sqlite"


def database_target() -> str:
    if _database_url:
        return f"postgresql:{safe_database_target(_database_url)}"
    return f"sqlite:{_path.resolve()}"


@asynccontextmanager
async def connection():
    """Return a configured PostgreSQL or SQLite compatibility connection."""

    backend = database_backend()
    acquire_started = time.monotonic()
    metrics.increment(f"database.{backend}.acquire_attempts")
    if _database_url:
        async with _postgres_pool.connection(_database_url) as db:
            metrics.observe(
                f"database.{backend}.acquire_ms",
                (time.monotonic() - acquire_started) * 1_000,
            )
            metrics.increment(f"database.{backend}.in_use")
            started = time.monotonic()
            try:
                yield db
            finally:
                metrics.increment(f"database.{backend}.in_use", -1)
                metrics.observe(
                    f"database.{backend}.connection_ms",
                    (time.monotonic() - started) * 1_000,
                )
        return
    if _persistent:
        async with _sqlite_pool.connection(_path) as db:
            metrics.observe(
                f"database.{backend}.acquire_ms",
                (time.monotonic() - acquire_started) * 1_000,
            )
            metrics.increment(f"database.{backend}.in_use")
            started = time.monotonic()
            try:
                yield db
            finally:
                metrics.increment(f"database.{backend}.in_use", -1)
                metrics.observe(
                    f"database.{backend}.connection_ms",
                    (time.monotonic() - started) * 1_000,
                )
        return
    _path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(
        _path,
        timeout=DB_BUSY_TIMEOUT_MS / 1_000,
    ) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("PRAGMA synchronous = NORMAL")
        metrics.observe(
            f"database.{backend}.acquire_ms",
            (time.monotonic() - acquire_started) * 1_000,
        )
        metrics.increment(f"database.{backend}.in_use")
        started = time.monotonic()
        try:
            yield db
        finally:
            metrics.increment(f"database.{backend}.in_use", -1)
            metrics.observe(
                f"database.{backend}.connection_ms",
                (time.monotonic() - started) * 1_000,
            )


async def analyze_database() -> None:
    """Rebuild planner statistics. Rare, administrative, never on a timer."""

    if _database_url:
        await _postgres_pool.analyze(_database_url)
        return
    async with connection() as db:
        await db.execute("ANALYZE")


async def checkpoint_database(*, truncate: bool = False) -> None:
    if _database_url:
        await _postgres_pool.maintain(_database_url)
        return
    if _persistent:
        await _sqlite_pool.checkpoint(truncate=truncate)
        return
    async with connection() as db:
        mode = "TRUNCATE" if truncate else "PASSIVE"
        await db.execute(f"PRAGMA wal_checkpoint({mode})")
        await db.execute("PRAGMA optimize")


async def close_database() -> None:
    global _persistent
    _persistent = False
    await _sqlite_pool.close()
    await _postgres_pool.close()


def database_status() -> dict[str, int | bool | str]:
    if _database_url:
        return {
            **_postgres_pool.status(),
            "backend": "postgresql",
            "persistent": _persistent,
            "target": safe_database_target(_database_url),
        }
    return {
        **_sqlite_pool.status(),
        "backend": "sqlite",
        "persistent": _persistent,
        "path": str(_path),
    }
