"""Secret birthday fundraisers and their contributions.

Extracted from the former monolithic ``utils.database``. The names are
re-exported from there, so existing imports keep working while new code
depends on this module directly.
"""

from __future__ import annotations

from utils.validation import normalize_positive_amount

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write

async def create_fundraiser(
    target_user_id: int,
    target_username: str,
    channel_id: int,
    *,
    guild_id: int | None = None,
) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO birthday_fundraisers
               (target_user_id, target_username, channel_id, created_at, guild_id)
               VALUES (?, ?, ?, ?, ?)""",
            (target_user_id, target_username, channel_id, _utc_now(), guild_id),
        )
        await db.commit()
        return cursor.lastrowid


async def add_contribution(
    fundraiser_id: int, user_id: int, username: str, amount: int
) -> None:
    amount = normalize_positive_amount(amount)
    async with _connect() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await begin_write(db, "fundraiser", fundraiser_id)
        cursor = await db.execute(
            "UPDATE birthday_fundraisers SET total_amount = total_amount + ? WHERE id = ?",
            (amount, fundraiser_id),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            raise ValueError("Сбор не найден")
        await db.execute(
            """INSERT INTO fundraiser_contributions
               (fundraiser_id, user_id, username, amount, contributed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (fundraiser_id, user_id, username, amount, _utc_now()),
        )
        await db.commit()


async def get_fundraiser_by_channel(channel_id: int):
    async with _connect() as db:
        async with db.execute(
            """SELECT id, target_user_id, target_username, total_amount,
                      message_id, guild_id
               FROM birthday_fundraisers WHERE channel_id = ?""",
            (channel_id,),
        ) as cursor:
            return await cursor.fetchone()


async def set_fundraiser_message(fundraiser_id: int, message_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE birthday_fundraisers SET message_id = ? WHERE id = ?",
            (message_id, fundraiser_id),
        )
        await db.commit()


async def get_fundraiser_panels():
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT id, guild_id, channel_id, message_id
               FROM birthday_fundraisers"""
        )


async def delete_fundraiser_by_channel(channel_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            "DELETE FROM birthday_fundraisers WHERE channel_id = ?",
            (channel_id,),
        )
        await db.commit()


async def delete_contribution(fundraiser_id: int, username: str) -> bool:
    """Delete all contributions by a username and keep the cached total consistent."""
    async with _connect() as db:
        await begin_write(db, "fundraiser", fundraiser_id)
        async with db.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM fundraiser_contributions
               WHERE fundraiser_id = ? AND username = ?""",
            (fundraiser_id, username),
        ) as cursor:
            total = (await cursor.fetchone())[0]
        if total == 0:
            await db.rollback()
            return False
        await db.execute(
            "DELETE FROM fundraiser_contributions WHERE fundraiser_id = ? AND username = ?",
            (fundraiser_id, username),
        )
        await db.execute(
            """UPDATE birthday_fundraisers
               SET total_amount = MAX(0, total_amount - ?) WHERE id = ?""",
            (total, fundraiser_id),
        )
        await db.commit()
        return True
