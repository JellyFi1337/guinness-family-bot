from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from infrastructure.database import connection, utc_now
from infrastructure.transactions import begin_write


@dataclass(frozen=True, slots=True)
class MusicTrack:
    id: int
    interaction_id: int
    guild_id: int
    text_channel_id: int
    voice_channel_id: int
    requester_id: int
    query: str
    webpage_url: str
    title: str
    duration_seconds: int
    price: int
    volume_percent: int
    state: str
    refund_reason: str | None
    requested_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class MusicSession:
    guild_id: int
    text_channel_id: int
    message_id: int | None
    repeat_mode: str
    updated_at: str


class MusicStatistics(TypedDict):
    requested: int
    completed: int
    refunded: int
    gross_spent: int
    returned: int
    top: list[tuple[int, int]]


_COLUMNS = """id, interaction_id, guild_id, text_channel_id, voice_channel_id,
    requester_id, query, webpage_url, title, duration_seconds, price, state,
    refund_reason, requested_at, started_at, finished_at, volume_percent"""


def _track(row) -> MusicTrack:
    return MusicTrack(
        id=int(row[0]),
        interaction_id=int(row[1]),
        guild_id=int(row[2]),
        text_channel_id=int(row[3]),
        voice_channel_id=int(row[4]),
        requester_id=int(row[5]),
        query=str(row[6]),
        webpage_url=str(row[7]),
        title=str(row[8]),
        duration_seconds=int(row[9]),
        price=int(row[10]),
        volume_percent=int(row[16]),
        state=str(row[11]),
        refund_reason=str(row[12]) if row[12] is not None else None,
        requested_at=str(row[13]),
        started_at=str(row[14]) if row[14] is not None else None,
        finished_at=str(row[15]) if row[15] is not None else None,
    )


def _session(row) -> MusicSession:
    return MusicSession(
        guild_id=int(row[0]),
        text_channel_id=int(row[1]),
        message_id=int(row[2]) if row[2] is not None else None,
        repeat_mode=str(row[3]),
        updated_at=str(row[4]),
    )


async def get_music_session(guild_id: int) -> MusicSession | None:
    async with connection() as db:
        rows = await db.execute_fetchall(
            """SELECT guild_id, text_channel_id, message_id, repeat_mode, updated_at
               FROM music_sessions WHERE guild_id = ?""",
            (guild_id,),
        )
    return _session(rows[0]) if rows else None


async def list_music_sessions() -> list[MusicSession]:
    async with connection() as db:
        rows = await db.execute_fetchall(
            """SELECT guild_id, text_channel_id, message_id, repeat_mode, updated_at
               FROM music_sessions ORDER BY guild_id"""
        )
    return [_session(row) for row in rows]


async def upsert_music_session(
    *,
    guild_id: int,
    text_channel_id: int,
    message_id: int | None,
    repeat_mode: str,
) -> MusicSession:
    if repeat_mode not in {"off", "track", "queue"}:
        raise ValueError("Unknown music repeat mode")
    now = utc_now()
    async with connection() as db:
        try:
            await begin_write(db, "music-session", guild_id)
            existing = await db.execute_fetchall(
                "SELECT 1 FROM music_sessions WHERE guild_id = ?",
                (guild_id,),
            )
            if existing:
                await db.execute(
                    """UPDATE music_sessions
                       SET text_channel_id = ?, message_id = ?,
                           repeat_mode = ?, updated_at = ?
                       WHERE guild_id = ?""",
                    (
                        text_channel_id,
                        message_id,
                        repeat_mode,
                        now,
                        guild_id,
                    ),
                )
            else:
                await db.execute(
                    """INSERT INTO music_sessions
                       (guild_id, text_channel_id, message_id, repeat_mode, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        guild_id,
                        text_channel_id,
                        message_id,
                        repeat_mode,
                        now,
                    ),
                )
            rows = await db.execute_fetchall(
                """SELECT guild_id, text_channel_id, message_id, repeat_mode, updated_at
                   FROM music_sessions WHERE guild_id = ?""",
                (guild_id,),
            )
            await db.commit()
            return _session(rows[0])
        except BaseException:
            await db.rollback()
            raise


async def delete_music_session(guild_id: int) -> bool:
    async with connection() as db:
        cursor = await db.execute(
            "DELETE FROM music_sessions WHERE guild_id = ?",
            (guild_id,),
        )
        await db.commit()
    return cursor.rowcount == 1


async def create_paid_music_track(
    *,
    interaction_id: int,
    guild_id: int,
    text_channel_id: int,
    voice_channel_id: int,
    requester_id: int,
    query: str,
    webpage_url: str,
    title: str,
    duration_seconds: int,
    price: int,
    volume_percent: int = 50,
) -> MusicTrack | None:
    """Charge once and persist one queue entry in the same transaction."""

    if duration_seconds < 1 or price < 1:
        raise ValueError("Music duration and price must be positive")
    if not 1 <= volume_percent <= 100:
        raise ValueError("Music volume must be between 1 and 100")
    async with connection() as db:
        try:
            await begin_write(db, "music-order", interaction_id)
            existing = await db.execute_fetchall(
                f"SELECT {_COLUMNS} FROM music_tracks WHERE interaction_id = ?",
                (interaction_id,),
            )
            if existing:
                await db.commit()
                return _track(existing[0])

            await db.execute(
                "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)",
                (requester_id,),
            )
            account = await db.execute_fetchall(
                "SELECT balance FROM coin_accounts WHERE user_id = ?",
                (requester_id,),
            )
            if not account or int(account[0][0]) < price:
                await db.rollback()
                return None

            now = utc_now()
            await db.execute(
                "UPDATE coin_accounts SET balance = balance - ? WHERE user_id = ?",
                (price, requester_id),
            )
            cursor = await db.execute(
                """INSERT INTO music_tracks
                   (interaction_id, guild_id, text_channel_id, voice_channel_id,
                    requester_id, query, webpage_url, title, duration_seconds,
                    price, requested_at, volume_percent)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    interaction_id,
                    guild_id,
                    text_channel_id,
                    voice_channel_id,
                    requester_id,
                    query,
                    webpage_url,
                    title,
                    duration_seconds,
                    price,
                    now,
                    volume_percent,
                ),
            )
            track_id = int(cursor.lastrowid)
            await db.execute(
                """INSERT INTO coin_transactions
                   (user_id, amount, action, created_at)
                   VALUES (?, ?, 'music_play', ?)""",
                (requester_id, -price, now),
            )
            rows = await db.execute_fetchall(
                f"SELECT {_COLUMNS} FROM music_tracks WHERE id = ?",
                (track_id,),
            )
            await db.commit()
            return _track(rows[0])
        except BaseException:
            await db.rollback()
            raise


async def set_music_track_volume(track_id: int, volume_percent: int) -> bool:
    if not 1 <= volume_percent <= 100:
        raise ValueError("Music volume must be between 1 and 100")
    async with connection() as db:
        cursor = await db.execute(
            """UPDATE music_tracks
               SET volume_percent = ?
               WHERE id = ? AND state = 'playing'""",
            (volume_percent, track_id),
        )
        await db.commit()
    return cursor.rowcount == 1


async def mark_music_track_playing(track_id: int) -> bool:
    now = utc_now()
    async with connection() as db:
        cursor = await db.execute(
            """UPDATE music_tracks
               SET state = 'playing', started_at = ?
               WHERE id = ? AND state = 'queued'""",
            (now, track_id),
        )
        await db.commit()
    return cursor.rowcount == 1


async def complete_music_track(track_id: int) -> bool:
    now = utc_now()
    async with connection() as db:
        cursor = await db.execute(
            """UPDATE music_tracks
               SET state = 'completed', finished_at = ?
               WHERE id = ? AND state = 'playing'""",
            (now, track_id),
        )
        await db.commit()
    return cursor.rowcount == 1


async def cancel_music_track(track_id: int, reason: str) -> bool:
    now = utc_now()
    async with connection() as db:
        cursor = await db.execute(
            """UPDATE music_tracks
               SET state = 'cancelled', refund_reason = ?, finished_at = ?
               WHERE id = ? AND state IN ('queued', 'playing')""",
            (reason[:100], now, track_id),
        )
        await db.commit()
    return cursor.rowcount == 1


async def refund_music_track(track_id: int, reason: str) -> bool:
    """Refund a queued/playing track exactly once."""

    now = utc_now()
    async with connection() as db:
        try:
            await begin_write(db, "music-track", track_id)
            rows = await db.execute_fetchall(
                """SELECT requester_id, price FROM music_tracks
                   WHERE id = ? AND state IN ('queued', 'playing')""",
                (track_id,),
            )
            if not rows:
                await db.rollback()
                return False
            requester_id, price = map(int, rows[0])
            cursor = await db.execute(
                """UPDATE music_tracks
                   SET state = 'refunded', refund_reason = ?, finished_at = ?
                   WHERE id = ? AND state IN ('queued', 'playing')""",
                (reason[:100], now, track_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.execute(
                "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)",
                (requester_id,),
            )
            await db.execute(
                "UPDATE coin_accounts SET balance = balance + ? WHERE user_id = ?",
                (price, requester_id),
            )
            await db.execute(
                """INSERT INTO coin_transactions
                   (user_id, amount, action, created_at)
                   VALUES (?, ?, 'music_refund', ?)""",
                (requester_id, price, now),
            )
            await db.commit()
            return True
        except BaseException:
            await db.rollback()
            raise


async def refund_interrupted_music_tracks() -> int:
    """Return coins for tracks left active by a previous bot process."""

    now = utc_now()
    async with connection() as db:
        try:
            await begin_write(db, "music-recovery", "unfinished")
            rows = await db.execute_fetchall(
                """SELECT id, requester_id, price FROM music_tracks
                   WHERE state IN ('queued', 'playing')"""
            )
            for track_id, requester_id, price in rows:
                await db.execute(
                    "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)",
                    (requester_id,),
                )
                await db.execute(
                    "UPDATE coin_accounts SET balance = balance + ? WHERE user_id = ?",
                    (price, requester_id),
                )
                await db.execute(
                    """INSERT INTO coin_transactions
                       (user_id, amount, action, created_at)
                       VALUES (?, ?, 'music_restart_refund', ?)""",
                    (requester_id, price, now),
                )
                await db.execute(
                    """UPDATE music_tracks
                       SET state = 'refunded',
                           refund_reason = 'bot_restart',
                           finished_at = ?
                       WHERE id = ? AND state IN ('queued', 'playing')""",
                    (now, track_id),
                )
            await db.commit()
            return len(rows)
        except BaseException:
            await db.rollback()
            raise


async def get_music_statistics(guild_id: int) -> MusicStatistics:
    async with connection() as db:
        totals = await db.execute_fetchall(
            """SELECT COUNT(*),
                      COALESCE(SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END), 0),
                      COALESCE(SUM(CASE WHEN state = 'refunded' THEN 1 ELSE 0 END), 0),
                      COALESCE(SUM(price), 0),
                      COALESCE(SUM(CASE WHEN state = 'refunded' THEN price ELSE 0 END), 0)
               FROM music_tracks WHERE guild_id = ?""",
            (guild_id,),
        )
        top = await db.execute_fetchall(
            """SELECT requester_id, COUNT(*) AS tracks
               FROM music_tracks
               WHERE guild_id = ?
               GROUP BY requester_id
               ORDER BY tracks DESC, requester_id
               LIMIT 5""",
            (guild_id,),
        )
    row = totals[0]
    return {
        "requested": int(row[0]),
        "completed": int(row[1]),
        "refunded": int(row[2]),
        "gross_spent": int(row[3]),
        "returned": int(row[4]),
        "top": [(int(item[0]), int(item[1])) for item in top],
    }
