"""Bonus periods and proposed game-money shares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import (
    DATABASE_INTEGRITY_ERRORS,
    connection as _connect,
    utc_now,
)
from infrastructure.transactions import begin_write

PERIOD_KINDS = ("week", "rating")
PERIOD_STATUSES = ("draft", "proposed", "approved", "cancelled")
POOLS = ("combat", "mentoring", "logistics")

_PERIOD_COLUMNS = (
    "id, guild_id, family_id, period_key, period_kind, starts_at, ends_at, "
    "fund_amount, mentor_fund, logistics_fund, min_points, max_share_percent, "
    "rule_version, status, declared_by, approved_by, version, created_at"
)
_SHARE_COLUMNS = (
    "id, guild_id, family_id, period_id, member_discord_id, pool, points, "
    "proposed_amount, adjusted_amount"
)


@dataclass(frozen=True, slots=True)
class BonusPeriod:
    id: int
    guild_id: int
    family_id: int
    period_key: str
    period_kind: str
    starts_at: str
    ends_at: str
    fund_amount: int
    mentor_fund: int
    logistics_fund: int
    min_points: int
    max_share_percent: int
    rule_version: int
    status: str
    declared_by: int
    approved_by: int | None
    version: int
    created_at: str

    def total_fund(self) -> int:
        return self.fund_amount + self.mentor_fund + self.logistics_fund


@dataclass(frozen=True, slots=True)
class BonusShare:
    id: int
    guild_id: int
    family_id: int
    period_id: int
    member_discord_id: int
    pool: str
    points: int
    proposed_amount: int
    adjusted_amount: int


def _inserted_id(cursor: Any) -> int:
    row_id = int(cursor.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("insert did not return an id")
    return row_id


def _require_non_negative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _to_period(row: Any) -> BonusPeriod:
    return BonusPeriod(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        period_key=str(row[3]),
        period_kind=str(row[4]),
        starts_at=str(row[5]),
        ends_at=str(row[6]),
        fund_amount=int(row[7]),
        mentor_fund=int(row[8]),
        logistics_fund=int(row[9]),
        min_points=int(row[10]),
        max_share_percent=int(row[11]),
        rule_version=int(row[12]),
        status=str(row[13]),
        declared_by=int(row[14]),
        approved_by=int(row[15]) if row[15] is not None else None,
        version=int(row[16]),
        created_at=str(row[17]),
    )


def _to_share(row: Any) -> BonusShare:
    return BonusShare(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        period_id=int(row[3]),
        member_discord_id=int(row[4]),
        pool=str(row[5]),
        points=int(row[6]),
        proposed_amount=int(row[7]),
        adjusted_amount=int(row[8]),
    )


async def get_period(period_id: int) -> BonusPeriod | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_PERIOD_COLUMNS} FROM bonus_periods WHERE id = ?",
            (period_id,),
        )
    return _to_period(rows[0]) if rows else None


async def find_period(*, family_id: int, period_key: str) -> BonusPeriod | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_PERIOD_COLUMNS} FROM bonus_periods
                WHERE family_id = ? AND period_key = ?""",
            (family_id, period_key),
        )
    return _to_period(rows[0]) if rows else None


async def latest_period(family_id: int) -> BonusPeriod | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_PERIOD_COLUMNS} FROM bonus_periods
                WHERE family_id = ? AND status != 'cancelled'
                ORDER BY id DESC LIMIT 1""",
            (family_id,),
        )
    return _to_period(rows[0]) if rows else None


async def insert_period(
    *,
    guild_id: int,
    family_id: int,
    period_key: str,
    period_kind: str,
    starts_at: str,
    ends_at: str,
    fund_amount: int,
    mentor_fund: int,
    logistics_fund: int,
    min_points: int,
    max_share_percent: int,
    rule_version: int,
    declared_by: int,
) -> BonusPeriod:
    if period_kind not in PERIOD_KINDS:
        raise ValueError("unknown bonus period kind")
    fund_amount = _require_non_negative_int(fund_amount, label="fund")
    mentor_fund = _require_non_negative_int(mentor_fund, label="mentor_fund")
    logistics_fund = _require_non_negative_int(
        logistics_fund, label="logistics_fund"
    )
    min_points = _require_non_negative_int(min_points, label="min_points")
    max_share_percent = _require_non_negative_int(
        max_share_percent, label="max_share_percent"
    )
    if max_share_percent > 100:
        raise ValueError("max_share_percent must be 0..100")
    rule_version = _require_non_negative_int(rule_version, label="rule_version")
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "bonus-period", f"{family_id}:{period_key}")
        existing = await db.execute_fetchall(
            f"""SELECT {_PERIOD_COLUMNS} FROM bonus_periods
                WHERE family_id = ? AND period_key = ?""",
            (family_id, period_key),
        )
        if existing:
            await db.commit()
            return _to_period(existing[0])
        try:
            cursor = await db.execute(
                """INSERT INTO bonus_periods (
                       guild_id, family_id, period_key, period_kind, starts_at,
                       ends_at, fund_amount, mentor_fund, logistics_fund,
                       min_points, max_share_percent, rule_version, status,
                       declared_by, version, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, 1, ?)""",
                (
                    guild_id,
                    family_id,
                    period_key,
                    period_kind,
                    starts_at,
                    ends_at,
                    fund_amount,
                    mentor_fund,
                    logistics_fund,
                    min_points,
                    max_share_percent,
                    rule_version,
                    declared_by,
                    stamp,
                ),
            )
            period_id = _inserted_id(cursor)
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            loaded = await find_period(family_id=family_id, period_key=period_key)
            if loaded is None:
                raise
            return loaded
    found = await get_period(period_id)
    if found is None:
        raise RuntimeError("bonus period insert did not persist")
    return found


async def update_period(
    period_id: int,
    *,
    expected_version: int,
    fund_amount: int | None = None,
    mentor_fund: int | None = None,
    logistics_fund: int | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    min_points: int | None = None,
    max_share_percent: int | None = None,
    rule_version: int | None = None,
    status: str | None = None,
    approved_by: int | None = None,
) -> BonusPeriod | None:
    if status is not None and status not in PERIOD_STATUSES:
        raise ValueError("unknown bonus period status")
    async with _connect() as db:
        await begin_write(db, "bonus-period-update", period_id)
        rows = await db.execute_fetchall(
            f"SELECT {_PERIOD_COLUMNS} FROM bonus_periods WHERE id = ?",
            (period_id,),
        )
        if not rows:
            await db.commit()
            return None
        current = _to_period(rows[0])
        if current.version != expected_version:
            await db.commit()
            return None
        fields = ["version = version + 1"]
        params: list[Any] = []
        updates = (
            ("fund_amount", fund_amount),
            ("mentor_fund", mentor_fund),
            ("logistics_fund", logistics_fund),
            ("starts_at", starts_at),
            ("ends_at", ends_at),
            ("min_points", min_points),
            ("max_share_percent", max_share_percent),
            ("rule_version", rule_version),
            ("status", status),
            ("approved_by", approved_by),
        )
        for column, value in updates:
            if value is not None:
                fields.append(f"{column} = ?")
                params.append(value)
        params.extend((period_id, expected_version))
        cursor = await db.execute(
            f"""UPDATE bonus_periods SET {", ".join(fields)}
                WHERE id = ? AND version = ?""",
            tuple(params),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
    return await get_period(period_id)


async def list_shares(period_id: int) -> list[BonusShare]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_SHARE_COLUMNS} FROM bonus_shares
                WHERE period_id = ?
                ORDER BY member_discord_id, pool""",
            (period_id,),
        )
    return [_to_share(row) for row in rows]


async def replace_shares(
    *,
    guild_id: int,
    family_id: int,
    period_id: int,
    shares: list[tuple[int, str, int, int, int]],
) -> list[BonusShare]:
    cleaned: list[tuple[int, str, int, int, int]] = []
    for member_discord_id, pool, points, proposed, adjusted in shares:
        if pool not in POOLS:
            raise ValueError("unknown bonus pool")
        cleaned.append(
            (
                _require_non_negative_int(member_discord_id, label="member"),
                pool,
                _require_non_negative_int(points, label="points"),
                _require_non_negative_int(proposed, label="proposed"),
                _require_non_negative_int(adjusted, label="adjusted"),
            )
        )
    async with _connect() as db:
        await begin_write(db, "bonus-shares", period_id)
        await db.execute("DELETE FROM bonus_shares WHERE period_id = ?", (period_id,))
        for member_discord_id, pool, points, proposed, adjusted in cleaned:
            await db.execute(
                """INSERT INTO bonus_shares (
                       guild_id, family_id, period_id, member_discord_id, pool,
                       points, proposed_amount, adjusted_amount
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    period_id,
                    member_discord_id,
                    pool,
                    points,
                    proposed,
                    adjusted,
                ),
            )
        await db.commit()
    return await list_shares(period_id)


async def update_share_amount(
    share_id: int, *, adjusted_amount: int
) -> BonusShare | None:
    amount = _require_non_negative_int(adjusted_amount, label="adjusted")
    async with _connect() as db:
        await begin_write(db, "bonus-share", share_id)
        cursor = await db.execute(
            "UPDATE bonus_shares SET adjusted_amount = ? WHERE id = ?",
            (amount, share_id),
        )
        if cursor.rowcount != 1:
            await db.commit()
            return None
        await db.commit()
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_SHARE_COLUMNS} FROM bonus_shares WHERE id = ?",
            (share_id,),
        )
    return _to_share(rows[0]) if rows else None
