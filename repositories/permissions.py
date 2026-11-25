"""Role grants for named domain permissions. A grant is on a role, not a person."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from infrastructure.database import connection as _connect


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    guild_id: int
    permission: str
    role_id: int


async def insert_grant(
    db: Any,
    *,
    guild_id: int,
    permission: str,
    role_id: int,
) -> bool:
    cursor = await db.execute(
        """INSERT INTO permission_grants (guild_id, permission, role_id)
           VALUES (?, ?, ?)
           ON CONFLICT(guild_id, permission, role_id) DO NOTHING""",
        (guild_id, permission, role_id),
    )
    return int(cursor.rowcount or 0) > 0


async def delete_grant(
    db: Any,
    *,
    guild_id: int,
    permission: str,
    role_id: int,
) -> bool:
    cursor = await db.execute(
        """DELETE FROM permission_grants
            WHERE guild_id = ? AND permission = ? AND role_id = ?""",
        (guild_id, permission, role_id),
    )
    return int(cursor.rowcount or 0) > 0


async def grant_permission(guild_id: int, permission: str, role_id: int) -> bool:
    async with _connect() as db:
        inserted = await insert_grant(
            db, guild_id=guild_id, permission=permission, role_id=role_id
        )
        await db.commit()
        return inserted


async def revoke_permission(guild_id: int, permission: str, role_id: int) -> bool:
    async with _connect() as db:
        removed = await delete_grant(
            db, guild_id=guild_id, permission=permission, role_id=role_id
        )
        await db.commit()
        return removed


async def list_role_permissions(guild_id: int, role_id: int) -> frozenset[str]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT permission FROM permission_grants
                WHERE guild_id = ? AND role_id = ?""",
            (guild_id, role_id),
        )
    return frozenset(str(row[0]) for row in rows)


async def list_permissions_for_roles(
    guild_id: int, role_ids: Iterable[int]
) -> frozenset[str]:
    ids = tuple(int(role_id) for role_id in role_ids)
    if not ids:
        return frozenset()
    placeholders = ", ".join("?" for _ in ids)
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT permission FROM permission_grants
                 WHERE guild_id = ? AND role_id IN ({placeholders})""",
            (guild_id, *ids),
        )
    return frozenset(str(row[0]) for row in rows)


async def list_grants(guild_id: int) -> list[PermissionGrant]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT guild_id, permission, role_id
                 FROM permission_grants
                WHERE guild_id = ?
                ORDER BY role_id, permission""",
            (guild_id,),
        )
    return [
        PermissionGrant(guild_id=int(row[0]), permission=str(row[1]), role_id=int(row[2]))
        for row in rows
    ]
