from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


_RAW_BATCH_SIZE = 2_000
_MAX_BATCHES_PER_RUN = 5
_CLEANUP_BATCH_SIZE = 5_000


async def record_processed_action_once(
    guild_id: int,
    event_key: str,
    action_kind: str,
    outcome: str,
    *,
    actor_id: int | None = None,
) -> bool:
    """Record an audit statistic exactly once across Discord/outbox retries."""

    if action_kind not in {"ticket", "application"}:
        raise ValueError("Unsupported processed action kind")
    key = event_key.strip()
    if not key or len(key) > 200:
        raise ValueError("Invalid processed action event key")
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "stats-action", f"{guild_id}:{key}")
        try:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO stats_action_receipts
                   (guild_id, event_key, created_at) VALUES (?, ?, ?)""",
                (guild_id, key, now),
            )
            if cursor.rowcount == 1:
                await db.execute(
                    """INSERT INTO stats_processed_actions
                       (guild_id, action_kind, outcome, actor_id, processed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (guild_id, action_kind, outcome, actor_id, now),
                )
            await db.commit()
            return cursor.rowcount == 1
        except BaseException:
            await db.rollback()
            raise


def _split_voice_session(start: datetime, end: datetime):
    cursor = start
    while cursor < end:
        boundary = datetime.combine(
            cursor.date() + timedelta(days=1), time.min, tzinfo=UTC
        )
        segment_end = min(end, boundary)
        yield cursor.date().isoformat(), max(
            0, int((segment_end - cursor).total_seconds())
        )
        cursor = segment_end


async def _aggregate_messages(cutoff: str) -> int:
    async with _connect() as db:
        await begin_write(db, "stats-retention", "messages")
        try:
            await db.execute(
                """INSERT INTO stats_daily_messages
                   (guild_id, day, channel_id, user_id, total, deleted,
                    family_visible)
                   SELECT guild_id, substr(created_at, 1, 10), channel_id, user_id,
                          COUNT(*),
                          SUM(CASE WHEN deleted_at IS NULL THEN 0 ELSE 1 END),
                          MAX(COALESCE(family_visible, 0))
                   FROM stats_messages
                   WHERE message_id IN (
                       SELECT message_id FROM stats_messages
                       WHERE created_at < ? ORDER BY created_at, message_id LIMIT ?
                   )
                   GROUP BY guild_id, substr(created_at, 1, 10), channel_id, user_id
                   ON CONFLICT(guild_id, day, channel_id, user_id) DO UPDATE SET
                       total = stats_daily_messages.total + excluded.total,
                       deleted = stats_daily_messages.deleted + excluded.deleted,
                       family_visible = MAX(
                           COALESCE(stats_daily_messages.family_visible, 0),
                           COALESCE(excluded.family_visible, 0)
                       )""",
                (cutoff, _RAW_BATCH_SIZE),
            )
            cursor = await db.execute(
                """DELETE FROM stats_messages WHERE message_id IN (
                       SELECT message_id FROM stats_messages
                       WHERE created_at < ? ORDER BY created_at, message_id LIMIT ?
                   )""",
                (cutoff, _RAW_BATCH_SIZE),
            )
            await db.commit()
            return max(0, cursor.rowcount)
        except BaseException:
            await db.rollback()
            raise


async def _aggregate_members(cutoff: str) -> int:
    async with _connect() as db:
        await begin_write(db, "stats-retention", "members")
        try:
            await db.execute(
                """INSERT INTO stats_daily_members (guild_id, day, joined, left_count)
                   SELECT guild_id, substr(event_at, 1, 10),
                          SUM(CASE WHEN event_type = 'join' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN event_type = 'leave' THEN 1 ELSE 0 END)
                   FROM stats_member_events
                   WHERE id IN (
                       SELECT id FROM stats_member_events
                       WHERE event_at < ? ORDER BY event_at, id LIMIT ?
                   )
                   GROUP BY guild_id, substr(event_at, 1, 10)
                   ON CONFLICT(guild_id, day) DO UPDATE SET
                       joined = stats_daily_members.joined + excluded.joined,
                       left_count = stats_daily_members.left_count + excluded.left_count""",
                (cutoff, _RAW_BATCH_SIZE),
            )
            cursor = await db.execute(
                """DELETE FROM stats_member_events WHERE id IN (
                       SELECT id FROM stats_member_events
                       WHERE event_at < ? ORDER BY event_at, id LIMIT ?
                   )""",
                (cutoff, _RAW_BATCH_SIZE),
            )
            await db.commit()
            return max(0, cursor.rowcount)
        except BaseException:
            await db.rollback()
            raise


async def _aggregate_actions(cutoff: str) -> int:
    async with _connect() as db:
        await begin_write(db, "stats-retention", "actions")
        try:
            await db.execute(
                """INSERT INTO stats_daily_actions
                   (guild_id, day, action_kind, outcome, total)
                   SELECT guild_id, substr(processed_at, 1, 10), action_kind, outcome,
                          COUNT(*)
                   FROM stats_processed_actions
                   WHERE id IN (
                       SELECT id FROM stats_processed_actions
                       WHERE processed_at < ? ORDER BY processed_at, id LIMIT ?
                   )
                   GROUP BY guild_id, substr(processed_at, 1, 10), action_kind, outcome
                   ON CONFLICT(guild_id, day, action_kind, outcome) DO UPDATE SET
                       total = stats_daily_actions.total + excluded.total""",
                (cutoff, _RAW_BATCH_SIZE),
            )
            cursor = await db.execute(
                """DELETE FROM stats_processed_actions WHERE id IN (
                       SELECT id FROM stats_processed_actions
                       WHERE processed_at < ? ORDER BY processed_at, id LIMIT ?
                   )""",
                (cutoff, _RAW_BATCH_SIZE),
            )
            await db.commit()
            return max(0, cursor.rowcount)
        except BaseException:
            await db.rollback()
            raise


async def _aggregate_voice(cutoff: str) -> int:
    async with _connect() as db:
        await begin_write(db, "stats-retention", "voice")
        try:
            rows = await db.execute_fetchall(
                """SELECT id, guild_id, user_id, channel_id, channel_name,
                          room_creator_id, family_visible, started_at, ended_at
                   FROM stats_voice_sessions
                   WHERE ended_at IS NOT NULL AND ended_at < ?
                   ORDER BY ended_at, id LIMIT ?""",
                (cutoff, _RAW_BATCH_SIZE),
            )
            if not rows:
                await db.commit()
                return 0

            voice_totals: dict[tuple[int, str, int, int], int] = defaultdict(int)
            voice_metadata: dict[
                tuple[int, str, int, int], tuple[str, int | None, bool]
            ] = {}
            for (
                _,
                guild_id,
                user_id,
                channel_id,
                channel_name,
                room_creator_id,
                family_visible,
                started_at,
                ended_at,
            ) in rows:
                start = datetime.fromisoformat(started_at).astimezone(UTC)
                end = datetime.fromisoformat(ended_at).astimezone(UTC)
                for day, seconds in _split_voice_session(start, end):
                    key = (guild_id, day, channel_id, user_id)
                    voice_totals[key] += seconds
                    previous_name, previous_creator, previous_visible = (
                        voice_metadata.get(key, ("", None, False))
                    )
                    voice_metadata[key] = (
                        channel_name or previous_name,
                        room_creator_id
                        if room_creator_id is not None
                        else previous_creator,
                        previous_visible or family_visible == 1,
                    )
            await db.executemany(
                """INSERT INTO stats_daily_voice
                   (guild_id, day, channel_id, user_id, seconds,
                    channel_name, room_creator_id, family_visible)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, day, channel_id, user_id) DO UPDATE SET
                       seconds = stats_daily_voice.seconds + excluded.seconds,
                       channel_name = COALESCE(
                           excluded.channel_name, stats_daily_voice.channel_name
                       ),
                       room_creator_id = COALESCE(
                           excluded.room_creator_id,
                           stats_daily_voice.room_creator_id
                       ),
                       family_visible = MAX(
                           COALESCE(stats_daily_voice.family_visible, 0),
                           COALESCE(excluded.family_visible, 0)
                       )""",
                (
                    (
                        guild_id,
                        day,
                        channel_id,
                        user_id,
                        seconds,
                        voice_metadata[(guild_id, day, channel_id, user_id)][0],
                        voice_metadata[(guild_id, day, channel_id, user_id)][1],
                        int(
                            voice_metadata[
                                (guild_id, day, channel_id, user_id)
                            ][2]
                        ),
                    )
                    for (
                        guild_id,
                        day,
                        channel_id,
                        user_id,
                    ), seconds in voice_totals.items()
                ),
            )
            await db.executemany(
                "DELETE FROM stats_voice_sessions WHERE id = ?",
                ((row[0],) for row in rows),
            )
            await db.commit()
            return len(rows)
        except BaseException:
            await db.rollback()
            raise


async def spill_message_events(records) -> int:
    """Persist message events that did not fit the in-memory queue.

    Dropping them was a silent loss of Coins, because the same queue carried
    both statistics and the ``content_eligible`` flag that pays out. Writing
    here is deliberate backpressure: it only happens once the configured buffer
    is exhausted, and it is far cheaper than paying users incorrectly.
    """

    now = _utc_now()
    rows = [
        (
            int(record[0]),
            int(record[1]),
            int(record[2]),
            str(record[3]),
            int(record[4]),
            str(record[5]),
            1 if record[6] else 0,
            None if record[7] is None else int(bool(record[7])),
            now,
        )
        for record in records
    ]
    if not rows:
        return 0
    async with _connect() as db:
        await begin_write(db, "stats-spillover", "messages")
        try:
            await db.executemany(
                """INSERT OR IGNORE INTO message_event_spillover
                   (message_id, guild_id, channel_id, channel_name, user_id,
                    created_at, content_eligible, family_visible, spilled_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return len(rows)


async def drain_message_spillover(limit: int = 500) -> list[tuple]:
    """Claim a batch of spilled events and remove them from the table.

    Replayed through the normal ingest path, so ``coin_activity_ledger``'s
    uniqueness still guarantees each message pays out at most once.
    """

    limit = max(1, int(limit))
    async with _connect() as db:
        await begin_write(db, "stats-spillover", "messages")
        try:
            rows = await db.execute_fetchall(
                """SELECT message_id, guild_id, channel_id, channel_name, user_id,
                          created_at, content_eligible, family_visible
                     FROM message_event_spillover
                    ORDER BY spilled_at, message_id
                    LIMIT ?""",
                (limit,),
            )
            if rows:
                placeholders = ",".join("?" for _ in rows)
                await db.execute(
                    f"""DELETE FROM message_event_spillover
                        WHERE message_id IN ({placeholders})""",
                    tuple(int(row[0]) for row in rows),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return [
        (
            int(row[0]),
            int(row[1]),
            int(row[2]),
            str(row[3]),
            int(row[4]),
            str(row[5]),
            bool(row[6]),
            None if row[7] is None else bool(row[7]),
        )
        for row in rows
    ]


async def count_message_spillover() -> int:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM message_event_spillover"
        )
    return int(rows[0][0]) if rows else 0


async def _delete_raw_batch(table: str, column: str, cutoff: str) -> int:
    primary_keys = {
        ("stats_channel_events", "event_at"): ("id",),
        ("stats_pending_deletions", "deleted_at"): ("message_id",),
        ("stats_action_receipts", "created_at"): ("guild_id", "event_key"),
    }
    keys = primary_keys.get((table, column))
    if keys is None:
        raise ValueError("Unsupported statistics retention target")
    key_list = ", ".join(keys)
    key_expression = keys[0] if len(keys) == 1 else f"({key_list})"
    async with _connect() as db:
        cursor = await db.execute(
            f"""DELETE FROM {table} WHERE {key_expression} IN (
                    SELECT {key_list} FROM {table}
                    WHERE {column} < ?
                    ORDER BY {column}, {key_list} LIMIT ?
                )""",
            (cutoff, _CLEANUP_BATCH_SIZE),
        )
        await db.commit()
        return max(0, cursor.rowcount)


async def _delete_aggregate_batch(table: str, cutoff: str) -> int:
    primary_keys = {
        "stats_daily_messages": ("guild_id", "day", "channel_id", "user_id"),
        "stats_daily_voice": ("guild_id", "day", "channel_id", "user_id"),
        "stats_daily_members": ("guild_id", "day"),
        "stats_daily_actions": ("guild_id", "day", "action_kind", "outcome"),
    }
    keys = primary_keys.get(table)
    if keys is None:
        raise ValueError("Unsupported aggregate retention target")
    key_list = ", ".join(keys)
    key_expression = f"({key_list})"
    async with _connect() as db:
        cursor = await db.execute(
            f"""DELETE FROM {table} WHERE {key_expression} IN (
                    SELECT {key_list} FROM {table}
                    WHERE day < ? ORDER BY day, {key_list} LIMIT ?
                )""",
            (cutoff, _CLEANUP_BATCH_SIZE),
        )
        await db.commit()
        return max(0, cursor.rowcount)


async def _drain(worker: Callable[[str], Awaitable[int]], cutoff: str) -> int:
    total = 0
    for _ in range(_MAX_BATCHES_PER_RUN):
        deleted = await worker(cutoff)
        total += deleted
        if deleted < _RAW_BATCH_SIZE:
            break
        await asyncio.sleep(0)
    return total


async def _has_backlog(raw_cutoff: str, aggregate_cutoff: str) -> bool:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT
                 EXISTS(SELECT 1 FROM stats_messages WHERE created_at < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_member_events WHERE event_at < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_voice_sessions
                        WHERE ended_at IS NOT NULL AND ended_at < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_channel_events WHERE event_at < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_processed_actions WHERE processed_at < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_pending_deletions WHERE deleted_at < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_action_receipts WHERE created_at < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_daily_messages WHERE day < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_daily_voice WHERE day < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_daily_members WHERE day < ? LIMIT 1)
              OR EXISTS(SELECT 1 FROM stats_daily_actions WHERE day < ? LIMIT 1)""",
            (
                raw_cutoff,
                raw_cutoff,
                raw_cutoff,
                raw_cutoff,
                raw_cutoff,
                raw_cutoff,
                aggregate_cutoff,
                aggregate_cutoff,
                aggregate_cutoff,
                aggregate_cutoff,
                aggregate_cutoff,
            ),
        )
    return bool(rows and rows[0][0])


async def aggregate_and_prune_stats(
    *,
    raw_retention_days: int,
    aggregate_retention_days: int,
    force: bool = False,
) -> dict[str, int]:
    """Incrementally aggregate and prune statistics without long write locks.

    Each raw batch is aggregated and deleted in the same short transaction, so
    a crash cannot double-count it. Large historical backlogs are drained over
    multiple supervisor passes instead of blocking normal button writes.
    """

    now = datetime.now(UTC)
    today = now.date().isoformat()
    raw_cutoff = (now - timedelta(days=raw_retention_days)).isoformat()
    aggregate_cutoff = (
        now.date() - timedelta(days=aggregate_retention_days)
    ).isoformat()

    if not force:
        async with _connect() as db:
            state = await db.execute_fetchall(
                """SELECT value FROM stats_retention_state
                   WHERE key = 'last_daily_maintenance'"""
            )
        if state and state[0][0] == today:
            return {"skipped": 1}

    deleted = {
        "messages": await _drain(_aggregate_messages, raw_cutoff),
        "members": await _drain(_aggregate_members, raw_cutoff),
        "actions": await _drain(_aggregate_actions, raw_cutoff),
        "voice": await _drain(_aggregate_voice, raw_cutoff),
        "channels": await _delete_raw_batch(
            "stats_channel_events", "event_at", raw_cutoff
        ),
        "tombstones": await _delete_raw_batch(
            "stats_pending_deletions", "deleted_at", raw_cutoff
        ),
        "action_receipts": await _delete_raw_batch(
            # Idempotency receipts must outlive raw event rows so a delayed
            # outbox replay cannot increment a weekly/monthly aggregate twice.
            "stats_action_receipts", "created_at", aggregate_cutoff
        ),
    }
    for table in (
        "stats_daily_messages",
        "stats_daily_voice",
        "stats_daily_members",
        "stats_daily_actions",
    ):
        await _delete_aggregate_batch(table, aggregate_cutoff)

    backlog = await _has_backlog(raw_cutoff, aggregate_cutoff)
    deleted["backlog"] = int(backlog)
    if not backlog:
        async with _connect() as db:
            await db.execute(
                """INSERT INTO stats_retention_state (key, value, updated_at)
                   VALUES ('last_daily_maintenance', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_at = excluded.updated_at""",
                (today, _utc_now()),
            )
            await db.commit()
    return deleted
