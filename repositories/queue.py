"""Queue overlay: assignment metadata only. Domain objects stay in their modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import (
    DATABASE_INTEGRITY_ERRORS,
    connection as _connect,
    utc_now,
)
from infrastructure.transactions import begin_write

CLAIM_METADATA_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "guild_id",
        "family_id",
        "item_kind",
        "item_id",
        "assignee_id",
        "claimed_at",
        "version",
        "escalated_to",
        "escalated_at",
        "delay_reason",
        "last_transfer_reason",
        "ping_user_id",
    }
)

_CLAIM_COLUMNS = (
    "id, guild_id, family_id, item_kind, item_id, assignee_id, claimed_at, "
    "version, escalated_to, escalated_at, delay_reason, last_transfer_reason, "
    "ping_user_id"
)
_APPEAL_COLUMNS = (
    "id, guild_id, family_id, author_id, accused_id, reason, status, created_at"
)


@dataclass(frozen=True, slots=True)
class QueueClaim:
    id: int
    guild_id: int
    family_id: int
    item_kind: str
    item_id: str
    assignee_id: int | None
    claimed_at: str | None
    version: int
    escalated_to: int | None
    escalated_at: str | None
    delay_reason: str | None
    last_transfer_reason: str | None
    ping_user_id: int | None


@dataclass(frozen=True, slots=True)
class QueueTransfer:
    id: int
    guild_id: int
    family_id: int
    item_kind: str
    item_id: str
    actor_id: int
    from_id: int | None
    to_id: int
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class StaffAppeal:
    id: int
    guild_id: int
    family_id: int
    author_id: int
    accused_id: int
    reason: str
    status: str
    created_at: str


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


def _to_claim(row: Any) -> QueueClaim:
    return QueueClaim(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        item_kind=str(row[3]),
        item_id=str(row[4]),
        assignee_id=_optional_int(row[5]),
        claimed_at=_optional_text(row[6]),
        version=int(row[7]),
        escalated_to=_optional_int(row[8]),
        escalated_at=_optional_text(row[9]),
        delay_reason=_optional_text(row[10]),
        last_transfer_reason=_optional_text(row[11]),
        ping_user_id=_optional_int(row[12]),
    )


def _to_appeal(row: Any) -> StaffAppeal:
    return StaffAppeal(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        author_id=int(row[3]),
        accused_id=int(row[4]),
        reason=str(row[5]),
        status=str(row[6]),
        created_at=str(row[7]),
    )


async def get_claim(
    family_id: int, item_kind: str, item_id: str
) -> QueueClaim | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_CLAIM_COLUMNS} FROM queue_claims
                WHERE family_id = ? AND item_kind = ? AND item_id = ?""",
            (family_id, item_kind, item_id),
        )
    return _to_claim(rows[0]) if rows else None


async def list_claims(family_id: int) -> list[QueueClaim]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_CLAIM_COLUMNS} FROM queue_claims
                WHERE family_id = ?""",
            (family_id,),
        )
    return [_to_claim(row) for row in rows]


async def list_claim_columns() -> tuple[str, ...]:
    async with _connect() as db:
        rows = await db.execute_fetchall("PRAGMA table_info(queue_claims)")
    return tuple(str(row[1]) for row in rows)


async def try_claim(
    *,
    guild_id: int,
    family_id: int,
    item_kind: str,
    item_id: str,
    assignee_id: int,
) -> QueueClaim | None:
    """Assign if free or already owned by the same person. None on conflict."""

    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "queue-claim", f"{item_kind}:{item_id}")
        rows = await db.execute_fetchall(
            f"""SELECT {_CLAIM_COLUMNS} FROM queue_claims
                WHERE family_id = ? AND item_kind = ? AND item_id = ?""",
            (family_id, item_kind, item_id),
        )
        if rows:
            current = _to_claim(rows[0])
            if current.assignee_id == assignee_id:
                await db.commit()
                return current
            if current.assignee_id is not None:
                await db.commit()
                return None
            cursor = await db.execute(
                """UPDATE queue_claims
                      SET assignee_id = ?, claimed_at = ?, version = version + 1,
                          ping_user_id = ?
                    WHERE id = ? AND assignee_id IS NULL""",
                (assignee_id, stamp, assignee_id, current.id),
            )
            await db.commit()
            if cursor.rowcount <= 0:
                return None
            return await get_claim(family_id, item_kind, item_id)
        try:
            cursor = await db.execute(
                """INSERT INTO queue_claims (
                       guild_id, family_id, item_kind, item_id, assignee_id,
                       claimed_at, version, ping_user_id
                   ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    guild_id,
                    family_id,
                    item_kind,
                    item_id,
                    assignee_id,
                    stamp,
                    assignee_id,
                ),
            )
            _inserted_id(cursor)
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            return None
    return await get_claim(family_id, item_kind, item_id)


async def transfer_claim(
    *,
    family_id: int,
    item_kind: str,
    item_id: str,
    actor_id: int,
    to_id: int,
    reason: str,
    expected_version: int,
) -> QueueClaim | None:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "queue-transfer", f"{item_kind}:{item_id}")
        rows = await db.execute_fetchall(
            f"""SELECT {_CLAIM_COLUMNS} FROM queue_claims
                WHERE family_id = ? AND item_kind = ? AND item_id = ?""",
            (family_id, item_kind, item_id),
        )
        if not rows:
            await db.commit()
            return None
        current = _to_claim(rows[0])
        if current.version != expected_version:
            await db.commit()
            return None
        cursor = await db.execute(
            """UPDATE queue_claims
                  SET assignee_id = ?, claimed_at = ?, version = version + 1,
                      last_transfer_reason = ?, ping_user_id = ?
                WHERE id = ? AND version = ?""",
            (to_id, stamp, reason, to_id, current.id, expected_version),
        )
        if cursor.rowcount <= 0:
            await db.commit()
            return None
        await db.execute(
            """INSERT INTO queue_transfers (
                   guild_id, family_id, item_kind, item_id, actor_id,
                   from_id, to_id, reason, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                current.guild_id,
                family_id,
                item_kind,
                item_id,
                actor_id,
                current.assignee_id,
                to_id,
                reason,
                stamp,
            ),
        )
        await db.commit()
    return await get_claim(family_id, item_kind, item_id)


async def apply_escalation(
    *,
    guild_id: int,
    family_id: int,
    item_kind: str,
    item_id: str,
    deputy_id: int | None,
    delay_reason: str,
) -> QueueClaim:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "queue-escalate", f"{item_kind}:{item_id}")
        rows = await db.execute_fetchall(
            f"""SELECT {_CLAIM_COLUMNS} FROM queue_claims
                WHERE family_id = ? AND item_kind = ? AND item_id = ?""",
            (family_id, item_kind, item_id),
        )
        if rows:
            current = _to_claim(rows[0])
            if deputy_id is None:
                await db.execute(
                    """UPDATE queue_claims
                          SET delay_reason = ?, version = version + 1
                        WHERE id = ?""",
                    (delay_reason, current.id),
                )
            else:
                await db.execute(
                    """UPDATE queue_claims
                          SET assignee_id = ?, claimed_at = ?, version = version + 1,
                              escalated_to = ?, escalated_at = ?,
                              delay_reason = ?, ping_user_id = ?
                        WHERE id = ?""",
                    (
                        deputy_id,
                        stamp,
                        deputy_id,
                        stamp,
                        delay_reason,
                        deputy_id,
                        current.id,
                    ),
                )
            await db.commit()
            loaded = await get_claim(family_id, item_kind, item_id)
            if loaded is None:
                raise RuntimeError("escalation did not persist")
            return loaded
        await db.execute(
            """INSERT INTO queue_claims (
                   guild_id, family_id, item_kind, item_id, assignee_id,
                   claimed_at, version, escalated_to, escalated_at,
                   delay_reason, ping_user_id
               ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (
                guild_id,
                family_id,
                item_kind,
                item_id,
                deputy_id,
                stamp if deputy_id is not None else None,
                deputy_id,
                stamp if deputy_id is not None else None,
                delay_reason,
                deputy_id,
            ),
        )
        await db.commit()
    loaded = await get_claim(family_id, item_kind, item_id)
    if loaded is None:
        raise RuntimeError("escalation insert did not persist")
    return loaded


async def insert_appeal(
    *,
    guild_id: int,
    family_id: int,
    author_id: int,
    accused_id: int,
    reason: str,
) -> StaffAppeal:
    stamp = utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO staff_appeals (
                   guild_id, family_id, author_id, accused_id, reason,
                   status, created_at
               ) VALUES (?, ?, ?, ?, ?, 'open', ?)""",
            (guild_id, family_id, author_id, accused_id, reason, stamp),
        )
        item_id = _inserted_id(cursor)
        await db.commit()
    item = await get_appeal(item_id)
    if item is None:
        raise RuntimeError("appeal insert did not persist")
    return item


async def get_appeal(appeal_id: int) -> StaffAppeal | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_APPEAL_COLUMNS} FROM staff_appeals WHERE id = ?",
            (appeal_id,),
        )
    return _to_appeal(rows[0]) if rows else None


async def list_open_appeals(family_id: int) -> list[StaffAppeal]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_APPEAL_COLUMNS} FROM staff_appeals
                WHERE family_id = ? AND status = 'open'
                ORDER BY created_at, id""",
            (family_id,),
        )
    return [_to_appeal(row) for row in rows]


async def list_open_appeals_for_author(
    family_id: int, author_id: int
) -> list[StaffAppeal]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_APPEAL_COLUMNS} FROM staff_appeals
                WHERE family_id = ? AND author_id = ? AND status = 'open'
                ORDER BY created_at, id""",
            (family_id, author_id),
        )
    return [_to_appeal(row) for row in rows]
