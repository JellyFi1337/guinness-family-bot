"""Organization upgrade tree, development goal, seasons and lore reviews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import (
    DATABASE_INTEGRITY_ERRORS,
    connection as _connect,
    utc_now,
)
from infrastructure.transactions import begin_write

TRACKS = frozenset({"legal_contracts", "illegal_contracts", "other"})
CONTRACT_TRACKS = frozenset({"legal_contracts", "illegal_contracts"})
REVIEW_KINDS = frozenset({"upgrade", "template", "clearance"})
REVIEW_OPEN = "needs_review"
REVIEW_DONE = "reviewed"
HOURS_SOURCE = "human"

_UPGRADE_COLUMNS = (
    "id, guild_id, family_id, key, title, track, max_level, depends_on, "
    "depends_level, requires_org_level, requires_contracts, requires_sku, "
    "requires_qty, cost_reputation, source, verified_at, lore_revision, "
    "version, created_at"
)
_PROGRESS_COLUMNS = "id, guild_id, family_id, upgrade_id, current_level"
_DEV_COLUMNS = (
    "id, guild_id, family_id, org_level, spent_reputation, "
    "direction_changed_at, cooldown_until, pending_direction, pending_spent, "
    "pending_preview_at, goal_upgrade_id, goal_assignee_id, goal_note, "
    "lore_revision, lore_published_at, version"
)
_REVIEW_COLUMNS = (
    "id, guild_id, family_id, kind, subject_key, lore_revision, status"
)
_HOURS_COLUMNS = (
    "id, guild_id, family_id, member_id, season_key, hours, entered_by, "
    "entered_at, source"
)
_SEASON_COLUMNS = (
    "id, guild_id, family_id, season_key, starts_on, ends_on, "
    "influence_daily_limit, prize_places, half_loss_threshold, "
    "announced_by, announced_at"
)


@dataclass(frozen=True, slots=True)
class OrgUpgrade:
    id: int
    guild_id: int
    family_id: int
    key: str
    title: str
    track: str
    max_level: int
    depends_on: str
    depends_level: int
    requires_org_level: int
    requires_contracts: int
    requires_sku: str
    requires_qty: int
    cost_reputation: int
    source: str
    verified_at: str | None
    lore_revision: str
    version: int
    created_at: str


@dataclass(frozen=True, slots=True)
class UpgradeProgress:
    id: int
    guild_id: int
    family_id: int
    upgrade_id: int
    current_level: int


@dataclass(frozen=True, slots=True)
class OrgDevelopment:
    id: int
    guild_id: int
    family_id: int
    org_level: int
    spent_reputation: int
    direction_changed_at: str | None
    cooldown_until: str | None
    pending_direction: str | None
    pending_spent: int | None
    pending_preview_at: str | None
    goal_upgrade_id: int | None
    goal_assignee_id: int | None
    goal_note: str
    lore_revision: str
    lore_published_at: str | None
    version: int


@dataclass(frozen=True, slots=True)
class LoreReview:
    id: int
    guild_id: int
    family_id: int
    kind: str
    subject_key: str
    lore_revision: str
    status: str


@dataclass(frozen=True, slots=True)
class SeasonHours:
    id: int
    guild_id: int
    family_id: int
    member_id: int
    season_key: str
    hours: int
    entered_by: int
    entered_at: str
    source: str


@dataclass(frozen=True, slots=True)
class FamilySeason:
    id: int
    guild_id: int
    family_id: int
    season_key: str
    starts_on: str
    ends_on: str
    influence_daily_limit: int
    prize_places: str
    half_loss_threshold: int
    announced_by: int
    announced_at: str


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _to_upgrade(row: Any) -> OrgUpgrade:
    return OrgUpgrade(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        key=str(row[3]),
        title=str(row[4]),
        track=str(row[5]),
        max_level=int(row[6]),
        depends_on=str(row[7] or ""),
        depends_level=int(row[8] or 0),
        requires_org_level=int(row[9] or 0),
        requires_contracts=int(row[10] or 0),
        requires_sku=str(row[11] or ""),
        requires_qty=int(row[12] or 0),
        cost_reputation=int(row[13] or 0),
        source=str(row[14] or ""),
        verified_at=_optional_text(row[15]),
        lore_revision=str(row[16] or ""),
        version=int(row[17] or 1),
        created_at=str(row[18]),
    )


def _to_progress(row: Any) -> UpgradeProgress:
    return UpgradeProgress(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        upgrade_id=int(row[3]),
        current_level=int(row[4]),
    )


def _to_dev(row: Any) -> OrgDevelopment:
    return OrgDevelopment(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        org_level=int(row[3] or 0),
        spent_reputation=int(row[4] or 0),
        direction_changed_at=_optional_text(row[5]),
        cooldown_until=_optional_text(row[6]),
        pending_direction=_optional_text(row[7]),
        pending_spent=_optional_int(row[8]),
        pending_preview_at=_optional_text(row[9]),
        goal_upgrade_id=_optional_int(row[10]),
        goal_assignee_id=_optional_int(row[11]),
        goal_note=str(row[12] or ""),
        lore_revision=str(row[13] or ""),
        lore_published_at=_optional_text(row[14]),
        version=int(row[15] or 1),
    )


def _to_review(row: Any) -> LoreReview:
    return LoreReview(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        kind=str(row[3]),
        subject_key=str(row[4]),
        lore_revision=str(row[5]),
        status=str(row[6]),
    )


def _to_hours(row: Any) -> SeasonHours:
    return SeasonHours(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        member_id=int(row[3]),
        season_key=str(row[4]),
        hours=int(row[5]),
        entered_by=int(row[6]),
        entered_at=str(row[7]),
        source=str(row[8]),
    )


def _to_season(row: Any) -> FamilySeason:
    return FamilySeason(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        season_key=str(row[3]),
        starts_on=str(row[4]),
        ends_on=str(row[5]),
        influence_daily_limit=int(row[6] or 0),
        prize_places=str(row[7] or ""),
        half_loss_threshold=int(row[8] or 0),
        announced_by=int(row[9]),
        announced_at=str(row[10]),
    )


async def _one(db: Any, query: str, params: tuple[object, ...] = ()) -> Any:
    async with db.execute(query, params) as cursor:
        return await cursor.fetchone()


async def insert_upgrade(
    *,
    guild_id: int,
    family_id: int,
    key: str,
    title: str,
    track: str,
    max_level: int,
    depends_on: str = "",
    depends_level: int = 0,
    requires_org_level: int = 0,
    requires_contracts: int = 0,
    requires_sku: str = "",
    requires_qty: int = 0,
    cost_reputation: int = 0,
    source: str = "",
    verified_at: str | None = None,
    lore_revision: str = "",
) -> OrgUpgrade:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "org-upgrade", family_id)
        try:
            cursor = await db.execute(
                """INSERT INTO org_upgrades (
                       guild_id, family_id, key, title, track, max_level,
                       depends_on, depends_level, requires_org_level,
                       requires_contracts, requires_sku, requires_qty,
                       cost_reputation, source, verified_at, lore_revision,
                       version, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    guild_id,
                    family_id,
                    key,
                    title,
                    track,
                    max_level,
                    depends_on,
                    depends_level,
                    requires_org_level,
                    requires_contracts,
                    requires_sku,
                    requires_qty,
                    cost_reputation,
                    source,
                    verified_at,
                    lore_revision,
                    stamp,
                ),
            )
            upgrade_id = int(cursor.lastrowid)
            await db.execute(
                """INSERT INTO org_upgrade_progress (
                       guild_id, family_id, upgrade_id, current_level
                   ) VALUES (?, ?, ?, 0)""",
                (guild_id, family_id, upgrade_id),
            )
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            raise
    item = await get_upgrade(upgrade_id)
    assert item is not None
    return item


async def get_upgrade(upgrade_id: int) -> OrgUpgrade | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"SELECT {_UPGRADE_COLUMNS} FROM org_upgrades WHERE id = ?",
            (upgrade_id,),
        )
    return None if row is None else _to_upgrade(row)


async def get_upgrade_by_key(family_id: int, key: str) -> OrgUpgrade | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"""SELECT {_UPGRADE_COLUMNS} FROM org_upgrades
                WHERE family_id = ? AND key = ?""",
            (family_id, key),
        )
    return None if row is None else _to_upgrade(row)


async def list_upgrades(family_id: int) -> list[OrgUpgrade]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_UPGRADE_COLUMNS} FROM org_upgrades
                WHERE family_id = ?
                ORDER BY track, key""",
            (family_id,),
        )
    return [_to_upgrade(row) for row in rows]


async def get_progress(upgrade_id: int) -> UpgradeProgress | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"""SELECT {_PROGRESS_COLUMNS} FROM org_upgrade_progress
                WHERE upgrade_id = ?""",
            (upgrade_id,),
        )
    return None if row is None else _to_progress(row)


async def list_progress(family_id: int) -> list[UpgradeProgress]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_PROGRESS_COLUMNS} FROM org_upgrade_progress
                WHERE family_id = ?""",
            (family_id,),
        )
    return [_to_progress(row) for row in rows]


async def set_progress_level(upgrade_id: int, level: int) -> UpgradeProgress | None:
    async with _connect() as db:
        await begin_write(db, "org-progress", upgrade_id)
        await db.execute(
            """UPDATE org_upgrade_progress
               SET current_level = ?
               WHERE upgrade_id = ?""",
            (level, upgrade_id),
        )
        await db.commit()
    return await get_progress(upgrade_id)


async def reset_all_progress(family_id: int) -> None:
    async with _connect() as db:
        await begin_write(db, "org-progress-reset", family_id)
        await db.execute(
            """UPDATE org_upgrade_progress
               SET current_level = 0
               WHERE family_id = ?""",
            (family_id,),
        )
        await db.commit()


async def track_has_progress(family_id: int, track: str) -> bool:
    async with _connect() as db:
        row = await _one(
            db,
            """SELECT 1
               FROM org_upgrade_progress p
               JOIN org_upgrades u ON u.id = p.upgrade_id
               WHERE p.family_id = ? AND u.track = ? AND p.current_level > 0
               LIMIT 1""",
            (family_id, track),
        )
    return row is not None


async def ensure_development(guild_id: int, family_id: int) -> OrgDevelopment:
    async with _connect() as db:
        await begin_write(db, "org-dev", family_id)
        row = await _one(
            db,
            f"SELECT {_DEV_COLUMNS} FROM org_development WHERE family_id = ?",
            (family_id,),
        )
        if row is None:
            await db.execute(
                """INSERT INTO org_development (
                       guild_id, family_id, org_level, spent_reputation,
                       goal_note, lore_revision, version
                   ) VALUES (?, ?, 0, 0, '', '', 1)""",
                (guild_id, family_id),
            )
        await db.commit()
    item = await get_development(family_id)
    assert item is not None
    return item


async def get_development(family_id: int) -> OrgDevelopment | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"SELECT {_DEV_COLUMNS} FROM org_development WHERE family_id = ?",
            (family_id,),
        )
    return None if row is None else _to_dev(row)


async def update_development(
    family_id: int,
    *,
    org_level: int | None = None,
    spent_reputation: int | None = None,
    direction_changed_at: str | None = None,
    cooldown_until: str | None = None,
    pending_direction: str | None = None,
    clear_pending: bool = False,
    pending_spent: int | None = None,
    pending_preview_at: str | None = None,
    goal_upgrade_id: int | None = None,
    clear_goal: bool = False,
    goal_assignee_id: int | None = None,
    goal_note: str | None = None,
    lore_revision: str | None = None,
    lore_published_at: str | None = None,
    expected_version: int | None = None,
) -> OrgDevelopment | None:
    async with _connect() as db:
        await begin_write(db, "org-dev", family_id)
        row = await _one(
            db,
            f"SELECT {_DEV_COLUMNS} FROM org_development WHERE family_id = ?",
            (family_id,),
        )
        if row is None:
            await db.rollback()
            return None
        current = _to_dev(row)
        if expected_version is not None and current.version != expected_version:
            await db.rollback()
            return None
        pending = None if clear_pending else (
            current.pending_direction if pending_direction is None else pending_direction
        )
        pending_spent_value = None if clear_pending else (
            current.pending_spent if pending_spent is None else pending_spent
        )
        pending_at = None if clear_pending else (
            current.pending_preview_at
            if pending_preview_at is None
            else pending_preview_at
        )
        goal = None if clear_goal else (
            current.goal_upgrade_id if goal_upgrade_id is None else goal_upgrade_id
        )
        await db.execute(
            """UPDATE org_development SET
                   org_level = ?,
                   spent_reputation = ?,
                   direction_changed_at = ?,
                   cooldown_until = ?,
                   pending_direction = ?,
                   pending_spent = ?,
                   pending_preview_at = ?,
                   goal_upgrade_id = ?,
                   goal_assignee_id = ?,
                   goal_note = ?,
                   lore_revision = ?,
                   lore_published_at = ?,
                   version = version + 1
               WHERE family_id = ?""",
            (
                current.org_level if org_level is None else org_level,
                current.spent_reputation
                if spent_reputation is None
                else spent_reputation,
                current.direction_changed_at
                if direction_changed_at is None
                else direction_changed_at,
                current.cooldown_until if cooldown_until is None else cooldown_until,
                pending,
                pending_spent_value,
                pending_at,
                goal,
                current.goal_assignee_id
                if goal_assignee_id is None
                else goal_assignee_id,
                current.goal_note if goal_note is None else goal_note,
                current.lore_revision if lore_revision is None else lore_revision,
                current.lore_published_at
                if lore_published_at is None
                else lore_published_at,
                family_id,
            ),
        )
        await db.commit()
    return await get_development(family_id)


async def upsert_lore_review(
    *,
    guild_id: int,
    family_id: int,
    kind: str,
    subject_key: str,
    lore_revision: str,
    status: str = REVIEW_OPEN,
) -> LoreReview:
    async with _connect() as db:
        await begin_write(db, "org-lore", family_id)
        existing = await _one(
            db,
            f"""SELECT {_REVIEW_COLUMNS} FROM org_lore_reviews
                WHERE family_id = ? AND kind = ? AND subject_key = ?
                  AND lore_revision = ?""",
            (family_id, kind, subject_key, lore_revision),
        )
        if existing is None:
            await db.execute(
                """INSERT INTO org_lore_reviews (
                       guild_id, family_id, kind, subject_key, lore_revision, status
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, family_id, kind, subject_key, lore_revision, status),
            )
        else:
            await db.execute(
                """UPDATE org_lore_reviews SET status = ?
                   WHERE id = ?""",
                (status, int(existing[0])),
            )
        await db.commit()
    item = await get_lore_review(family_id, kind, subject_key, lore_revision)
    assert item is not None
    return item


async def get_lore_review(
    family_id: int, kind: str, subject_key: str, lore_revision: str
) -> LoreReview | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"""SELECT {_REVIEW_COLUMNS} FROM org_lore_reviews
                WHERE family_id = ? AND kind = ? AND subject_key = ?
                  AND lore_revision = ?""",
            (family_id, kind, subject_key, lore_revision),
        )
    return None if row is None else _to_review(row)


async def list_open_reviews(family_id: int, lore_revision: str) -> list[LoreReview]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_REVIEW_COLUMNS} FROM org_lore_reviews
                WHERE family_id = ? AND lore_revision = ? AND status = ?
                ORDER BY kind, subject_key""",
            (family_id, lore_revision, REVIEW_OPEN),
        )
    return [_to_review(row) for row in rows]


async def upsert_season_hours(
    *,
    guild_id: int,
    family_id: int,
    member_id: int,
    season_key: str,
    hours: int,
    entered_by: int,
) -> SeasonHours:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "season-hours", member_id)
        existing = await _one(
            db,
            f"""SELECT {_HOURS_COLUMNS} FROM season_play_hours
                WHERE family_id = ? AND member_id = ? AND season_key = ?""",
            (family_id, member_id, season_key),
        )
        if existing is None:
            await db.execute(
                """INSERT INTO season_play_hours (
                       guild_id, family_id, member_id, season_key, hours,
                       entered_by, entered_at, source
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    member_id,
                    season_key,
                    hours,
                    entered_by,
                    stamp,
                    HOURS_SOURCE,
                ),
            )
        else:
            await db.execute(
                """UPDATE season_play_hours
                   SET hours = ?, entered_by = ?, entered_at = ?, source = ?
                   WHERE id = ?""",
                (hours, entered_by, stamp, HOURS_SOURCE, int(existing[0])),
            )
        await db.commit()
    item = await get_season_hours(family_id, member_id, season_key)
    assert item is not None
    return item


async def get_season_hours(
    family_id: int, member_id: int, season_key: str
) -> SeasonHours | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"""SELECT {_HOURS_COLUMNS} FROM season_play_hours
                WHERE family_id = ? AND member_id = ? AND season_key = ?""",
            (family_id, member_id, season_key),
        )
    return None if row is None else _to_hours(row)


async def insert_family_season(
    *,
    guild_id: int,
    family_id: int,
    season_key: str,
    starts_on: str,
    ends_on: str,
    announced_by: int,
    influence_daily_limit: int = 0,
    prize_places: str = "",
    half_loss_threshold: int = 0,
) -> FamilySeason:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "family-season", family_id)
        try:
            cursor = await db.execute(
                """INSERT INTO family_seasons (
                       guild_id, family_id, season_key, starts_on, ends_on,
                       influence_daily_limit, prize_places, half_loss_threshold,
                       announced_by, announced_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    season_key,
                    starts_on,
                    ends_on,
                    influence_daily_limit,
                    prize_places,
                    half_loss_threshold,
                    announced_by,
                    stamp,
                ),
            )
            season_id = int(cursor.lastrowid)
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            raise
    item = await get_family_season(season_id)
    assert item is not None
    return item


async def get_family_season(season_id: int) -> FamilySeason | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"SELECT {_SEASON_COLUMNS} FROM family_seasons WHERE id = ?",
            (season_id,),
        )
    return None if row is None else _to_season(row)


async def get_family_season_by_key(
    family_id: int, season_key: str
) -> FamilySeason | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"""SELECT {_SEASON_COLUMNS} FROM family_seasons
                WHERE family_id = ? AND season_key = ?""",
            (family_id, season_key),
        )
    return None if row is None else _to_season(row)
