"""Onboarding tracks, training assignments, rule versions and promotions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import connection as _connect, utc_now
from repositories.members import Member, get_member

TRACK_STATUSES = frozenset({"active", "completed", "cancelled"})
ASSIGNMENT_STATUSES = frozenset({"open", "done", "cancelled"})
PROPOSAL_STATUSES = frozenset({"pending", "accepted", "rejected"})
ONBOARDING_STEPS: tuple[str, ...] = (
    "rules",
    "comms",
    "first_task",
    "skill_check",
    "main_review",
)


@dataclass(frozen=True, slots=True)
class OnboardingTrack:
    id: int
    guild_id: int
    family_id: int
    member_id: int
    application_number: str | None
    specialization: str
    mentor_id: int | None
    current_step: str
    status: str
    started_at: str
    probation_until: str | None


@dataclass(frozen=True, slots=True)
class TrainingAssignment:
    id: int
    guild_id: int
    family_id: int
    track_id: int
    member_id: int
    mentor_id: int | None
    specialization: str
    step_key: str
    title: str
    status: str
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class RulesVersion:
    id: int
    guild_id: int
    family_id: int
    activity: str
    version: str
    published_at: str
    published_by: int


@dataclass(frozen=True, slots=True)
class PromotionCriteria:
    id: int
    guild_id: int
    family_id: int
    title: str
    body: str
    published_at: str
    published_by: int


@dataclass(frozen=True, slots=True)
class PromotionProposal:
    id: int
    guild_id: int
    family_id: int
    member_id: int
    criteria_id: int
    proposed_by: int
    status: str
    reason: str | None
    decided_by: int | None
    created_at: str
    decided_at: str | None


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


def _to_track(row: Any) -> OnboardingTrack:
    return OnboardingTrack(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        member_id=int(row[3]),
        application_number=_optional_text(row[4]),
        specialization=str(row[5]),
        mentor_id=_optional_int(row[6]),
        current_step=str(row[7]),
        status=str(row[8]),
        started_at=str(row[9]),
        probation_until=_optional_text(row[10]),
    )


def _to_assignment(row: Any) -> TrainingAssignment:
    return TrainingAssignment(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        track_id=int(row[3]),
        member_id=int(row[4]),
        mentor_id=_optional_int(row[5]),
        specialization=str(row[6]),
        step_key=str(row[7]),
        title=str(row[8]),
        status=str(row[9]),
        created_at=str(row[10]),
        completed_at=_optional_text(row[11]),
    )


def _to_rules(row: Any) -> RulesVersion:
    return RulesVersion(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        activity=str(row[3]),
        version=str(row[4]),
        published_at=str(row[5]),
        published_by=int(row[6]),
    )


def _to_criteria(row: Any) -> PromotionCriteria:
    return PromotionCriteria(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        title=str(row[3]),
        body=str(row[4]),
        published_at=str(row[5]),
        published_by=int(row[6]),
    )


def _to_proposal(row: Any) -> PromotionProposal:
    return PromotionProposal(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        member_id=int(row[3]),
        criteria_id=int(row[4]),
        proposed_by=int(row[5]),
        status=str(row[6]),
        reason=_optional_text(row[7]),
        decided_by=_optional_int(row[8]),
        created_at=str(row[9]),
        decided_at=_optional_text(row[10]),
    )


_TRACK_COLUMNS = """id, guild_id, family_id, member_id, application_number,
                    specialization, mentor_id, current_step, status,
                    started_at, probation_until"""
_ASSIGN_COLUMNS = """id, guild_id, family_id, track_id, member_id, mentor_id,
                     specialization, step_key, title, status, created_at,
                     completed_at"""
_RULES_COLUMNS = """id, guild_id, family_id, activity, version,
                    published_at, published_by"""
_CRITERIA_COLUMNS = """id, guild_id, family_id, title, body,
                       published_at, published_by"""
_PROPOSAL_COLUMNS = """id, guild_id, family_id, member_id, criteria_id,
                       proposed_by, status, reason, decided_by,
                       created_at, decided_at"""


async def create_track(
    *,
    guild_id: int,
    family_id: int,
    member_id: int,
    specialization: str,
    application_number: str | None = None,
    mentor_id: int | None = None,
    probation_until: str | None = None,
    started_at: str | None = None,
) -> OnboardingTrack:
    existing = await get_track_for_member(family_id, member_id)
    if existing is not None and existing.status == "active":
        return existing
    stamp = started_at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO onboarding_tracks (
                   guild_id, family_id, member_id, application_number,
                   specialization, mentor_id, current_step, status,
                   started_at, probation_until
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                guild_id,
                family_id,
                member_id,
                application_number,
                specialization,
                mentor_id,
                ONBOARDING_STEPS[0],
                stamp,
                probation_until,
            ),
        )
        track_id = _inserted_id(cursor)
        await db.commit()
    track = await get_track(track_id)
    if track is None:
        raise RuntimeError("onboarding track insert did not persist")
    return track


async def get_track(track_id: int) -> OnboardingTrack | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_TRACK_COLUMNS} FROM onboarding_tracks WHERE id = ?",
            (track_id,),
        )
    return _to_track(rows[0]) if rows else None


async def get_track_for_member(
    family_id: int, member_id: int
) -> OnboardingTrack | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_TRACK_COLUMNS}
                  FROM onboarding_tracks
                 WHERE family_id = ? AND member_id = ?
                 ORDER BY id DESC""",
            (family_id, member_id),
        )
    return _to_track(rows[0]) if rows else None


async def list_tracks_for_mentor(
    family_id: int, mentor_id: int
) -> list[OnboardingTrack]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_TRACK_COLUMNS}
                  FROM onboarding_tracks
                 WHERE family_id = ? AND mentor_id = ? AND status = 'active'
                 ORDER BY id""",
            (family_id, mentor_id),
        )
    return [_to_track(row) for row in rows]


async def set_track_step(
    track_id: int, step_key: str, *, status: str | None = None
) -> OnboardingTrack:
    if step_key not in ONBOARDING_STEPS:
        raise ValueError(f"unknown onboarding step: {step_key}")
    async with _connect() as db:
        await db.execute(
            """UPDATE onboarding_tracks
                  SET current_step = ?, status = COALESCE(?, status)
                WHERE id = ?""",
            (step_key, status, track_id),
        )
        await db.commit()
    track = await get_track(track_id)
    if track is None:
        raise RuntimeError("onboarding track disappeared")
    return track


async def set_track_mentor(track_id: int, mentor_id: int | None) -> OnboardingTrack:
    async with _connect() as db:
        await db.execute(
            "UPDATE onboarding_tracks SET mentor_id = ? WHERE id = ?",
            (mentor_id, track_id),
        )
        await db.commit()
    track = await get_track(track_id)
    if track is None:
        raise RuntimeError("onboarding track disappeared")
    return track


async def create_assignment(
    *,
    guild_id: int,
    family_id: int,
    track_id: int,
    member_id: int,
    specialization: str,
    step_key: str,
    title: str,
    mentor_id: int | None = None,
    created_at: str | None = None,
) -> TrainingAssignment:
    stamp = created_at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO training_assignments (
                   guild_id, family_id, track_id, member_id, mentor_id,
                   specialization, step_key, title, status, created_at,
                   completed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL)""",
            (
                guild_id,
                family_id,
                track_id,
                member_id,
                mentor_id,
                specialization,
                step_key,
                title,
                stamp,
            ),
        )
        assignment_id = _inserted_id(cursor)
        await db.commit()
    item = await get_assignment(assignment_id)
    if item is None:
        raise RuntimeError("training assignment insert did not persist")
    return item


async def get_assignment(assignment_id: int) -> TrainingAssignment | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_ASSIGN_COLUMNS} FROM training_assignments WHERE id = ?",
            (assignment_id,),
        )
    return _to_assignment(rows[0]) if rows else None


async def list_assignments_for_member(
    member_id: int, *, open_only: bool = False
) -> list[TrainingAssignment]:
    clause = "AND status = 'open'" if open_only else ""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_ASSIGN_COLUMNS}
                  FROM training_assignments
                 WHERE member_id = ? {clause}
                 ORDER BY id""",
            (member_id,),
        )
    return [_to_assignment(row) for row in rows]


async def complete_assignment(
    assignment_id: int, *, at: str | None = None
) -> TrainingAssignment | None:
    stamp = at or utc_now()
    async with _connect() as db:
        await db.execute(
            """UPDATE training_assignments
                  SET status = 'done', completed_at = ?
                WHERE id = ? AND status = 'open'""",
            (stamp, assignment_id),
        )
        await db.commit()
    return await get_assignment(assignment_id)


async def publish_rules_version(
    *,
    guild_id: int,
    family_id: int,
    activity: str,
    version: str,
    published_by: int,
    published_at: str | None = None,
) -> RulesVersion:
    stamp = published_at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO rules_versions (
                   guild_id, family_id, activity, version,
                   published_at, published_by
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, family_id, activity, version, stamp, published_by),
        )
        item_id = _inserted_id(cursor)
        await db.commit()
    item = await get_rules_version(item_id)
    if item is None:
        raise RuntimeError("rules version insert did not persist")
    return item


async def get_rules_version(item_id: int) -> RulesVersion | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_RULES_COLUMNS} FROM rules_versions WHERE id = ?",
            (item_id,),
        )
    return _to_rules(rows[0]) if rows else None


async def latest_rules_version(
    family_id: int, activity: str
) -> RulesVersion | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_RULES_COLUMNS}
                  FROM rules_versions
                 WHERE family_id = ? AND activity = ?
                 ORDER BY id DESC""",
            (family_id, activity),
        )
    return _to_rules(rows[0]) if rows else None


async def publish_criteria(
    *,
    guild_id: int,
    family_id: int,
    title: str,
    body: str,
    published_by: int,
    published_at: str | None = None,
) -> PromotionCriteria:
    stamp = published_at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO promotion_criteria (
                   guild_id, family_id, title, body, published_at, published_by
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, family_id, title, body, stamp, published_by),
        )
        item_id = _inserted_id(cursor)
        await db.commit()
    item = await get_criteria(item_id)
    if item is None:
        raise RuntimeError("promotion criteria insert did not persist")
    return item


async def get_criteria(item_id: int) -> PromotionCriteria | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_CRITERIA_COLUMNS} FROM promotion_criteria WHERE id = ?",
            (item_id,),
        )
    return _to_criteria(rows[0]) if rows else None


async def latest_criteria(family_id: int) -> PromotionCriteria | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_CRITERIA_COLUMNS}
                  FROM promotion_criteria
                 WHERE family_id = ?
                 ORDER BY id DESC""",
            (family_id,),
        )
    return _to_criteria(rows[0]) if rows else None


async def create_proposal(
    *,
    guild_id: int,
    family_id: int,
    member_id: int,
    criteria_id: int,
    proposed_by: int,
    created_at: str | None = None,
) -> PromotionProposal:
    existing = await get_pending_proposal(family_id, member_id, criteria_id)
    if existing is not None:
        return existing
    stamp = created_at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO promotion_proposals (
                   guild_id, family_id, member_id, criteria_id, proposed_by,
                   status, reason, decided_by, created_at, decided_at
               ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, NULL)""",
            (
                guild_id,
                family_id,
                member_id,
                criteria_id,
                proposed_by,
                stamp,
            ),
        )
        item_id = _inserted_id(cursor)
        await db.commit()
    item = await get_proposal(item_id)
    if item is None:
        raise RuntimeError("promotion proposal insert did not persist")
    return item


async def get_proposal(item_id: int) -> PromotionProposal | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_PROPOSAL_COLUMNS} FROM promotion_proposals WHERE id = ?",
            (item_id,),
        )
    return _to_proposal(rows[0]) if rows else None


async def list_pending_proposals(family_id: int) -> list[PromotionProposal]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_PROPOSAL_COLUMNS}
                  FROM promotion_proposals
                 WHERE family_id = ? AND status = 'pending'
                 ORDER BY created_at, id""",
            (family_id,),
        )
    return [_to_proposal(row) for row in rows]


async def get_pending_proposal(
    family_id: int, member_id: int, criteria_id: int
) -> PromotionProposal | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_PROPOSAL_COLUMNS}
                  FROM promotion_proposals
                 WHERE family_id = ? AND member_id = ?
                   AND criteria_id = ? AND status = 'pending'
                 ORDER BY id DESC""",
            (family_id, member_id, criteria_id),
        )
    return _to_proposal(rows[0]) if rows else None


async def decide_proposal(
    proposal_id: int,
    *,
    accepted: bool,
    decided_by: int,
    reason: str | None = None,
    at: str | None = None,
) -> PromotionProposal | None:
    stamp = at or utc_now()
    status = "accepted" if accepted else "rejected"
    async with _connect() as db:
        await db.execute(
            """UPDATE promotion_proposals
                  SET status = ?, decided_by = ?, reason = ?, decided_at = ?
                WHERE id = ? AND status = 'pending'""",
            (status, decided_by, reason, stamp, proposal_id),
        )
        await db.commit()
    return await get_proposal(proposal_id)


async def member_for_track(track: OnboardingTrack) -> Member | None:
    return await get_member(track.member_id)
