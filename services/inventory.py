"""Family warehouse, kit issue and irreversible storage warnings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from core.permissions import AccessDecision, Permission, evaluate_access
from core.settings import get_registry
from repositories.events import get_event
from repositories.inventory import (
    InventoryItem,
    InventoryKit,
    InventoryMovement,
    InventoryReservation,
    KitLine,
    StockConflict,
    StorageLocation,
    TaxDuty,
    add_kit_line,
    apply_adjust,
    apply_consume,
    apply_issue,
    apply_receive,
    apply_release,
    apply_reserve,
    apply_return,
    clear_location_stock,
    ensure_locations,
    get_item_by_sku,
    get_kit,
    get_location_by_kind,
    insert_item,
    insert_kit,
    insert_reconciliation,
    latest_reconciliation,
    list_balances,
    list_items,
    list_kit_lines,
    list_kits_for_event,
    list_kits_for_member,
    list_locations,
    list_open_issued_kits,
    list_open_reservations_for_event,
    list_open_reservations_for_kit,
    list_tax_duties,
    occupied_quantity,
    occupied_slots,
    update_kit,
    update_kit_line_counts,
    update_location,
    upsert_tax_duty,
)
from repositories.members import get_current_member
from repositories.operations import enqueue_operation
from services.audit import record_decision
from services.membership import ensure_family
from ui.command_registry import cmd
from ui.design import Accent
from ui.message_specs import MessageSpec, MessageTone
from ui.templates import notice

KIND_LABELS = {
    "warehouse": "Склад организации",
    "office_safe": "Сейф офиса",
    "gun_cabinet": "Оружейный шкаф",
    "pantry": "Кладовая",
    "wardrobe": "Гардероб",
    "garage": "Гараж",
    "mat_truck": "Матовозка",
    "handout": "Выдача на руки",
}
MAT_LEVEL_LABELS = {
    0: "нет",
    1: "Bobcat XL",
    2: "Speedo",
    3: "Guardian",
}
TAX_KIND = "inventory.tax.remind"
STATUS_LABELS = {
    "requested": "запрошен",
    "reserved": "зарезервирован",
    "issued": "выдан",
    "returned": "возвращён",
    "cancelled": "отменён",
}


class InventoryError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        decision: AccessDecision | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.decision = decision


@dataclass(frozen=True, slots=True)
class InventoryAlert:
    item_id: str
    created_at: str
    subject_discord_id: int
    title: str


def _actor_id(actor: Any) -> int:
    return int(actor.id)


def _actor_name(actor: Any) -> str:
    return str(getattr(actor, "display_name", None) or getattr(actor, "name", actor.id))


def _parse_stamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def _int_setting(guild_id: int, key: str) -> int:
    raw = await get_registry().get(guild_id, key)
    return int(raw or 0)


async def _require(actor: Any, permission: Permission) -> None:
    decision = await evaluate_access(actor, permission)
    if decision.allowed:
        return
    raise InventoryError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


async def _family_member(actor: Any, guild_id: int):
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, _actor_id(actor))
    if member is None:
        raise InventoryError(
            "Вас нет в текущем составе. Склад не изменён; "
            "обратитесь к старшему составу.",
            code="INV.NOT_MEMBER",
            retryable=False,
        )
    return family, member


def _occupancy_kind(kind: str) -> str:
    if kind == "warehouse":
        return "slots"
    if kind in {"gun_cabinet", "pantry", "mat_truck", "office_safe"}:
        return "quantity"
    return "none"


async def _occupied_value(location: StorageLocation) -> int:
    if location.kind == "warehouse":
        return await occupied_slots(location.id)
    if location.kind == "garage":
        from repositories.cars import count_active_cars

        return await count_active_cars(location.family_id)
    if location.kind in {"gun_cabinet", "pantry", "mat_truck", "office_safe"}:
        return await occupied_quantity(location.id)
    return 0


def _mat_capacity(level: int, l1: int, l2: int, l3: int) -> int:
    if level <= 0:
        return 0
    if level == 1:
        return l1
    if level == 2:
        return l2
    return l3


async def ensure_family_locations(guild_id: int, family_id: int) -> list[StorageLocation]:
    warehouse = await _int_setting(guild_id, "INVENTORY_WAREHOUSE_SLOTS")
    safe = await _int_setting(guild_id, "INVENTORY_SAFE_MONEY")
    gun = await _int_setting(guild_id, "INVENTORY_GUN_CABINET_KG")
    pantry = await _int_setting(guild_id, "INVENTORY_PANTRY_KG")
    garage = await _int_setting(guild_id, "INVENTORY_GARAGE_EXTRA_MAX")
    level = await _int_setting(guild_id, "INVENTORY_MAT_LEVEL")
    l1 = await _int_setting(guild_id, "INVENTORY_MAT_L1_KG")
    l2 = await _int_setting(guild_id, "INVENTORY_MAT_L2_KG")
    l3 = await _int_setting(guild_id, "INVENTORY_MAT_L3_KG")
    specs = (
        ("warehouse", KIND_LABELS["warehouse"], warehouse, "slots", 0),
        ("office_safe", KIND_LABELS["office_safe"], safe, "money", 0),
        ("gun_cabinet", KIND_LABELS["gun_cabinet"], gun, "kg", 0),
        ("pantry", KIND_LABELS["pantry"], pantry, "kg", 0),
        ("wardrobe", KIND_LABELS["wardrobe"], 0, "slots", 0),
        ("garage", KIND_LABELS["garage"], garage, "places", 0),
        (
            "mat_truck",
            KIND_LABELS["mat_truck"],
            _mat_capacity(level, l1, l2, l3),
            "kg",
            level,
        ),
        ("handout", KIND_LABELS["handout"], 0, "none", 0),
    )
    locations = await ensure_locations(
        guild_id=guild_id, family_id=family_id, specs=specs
    )
    truck = next((item for item in locations if item.kind == "mat_truck"), None)
    expected = _mat_capacity(level, l1, l2, l3)
    if truck is not None and (truck.mat_level != level or truck.capacity != expected):
        if truck.mat_level == 0 and level == 0:
            updated = await update_location(
                truck.id,
                expected_version=truck.version,
                capacity=expected,
                mat_level=level,
            )
            if updated is not None:
                locations = await list_locations(family_id)
    return locations


async def _location(
    family_id: int, kind: str, *, guild_id: int
) -> StorageLocation:
    await ensure_family_locations(guild_id, family_id)
    found = await get_location_by_kind(family_id, kind)
    if found is None:
        raise InventoryError(
            "Место хранения не создано. Операция не записана; повторите.",
            code="INV.LOCATION",
            retryable=True,
        )
    return found


async def _item(family_id: int, sku: str) -> InventoryItem:
    text = sku.strip()
    if not text:
        raise InventoryError(
            "Укажите артикул предмета. Каталог не изменён.",
            code="INV.SKU",
            retryable=True,
        )
    found = await get_item_by_sku(family_id, text)
    if found is None:
        raise InventoryError(
            f"Предмет «{text}» не найден в каталоге семьи. "
            "Остаток не изменён; добавьте его командой склада.",
            code="INV.ITEM",
            retryable=True,
        )
    return found


def _conflict_error(exc: StockConflict) -> InventoryError:
    if exc.code == "sold":
        return InventoryError(
            "Склад продан государству. Движения по нему больше не записываются.",
            code="INV.SOLD",
        )
    if exc.code == "occupancy":
        return InventoryError(
            "Ёмкость места хранения исчерпана. Остаток не записан; "
            "освободите место или выберите другое хранилище.",
            code="INV.FULL",
            retryable=True,
        )
    if exc.code == "insufficient":
        return InventoryError(
            "Доступного остатка не хватает. Резерв не создан; "
            f"доступно {exc.available}. Безопасно повторить с меньшим количеством.",
            code="INV.STOCK",
            retryable=True,
        )
    if exc.code == "duplicate":
        return InventoryError(
            "Это движение уже записано. Повтор не создал вторую операцию.",
            code="INV.DUPLICATE",
            retryable=False,
        )
    return InventoryError(
        "Операция со складом не выполнена. Остаток не менялся; повторите.",
        code="INV.CONFLICT",
        retryable=True,
    )


def locations_card(
    locations: list[StorageLocation],
    occupancy: dict[int, int],
    last_recon: str | None,
) -> MessageSpec:
    details: list[tuple[str, str]] = []
    for item in locations:
        used = occupancy.get(item.id, 0)
        extra = ""
        if item.kind == "mat_truck":
            extra = f" · {MAT_LEVEL_LABELS.get(item.mat_level, item.mat_level)}"
        if item.sold:
            extra += " · продан государству"
        if item.auctioned:
            extra += " · на аукционе маркетплейса, предметы на месте"
        cap = "не задана" if item.capacity <= 0 else f"{used}/{item.capacity} {item.capacity_unit}"
        details.append((KIND_LABELS.get(item.kind, item.kind), cap + extra))
    details.append(
        (
            "Сверка с игрой",
            last_recon or "ещё не проводилась — первая версия учитывает вручную",
        )
    )
    return notice(
        code="INV.LOCATIONS",
        title="Места хранения",
        summary="Ёмкости взяты из подтверждённых значений Majestic. Каталог предметов задаёт старший состав.",
        details=tuple(details),
        next_step=f"Поступление — {cmd('stock receive')}. Сверка — {cmd('stock reconcile')}.",
        accent=Accent.LOGISTICS,
    )


def kit_card(kit: InventoryKit, lines: list[KitLine], items: dict[int, InventoryItem]) -> MessageSpec:
    details = [
        ("Событие", str(kit.event_id)),
        ("Статус", STATUS_LABELS.get(kit.status, kit.status)),
        ("Заказал", f"<@{kit.requester_id}>"),
    ]
    for line in lines:
        item = items.get(line.item_id)
        title = item.title if item is not None else str(line.item_id)
        details.append(
            (
                title,
                (
                    f"запрошено {line.requested} · резерв {line.reserved} · "
                    f"выдано {line.issued} · возврат {line.returned} · "
                    f"расход {line.consumed}"
                ),
            )
        )
    return notice(
        code="INV.KIT",
        title=f"Комплект #{kit.id}",
        summary="Состояния не смешиваются: возврат и расход — разные операции.",
        details=tuple(details),
        next_step=(
            f"Резерв — {cmd('stock kit_reserve')}. Выдача — {cmd('stock kit_issue')}."
            if kit.status == "requested"
            else f"Возврат — {cmd('stock kit_return')}. Расход — {cmd('stock consume')}."
        ),
        accent=Accent.LOGISTICS,
        reference=str(kit.id),
    )


async def list_storage(actor: Any, guild_id: int) -> MessageSpec:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    locations = await ensure_family_locations(guild_id, family.id)
    occupancy = {item.id: await _occupied_value(item) for item in locations}
    warehouse = next((item for item in locations if item.kind == "warehouse"), None)
    recon = (
        None
        if warehouse is None
        else await latest_reconciliation(warehouse.id)
    )
    return locations_card(
        locations,
        occupancy,
        None if recon is None else recon.created_at,
    )


async def create_item(
    actor: Any,
    guild_id: int,
    *,
    sku: str,
    title: str,
    unit: str,
    low_stock: int = 0,
) -> InventoryItem:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    code = sku.strip()
    name = title.strip()
    measure = unit.strip()
    if not code or not name or not measure:
        raise InventoryError(
            "Нужны артикул, название и единица. Предмет не добавлен.",
            code="INV.ITEM_FIELDS",
            retryable=True,
        )
    if low_stock < 0:
        raise InventoryError(
            "Порог запаса не может быть отрицательным. Предмет не добавлен.",
            code="INV.LOW_STOCK",
            retryable=True,
        )
    item = await insert_item(
        guild_id=guild_id,
        family_id=family.id,
        sku=code,
        title=name,
        unit=measure,
        low_stock=low_stock,
    )
    await record_decision(
        guild_id=guild_id,
        action="INV.ITEM",
        subject_type="inventory_item",
        subject_id=str(item.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=f"{code}:{name}",
        source="command",
    )
    return item


async def receive_stock(
    actor: Any,
    guild_id: int,
    *,
    sku: str,
    quantity: int,
    location_kind: str = "warehouse",
    basis: str | None = None,
) -> InventoryMovement:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    quantity = _qty(quantity)
    if location_kind not in KIND_LABELS:
        raise InventoryError(
            "Неизвестное место хранения. Поступление не записано.",
            code="INV.LOCATION",
            retryable=True,
        )
    location = await _location(family.id, location_kind, guild_id=guild_id)
    if location.kind == "mat_truck" and location.mat_level <= 0:
        raise InventoryError(
            "Матовозки нет — уровень 0. Поступление не записано; "
            "сначала отметьте покупку, когда кузов пуст.",
            code="INV.NO_TRUCK",
            retryable=True,
        )
    item = await _item(family.id, sku)
    basis_id = (basis or "").strip() or f"receive:{item.id}:{location.id}:{utc_stamp()}"
    try:
        movement = await apply_receive(
            guild_id=guild_id,
            family_id=family.id,
            location=location,
            item_id=item.id,
            quantity=quantity,
            actor_id=_actor_id(actor),
            basis_kind="receive",
            basis_id=basis_id,
            occupancy_kind=_occupancy_kind(location.kind),
        )
    except StockConflict as exc:
        raise _conflict_error(exc) from exc
    await record_decision(
        guild_id=guild_id,
        action="INV.RECEIVE",
        subject_type="inventory_movement",
        subject_id=str(movement.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=f"{item.sku}:{quantity}",
        source="command",
    )
    if location.kind == "warehouse":
        from services.contribution import ContributionError, award_from_warehouse

        try:
            await award_from_warehouse(actor, guild_id, movement.id)
        except ContributionError:
            pass
    return movement


def utc_stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _qty(quantity: int) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise InventoryError(
            "Количество должно быть положительным целым. Операция не записана.",
            code="INV.QTY",
            retryable=True,
        )
    return quantity


async def reserve_for_event(
    actor: Any,
    guild_id: int,
    *,
    event_id: int,
    sku: str,
    quantity: int,
    location_kind: str = "warehouse",
) -> InventoryReservation:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    quantity = _qty(quantity)
    event = await get_event(event_id)
    if event is None or event.family_id != family.id:
        raise InventoryError(
            "Событие не найдено. Резерв не создан.",
            code="INV.EVENT",
            retryable=True,
        )
    if event.status == "cancelled":
        raise InventoryError(
            "Событие отменено. Резерв не создан.",
            code="INV.EVENT_CANCELLED",
        )
    location = await _location(family.id, location_kind, guild_id=guild_id)
    item = await _item(family.id, sku)
    basis_id = f"event:{event.id}:{location.id}:{item.id}"
    try:
        reservation = await apply_reserve(
            guild_id=guild_id,
            family_id=family.id,
            location=location,
            item_id=item.id,
            quantity=quantity,
            actor_id=_actor_id(actor),
            event_id=event.id,
            kit_id=None,
            basis_kind="event",
            basis_id=basis_id,
        )
    except StockConflict as exc:
        raise _conflict_error(exc) from exc
    await record_decision(
        guild_id=guild_id,
        action="INV.RESERVE",
        subject_type="inventory_reservation",
        subject_id=str(reservation.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=f"event:{event.id}:{item.sku}:{quantity}",
        source="command",
    )
    return reservation


async def release_event_reserves(
    actor: Any | None, guild_id: int, event_id: int
) -> list[InventoryReservation]:
    family = await ensure_family(guild_id)
    actor_id = _actor_id(actor) if actor is not None else 0
    released: list[InventoryReservation] = []
    for reservation in await list_open_reservations_for_event(family.id, event_id):
        try:
            updated = await apply_release(
                reservation,
                actor_id=actor_id,
                basis_kind="event",
                basis_id=f"event:{event_id}:{reservation.id}",
            )
        except StockConflict as exc:
            raise _conflict_error(exc) from exc
        released.append(updated)
    for kit in await list_kits_for_event(family.id, event_id):
        if kit.status in {"requested", "reserved"}:
            await update_kit(
                kit.id,
                expected_version=kit.version,
                status="cancelled",
            )
    return released


async def request_kit(
    actor: Any,
    guild_id: int,
    *,
    event_id: int,
    sku: str,
    quantity: int,
) -> InventoryKit:
    family, member = await _family_member(actor, guild_id)
    quantity = _qty(quantity)
    event = await get_event(event_id)
    if event is None or event.family_id != family.id:
        raise InventoryError(
            "Событие не найдено. Комплект не заказан.",
            code="INV.EVENT",
            retryable=True,
        )
    if event.status == "cancelled":
        raise InventoryError(
            "Событие отменено. Комплект не заказан.",
            code="INV.EVENT_CANCELLED",
        )
    item = await _item(family.id, sku)
    open_kits = [
        kit
        for kit in await list_kits_for_event(family.id, event.id)
        if kit.requester_id == member.discord_id and kit.status == "requested"
    ]
    kit = open_kits[0] if open_kits else await insert_kit(
        guild_id=guild_id,
        family_id=family.id,
        event_id=event.id,
        requester_id=member.discord_id,
    )
    await add_kit_line(kit_id=kit.id, item_id=item.id, requested=quantity)
    found = await get_kit(kit.id)
    if found is None:
        raise InventoryError(
            "Комплект не сохранился. Повторите заказ.",
            code="INV.KIT",
            retryable=True,
        )
    return found


async def reserve_kit(actor: Any, guild_id: int, kit_id: int) -> InventoryKit:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    kit = await get_kit(kit_id)
    if kit is None or kit.family_id != family.id:
        raise InventoryError(
            "Комплект не найден. Резерв не создан.",
            code="INV.KIT",
            retryable=True,
        )
    if kit.status == "reserved":
        return kit
    if kit.status != "requested":
        raise InventoryError(
            "Резервируют только запрошенный комплект. Состояние не менялось.",
            code="INV.KIT_STATE",
        )
    warehouse = await _location(family.id, "warehouse", guild_id=guild_id)
    lines = await list_kit_lines(kit.id)
    if not lines:
        raise InventoryError(
            "В комплекте нет позиций. Резерв не создан.",
            code="INV.KIT_EMPTY",
            retryable=True,
        )
    created: list[InventoryReservation] = []
    try:
        for line in lines:
            need = line.requested - line.reserved
            if need <= 0:
                continue
            basis_id = f"kit:{kit.id}:{line.item_id}"
            created.append(
                await apply_reserve(
                    guild_id=guild_id,
                    family_id=family.id,
                    location=warehouse,
                    item_id=line.item_id,
                    quantity=need,
                    actor_id=_actor_id(actor),
                    event_id=kit.event_id,
                    kit_id=kit.id,
                    basis_kind="kit",
                    basis_id=basis_id,
                )
            )
            await update_kit_line_counts(line.id, reserved=line.requested)
    except StockConflict as exc:
        for reservation in created:
            await apply_release(
                reservation,
                actor_id=_actor_id(actor),
                basis_kind="kit",
                basis_id=f"kit:{kit.id}:{reservation.item_id}:abort",
            )
        raise _conflict_error(exc) from exc
    updated = await update_kit(
        kit.id,
        expected_version=kit.version,
        status="reserved",
        reserved_by=_actor_id(actor),
    )
    if updated is None:
        raise InventoryError(
            "Комплект изменился. Резерв позиций записан; откройте карточку и повторите статус.",
            code="INV.CONFLICT",
            retryable=True,
        )
    return updated


async def issue_kit(actor: Any, guild_id: int, kit_id: int) -> InventoryKit:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    kit = await get_kit(kit_id)
    if kit is None or kit.family_id != family.id:
        raise InventoryError(
            "Комплект не найден. Выдача не записана.",
            code="INV.KIT",
            retryable=True,
        )
    if kit.status == "issued":
        return kit
    if kit.status != "reserved":
        raise InventoryError(
            "Выдают только зарезервированный комплект. Состояние не менялось.",
            code="INV.KIT_STATE",
        )
    handout = await _location(family.id, "handout", guild_id=guild_id)
    for reservation in await list_open_reservations_for_kit(kit.id):
        try:
            await apply_issue(
                reservation,
                handout=handout,
                actor_id=_actor_id(actor),
                basis_kind="kit",
                basis_id=f"kit:{kit.id}:{reservation.item_id}",
            )
        except StockConflict as exc:
            raise _conflict_error(exc) from exc
    for line in await list_kit_lines(kit.id):
        await update_kit_line_counts(line.id, issued=line.reserved)
    updated = await update_kit(
        kit.id,
        expected_version=kit.version,
        status="issued",
        issued_by=_actor_id(actor),
    )
    if updated is None:
        raise InventoryError(
            "Комплект изменился. Выдача позиций записана; откройте карточку.",
            code="INV.CONFLICT",
            retryable=True,
        )
    from services.contribution import ContributionError, award_from_logistics

    try:
        await award_from_logistics(actor, guild_id, updated.id)
    except ContributionError:
        pass
    return updated


def _outstanding(line: KitLine) -> int:
    return line.issued - line.returned - line.consumed


async def return_kit_item(
    actor: Any,
    guild_id: int,
    *,
    kit_id: int,
    sku: str,
    quantity: int,
) -> InventoryKit:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    quantity = _qty(quantity)
    kit = await get_kit(kit_id)
    if kit is None or kit.family_id != family.id:
        raise InventoryError(
            "Комплект не найден. Возврат не записан.",
            code="INV.KIT",
            retryable=True,
        )
    if kit.status != "issued":
        raise InventoryError(
            "Возвращают остаток только с выданного комплекта.",
            code="INV.KIT_STATE",
        )
    item = await _item(family.id, sku)
    line = next((row for row in await list_kit_lines(kit.id) if row.item_id == item.id), None)
    if line is None or quantity > _outstanding(line):
        raise InventoryError(
            "Возвращаемое количество больше выданного остатка. "
            "Расход и возврат не смешиваются; остаток не изменён.",
            code="INV.RETURN_QTY",
            retryable=True,
        )
    warehouse = await _location(family.id, "warehouse", guild_id=guild_id)
    handout = await _location(family.id, "handout", guild_id=guild_id)
    basis_id = f"kit:{kit.id}:{item.id}:return:{line.returned + quantity}"
    try:
        await apply_return(
            guild_id=guild_id,
            family_id=family.id,
            handout=handout,
            warehouse=warehouse,
            item_id=item.id,
            quantity=quantity,
            actor_id=_actor_id(actor),
            basis_kind="kit",
            basis_id=basis_id,
        )
    except StockConflict as exc:
        raise _conflict_error(exc) from exc
    await update_kit_line_counts(line.id, returned=line.returned + quantity)
    return await _maybe_close_kit(kit)


async def consume_kit_item(
    actor: Any,
    guild_id: int,
    *,
    kit_id: int,
    sku: str,
    quantity: int,
) -> InventoryKit:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    quantity = _qty(quantity)
    kit = await get_kit(kit_id)
    if kit is None or kit.family_id != family.id:
        raise InventoryError(
            "Комплект не найден. Расход не записан.",
            code="INV.KIT",
            retryable=True,
        )
    if kit.status != "issued":
        raise InventoryError(
            "Списание по результату делают только по выданному комплекту. "
            "Это не отмена резерва.",
            code="INV.KIT_STATE",
        )
    item = await _item(family.id, sku)
    line = next((row for row in await list_kit_lines(kit.id) if row.item_id == item.id), None)
    if line is None or quantity > _outstanding(line):
        raise InventoryError(
            "Списываемое количество больше выданного остатка. "
            "Возврат этим действием не выполняется.",
            code="INV.CONSUME_QTY",
            retryable=True,
        )
    handout = await _location(family.id, "handout", guild_id=guild_id)
    basis_id = f"kit:{kit.id}:{item.id}:consume:{line.consumed + quantity}"
    try:
        await apply_consume(
            guild_id=guild_id,
            family_id=family.id,
            handout=handout,
            item_id=item.id,
            quantity=quantity,
            actor_id=_actor_id(actor),
            basis_kind="kit",
            basis_id=basis_id,
        )
    except StockConflict as exc:
        raise _conflict_error(exc) from exc
    await update_kit_line_counts(line.id, consumed=line.consumed + quantity)
    return await _maybe_close_kit(kit)


async def _maybe_close_kit(kit: InventoryKit) -> InventoryKit:
    lines = await list_kit_lines(kit.id)
    if lines and all(_outstanding(line) <= 0 for line in lines):
        updated = await update_kit(
            kit.id, expected_version=kit.version, status="returned"
        )
        if updated is not None:
            return updated
    found = await get_kit(kit.id)
    if found is None:
        raise InventoryError(
            "Комплект пропал после операции. Проверьте карточку.",
            code="INV.KIT",
            retryable=True,
        )
    return found


async def show_kit(
    actor: Any, guild_id: int, *, kit_id: int | None = None, event_id: int | None = None
) -> MessageSpec:
    family, member = await _family_member(actor, guild_id)
    kit: InventoryKit | None = None
    if kit_id is not None:
        kit = await get_kit(kit_id)
    elif event_id is not None:
        kits = await list_kits_for_event(family.id, event_id)
        kit = kits[-1] if kits else None
    if kit is None or kit.family_id != family.id:
        raise InventoryError(
            "Комплект не найден.",
            code="INV.KIT",
            retryable=True,
        )
    manage = await evaluate_access(actor, Permission.INVENTORY_MANAGE)
    event = await get_event(kit.event_id)
    commander = event is not None and event.commander_id == member.discord_id
    if not manage.allowed and not commander and kit.requester_id != member.discord_id:
        raise InventoryError(
            "Карточку комплекта видят логист, командир события или заказчик.",
            code="INV.KIT_VIEW",
        )
    lines = await list_kit_lines(kit.id)
    catalog = {item.id: item for item in await list_items(family.id)}
    return kit_card(kit, lines, catalog)


async def adjust_stock(
    actor: Any,
    guild_id: int,
    *,
    sku: str,
    quantity: int,
    reason: str,
    location_kind: str = "warehouse",
    add: bool = True,
) -> InventoryMovement:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    quantity = _qty(quantity)
    text = reason.strip()
    if not text:
        raise InventoryError(
            "Укажите причину корректировки. Остаток без основания не меняется.",
            code="INV.REASON",
            retryable=True,
        )
    location = await _location(family.id, location_kind, guild_id=guild_id)
    item = await _item(family.id, sku)
    basis_id = f"adjust:{location.id}:{item.id}:{utc_stamp()}"
    try:
        movement = await apply_adjust(
            guild_id=guild_id,
            family_id=family.id,
            location=location,
            item_id=item.id,
            quantity=quantity,
            add=add,
            actor_id=_actor_id(actor),
            reason=text,
            basis_id=basis_id,
            occupancy_kind=_occupancy_kind(location.kind),
        )
    except StockConflict as exc:
        raise _conflict_error(exc) from exc
    await record_decision(
        guild_id=guild_id,
        action="INV.ADJUST",
        subject_type="inventory_movement",
        subject_id=str(movement.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        reason=text,
        after=f"{item.sku}:{quantity}:{'add' if add else 'remove'}",
        source="command",
    )
    return movement


async def reconcile_location(
    actor: Any,
    guild_id: int,
    *,
    location_kind: str,
    note: str,
) -> MessageSpec:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    text = note.strip()
    if not text:
        raise InventoryError(
            "Опишите сверку с игрой. Запись не создана.",
            code="INV.RECON",
            retryable=True,
        )
    location = await _location(family.id, location_kind, guild_id=guild_id)
    recon = await insert_reconciliation(
        guild_id=guild_id,
        family_id=family.id,
        location_id=location.id,
        note=text,
        actor_id=_actor_id(actor),
    )
    await record_decision(
        guild_id=guild_id,
        action="INV.RECONCILE",
        subject_type="inventory_reconciliation",
        subject_id=str(recon.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        reason=text,
        source="command",
    )
    occupancy = await _occupied_value(location)
    return notice(
        code="INV.RECONCILED",
        title=KIND_LABELS.get(location.kind, location.kind),
        summary=f"Сверка записана {recon.created_at}. Бот сам игру не читает.",
        details=(
            ("Занятость", f"{occupancy}/{location.capacity} {location.capacity_unit}"),
            ("Заметка", text),
        ),
        next_step=f"Остатки — {cmd('stock locations')}.",
        accent=Accent.LOGISTICS,
        reference=str(recon.id),
    )


async def list_alert_items(guild_id: int, family_id: int) -> tuple[InventoryAlert, ...]:
    await ensure_family_locations(guild_id, family_id)
    alerts: list[InventoryAlert] = []
    now = datetime.now(UTC)
    for location in await list_locations(family_id):
        used = await _occupied_value(location)
        if location.capacity > 0 and used >= location.capacity and location.kind != "garage":
            alerts.append(
                InventoryAlert(
                    item_id=f"loc:{location.id}",
                    created_at=location.created_at,
                    subject_discord_id=0,
                    title=f"{KIND_LABELS.get(location.kind, location.kind)} заполнен",
                )
            )
        recon = await latest_reconciliation(location.id)
        stale_hours = await _int_setting(guild_id, "INVENTORY_RECONCILE_STALE_HOURS")
        if stale_hours > 0:
            stamp = (
                None if recon is None else _parse_stamp(recon.created_at)
            )
            if stamp is None or now - stamp >= timedelta(hours=stale_hours):
                alerts.append(
                    InventoryAlert(
                        item_id=f"recon:{location.id}",
                        created_at=location.created_at if recon is None else recon.created_at,
                        subject_discord_id=0,
                        title=f"Нет свежей сверки: {KIND_LABELS.get(location.kind, location.kind)}",
                    )
                )
        catalog = {item.id: item for item in await list_items(family_id)}
        default_low = await _int_setting(guild_id, "INVENTORY_LOW_STOCK")
        for balance in await list_balances(location.id):
            item = catalog.get(balance.item_id)
            if item is None:
                continue
            threshold = item.low_stock if item.low_stock > 0 else default_low
            if threshold <= 0:
                continue
            if balance.on_hand < threshold:
                alerts.append(
                    InventoryAlert(
                        item_id=f"item:{item.id}:{location.id}",
                        created_at=item.created_at,
                        subject_discord_id=0,
                        title=f"Запас ниже порога: {item.title}",
                    )
                )
    for kit in await list_open_issued_kits(family_id):
        alerts.append(
            InventoryAlert(
                item_id=f"kit:{kit.id}",
                created_at=kit.created_at,
                subject_discord_id=kit.requester_id,
                title=f"Невозвращённый комплект #{kit.id}",
            )
        )
    for duty in await list_tax_duties(family_id):
        if _parse_stamp(duty.due_at) <= now:
            alerts.append(
                InventoryAlert(
                    item_id=f"tax:{duty.id}",
                    created_at=duty.due_at,
                    subject_discord_id=duty.responsible_id,
                    title="Налог за имущество: срок наступил",
                )
            )
    return tuple(alerts)


async def alerts_card(actor: Any, guild_id: int) -> MessageSpec:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    items = await list_alert_items(guild_id, family.id)
    if not items:
        return notice(
            code="INV.ALERTS",
            title="Оповещения склада",
            summary="Открытых оповещений нет.",
            next_step=f"Сверка — {cmd('stock reconcile')}.",
            accent=Accent.LOGISTICS,
        )
    details = tuple((item.item_id, item.title) for item in items[:20])
    return notice(
        code="INV.ALERTS",
        title="Оповещения склада",
        summary="Пороги по умолчанию выведены из ёмкостей мест хранения. 0 у предмета — оповещение не задано.",
        details=details,
        next_step=f"Очередь — {cmd('queue list')}.",
        accent=Accent.LOGISTICS,
        tone=MessageTone.WARNING,
    )


async def event_kit_status(guild_id: int, event_id: int) -> str:
    family = await ensure_family(guild_id)
    kits = await list_kits_for_event(family.id, event_id)
    if not kits:
        return "none"
    statuses = {kit.status for kit in kits}
    if "requested" in statuses:
        return "requested"
    if "reserved" in statuses:
        return "reserved"
    if "issued" in statuses:
        return "issued"
    if "returned" in statuses:
        return "returned"
    return "none"


async def event_kit_gap_text(guild_id: int, event_id: int) -> str | None:
    status = await event_kit_status(guild_id, event_id)
    if status == "issued":
        return None
    if status == "returned":
        return None
    if status == "reserved":
        return "Комплект зарезервирован, но ещё не выдан."
    if status == "requested":
        return "Комплект запрошен, логист ещё не зарезервировал остаток."
    return "Комплект не запрошен — готовность снабжения не подтверждена."


async def member_resource_lines(
    guild_id: int, discord_id: int
) -> list[tuple[str, str]]:
    family = await ensure_family(guild_id)
    lines: list[tuple[str, str]] = []
    kits = await list_kits_for_member(family.id, discord_id)
    for kit in kits:
        lines.append(
            (
                f"Комплект #{kit.id}",
                f"{STATUS_LABELS.get(kit.status, kit.status)} · событие {kit.event_id}",
            )
        )
    warehouse = await get_location_by_kind(family.id, "warehouse")
    if warehouse is not None:
        recon = await latest_reconciliation(warehouse.id)
        lines.append(
            (
                "Сверка склада",
                recon.created_at if recon is not None else "ещё не проводилась",
            )
        )
    return lines


async def upgrade_mat_truck(
    actor: Any,
    guild_id: int,
    *,
    confirm_unload: bool,
    confirm: bool,
) -> StorageLocation:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    truck = await _location(family.id, "mat_truck", guild_id=guild_id)
    if truck.mat_level >= 3:
        raise InventoryError(
            "Guardian — максимальный подтверждённый уровень матовозки.",
            code="INV.MAT_MAX",
        )
    cargo = await occupied_quantity(truck.id)
    reserved = 0
    for balance in await list_balances(truck.id):
        reserved += balance.reserved
    if cargo > 0 or reserved > 0:
        raise InventoryError(
            "Покупка следующего уровня уничтожает текущую матовозку вместе с содержимым. "
            "Сначала выгрузите груз в склад организации. Отметка «выгрузить» не заменяет пустой кузов. "
            f"Сейчас на борту {cargo}, в резерве {reserved}.",
            code="INV.MAT_CARGO",
            retryable=True,
        )
    if not confirm_unload:
        raise InventoryError(
            "Перед улучшением подтвердите чек-лист: выгрузить матовозку. "
            "Уровень не изменён.",
            code="INV.MAT_UNLOAD",
            retryable=True,
        )
    if not confirm:
        raise InventoryError(
            "Улучшение необратимо уничтожит текущую матовозку. "
            "Повторите с подтверждением, когда кузов пуст.",
            code="INV.MAT_CONFIRM",
            retryable=True,
        )
    next_level = truck.mat_level + 1
    l1 = await _int_setting(guild_id, "INVENTORY_MAT_L1_KG")
    l2 = await _int_setting(guild_id, "INVENTORY_MAT_L2_KG")
    l3 = await _int_setting(guild_id, "INVENTORY_MAT_L3_KG")
    updated = await update_location(
        truck.id,
        expected_version=truck.version,
        mat_level=next_level,
        capacity=_mat_capacity(next_level, l1, l2, l3),
    )
    if updated is None:
        raise InventoryError(
            "Матовозка изменилась. Уровень не записан; откройте карточку и повторите.",
            code="INV.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="INV.MAT_UPGRADE",
        subject_type="storage_location",
        subject_id=str(updated.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=str(next_level),
        source="command",
    )
    return updated


async def sell_warehouse(
    actor: Any, guild_id: int, *, confirm: bool
) -> StorageLocation:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    warehouse = await _location(family.id, "warehouse", guild_id=guild_id)
    percent = await _int_setting(guild_id, "INVENTORY_SALE_PERCENT")
    if not confirm:
        raise InventoryError(
            f"Продажа склада государству даёт {percent}% стоимости и удаляет все предметы. "
            "Это не аукцион за неуплату налога: там предметы остаются. "
            "Повторите с подтверждением, если готовы потерять содержимое. "
            "Бот цену склада не считает.",
            code="INV.SELL_CONFIRM",
            retryable=True,
        )
    await clear_location_stock(
        guild_id=guild_id,
        family_id=family.id,
        location=warehouse,
        actor_id=_actor_id(actor),
        basis_id=f"sell:{warehouse.id}:{utc_stamp()}",
        reason="продажа склада государству",
    )
    updated = await update_location(
        warehouse.id, expected_version=warehouse.version, sold=True
    )
    if updated is None:
        raise InventoryError(
            "Склад изменился. Продажа не зафиксирована; откройте карточку.",
            code="INV.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="INV.SELL",
        subject_type="storage_location",
        subject_id=str(updated.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        reason=f"{percent}% стоимости, предметы удалены",
        source="command",
    )
    return updated


async def mark_warehouse_auction(
    actor: Any, guild_id: int, *, confirm: bool
) -> StorageLocation:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    warehouse = await _location(family.id, "warehouse", guild_id=guild_id)
    if not confirm:
        raise InventoryError(
            "Неуплата налога выставляет склад на аукцион маркетплейса, но предметы остаются. "
            "Это не продажа государству. Повторите с подтверждением.",
            code="INV.AUCTION_CONFIRM",
            retryable=True,
        )
    updated = await update_location(
        warehouse.id, expected_version=warehouse.version, auctioned=True
    )
    if updated is None:
        raise InventoryError(
            "Склад изменился. Метка аукциона не записана; откройте карточку.",
            code="INV.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="INV.AUCTION",
        subject_type="storage_location",
        subject_id=str(updated.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        reason="неуплата налога, предметы остаются",
        source="command",
    )
    return updated


async def set_tax_duty(
    actor: Any,
    guild_id: int,
    *,
    due_at: str,
    responsible_id: int,
    backup_id: int,
) -> TaxDuty:
    await _require(actor, Permission.INVENTORY_MANAGE)
    family, _member = await _family_member(actor, guild_id)
    try:
        stamp = _parse_stamp(due_at)
    except ValueError as exc:
        raise InventoryError(
            "Срок налога — дата в ISO. Интервал бот не выдумывает: его задаёте вы.",
            code="INV.TAX_WHEN",
            retryable=True,
        ) from exc
    if responsible_id <= 0 or backup_id <= 0:
        raise InventoryError(
            "Нужны ответственный и резервный исполнитель. Напоминание не создано.",
            code="INV.TAX_PEOPLE",
            retryable=True,
        )
    if responsible_id == backup_id:
        raise InventoryError(
            "Резервный исполнитель должен отличаться от ответственного.",
            code="INV.TAX_BACKUP",
            retryable=True,
        )
    iso = stamp.isoformat()
    duty = await upsert_tax_duty(
        guild_id=guild_id,
        family_id=family.id,
        property_kind="warehouse",
        due_at=iso,
        responsible_id=responsible_id,
        backup_id=backup_id,
    )
    await enqueue_operation(
        idempotency_key=f"inventory-tax-{guild_id}-{duty.id}-{iso}",
        guild_id=guild_id,
        kind=TAX_KIND,
        payload={
            "duty_id": duty.id,
            "responsible_id": responsible_id,
            "backup_id": backup_id,
            "due_at": iso,
        },
        aggregate_id=duty.id,
        available_at=iso,
    )
    await record_decision(
        guild_id=guild_id,
        action="INV.TAX",
        subject_type="inventory_tax_duty",
        subject_id=str(duty.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=iso,
        source="command",
    )
    return duty


def tax_reminder_spec(payload: dict[str, Any]) -> MessageSpec:
    due_at = str(payload.get("due_at") or "")
    responsible = int(payload.get("responsible_id") or 0)
    backup = int(payload.get("backup_id") or 0)
    return notice(
        code="INV.TAX_DUE",
        title="Налог за имущество",
        summary=(
            f"Срок {due_at}. Ответственный <@{responsible}>, "
            f"резервный исполнитель <@{backup}>."
        ),
        next_step="Оплатите налог в игре. Интервал бот не назначает — его задаёт старший состав.",
        accent=Accent.LOGISTICS,
        tone=MessageTone.WARNING,
        reference=str(payload.get("duty_id")),
    )


def movement_spec(movement: InventoryMovement, *, title: str, sku: str) -> MessageSpec:
    return notice(
        code=f"INV.{movement.operation.upper()}",
        title=title,
        summary=(
            f"{sku}: {movement.quantity}. Основание {movement.basis_kind} "
            f"{movement.basis_id}."
        ),
        details=(
            ("Операция", movement.operation),
            ("Откуда", str(movement.from_location_id or "—")),
            ("Куда", str(movement.to_location_id or "—")),
        ),
        next_step=f"Места хранения — {cmd('stock locations')}.",
        accent=Accent.LOGISTICS,
        reference=str(movement.id),
    )


def item_spec(item: InventoryItem) -> MessageSpec:
    return notice(
        code="INV.ITEM",
        title=item.title,
        summary=f"Артикул {item.sku}, единица {item.unit}. Каталог не копирует весь игровой инвентарь.",
        details=(
            ("Порог запаса", "не задан" if item.low_stock <= 0 else str(item.low_stock)),
        ),
        next_step=f"Поступление — {cmd('stock receive')}.",
        accent=Accent.LOGISTICS,
        reference=str(item.id),
    )
