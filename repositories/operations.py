from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from infrastructure.transactions import begin_write
from infrastructure.database import (
    connection as _connect,
    database_backend,
    utc_now as _utc_now,
)


@dataclass(frozen=True, slots=True)
class OutboxOperation:
    id: int
    idempotency_key: str
    guild_id: int
    kind: str
    aggregate_id: str | None
    payload: dict[str, Any]
    attempts: int
    created_at: str


_WAKE_EVENT: asyncio.Event | None = None


def wake_operation_worker() -> None:
    """Wake the in-process worker without coupling repositories to the bot."""

    global _WAKE_EVENT
    if _WAKE_EVENT is None:
        _WAKE_EVENT = asyncio.Event()
    _WAKE_EVENT.set()


def consume_operation_wake() -> bool:
    event = _WAKE_EVENT
    if event is None or not event.is_set():
        return False
    event.clear()
    return True


async def enqueue_operation(
    *,
    idempotency_key: str,
    guild_id: int,
    kind: str,
    payload: dict[str, Any],
    aggregate_id: str | int | None = None,
    available_at: str | datetime | None = None,
) -> int:
    now = _utc_now()
    if available_at is None:
        when = now
    elif isinstance(available_at, datetime):
        stamp = (
            available_at
            if available_at.tzinfo is not None
            else available_at.replace(tzinfo=UTC)
        )
        when = stamp.astimezone(UTC).isoformat()
    else:
        when = available_at
    async with _connect() as db:
        await db.execute(
            "BEGIN" if database_backend() == "postgresql" else "BEGIN IMMEDIATE"
        )
        await db.execute(
            """INSERT OR IGNORE INTO operation_outbox
               (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                idempotency_key,
                guild_id,
                kind,
                str(aggregate_id) if aggregate_id is not None else None,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                when,
                now,
                now,
            ),
        )
        row = await db.execute_fetchall(
            "SELECT id FROM operation_outbox WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        await db.commit()
    wake_operation_worker()
    return int(row[0][0])


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: int
    idempotency_key: str
    kind: str
    state: str
    available_at: str
    aggregate_id: str | None


async def cancel_pending_operations(
    *,
    guild_id: int,
    aggregate_id: str | int,
    kind_prefix: str,
    reason: str,
) -> int:
    """Mark pending/retry outbox rows as dead so a reschedule can replace them."""

    now = _utc_now()
    pattern = f"{kind_prefix}%"
    async with _connect() as db:
        await db.execute(
            "BEGIN" if database_backend() == "postgresql" else "BEGIN IMMEDIATE"
        )
        cursor = await db.execute(
            """UPDATE operation_outbox
               SET state = 'dead', last_error = ?, lease_until = NULL, updated_at = ?
               WHERE guild_id = ? AND aggregate_id = ?
                 AND kind LIKE ?
                 AND state IN ('pending', 'retry')""",
            (
                f"cancelled:{reason}"[:1_000],
                now,
                guild_id,
                str(aggregate_id),
                pattern,
            ),
        )
        await db.commit()
    return max(0, int(cursor.rowcount))


async def list_operations_for_aggregate(
    *,
    guild_id: int,
    aggregate_id: str | int,
    kind_prefix: str | None = None,
) -> list[OutboxRecord]:
    where = "guild_id = ? AND aggregate_id = ?"
    params: list[Any] = [guild_id, str(aggregate_id)]
    if kind_prefix:
        where += " AND kind LIKE ?"
        params.append(f"{kind_prefix}%")
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT id, idempotency_key, kind, state, available_at, aggregate_id
                FROM operation_outbox
                WHERE {where}
                ORDER BY id""",
            tuple(params),
        )
    return [
        OutboxRecord(
            int(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]) if row[5] is not None else None,
        )
        for row in rows
    ]


def _operation_from_row(row: tuple[Any, ...]) -> OutboxOperation:
    return OutboxOperation(
        int(row[0]),
        str(row[1]),
        int(row[2]),
        str(row[3]),
        str(row[4]) if row[4] is not None else None,
        json.loads(row[5]),
        int(row[6]),
        str(row[7]),
    )


async def reap_expired_operations() -> int:
    now = datetime.now(UTC).isoformat()
    async with _connect() as db:
        await db.execute("BEGIN" if database_backend() == "postgresql" else "BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                """UPDATE operation_outbox
                   SET state = 'retry', lease_until = NULL, updated_at = ?
                   WHERE state = 'processing' AND lease_until <= ?""",
                (now, now),
            )
            count = max(0, int(cursor.rowcount))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    if count:
        wake_operation_worker()
    return count


async def claim_operations(
    *,
    limit: int = 25,
    lease_seconds: int = 120,
    kinds: tuple[str, ...] | None = None,
) -> list[OutboxOperation]:
    now = datetime.now(UTC)
    now_text = now.isoformat()
    lease_until = (now + timedelta(seconds=max(10, lease_seconds))).isoformat()
    batch_size = max(1, min(int(limit), 100))
    where = "state IN ('pending', 'retry') AND available_at <= ?"
    params: list[Any] = [now_text]
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        where += f" AND kind IN ({placeholders})"
        params.extend(kinds)

    async with _connect() as db:
        if database_backend() == "postgresql":
            await db.execute("BEGIN")
            try:
                rows = await db.execute_fetchall(
                    f"""WITH candidates AS (
                            SELECT id
                            FROM operation_outbox
                            WHERE {where}
                            ORDER BY id
                            LIMIT ?
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE operation_outbox AS operation
                        SET state = 'processing',
                            attempts = operation.attempts + 1,
                            lease_until = ?,
                            updated_at = ?
                        FROM candidates
                        WHERE operation.id = candidates.id
                        RETURNING operation.id, operation.idempotency_key,
                                  operation.guild_id, operation.kind,
                                  operation.aggregate_id, operation.payload_json,
                                  operation.attempts, operation.created_at""",
                    (*params, batch_size, lease_until, now_text),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return sorted(
                (_operation_from_row(tuple(row)) for row in rows),
                key=lambda item: item.id,
            )

        await db.execute("BEGIN IMMEDIATE")
        try:
            rows = await db.execute_fetchall(
                f"""SELECT id, idempotency_key, guild_id, kind, aggregate_id,
                           payload_json, attempts, created_at
                    FROM operation_outbox
                    WHERE {where}
                    ORDER BY id LIMIT ?""",
                (*params, batch_size),
            )
            if not rows:
                await db.commit()
                return []
            ids = [int(row[0]) for row in rows]
            await db.executemany(
                """UPDATE operation_outbox
                   SET state = 'processing', attempts = attempts + 1,
                       lease_until = ?, updated_at = ?
                   WHERE id = ? AND state IN ('pending', 'retry')""",
                ((lease_until, now_text, operation_id) for operation_id in ids),
            )
            claimed = await db.execute_fetchall(
                f"""SELECT id, idempotency_key, guild_id, kind, aggregate_id,
                           payload_json, attempts, created_at
                    FROM operation_outbox
                    WHERE id IN ({','.join('?' for _ in ids)})
                      AND state = 'processing'
                    ORDER BY id""",
                ids,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return [_operation_from_row(tuple(row)) for row in claimed]


async def claim_operation(
    *,
    lease_seconds: int = 120,
    kinds: tuple[str, ...] | None = None,
) -> OutboxOperation | None:
    # Keep the public single-claim API self-healing for administrative tools
    # and tests. The production worker runs the reaper on a separate cadence.
    await reap_expired_operations()
    claimed = await claim_operations(
        limit=1,
        lease_seconds=lease_seconds,
        kinds=kinds,
    )
    return claimed[0] if claimed else None


async def complete_operation(operation_id: int, external_id: str | int | None = None) -> None:
    now = _utc_now()
    async with _connect() as db:
        await db.execute(
            """UPDATE operation_outbox
               SET state = 'completed', external_id = ?, lease_until = NULL,
                   completed_at = ?, updated_at = ?, last_error = NULL
               WHERE id = ? AND state = 'processing'""",
            (
                str(external_id) if external_id is not None else None,
                now,
                now,
                operation_id,
            ),
        )
        await db.commit()


async def retry_operation(
    operation_id: int,
    error: BaseException,
    *,
    delay_seconds: float,
    dead: bool = False,
) -> None:
    now = datetime.now(UTC)
    available = now + timedelta(seconds=max(1.0, delay_seconds))
    async with _connect() as db:
        await db.execute(
            """UPDATE operation_outbox
               SET state = ?, available_at = ?, lease_until = NULL,
                   last_error = ?, updated_at = ? WHERE id = ?""",
            (
                "dead" if dead else "retry",
                available.isoformat(),
                f"{type(error).__name__}: {error}"[:1_000],
                now.isoformat(),
                operation_id,
            ),
        )
        await db.commit()


async def update_task_health(
    name: str,
    state: str,
    *,
    last_started: str | None = None,
    last_success: str | None = None,
    last_failure: str | None = None,
    consecutive_failures: int = 0,
    last_error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO task_health
               (name, state, last_started, last_success, last_failure,
                consecutive_failures, last_error, duration_ms, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   state = excluded.state,
                   last_started = COALESCE(excluded.last_started, task_health.last_started),
                   last_success = COALESCE(excluded.last_success, task_health.last_success),
                   last_failure = COALESCE(excluded.last_failure, task_health.last_failure),
                   consecutive_failures = excluded.consecutive_failures,
                   last_error = excluded.last_error,
                   duration_ms = excluded.duration_ms,
                   updated_at = excluded.updated_at""",
            (
                name,
                state,
                last_started,
                last_success,
                last_failure,
                consecutive_failures,
                last_error[:1_000] if last_error else None,
                duration_ms,
                _utc_now(),
            ),
        )
        await db.commit()


async def claim_command_receipt(
    guild_id: int, interaction_id: int, kind: str
) -> bool:
    now = _utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO command_receipts
               (guild_id, interaction_id, kind, state, created_at, updated_at)
               VALUES (?, ?, ?, 'processing', ?, ?)""",
            (guild_id, interaction_id, kind, now, now),
        )
        await db.commit()
    return cursor.rowcount == 1


async def complete_command_receipt(
    guild_id: int,
    interaction_id: int,
    kind: str,
    *,
    result_ref: str | None = None,
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE command_receipts SET state = 'completed', result_ref = ?, updated_at = ?
               WHERE guild_id = ? AND interaction_id = ? AND kind = ?""",
            (result_ref, _utc_now(), guild_id, interaction_id, kind),
        )
        await db.commit()


async def get_operational_health() -> dict[str, list[tuple]]:
    async with _connect() as db:
        outbox = await db.execute_fetchall(
            """SELECT state, COUNT(*) FROM operation_outbox GROUP BY state
               ORDER BY state"""
        )
        tasks = await db.execute_fetchall(
            """SELECT name, state, consecutive_failures, last_success, last_error
               FROM task_health ORDER BY name"""
        )
    return {"outbox": outbox, "tasks": tasks}


async def prune_operational_history(
    *,
    operation_retention_days: int,
    poll_retention_days: int,
    batch_size: int = 5_000,
) -> dict[str, int]:
    """Bound idempotency/outbox/poll history in small indexed batches."""

    now = datetime.now(UTC)
    operation_cutoff = (
        now - timedelta(days=max(7, operation_retention_days))
    ).isoformat()
    dead_cutoff = (
        now - timedelta(days=max(180, operation_retention_days * 6))
    ).isoformat()
    poll_cutoff = (
        now - timedelta(days=max(30, poll_retention_days))
    ).isoformat()
    limit = max(100, min(int(batch_size), 20_000))

    statements = (
        (
            "outbox_completed",
            """DELETE FROM operation_outbox WHERE id IN (
                   SELECT id FROM operation_outbox
                   WHERE state = 'completed' AND completed_at < ?
                   ORDER BY completed_at, id LIMIT ?
               )""",
            (operation_cutoff, limit),
        ),
        (
            "outbox_dead",
            """DELETE FROM operation_outbox WHERE id IN (
                   SELECT id FROM operation_outbox
                   WHERE state = 'dead' AND updated_at < ?
                   ORDER BY updated_at, id LIMIT ?
               )""",
            (dead_cutoff, limit),
        ),
        (
            "receipts",
            """DELETE FROM command_receipts
               WHERE (guild_id, interaction_id, kind) IN (
                   SELECT guild_id, interaction_id, kind
                   FROM command_receipts
                   WHERE state = 'completed' AND updated_at < ?
                   ORDER BY updated_at, guild_id, interaction_id, kind
                   LIMIT ?
               )""",
            (operation_cutoff, limit),
        ),
        (
            "shop_intents",
            """DELETE FROM shop_purchase_intents WHERE id IN (
                   SELECT id FROM shop_purchase_intents
                   WHERE state IN ('completed', 'refunded') AND completed_at < ?
                   ORDER BY completed_at, id LIMIT ?
               )""",
            (operation_cutoff, limit),
        ),
        (
            "polls",
            """DELETE FROM polls WHERE id IN (
                   SELECT id FROM polls
                   WHERE status = 'closed' AND closed_at < ?
                   ORDER BY closed_at, id LIMIT ?
               )""",
            (poll_cutoff, limit),
        ),
    )
    deleted: dict[str, int] = {}
    async with _connect() as db:
        # Retention is bounded and unrelated to live traffic; it has no business
        # holding the process-wide write lock while it runs.
        await begin_write(db, "retention", "operations")
        try:
            for key, query, params in statements:
                cursor = await db.execute(query, params)
                deleted[key] = max(0, cursor.rowcount)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return deleted
