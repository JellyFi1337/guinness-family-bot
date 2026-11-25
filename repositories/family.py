"""Family context and internal ranks. Discord roles stay a separate link."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import connection as _connect, utc_now


@dataclass(frozen=True, slots=True)
class FamilyContext:
    id: int
    guild_id: int
    project: str
    server_key: str
    family_name: str
    family_tag: str
    direction: str
    member_limit: int | None
    created_at: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class Rank:
    id: int
    guild_id: int
    family_id: int
    name: str
    sort_order: int
    discord_role_id: int | None
    is_staff: bool


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _inserted_id(cursor: Any) -> int:
    row_id = int(cursor.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("insert did not return an id")
    return row_id


def _to_family(row: Any) -> FamilyContext:
    return FamilyContext(
        id=int(row[0]),
        guild_id=int(row[1]),
        project=str(row[2]),
        server_key=str(row[3]),
        family_name=str(row[4]),
        family_tag=str(row[5]),
        direction=str(row[6]),
        member_limit=_optional_int(row[7]),
        created_at=str(row[8]),
        archived_at=_optional_text(row[9]),
    )


def _to_rank(row: Any) -> Rank:
    return Rank(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        name=str(row[3]),
        sort_order=int(row[4]),
        discord_role_id=_optional_int(row[5]),
        is_staff=int(row[6]) == 1,
    )


_FAMILY_COLUMNS = """id, guild_id, project, server_key, family_name, family_tag,
                     direction, member_limit, created_at, archived_at"""
_RANK_COLUMNS = """id, guild_id, family_id, name, sort_order, discord_role_id,
                   is_staff"""


async def create_family_context(
    *,
    guild_id: int,
    project: str,
    server_key: str,
    family_name: str,
    family_tag: str,
    direction: str,
    member_limit: int | None = None,
    created_at: str | None = None,
) -> FamilyContext:
    stamp = created_at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO family_context (
                   guild_id, project, server_key, family_name, family_tag,
                   direction, member_limit, created_at, archived_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                guild_id,
                project,
                server_key,
                family_name,
                family_tag,
                direction,
                member_limit,
                stamp,
            ),
        )
        family_id = _inserted_id(cursor)
        await db.commit()
    family = await get_family(family_id)
    if family is None:
        raise RuntimeError("family_context insert did not persist")
    return family


async def archive_family_context(
    family_id: int, *, archived_at: str | None = None
) -> FamilyContext | None:
    stamp = archived_at or utc_now()
    async with _connect() as db:
        await db.execute(
            """UPDATE family_context
                  SET archived_at = ?
                WHERE id = ? AND archived_at IS NULL""",
            (stamp, family_id),
        )
        await db.commit()
    return await get_family(family_id)


async def get_family(family_id: int) -> FamilyContext | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_FAMILY_COLUMNS} FROM family_context WHERE id = ?",
            (family_id,),
        )
    return _to_family(rows[0]) if rows else None


async def get_active_family(guild_id: int) -> FamilyContext | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_FAMILY_COLUMNS}
                  FROM family_context
                 WHERE guild_id = ? AND archived_at IS NULL""",
            (guild_id,),
        )
    return _to_family(rows[0]) if rows else None


async def update_family_direction(
    family_id: int, direction: str
) -> FamilyContext | None:
    async with _connect() as db:
        await db.execute(
            """UPDATE family_context
                  SET direction = ?
                WHERE id = ? AND archived_at IS NULL""",
            (direction, family_id),
        )
        await db.commit()
    return await get_family(family_id)


async def create_rank(
    *,
    guild_id: int,
    family_id: int,
    name: str,
    sort_order: int,
    discord_role_id: int | None = None,
    is_staff: bool = False,
) -> Rank:
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO ranks (
                   guild_id, family_id, name, sort_order, discord_role_id, is_staff
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                family_id,
                name,
                sort_order,
                discord_role_id,
                1 if is_staff else 0,
            ),
        )
        rank_id = _inserted_id(cursor)
        await db.commit()
    rank = await get_rank(rank_id)
    if rank is None:
        raise RuntimeError("ranks insert did not persist")
    return rank


async def get_rank(rank_id: int) -> Rank | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_RANK_COLUMNS} FROM ranks WHERE id = ?",
            (rank_id,),
        )
    return _to_rank(rows[0]) if rows else None


async def list_ranks(family_id: int) -> list[Rank]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_RANK_COLUMNS}
                  FROM ranks
                 WHERE family_id = ?
                 ORDER BY sort_order, id""",
            (family_id,),
        )
    return [_to_rank(row) for row in rows]
