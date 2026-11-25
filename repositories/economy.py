from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable, Sequence

import aiosqlite

from config import (
    COINS_MESSAGE_DAILY_CAP,
    COINS_MESSAGE_COOLDOWN_SECONDS,
    COINS_MESSAGE_REWARD_MILLI,
    COINS_VOICE_DAILY_CAP,
    COINS_VOICE_REWARD_MILLI,
)
from infrastructure.database import connection, database_backend, utc_now


MESSAGE_REWARD_KEY = "economy_message_reward_milli"
VOICE_REWARD_KEY = "economy_voice_reward_milli"
MESSAGE_COOLDOWN_KEY = "economy_message_cooldown_seconds"


@dataclass(frozen=True, slots=True)
class EconomySettings:
    message_reward_milli: int
    voice_reward_milli: int
    message_cooldown_seconds: int
    message_daily_cap: int = COINS_MESSAGE_DAILY_CAP
    voice_daily_cap: int = COINS_VOICE_DAILY_CAP


async def get_economy_settings(guild_id: int) -> EconomySettings:
    async with connection() as db:
        rows = await db.execute_fetchall(
            """SELECT key, value FROM guild_settings
               WHERE guild_id = ? AND key IN (?, ?, ?)""",
            (guild_id, MESSAGE_REWARD_KEY, VOICE_REWARD_KEY, MESSAGE_COOLDOWN_KEY),
        )
    values = {str(row[0]): str(row[1]) for row in rows}
    return EconomySettings(
        message_reward_milli=_setting_int(
            values.get(MESSAGE_REWARD_KEY), COINS_MESSAGE_REWARD_MILLI, 0, 1_000_000_000
        ),
        voice_reward_milli=_setting_int(
            values.get(VOICE_REWARD_KEY), COINS_VOICE_REWARD_MILLI, 0, 1_000_000_000
        ),
        message_cooldown_seconds=_setting_int(
            values.get(MESSAGE_COOLDOWN_KEY), COINS_MESSAGE_COOLDOWN_SECONDS, 0, 3600
        ),
        message_daily_cap=COINS_MESSAGE_DAILY_CAP,
        voice_daily_cap=COINS_VOICE_DAILY_CAP,
    )


def _setting_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        return default
    return min(maximum, max(minimum, parsed))


async def set_economy_settings(guild_id: int, settings: EconomySettings) -> None:
    now = utc_now()
    async with connection() as db:
        await db.executemany(
            """INSERT INTO guild_settings (guild_id, key, value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, key) DO UPDATE SET
                   value = excluded.value, updated_at = excluded.updated_at""",
            (
                (guild_id, MESSAGE_REWARD_KEY, str(settings.message_reward_milli), now),
                (guild_id, VOICE_REWARD_KEY, str(settings.voice_reward_milli), now),
                (
                    guild_id,
                    MESSAGE_COOLDOWN_KEY,
                    str(settings.message_cooldown_seconds),
                    now,
                ),
            ),
        )
        await db.commit()


async def _ensure_state(
    db: aiosqlite.Connection, guild_id: int, user_id: int
) -> tuple[int, str | None]:
    await db.execute(
        """INSERT OR IGNORE INTO coin_activity_state
           (guild_id, user_id, pending_milli)
           VALUES (?, ?, 0)""",
        (guild_id, user_id),
    )
    lock = " FOR UPDATE" if database_backend() == "postgresql" else ""
    row = list(
        await db.execute_fetchall(
            f"""SELECT pending_milli, last_message_reward_at
                FROM coin_activity_state
                WHERE guild_id = ? AND user_id = ?{lock}""",
            (guild_id, user_id),
        )
    )
    return int(row[0][0]), row[0][1]


async def _credit_activity(
    db: aiosqlite.Connection,
    *,
    guild_id: int,
    user_id: int,
    source: str,
    event_key: str,
    units_milli: int,
    occurred_at: str,
    eligible: bool,
    update_message_time: bool = False,
    daily_coin_cap: int = 0,
) -> int | None:
    cursor = await db.execute(
        """INSERT OR IGNORE INTO coin_activity_ledger
           (guild_id, user_id, source, event_key, units_milli,
            coins_awarded, eligible, occurred_at)
           VALUES (?, ?, ?, ?, 0, 0, ?, ?)""",
        (guild_id, user_id, source, event_key, int(eligible), occurred_at),
    )
    if cursor.rowcount == 0:
        return None

    pending, _ = await _ensure_state(db, guild_id, user_id)
    awarded = 0
    if eligible and units_milli > 0:
        total = pending + units_milli
        awarded, pending = divmod(total, 1000)
        if daily_coin_cap:
            day = datetime.fromisoformat(occurred_at).astimezone(UTC).date().isoformat()
            rows = list(
                await db.execute_fetchall(
                    """SELECT coins_awarded FROM coin_activity_daily
                       WHERE guild_id = ? AND day = ? AND user_id = ?
                         AND source = ?""",
                    (guild_id, day, user_id, source),
                )
            )
            already_awarded = int(rows[0][0]) if rows else 0
            remaining = max(0, daily_coin_cap - already_awarded)
            if awarded > remaining:
                awarded = remaining
                pending = 0
        await db.execute(
            """UPDATE coin_activity_state
               SET pending_milli = ?,
                   last_message_reward_at = CASE
                       WHEN ? = 1 THEN ?
                       ELSE last_message_reward_at
                   END
               WHERE guild_id = ? AND user_id = ?""",
            (pending, int(update_message_time), occurred_at, guild_id, user_id),
        )
        if awarded:
            await db.execute(
                """INSERT INTO coin_accounts (user_id, balance)
                   VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       balance = coin_accounts.balance + excluded.balance""",
                (user_id, awarded),
            )
    elif update_message_time:
        await db.execute(
            """UPDATE coin_activity_state SET last_message_reward_at = ?
               WHERE guild_id = ? AND user_id = ?""",
            (occurred_at, guild_id, user_id),
        )

    await db.execute(
        """UPDATE coin_activity_ledger
           SET units_milli = ?, coins_awarded = ?, eligible = ?
           WHERE source = ? AND event_key = ?""",
        (units_milli if eligible else 0, awarded, int(eligible), source, event_key),
    )
    day = datetime.fromisoformat(occurred_at).astimezone(UTC).date().isoformat()
    await db.execute(
        """INSERT INTO coin_activity_daily
           (guild_id, day, user_id, source, units_milli, coins_awarded,
            events, eligible_events)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT(guild_id, day, user_id, source) DO UPDATE SET
               units_milli =
                   coin_activity_daily.units_milli + excluded.units_milli,
               coins_awarded =
                   coin_activity_daily.coins_awarded + excluded.coins_awarded,
               events = coin_activity_daily.events + 1,
               eligible_events =
                   coin_activity_daily.eligible_events
                   + excluded.eligible_events""",
        (
            guild_id,
            day,
            user_id,
            source,
            units_milli if eligible else 0,
            awarded,
            int(eligible),
        ),
    )
    return awarded


async def award_message_activity(
    records: Sequence[tuple],
    settings_by_guild: dict[int, EconomySettings],
) -> int:
    """Idempotently award a gateway batch in one SQLite transaction."""

    normalised = [
        (
            int(record[0]),
            int(record[1]),
            int(record[2]),
            str(record[3]),
            int(record[4]),
            str(record[5]),
            bool(record[6]) if len(record) > 6 else True,
        )
        for record in records
    ]
    unique = {str(record[0]): record for record in normalised}
    if not unique:
        return 0
    async with connection() as db:
        postgres = database_backend() == "postgresql"
        await db.execute("BEGIN" if postgres else "BEGIN IMMEDIATE")
        try:
            existing: set[str] = set()
            event_keys = list(unique)
            for offset in range(0, len(event_keys), 900):
                batch = event_keys[offset : offset + 900]
                placeholders = ",".join("?" for _ in batch)
                rows = await db.execute_fetchall(
                    f"""SELECT event_key FROM coin_activity_ledger
                        WHERE source = 'message' AND event_key IN ({placeholders})""",
                    batch,
                )
                existing.update(str(row[0]) for row in rows)
            pending_records = [
                record for key, record in unique.items() if key not in existing
            ]
            if not pending_records:
                await db.rollback()
                return 0

            states: dict[tuple[int, int], tuple[int, str | None]] = {}
            users_by_guild: dict[int, set[int]] = defaultdict(set)
            for (
                _message_id,
                guild_id,
                _channel,
                _name,
                user_id,
                _at,
                _eligible,
            ) in pending_records:
                users_by_guild[int(guild_id)].add(int(user_id))
            for guild_id, user_ids in users_by_guild.items():
                ordered_user_ids = list(user_ids)
                # Distinct from the event-key batch above: one name for two
                # element types is what made this loop untypeable.
                for offset in range(0, len(ordered_user_ids), 900):
                    user_batch = ordered_user_ids[offset : offset + 900]
                    if postgres:
                        await db.executemany(
                            """INSERT INTO coin_activity_state
                               (guild_id, user_id, pending_milli)
                               VALUES (?, ?, 0)
                               ON CONFLICT(guild_id, user_id) DO NOTHING""",
                            ((guild_id, user_id) for user_id in user_batch),
                        )
                    placeholders = ",".join("?" for _ in user_batch)
                    rows = await db.execute_fetchall(
                        f"""SELECT user_id, pending_milli, last_message_reward_at
                            FROM coin_activity_state
                            WHERE guild_id = ? AND user_id IN ({placeholders})
                            {"FOR UPDATE" if postgres else ""}""",
                        (guild_id, *user_batch),
                    )
                    states.update(
                        {
                            (guild_id, int(row[0])): (int(row[1]), row[2])
                            for row in rows
                        }
                    )

            ledger_rows: list[tuple[object, ...]] = []
            daily: dict[tuple[int, str, int, str], list[int]] = defaultdict(
                lambda: [0, 0, 0, 0]
            )
            daily_awarded: dict[tuple[int, str, int], int] = {}
            days_by_guild: dict[int, set[str]] = defaultdict(set)
            for record in pending_records:
                day = (
                    datetime.fromisoformat(record[5])
                    .astimezone(UTC)
                    .date()
                    .isoformat()
                )
                days_by_guild[record[1]].add(day)
            for guild_id, days in days_by_guild.items():
                day_values = list(days)
                user_values = list(users_by_guild[guild_id])
                day_placeholders = ",".join("?" for _ in day_values)
                user_placeholders = ",".join("?" for _ in user_values)
                rows = await db.execute_fetchall(
                    f"""SELECT day, user_id, coins_awarded
                        FROM coin_activity_daily
                        WHERE guild_id = ? AND source = 'message'
                          AND day IN ({day_placeholders})
                          AND user_id IN ({user_placeholders})""",
                    (guild_id, *day_values, *user_values),
                )
                daily_awarded.update(
                    {
                        (guild_id, str(row[0]), int(row[1])): int(row[2])
                        for row in rows
                    }
                )
            credits: dict[int, int] = defaultdict(int)
            for (
                message_id,
                guild_id,
                _channel_id,
                _channel_name,
                user_id,
                occurred_at,
                content_eligible,
            ) in sorted(
                pending_records, key=lambda item: item[5]
            ):
                settings = settings_by_guild[guild_id]
                state_key = (guild_id, user_id)
                pending, last_reward = states.get(state_key, (0, None))
                eligible = content_eligible
                if last_reward and settings.message_cooldown_seconds:
                    elapsed = (
                        datetime.fromisoformat(occurred_at)
                        - datetime.fromisoformat(last_reward)
                    ).total_seconds()
                    eligible = (
                        eligible
                        and elapsed >= settings.message_cooldown_seconds
                    )
                day = (
                    datetime.fromisoformat(occurred_at)
                    .astimezone(UTC)
                    .date()
                    .isoformat()
                )
                cap_key = (guild_id, day, user_id)
                if (
                    eligible
                    and settings.message_daily_cap
                    and daily_awarded.get(cap_key, 0)
                    >= settings.message_daily_cap
                ):
                    eligible = False
                units = settings.message_reward_milli if eligible else 0
                awarded = 0
                if eligible:
                    awarded, pending = divmod(pending + units, 1000)
                    if settings.message_daily_cap:
                        remaining = max(
                            0,
                            settings.message_daily_cap
                            - daily_awarded.get(cap_key, 0),
                        )
                        if awarded > remaining:
                            awarded = remaining
                            pending = 0
                    last_reward = occurred_at
                    states[state_key] = (pending, last_reward)
                    credits[user_id] += awarded
                    daily_awarded[cap_key] = (
                        daily_awarded.get(cap_key, 0) + awarded
                    )
                ledger_rows.append(
                    (
                        guild_id,
                        user_id,
                        "message",
                        str(message_id),
                        units,
                        awarded,
                        int(eligible),
                        occurred_at,
                    )
                )
                aggregate = daily[(guild_id, day, user_id, "message")]
                aggregate[0] += units
                aggregate[1] += awarded
                aggregate[2] += 1
                aggregate[3] += int(eligible)

            await db.executemany(
                """INSERT INTO coin_activity_ledger
                   (guild_id, user_id, source, event_key, units_milli,
                    coins_awarded, eligible, occurred_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ledger_rows,
            )
            await db.executemany(
                """INSERT INTO coin_activity_state
                   (guild_id, user_id, pending_milli, last_message_reward_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET
                       pending_milli = excluded.pending_milli,
                       last_message_reward_at = excluded.last_message_reward_at""",
                (
                    (guild_id, user_id, pending, last_reward)
                    for (guild_id, user_id), (pending, last_reward) in states.items()
                ),
            )
            await db.executemany(
                """INSERT INTO coin_accounts (user_id, balance)
                   VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       balance = coin_accounts.balance + excluded.balance""",
                ((user_id, amount) for user_id, amount in credits.items() if amount),
            )
            await db.executemany(
                """INSERT INTO coin_activity_daily
                   (guild_id, day, user_id, source, units_milli, coins_awarded,
                    events, eligible_events)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, day, user_id, source) DO UPDATE SET
                       units_milli =
                           coin_activity_daily.units_milli
                           + excluded.units_milli,
                       coins_awarded =
                           coin_activity_daily.coins_awarded
                           + excluded.coins_awarded,
                       events =
                           coin_activity_daily.events + excluded.events,
                       eligible_events =
                           coin_activity_daily.eligible_events
                           + excluded.eligible_events""",
                (
                    (*key, *values)
                    for key, values in daily.items()
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return sum(credits.values())


async def sync_voice_activity(
    guild_id: int,
    active_members: Iterable[tuple[int, int]],
    settings: EconomySettings,
    *,
    reset: bool = False,
    now: datetime | None = None,
    observed_user_ids: Iterable[int] | None = None,
) -> int:
    current = {int(user_id): int(channel_id) for user_id, channel_id in active_members}
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    occurred_at = moment.isoformat()
    awarded_total = 0
    async with connection() as db:
        postgres = database_backend() == "postgresql"
        await db.execute("BEGIN" if postgres else "BEGIN IMMEDIATE")
        try:
            if reset:
                await db.execute("DELETE FROM coin_voice_sessions WHERE guild_id = ?", (guild_id,))
                await db.executemany(
                    """INSERT INTO coin_voice_sessions
                       (guild_id, user_id, channel_id, last_accrued_at)
                       VALUES (?, ?, ?, ?)""",
                    ((guild_id, user_id, channel_id, occurred_at) for user_id, channel_id in current.items()),
                )
                await db.commit()
                return 0

            observed = (
                {int(user_id) for user_id in observed_user_ids}
                if observed_user_ids is not None
                else None
            )
            if observed is not None and not observed:
                await db.commit()
                return 0
            if postgres and current:
                await db.executemany(
                    """INSERT INTO coin_voice_sessions
                       (guild_id, user_id, channel_id, last_accrued_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(guild_id, user_id) DO NOTHING""",
                    (
                        (guild_id, user_id, channel_id, occurred_at)
                        for user_id, channel_id in current.items()
                    ),
                )
            if observed is None:
                rows = await db.execute_fetchall(
                    """SELECT user_id, channel_id, last_accrued_at
                       FROM coin_voice_sessions WHERE guild_id = ?
                       FOR UPDATE"""
                    if postgres
                    else """SELECT user_id, channel_id, last_accrued_at
                            FROM coin_voice_sessions WHERE guild_id = ?""",
                    (guild_id,),
                )
            else:
                placeholders = ",".join("?" for _ in observed)
                rows = await db.execute_fetchall(
                    f"""SELECT user_id, channel_id, last_accrued_at
                        FROM coin_voice_sessions
                        WHERE guild_id = ? AND user_id IN ({placeholders})
                        {"FOR UPDATE" if postgres else ""}""",
                    (guild_id, *observed),
                )
            existing = {int(row[0]): (int(row[1]), str(row[2])) for row in rows}
            for user_id, (_old_channel, last_value) in existing.items():
                last = datetime.fromisoformat(last_value).astimezone(UTC)
                minutes = max(0, int((moment - last).total_seconds() // 60))
                if minutes:
                    advanced = last + timedelta(minutes=minutes)
                    event_key = f"{guild_id}:{user_id}:{last_value}:{advanced.isoformat()}"
                    awarded = await _credit_activity(
                        db,
                        guild_id=guild_id,
                        user_id=user_id,
                        source="voice",
                        event_key=event_key,
                        units_milli=minutes * settings.voice_reward_milli,
                        occurred_at=advanced.isoformat(),
                        eligible=True,
                        daily_coin_cap=settings.voice_daily_cap,
                    )
                    if awarded is not None:
                        awarded_total += awarded
                    await db.execute(
                        """UPDATE coin_voice_sessions
                           SET last_accrued_at = ?, channel_id = ?
                           WHERE guild_id = ? AND user_id = ?""",
                        (
                            advanced.isoformat(),
                            current.get(user_id, _old_channel),
                            guild_id,
                            user_id,
                        ),
                    )
                if user_id not in current:
                    await db.execute(
                        "DELETE FROM coin_voice_sessions WHERE guild_id = ? AND user_id = ?",
                        (guild_id, user_id),
                    )

            new_users = current.keys() - existing.keys()
            await db.executemany(
                """INSERT INTO coin_voice_sessions
                   (guild_id, user_id, channel_id, last_accrued_at)
                   VALUES (?, ?, ?, ?)""",
                ((guild_id, user_id, current[user_id], occurred_at) for user_id in new_users),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return awarded_total


async def get_economy_statistics(guild_id: int, start_day: str) -> dict[str, object]:
    async with connection() as db:
        source_rows = await db.execute_fetchall(
            """SELECT source, SUM(units_milli), SUM(coins_awarded),
                      SUM(events), SUM(eligible_events)
               FROM coin_activity_daily
               WHERE guild_id = ? AND day >= ? GROUP BY source""",
            (guild_id, start_day),
        )
        top_rows = await db.execute_fetchall(
            """SELECT user_id, SUM(coins_awarded) AS total
               FROM coin_activity_daily
               WHERE guild_id = ? AND day >= ?
               GROUP BY user_id ORDER BY total DESC, user_id LIMIT 10""",
            (guild_id, start_day),
        )
        pending_row = await db.execute_fetchall(
            "SELECT COALESCE(SUM(pending_milli), 0) FROM coin_activity_state WHERE guild_id = ?",
            (guild_id,),
        )
    return {
        "sources": [tuple(row) for row in source_rows],
        "top": [tuple(row) for row in top_rows],
        "pending_milli": int(pending_row[0][0]),
    }


_PRUNE_BATCH_SIZE = 2_000
_PRUNE_MAX_BATCHES = 25


async def prune_economy_activity(
    raw_retention_days: int, aggregate_retention_days: int
) -> dict[str, int]:
    """Delete expired Coins activity in bounded batches.

    Two unbounded ``DELETE`` statements used to run on every five-minute
    maintenance tick. On a first run against a 90-day backlog that is a single
    very long write holding locks on the busiest economy tables; the work is
    now split into batches and drained across passes.
    """

    now = datetime.now(UTC)
    raw_before = (now - timedelta(days=raw_retention_days)).isoformat()
    daily_before = (now - timedelta(days=aggregate_retention_days)).date().isoformat()
    removed = {"ledger": 0, "daily": 0}

    for _ in range(_PRUNE_MAX_BATCHES):
        async with connection() as db:
            cursor = await db.execute(
                """DELETE FROM coin_activity_ledger WHERE id IN (
                       SELECT id FROM coin_activity_ledger
                       WHERE occurred_at < ?
                       ORDER BY occurred_at, id LIMIT ?
                   )""",
                (raw_before, _PRUNE_BATCH_SIZE),
            )
            await db.commit()
        deleted = max(0, cursor.rowcount)
        removed["ledger"] += deleted
        if deleted < _PRUNE_BATCH_SIZE:
            break

    for _ in range(_PRUNE_MAX_BATCHES):
        async with connection() as db:
            cursor = await db.execute(
                """DELETE FROM coin_activity_daily
                   WHERE (guild_id, day, user_id, source) IN (
                       SELECT guild_id, day, user_id, source
                       FROM coin_activity_daily
                       WHERE day < ?
                       ORDER BY day, guild_id, user_id, source LIMIT ?
                   )""",
                (daily_before, _PRUNE_BATCH_SIZE),
            )
            await db.commit()
        deleted = max(0, cursor.rowcount)
        removed["daily"] += deleted
        if deleted < _PRUNE_BATCH_SIZE:
            break
    return removed
