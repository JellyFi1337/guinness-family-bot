"""The family car park: the fleet, bookings and property register.

Extracted from the former monolithic ``utils.database``. The names are
re-exported from there, so existing imports keep working while new code
depends on this module directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write

STATE_ACTIVE = "active"
STATE_NEEDS_RECON = "needs_recon"
STATE_RETURNED = "returned_to_owner"
VEHICLE_STATES = frozenset({STATE_ACTIVE, STATE_NEEDS_RECON, STATE_RETURNED})
PROPERTY_KINDS = frozenset({"office", "warehouse", "other"})

_CAR_COLUMNS = (
    "id, name, plate, owner, added_by, created_at, "
    "guild_id, family_id, owner_member_id, purpose, cargo_kg, "
    "restrictions, access_note, state, version"
)
_BOOKING_COLUMNS = (
    "id, car_id, user_id, ic_name, starts_at, ends_at, cancelled_at, "
    "cancelled_by, purpose, event_id, driver_member_id, returned_at, "
    "returned_by, warning"
)
_PROPERTY_COLUMNS = (
    "id, guild_id, family_id, kind, title, responsible_id, backup_id, "
    "due_at, last_paid_at, executed, version, created_at"
)


@dataclass(frozen=True, slots=True)
class Car:
    id: int
    name: str
    plate: str
    owner: str
    added_by: int
    created_at: str
    guild_id: int | None
    family_id: int | None
    owner_member_id: int | None
    purpose: str
    cargo_kg: int
    restrictions: str
    access_note: str
    state: str
    version: int


@dataclass(frozen=True, slots=True)
class CarBooking:
    id: int
    car_id: int
    user_id: int
    ic_name: str
    starts_at: str
    ends_at: str
    cancelled_at: str | None
    cancelled_by: int | None
    purpose: str
    event_id: int | None
    driver_member_id: int | None
    returned_at: str | None
    returned_by: int | None
    warning: str


@dataclass(frozen=True, slots=True)
class FamilyProperty:
    id: int
    guild_id: int
    family_id: int
    kind: str
    title: str
    responsible_id: int
    backup_id: int
    due_at: str
    last_paid_at: str | None
    executed: bool
    version: int
    created_at: str


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _to_car(row: Any) -> Car:
    return Car(
        id=int(row[0]),
        name=str(row[1]),
        plate=str(row[2]),
        owner=str(row[3]),
        added_by=int(row[4]),
        created_at=str(row[5]),
        guild_id=_optional_int(row[6]),
        family_id=_optional_int(row[7]),
        owner_member_id=_optional_int(row[8]),
        purpose=_text(row[9]),
        cargo_kg=int(row[10] or 0),
        restrictions=_text(row[11]),
        access_note=_text(row[12]),
        state=_text(row[13]) or STATE_ACTIVE,
        version=int(row[14] or 1),
    )


def _to_booking(row: Any) -> CarBooking:
    return CarBooking(
        id=int(row[0]),
        car_id=int(row[1]),
        user_id=int(row[2]),
        ic_name=str(row[3]),
        starts_at=str(row[4]),
        ends_at=str(row[5]),
        cancelled_at=None if row[6] is None else str(row[6]),
        cancelled_by=_optional_int(row[7]),
        purpose=_text(row[8]),
        event_id=_optional_int(row[9]),
        driver_member_id=_optional_int(row[10]),
        returned_at=None if row[11] is None else str(row[11]),
        returned_by=_optional_int(row[12]),
        warning=_text(row[13]),
    )


def _to_property(row: Any) -> FamilyProperty:
    return FamilyProperty(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        kind=str(row[3]),
        title=str(row[4]),
        responsible_id=int(row[5]),
        backup_id=int(row[6]),
        due_at=str(row[7]),
        last_paid_at=None if row[8] is None else str(row[8]),
        executed=bool(row[9]),
        version=int(row[10] or 1),
        created_at=str(row[11]),
    )


async def _one(db: Any, query: str, params: tuple[object, ...] = ()) -> Any:
    async with db.execute(query, params) as cursor:
        return await cursor.fetchone()


def is_bookable(state: str | None) -> bool:
    return (state or STATE_ACTIVE) == STATE_ACTIVE


async def add_car(
    name: str,
    plate: str,
    owner: str,
    added_by: int,
    *,
    guild_id: int | None = None,
    family_id: int | None = None,
    owner_member_id: int | None = None,
    purpose: str = "",
    cargo_kg: int = 0,
    restrictions: str = "",
    access_note: str = "",
    state: str = STATE_ACTIVE,
) -> int:
    status = state if state in VEHICLE_STATES else STATE_ACTIVE
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO cars (
                   name, plate, owner, added_by, created_at,
                   guild_id, family_id, owner_member_id, purpose, cargo_kg,
                   restrictions, access_note, state, version
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                name.strip(),
                plate.strip().upper(),
                owner.strip(),
                added_by,
                _utc_now(),
                guild_id,
                family_id,
                owner_member_id,
                purpose.strip(),
                int(cargo_kg),
                restrictions.strip(),
                access_note.strip(),
                status,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def get_car(car_id: int) -> Car | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"SELECT {_CAR_COLUMNS} FROM cars WHERE id = ?",
            (car_id,),
        )
    return None if row is None else _to_car(row)


async def list_member_cars(owner_member_id: int) -> list[Car]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_CAR_COLUMNS} FROM cars
                WHERE owner_member_id = ?
                ORDER BY id""",
            (owner_member_id,),
        )
    return [_to_car(row) for row in rows]


async def count_active_cars(family_id: int) -> int:
    async with _connect() as db:
        row = await _one(
            db,
            """SELECT COUNT(*) FROM cars
               WHERE family_id = ? AND state = ?""",
            (family_id, STATE_ACTIVE),
        )
    return int(row[0]) if row else 0


async def update_car(
    car_id: int,
    *,
    name: str | None = None,
    plate: str | None = None,
    owner: str | None = None,
    owner_member_id: int | None = None,
    clear_owner_member: bool = False,
    purpose: str | None = None,
    cargo_kg: int | None = None,
    restrictions: str | None = None,
    access_note: str | None = None,
    state: str | None = None,
    guild_id: int | None = None,
    family_id: int | None = None,
) -> Car | None:
    async with _connect() as db:
        await begin_write(db, "car", car_id)
        row = await _one(
            db,
            f"SELECT {_CAR_COLUMNS} FROM cars WHERE id = ?",
            (car_id,),
        )
        if row is None:
            await db.rollback()
            return None
        current = _to_car(row)
        new_state = current.state
        if state is not None:
            if state not in VEHICLE_STATES:
                await db.rollback()
                return None
            new_state = state
        linked = current.owner_member_id
        if clear_owner_member:
            linked = None
        elif owner_member_id is not None:
            linked = owner_member_id
        await db.execute(
            """UPDATE cars SET
                   name = ?, plate = ?, owner = ?, owner_member_id = ?,
                   purpose = ?, cargo_kg = ?, restrictions = ?,
                   access_note = ?, state = ?, guild_id = ?, family_id = ?,
                   version = version + 1
               WHERE id = ?""",
            (
                name.strip() if name and name.strip() else current.name,
                plate.strip().upper() if plate and plate.strip() else current.plate,
                owner.strip() if owner and owner.strip() else current.owner,
                linked,
                purpose.strip() if purpose is not None else current.purpose,
                current.cargo_kg if cargo_kg is None else int(cargo_kg),
                restrictions.strip() if restrictions is not None else current.restrictions,
                access_note.strip() if access_note is not None else current.access_note,
                new_state,
                current.guild_id if guild_id is None else guild_id,
                current.family_id if family_id is None else family_id,
                car_id,
            ),
        )
        await db.commit()
    return await get_car(car_id)


async def edit_car(
    car_id: int,
    *,
    name: str | None = None,
    plate: str | None = None,
    owner: str | None = None,
):
    async with _connect() as db:
        await begin_write(db, "car", car_id)
        car = await _one(
            db, "SELECT id, name, plate, owner FROM cars WHERE id = ?", (car_id,)
        )
        if car is None:
            await db.rollback()
            return None
        new_name = name.strip() if name and name.strip() else car[1]
        new_plate = plate.strip().upper() if plate and plate.strip() else car[2]
        new_owner = owner.strip() if owner and owner.strip() else car[3]
        await db.execute(
            "UPDATE cars SET name = ?, plate = ?, owner = ?, version = version + 1 WHERE id = ?",
            (new_name, new_plate, new_owner, car_id),
        )
        await db.commit()
        return car, (car_id, new_name, new_plate, new_owner)


async def get_cars_with_active_bookings():
    now = _utc_now()
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT c.id, c.name, c.plate, c.owner,
                      b.id, b.user_id, b.ic_name, b.ends_at,
                      c.state, c.owner_member_id, c.cargo_kg
               FROM cars c
               LEFT JOIN car_bookings b ON b.id = (
                   SELECT cb.id FROM car_bookings cb
                   WHERE cb.car_id = c.id AND cb.cancelled_at IS NULL
                     AND cb.returned_at IS NULL
                     AND cb.ends_at > ?
                   ORDER BY cb.ends_at DESC LIMIT 1
               )
               ORDER BY LOWER(c.name), LOWER(c.plate)""",
            (now,),
        )


async def remove_car(car_id: int):
    async with _connect() as db:
        await begin_write(db, "car", car_id)
        car = await _one(
            db, "SELECT id, name, plate, owner FROM cars WHERE id = ?", (car_id,)
        )
        if car is None:
            await db.rollback()
            return "missing", None
        active = await _one(
            db,
            """SELECT 1 FROM car_bookings WHERE car_id = ?
               AND cancelled_at IS NULL AND returned_at IS NULL
               AND ends_at > ? LIMIT 1""",
            (car_id, _utc_now()),
        )
        if active:
            await db.rollback()
            return "busy", car
        await db.execute("DELETE FROM cars WHERE id = ?", (car_id,))
        await db.commit()
        return "removed", car


async def create_car_booking(
    car_id: int,
    user_id: int,
    ic_name: str,
    starts_at: str,
    ends_at: str,
    *,
    purpose: str = "",
    event_id: int | None = None,
    driver_member_id: int | None = None,
):
    async with _connect() as db:
        await begin_write(db, "car", car_id)
        car = await _one(
            db,
            "SELECT name, plate, owner, state FROM cars WHERE id = ?",
            (car_id,),
        )
        if car is None:
            await db.rollback()
            return "missing", None
        if not is_bookable(None if car[3] is None else str(car[3])):
            await db.rollback()
            return "unavailable", None
        if ends_at <= starts_at:
            await db.rollback()
            return "invalid", None
        active = await _one(
            db,
            """SELECT id FROM car_bookings
               WHERE car_id = ?
                 AND cancelled_at IS NULL
                 AND returned_at IS NULL
                 AND starts_at < ?
                 AND ends_at > ?
               LIMIT 1""",
            (car_id, ends_at, starts_at),
        )
        if active:
            await db.rollback()
            return "busy", None
        cursor = await db.execute(
            """INSERT INTO car_bookings
               (car_id, user_id, ic_name, starts_at, ends_at,
                purpose, event_id, driver_member_id, warning)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')""",
            (
                car_id,
                user_id,
                ic_name.strip(),
                starts_at,
                ends_at,
                purpose.strip(),
                event_id,
                driver_member_id,
            ),
        )
        await db.commit()
        return "booked", (int(cursor.lastrowid), car[0], car[1], car[2])


async def get_booking(booking_id: int) -> CarBooking | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"SELECT {_BOOKING_COLUMNS} FROM car_bookings WHERE id = ?",
            (booking_id,),
        )
    return None if row is None else _to_booking(row)


async def get_active_user_car_bookings(user_id: int):
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT b.id, c.name, c.plate, b.ends_at
               FROM car_bookings b JOIN cars c ON c.id = b.car_id
               WHERE b.user_id = ? AND b.cancelled_at IS NULL
                 AND b.returned_at IS NULL AND b.ends_at > ?
               ORDER BY b.ends_at""",
            (user_id, _utc_now()),
        )


async def get_active_car_bookings():
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT b.id, c.name, c.plate, b.user_id, b.ic_name, b.ends_at
               FROM car_bookings b JOIN cars c ON c.id = b.car_id
               WHERE b.cancelled_at IS NULL AND b.returned_at IS NULL
                 AND b.ends_at > ?
               ORDER BY LOWER(c.name), LOWER(c.plate), b.ends_at""",
            (_utc_now(),),
        )


async def car_bookings_ended_since(started_at: str, ended_at: str) -> bool:
    async with _connect() as db:
        row = await _one(
            db,
            """SELECT 1 FROM car_bookings
               WHERE cancelled_at IS NULL
                 AND returned_at IS NULL
                 AND ends_at > ? AND ends_at <= ?
               LIMIT 1""",
            (started_at, ended_at),
        )
    return row is not None


async def cancel_car_booking(booking_id: int, user_id: int):
    async with _connect() as db:
        await begin_write(db, "car-booking", booking_id)
        booking = await _one(
            db,
            """SELECT b.id, c.name, c.plate, b.ends_at FROM car_bookings b
               JOIN cars c ON c.id = b.car_id
               WHERE b.id = ? AND b.user_id = ? AND b.cancelled_at IS NULL
                 AND b.returned_at IS NULL AND b.ends_at > ?""",
            (booking_id, user_id, _utc_now()),
        )
        if booking is None:
            await db.rollback()
            return None
        await db.execute(
            "UPDATE car_bookings SET cancelled_at = ?, cancelled_by = ? WHERE id = ?",
            (_utc_now(), user_id, booking_id),
        )
        await db.commit()
        return booking


async def force_cancel_car_booking(booking_id: int, cancelled_by: int):
    async with _connect() as db:
        await begin_write(db, "car-booking", booking_id)
        booking = await _one(
            db,
            """SELECT b.id, c.name, c.plate, b.user_id, b.ic_name, b.ends_at
               FROM car_bookings b JOIN cars c ON c.id = b.car_id
               WHERE b.id = ? AND b.cancelled_at IS NULL
                 AND b.returned_at IS NULL AND b.ends_at > ?""",
            (booking_id, _utc_now()),
        )
        if booking is None:
            await db.rollback()
            return None
        await db.execute(
            """UPDATE car_bookings
               SET cancelled_at = ?, cancelled_by = ?
               WHERE id = ? AND cancelled_at IS NULL""",
            (_utc_now(), cancelled_by, booking_id),
        )
        await db.commit()
        return booking


async def confirm_booking_return(booking_id: int, returned_by: int) -> CarBooking | None:
    async with _connect() as db:
        await begin_write(db, "car-booking", booking_id)
        row = await _one(
            db,
            f"SELECT {_BOOKING_COLUMNS} FROM car_bookings WHERE id = ?",
            (booking_id,),
        )
        if row is None:
            await db.rollback()
            return None
        current = _to_booking(row)
        if current.cancelled_at is not None or current.returned_at is not None:
            await db.rollback()
            return None
        stamp = _utc_now()
        await db.execute(
            """UPDATE car_bookings
               SET returned_at = ?, returned_by = ?
               WHERE id = ? AND returned_at IS NULL""",
            (stamp, returned_by, booking_id),
        )
        await db.commit()
    return await get_booking(booking_id)


async def mark_owner_departure(member_id: int, warning: str) -> tuple[int, int]:
    async with _connect() as db:
        await begin_write(db, "vehicle-owner", member_id)
        car_rows = await db.execute_fetchall(
            "SELECT id FROM cars WHERE owner_member_id = ? AND state = ?",
            (member_id, STATE_ACTIVE),
        )
        car_ids = [int(row[0]) for row in car_rows]
        if car_ids:
            await db.execute(
                """UPDATE cars SET state = ?, version = version + 1
                   WHERE owner_member_id = ? AND state = ?""",
                (STATE_NEEDS_RECON, member_id, STATE_ACTIVE),
            )
            placeholders = ",".join("?" * len(car_ids))
            cursor = await db.execute(
                f"""UPDATE car_bookings
                    SET warning = ?
                    WHERE car_id IN ({placeholders})
                      AND cancelled_at IS NULL
                      AND returned_at IS NULL
                      AND ends_at > ?""",
                (warning, *car_ids, _utc_now()),
            )
            warned = int(cursor.rowcount or 0)
        else:
            warned = 0
        await db.commit()
    return len(car_ids), warned


async def upsert_family_property(
    *,
    guild_id: int,
    family_id: int,
    kind: str,
    title: str,
    responsible_id: int,
    backup_id: int,
    due_at: str,
    last_paid_at: str | None = None,
) -> FamilyProperty:
    stamp = _utc_now()
    async with _connect() as db:
        await begin_write(db, "family-property", family_id)
        existing = await _one(
            db,
            f"""SELECT {_PROPERTY_COLUMNS} FROM family_properties
                WHERE guild_id = ? AND family_id = ? AND kind = ? AND title = ?""",
            (guild_id, family_id, kind, title),
        )
        if existing is None:
            cursor = await db.execute(
                """INSERT INTO family_properties (
                       guild_id, family_id, kind, title, responsible_id,
                       backup_id, due_at, last_paid_at, executed, version,
                       created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?)""",
                (
                    guild_id,
                    family_id,
                    kind,
                    title,
                    responsible_id,
                    backup_id,
                    due_at,
                    last_paid_at,
                    stamp,
                ),
            )
            property_id = int(cursor.lastrowid)
        else:
            property_id = int(existing[0])
            await db.execute(
                """UPDATE family_properties SET
                       responsible_id = ?, backup_id = ?, due_at = ?,
                       last_paid_at = COALESCE(?, last_paid_at),
                       version = version + 1
                   WHERE id = ?""",
                (responsible_id, backup_id, due_at, last_paid_at, property_id),
            )
        await db.commit()
    item = await get_family_property(property_id)
    assert item is not None
    return item


async def get_family_property(property_id: int) -> FamilyProperty | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"SELECT {_PROPERTY_COLUMNS} FROM family_properties WHERE id = ?",
            (property_id,),
        )
    return None if row is None else _to_property(row)


async def get_family_property_by_title(
    family_id: int, kind: str, title: str
) -> FamilyProperty | None:
    async with _connect() as db:
        row = await _one(
            db,
            f"""SELECT {_PROPERTY_COLUMNS} FROM family_properties
                WHERE family_id = ? AND kind = ? AND title = ?""",
            (family_id, kind, title),
        )
    return None if row is None else _to_property(row)


async def list_family_properties(family_id: int) -> list[FamilyProperty]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_PROPERTY_COLUMNS} FROM family_properties
                WHERE family_id = ?
                ORDER BY kind, title""",
            (family_id,),
        )
    return [_to_property(row) for row in rows]


async def mark_property_executed(
    property_id: int, *, paid_at: str | None = None
) -> FamilyProperty | None:
    stamp = paid_at or _utc_now()
    async with _connect() as db:
        await begin_write(db, "family-property-row", property_id)
        row = await _one(
            db,
            f"SELECT {_PROPERTY_COLUMNS} FROM family_properties WHERE id = ?",
            (property_id,),
        )
        if row is None:
            await db.rollback()
            return None
        await db.execute(
            """UPDATE family_properties
               SET executed = 1, last_paid_at = ?, version = version + 1
               WHERE id = ?""",
            (stamp, property_id),
        )
        await db.commit()
    return await get_family_property(property_id)
