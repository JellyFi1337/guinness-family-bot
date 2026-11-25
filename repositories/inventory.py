"""Storage locations, inventory balances, movements, kits and tax duties."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import (
    DATABASE_INTEGRITY_ERRORS,
    connection as _connect,
    utc_now,
)
from infrastructure.transactions import begin_write

LOCATION_KINDS = (
    "warehouse",
    "office_safe",
    "gun_cabinet",
    "pantry",
    "wardrobe",
    "garage",
    "mat_truck",
    "handout",
)
MOVEMENT_OPS = (
    "receive",
    "reserve",
    "release",
    "issue",
    "return",
    "consume",
    "adjust",
)
RESERVE_STATUSES = ("open", "released", "issued")
KIT_STATUSES = ("requested", "reserved", "issued", "returned", "cancelled")

_LOCATION_COLUMNS = (
    "id, guild_id, family_id, kind, name, capacity, capacity_unit, "
    "mat_level, sold, auctioned, version, created_at"
)
_ITEM_COLUMNS = (
    "id, guild_id, family_id, sku, title, unit, low_stock, created_at"
)
_BALANCE_COLUMNS = (
    "id, guild_id, family_id, location_id, item_id, on_hand, reserved"
)
_MOVEMENT_COLUMNS = (
    "id, guild_id, family_id, item_id, quantity, from_location_id, "
    "to_location_id, operation, basis_kind, basis_id, actor_id, reason, created_at"
)
_RESERVE_COLUMNS = (
    "id, guild_id, family_id, location_id, item_id, quantity, event_id, "
    "kit_id, status, actor_id, created_at"
)
_KIT_COLUMNS = (
    "id, guild_id, family_id, event_id, requester_id, status, reserved_by, "
    "issued_by, version, created_at"
)
_LINE_COLUMNS = (
    "id, kit_id, item_id, requested, reserved, issued, returned, consumed"
)
_RECON_COLUMNS = (
    "id, guild_id, family_id, location_id, note, actor_id, created_at"
)
_TAX_COLUMNS = (
    "id, guild_id, family_id, property_kind, due_at, responsible_id, "
    "backup_id, last_paid_at"
)


class StockConflict(Exception):
    def __init__(self, code: str, *, available: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.available = available


@dataclass(frozen=True, slots=True)
class StorageLocation:
    id: int
    guild_id: int
    family_id: int
    kind: str
    name: str
    capacity: int
    capacity_unit: str
    mat_level: int
    sold: bool
    auctioned: bool
    version: int
    created_at: str


@dataclass(frozen=True, slots=True)
class InventoryItem:
    id: int
    guild_id: int
    family_id: int
    sku: str
    title: str
    unit: str
    low_stock: int
    created_at: str


@dataclass(frozen=True, slots=True)
class InventoryBalance:
    id: int
    guild_id: int
    family_id: int
    location_id: int
    item_id: int
    on_hand: int
    reserved: int

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


@dataclass(frozen=True, slots=True)
class InventoryMovement:
    id: int
    guild_id: int
    family_id: int
    item_id: int
    quantity: int
    from_location_id: int | None
    to_location_id: int | None
    operation: str
    basis_kind: str
    basis_id: str
    actor_id: int
    reason: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class InventoryReservation:
    id: int
    guild_id: int
    family_id: int
    location_id: int
    item_id: int
    quantity: int
    event_id: int | None
    kit_id: int | None
    status: str
    actor_id: int
    created_at: str


@dataclass(frozen=True, slots=True)
class InventoryKit:
    id: int
    guild_id: int
    family_id: int
    event_id: int
    requester_id: int
    status: str
    reserved_by: int | None
    issued_by: int | None
    version: int
    created_at: str


@dataclass(frozen=True, slots=True)
class KitLine:
    id: int
    kit_id: int
    item_id: int
    requested: int
    reserved: int
    issued: int
    returned: int
    consumed: int


@dataclass(frozen=True, slots=True)
class InventoryReconciliation:
    id: int
    guild_id: int
    family_id: int
    location_id: int
    note: str
    actor_id: int
    created_at: str


@dataclass(frozen=True, slots=True)
class TaxDuty:
    id: int
    guild_id: int
    family_id: int
    property_kind: str
    due_at: str
    responsible_id: int
    backup_id: int
    last_paid_at: str | None


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


def _to_location(row: tuple[Any, ...]) -> StorageLocation:
    return StorageLocation(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        kind=str(row[3]),
        name=str(row[4]),
        capacity=int(row[5]),
        capacity_unit=str(row[6]),
        mat_level=int(row[7]),
        sold=bool(int(row[8])),
        auctioned=bool(int(row[9])),
        version=int(row[10]),
        created_at=str(row[11]),
    )


def _to_item(row: tuple[Any, ...]) -> InventoryItem:
    return InventoryItem(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        sku=str(row[3]),
        title=str(row[4]),
        unit=str(row[5]),
        low_stock=int(row[6]),
        created_at=str(row[7]),
    )


def _to_balance(row: tuple[Any, ...]) -> InventoryBalance:
    return InventoryBalance(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        location_id=int(row[3]),
        item_id=int(row[4]),
        on_hand=int(row[5]),
        reserved=int(row[6]),
    )


def _to_movement(row: tuple[Any, ...]) -> InventoryMovement:
    return InventoryMovement(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        item_id=int(row[3]),
        quantity=int(row[4]),
        from_location_id=None if row[5] is None else int(row[5]),
        to_location_id=None if row[6] is None else int(row[6]),
        operation=str(row[7]),
        basis_kind=str(row[8]),
        basis_id=str(row[9]),
        actor_id=int(row[10]),
        reason=None if row[11] is None else str(row[11]),
        created_at=str(row[12]),
    )


def _to_reservation(row: tuple[Any, ...]) -> InventoryReservation:
    return InventoryReservation(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        location_id=int(row[3]),
        item_id=int(row[4]),
        quantity=int(row[5]),
        event_id=None if row[6] is None else int(row[6]),
        kit_id=None if row[7] is None else int(row[7]),
        status=str(row[8]),
        actor_id=int(row[9]),
        created_at=str(row[10]),
    )


def _to_kit(row: tuple[Any, ...]) -> InventoryKit:
    return InventoryKit(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        event_id=int(row[3]),
        requester_id=int(row[4]),
        status=str(row[5]),
        reserved_by=None if row[6] is None else int(row[6]),
        issued_by=None if row[7] is None else int(row[7]),
        version=int(row[8]),
        created_at=str(row[9]),
    )


def _to_line(row: tuple[Any, ...]) -> KitLine:
    return KitLine(
        id=int(row[0]),
        kit_id=int(row[1]),
        item_id=int(row[2]),
        requested=int(row[3]),
        reserved=int(row[4]),
        issued=int(row[5]),
        returned=int(row[6]),
        consumed=int(row[7]),
    )


def _to_recon(row: tuple[Any, ...]) -> InventoryReconciliation:
    return InventoryReconciliation(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        location_id=int(row[3]),
        note=str(row[4]),
        actor_id=int(row[5]),
        created_at=str(row[6]),
    )


def _to_tax(row: tuple[Any, ...]) -> TaxDuty:
    return TaxDuty(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        property_kind=str(row[3]),
        due_at=str(row[4]),
        responsible_id=int(row[5]),
        backup_id=int(row[6]),
        last_paid_at=None if row[7] is None else str(row[7]),
    )


async def get_location(location_id: int) -> StorageLocation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_LOCATION_COLUMNS} FROM storage_locations WHERE id = ?",
            (location_id,),
        )
    return _to_location(rows[0]) if rows else None


async def get_location_by_kind(
    family_id: int, kind: str
) -> StorageLocation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_LOCATION_COLUMNS} FROM storage_locations
                WHERE family_id = ? AND kind = ?""",
            (family_id, kind),
        )
    return _to_location(rows[0]) if rows else None


async def list_locations(family_id: int) -> list[StorageLocation]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_LOCATION_COLUMNS} FROM storage_locations
                WHERE family_id = ? ORDER BY id""",
            (family_id,),
        )
    return [_to_location(row) for row in rows]


async def ensure_locations(
    *,
    guild_id: int,
    family_id: int,
    specs: tuple[tuple[str, str, int, str, int], ...],
) -> list[StorageLocation]:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        for kind, name, capacity, unit, mat_level in specs:
            await db.execute(
                """INSERT OR IGNORE INTO storage_locations
                   (guild_id, family_id, kind, name, capacity, capacity_unit,
                    mat_level, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    kind,
                    name,
                    capacity,
                    unit,
                    mat_level,
                    stamp,
                ),
            )
        await db.commit()
    return await list_locations(family_id)


async def update_location(
    location_id: int,
    *,
    expected_version: int,
    capacity: int | None = None,
    mat_level: int | None = None,
    sold: bool | None = None,
    auctioned: bool | None = None,
) -> StorageLocation | None:
    async with _connect() as db:
        await begin_write(db, "inventory-location", location_id)
        rows = await db.execute_fetchall(
            f"SELECT {_LOCATION_COLUMNS} FROM storage_locations WHERE id = ?",
            (location_id,),
        )
        if not rows:
            await db.commit()
            return None
        current = _to_location(rows[0])
        if current.version != expected_version:
            await db.commit()
            return None
        fields = ["version = version + 1"]
        params: list[Any] = []
        if capacity is not None:
            fields.append("capacity = ?")
            params.append(_require_non_negative_int(capacity, label="capacity"))
        if mat_level is not None:
            fields.append("mat_level = ?")
            params.append(mat_level)
        if sold is not None:
            fields.append("sold = ?")
            params.append(1 if sold else 0)
        if auctioned is not None:
            fields.append("auctioned = ?")
            params.append(1 if auctioned else 0)
        params.extend((location_id, expected_version))
        cursor = await db.execute(
            f"""UPDATE storage_locations SET {", ".join(fields)}
                WHERE id = ? AND version = ?""",
            params,
        )
        if cursor.rowcount != 1:
            await db.commit()
            return None
        await db.commit()
    return await get_location(location_id)


async def get_item(item_id: int) -> InventoryItem | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_ITEM_COLUMNS} FROM inventory_items WHERE id = ?",
            (item_id,),
        )
    return _to_item(rows[0]) if rows else None


async def get_item_by_sku(family_id: int, sku: str) -> InventoryItem | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_ITEM_COLUMNS} FROM inventory_items
                WHERE family_id = ? AND sku = ?""",
            (family_id, sku),
        )
    return _to_item(rows[0]) if rows else None


async def list_items(family_id: int) -> list[InventoryItem]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_ITEM_COLUMNS} FROM inventory_items
                WHERE family_id = ? ORDER BY sku""",
            (family_id,),
        )
    return [_to_item(row) for row in rows]


async def insert_item(
    *,
    guild_id: int,
    family_id: int,
    sku: str,
    title: str,
    unit: str,
    low_stock: int,
) -> InventoryItem:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        try:
            cursor = await db.execute(
                """INSERT INTO inventory_items
                   (guild_id, family_id, sku, title, unit, low_stock, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    sku,
                    title,
                    unit,
                    _require_non_negative_int(low_stock, label="low_stock"),
                    stamp,
                ),
            )
            item_id = _inserted_id(cursor)
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            loaded = await get_item_by_sku(family_id, sku)
            if loaded is None:
                raise
            return loaded
    found = await get_item(item_id)
    if found is None:
        raise RuntimeError("inventory item insert did not persist")
    return found


async def get_balance(
    location_id: int, item_id: int
) -> InventoryBalance | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_BALANCE_COLUMNS} FROM inventory_balances
                WHERE location_id = ? AND item_id = ?""",
            (location_id, item_id),
        )
    return _to_balance(rows[0]) if rows else None


async def list_balances(location_id: int) -> list[InventoryBalance]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_BALANCE_COLUMNS} FROM inventory_balances
                WHERE location_id = ? ORDER BY item_id""",
            (location_id,),
        )
    return [_to_balance(row) for row in rows]


async def occupied_slots(location_id: int) -> int:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT COUNT(*) FROM inventory_balances
               WHERE location_id = ? AND on_hand > 0""",
            (location_id,),
        )
    return int(rows[0][0]) if rows else 0


async def occupied_quantity(location_id: int) -> int:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT COALESCE(SUM(on_hand), 0) FROM inventory_balances
               WHERE location_id = ?""",
            (location_id,),
        )
    return int(rows[0][0]) if rows else 0


async def get_movement(movement_id: int) -> InventoryMovement | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_MOVEMENT_COLUMNS} FROM inventory_movements WHERE id = ?",
            (movement_id,),
        )
    return _to_movement(rows[0]) if rows else None


async def find_movement(
    *,
    family_id: int,
    item_id: int,
    operation: str,
    basis_kind: str,
    basis_id: str,
) -> InventoryMovement | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_MOVEMENT_COLUMNS} FROM inventory_movements
                WHERE family_id = ? AND item_id = ? AND operation = ?
                  AND basis_kind = ? AND basis_id = ?""",
            (family_id, item_id, operation, basis_kind, basis_id),
        )
    return _to_movement(rows[0]) if rows else None


async def get_reservation(reservation_id: int) -> InventoryReservation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_RESERVE_COLUMNS} FROM inventory_reservations WHERE id = ?",
            (reservation_id,),
        )
    return _to_reservation(rows[0]) if rows else None


async def list_open_reservations_for_event(
    family_id: int, event_id: int
) -> list[InventoryReservation]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_RESERVE_COLUMNS} FROM inventory_reservations
                WHERE family_id = ? AND event_id = ? AND status = 'open'
                ORDER BY id""",
            (family_id, event_id),
        )
    return [_to_reservation(row) for row in rows]


async def list_open_reservations_for_kit(
    kit_id: int,
) -> list[InventoryReservation]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_RESERVE_COLUMNS} FROM inventory_reservations
                WHERE kit_id = ? AND status = 'open' ORDER BY id""",
            (kit_id,),
        )
    return [_to_reservation(row) for row in rows]


async def get_kit(kit_id: int) -> InventoryKit | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_KIT_COLUMNS} FROM inventory_kits WHERE id = ?",
            (kit_id,),
        )
    return _to_kit(rows[0]) if rows else None


async def list_kits_for_event(
    family_id: int, event_id: int
) -> list[InventoryKit]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_KIT_COLUMNS} FROM inventory_kits
                WHERE family_id = ? AND event_id = ? ORDER BY id""",
            (family_id, event_id),
        )
    return [_to_kit(row) for row in rows]


async def list_kits_for_member(
    family_id: int, requester_id: int
) -> list[InventoryKit]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_KIT_COLUMNS} FROM inventory_kits
                WHERE family_id = ? AND requester_id = ?
                  AND status IN ('requested', 'reserved', 'issued')
                ORDER BY id""",
            (family_id, requester_id),
        )
    return [_to_kit(row) for row in rows]


async def list_open_issued_kits(family_id: int) -> list[InventoryKit]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_KIT_COLUMNS} FROM inventory_kits
                WHERE family_id = ? AND status = 'issued' ORDER BY id""",
            (family_id,),
        )
    return [_to_kit(row) for row in rows]


async def list_kit_lines(kit_id: int) -> list[KitLine]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_LINE_COLUMNS} FROM inventory_kit_lines
                WHERE kit_id = ? ORDER BY item_id""",
            (kit_id,),
        )
    return [_to_line(row) for row in rows]


async def insert_kit(
    *,
    guild_id: int,
    family_id: int,
    event_id: int,
    requester_id: int,
) -> InventoryKit:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        cursor = await db.execute(
            """INSERT INTO inventory_kits
               (guild_id, family_id, event_id, requester_id, status, created_at)
               VALUES (?, ?, ?, ?, 'requested', ?)""",
            (guild_id, family_id, event_id, requester_id, stamp),
        )
        kit_id = _inserted_id(cursor)
        await db.commit()
    found = await get_kit(kit_id)
    if found is None:
        raise RuntimeError("inventory kit insert did not persist")
    return found


async def add_kit_line(
    *,
    kit_id: int,
    item_id: int,
    requested: int,
) -> KitLine:
    qty = _require_positive_int(requested, label="requested")
    async with _connect() as db:
        await begin_write(db, "inventory-kit", kit_id)
        rows = await db.execute_fetchall(
            f"SELECT {_LINE_COLUMNS} FROM inventory_kit_lines WHERE kit_id = ? AND item_id = ?",
            (kit_id, item_id),
        )
        if rows:
            current = _to_line(rows[0])
            cursor = await db.execute(
                """UPDATE inventory_kit_lines SET requested = requested + ?
                   WHERE id = ?""",
                (qty, current.id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise RuntimeError("kit line update failed")
        else:
            await db.execute(
                """INSERT INTO inventory_kit_lines
                   (kit_id, item_id, requested) VALUES (?, ?, ?)""",
                (kit_id, item_id, qty),
            )
        await db.commit()
    lines = await list_kit_lines(kit_id)
    for line in lines:
        if line.item_id == item_id:
            return line
    raise RuntimeError("kit line insert did not persist")


async def latest_reconciliation(
    location_id: int,
) -> InventoryReconciliation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_RECON_COLUMNS} FROM inventory_reconciliations
                WHERE location_id = ? ORDER BY id DESC LIMIT 1""",
            (location_id,),
        )
    return _to_recon(rows[0]) if rows else None


async def insert_reconciliation(
    *,
    guild_id: int,
    family_id: int,
    location_id: int,
    note: str,
    actor_id: int,
) -> InventoryReconciliation:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        cursor = await db.execute(
            """INSERT INTO inventory_reconciliations
               (guild_id, family_id, location_id, note, actor_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, family_id, location_id, note, actor_id, stamp),
        )
        recon_id = _inserted_id(cursor)
        await db.commit()
    found = await _get_recon(recon_id)
    if found is None:
        raise RuntimeError("inventory reconciliation insert did not persist")
    return found


async def _get_recon(recon_id: int) -> InventoryReconciliation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_RECON_COLUMNS} FROM inventory_reconciliations WHERE id = ?",
            (recon_id,),
        )
    return _to_recon(rows[0]) if rows else None


async def get_tax_duty(family_id: int, property_kind: str = "warehouse") -> TaxDuty | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_TAX_COLUMNS} FROM inventory_tax_duties
                WHERE family_id = ? AND property_kind = ?""",
            (family_id, property_kind),
        )
    return _to_tax(rows[0]) if rows else None


async def upsert_tax_duty(
    *,
    guild_id: int,
    family_id: int,
    property_kind: str,
    due_at: str,
    responsible_id: int,
    backup_id: int,
) -> TaxDuty:
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        current = await db.execute_fetchall(
            f"""SELECT {_TAX_COLUMNS} FROM inventory_tax_duties
                WHERE family_id = ? AND property_kind = ?""",
            (family_id, property_kind),
        )
        if current:
            await db.execute(
                """UPDATE inventory_tax_duties
                   SET due_at = ?, responsible_id = ?, backup_id = ?
                   WHERE id = ?""",
                (due_at, responsible_id, backup_id, int(current[0][0])),
            )
            duty_id = int(current[0][0])
        else:
            cursor = await db.execute(
                """INSERT INTO inventory_tax_duties
                   (guild_id, family_id, property_kind, due_at,
                    responsible_id, backup_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    property_kind,
                    due_at,
                    responsible_id,
                    backup_id,
                ),
            )
            duty_id = _inserted_id(cursor)
        await db.commit()
    found = await _get_tax(duty_id)
    if found is None:
        raise RuntimeError("tax duty upsert did not persist")
    return found


async def _get_tax(duty_id: int) -> TaxDuty | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_TAX_COLUMNS} FROM inventory_tax_duties WHERE id = ?",
            (duty_id,),
        )
    return _to_tax(rows[0]) if rows else None


async def list_tax_duties(family_id: int) -> list[TaxDuty]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_TAX_COLUMNS} FROM inventory_tax_duties
                WHERE family_id = ? ORDER BY id""",
            (family_id,),
        )
    return [_to_tax(row) for row in rows]


async def _lock_balance(
    db: Any, *, location_id: int, item_id: int
) -> InventoryBalance | None:
    rows = await db.execute_fetchall(
        f"""SELECT {_BALANCE_COLUMNS} FROM inventory_balances
            WHERE location_id = ? AND item_id = ?""",
        (location_id, item_id),
    )
    return _to_balance(rows[0]) if rows else None


async def _ensure_balance_row(
    db: Any,
    *,
    guild_id: int,
    family_id: int,
    location_id: int,
    item_id: int,
) -> InventoryBalance:
    current = await _lock_balance(db, location_id=location_id, item_id=item_id)
    if current is not None:
        return current
    await db.execute(
        """INSERT INTO inventory_balances
           (guild_id, family_id, location_id, item_id, on_hand, reserved)
           VALUES (?, ?, ?, ?, 0, 0)""",
        (guild_id, family_id, location_id, item_id),
    )
    created = await _lock_balance(db, location_id=location_id, item_id=item_id)
    if created is None:
        raise RuntimeError("inventory balance insert did not persist")
    return created


async def _insert_movement(
    db: Any,
    *,
    guild_id: int,
    family_id: int,
    item_id: int,
    quantity: int,
    from_location_id: int | None,
    to_location_id: int | None,
    operation: str,
    basis_kind: str,
    basis_id: str,
    actor_id: int,
    reason: str | None,
) -> int:
    stamp = utc_now()
    cursor = await db.execute(
        """INSERT INTO inventory_movements
           (guild_id, family_id, item_id, quantity, from_location_id,
            to_location_id, operation, basis_kind, basis_id, actor_id,
            reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            guild_id,
            family_id,
            item_id,
            quantity,
            from_location_id,
            to_location_id,
            operation,
            basis_kind,
            basis_id,
            actor_id,
            reason,
            stamp,
        ),
    )
    return _inserted_id(cursor)


async def apply_receive(
    *,
    guild_id: int,
    family_id: int,
    location: StorageLocation,
    item_id: int,
    quantity: int,
    actor_id: int,
    basis_kind: str,
    basis_id: str,
    occupancy_kind: str,
) -> InventoryMovement:
    qty = _require_positive_int(quantity, label="quantity")
    existing = await find_movement(
        family_id=family_id,
        item_id=item_id,
        operation="receive",
        basis_kind=basis_kind,
        basis_id=basis_id,
    )
    if existing is not None:
        return existing
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        if location.sold:
            await db.rollback()
            raise StockConflict("sold")
        current_balance = await _lock_balance(
            db, location_id=location.id, item_id=item_id
        )
        slot_count = await db.execute_fetchall(
            """SELECT COUNT(*) FROM inventory_balances
               WHERE location_id = ? AND on_hand > 0""",
            (location.id,),
        )
        qty_sum = await db.execute_fetchall(
            """SELECT COALESCE(SUM(on_hand), 0) FROM inventory_balances
               WHERE location_id = ?""",
            (location.id,),
        )
        occupied_slots = int(slot_count[0][0]) if slot_count else 0
        occupied_qty = int(qty_sum[0][0]) if qty_sum else 0
        new_sku = current_balance is None or current_balance.on_hand <= 0
        if occupancy_kind == "slots" and new_sku and location.capacity > 0:
            if occupied_slots >= location.capacity:
                await db.rollback()
                raise StockConflict("occupancy")
        if occupancy_kind == "quantity" and location.capacity > 0:
            if occupied_qty + qty > location.capacity:
                await db.rollback()
                raise StockConflict("occupancy")
        await _ensure_balance_row(
            db,
            guild_id=guild_id,
            family_id=family_id,
            location_id=location.id,
            item_id=item_id,
        )
        await db.execute(
            """UPDATE inventory_balances SET on_hand = on_hand + ?
               WHERE location_id = ? AND item_id = ?""",
            (qty, location.id, item_id),
        )
        try:
            movement_id = await _insert_movement(
                db,
                guild_id=guild_id,
                family_id=family_id,
                item_id=item_id,
                quantity=qty,
                from_location_id=None,
                to_location_id=location.id,
                operation="receive",
                basis_kind=basis_kind,
                basis_id=basis_id,
                actor_id=actor_id,
                reason=None,
            )
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            loaded = await find_movement(
                family_id=family_id,
                item_id=item_id,
                operation="receive",
                basis_kind=basis_kind,
                basis_id=basis_id,
            )
            if loaded is None:
                raise
            return loaded
    found = await get_movement(movement_id)
    if found is None:
        raise RuntimeError("receive movement did not persist")
    return found


async def apply_reserve(
    *,
    guild_id: int,
    family_id: int,
    location: StorageLocation,
    item_id: int,
    quantity: int,
    actor_id: int,
    event_id: int | None,
    kit_id: int | None,
    basis_kind: str,
    basis_id: str,
) -> InventoryReservation:
    qty = _require_positive_int(quantity, label="quantity")
    existing_move = await find_movement(
        family_id=family_id,
        item_id=item_id,
        operation="reserve",
        basis_kind=basis_kind,
        basis_id=basis_id,
    )
    if existing_move is not None:
        rows: list[InventoryReservation] = []
        if kit_id is not None:
            rows = await list_open_reservations_for_kit(kit_id)
        elif event_id is not None:
            rows = await list_open_reservations_for_event(family_id, event_id)
        for item in rows:
            if item.item_id == item_id and item.location_id == location.id:
                return item
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        if location.sold:
            await db.rollback()
            raise StockConflict("sold")
        balance = await _lock_balance(
            db, location_id=location.id, item_id=item_id
        )
        available = 0 if balance is None else balance.available
        if qty > available:
            await db.rollback()
            raise StockConflict("insufficient", available=available)
        if balance is None:
            await db.rollback()
            raise StockConflict("insufficient", available=0)
        await db.execute(
            """UPDATE inventory_balances SET reserved = reserved + ?
               WHERE location_id = ? AND item_id = ?""",
            (qty, location.id, item_id),
        )
        stamp = utc_now()
        cursor = await db.execute(
            """INSERT INTO inventory_reservations
               (guild_id, family_id, location_id, item_id, quantity,
                event_id, kit_id, status, actor_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (
                guild_id,
                family_id,
                location.id,
                item_id,
                qty,
                event_id,
                kit_id,
                actor_id,
                stamp,
            ),
        )
        reservation_id = _inserted_id(cursor)
        try:
            await _insert_movement(
                db,
                guild_id=guild_id,
                family_id=family_id,
                item_id=item_id,
                quantity=qty,
                from_location_id=location.id,
                to_location_id=location.id,
                operation="reserve",
                basis_kind=basis_kind,
                basis_id=basis_id,
                actor_id=actor_id,
                reason=None,
            )
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            raise StockConflict("duplicate") from None
    found = await get_reservation(reservation_id)
    if found is None:
        raise RuntimeError("reservation insert did not persist")
    return found


async def apply_release(
    reservation: InventoryReservation,
    *,
    actor_id: int,
    basis_kind: str,
    basis_id: str,
) -> InventoryReservation:
    existing = await find_movement(
        family_id=reservation.family_id,
        item_id=reservation.item_id,
        operation="release",
        basis_kind=basis_kind,
        basis_id=basis_id,
    )
    if existing is not None:
        loaded = await get_reservation(reservation.id)
        if loaded is None:
            raise RuntimeError("released reservation missing")
        return loaded
    async with _connect() as db:
        await begin_write(db, "inventory", reservation.family_id)
        rows = await db.execute_fetchall(
            f"SELECT {_RESERVE_COLUMNS} FROM inventory_reservations WHERE id = ?",
            (reservation.id,),
        )
        if not rows:
            await db.rollback()
            raise StockConflict("missing")
        current = _to_reservation(rows[0])
        if current.status != "open":
            await db.commit()
            return current
        await db.execute(
            """UPDATE inventory_balances
               SET reserved = reserved - ?
               WHERE location_id = ? AND item_id = ? AND reserved >= ?""",
            (
                current.quantity,
                current.location_id,
                current.item_id,
                current.quantity,
            ),
        )
        await db.execute(
            """UPDATE inventory_reservations SET status = 'released' WHERE id = ?""",
            (current.id,),
        )
        await _insert_movement(
            db,
            guild_id=current.guild_id,
            family_id=current.family_id,
            item_id=current.item_id,
            quantity=current.quantity,
            from_location_id=current.location_id,
            to_location_id=current.location_id,
            operation="release",
            basis_kind=basis_kind,
            basis_id=basis_id,
            actor_id=actor_id,
            reason=None,
        )
        await db.commit()
    found = await get_reservation(reservation.id)
    if found is None:
        raise RuntimeError("reservation release did not persist")
    return found


async def apply_issue(
    reservation: InventoryReservation,
    *,
    handout: StorageLocation,
    actor_id: int,
    basis_kind: str,
    basis_id: str,
) -> InventoryReservation:
    existing = await find_movement(
        family_id=reservation.family_id,
        item_id=reservation.item_id,
        operation="issue",
        basis_kind=basis_kind,
        basis_id=basis_id,
    )
    if existing is not None:
        loaded = await get_reservation(reservation.id)
        if loaded is None:
            raise RuntimeError("issued reservation missing")
        return loaded
    async with _connect() as db:
        await begin_write(db, "inventory", reservation.family_id)
        rows = await db.execute_fetchall(
            f"SELECT {_RESERVE_COLUMNS} FROM inventory_reservations WHERE id = ?",
            (reservation.id,),
        )
        if not rows:
            await db.rollback()
            raise StockConflict("missing")
        current = _to_reservation(rows[0])
        if current.status != "open":
            await db.commit()
            return current
        cursor = await db.execute(
            """UPDATE inventory_balances
               SET on_hand = on_hand - ?, reserved = reserved - ?
               WHERE location_id = ? AND item_id = ?
                 AND on_hand >= ? AND reserved >= ?""",
            (
                current.quantity,
                current.quantity,
                current.location_id,
                current.item_id,
                current.quantity,
                current.quantity,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise StockConflict("insufficient")
        await _ensure_balance_row(
            db,
            guild_id=current.guild_id,
            family_id=current.family_id,
            location_id=handout.id,
            item_id=current.item_id,
        )
        await db.execute(
            """UPDATE inventory_balances SET on_hand = on_hand + ?
               WHERE location_id = ? AND item_id = ?""",
            (current.quantity, handout.id, current.item_id),
        )
        await db.execute(
            """UPDATE inventory_reservations SET status = 'issued' WHERE id = ?""",
            (current.id,),
        )
        await _insert_movement(
            db,
            guild_id=current.guild_id,
            family_id=current.family_id,
            item_id=current.item_id,
            quantity=current.quantity,
            from_location_id=current.location_id,
            to_location_id=handout.id,
            operation="issue",
            basis_kind=basis_kind,
            basis_id=basis_id,
            actor_id=actor_id,
            reason=None,
        )
        await db.commit()
    found = await get_reservation(reservation.id)
    if found is None:
        raise RuntimeError("issue did not persist")
    return found


async def apply_return(
    *,
    guild_id: int,
    family_id: int,
    handout: StorageLocation,
    warehouse: StorageLocation,
    item_id: int,
    quantity: int,
    actor_id: int,
    basis_kind: str,
    basis_id: str,
) -> InventoryMovement:
    qty = _require_positive_int(quantity, label="quantity")
    existing = await find_movement(
        family_id=family_id,
        item_id=item_id,
        operation="return",
        basis_kind=basis_kind,
        basis_id=basis_id,
    )
    if existing is not None:
        return existing
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        source = await _lock_balance(db, location_id=handout.id, item_id=item_id)
        if source is None or source.on_hand < qty:
            available = 0 if source is None else source.on_hand
            await db.rollback()
            raise StockConflict("insufficient", available=available)
        await db.execute(
            """UPDATE inventory_balances SET on_hand = on_hand - ?
               WHERE location_id = ? AND item_id = ?""",
            (qty, handout.id, item_id),
        )
        await _ensure_balance_row(
            db,
            guild_id=guild_id,
            family_id=family_id,
            location_id=warehouse.id,
            item_id=item_id,
        )
        await db.execute(
            """UPDATE inventory_balances SET on_hand = on_hand + ?
               WHERE location_id = ? AND item_id = ?""",
            (qty, warehouse.id, item_id),
        )
        movement_id = await _insert_movement(
            db,
            guild_id=guild_id,
            family_id=family_id,
            item_id=item_id,
            quantity=qty,
            from_location_id=handout.id,
            to_location_id=warehouse.id,
            operation="return",
            basis_kind=basis_kind,
            basis_id=basis_id,
            actor_id=actor_id,
            reason=None,
        )
        await db.commit()
    found = await get_movement(movement_id)
    if found is None:
        raise RuntimeError("return movement did not persist")
    return found


async def apply_consume(
    *,
    guild_id: int,
    family_id: int,
    handout: StorageLocation,
    item_id: int,
    quantity: int,
    actor_id: int,
    basis_kind: str,
    basis_id: str,
) -> InventoryMovement:
    qty = _require_positive_int(quantity, label="quantity")
    existing = await find_movement(
        family_id=family_id,
        item_id=item_id,
        operation="consume",
        basis_kind=basis_kind,
        basis_id=basis_id,
    )
    if existing is not None:
        return existing
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        source = await _lock_balance(db, location_id=handout.id, item_id=item_id)
        if source is None or source.on_hand < qty:
            available = 0 if source is None else source.on_hand
            await db.rollback()
            raise StockConflict("insufficient", available=available)
        await db.execute(
            """UPDATE inventory_balances SET on_hand = on_hand - ?
               WHERE location_id = ? AND item_id = ?""",
            (qty, handout.id, item_id),
        )
        movement_id = await _insert_movement(
            db,
            guild_id=guild_id,
            family_id=family_id,
            item_id=item_id,
            quantity=qty,
            from_location_id=handout.id,
            to_location_id=None,
            operation="consume",
            basis_kind=basis_kind,
            basis_id=basis_id,
            actor_id=actor_id,
            reason=None,
        )
        await db.commit()
    found = await get_movement(movement_id)
    if found is None:
        raise RuntimeError("consume movement did not persist")
    return found


async def apply_adjust(
    *,
    guild_id: int,
    family_id: int,
    location: StorageLocation,
    item_id: int,
    quantity: int,
    add: bool,
    actor_id: int,
    reason: str,
    basis_id: str,
    occupancy_kind: str,
) -> InventoryMovement:
    qty = _require_positive_int(quantity, label="quantity")
    existing = await find_movement(
        family_id=family_id,
        item_id=item_id,
        operation="adjust",
        basis_kind="adjust",
        basis_id=basis_id,
    )
    if existing is not None:
        return existing
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        await _ensure_balance_row(
            db,
            guild_id=guild_id,
            family_id=family_id,
            location_id=location.id,
            item_id=item_id,
        )
        balance = await _lock_balance(db, location_id=location.id, item_id=item_id)
        if balance is None:
            await db.rollback()
            raise StockConflict("missing")
        if add:
            slot_count = await db.execute_fetchall(
                """SELECT COUNT(*) FROM inventory_balances
                   WHERE location_id = ? AND on_hand > 0""",
                (location.id,),
            )
            qty_sum = await db.execute_fetchall(
                """SELECT COALESCE(SUM(on_hand), 0) FROM inventory_balances
                   WHERE location_id = ?""",
                (location.id,),
            )
            occupied_slots = int(slot_count[0][0]) if slot_count else 0
            occupied_qty = int(qty_sum[0][0]) if qty_sum else 0
            new_sku = balance.on_hand <= 0
            if occupancy_kind == "slots" and new_sku and location.capacity > 0:
                if occupied_slots >= location.capacity:
                    await db.rollback()
                    raise StockConflict("occupancy")
            if occupancy_kind == "quantity" and location.capacity > 0:
                if occupied_qty + qty > location.capacity:
                    await db.rollback()
                    raise StockConflict("occupancy")
            await db.execute(
                """UPDATE inventory_balances SET on_hand = on_hand + ?
                   WHERE location_id = ? AND item_id = ?""",
                (qty, location.id, item_id),
            )
        else:
            if balance.available < qty:
                await db.rollback()
                raise StockConflict("insufficient", available=balance.available)
            await db.execute(
                """UPDATE inventory_balances SET on_hand = on_hand - ?
                   WHERE location_id = ? AND item_id = ?""",
                (qty, location.id, item_id),
            )
        movement_id = await _insert_movement(
            db,
            guild_id=guild_id,
            family_id=family_id,
            item_id=item_id,
            quantity=qty,
            from_location_id=None if add else location.id,
            to_location_id=location.id if add else None,
            operation="adjust",
            basis_kind="adjust",
            basis_id=basis_id,
            actor_id=actor_id,
            reason=reason,
        )
        await db.commit()
    found = await get_movement(movement_id)
    if found is None:
        raise RuntimeError("adjust movement did not persist")
    return found


async def clear_location_stock(
    *,
    guild_id: int,
    family_id: int,
    location: StorageLocation,
    actor_id: int,
    basis_id: str,
    reason: str,
) -> list[InventoryMovement]:
    balances = await list_balances(location.id)
    created: list[InventoryMovement] = []
    async with _connect() as db:
        await begin_write(db, "inventory", family_id)
        for balance in balances:
            if balance.on_hand <= 0 and balance.reserved <= 0:
                continue
            await db.execute(
                """UPDATE inventory_balances
                   SET on_hand = 0, reserved = 0
                   WHERE id = ?""",
                (balance.id,),
            )
            if balance.reserved > 0:
                await db.execute(
                    """UPDATE inventory_reservations SET status = 'released'
                       WHERE location_id = ? AND item_id = ? AND status = 'open'""",
                    (location.id, balance.item_id),
                )
            if balance.on_hand > 0:
                movement_id = await _insert_movement(
                    db,
                    guild_id=guild_id,
                    family_id=family_id,
                    item_id=balance.item_id,
                    quantity=balance.on_hand,
                    from_location_id=location.id,
                    to_location_id=None,
                    operation="consume",
                    basis_kind="sell",
                    basis_id=f"{basis_id}:{balance.item_id}",
                    actor_id=actor_id,
                    reason=reason,
                )
                created.append(InventoryMovement(
                    id=movement_id,
                    guild_id=guild_id,
                    family_id=family_id,
                    item_id=balance.item_id,
                    quantity=balance.on_hand,
                    from_location_id=location.id,
                    to_location_id=None,
                    operation="consume",
                    basis_kind="sell",
                    basis_id=f"{basis_id}:{balance.item_id}",
                    actor_id=actor_id,
                    reason=reason,
                    created_at=utc_now(),
                ))
        await db.commit()
    return created


async def update_kit(
    kit_id: int,
    *,
    expected_version: int,
    status: str | None = None,
    reserved_by: int | None = None,
    issued_by: int | None = None,
) -> InventoryKit | None:
    if status is not None and status not in KIT_STATUSES:
        raise ValueError("unknown kit status")
    async with _connect() as db:
        await begin_write(db, "inventory-kit", kit_id)
        rows = await db.execute_fetchall(
            f"SELECT {_KIT_COLUMNS} FROM inventory_kits WHERE id = ?",
            (kit_id,),
        )
        if not rows:
            await db.commit()
            return None
        current = _to_kit(rows[0])
        if current.version != expected_version:
            await db.commit()
            return None
        fields = ["version = version + 1"]
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if reserved_by is not None:
            fields.append("reserved_by = ?")
            params.append(reserved_by)
        if issued_by is not None:
            fields.append("issued_by = ?")
            params.append(issued_by)
        params.extend((kit_id, expected_version))
        cursor = await db.execute(
            f"""UPDATE inventory_kits SET {", ".join(fields)}
                WHERE id = ? AND version = ?""",
            params,
        )
        if cursor.rowcount != 1:
            await db.commit()
            return None
        await db.commit()
    return await get_kit(kit_id)


async def update_kit_line_counts(
    line_id: int,
    *,
    reserved: int | None = None,
    issued: int | None = None,
    returned: int | None = None,
    consumed: int | None = None,
) -> KitLine | None:
    async with _connect() as db:
        await begin_write(db, "inventory-kit-line", line_id)
        rows = await db.execute_fetchall(
            f"SELECT {_LINE_COLUMNS} FROM inventory_kit_lines WHERE id = ?",
            (line_id,),
        )
        if not rows:
            await db.commit()
            return None
        fields: list[str] = []
        params: list[Any] = []
        if reserved is not None:
            fields.append("reserved = ?")
            params.append(_require_non_negative_int(reserved, label="reserved"))
        if issued is not None:
            fields.append("issued = ?")
            params.append(_require_non_negative_int(issued, label="issued"))
        if returned is not None:
            fields.append("returned = ?")
            params.append(_require_non_negative_int(returned, label="returned"))
        if consumed is not None:
            fields.append("consumed = ?")
            params.append(_require_non_negative_int(consumed, label="consumed"))
        if not fields:
            await db.commit()
            return _to_line(rows[0])
        params.append(line_id)
        await db.execute(
            f"UPDATE inventory_kit_lines SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await db.commit()
    rows = await _line_by_id(line_id)
    return rows


async def _line_by_id(line_id: int) -> KitLine | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_LINE_COLUMNS} FROM inventory_kit_lines WHERE id = ?",
            (line_id,),
        )
    return _to_line(rows[0]) if rows else None
