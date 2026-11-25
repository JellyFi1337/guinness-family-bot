"""Staff-filled map catalog. Names, versions and sources are not seeded."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import (
    DATABASE_INTEGRITY_ERRORS,
    connection as _connect,
    utc_now,
)
from infrastructure.transactions import begin_write

_MAP_COLUMNS = (
    "id, guild_id, family_id, name, event_type, version, source, "
    "created_by, created_at, archived_at, archived_by"
)


@dataclass(frozen=True, slots=True)
class FamilyMap:
    id: int
    guild_id: int
    family_id: int
    name: str
    event_type: str
    version: str
    source: str
    created_by: int
    created_at: str
    archived_at: str | None
    archived_by: int | None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _inserted_id(cursor: Any) -> int:
    row_id = int(cursor.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("insert did not return an id")
    return row_id


def _from_row(row: Any) -> FamilyMap:
    return FamilyMap(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        name=str(row[3]),
        event_type=str(row[4]),
        version=str(row[5]),
        source=str(row[6]),
        created_by=int(row[7]),
        created_at=str(row[8]),
        archived_at=_optional_text(row[9]),
        archived_by=_optional_int(row[10]),
    )


async def insert_map(
    *,
    guild_id: int,
    family_id: int,
    name: str,
    event_type: str,
    version: str,
    source: str,
    created_by: int,
) -> FamilyMap:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "maps", family_id)
        try:
            cursor = await db.execute(
                """INSERT INTO family_maps
                    (guild_id, family_id, name, event_type, version, source,
                     created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    name,
                    event_type,
                    version,
                    source,
                    created_by,
                    stamp,
                ),
            )
            map_id = _inserted_id(cursor)
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            raise
    found = await get_map(guild_id, map_id)
    if found is None:
        raise RuntimeError("family map insert did not persist")
    return found


async def get_map(guild_id: int, map_id: int) -> FamilyMap | None:
    async with _connect() as db:
        cursor = await db.execute(
            f"""SELECT {_MAP_COLUMNS}
                FROM family_maps
                WHERE guild_id = ? AND id = ?""",
            (guild_id, map_id),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _from_row(row)


async def list_active_maps(guild_id: int, family_id: int) -> tuple[FamilyMap, ...]:
    async with _connect() as db:
        cursor = await db.execute(
            f"""SELECT {_MAP_COLUMNS}
                FROM family_maps
                WHERE guild_id = ? AND family_id = ? AND archived_at IS NULL
                ORDER BY name, id""",
            (guild_id, family_id),
        )
        rows = await cursor.fetchall()
    return tuple(_from_row(row) for row in rows)


async def archive_map(
    guild_id: int,
    map_id: int,
    *,
    archived_by: int,
) -> FamilyMap | None:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "maps", map_id)
        cursor = await db.execute(
            """UPDATE family_maps
               SET archived_at = ?, archived_by = ?
               WHERE id = ? AND guild_id = ? AND archived_at IS NULL""",
            (stamp, archived_by, map_id, guild_id),
        )
        await db.commit()
        if int(cursor.rowcount or 0) <= 0:
            return None
    return await get_map(guild_id, map_id)
