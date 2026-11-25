from __future__ import annotations

from infrastructure.database import connection as _connect, utc_now as _utc_now


_SETTING_PREFIX = "command_cooldown_seconds:"


async def get_command_cooldown(
    guild_id: int,
    command_name: str,
) -> int | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT value FROM guild_settings
               WHERE guild_id = ? AND key = ?""",
            (guild_id, f"{_SETTING_PREFIX}{command_name}"),
        )
    if not rows:
        return None
    try:
        return max(0, int(rows[0][0]))
    except (TypeError, ValueError):
        return None


async def set_command_cooldown(
    guild_id: int,
    command_name: str,
    seconds: int,
) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO guild_settings (guild_id, key, value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (
                guild_id,
                f"{_SETTING_PREFIX}{command_name}",
                str(max(0, int(seconds))),
                _utc_now(),
            ),
        )
        await db.commit()
