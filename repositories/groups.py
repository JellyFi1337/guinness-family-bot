from __future__ import annotations

import json

from infrastructure.database import connection, utc_now


async def save_private_group(
    *,
    channel_id: int,
    guild_id: int,
    creator_id: int,
    member_ids: list[int],
    role_ids: list[int],
) -> None:
    async with connection() as db:
        await db.execute(
            """INSERT INTO private_group_channels
               (channel_id, guild_id, creator_id, member_ids_json,
                role_ids_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   creator_id = excluded.creator_id,
                   member_ids_json = excluded.member_ids_json,
                   role_ids_json = excluded.role_ids_json""",
            (
                channel_id,
                guild_id,
                creator_id,
                json.dumps(member_ids),
                json.dumps(role_ids),
                utc_now(),
            ),
        )
        await db.commit()
