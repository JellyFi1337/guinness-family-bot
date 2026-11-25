from __future__ import annotations

import json
from dataclasses import dataclass

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write

_LEGACY_KINDS = ("vzp_gathering", "contract")


def _int_ids(rows: list[tuple[object, ...]]) -> tuple[int, ...]:
    return tuple(int(str(row[0])) for row in rows)


@dataclass(frozen=True, slots=True)
class SummitPanel:
    id: int
    guild_id: int | None
    channel_id: int
    message_id: int | None
    kind: str


async def get_summit_panels() -> list[SummitPanel]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, channel_id, message_id, kind
               FROM summit_lists ORDER BY id"""
        )
        position_rows = await db.execute_fetchall(
            """SELECT id, guild_id, channel_id, message_id, 'vzp_positions'
               FROM summit_vzp_lists ORDER BY id"""
        )
    return [
        SummitPanel(
            int(row[0]),
            int(row[1]) if row[1] is not None else None,
            int(row[2]),
            int(row[3]) if row[3] is not None else None,
            str(row[4]),
        )
        for row in (*rows, *position_rows)
    ]


async def clear_summit_message(
    list_id: int, *, expected_message_id: int | None = None
) -> bool:
    async with _connect() as db:
        if expected_message_id is None:
            cursor = await db.execute(
                "UPDATE summit_lists SET message_id = NULL WHERE id = ?",
                (list_id,),
            )
        else:
            cursor = await db.execute(
                """UPDATE summit_lists SET message_id = NULL
                   WHERE id = ? AND message_id = ?""",
                (list_id, expected_message_id),
            )
        await db.commit()
    return cursor.rowcount > 0


async def clear_vzp_position_message(
    list_id: int, *, expected_message_id: int | None = None
) -> bool:
    async with _connect() as db:
        if expected_message_id is None:
            cursor = await db.execute(
                "UPDATE summit_vzp_lists SET message_id = NULL WHERE id = ?",
                (list_id,),
            )
        else:
            cursor = await db.execute(
                """UPDATE summit_vzp_lists SET message_id = NULL
                   WHERE id = ? AND message_id = ?""",
                (list_id, expected_message_id),
            )
        await db.commit()
    return cursor.rowcount > 0


async def set_summit_thread(list_id: int, thread_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE summit_lists SET thread_id = ?
               WHERE id = ? AND kind = 'list'""",
            (thread_id, list_id),
        )
        await db.commit()
    return cursor.rowcount > 0


async def request_summit_launch(list_id: int, guild_id: int) -> str:
    """Persist a launch intent before any public Discord side effect."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        rows = await db.execute_fetchall(
            """SELECT launched, launch_state, guild_id, archived_at
               FROM summit_lists WHERE id = ?""",
            (list_id,),
        )
        if not rows:
            await db.rollback()
            return "missing"
        launched, state, stored_guild_id, archived_at = rows[0]
        if archived_at is not None:
            await db.rollback()
            return "archived"
        if launched or state == "completed":
            await db.rollback()
            return "launched"
        member_rows = await db.execute_fetchall(
            "SELECT 1 FROM summit_list_members WHERE list_id = ? LIMIT 1",
            (list_id,),
        )
        if not member_rows:
            await db.rollback()
            return "empty"
        effective_guild = int(stored_guild_id or guild_id)
        await db.execute(
            """UPDATE summit_lists SET launch_state = 'pending', is_open = 0,
                                      launch_requested_at = ?
               WHERE id = ?""",
            (now, list_id),
        )
        key = f"summit:{list_id}:launch"
        await db.execute(
            """INSERT OR IGNORE INTO operation_outbox
               (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                available_at, created_at, updated_at)
               VALUES (?, ?, 'summit.launch', ?, ?, ?, ?, ?)""",
            (
                key,
                effective_guild,
                str(list_id),
                json.dumps({"list_id": list_id}, separators=(",", ":")),
                now,
                now,
                now,
            ),
        )
        await db.execute(
            """UPDATE operation_outbox SET state = 'retry', available_at = ?,
                       lease_until = NULL, updated_at = ?
               WHERE idempotency_key = ? AND state = 'dead'""",
            (now, now, key),
        )
        await db.commit()
    return "queued" if state != "pending" else "pending"


async def complete_summit_launch(list_id: int, thread_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE summit_lists SET launched = 1, is_open = 0,
                       thread_id = ?, launched_at = COALESCE(launched_at, ?),
                       launch_state = 'completed'
               WHERE id = ? AND launch_state IN ('pending', 'completed')""",
            (thread_id, _utc_now(), list_id),
        )
        await db.commit()
    return cursor.rowcount > 0


@dataclass(frozen=True, slots=True)
class ArchivedLegacyGathering:
    id: int
    guild_id: int | None
    title: str
    kind: str
    creator_id: int
    channel_id: int
    message_id: int | None
    member_ids: tuple[int, ...]
    reserve_ids: tuple[int, ...]
    position_user_ids: tuple[int, ...]
    position_list_id: int | None


@dataclass(frozen=True, slots=True)
class ArchivedVzpList:
    id: int
    guild_id: int | None
    title: str
    creator_id: int
    channel_id: int
    message_id: int | None
    occupant_ids: tuple[int, ...]


async def list_unarchived_legacy_gathering_ids() -> list[int]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id FROM summit_lists
               WHERE archived_at IS NULL AND kind IN ('vzp_gathering', 'contract')
               ORDER BY id"""
        )
    return [int(row[0]) for row in rows]


async def list_unarchived_vzp_list_ids() -> list[int]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id FROM summit_vzp_lists
               WHERE archived_at IS NULL
               ORDER BY id"""
        )
    return [int(row[0]) for row in rows]


async def is_summit_list_archived(list_id: int) -> bool:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT archived_at FROM summit_lists WHERE id = ?",
            (list_id,),
        )
    return bool(rows) and rows[0][0] is not None


async def is_vzp_list_archived(list_id: int) -> bool:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT archived_at FROM summit_vzp_lists WHERE id = ?",
            (list_id,),
        )
        if rows and rows[0][0] is not None:
            return True
        parent = await db.execute_fetchall(
            """SELECT archived_at FROM summit_lists
               WHERE position_list_id = ?""",
            (list_id,),
        )
    return any(row[0] is not None for row in parent)


async def archive_legacy_gathering(list_id: int) -> ArchivedLegacyGathering | None:
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, title, kind, creator_id, channel_id, message_id,
                      position_list_id
               FROM summit_lists
               WHERE id = ? AND archived_at IS NULL AND kind IN (?, ?)""",
            (list_id, *_LEGACY_KINDS),
        )
        if not rows:
            await db.rollback()
            return None
        (
            row_id,
            guild_id,
            title,
            kind,
            creator_id,
            channel_id,
            message_id,
            position_list_id,
        ) = rows[0]
        member_rows = await db.execute_fetchall(
            """SELECT user_id FROM summit_list_members
               WHERE list_id = ? ORDER BY joined_at, user_id""",
            (list_id,),
        )
        reserve_rows = await db.execute_fetchall(
            """SELECT user_id FROM summit_list_reserves
               WHERE list_id = ? ORDER BY joined_at, user_id""",
            (list_id,),
        )
        position_rows = []
        if position_list_id is not None:
            position_rows = await db.execute_fetchall(
                """SELECT user_id FROM summit_vzp_positions
                   WHERE list_id = ? AND user_id IS NOT NULL
                   ORDER BY position""",
                (int(position_list_id),),
            )
        cursor = await db.execute(
            """UPDATE summit_lists
               SET archived_at = ?, is_open = 0
               WHERE id = ? AND archived_at IS NULL""",
            (now, list_id),
        )
        if cursor.rowcount <= 0:
            await db.rollback()
            return None
        if position_list_id is not None:
            await db.execute(
                """UPDATE summit_vzp_lists SET archived_at = ?
                   WHERE id = ? AND archived_at IS NULL""",
                (now, int(position_list_id)),
            )
        await db.commit()
    return ArchivedLegacyGathering(
        id=int(row_id),
        guild_id=int(guild_id) if guild_id is not None else None,
        title=str(title),
        kind=str(kind),
        creator_id=int(creator_id),
        channel_id=int(channel_id),
        message_id=int(message_id) if message_id is not None else None,
        member_ids=_int_ids(list(member_rows)),
        reserve_ids=_int_ids(list(reserve_rows)),
        position_user_ids=_int_ids(list(position_rows)),
        position_list_id=(
            int(position_list_id) if position_list_id is not None else None
        ),
    )


async def archive_vzp_position_list(list_id: int) -> ArchivedVzpList | None:
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "summit_vzp", list_id)
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, title, creator_id, channel_id, message_id
               FROM summit_vzp_lists
               WHERE id = ? AND archived_at IS NULL""",
            (list_id,),
        )
        if not rows:
            await db.rollback()
            return None
        row_id, guild_id, title, creator_id, channel_id, message_id = rows[0]
        occupant_rows = await db.execute_fetchall(
            """SELECT user_id FROM summit_vzp_positions
               WHERE list_id = ? AND user_id IS NOT NULL
               ORDER BY position""",
            (list_id,),
        )
        cursor = await db.execute(
            """UPDATE summit_vzp_lists SET archived_at = ?
               WHERE id = ? AND archived_at IS NULL""",
            (now, list_id),
        )
        if cursor.rowcount <= 0:
            await db.rollback()
            return None
        await db.commit()
    return ArchivedVzpList(
        id=int(row_id),
        guild_id=int(guild_id) if guild_id is not None else None,
        title=str(title),
        creator_id=int(creator_id),
        channel_id=int(channel_id),
        message_id=int(message_id) if message_id is not None else None,
        occupant_ids=_int_ids(list(occupant_rows)),
    )
