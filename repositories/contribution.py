"""Versioned merit rules and append-only contribution ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import (
    DATABASE_INTEGRITY_ERRORS,
    connection as _connect,
    utc_now,
)
from infrastructure.transactions import begin_write

CATEGORIES = (
    "contract",
    "attendance",
    "mentoring",
    "warehouse",
    "logistics",
    "duty",
)
OPERATIONS = ("award", "reverse")

_RULE_COLUMNS = (
    "id, guild_id, family_id, version, contract_amount, attendance_amount, "
    "mentoring_amount, warehouse_amount, logistics_amount, duty_amount, "
    "created_by, created_at"
)
_ENTRY_COLUMNS = (
    "id, guild_id, family_id, member_discord_id, category, amount, operation, "
    "basis_kind, basis_id, rule_version, actor_id, created_at, reversal_of"
)
_MARK_COLUMNS = (
    "id, guild_id, family_id, period_key, marked_by, created_at"
)


@dataclass(frozen=True, slots=True)
class RuleVersion:
    id: int
    guild_id: int
    family_id: int
    version: int
    contract_amount: int
    attendance_amount: int
    mentoring_amount: int
    warehouse_amount: int
    logistics_amount: int
    duty_amount: int
    created_by: int
    created_at: str

    def amount_for(self, category: str) -> int:
        mapping = {
            "contract": self.contract_amount,
            "attendance": self.attendance_amount,
            "mentoring": self.mentoring_amount,
            "warehouse": self.warehouse_amount,
            "logistics": self.logistics_amount,
            "duty": self.duty_amount,
        }
        return int(mapping[category])

    def amounts(self) -> dict[str, int]:
        return {key: self.amount_for(key) for key in CATEGORIES}


@dataclass(frozen=True, slots=True)
class ContributionEntry:
    id: int
    guild_id: int
    family_id: int
    member_discord_id: int
    category: str
    amount: int
    operation: str
    basis_kind: str
    basis_id: str
    rule_version: int
    actor_id: int
    created_at: str
    reversal_of: int | None


@dataclass(frozen=True, slots=True)
class BonusMark:
    id: int
    guild_id: int
    family_id: int
    period_key: str
    marked_by: int
    created_at: str


def _inserted_id(cursor: Any) -> int:
    row_id = int(cursor.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("insert did not return an id")
    return row_id


def _require_non_negative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _require_positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _to_rule(row: Any) -> RuleVersion:
    return RuleVersion(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        version=int(row[3]),
        contract_amount=int(row[4]),
        attendance_amount=int(row[5]),
        mentoring_amount=int(row[6]),
        warehouse_amount=int(row[7]),
        logistics_amount=int(row[8]),
        duty_amount=int(row[9]),
        created_by=int(row[10]),
        created_at=str(row[11]),
    )


def _to_entry(row: Any) -> ContributionEntry:
    return ContributionEntry(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        member_discord_id=int(row[3]),
        category=str(row[4]),
        amount=int(row[5]),
        operation=str(row[6]),
        basis_kind=str(row[7]),
        basis_id=str(row[8]),
        rule_version=int(row[9]),
        actor_id=int(row[10]),
        created_at=str(row[11]),
        reversal_of=int(row[12]) if row[12] is not None else None,
    )


def _to_mark(row: Any) -> BonusMark:
    return BonusMark(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        period_key=str(row[3]),
        marked_by=int(row[4]),
        created_at=str(row[5]),
    )


async def latest_rule_version(family_id: int) -> RuleVersion | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_RULE_COLUMNS} FROM contribution_rule_versions
                WHERE family_id = ?
                ORDER BY version DESC LIMIT 1""",
            (family_id,),
        )
    return _to_rule(rows[0]) if rows else None


async def get_rule_version(version_id: int) -> RuleVersion | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_RULE_COLUMNS} FROM contribution_rule_versions WHERE id = ?",
            (version_id,),
        )
    return _to_rule(rows[0]) if rows else None


async def insert_rule_version(
    *,
    guild_id: int,
    family_id: int,
    amounts: dict[str, int],
    created_by: int,
) -> RuleVersion:
    cleaned = {
        key: _require_non_negative_int(amounts.get(key, 0), label=key)
        for key in CATEGORIES
    }
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "contribution-rules", family_id)
        rows = await db.execute_fetchall(
            """SELECT COALESCE(MAX(version), 0) FROM contribution_rule_versions
               WHERE family_id = ?""",
            (family_id,),
        )
        next_version = int(rows[0][0]) + 1 if rows else 1
        cursor = await db.execute(
            """INSERT INTO contribution_rule_versions (
                   guild_id, family_id, version, contract_amount,
                   attendance_amount, mentoring_amount, warehouse_amount,
                   logistics_amount, duty_amount, created_by, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                family_id,
                next_version,
                cleaned["contract"],
                cleaned["attendance"],
                cleaned["mentoring"],
                cleaned["warehouse"],
                cleaned["logistics"],
                cleaned["duty"],
                created_by,
                stamp,
            ),
        )
        rule_id = _inserted_id(cursor)
        await db.commit()
    found = await get_rule_version(rule_id)
    if found is None:
        raise RuntimeError("rule version insert did not persist")
    return found


async def find_entry(
    *,
    family_id: int,
    member_discord_id: int,
    basis_kind: str,
    basis_id: str,
    operation: str,
) -> ContributionEntry | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_ENTRY_COLUMNS} FROM contribution_entries
                WHERE family_id = ? AND member_discord_id = ?
                  AND basis_kind = ? AND basis_id = ? AND operation = ?""",
            (family_id, member_discord_id, basis_kind, basis_id, operation),
        )
    return _to_entry(rows[0]) if rows else None


async def get_entry(entry_id: int) -> ContributionEntry | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_ENTRY_COLUMNS} FROM contribution_entries WHERE id = ?",
            (entry_id,),
        )
    return _to_entry(rows[0]) if rows else None


async def insert_entry(
    *,
    guild_id: int,
    family_id: int,
    member_discord_id: int,
    category: str,
    amount: int,
    operation: str,
    basis_kind: str,
    basis_id: str,
    rule_version: int,
    actor_id: int,
    reversal_of: int | None = None,
) -> ContributionEntry:
    if category not in CATEGORIES:
        raise ValueError("unknown contribution category")
    if operation not in OPERATIONS:
        raise ValueError("unknown contribution operation")
    amount = _require_positive_int(amount, label="contribution amount")
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(
            db,
            "contribution-entry",
            f"{family_id}:{member_discord_id}:{basis_kind}:{basis_id}:{operation}",
        )
        existing = await db.execute_fetchall(
            f"""SELECT {_ENTRY_COLUMNS} FROM contribution_entries
                WHERE family_id = ? AND member_discord_id = ?
                  AND basis_kind = ? AND basis_id = ? AND operation = ?""",
            (family_id, member_discord_id, basis_kind, basis_id, operation),
        )
        if existing:
            await db.commit()
            return _to_entry(existing[0])
        try:
            cursor = await db.execute(
                """INSERT INTO contribution_entries (
                       guild_id, family_id, member_discord_id, category, amount,
                       operation, basis_kind, basis_id, rule_version, actor_id,
                       created_at, reversal_of
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    member_discord_id,
                    category,
                    amount,
                    operation,
                    basis_kind,
                    basis_id,
                    rule_version,
                    actor_id,
                    stamp,
                    reversal_of,
                ),
            )
            entry_id = _inserted_id(cursor)
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            loaded = await find_entry(
                family_id=family_id,
                member_discord_id=member_discord_id,
                basis_kind=basis_kind,
                basis_id=basis_id,
                operation=operation,
            )
            if loaded is None:
                raise
            return loaded
    found = await get_entry(entry_id)
    if found is None:
        raise RuntimeError("contribution insert did not persist")
    return found


async def list_entries_for_member(
    family_id: int, member_discord_id: int
) -> list[ContributionEntry]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_ENTRY_COLUMNS} FROM contribution_entries
                WHERE family_id = ? AND member_discord_id = ?
                ORDER BY id""",
            (family_id, member_discord_id),
        )
    return [_to_entry(row) for row in rows]


async def list_entries_for_basis(
    family_id: int, basis_kind: str, basis_id: str
) -> list[ContributionEntry]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_ENTRY_COLUMNS} FROM contribution_entries
                WHERE family_id = ? AND basis_kind = ? AND basis_id = ?
                ORDER BY id""",
            (family_id, basis_kind, basis_id),
        )
    return [_to_entry(row) for row in rows]


async def category_totals_in_range(
    family_id: int, *, starts_at: str, ends_at: str
) -> dict[int, dict[str, int]]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT member_discord_id, category, operation, amount
               FROM contribution_entries
               WHERE family_id = ?
                 AND created_at >= ?
                 AND created_at < ?""",
            (family_id, starts_at, ends_at),
        )
    totals: dict[int, dict[str, int]] = {}
    for member_discord_id, category, operation, amount in rows:
        member_id = int(member_discord_id)
        bucket = totals.setdefault(member_id, dict.fromkeys(CATEGORIES, 0))
        value = int(amount)
        key = str(category)
        if operation == "award":
            bucket[key] = bucket.get(key, 0) + value
        elif operation == "reverse":
            bucket[key] = bucket.get(key, 0) - value
    return totals


async def member_entries_in_range(
    family_id: int,
    member_discord_id: int,
    *,
    starts_at: str,
    ends_at: str,
) -> list[ContributionEntry]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_ENTRY_COLUMNS} FROM contribution_entries
                WHERE family_id = ?
                  AND member_discord_id = ?
                  AND created_at >= ?
                  AND created_at < ?
                ORDER BY id""",
            (family_id, member_discord_id, starts_at, ends_at),
        )
    return [_to_entry(row) for row in rows]


async def member_category_totals(
    family_id: int, member_discord_id: int
) -> dict[str, int]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT category, operation, amount FROM contribution_entries
               WHERE family_id = ? AND member_discord_id = ?""",
            (family_id, member_discord_id),
        )
    totals = dict.fromkeys(CATEGORIES, 0)
    for category, operation, amount in rows:
        value = int(amount)
        if operation == "award":
            totals[str(category)] = totals.get(str(category), 0) + value
        elif operation == "reverse":
            totals[str(category)] = totals.get(str(category), 0) - value
    return totals


async def insert_bonus_mark(
    *,
    guild_id: int,
    family_id: int,
    period_key: str,
    marked_by: int,
) -> BonusMark:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "contribution-bonus", f"{family_id}:{period_key}")
        existing = await db.execute_fetchall(
            f"""SELECT {_MARK_COLUMNS} FROM contribution_bonus_marks
                WHERE family_id = ? AND period_key = ?""",
            (family_id, period_key),
        )
        if existing:
            await db.commit()
            return _to_mark(existing[0])
        cursor = await db.execute(
            """INSERT INTO contribution_bonus_marks (
                   guild_id, family_id, period_key, marked_by, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (guild_id, family_id, period_key, marked_by, stamp),
        )
        mark_id = _inserted_id(cursor)
        await db.commit()
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_MARK_COLUMNS} FROM contribution_bonus_marks WHERE id = ?",
            (mark_id,),
        )
    if not rows:
        raise RuntimeError("bonus mark insert did not persist")
    return _to_mark(rows[0])
