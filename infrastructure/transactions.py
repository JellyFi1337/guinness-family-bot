from __future__ import annotations

import hashlib

from infrastructure.database import database_backend


def _lock_id(namespace: str, key: object) -> int:
    payload = f"{namespace}:{key}".encode("utf-8")
    return int.from_bytes(
        hashlib.blake2b(payload, digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )


async def begin_write(db, namespace: str, key: object) -> None:
    """Begin a write transaction scoped to one business aggregate.

    SQLite retains its process-wide writer lock. PostgreSQL serialises only
    operations touching the same stable aggregate key, so unrelated guilds,
    users and workflows can write concurrently.

    On PostgreSQL, ``BEGIN`` is idempotent when the connection already has an
    open transaction (common after a prior SELECT on the same checkout).
    """

    if database_backend() != "postgresql":
        await db.execute("BEGIN IMMEDIATE")
        return
    if not getattr(db, "in_transaction", False):
        await db.execute("BEGIN")
    cursor = await db.execute(
        "SELECT pg_advisory_xact_lock(?)",
        (_lock_id(namespace, key),),
    )
    await cursor.fetchone()
    await cursor.close()
