from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from infrastructure.database import connection as _connect


MSK = timezone(timedelta(hours=3))


@dataclass(frozen=True, slots=True)
class GameOnlineDay:
    day: str
    peak_players: int
    minimum_players: int
    average_players: float
    sample_count: int
    last_players: int
    last_sample_at: str


def server_key(query: str) -> str:
    return " ".join(query.casefold().split())


async def record_game_online_sample(
    query: str,
    server_name: str,
    players: int,
    *,
    observed_at: datetime | None = None,
) -> None:
    if players < 0:
        raise ValueError("players must be non-negative")
    observed = observed_at or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    observed = observed.astimezone(UTC)
    day = observed.astimezone(MSK).date().isoformat()
    key = server_key(query)
    if not key:
        raise ValueError("server query must not be empty")
    async with _connect() as db:
        await db.execute(
            """INSERT INTO game_server_daily_online
               (server_key, server_name, day, peak_players, minimum_players,
                sample_sum, sample_count, last_players, last_sample_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(server_key, day) DO UPDATE SET
                   server_name = excluded.server_name,
                   peak_players = MAX(
                       game_server_daily_online.peak_players,
                       excluded.peak_players
                   ),
                   minimum_players = MIN(
                       game_server_daily_online.minimum_players,
                       excluded.minimum_players
                   ),
                   sample_sum = game_server_daily_online.sample_sum
                       + excluded.sample_sum,
                   sample_count = game_server_daily_online.sample_count + 1,
                   last_players = excluded.last_players,
                   last_sample_at = excluded.last_sample_at""",
            (
                key,
                server_name,
                day,
                players,
                players,
                players,
                players,
                observed.isoformat(),
            ),
        )
        await db.commit()


async def get_game_online_days(
    query: str,
    *,
    since_day: str,
) -> list[GameOnlineDay]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT day, peak_players, minimum_players, sample_sum,
                      sample_count, last_players, last_sample_at
               FROM game_server_daily_online
               WHERE server_key = ? AND day >= ?
               ORDER BY day""",
            (server_key(query), since_day),
        )
    return [
        GameOnlineDay(
            day=str(row[0]),
            peak_players=int(row[1]),
            minimum_players=int(row[2]),
            average_players=(int(row[3]) / max(1, int(row[4]))),
            sample_count=int(row[4]),
            last_players=int(row[5]),
            last_sample_at=str(row[6]),
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class ExternalSnapshotRow:
    guild_id: int
    source_key: str
    resource: str
    payload_text: str
    fetched_at: str
    attribution: str


async def upsert_external_snapshot(
    guild_id: int,
    source_key: str,
    resource: str,
    payload_text: str,
    fetched_at: str,
    attribution: str,
) -> None:
    if guild_id <= 0:
        raise ValueError("guild_id must be positive")
    if source_key not in {"rage_master", "majestic_api"}:
        raise ValueError("unknown external source")
    name = resource.strip()
    if not name:
        raise ValueError("resource must not be empty")
    body = payload_text.strip()
    if not body:
        raise ValueError("payload_text must not be empty")
    stamp = fetched_at.strip()
    if not stamp:
        raise ValueError("fetched_at must not be empty")
    async with _connect() as db:
        await db.execute(
            """INSERT INTO external_snapshots
               (guild_id, source_key, resource, payload_text, fetched_at, attribution)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, source_key, resource) DO UPDATE SET
                   payload_text = excluded.payload_text,
                   fetched_at = excluded.fetched_at,
                   attribution = excluded.attribution""",
            (guild_id, source_key, name, body, stamp, attribution),
        )
        await db.commit()


async def get_external_snapshot(
    guild_id: int,
    source_key: str,
    resource: str,
) -> ExternalSnapshotRow | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT guild_id, source_key, resource, payload_text, fetched_at,
                      attribution
               FROM external_snapshots
               WHERE guild_id = ? AND source_key = ? AND resource = ?
               LIMIT 1""",
            (guild_id, source_key, resource),
        )
    if not rows:
        return None
    row = rows[0]
    return ExternalSnapshotRow(
        guild_id=int(row[0]),
        source_key=str(row[1]),
        resource=str(row[2]),
        payload_text=str(row[3]),
        fetched_at=str(row[4]),
        attribution=str(row[5]),
    )
