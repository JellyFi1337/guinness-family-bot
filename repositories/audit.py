"""Decision log. Append-only from the public API: no row update or delete-by-id."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import connection as _connect


@dataclass(frozen=True, slots=True)
class Decision:
    id: int
    guild_id: int
    created_at: str
    correlation_id: str
    actor_id: int
    actor_name: str
    action: str
    subject_type: str
    subject_id: str
    before: str | None
    after: str | None
    reason: str | None
    source: str


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _to_decision(row: Any) -> Decision:
    return Decision(
        id=int(row[0]),
        guild_id=int(row[1]),
        created_at=str(row[2]),
        correlation_id=str(row[3]),
        actor_id=int(row[4]),
        actor_name=str(row[5]),
        action=str(row[6]),
        subject_type=str(row[7]),
        subject_id=str(row[8]),
        before=_optional_text(row[9]),
        after=_optional_text(row[10]),
        reason=_optional_text(row[11]),
        source=str(row[12]),
    )


_COLUMNS = """id, guild_id, created_at, correlation_id, actor_id, actor_name,
              action, subject_type, subject_id, before, after, reason, source"""


async def insert_decision(
    db: Any,
    *,
    guild_id: int,
    created_at: str,
    correlation_id: str,
    actor_id: int,
    actor_name: str,
    action: str,
    subject_type: str,
    subject_id: str,
    before: str | None,
    after: str | None,
    reason: str | None,
    source: str,
) -> int:
    cursor = await db.execute(
        """INSERT INTO decision_log (
               guild_id, created_at, correlation_id, actor_id, actor_name,
               action, subject_type, subject_id, before, after, reason, source
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            guild_id,
            created_at,
            correlation_id,
            actor_id,
            actor_name,
            action,
            subject_type,
            subject_id,
            before,
            after,
            reason,
            source,
        ),
    )
    decision_id = int(cursor.lastrowid or 0)
    if decision_id <= 0:
        raise RuntimeError("decision_log insert did not return an id")
    return decision_id


async def get_decision(guild_id: int, decision_id: int) -> Decision | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_COLUMNS}
                  FROM decision_log
                 WHERE guild_id = ? AND id = ?""",
            (guild_id, decision_id),
        )
    return _to_decision(rows[0]) if rows else None


async def list_decisions(
    guild_id: int,
    *,
    actor_id: int | None = None,
    action: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Decision]:
    clauses = ["guild_id = ?"]
    params: list[object] = [guild_id]
    if actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(actor_id)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if subject_type:
        clauses.append("subject_type = ?")
        params.append(subject_type)
    if subject_id:
        clauses.append("subject_id = ?")
        params.append(subject_id)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if until:
        clauses.append("created_at <= ?")
        params.append(until)
    params.extend((max(1, int(limit)), max(0, int(offset))))
    where = " AND ".join(clauses)
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_COLUMNS}
                  FROM decision_log
                 WHERE {where}
                 ORDER BY created_at DESC, id DESC
                 LIMIT ? OFFSET ?""",
            tuple(params),
        )
    return [_to_decision(row) for row in rows]


async def prune_expired_decisions(*, older_than: str) -> int:
    """Bulk retention cleanup. There is no public delete of a single row."""

    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM decision_log WHERE created_at < ?",
            (older_than,),
        )
        await db.commit()
        return int(cursor.rowcount or 0)
