"""Giveaways: entries and the two-phase draw that survives a restart.

Extracted from the former monolithic ``utils.database``. The names are
re-exported from there, so existing imports keep working while new code
depends on this module directly.
"""

from __future__ import annotations

import json

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


async def _one(db, query: str, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchone()

async def create_giveaway(
    title: str,
    prize: str,
    winner_count: int,
    channel_id: int,
    creator_id: int,
    ends_at: str,
    guild_id: int | None = None,
) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO giveaways
               (title, prize, winner_count, channel_id, creator_id, ends_at,
                created_at, guild_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title.strip(),
                prize.strip(),
                winner_count,
                channel_id,
                creator_id,
                ends_at,
                _utc_now(),
                guild_id,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def set_giveaway_message(giveaway_id: int, message_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE giveaways SET message_id = ? WHERE id = ?",
            (message_id, giveaway_id),
        )
        await db.commit()


async def get_giveaway(giveaway_id: int):
    async with _connect() as db:
        return await _one(
            db,
            """SELECT id, title, prize, winner_count, channel_id, message_id,
                      creator_id, ends_at, status
               FROM giveaways WHERE id = ?""",
            (giveaway_id,),
        )


async def get_active_giveaways():
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT id, guild_id, channel_id, message_id, ends_at FROM giveaways
               WHERE status = 'active'"""
        )


async def clear_giveaway_message(
    giveaway_id: int, *, expected_message_id: int | None = None
) -> bool:
    async with _connect() as db:
        if expected_message_id is None:
            cursor = await db.execute(
                "UPDATE giveaways SET message_id = NULL WHERE id = ?",
                (giveaway_id,),
            )
        else:
            cursor = await db.execute(
                """UPDATE giveaways SET message_id = NULL
                   WHERE id = ? AND message_id = ?""",
                (giveaway_id, expected_message_id),
            )
        await db.commit()
    return cursor.rowcount > 0


async def get_giveaway_entries(giveaway_id: int) -> list[int]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?
               ORDER BY joined_at, user_id""",
            (giveaway_id,),
        )
        return [row[0] for row in rows]


async def toggle_giveaway_entry(giveaway_id: int, user_id: int) -> str:
    async with _connect() as db:
        await begin_write(db, "giveaway", giveaway_id)
        giveaway = await _one(
            db, "SELECT status, ends_at FROM giveaways WHERE id = ?", (giveaway_id,)
        )
        if giveaway is None or giveaway[0] != "active" or giveaway[1] <= _utc_now():
            await db.rollback()
            return "closed"
        current = await _one(
            db,
            "SELECT 1 FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?",
            (giveaway_id, user_id),
        )
        if current:
            await db.execute(
                "DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?",
                (giveaway_id, user_id),
            )
            result = "left"
        else:
            await db.execute(
                """INSERT INTO giveaway_entries (giveaway_id, user_id, joined_at)
                   VALUES (?, ?, ?)""",
                (giveaway_id, user_id, _utc_now()),
            )
            result = "joined"
        await db.commit()
        return result


async def finish_giveaway(giveaway_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE giveaways SET status = 'finished', finished_at = ?
               WHERE id = ? AND status = 'active'""",
            (_utc_now(), giveaway_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def prepare_giveaway_draw(
    giveaway_id: int,
    winner_ids: list[int],
    *,
    force_refresh: bool = False,
    forced_by_id: int | None = None,
) -> tuple[int, list[int]] | None:
    """Persist one immutable draw and its recovery operation before Discord I/O."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "giveaway", giveaway_id)
        row = await _one(
            db,
            """SELECT status, winner_ids, draw_version, guild_id
               FROM giveaways WHERE id = ?""",
            (giveaway_id,),
        )
        if row is None:
            await db.rollback()
            return None
        status, stored_winners, draw_version, guild_id = row
        if status == "announcing" and stored_winners:
            await db.commit()
            return int(draw_version), [int(value) for value in json.loads(stored_winners)]
        if status != "active" and not (force_refresh and status == "finished"):
            await db.rollback()
            return None
        version = int(draw_version or 0) + 1
        winners_json = json.dumps([int(value) for value in winner_ids], separators=(",", ":"))
        await db.execute(
            """UPDATE giveaways
               SET status = 'announcing', winner_ids = ?, draw_version = ?,
                   result_message_id = NULL, announcement_state = 'pending'
               WHERE id = ?""",
            (winners_json, version, giveaway_id),
        )
        key = f"giveaway:{giveaway_id}:draw:{version}"
        payload_data = {
            "giveaway_id": giveaway_id,
            "draw_version": version,
        }
        if forced_by_id is not None:
            payload_data["forced_by_id"] = int(forced_by_id)
        payload = json.dumps(payload_data, separators=(",", ":"))
        await db.execute(
            """INSERT OR IGNORE INTO operation_outbox
               (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                available_at, created_at, updated_at)
               VALUES (?, ?, 'giveaway.announce', ?, ?, ?, ?, ?)""",
            (key, int(guild_id or 0), str(giveaway_id), payload, now, now, now),
        )
        await db.commit()
        return version, [int(value) for value in winner_ids]


async def get_giveaway_draw(giveaway_id: int):
    async with _connect() as db:
        row = await _one(
            db,
            """SELECT draw_version, winner_ids, result_message_id,
                      announcement_state, status
               FROM giveaways WHERE id = ?""",
            (giveaway_id,),
        )
    if row is None:
        return None
    return (
        int(row[0] or 0),
        [int(value) for value in json.loads(row[1] or "[]")],
        row[2],
        row[3],
        row[4],
    )


async def complete_giveaway_draw(
    giveaway_id: int,
    draw_version: int,
    result_message_id: int,
) -> bool:
    now = _utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE giveaways
               SET status = 'finished', announcement_state = 'announced',
                   result_message_id = ?, finished_at = ?
               WHERE id = ? AND draw_version = ?
                 AND status = 'announcing'""",
            (result_message_id, now, giveaway_id, draw_version),
        )
        await db.commit()
        return cursor.rowcount == 1


async def delete_giveaway(giveaway_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE giveaways SET status = 'deleted', finished_at = ?
               WHERE id = ? AND status != 'deleted'""",
            (_utc_now(), giveaway_id),
        )
        await db.commit()
        return cursor.rowcount > 0
