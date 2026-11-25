"""Stored birthdays for the family panel.

Extracted from the former monolithic ``utils.database``. The names are
re-exported from there, so existing imports keep working while new code
depends on this module directly.
"""

from __future__ import annotations

from utils.validation import normalize_birthday

from infrastructure.database import connection as _connect, utc_now as _utc_now

async def add_birthday(user_id: int, date: str, name: str | None = None) -> None:
    date = normalize_birthday(date)
    async with _connect() as db:
        await db.execute(
            """INSERT INTO birthdays (user_id, date, name, added_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   date = excluded.date,
                   name = excluded.name,
                   added_at = excluded.added_at""",
            (user_id, date, name, _utc_now()),
        )
        await db.commit()


async def get_all_birthdays() -> list[dict]:
    async with _connect() as db:
        async with db.execute(
            """SELECT user_id, date, name FROM birthdays
               ORDER BY CAST(substr(date, 4, 2) AS INTEGER),
                        CAST(substr(date, 1, 2) AS INTEGER)"""
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {"user_id": row[0], "date": row[1], "name": row[2] or "Участник"}
        for row in rows
    ]


async def remove_birthday(user_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute("DELETE FROM birthdays WHERE user_id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount > 0
