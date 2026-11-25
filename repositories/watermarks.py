"""Durable progress markers for periodic jobs.

A job that keeps its "last processed up to" position in an attribute loses it on
restart, and — worse — advancing it before the work succeeds silently drops the
window that failed. The car-park expiry sweep did exactly that: the watermark
moved first, so any failed refresh permanently skipped the bookings that had
expired during it.

Advance a watermark only after the pass it covers has succeeded.
"""

from __future__ import annotations

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


async def get_watermark(guild_id: int, job: str) -> str | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT position FROM job_watermarks WHERE guild_id = ? AND job = ?",
            (guild_id, job),
        )
    return str(rows[0][0]) if rows else None


async def set_watermark(guild_id: int, job: str, position: str) -> None:
    """Record progress. Call this only after the covered pass succeeded."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "job-watermark", f"{guild_id}:{job}")
        try:
            await db.execute(
                """INSERT INTO job_watermarks (guild_id, job, position, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, job) DO UPDATE SET
                       position = excluded.position,
                       updated_at = excluded.updated_at""",
                (guild_id, job, position, now),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
