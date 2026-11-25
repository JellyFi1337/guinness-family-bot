from __future__ import annotations

from dataclasses import dataclass

from infrastructure.database import connection as _connect, utc_now as _utc_now


STATIC_PANELS_ENABLED_KEY = "static_panels_enabled"


@dataclass(frozen=True, slots=True)
class StaticMessageRecord:
    guild_id: int
    panel_key: str
    channel_id: int
    message_id: int
    revision: int
    asset_digest: str | None
    payload_digest: str | None
    updated_at: str = ""


def _record_from_row(row: tuple[object, ...]) -> StaticMessageRecord:
    updated = str(row[7]) if len(row) > 7 and row[7] is not None else ""
    return StaticMessageRecord(
        int(str(row[0])),
        str(row[1]),
        int(str(row[2])),
        int(str(row[3])),
        int(str(row[4])),
        None if row[5] is None else str(row[5]),
        None if row[6] is None else str(row[6]),
        updated,
    )


async def get_static_message(
    guild_id: int, panel_key: str
) -> StaticMessageRecord | None:
    async with _connect() as db:
        row = await db.execute_fetchall(
            """SELECT guild_id, panel_key, channel_id, message_id, revision,
                      asset_digest, payload_digest, updated_at
               FROM static_messages WHERE guild_id = ? AND panel_key = ?""",
            (guild_id, panel_key),
        )
    return _record_from_row(row[0]) if row else None


async def list_static_messages(guild_id: int) -> tuple[StaticMessageRecord, ...]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT guild_id, panel_key, channel_id, message_id, revision,
                      asset_digest, payload_digest, updated_at
               FROM static_messages WHERE guild_id = ?
               ORDER BY panel_key""",
            (guild_id,),
        )
    return tuple(_record_from_row(row) for row in rows)


async def save_static_message(
    guild_id: int,
    panel_key: str,
    channel_id: int,
    message_id: int,
    *,
    revision: int = 1,
    asset_digest: str | None = None,
    payload_digest: str | None = None,
) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO static_messages
               (guild_id, panel_key, channel_id, message_id, revision,
                asset_digest, payload_digest, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, panel_key) DO UPDATE SET
                   channel_id = excluded.channel_id,
                   message_id = excluded.message_id,
                   revision = excluded.revision,
                   asset_digest = excluded.asset_digest,
                   payload_digest = excluded.payload_digest,
                   updated_at = excluded.updated_at""",
            (
                guild_id,
                panel_key,
                channel_id,
                message_id,
                revision,
                asset_digest,
                payload_digest,
                _utc_now(),
            ),
        )
        await db.commit()


async def delete_static_message(guild_id: int, panel_key: str) -> None:
    async with _connect() as db:
        await db.execute(
            "DELETE FROM static_messages WHERE guild_id = ? AND panel_key = ?",
            (guild_id, panel_key),
        )
        await db.commit()


async def set_guild_setting(guild_id: int, key: str, value: str) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO guild_settings (guild_id, key, value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, key) DO UPDATE SET
                   value = excluded.value, updated_at = excluded.updated_at""",
            (guild_id, key, value, _utc_now()),
        )
        await db.commit()


async def get_guild_setting(guild_id: int, key: str) -> str | None:
    async with _connect() as db:
        row = await db.execute_fetchall(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
    return str(row[0][0]) if row else None


async def list_guild_settings(guild_id: int, *, prefix: str = "") -> dict[str, str]:
    async with _connect() as db:
        if prefix:
            rows = await db.execute_fetchall(
                """SELECT key, value FROM guild_settings
                    WHERE guild_id = ? AND key LIKE ?""",
                (guild_id, f"{prefix}%"),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT key, value FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            )
    return {str(key): str(value) for key, value in rows}


async def set_static_panels_enabled(guild_id: int, enabled: bool) -> None:
    await set_guild_setting(
        guild_id,
        STATIC_PANELS_ENABLED_KEY,
        "1" if enabled else "0",
    )


async def static_panels_enabled(guild_id: int) -> bool:
    """Panels are enabled by default until an explicit server reset disables them."""

    value = await get_guild_setting(guild_id, STATIC_PANELS_ENABLED_KEY)
    return value != "0"
