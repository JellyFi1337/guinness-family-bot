"""Family vehicles, event reserves and the property register."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.permissions import AccessDecision
from core.settings import get_registry
from repositories.cars import (
    PROPERTY_KINDS,
    STATE_ACTIVE,
    STATE_NEEDS_RECON,
    STATE_RETURNED,
    Car,
    CarBooking,
    FamilyProperty,
    add_car,
    confirm_booking_return,
    create_car_booking,
    get_booking,
    get_car,
    get_family_property_by_title,
    list_family_properties,
    mark_owner_departure as store_owner_departure,
    mark_property_executed,
    update_car,
    upsert_family_property,
)
from repositories.events import get_event
from repositories.inventory import list_tax_duties
from repositories.members import get_current_member, get_member, list_current_roster
from services.audit import record_decision
from services.membership import ensure_family
from ui.command_registry import cmd
from ui.design import Accent
from ui.message_specs import MessageSpec, MessageTone
from ui.templates import notice

OWNER_LEFT_WARNING = (
    "Владелец вышел из семьи. В игре Majestic транспорт возвращается ему. "
    "Машина требует сверки и не считается свободной."
)
STATE_LABELS = {
    STATE_ACTIVE: "в семье",
    STATE_NEEDS_RECON: "требует сверки",
    STATE_RETURNED: "вернулся владельцу",
}
KIND_LABELS = {
    "office": "офис",
    "warehouse": "склад",
    "other": "другое",
}
MAT_LEVEL_SETTINGS = {
    1: "INVENTORY_MAT_L1_KG",
    2: "INVENTORY_MAT_L2_KG",
    3: "INVENTORY_MAT_L3_KG",
}


class VehicleError(Exception):
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
class BookingResult:
    status: str
    booking: CarBooking | None
    car: Car | None
    warning: str


def _actor_id(actor: Any) -> int:
    return int(actor.id)


def _actor_name(actor: Any) -> str:
    return str(getattr(actor, "display_name", None) or getattr(actor, "name", actor.id))


async def _int_setting(guild_id: int, key: str) -> int:
    value = await get_registry().get(guild_id, key)
    return int(value)


async def register_vehicle(
    actor: Any,
    guild_id: int,
    *,
    name: str,
    plate: str,
    owner: str,
    owner_discord_id: int | None = None,
    purpose: str = "",
    cargo_kg: int | None = None,
    mat_level: int | None = None,
    restrictions: str = "",
    access_note: str = "",
) -> Car:
    family = await ensure_family(guild_id)
    owner_member_id: int | None = None
    owner_label = owner.strip()
    if owner_discord_id is not None:
        member = await get_current_member(family.id, owner_discord_id)
        if member is None:
            raise VehicleError(
                "Участник не найден в составе. Владельца-профиль не записали.",
                code="VEH.OWNER",
                retryable=True,
            )
        owner_member_id = member.id
        if not owner_label:
            owner_label = member.ic_name
    confirmed = await _confirmed_cargo(
        guild_id, cargo_kg=cargo_kg, mat_level=mat_level, name=name
    )
    car_id = await add_car(
        name,
        plate,
        owner_label,
        _actor_id(actor),
        guild_id=guild_id,
        family_id=family.id,
        owner_member_id=owner_member_id,
        purpose=purpose,
        cargo_kg=confirmed,
        restrictions=restrictions,
        access_note=access_note,
    )
    car = await get_car(car_id)
    if car is None:
        raise VehicleError(
            "Машина не сохранилась. Повторите добавление.",
            code="VEH.NOT_FOUND",
            retryable=True,
        )
    return car


async def _confirmed_cargo(
    guild_id: int,
    *,
    cargo_kg: int | None,
    mat_level: int | None,
    name: str,
) -> int:
    del name
    if mat_level is not None:
        if mat_level not in MAT_LEVEL_SETTINGS:
            raise VehicleError(
                "Уровень матовозки — 1, 2 или 3 (1000 / 2000 / 4000 кг после подтверждения). "
                "Грузоподъёмность не записана.",
                code="VEH.CARGO",
                retryable=True,
            )
        return await _int_setting(guild_id, MAT_LEVEL_SETTINGS[mat_level])
    if cargo_kg is None:
        return 0
    if cargo_kg < 0:
        raise VehicleError(
            "Грузоподъёмность не может быть отрицательной. Поле не записано.",
            code="VEH.CARGO",
            retryable=True,
        )
    return int(cargo_kg)


async def link_owner(
    car_id: int,
    member_id: int,
    *,
    preserve_label: bool = True,
) -> Car:
    car = await get_car(car_id)
    if car is None:
        raise VehicleError(
            "Машина не найдена. Ссылку на профиль не записали.",
            code="VEH.NOT_FOUND",
            retryable=True,
        )
    member = await get_member(member_id)
    if member is None:
        raise VehicleError(
            "Профиль участника не найден. Ссылку не записали.",
            code="VEH.OWNER",
            retryable=True,
        )
    owner_label = car.owner if preserve_label else member.ic_name
    updated = await update_car(
        car_id,
        owner=owner_label,
        owner_member_id=member.id,
        family_id=member.family_id if car.family_id is None else None,
        guild_id=member.guild_id if car.guild_id is None else None,
    )
    if updated is None:
        raise VehicleError(
            "Машина изменилась. Откройте карточку и повторите привязку.",
            code="VEH.CONFLICT",
            retryable=True,
        )
    return updated


async def link_owner_from_label(car_id: int) -> Car:
    car = await get_car(car_id)
    if car is None:
        raise VehicleError(
            "Машина не найдена. Ссылку на профиль не записали.",
            code="VEH.NOT_FOUND",
            retryable=True,
        )
    if car.family_id is None:
        raise VehicleError(
            "У машины нет семьи. Сначала привяжите её к составу.",
            code="VEH.OWNER",
            retryable=True,
        )
    matches = [
        item
        for item in await list_current_roster(car.family_id)
        if item.ic_name.casefold() == car.owner.strip().casefold()
    ]
    if len(matches) != 1:
        raise VehicleError(
            "Строка владельца не совпала ровно с одним профилем. Ссылку не записали, запасное имя сохранено.",
            code="VEH.OWNER",
            retryable=True,
        )
    return await link_owner(car_id, matches[0].id, preserve_label=True)


async def assign_vehicle(
    actor: Any,
    guild_id: int,
    car_id: int,
    *,
    owner_discord_id: int | None = None,
    purpose: str | None = None,
    cargo_kg: int | None = None,
    mat_level: int | None = None,
    restrictions: str | None = None,
    access_note: str | None = None,
) -> Car:
    family = await ensure_family(guild_id)
    car = await get_car(car_id)
    if car is None:
        raise VehicleError(
            "Машина не найдена. Назначение не записано.",
            code="VEH.NOT_FOUND",
            retryable=True,
        )
    owner_member_id: int | None = None
    owner_label: str | None = None
    if owner_discord_id is not None:
        member = await get_current_member(family.id, owner_discord_id)
        if member is None:
            raise VehicleError(
                "Участник не найден в составе. Владельца не сменили.",
                code="VEH.OWNER",
                retryable=True,
            )
        owner_member_id = member.id
        owner_label = member.ic_name
    confirmed: int | None = None
    if cargo_kg is not None or mat_level is not None:
        confirmed = await _confirmed_cargo(
            guild_id, cargo_kg=cargo_kg, mat_level=mat_level, name=car.name
        )
    updated = await update_car(
        car_id,
        owner=owner_label,
        owner_member_id=owner_member_id,
        purpose=purpose,
        cargo_kg=confirmed,
        restrictions=restrictions,
        access_note=access_note,
        family_id=family.id if car.family_id is None else None,
        guild_id=guild_id if car.guild_id is None else None,
    )
    if updated is None:
        raise VehicleError(
            "Машина изменилась. Откройте карточку и повторите.",
            code="VEH.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="VEH.ASSIGN",
        subject_type="car",
        subject_id=str(car_id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        source="command",
    )
    return updated


async def recon_vehicle(
    actor: Any,
    guild_id: int,
    car_id: int,
    *,
    returned_to_owner: bool,
) -> Car:
    car = await get_car(car_id)
    if car is None:
        raise VehicleError(
            "Машина не найдена. Сверку не закрыли.",
            code="VEH.NOT_FOUND",
            retryable=True,
        )
    new_state = STATE_RETURNED if returned_to_owner else STATE_ACTIVE
    updated = await update_car(car_id, state=new_state)
    if updated is None:
        raise VehicleError(
            "Машина изменилась. Сверку не закрыли; откройте карточку и повторите.",
            code="VEH.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="VEH.RECON",
        subject_type="car",
        subject_id=str(car_id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=car.state,
        after=new_state,
        source="command",
    )
    return updated


async def mark_owner_departure(member: Any) -> tuple[int, int]:
    return await store_owner_departure(int(member.id), OWNER_LEFT_WARNING)


async def book_vehicle(
    actor: Any,
    guild_id: int,
    car_id: int,
    *,
    ic_name: str,
    starts_at: str,
    ends_at: str,
    purpose: str = "",
    event_id: int | None = None,
    driver_discord_id: int | None = None,
) -> BookingResult:
    family = await ensure_family(guild_id)
    car = await get_car(car_id)
    if car is None:
        raise VehicleError(
            "Машина не найдена. Бронь не создана.",
            code="VEH.NOT_FOUND",
            retryable=True,
        )
    if car.state in {STATE_NEEDS_RECON, STATE_RETURNED}:
        raise VehicleError(
            OWNER_LEFT_WARNING if car.state == STATE_NEEDS_RECON
            else "Транспорт вернулся владельцу и не считается доступным. Бронь не создана.",
            code="VEH.UNAVAILABLE",
            retryable=False,
        )
    event_ref = event_id
    window_start = starts_at
    window_end = ends_at
    if event_id is not None:
        event = await get_event(event_id)
        if event is None or event.family_id != family.id:
            raise VehicleError(
                "Событие не найдено. Бронь не привязана.",
                code="VEH.EVENT",
                retryable=True,
            )
        if event.status != "scheduled":
            raise VehicleError(
                "Событие уже не активно. Бронь не создана.",
                code="VEH.EVENT",
                retryable=True,
            )
        if not starts_at:
            window_start = event.starts_at
        if not ends_at:
            window_end = event.ends_at
        event_ref = event.id
    elif not starts_at or not ends_at:
        raise VehicleError(
            "Нужны начало и конец брони. Бронь не создана.",
            code="VEH.WINDOW",
            retryable=True,
        )
    driver_member_id: int | None = None
    driver_id = driver_discord_id if driver_discord_id is not None else _actor_id(actor)
    driver = await get_current_member(family.id, driver_id)
    if driver is not None:
        driver_member_id = driver.id
    status, payload = await create_car_booking(
        car_id,
        _actor_id(actor),
        ic_name,
        window_start,
        window_end,
        purpose=purpose,
        event_id=event_ref,
        driver_member_id=driver_member_id,
    )
    if status == "unavailable":
        raise VehicleError(
            OWNER_LEFT_WARNING,
            code="VEH.UNAVAILABLE",
            retryable=False,
        )
    if status == "busy":
        raise VehicleError(
            "Интервал пересекается с другой бронью этой машины. Выберите другое окно.",
            code="VEH.BUSY",
            retryable=True,
        )
    if status == "invalid":
        raise VehicleError(
            "Окончание брони должно быть позже начала. Бронь не создана.",
            code="VEH.WINDOW",
            retryable=True,
        )
    if status != "booked" or payload is None:
        raise VehicleError(
            "Машина не найдена. Бронь не создана.",
            code="VEH.NOT_FOUND",
            retryable=True,
        )
    booking = await get_booking(int(payload[0]))
    return BookingResult(status="booked", booking=booking, car=car, warning="")


async def return_booking(actor: Any, booking_id: int) -> CarBooking:
    booking = await confirm_booking_return(booking_id, _actor_id(actor))
    if booking is None:
        raise VehicleError(
            "Активная бронь не найдена или уже закрыта. Отметку возврата не поставили.",
            code="VEH.NOT_FOUND",
            retryable=True,
        )
    return booking


async def upsert_property(
    actor: Any,
    guild_id: int,
    *,
    kind: str,
    title: str,
    responsible_id: int,
    backup_id: int,
    due_at: str,
    last_paid_at: str | None = None,
) -> FamilyProperty:
    if kind not in PROPERTY_KINDS:
        raise VehicleError(
            "Вид имущества — офис, склад или другое. Запись не создана.",
            code="VEH.PROPERTY",
            retryable=True,
        )
    name = title.strip()
    due = due_at.strip()
    if not name or not due:
        raise VehicleError(
            "Нужны название и срок, который задаёт состав. Интервал налога бот не выдумывает.",
            code="VEH.PROPERTY",
            retryable=True,
        )
    family = await ensure_family(guild_id)
    item = await upsert_family_property(
        guild_id=guild_id,
        family_id=family.id,
        kind=kind,
        title=name,
        responsible_id=responsible_id,
        backup_id=backup_id,
        due_at=due,
        last_paid_at=last_paid_at,
    )
    await record_decision(
        guild_id=guild_id,
        action="VEH.PROPERTY",
        subject_type="family_property",
        subject_id=str(item.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        source="command",
    )
    return item


async def execute_property(
    actor: Any,
    guild_id: int,
    *,
    kind: str,
    title: str,
    paid_at: str | None = None,
) -> FamilyProperty:
    family = await ensure_family(guild_id)
    item = await get_family_property_by_title(family.id, kind, title.strip())
    if item is None:
        raise VehicleError(
            "Имущество не найдено. Отметку исполнения не поставили.",
            code="VEH.PROPERTY",
            retryable=True,
        )
    updated = await mark_property_executed(item.id, paid_at=paid_at)
    if updated is None:
        raise VehicleError(
            "Имущество изменилось. Откройте запись и повторите.",
            code="VEH.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="VEH.PROPERTY_PAID",
        subject_type="family_property",
        subject_id=str(updated.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        source="command",
    )
    return updated


def vehicle_card(car: Car) -> MessageSpec:
    owner = car.owner
    if car.owner_member_id is not None:
        owner = f"{car.owner} · профиль #{car.owner_member_id}"
    cargo = (
        "не подтверждена"
        if car.cargo_kg <= 0
        else f"{car.cargo_kg} кг (подтверждено составом, не из названия модели)"
    )
    details = [
        ("Номера", f"`{car.plate}`"),
        ("Владелец", owner),
        ("Назначение", car.purpose or "не задано"),
        ("Грузоподъёмность", cargo),
        ("Ограничения", car.restrictions or "не указаны"),
        (
            "Доступ по рангам в игре",
            car.access_note or "справочно, бот игровые права не выдаёт",
        ),
        ("Состояние", STATE_LABELS.get(car.state, car.state)),
    ]
    warning = ""
    tone = MessageTone.INFO
    if car.state == STATE_NEEDS_RECON:
        warning = OWNER_LEFT_WARNING
        tone = MessageTone.WARNING
    elif car.state == STATE_RETURNED:
        warning = "Транспорт вернулся владельцу и не считается доступным."
        tone = MessageTone.WARNING
    return notice(
        code="VEH.CARD",
        title=car.name,
        summary=warning or "Карточка семейного транспорта.",
        details=tuple(details),
        next_step=(
            f"Сверка — {cmd('car recon')}."
            if car.state == STATE_NEEDS_RECON
            else f"Бронь под событие — {cmd('car reserve')}."
        ),
        accent=Accent.LOGISTICS,
        tone=tone,
        reference=car.plate,
    )


def property_card(item: FamilyProperty) -> MessageSpec:
    return notice(
        code="VEH.PROPERTY",
        title=item.title,
        summary="Срок и дату оплаты задаёт состав. Налоговый интервал бот не выдумывает.",
        details=(
            ("Вид", KIND_LABELS.get(item.kind, item.kind)),
            ("Ответственный", f"<@{item.responsible_id}>"),
            ("Резервный", f"<@{item.backup_id}>"),
            ("Срок", item.due_at),
            ("Оплата", item.last_paid_at or "не отмечена"),
            ("Исполнение", "отмечено" if item.executed else "не отмечено"),
        ),
        next_step=(
            "Налог склада по-прежнему ведёт складская запись, эта карточка её не дублирует."
            if item.kind == "warehouse"
            else f"Отметить исполнение — {cmd('car property_mark')}."
        ),
        accent=Accent.LOGISTICS,
        reference=str(item.id),
    )


async def list_property_cards(guild_id: int) -> tuple[FamilyProperty, ...]:
    family = await ensure_family(guild_id)
    return tuple(await list_family_properties(family.id))


async def warehouse_tax_untouched(guild_id: int) -> bool:
    """8.1 warehouse tax stays on inventory_tax_duties; 8.2 does not write it."""

    family = await ensure_family(guild_id)
    return not await list_tax_duties(family.id)
