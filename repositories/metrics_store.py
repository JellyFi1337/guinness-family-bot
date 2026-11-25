"""Persistence for the in-memory metrics registry.

`MetricsRegistry` is deliberately fast and in-process: a lock, three dicts and a
bounded deque. It is also erased by every restart, which is when an operator is
most likely to be looking — right after the thing that made them restart.

Counters are restored on startup so "how many times has this happened" survives;
snapshots keep a short trend so a question asked on Tuesday can still be
answered about Monday. The registry itself stays untouched on the hot path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


SNAPSHOT_RETENTION_DAYS = 14
_SNAPSHOT_PRUNE_BATCH = 500


async def load_counters() -> dict[str, int]:
    """Cumulative counters recorded before the last shutdown."""

    async with _connect() as db:
        rows = await db.execute_fetchall("SELECT name, value FROM metric_counters")
    return {str(name): int(value) for name, value in rows}


async def persist_snapshot(snapshot: dict) -> None:
    """Store counters for restore and the whole snapshot for trend analysis."""

    now = _utc_now()
    counters = {
        str(name): int(value)
        for name, value in dict(snapshot.get("counters", {})).items()
    }
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    async with _connect() as db:
        await begin_write(db, "metrics", "snapshot")
        try:
            for name, value in counters.items():
                await db.execute(
                    """INSERT INTO metric_counters (name, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                           value = excluded.value,
                           updated_at = excluded.updated_at""",
                    (name, value, now),
                )
            await db.execute(
                """INSERT INTO metric_snapshots (captured_at, payload_json)
                   VALUES (?, ?)""",
                (now, payload),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def prune_snapshots(retention_days: int = SNAPSHOT_RETENTION_DAYS) -> int:
    """Drop snapshots past retention, in one bounded batch per call."""

    cutoff = (datetime.now(UTC) - timedelta(days=max(1, retention_days))).isoformat()
    async with _connect() as db:
        await begin_write(db, "metrics", "snapshot")
        try:
            cursor = await db.execute(
                """DELETE FROM metric_snapshots WHERE id IN (
                       SELECT id FROM metric_snapshots
                       WHERE captured_at < ?
                       ORDER BY captured_at, id LIMIT ?
                   )""",
                (cutoff, _SNAPSHOT_PRUNE_BATCH),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return max(0, cursor.rowcount)


async def recent_snapshots(limit: int = 24) -> list[tuple[str, dict]]:
    """Most recent snapshots, newest first."""

    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT captured_at, payload_json FROM metric_snapshots
               ORDER BY captured_at DESC, id DESC LIMIT ?""",
            (max(1, int(limit)),),
        )
    result: list[tuple[str, dict]] = []
    for captured_at, payload in rows:
        try:
            result.append((str(captured_at), json.loads(payload)))
        except ValueError:
            continue
    return result
