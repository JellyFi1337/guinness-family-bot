"""Family events and roster seats. Capacity is a number, not a button count."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from infrastructure.database import (
    DATABASE_INTEGRITY_ERRORS,
    connection as _connect,
    utc_now,
)
from infrastructure.transactions import begin_write

EVENT_STATUSES = frozenset({"scheduled", "cancelled"})
ROSTER_SLOTS = frozenset({"main", "reserve"})
ATTENDANCE_STATUSES = frozenset(
    {"joined", "ready", "attended", "reserve", "excused", "no_show"}
)
RESERVE_ORDER_BASIS = "время записи"

_EVENT_COLUMNS = (
    "id, guild_id, family_id, template_key, title, starts_at, ends_at, "
    "commander_id, meetup, capacity, reserve_capacity, resource_plan, "
    "f2_event_ref, snapshot_json, status, version, created_by, created_at, "
    "cancelled_at, cancelled_by, roster_locked_at, roster_locked_by"
)
_SEAT_COLUMNS = (
    "id, guild_id, family_id, event_id, discord_id, slot, role_key, joined_at, "
    "attendance, voice_seen_at, last_reason"
)
_LOG_COLUMNS = (
    "id, guild_id, family_id, event_id, discord_id, from_status, to_status, "
    "actor_id, reason, created_at"
)


@dataclass(frozen=True, slots=True)
class EventSeat:
    id: int
    guild_id: int
    family_id: int
    event_id: int
    discord_id: int
    slot: str
    role_key: str | None
    joined_at: str
    attendance: str
    voice_seen_at: str | None
    last_reason: str | None


@dataclass(frozen=True, slots=True)
class FamilyEvent:
    id: int
    guild_id: int
    family_id: int
    template_key: str
    title: str
    starts_at: str
    ends_at: str
    commander_id: int
    meetup: str
    capacity: int
    reserve_capacity: int
    resource_plan: str
    f2_event_ref: str | None
    snapshot: dict[str, Any]
    status: str
    version: int
    created_by: int
    created_at: str
    cancelled_at: str | None
    cancelled_by: int | None
    roster_locked_at: str | None
    roster_locked_by: int | None

    @property
    def roster_locked(self) -> bool:
        return self.roster_locked_at is not None


@dataclass(frozen=True, slots=True)
class AttendanceLog:
    id: int
    guild_id: int
    family_id: int
    event_id: int
    discord_id: int
    from_status: str
    to_status: str
    actor_id: int
    reason: str | None
    created_at: str


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _inserted_id(cursor: Any) -> int:
    row_id = int(cursor.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("insert did not return an id")
    return row_id


def _parse_snapshot(raw: object) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    loaded = json.loads(str(raw))
    return loaded if isinstance(loaded, dict) else {}


def _to_event(row: Any) -> FamilyEvent:
    return FamilyEvent(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        template_key=str(row[3]),
        title=str(row[4]),
        starts_at=str(row[5]),
        ends_at=str(row[6]),
        commander_id=int(row[7]),
        meetup=str(row[8]),
        capacity=int(row[9]),
        reserve_capacity=int(row[10]),
        resource_plan=str(row[11] or ""),
        f2_event_ref=_optional_text(row[12]),
        snapshot=_parse_snapshot(row[13]),
        status=str(row[14]),
        version=int(row[15]),
        created_by=int(row[16]),
        created_at=str(row[17]),
        cancelled_at=_optional_text(row[18]),
        cancelled_by=_optional_int(row[19]),
        roster_locked_at=_optional_text(row[20]),
        roster_locked_by=_optional_int(row[21]),
    )


def _to_seat(row: Any) -> EventSeat:
    return EventSeat(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        event_id=int(row[3]),
        discord_id=int(row[4]),
        slot=str(row[5]),
        role_key=_optional_text(row[6]),
        joined_at=str(row[7]),
        attendance=str(row[8] or ("reserve" if str(row[5]) == "reserve" else "joined")),
        voice_seen_at=_optional_text(row[9]),
        last_reason=_optional_text(row[10]),
    )


def _to_log(row: Any) -> AttendanceLog:
    return AttendanceLog(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        event_id=int(row[3]),
        discord_id=int(row[4]),
        from_status=str(row[5]),
        to_status=str(row[6]),
        actor_id=int(row[7]),
        reason=_optional_text(row[8]),
        created_at=str(row[9]),
    )


async def get_event(event_id: int) -> FamilyEvent | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_EVENT_COLUMNS} FROM family_events WHERE id = ?",
            (event_id,),
        )
    return _to_event(rows[0]) if rows else None


async def find_event_by_start(
    family_id: int, template_key: str, starts_at: str
) -> FamilyEvent | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_EVENT_COLUMNS} FROM family_events
                WHERE family_id = ? AND template_key = ? AND starts_at = ?""",
            (family_id, template_key, starts_at),
        )
    return _to_event(rows[0]) if rows else None


async def list_scheduled_events(family_id: int) -> list[FamilyEvent]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_EVENT_COLUMNS} FROM family_events
                WHERE family_id = ? AND status = 'scheduled'
                ORDER BY starts_at, id""",
            (family_id,),
        )
    return [_to_event(row) for row in rows]


async def list_recent_events(family_id: int, *, limit: int = 40) -> list[FamilyEvent]:
    if limit < 1:
        return []
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_EVENT_COLUMNS} FROM family_events
                WHERE family_id = ?
                ORDER BY starts_at DESC, id DESC
                LIMIT ?""",
            (family_id, limit),
        )
    return [_to_event(row) for row in rows]


async def list_member_events(
    family_id: int, discord_id: int
) -> list[tuple[FamilyEvent, EventSeat]]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_EVENT_COLUMNS} FROM family_events
                WHERE family_id = ? AND status = 'scheduled'
                  AND id IN (
                      SELECT event_id FROM event_roster
                       WHERE family_id = ? AND discord_id = ?
                  )
                ORDER BY starts_at, id""",
            (family_id, family_id, discord_id),
        )
        events = [_to_event(row) for row in rows]
        result: list[tuple[FamilyEvent, EventSeat]] = []
        for event in events:
            seats = await db.execute_fetchall(
                f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                    WHERE event_id = ? AND discord_id = ?""",
                (event.id, discord_id),
            )
            if seats:
                result.append((event, _to_seat(seats[0])))
    return result


async def insert_event(
    *,
    guild_id: int,
    family_id: int,
    template_key: str,
    title: str,
    starts_at: str,
    ends_at: str,
    commander_id: int,
    meetup: str,
    capacity: int,
    reserve_capacity: int,
    resource_plan: str,
    f2_event_ref: str | None,
    snapshot: dict[str, Any],
    created_by: int,
) -> FamilyEvent:
    stamp = utc_now()
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    async with _connect() as db:
        await begin_write(db, "event-create", f"{family_id}:{template_key}:{starts_at}")
        existing = await db.execute_fetchall(
            f"""SELECT {_EVENT_COLUMNS} FROM family_events
                WHERE family_id = ? AND template_key = ? AND starts_at = ?""",
            (family_id, template_key, starts_at),
        )
        if existing:
            await db.commit()
            return _to_event(existing[0])
        try:
            cursor = await db.execute(
                """INSERT INTO family_events (
                       guild_id, family_id, template_key, title, starts_at,
                       ends_at, commander_id, meetup, capacity, reserve_capacity,
                       resource_plan, f2_event_ref, snapshot_json, status,
                       version, created_by, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled',
                             1, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    template_key,
                    title,
                    starts_at,
                    ends_at,
                    commander_id,
                    meetup,
                    capacity,
                    reserve_capacity,
                    resource_plan,
                    f2_event_ref,
                    payload,
                    created_by,
                    stamp,
                ),
            )
            event_id = _inserted_id(cursor)
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            loaded = await find_event_by_start(family_id, template_key, starts_at)
            if loaded is None:
                raise
            return loaded
    loaded = await get_event(event_id)
    if loaded is None:
        raise RuntimeError("event insert did not persist")
    return loaded


async def update_event(
    event_id: int,
    *,
    expected_version: int,
    starts_at: str | None = None,
    ends_at: str | None = None,
    commander_id: int | None = None,
    meetup: str | None = None,
    status: str | None = None,
    cancelled_by: int | None = None,
    roster_locked_by: int | None = None,
    snapshot: dict[str, Any] | None = None,
) -> FamilyEvent | None:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "event-update", event_id)
        rows = await db.execute_fetchall(
            f"SELECT {_EVENT_COLUMNS} FROM family_events WHERE id = ?",
            (event_id,),
        )
        if not rows:
            await db.commit()
            return None
        current = _to_event(rows[0])
        if current.version != expected_version:
            await db.commit()
            return None
        fields: list[str] = ["version = version + 1"]
        params: list[Any] = []
        if starts_at is not None:
            fields.append("starts_at = ?")
            params.append(starts_at)
        if ends_at is not None:
            fields.append("ends_at = ?")
            params.append(ends_at)
        if commander_id is not None:
            fields.append("commander_id = ?")
            params.append(commander_id)
        if meetup is not None:
            fields.append("meetup = ?")
            params.append(meetup)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
            if status == "cancelled":
                fields.append("cancelled_at = ?")
                fields.append("cancelled_by = ?")
                params.extend((stamp, cancelled_by))
        if roster_locked_by is not None:
            fields.append("roster_locked_at = ?")
            fields.append("roster_locked_by = ?")
            params.extend((stamp, roster_locked_by))
        if snapshot is not None:
            fields.append("snapshot_json = ?")
            params.append(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
        params.extend((event_id, expected_version))
        cursor = await db.execute(
            f"""UPDATE family_events SET {', '.join(fields)}
                WHERE id = ? AND version = ?""",
            tuple(params),
        )
        await db.commit()
        if cursor.rowcount <= 0:
            return None
    return await get_event(event_id)


async def list_roster(event_id: int) -> list[EventSeat]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ?
                ORDER BY slot, joined_at, id""",
            (event_id,),
        )
    return [_to_seat(row) for row in rows]


async def get_seat(event_id: int, discord_id: int) -> EventSeat | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ? AND discord_id = ?""",
            (event_id, discord_id),
        )
    return _to_seat(rows[0]) if rows else None


async def count_slots(event_id: int) -> tuple[int, int]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT slot, COUNT(*) FROM event_roster
                WHERE event_id = ? GROUP BY slot""",
            (event_id,),
        )
    return _slot_counts(rows)


async def count_role(event_id: int, role_key: str) -> int:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT COUNT(*) FROM event_roster
                WHERE event_id = ? AND role_key = ? AND slot = 'main'""",
            (event_id, role_key),
        )
    return int(rows[0][0]) if rows else 0


def _role_quota(snapshot: dict[str, Any], role_key: str) -> int | None:
    for item in snapshot.get("roles") or []:
        if isinstance(item, dict) and str(item.get("key")) == role_key:
            return int(item.get("quota") or 0)
    return None


def _slot_counts(rows: list[Any]) -> tuple[int, int]:
    main = 0
    reserve = 0
    for slot, count in rows:
        if str(slot) == "main":
            main = int(count)
        elif str(slot) == "reserve":
            reserve = int(count)
    return main, reserve


async def try_insert_seat(
    *,
    guild_id: int,
    family_id: int,
    event_id: int,
    discord_id: int,
    slot: str,
    role_key: str | None,
) -> EventSeat | None:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "event-join", event_id)
        rows = await db.execute_fetchall(
            f"SELECT {_EVENT_COLUMNS} FROM family_events WHERE id = ?",
            (event_id,),
        )
        if not rows:
            await db.commit()
            return None
        event = _to_event(rows[0])
        if event.status != "scheduled":
            await db.commit()
            return None
        existing = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ? AND discord_id = ?""",
            (event_id, discord_id),
        )
        if existing:
            await db.commit()
            return _to_seat(existing[0])
        counts = await db.execute_fetchall(
            """SELECT slot, COUNT(*) FROM event_roster
                WHERE event_id = ? GROUP BY slot""",
            (event_id,),
        )
        main, reserve = _slot_counts(counts)
        if slot == "main" and main < event.capacity:
            chosen = "main"
        elif slot == "reserve" and reserve < event.reserve_capacity:
            chosen = "reserve"
        elif main < event.capacity:
            chosen = "main"
        elif reserve < event.reserve_capacity:
            chosen = "reserve"
        else:
            await db.commit()
            return None
        if role_key is not None:
            quota = _role_quota(event.snapshot, role_key)
            if quota is None:
                await db.commit()
                return None
            if chosen == "main" and quota > 0:
                role_rows = await db.execute_fetchall(
                    """SELECT COUNT(*) FROM event_roster
                        WHERE event_id = ? AND role_key = ? AND slot = 'main'""",
                    (event_id, role_key),
                )
                taken = int(role_rows[0][0]) if role_rows else 0
                if taken >= quota:
                    await db.commit()
                    return None
        try:
            attendance = "reserve" if chosen == "reserve" else "joined"
            cursor = await db.execute(
                """INSERT INTO event_roster (
                       guild_id, family_id, event_id, discord_id, slot,
                       role_key, joined_at, attendance
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    event_id,
                    discord_id,
                    chosen,
                    role_key,
                    stamp,
                    attendance,
                ),
            )
            _inserted_id(cursor)
            await _insert_log(
                db,
                guild_id=guild_id,
                family_id=family_id,
                event_id=event_id,
                discord_id=discord_id,
                from_status=attendance,
                to_status=attendance,
                actor_id=discord_id,
                reason="запись",
                created_at=stamp,
            )
            await db.execute(
                """UPDATE family_events SET version = version + 1
                    WHERE id = ?""",
                (event_id,),
            )
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            return None
    return await get_seat(event_id, discord_id)


async def _insert_log(
    db: Any,
    *,
    guild_id: int,
    family_id: int,
    event_id: int,
    discord_id: int,
    from_status: str,
    to_status: str,
    actor_id: int,
    reason: str | None,
    created_at: str,
) -> None:
    await db.execute(
        """INSERT INTO event_attendance_log (
               guild_id, family_id, event_id, discord_id, from_status,
               to_status, actor_id, reason, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            guild_id,
            family_id,
            event_id,
            discord_id,
            from_status,
            to_status,
            actor_id,
            reason,
            created_at,
        ),
    )


async def delete_seat(event_id: int, discord_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM event_roster WHERE event_id = ? AND discord_id = ?",
            (event_id, discord_id),
        )
        await db.commit()
    return cursor.rowcount > 0


async def list_reserve_queue(event_id: int) -> list[EventSeat]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ? AND slot = 'reserve'
                ORDER BY joined_at, id""",
            (event_id,),
        )
    return [_to_seat(row) for row in rows]


async def list_attendance_log(event_id: int, discord_id: int | None = None) -> list[AttendanceLog]:
    if discord_id is None:
        query = f"""SELECT {_LOG_COLUMNS} FROM event_attendance_log
                    WHERE event_id = ? ORDER BY id"""
        params: tuple[Any, ...] = (event_id,)
    else:
        query = f"""SELECT {_LOG_COLUMNS} FROM event_attendance_log
                    WHERE event_id = ? AND discord_id = ? ORDER BY id"""
        params = (event_id, discord_id)
    async with _connect() as db:
        rows = await db.execute_fetchall(query, params)
    return [_to_log(row) for row in rows]


async def list_member_event_history(
    family_id: int, discord_id: int
) -> list[tuple[FamilyEvent, EventSeat]]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_EVENT_COLUMNS} FROM family_events
                WHERE family_id = ? AND id IN (
                    SELECT event_id FROM event_roster
                     WHERE family_id = ? AND discord_id = ?
                )
                ORDER BY starts_at, id""",
            (family_id, family_id, discord_id),
        )
        events = [_to_event(row) for row in rows]
        result: list[tuple[FamilyEvent, EventSeat]] = []
        for event in events:
            seats = await db.execute_fetchall(
                f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                    WHERE event_id = ? AND discord_id = ?""",
                (event.id, discord_id),
            )
            if seats:
                result.append((event, _to_seat(seats[0])))
    return result


async def set_seat_attendance(
    event_id: int,
    discord_id: int,
    *,
    attendance: str,
    actor_id: int,
    reason: str | None = None,
    voice_seen: bool = False,
    last_reason: str | None = None,
) -> EventSeat | None:
    if attendance not in ATTENDANCE_STATUSES:
        raise ValueError(f"unknown attendance: {attendance}")
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "event-attendance", event_id)
        rows = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ? AND discord_id = ?""",
            (event_id, discord_id),
        )
        if not rows:
            await db.commit()
            return None
        current = _to_seat(rows[0])
        fields = ["attendance = ?"]
        params: list[Any] = [attendance]
        if voice_seen:
            fields.append("voice_seen_at = ?")
            params.append(stamp)
        if last_reason is not None:
            fields.append("last_reason = ?")
            params.append(last_reason)
        params.extend((event_id, discord_id))
        await db.execute(
            f"""UPDATE event_roster SET {', '.join(fields)}
                WHERE event_id = ? AND discord_id = ?""",
            tuple(params),
        )
        if current.attendance != attendance or reason:
            await _insert_log(
                db,
                guild_id=current.guild_id,
                family_id=current.family_id,
                event_id=event_id,
                discord_id=discord_id,
                from_status=current.attendance,
                to_status=attendance,
                actor_id=actor_id,
                reason=reason,
                created_at=stamp,
            )
        await db.execute(
            "UPDATE family_events SET version = version + 1 WHERE id = ?",
            (event_id,),
        )
        await db.commit()
    return await get_seat(event_id, discord_id)


async def note_voice_seen(event_id: int, discord_id: int, *, actor_id: int) -> EventSeat | None:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "event-attendance", event_id)
        rows = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ? AND discord_id = ?""",
            (event_id, discord_id),
        )
        if not rows:
            await db.commit()
            return None
        current = _to_seat(rows[0])
        await db.execute(
            """UPDATE event_roster SET voice_seen_at = ?
                WHERE event_id = ? AND discord_id = ?""",
            (stamp, event_id, discord_id),
        )
        await _insert_log(
            db,
            guild_id=current.guild_id,
            family_id=current.family_id,
            event_id=event_id,
            discord_id=discord_id,
            from_status=current.attendance,
            to_status=current.attendance,
            actor_id=actor_id,
            reason="голос (подсказка, не явка)",
            created_at=stamp,
        )
        await db.commit()
    return await get_seat(event_id, discord_id)


async def release_seat(
    event_id: int,
    discord_id: int,
    *,
    actor_id: int,
    auto_promote: bool,
    reason: str | None = None,
) -> tuple[EventSeat | None, EventSeat | None]:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "event-join", event_id)
        rows = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ? AND discord_id = ?""",
            (event_id, discord_id),
        )
        if not rows:
            await db.commit()
            return None, None
        removed = _to_seat(rows[0])
        await db.execute(
            "DELETE FROM event_roster WHERE event_id = ? AND discord_id = ?",
            (event_id, discord_id),
        )
        await _insert_log(
            db,
            guild_id=removed.guild_id,
            family_id=removed.family_id,
            event_id=event_id,
            discord_id=discord_id,
            from_status=removed.attendance,
            to_status=removed.attendance,
            actor_id=actor_id,
            reason=reason or "снял запись",
            created_at=stamp,
        )
        promoted: EventSeat | None = None
        if auto_promote and removed.slot == "main":
            waiting = await db.execute_fetchall(
                f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                    WHERE event_id = ? AND slot = 'reserve'
                    ORDER BY joined_at, id LIMIT 1""",
                (event_id,),
            )
            if waiting:
                candidate = _to_seat(waiting[0])
                next_status = "ready" if candidate.attendance == "ready" else "joined"
                await db.execute(
                    """UPDATE event_roster
                          SET slot = 'main', attendance = ?
                        WHERE id = ?""",
                    (next_status, candidate.id),
                )
                await _insert_log(
                    db,
                    guild_id=candidate.guild_id,
                    family_id=candidate.family_id,
                    event_id=event_id,
                    discord_id=candidate.discord_id,
                    from_status=candidate.attendance,
                    to_status=next_status,
                    actor_id=actor_id,
                    reason="автоподъём резерва",
                    created_at=stamp,
                )
                promoted_rows = await db.execute_fetchall(
                    f"SELECT {_SEAT_COLUMNS} FROM event_roster WHERE id = ?",
                    (candidate.id,),
                )
                promoted = _to_seat(promoted_rows[0]) if promoted_rows else None
        await db.execute(
            "UPDATE family_events SET version = version + 1 WHERE id = ?",
            (event_id,),
        )
        await db.commit()
    return removed, promoted


async def promote_into_main(
    event_id: int,
    discord_id: int,
    *,
    actor_id: int,
    reason: str,
) -> EventSeat | None:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "event-join", event_id)
        events = await db.execute_fetchall(
            f"SELECT {_EVENT_COLUMNS} FROM family_events WHERE id = ?",
            (event_id,),
        )
        if not events:
            await db.commit()
            return None
        event = _to_event(events[0])
        counts = await db.execute_fetchall(
            """SELECT slot, COUNT(*) FROM event_roster
                WHERE event_id = ? GROUP BY slot""",
            (event_id,),
        )
        main, _reserve = _slot_counts(counts)
        if main >= event.capacity:
            await db.commit()
            return None
        rows = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ? AND discord_id = ?""",
            (event_id, discord_id),
        )
        if not rows:
            await db.commit()
            return None
        seat = _to_seat(rows[0])
        if seat.slot != "reserve":
            await db.commit()
            return seat
        next_status = "ready" if seat.attendance == "ready" else "joined"
        await db.execute(
            """UPDATE event_roster SET slot = 'main', attendance = ?
                WHERE id = ?""",
            (next_status, seat.id),
        )
        await _insert_log(
            db,
            guild_id=seat.guild_id,
            family_id=seat.family_id,
            event_id=event_id,
            discord_id=discord_id,
            from_status=seat.attendance,
            to_status=next_status,
            actor_id=actor_id,
            reason=reason,
            created_at=stamp,
        )
        await db.execute(
            "UPDATE family_events SET version = version + 1 WHERE id = ?",
            (event_id,),
        )
        await db.commit()
    return await get_seat(event_id, discord_id)


async def replace_main_with_reserve(
    event_id: int,
    outgoing_id: int,
    incoming_id: int,
    *,
    actor_id: int,
    reason: str,
) -> tuple[EventSeat | None, EventSeat | None]:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "event-join", event_id)
        outgoing_rows = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ? AND discord_id = ?""",
            (event_id, outgoing_id),
        )
        incoming_rows = await db.execute_fetchall(
            f"""SELECT {_SEAT_COLUMNS} FROM event_roster
                WHERE event_id = ? AND discord_id = ?""",
            (event_id, incoming_id),
        )
        if not outgoing_rows or not incoming_rows:
            await db.commit()
            return None, None
        outgoing = _to_seat(outgoing_rows[0])
        incoming = _to_seat(incoming_rows[0])
        if outgoing.slot != "main" or incoming.slot != "reserve":
            await db.commit()
            return None, None
        next_status = "ready" if incoming.attendance == "ready" else "joined"
        await db.execute(
            """UPDATE event_roster
                  SET slot = 'reserve', attendance = 'reserve', last_reason = ?
                WHERE id = ?""",
            (reason, outgoing.id),
        )
        await db.execute(
            """UPDATE event_roster
                  SET slot = 'main', attendance = ?, last_reason = ?
                WHERE id = ?""",
            (next_status, reason, incoming.id),
        )
        await _insert_log(
            db,
            guild_id=outgoing.guild_id,
            family_id=outgoing.family_id,
            event_id=event_id,
            discord_id=outgoing_id,
            from_status=outgoing.attendance,
            to_status="reserve",
            actor_id=actor_id,
            reason=reason,
            created_at=stamp,
        )
        await _insert_log(
            db,
            guild_id=incoming.guild_id,
            family_id=incoming.family_id,
            event_id=event_id,
            discord_id=incoming_id,
            from_status=incoming.attendance,
            to_status=next_status,
            actor_id=actor_id,
            reason=reason,
            created_at=stamp,
        )
        await db.execute(
            "UPDATE family_events SET version = version + 1 WHERE id = ?",
            (event_id,),
        )
        await db.commit()
    return await get_seat(event_id, outgoing_id), await get_seat(event_id, incoming_id)
