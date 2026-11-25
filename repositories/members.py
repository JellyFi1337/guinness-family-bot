"""Family roster, clearances and append-only membership history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.profile import active_profile
from infrastructure.database import connection as _connect, utc_now
from repositories.family import FamilyContext, Rank, get_family, get_rank

MEMBER_STATUSES = frozenset(
    {"candidate", "probation", "active", "leave", "left"}
)
CURRENT_STATUSES = frozenset({"candidate", "probation", "active", "leave"})
MEMBER_EVENT_TYPES = frozenset({"join", "promote", "demote", "leave", "exit"})
STATUS_LEFT = "left"


@dataclass(frozen=True, slots=True)
class Member:
    id: int
    guild_id: int
    family_id: int
    discord_id: int
    ic_name: str
    game_character_id: int | None
    joined_at: str
    left_at: str | None
    status: str
    rank_id: int | None
    mentor_id: int | None
    direction: str | None
    prime_time: str | None
    verified_at: str | None
    verified_by: int | None
    is_selected: bool


@dataclass(frozen=True, slots=True)
class Clearance:
    id: int
    guild_id: int
    family_id: int
    member_id: int
    activity: str
    granted_by: int
    granted_at: str
    expires_at: str | None
    rules_version: str | None
    revoked_at: str | None
    revoked_reason: str | None
    review_required_at: str | None = None


@dataclass(frozen=True, slots=True)
class MemberEvent:
    id: int
    guild_id: int
    family_id: int
    member_id: int
    discord_id: int
    event_type: str
    actor_id: int
    actor_name: str
    reason: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class MemberProfile:
    member: Member
    family: FamilyContext
    rank: Rank | None
    mentor: Member | None
    clearances: tuple[Clearance, ...]
    events: tuple[MemberEvent, ...]


@dataclass(frozen=True, slots=True)
class RosterLimit:
    current: int
    limit: int | None

    @property
    def has_limit(self) -> bool:
        return self.limit is not None

    @property
    def at_or_over_limit(self) -> bool:
        if self.limit is None:
            return False
        return self.current >= self.limit


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


def _to_member(row: Any) -> Member:
    return Member(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        discord_id=int(row[3]),
        ic_name=str(row[4]),
        game_character_id=_optional_int(row[5]),
        joined_at=str(row[6]),
        left_at=_optional_text(row[7]),
        status=str(row[8]),
        rank_id=_optional_int(row[9]),
        mentor_id=_optional_int(row[10]),
        direction=_optional_text(row[11]),
        prime_time=_optional_text(row[12]),
        verified_at=_optional_text(row[13]),
        verified_by=_optional_int(row[14]),
        is_selected=int(row[15]) == 1,
    )


def _to_clearance(row: Any) -> Clearance:
    return Clearance(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        member_id=int(row[3]),
        activity=str(row[4]),
        granted_by=int(row[5]),
        granted_at=str(row[6]),
        expires_at=_optional_text(row[7]),
        rules_version=_optional_text(row[8]),
        revoked_at=_optional_text(row[9]),
        revoked_reason=_optional_text(row[10]),
        review_required_at=_optional_text(row[11]) if len(row) > 11 else None,
    )


def _to_event(row: Any) -> MemberEvent:
    return MemberEvent(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        member_id=int(row[3]),
        discord_id=int(row[4]),
        event_type=str(row[5]),
        actor_id=int(row[6]),
        actor_name=str(row[7]),
        reason=_optional_text(row[8]),
        created_at=str(row[9]),
    )


_MEMBER_COLUMNS = """id, guild_id, family_id, discord_id, ic_name,
                     game_character_id, joined_at, left_at, status, rank_id,
                     mentor_id, direction, prime_time, verified_at, verified_by,
                     is_selected"""
_CLEARANCE_COLUMNS = """id, guild_id, family_id, member_id, activity, granted_by,
                        granted_at, expires_at, rules_version, revoked_at,
                        revoked_reason, review_required_at"""
_EVENT_COLUMNS = """id, guild_id, family_id, member_id, discord_id, event_type,
                    actor_id, actor_name, reason, created_at"""


def _require_status(status: str) -> str:
    if status not in MEMBER_STATUSES:
        raise ValueError(f"unknown member status: {status}")
    return status


def _require_event_type(event_type: str) -> str:
    if event_type not in MEMBER_EVENT_TYPES:
        raise ValueError(f"unknown member event type: {event_type}")
    return event_type


async def _insert_event(
    db: Any,
    *,
    guild_id: int,
    family_id: int,
    member_id: int,
    discord_id: int,
    event_type: str,
    actor_id: int,
    actor_name: str,
    reason: str | None,
    created_at: str,
) -> int:
    cursor = await db.execute(
        """INSERT INTO member_events (
               guild_id, family_id, member_id, discord_id, event_type,
               actor_id, actor_name, reason, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            guild_id,
            family_id,
            member_id,
            discord_id,
            _require_event_type(event_type),
            actor_id,
            actor_name,
            reason,
            created_at,
        ),
    )
    return _inserted_id(cursor)


async def add_member(
    *,
    guild_id: int,
    family_id: int,
    discord_id: int,
    ic_name: str,
    actor_id: int,
    actor_name: str,
    status: str = "candidate",
    game_character_id: int | None = None,
    rank_id: int | None = None,
    mentor_id: int | None = None,
    direction: str | None = None,
    prime_time: str | None = None,
    is_selected: bool = True,
    reason: str | None = None,
    joined_at: str | None = None,
) -> Member:
    stamp = joined_at or utc_now()
    status = _require_status(status)
    if status == STATUS_LEFT:
        raise ValueError("new membership cannot start as left")
    selected = 1 if is_selected else 0
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO members (
                   guild_id, family_id, discord_id, ic_name, game_character_id,
                   joined_at, left_at, status, rank_id, mentor_id, direction,
                   prime_time, verified_at, verified_by, is_selected
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
            (
                guild_id,
                family_id,
                discord_id,
                ic_name,
                game_character_id,
                stamp,
                status,
                rank_id,
                mentor_id,
                direction,
                prime_time,
                selected,
            ),
        )
        member_id = _inserted_id(cursor)
        await _insert_event(
            db,
            guild_id=guild_id,
            family_id=family_id,
            member_id=member_id,
            discord_id=discord_id,
            event_type="join",
            actor_id=actor_id,
            actor_name=actor_name,
            reason=reason,
            created_at=stamp,
        )
        await db.commit()
    member = await get_member(member_id)
    if member is None:
        raise RuntimeError("members insert did not persist")
    return member


async def get_member(member_id: int) -> Member | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_MEMBER_COLUMNS} FROM members WHERE id = ?",
            (member_id,),
        )
    return _to_member(rows[0]) if rows else None


async def get_current_member(
    family_id: int, discord_id: int, *, selected_only: bool = True
) -> Member | None:
    selected_clause = "AND is_selected = 1" if selected_only else ""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_MEMBER_COLUMNS}
                  FROM members
                 WHERE family_id = ?
                   AND discord_id = ?
                   AND status != ?
                   {selected_clause}
                 ORDER BY is_selected DESC, id""",
            (family_id, discord_id, STATUS_LEFT),
        )
    return _to_member(rows[0]) if rows else None


async def list_current_roster(
    family_id: int, *, selected_only: bool = True
) -> list[Member]:
    selected_clause = "AND is_selected = 1" if selected_only else ""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_MEMBER_COLUMNS}
                  FROM members
                 WHERE family_id = ?
                   AND status != ?
                   {selected_clause}
                 ORDER BY id""",
            (family_id, STATUS_LEFT),
        )
    return [_to_member(row) for row in rows]


async def list_active_roster(family_id: int) -> list[Member]:
    return await list_roster_by_status(family_id, "active")


async def list_roster_by_status(
    family_id: int, status: str, *, selected_only: bool = True
) -> list[Member]:
    status = _require_status(status)
    selected_clause = "AND is_selected = 1" if selected_only else ""
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_MEMBER_COLUMNS}
                  FROM members
                 WHERE family_id = ?
                   AND status = ?
                   {selected_clause}
                 ORDER BY id""",
            (family_id, status),
        )
    return [_to_member(row) for row in rows]


_ROSTER_SORTS: dict[str, str] = {
    "name": "ic_name COLLATE NOCASE, id",
    "joined": "joined_at, id",
    "status": "status, ic_name COLLATE NOCASE, id",
    "rank": "rank_id, ic_name COLLATE NOCASE, id",
}


async def list_roster(
    family_id: int,
    *,
    status: str | None = None,
    rank_id: int | None = None,
    direction: str | None = None,
    mentor_id: int | None = None,
    prime_time: str | None = None,
    selected_only: bool = True,
    include_left: bool = False,
    sort: str = "name",
) -> list[Member]:
    order = _ROSTER_SORTS.get(sort, _ROSTER_SORTS["name"])
    clauses = ["family_id = ?"]
    params: list[Any] = [family_id]
    if status:
        clauses.append("status = ?")
        params.append(_require_status(status))
    elif not include_left:
        clauses.append("status != ?")
        params.append(STATUS_LEFT)
    if rank_id is not None:
        clauses.append("rank_id = ?")
        params.append(rank_id)
    if direction:
        clauses.append("direction = ?")
        params.append(direction)
    if mentor_id is not None:
        clauses.append("mentor_id = ?")
        params.append(mentor_id)
    if prime_time:
        clauses.append("prime_time = ?")
        params.append(prime_time)
    if selected_only:
        clauses.append("is_selected = 1")
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_MEMBER_COLUMNS}
                  FROM members
                 WHERE {' AND '.join(clauses)}
                 ORDER BY {order}""",
            tuple(params),
        )
    return [_to_member(row) for row in rows]


async def count_current_roster(family_id: int) -> int:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT COUNT(DISTINCT discord_id)
                 FROM members
                WHERE family_id = ? AND status != ?""",
            (family_id, STATUS_LEFT),
        )
    return int(rows[0][0]) if rows else 0


async def check_roster_limit(guild_id: int, family_id: int) -> RosterLimit:
    profile = await active_profile(guild_id)
    current = await count_current_roster(family_id)
    return RosterLimit(current=current, limit=profile.member_limit)


async def get_member_profile(member_id: int) -> MemberProfile | None:
    member = await get_member(member_id)
    if member is None:
        return None
    family = await get_family(member.family_id)
    if family is None:
        raise RuntimeError(f"member {member_id} has no family_context")
    rank = await get_rank(member.rank_id) if member.rank_id is not None else None
    mentor = await get_member(member.mentor_id) if member.mentor_id is not None else None
    return MemberProfile(
        member=member,
        family=family,
        rank=rank,
        mentor=mentor,
        clearances=tuple(await list_clearances(member_id)),
        events=tuple(await list_member_events(member_id)),
    )


async def list_member_events(member_id: int) -> list[MemberEvent]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_EVENT_COLUMNS}
                  FROM member_events
                 WHERE member_id = ?
                 ORDER BY id""",
            (member_id,),
        )
    return [_to_event(row) for row in rows]


async def list_person_history(family_id: int, discord_id: int) -> list[MemberEvent]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_EVENT_COLUMNS}
                  FROM member_events
                 WHERE family_id = ? AND discord_id = ?
                 ORDER BY id""",
            (family_id, discord_id),
        )
    return [_to_event(row) for row in rows]


async def append_member_event(
    *,
    member_id: int,
    event_type: str,
    actor_id: int,
    actor_name: str,
    reason: str | None = None,
    created_at: str | None = None,
) -> MemberEvent:
    member = await get_member(member_id)
    if member is None:
        raise ValueError(f"unknown member: {member_id}")
    stamp = created_at or utc_now()
    async with _connect() as db:
        event_id = await _insert_event(
            db,
            guild_id=member.guild_id,
            family_id=member.family_id,
            member_id=member.id,
            discord_id=member.discord_id,
            event_type=event_type,
            actor_id=actor_id,
            actor_name=actor_name,
            reason=reason,
            created_at=stamp,
        )
        await db.commit()
    events = await list_member_events(member_id)
    for event in events:
        if event.id == event_id:
            return event
    raise RuntimeError("member_events insert did not persist")


async def mark_leave(
    member_id: int,
    *,
    actor_id: int,
    actor_name: str,
    reason: str | None = None,
    at: str | None = None,
) -> Member:
    stamp = at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE members
                  SET status = 'leave'
                WHERE id = ? AND status != ?""",
            (member_id, STATUS_LEFT),
        )
        if int(cursor.rowcount or 0) == 0:
            raise ValueError(f"member {member_id} is not current")
        member_rows = await db.execute_fetchall(
            f"SELECT {_MEMBER_COLUMNS} FROM members WHERE id = ?",
            (member_id,),
        )
        member = _to_member(member_rows[0])
        await _insert_event(
            db,
            guild_id=member.guild_id,
            family_id=member.family_id,
            member_id=member.id,
            discord_id=member.discord_id,
            event_type="leave",
            actor_id=actor_id,
            actor_name=actor_name,
            reason=reason,
            created_at=stamp,
        )
        await db.commit()
    updated = await get_member(member_id)
    if updated is None:
        raise RuntimeError("member disappeared after leave")
    return updated


async def mark_exit(
    member_id: int,
    *,
    actor_id: int,
    actor_name: str,
    reason: str | None = None,
    at: str | None = None,
) -> Member:
    stamp = at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE members
                  SET status = ?, left_at = ?, is_selected = 0
                WHERE id = ? AND status != ?""",
            (STATUS_LEFT, stamp, member_id, STATUS_LEFT),
        )
        if int(cursor.rowcount or 0) == 0:
            raise ValueError(f"member {member_id} is not current")
        member_rows = await db.execute_fetchall(
            f"SELECT {_MEMBER_COLUMNS} FROM members WHERE id = ?",
            (member_id,),
        )
        member = _to_member(member_rows[0])
        await _insert_event(
            db,
            guild_id=member.guild_id,
            family_id=member.family_id,
            member_id=member.id,
            discord_id=member.discord_id,
            event_type="exit",
            actor_id=actor_id,
            actor_name=actor_name,
            reason=reason,
            created_at=stamp,
        )
        await db.commit()
    updated = await get_member(member_id)
    if updated is None:
        raise RuntimeError("member disappeared after exit")
    return updated


async def set_member_status(
    member_id: int,
    status: str,
    *,
    actor_id: int,
    actor_name: str,
    event_type: str,
    reason: str | None = None,
    at: str | None = None,
) -> Member:
    status = _require_status(status)
    if status == STATUS_LEFT:
        return await mark_exit(
            member_id,
            actor_id=actor_id,
            actor_name=actor_name,
            reason=reason,
            at=at,
        )
    stamp = at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE members
                  SET status = ?
                WHERE id = ? AND status != ?""",
            (status, member_id, STATUS_LEFT),
        )
        if int(cursor.rowcount or 0) == 0:
            raise ValueError(f"member {member_id} is not current")
        member_rows = await db.execute_fetchall(
            f"SELECT {_MEMBER_COLUMNS} FROM members WHERE id = ?",
            (member_id,),
        )
        member = _to_member(member_rows[0])
        await _insert_event(
            db,
            guild_id=member.guild_id,
            family_id=member.family_id,
            member_id=member.id,
            discord_id=member.discord_id,
            event_type=event_type,
            actor_id=actor_id,
            actor_name=actor_name,
            reason=reason,
            created_at=stamp,
        )
        await db.commit()
    updated = await get_member(member_id)
    if updated is None:
        raise RuntimeError("member disappeared after status change")
    return updated


async def verify_member(
    member_id: int,
    *,
    actor_id: int,
    actor_name: str,
    reason: str | None = None,
    at: str | None = None,
) -> Member:
    stamp = at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE members
                  SET status = 'active', verified_at = ?, verified_by = ?
                WHERE id = ? AND status != ?""",
            (stamp, actor_id, member_id, STATUS_LEFT),
        )
        if int(cursor.rowcount or 0) == 0:
            raise ValueError(f"member {member_id} is not current")
        member_rows = await db.execute_fetchall(
            f"SELECT {_MEMBER_COLUMNS} FROM members WHERE id = ?",
            (member_id,),
        )
        member = _to_member(member_rows[0])
        await _insert_event(
            db,
            guild_id=member.guild_id,
            family_id=member.family_id,
            member_id=member.id,
            discord_id=member.discord_id,
            event_type="promote",
            actor_id=actor_id,
            actor_name=actor_name,
            reason=reason,
            created_at=stamp,
        )
        await db.commit()
    updated = await get_member(member_id)
    if updated is None:
        raise RuntimeError("member disappeared after verify")
    return updated


async def select_character(member_id: int) -> Member:
    member = await get_member(member_id)
    if member is None:
        raise ValueError(f"unknown member: {member_id}")
    if member.status == STATUS_LEFT:
        raise ValueError("cannot select a departed membership")
    async with _connect() as db:
        await db.execute(
            """UPDATE members
                  SET is_selected = 0
                WHERE family_id = ?
                  AND discord_id = ?
                  AND status != ?
                  AND id != ?""",
            (member.family_id, member.discord_id, STATUS_LEFT, member.id),
        )
        await db.execute(
            "UPDATE members SET is_selected = 1 WHERE id = ?",
            (member.id,),
        )
        await db.commit()
    selected = await get_member(member_id)
    if selected is None:
        raise RuntimeError("member disappeared after select")
    return selected


async def grant_clearance(
    *,
    member_id: int,
    activity: str,
    granted_by: int,
    expires_at: str | None = None,
    rules_version: str | None = None,
    granted_at: str | None = None,
) -> Clearance:
    member = await get_member(member_id)
    if member is None:
        raise ValueError(f"unknown member: {member_id}")
    stamp = granted_at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO clearances (
                   guild_id, family_id, member_id, activity, granted_by,
                   granted_at, expires_at, rules_version, revoked_at,
                   revoked_reason, review_required_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)""",
            (
                member.guild_id,
                member.family_id,
                member.id,
                activity,
                granted_by,
                stamp,
                expires_at,
                rules_version,
            ),
        )
        clearance_id = _inserted_id(cursor)
        await db.commit()
    for item in await list_clearances(member_id):
        if item.id == clearance_id:
            return item
    raise RuntimeError("clearances insert did not persist")


async def revoke_clearance(
    clearance_id: int, *, reason: str | None = None, at: str | None = None
) -> Clearance | None:
    stamp = at or utc_now()
    async with _connect() as db:
        await db.execute(
            """UPDATE clearances
                  SET revoked_at = ?, revoked_reason = ?
                WHERE id = ? AND revoked_at IS NULL""",
            (stamp, reason, clearance_id),
        )
        rows = await db.execute_fetchall(
            f"SELECT {_CLEARANCE_COLUMNS} FROM clearances WHERE id = ?",
            (clearance_id,),
        )
        await db.commit()
    return _to_clearance(rows[0]) if rows else None


async def mark_clearances_for_review(
    *,
    family_id: int,
    activity: str,
    previous_version: str,
    at: str | None = None,
) -> int:
    stamp = at or utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE clearances
                  SET review_required_at = ?
                WHERE family_id = ?
                  AND activity = ?
                  AND rules_version = ?
                  AND revoked_at IS NULL
                  AND review_required_at IS NULL""",
            (stamp, family_id, activity, previous_version),
        )
        await db.commit()
        return int(cursor.rowcount or 0)


async def acknowledge_clearance_review(clearance_id: int) -> Clearance | None:
    async with _connect() as db:
        await db.execute(
            """UPDATE clearances
                  SET review_required_at = NULL
                WHERE id = ? AND revoked_at IS NULL""",
            (clearance_id,),
        )
        rows = await db.execute_fetchall(
            f"SELECT {_CLEARANCE_COLUMNS} FROM clearances WHERE id = ?",
            (clearance_id,),
        )
        await db.commit()
    return _to_clearance(rows[0]) if rows else None


async def list_clearances(member_id: int) -> list[Clearance]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_CLEARANCE_COLUMNS}
                  FROM clearances
                 WHERE member_id = ?
                 ORDER BY id""",
            (member_id,),
        )
    return [_to_clearance(row) for row in rows]


async def list_clearance_activities(family_id: int) -> list[str]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT DISTINCT activity FROM clearances
               WHERE family_id = ? AND revoked_at IS NULL
               ORDER BY activity""",
            (family_id,),
        )
    return [str(row[0]) for row in rows]
