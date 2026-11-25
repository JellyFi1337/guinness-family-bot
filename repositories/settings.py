"""Persistent user-facing settings. Separate from guild_settings on purpose."""

from __future__ import annotations

from typing import Any

from infrastructure.database import connection as _connect, utc_now as _utc_now


async def get_runtime_setting(guild_id: int, key: str) -> str | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT value FROM runtime_settings
                WHERE guild_id = ? AND key = ?""",
            (guild_id, key),
        )
    return str(rows[0][0]) if rows else None


async def list_runtime_settings(guild_id: int) -> dict[str, str]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT key, value FROM runtime_settings WHERE guild_id = ?",
            (guild_id,),
        )
    return {str(key): str(value) for key, value in rows}


async def upsert_runtime_setting(
    db: Any,
    guild_id: int,
    key: str,
    value: str,
    *,
    updated_by: str | None,
) -> None:
    """Write one setting on an open connection. The caller owns the commit."""

    await db.execute(
        """INSERT INTO runtime_settings
               (guild_id, key, value, updated_by, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(guild_id, key) DO UPDATE SET
               value = excluded.value,
               updated_by = excluded.updated_by,
               updated_at = excluded.updated_at""",
        (guild_id, key, value, updated_by, _utc_now()),
    )


async def set_runtime_setting(
    guild_id: int,
    key: str,
    value: str,
    *,
    updated_by: str | None,
) -> None:
    async with _connect() as db:
        await upsert_runtime_setting(
            db, guild_id, key, value, updated_by=updated_by
        )
        await db.commit()


async def delete_runtime_setting_on(db: Any, guild_id: int, key: str) -> None:
    """Delete one setting on an open connection. The caller owns the commit."""

    await db.execute(
        "DELETE FROM runtime_settings WHERE guild_id = ? AND key = ?",
        (guild_id, key),
    )


async def delete_runtime_setting(guild_id: int, key: str) -> None:
    async with _connect() as db:
        await delete_runtime_setting_on(db, guild_id, key)
        await db.commit()
