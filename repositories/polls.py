from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


@dataclass(frozen=True, slots=True)
class PollRecord:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    creator_id: int
    question: str
    options: tuple[str, ...]
    multiple: bool
    anonymous: bool
    closes_at: str
    status: str
    ui_revision: int


def _record(row) -> PollRecord | None:
    if row is None:
        return None
    return PollRecord(
        id=int(row[0]),
        guild_id=int(row[1]),
        channel_id=int(row[2]),
        message_id=int(row[3]) if row[3] is not None else None,
        creator_id=int(row[4]),
        question=str(row[5]),
        options=tuple(json.loads(row[6])),
        multiple=bool(row[7]),
        anonymous=bool(row[8]),
        closes_at=str(row[9]),
        status=str(row[10]),
        ui_revision=int(row[11]),
    )


async def create_poll(
    *,
    guild_id: int,
    channel_id: int,
    creator_id: int,
    question: str,
    options: list[str],
    multiple: bool,
    anonymous: bool,
    closes_at: str,
) -> int:
    now = _utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO polls
               (guild_id, channel_id, creator_id, question, options_json,
                multiple, anonymous, closes_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                channel_id,
                creator_id,
                question,
                json.dumps(options, ensure_ascii=False, separators=(",", ":")),
                int(multiple),
                int(anonymous),
                closes_at,
                now,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def set_poll_message(
    poll_id: int,
    message_id: int,
    *,
    synced_revision: int | None = None,
) -> None:
    async with _connect() as db:
        if synced_revision is None:
            await db.execute(
                "UPDATE polls SET message_id = ? WHERE id = ?", (message_id, poll_id)
            )
        else:
            await db.execute(
                """UPDATE polls SET message_id = ?, synced_revision = ?
                   WHERE id = ? AND synced_revision < ?""",
                (message_id, synced_revision, poll_id, synced_revision),
            )
        await db.commit()


async def delete_poll(poll_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
        await db.commit()


async def get_poll(poll_id: int) -> PollRecord | None:
    async with _connect() as db:
        async with db.execute(
            """SELECT id, guild_id, channel_id, message_id, creator_id,
                      question, options_json, multiple, anonymous, closes_at, status,
                      ui_revision
               FROM polls WHERE id = ?""",
            (poll_id,),
        ) as cursor:
            return _record(await cursor.fetchone())


async def get_active_polls() -> list[PollRecord]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, channel_id, message_id, creator_id,
                      question, options_json, multiple, anonymous, closes_at, status,
                      ui_revision
               FROM polls WHERE status = 'open' AND message_id IS NOT NULL"""
        )
    return [record for row in rows if (record := _record(row)) is not None]


async def get_poll_id_by_message(guild_id: int, message_id: int) -> int | None:
    """Resolve a persisted poll from a Discord deletion event."""

    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id FROM polls
               WHERE guild_id = ? AND message_id = ?
               LIMIT 1""",
            (guild_id, message_id),
        )
    return int(rows[0][0]) if rows else None


async def mark_poll_message_missing(
    poll_id: int,
    *,
    guild_id: int,
    message_id: int,
) -> bool:
    """Forget a deleted panel so the regular synchronizer recreates it.

    The message predicate prevents a late delete event for an old panel from
    clearing a newer replacement that has already been persisted.
    """

    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE polls
               SET message_id = NULL, synced_revision = -1
               WHERE id = ? AND guild_id = ? AND message_id = ?""",
            (poll_id, guild_id, message_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def mark_poll_messages_missing(
    guild_id: int, message_ids: set[int] | frozenset[int]
) -> tuple[int, ...]:
    """Batch the bulk-delete path through one pooled connection/transaction."""

    ids = tuple(dict.fromkeys(int(value) for value in message_ids))
    if not ids:
        return ()
    placeholders = ",".join("?" for _ in ids)
    async with _connect() as db:
        await begin_write(db, "poll-messages", guild_id)
        rows = await db.execute_fetchall(
            f"""SELECT id FROM polls WHERE guild_id = ?
                AND message_id IN ({placeholders})""",
            (guild_id, *ids),
        )
        if rows:
            poll_ids = tuple(int(row[0]) for row in rows)
            poll_placeholders = ",".join("?" for _ in poll_ids)
            await db.execute(
                f"""UPDATE polls SET message_id = NULL, synced_revision = -1
                    WHERE guild_id = ? AND id IN ({poll_placeholders})""",
                (guild_id, *poll_ids),
            )
        else:
            poll_ids = ()
        await db.commit()
    return poll_ids


async def mark_open_polls_dirty() -> int:
    """Force a startup fetch so deletions missed while offline are repaired."""

    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE polls SET synced_revision = -1 WHERE status = 'open'"
        )
        await db.commit()
    return cursor.rowcount


async def get_poll_results(poll_id: int) -> tuple[list[int], int]:
    poll = await get_poll(poll_id)
    if poll is None:
        return [], 0
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT option_index, COUNT(*) FROM poll_votes
               WHERE poll_id = ? GROUP BY option_index""",
            (poll_id,),
        )
        voters = await db.execute_fetchall(
            "SELECT COUNT(DISTINCT user_id) FROM poll_votes WHERE poll_id = ?",
            (poll_id,),
        )
    totals = [0] * len(poll.options)
    for option_index, total in rows:
        if 0 <= int(option_index) < len(totals):
            totals[int(option_index)] = int(total)
    return totals, int(voters[0][0]) if voters else 0


async def cast_poll_vote(
    poll_id: int, user_id: int, selected: tuple[int, ...]
) -> str:
    selected = tuple(dict.fromkeys(int(index) for index in selected))
    now = datetime.now(UTC)
    async with _connect() as db:
        await begin_write(db, "poll", poll_id)
        rows = await db.execute_fetchall(
            """SELECT options_json, multiple, status, closes_at
               FROM polls WHERE id = ?""",
            (poll_id,),
        )
        if not rows:
            await db.rollback()
            return "not_found"
        options = json.loads(rows[0][0])
        multiple = bool(rows[0][1])
        if rows[0][2] != "open" or datetime.fromisoformat(rows[0][3]) <= now:
            await db.execute(
                """UPDATE polls SET status = 'closed',
                                    closed_at = COALESCE(closed_at, ?),
                                    ui_revision = ui_revision + 1
                   WHERE id = ? AND status = 'open'""",
                (now.isoformat(), poll_id),
            )
            await db.commit()
            return "closed"
        if not selected or any(index < 0 or index >= len(options) for index in selected):
            await db.rollback()
            return "invalid"
        if not multiple and len(selected) != 1:
            await db.rollback()
            return "invalid"
        await db.execute(
            "DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        )
        await db.executemany(
            """INSERT INTO poll_votes (poll_id, user_id, option_index, voted_at)
               VALUES (?, ?, ?, ?)""",
            ((poll_id, user_id, index, now.isoformat()) for index in selected),
        )
        await db.execute(
            "UPDATE polls SET ui_revision = ui_revision + 1 WHERE id = ?",
            (poll_id,),
        )
        await db.commit()
    return "recorded"


async def close_poll(poll_id: int) -> bool:
    now = _utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE polls SET status = 'closed', closed_at = ?,
                                ui_revision = ui_revision + 1
               WHERE id = ? AND status = 'open'""",
            (now, poll_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def mark_expired_polls(now: str | None = None) -> int:
    timestamp = now or _utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE polls SET status = 'closed', closed_at = ?,
                                ui_revision = ui_revision + 1
               WHERE status = 'open' AND closes_at <= ?""",
            (timestamp, timestamp),
        )
        await db.commit()
        return cursor.rowcount


async def get_polls_needing_sync(*, limit: int = 100) -> list[PollRecord]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, channel_id, message_id, creator_id,
                      question, options_json, multiple, anonymous, closes_at, status,
                      ui_revision
               FROM polls
               WHERE message_id IS NULL OR synced_revision < ui_revision
               ORDER BY id LIMIT ?""",
            (max(1, min(int(limit), 1_000)),),
        )
    return [record for row in rows if (record := _record(row)) is not None]


async def mark_poll_synced(poll_id: int, revision: int, message_id: int) -> None:
    await set_poll_message(
        poll_id,
        message_id,
        synced_revision=revision,
    )


async def acknowledge_poll_revision(poll_id: int, revision: int) -> None:
    """Stop retrying a revision whose Discord destination no longer exists."""

    async with _connect() as db:
        await db.execute(
            """UPDATE polls SET synced_revision = MAX(synced_revision, ?)
               WHERE id = ?""",
            (revision, poll_id),
        )
        await db.commit()
