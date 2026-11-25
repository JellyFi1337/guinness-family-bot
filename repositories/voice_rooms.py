"""Temporary voice rooms: ownership, transfers and per-owner ban lists.

Extracted from the former monolithic ``utils.database``. The names are
re-exported from there, so existing imports keep working while new code
depends on this module directly.
"""

from __future__ import annotations

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


async def _one(db, query: str, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchone()

async def create_voice_room(
    channel_id: int,
    owner_id: int,
    *,
    creator_id: int | None = None,
) -> bool:
    """Register a temporary room without losing its original creator.

    ``owner_id`` may change when a room is transferred. ``creator_id`` is
    deliberately write-once and is used to attribute historical voice
    activity after Discord deletes the temporary channel.
    """

    original_creator_id = owner_id if creator_id is None else creator_id
    async with _connect() as db:
        await begin_write(db, "voice-room", channel_id)
        existing = await _one(
            db,
            """SELECT channel_id FROM voice_rooms
               WHERE owner_id = ?
                  OR (transfer_state = 'pending' AND previous_owner_id = ?)
               LIMIT 1""",
            (owner_id, owner_id),
        )
        if existing is not None and existing[0] != channel_id:
            await db.rollback()
            return False
        await db.execute(
            """INSERT INTO voice_rooms
               (channel_id, owner_id, creator_id, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   owner_id = excluded.owner_id,
                   creator_id = COALESCE(
                       voice_rooms.creator_id, excluded.creator_id
                   )""",
            (channel_id, owner_id, original_creator_id, _utc_now()),
        )
        await db.commit()
        return True


async def set_voice_room_message(channel_id: int, message_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE voice_rooms SET message_id = ? WHERE channel_id = ?",
            (message_id, channel_id),
        )
        await db.commit()


async def get_voice_room(channel_id: int):
    async with _connect() as db:
        return await _one(
            db,
            "SELECT channel_id, owner_id, message_id FROM voice_rooms WHERE channel_id = ?",
            (channel_id,),
        )


async def get_voice_room_by_owner(owner_id: int):
    async with _connect() as db:
        return await _one(
            db,
            """SELECT channel_id, owner_id, message_id FROM voice_rooms
               WHERE owner_id = ?
                  OR (transfer_state = 'pending' AND previous_owner_id = ?)
               ORDER BY channel_id LIMIT 1""",
            (owner_id, owner_id),
        )


async def set_voice_room_owner(channel_id: int, owner_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE voice_rooms SET owner_id = ?, previous_owner_id = NULL,
                                      transfer_state = 'completed'
               WHERE channel_id = ?""",
            (owner_id, channel_id),
        )
        await db.commit()


async def transfer_voice_room_owner(
    channel_id: int, current_owner_id: int, new_owner_id: int
) -> bool:
    """Transfer ownership only when the target does not own another room."""
    async with _connect() as db:
        await begin_write(db, "voice-room", channel_id)
        existing = await _one(
            db,
            "SELECT channel_id FROM voice_rooms WHERE owner_id = ?",
            (new_owner_id,),
        )
        if existing is not None and existing[0] != channel_id:
            await db.rollback()
            return False
        cursor = await db.execute(
            """UPDATE voice_rooms SET owner_id = ?, previous_owner_id = ?,
                                      transfer_state = 'pending'
               WHERE channel_id = ? AND owner_id = ?""",
            (new_owner_id, current_owner_id, channel_id, current_owner_id),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            return False
        await db.commit()
        return True


async def complete_voice_room_transfer(channel_id: int, owner_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE voice_rooms SET transfer_state = 'completed',
                                      previous_owner_id = NULL
               WHERE channel_id = ? AND owner_id = ?""",
            (channel_id, owner_id),
        )
        await db.commit()
    return cursor.rowcount > 0


async def get_pending_voice_room_transfers():
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT channel_id, previous_owner_id, owner_id
               FROM voice_rooms WHERE transfer_state = 'pending'
               ORDER BY channel_id"""
        )


async def delete_voice_room(channel_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM voice_rooms WHERE channel_id = ?", (channel_id,))
        await db.commit()


async def get_all_voice_rooms():
    async with _connect() as db:
        async with db.execute(
            "SELECT channel_id, owner_id, message_id FROM voice_rooms"
        ) as cursor:
            return await cursor.fetchall()


async def get_voice_rooms_snapshot() -> list[tuple[int, int, int | None, bool, tuple[int, ...]]]:
    """Every room with its owner's VIP status and ban list, in three queries.

    The reconciler previously called ``get_voice_owner_bans`` and
    ``has_active_vip`` once per room — and ``has_active_vip`` goes through
    ``get_coin_account``, which performs an INSERT and a COMMIT. Thirty rooms
    meant sixty round trips and thirty writes every three minutes, for a job
    that usually changes nothing.

    Returns ``(channel_id, owner_id, message_id, owner_has_vip, banned_ids)``.
    """

    async with _connect() as db:
        rooms = await db.execute_fetchall(
            "SELECT channel_id, owner_id, message_id FROM voice_rooms"
        )
        if not rooms:
            return []
        owner_ids = sorted({int(row[1]) for row in rooms})
        placeholders = ",".join("?" for _ in owner_ids)
        vip_rows = await db.execute_fetchall(
            f"""SELECT user_id, vip_until FROM coin_accounts
                WHERE user_id IN ({placeholders})""",
            tuple(owner_ids),
        )
        ban_rows = await db.execute_fetchall(
            f"""SELECT owner_id, banned_user_id FROM voice_owner_bans
                WHERE owner_id IN ({placeholders})
                ORDER BY owner_id, created_at, banned_user_id""",
            tuple(owner_ids),
        )
    now = _utc_now()
    vip = {
        int(user_id): bool(vip_until and str(vip_until) > now)
        for user_id, vip_until in vip_rows
    }
    bans: dict[int, list[int]] = {}
    for owner_id, banned_user_id in ban_rows:
        bans.setdefault(int(owner_id), []).append(int(banned_user_id))
    return [
        (
            int(channel_id),
            int(owner_id),
            int(message_id) if message_id is not None else None,
            vip.get(int(owner_id), False),
            tuple(bans.get(int(owner_id), ())),
        )
        for channel_id, owner_id, message_id in rooms
    ]


async def add_voice_owner_ban(owner_id: int, banned_user_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT OR IGNORE INTO voice_owner_bans
               (owner_id, banned_user_id, created_at) VALUES (?, ?, ?)""",
            (owner_id, banned_user_id, _utc_now()),
        )
        await db.commit()


async def remove_voice_owner_ban(owner_id: int, banned_user_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM voice_owner_bans WHERE owner_id = ? AND banned_user_id = ?",
            (owner_id, banned_user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_voice_owner_bans(owner_id: int) -> list[int]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT banned_user_id FROM voice_owner_bans
               WHERE owner_id = ? ORDER BY created_at, banned_user_id""",
            (owner_id,),
        )
        return [row[0] for row in rows]


async def clear_voice_owner_bans(owner_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            "DELETE FROM voice_owner_bans WHERE owner_id = ?", (owner_id,)
        )
        await db.commit()
