"""Universal family events. Capacity is stored as a number, never as buttons."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.permissions import ActionScope, AccessDecision, Permission, evaluate_access, evaluate_action
from core.profile import KNOWN_UPGRADES, active_profile
from repositories.events import (
    RESERVE_ORDER_BASIS,
    EventSeat,
    FamilyEvent,
    count_role,
    count_slots,
    find_event_by_start,
    get_event,
    get_seat,
    insert_event,
    list_member_event_history,
    promote_into_main,
    list_member_events,
    list_reserve_queue,
    list_roster,
    list_scheduled_events,
    note_voice_seen,
    release_seat,
    replace_main_with_reserve,
    set_seat_attendance,
    try_insert_seat,
    update_event,
)
from repositories.members import get_current_member, list_member_events as list_lifecycle
from repositories.operations import enqueue_operation
from repositories.training import latest_rules_version
from services.audit import record_decision
from services.calendar import (
    active_schedule,
    cancel_event_reminders,
    conflict_note,
    conflicting_schedule_events,
    default_schedule,
    discord_timestamp,
    schedule_win_reminders,
    sync_event_reminders,
)
from services.member_panel import SECTION_EVENTS, PanelItem, register_panel_source
from services.membership import (
    OpenObligation,
    can_take_internal_tasks,
    ensure_family,
    ensure_obligation_source,
)
from services.roster_watch import register_leftover_action
from ui.command_registry import cmd
from ui.message_specs import MessageSpec, MessageTone
from ui.templates import notice

MAT_TRUCK_UPGRADES = frozenset(
    key for key in ("mat_truck_l1", "mat_truck_l2", "mat_truck_l3") if key in KNOWN_UPGRADES
)


class EventError(Exception):
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
class RoleSpec:
    key: str
    title: str
    quota: int


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class EventTemplate:
    key: str
    title: str
    required_upgrades: tuple[str, ...]
    requires_mat_truck: bool
    capacity: int | None
    min_participants: int | None
    max_workshops: int | None
    attack_limit: int | None
    roles: tuple[RoleSpec, ...]
    checklist: tuple[ChecklistItem, ...]
    notes: tuple[str, ...]
    rule_activity: str | None


@dataclass(frozen=True, slots=True)
class JoinResult:
    event: FamilyEvent
    seat: EventSeat
    overlap_warning: str | None


@dataclass(frozen=True, slots=True)
class LeaveResult:
    event: FamilyEvent
    promoted: EventSeat | None


@dataclass(frozen=True, slots=True)
class ReservePlace:
    position: int
    seat: EventSeat
    basis: str


@dataclass(frozen=True, slots=True)
class AttendanceStats:
    obligated: int
    attended: int
    no_show: int
    pending: int
    cancelled_excluded: int
    excused_excluded: int
    leave_excluded: int


ATTENDANCE_LABELS = {
    "joined": "записался",
    "ready": "подтвердил готовность",
    "attended": "участвовал",
    "reserve": "резерв",
    "excused": "освобождён от участия",
    "no_show": "не явился",
}


_DEFAULT_ROLES = (
    RoleSpec("assault", "штурм", 0),
    RoleSpec("support", "поддержка", 0),
    RoleSpec("mat_driver", "водитель матовозки", 0),
    RoleSpec("recon", "разведка", 0),
    RoleSpec("medic", "медик", 0),
)
_MEETING_ROLES = (
    RoleSpec("lead", "ведущий", 0),
    RoleSpec("participant", "участник", 0),
)
_VZA_CHECKLIST = (
    ChecklistItem("forbidden", "анимации, кроме функциональных"),
    ChecklistItem("forbidden", "снайперские винтовки"),
    ChecklistItem("forbidden", "тепловизор"),
    ChecklistItem("forbidden", "дрон"),
)

EVENT_TEMPLATES: dict[str, EventTemplate] = {
    "capt_square": EventTemplate(
        key="capt_square",
        title="Капт: обычный квадрат",
        required_upgrades=("territory_control",),
        requires_mat_truck=False,
        capacity=20,
        min_participants=None,
        max_workshops=None,
        attack_limit=None,
        roles=_DEFAULT_ROLES,
        checklist=(),
        notes=("Телепорт на капт идёт через NPC в офисе организации.",),
        rule_activity="territory_control",
    ),
    "capt_business": EventTemplate(
        key="capt_business",
        title="Капт: Бизнес",
        required_upgrades=("territory_control",),
        requires_mat_truck=False,
        capacity=20,
        min_participants=None,
        max_workshops=None,
        attack_limit=None,
        roles=_DEFAULT_ROLES,
        checklist=(),
        notes=("Телепорт на капт идёт через NPC в офисе организации.",),
        rule_activity="territory_control",
    ),
    "capt_checkpoint": EventTemplate(
        key="capt_checkpoint",
        title="Капт: контрольная точка",
        required_upgrades=("territory_control",),
        requires_mat_truck=False,
        capacity=35,
        min_participants=None,
        max_workshops=None,
        attack_limit=None,
        roles=_DEFAULT_ROLES,
        checklist=(),
        notes=("Телепорт на капт идёт через NPC в офисе организации.",),
        rule_activity="territory_control",
    ),
    "capt_mansion": EventTemplate(
        key="capt_mansion",
        title="Капт: Особняк",
        required_upgrades=("territory_control",),
        requires_mat_truck=False,
        capacity=50,
        min_participants=None,
        max_workshops=None,
        attack_limit=None,
        roles=_DEFAULT_ROLES,
        checklist=(),
        notes=("Телепорт на капт идёт через NPC в офисе организации.",),
        rule_activity="territory_control",
    ),
    "air_cargo": EventTemplate(
        key="air_cargo",
        title="ВЗА",
        required_upgrades=("air_cargo",),
        requires_mat_truck=True,
        capacity=None,
        min_participants=None,
        max_workshops=None,
        attack_limit=None,
        roles=_DEFAULT_ROLES,
        checklist=_VZA_CHECKLIST,
        notes=(
            "С запрещённым предметом в инвентаре система не телепортирует в зону.",
        ),
        rule_activity="air_cargo",
    ),
    "workshops": EventTemplate(
        key="workshops",
        title="Захват цехов",
        required_upgrades=("workshops",),
        requires_mat_truck=False,
        capacity=None,
        min_participants=4,
        max_workshops=5,
        attack_limit=None,
        roles=_DEFAULT_ROLES,
        checklist=(),
        notes=(
            "На захват одного цеха нужно минимум 4 человека. Цехов не больше 5.",
        ),
        rule_activity="workshops",
    ),
    "dealers": EventTemplate(
        key="dealers",
        title="Вербовка дилеров",
        required_upgrades=("dealers",),
        requires_mat_truck=False,
        capacity=None,
        min_participants=None,
        max_workshops=None,
        attack_limit=None,
        roles=_DEFAULT_ROLES,
        checklist=(),
        notes=(),
        rule_activity="dealers",
    ),
    "fort_zancudo": EventTemplate(
        key="fort_zancudo",
        title="Форт-Занкудо",
        required_upgrades=("fort_zancudo",),
        requires_mat_truck=False,
        capacity=None,
        min_participants=10,
        max_workshops=None,
        attack_limit=2,
        roles=_DEFAULT_ROLES,
        checklist=(),
        notes=("Лимит нападений организации: 2.",),
        rule_activity="fort_zancudo",
    ),
    "cayo_perico": EventTemplate(
        key="cayo_perico",
        title="Кайо-Перико",
        required_upgrades=("cayo_perico",),
        requires_mat_truck=False,
        capacity=None,
        min_participants=10,
        max_workshops=None,
        attack_limit=3,
        roles=_DEFAULT_ROLES,
        checklist=(),
        notes=("Лимит нападений организации: 3.",),
        rule_activity="cayo_perico",
    ),
    "contract_run": EventTemplate(
        key="contract_run",
        title="Контрактный выезд",
        required_upgrades=(),
        requires_mat_truck=False,
        capacity=None,
        min_participants=None,
        max_workshops=None,
        attack_limit=None,
        roles=_DEFAULT_ROLES,
        checklist=(),
        notes=(),
        rule_activity=None,
    ),
    "training": EventTemplate(
        key="training",
        title="Обучение",
        required_upgrades=(),
        requires_mat_truck=False,
        capacity=None,
        min_participants=None,
        max_workshops=None,
        attack_limit=None,
        roles=_MEETING_ROLES,
        checklist=(),
        notes=(),
        rule_activity=None,
    ),
    "meeting": EventTemplate(
        key="meeting",
        title="Внутренняя встреча",
        required_upgrades=(),
        requires_mat_truck=False,
        capacity=None,
        min_participants=None,
        max_workshops=None,
        attack_limit=None,
        roles=_MEETING_ROLES,
        checklist=(),
        notes=(),
        rule_activity=None,
    ),
}


def has_mat_truck(enabled: frozenset[str]) -> bool:
    return bool(enabled & MAT_TRUCK_UPGRADES)


def parse_role_quotas(raw: str | None) -> dict[str, int] | None:
    if raw is None or not raw.strip():
        return None
    result: dict[str, int] = {}
    for chunk in raw.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if ":" not in piece:
            raise EventError(
                "Квоты пишутся как роль:число через запятую. "
                "Событие не создано.",
                code="EVENTS.ROLE",
                retryable=True,
            )
        key, value = piece.split(":", 1)
        try:
            quota = int(value.strip())
        except ValueError as exc:
            raise EventError(
                "Квота роли должна быть целым числом. Событие не создано.",
                code="EVENTS.ROLE",
                retryable=True,
            ) from exc
        result[key.strip()] = quota
    return result or None


def get_template(key: str) -> EventTemplate:
    template = EVENT_TEMPLATES.get(key.strip())
    if template is None:
        raise EventError(
            "Неизвестный шаблон события. "
            "Событие не создано; выберите шаблон из списка.",
            code="EVENTS.UNKNOWN_TEMPLATE",
            retryable=True,
        )
    return template


def _actor_id(actor: Any) -> int:
    return int(actor.id)


def _actor_name(actor: Any) -> str:
    return str(getattr(actor, "display_name", None) or getattr(actor, "name", actor.id))


def parse_stamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EventError(
            "Время пишется в ISO, например 2026-09-09T18:00:00+03:00. "
            "Событие не изменено; исправьте время и повторите.",
            code="EVENTS.BAD_TIME",
            retryable=True,
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def intervals_overlap(left_start: str, left_end: str, right_start: str, right_end: str) -> bool:
    return parse_stamp(left_start) < parse_stamp(right_end) and parse_stamp(
        right_start
    ) < parse_stamp(left_end)


async def _require(actor: Any, permission: Permission) -> None:
    decision = await evaluate_access(actor, permission)
    if decision.allowed:
        return
    raise EventError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


async def _require_manage(actor: Any, event: FamilyEvent) -> None:
    everyone = await evaluate_access(actor, Permission.EVENTS_MANAGE_ALL)
    if everyone.allowed:
        return
    own = await evaluate_action(
        actor,
        Permission.EVENTS_MANAGE_OWN,
        ActionScope(kind="event", owner_id=event.commander_id),
    )
    if own.allowed:
        return
    raise EventError(
        own.summary or everyone.summary,
        code=own.code if not own.allowed else everyone.code,
        retryable=False,
        decision=own if not own.allowed else everyone,
    )


async def _require_event_commander(actor: Any, event: FamilyEvent) -> None:
    if _actor_id(actor) == event.commander_id:
        return
    raise EventError(
        "Итоговую явку подтверждает командир этого события. "
        "Отметка не записана.",
        code="EVENTS.NOT_COMMANDER",
    )


async def _family_member(actor: Any, guild_id: int):
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, _actor_id(actor))
    if member is None:
        raise EventError(
            "Вас нет в текущем составе. Событие не изменено; "
            "обратитесь к кадровику.",
            code="EVENTS.NOT_A_MEMBER",
        )
    return family, member


def template_blocked(template: EventTemplate, enabled: frozenset[str]) -> str | None:
    missing = [key for key in template.required_upgrades if key not in enabled]
    if missing:
        return "не открыто улучшение: " + ", ".join(missing)
    if template.requires_mat_truck and not has_mat_truck(enabled):
        return "для ВЗА нужна матовозка (улучшение mat_truck_l1, l2 или l3)"
    return None


def available_templates(enabled: frozenset[str]) -> tuple[EventTemplate, ...]:
    return tuple(
        item
        for item in EVENT_TEMPLATES.values()
        if template_blocked(item, enabled) is None
    )


def _role_map(template: EventTemplate) -> dict[str, RoleSpec]:
    return {item.key: item for item in template.roles}


def _resolve_capacity(
    template: EventTemplate,
    *,
    capacity: int | None,
    workshop_count: int | None,
) -> tuple[int, int | None]:
    count: int | None = None
    if template.max_workshops is not None:
        if workshop_count is None:
            raise EventError(
                "Для захвата цехов укажите число цехов от 1 до 5. "
                "Событие не создано.",
                code="EVENTS.WORKSHOP_COUNT",
                retryable=True,
            )
        if workshop_count < 1 or workshop_count > template.max_workshops:
            raise EventError(
                "Цехов может быть от 1 до 5. Событие не создано.",
                code="EVENTS.WORKSHOP_COUNT",
                retryable=True,
            )
        count = workshop_count
        minimum = (template.min_participants or 4) * workshop_count
        resolved = capacity if capacity is not None else minimum
        if resolved < minimum:
            raise EventError(
                f"На {workshop_count} цех(ов) нужно минимум {minimum} человек. "
                "Событие не создано.",
                code="EVENTS.CAPACITY",
                retryable=True,
            )
        return resolved, count
    if template.capacity is not None:
        if capacity is not None and capacity != template.capacity:
            raise EventError(
                f"Вместимость шаблона «{template.title}» задана сервером: "
                f"{template.capacity}. Событие не создано; не меняйте лимит.",
                code="EVENTS.CAPACITY",
                retryable=True,
            )
        return template.capacity, count
    if capacity is None or capacity <= 0:
        raise EventError(
            "Вместимость этого шаблона не подтверждена источником. "
            "Укажите число мест сами. Событие не создано.",
            code="EVENTS.CAPACITY",
            retryable=True,
        )
    if template.min_participants is not None and capacity < template.min_participants:
        raise EventError(
            f"Нужно минимум {template.min_participants} участников организации. "
            "Событие не создано.",
            code="EVENTS.CAPACITY",
            retryable=True,
        )
    return capacity, count


async def list_templates(actor: Any, guild_id: int) -> tuple[EventTemplate, ...]:
    await _require(actor, Permission.EVENTS_CREATE)
    profile = await active_profile(guild_id)
    return available_templates(profile.enabled_upgrades)


async def create_event(
    actor: Any,
    guild_id: int,
    template_key: str,
    *,
    starts_at: str,
    ends_at: str,
    meetup: str,
    capacity: int | None = None,
    reserve_capacity: int = 0,
    workshop_count: int | None = None,
    resource_plan: str | None = None,
    f2_event_ref: str | None = None,
    commander_id: int | None = None,
    role_quotas: dict[str, int] | None = None,
) -> FamilyEvent:
    await _require(actor, Permission.EVENTS_CREATE)
    family, _member = await _family_member(actor, guild_id)
    template = get_template(template_key)
    profile = await active_profile(guild_id)
    blocked = template_blocked(template, profile.enabled_upgrades)
    if blocked is not None:
        raise EventError(
            f"Шаблон «{template.title}» недоступен: {blocked}. "
            "Событие не создано; откройте улучшение в профиле или выберите другой шаблон.",
            code="EVENTS.UPGRADE",
            retryable=False,
        )
    start = parse_stamp(starts_at)
    end = parse_stamp(ends_at)
    if end <= start:
        raise EventError(
            "Окончание должно быть позже начала. Событие не создано.",
            code="EVENTS.BAD_TIME",
            retryable=True,
        )
    place = meetup.strip()
    if not place:
        raise EventError(
            "Укажите место сбора. Событие не создано.",
            code="EVENTS.MEETUP",
            retryable=True,
        )
    if reserve_capacity < 0:
        raise EventError(
            "Резерв не может быть отрицательным. Событие не создано.",
            code="EVENTS.CAPACITY",
            retryable=True,
        )
    resolved_capacity, workshops = _resolve_capacity(
        template, capacity=capacity, workshop_count=workshop_count
    )
    commander = commander_id if commander_id is not None else _actor_id(actor)
    if await get_current_member(family.id, commander) is None:
        raise EventError(
            "Командир должен быть в текущем составе. Событие не создано.",
            code="EVENTS.COMMANDER",
            retryable=True,
        )
    roles = list(template.roles)
    if role_quotas:
        known = {item.key for item in roles}
        unknown = [key for key in role_quotas if key not in known]
        if unknown:
            raise EventError(
                "Неизвестная роль для квоты: "
                + ", ".join(unknown)
                + ". Событие не создано.",
                code="EVENTS.ROLE",
                retryable=True,
            )
        if any(value < 0 for value in role_quotas.values()):
            raise EventError(
                "Квота роли не может быть отрицательной. Событие не создано.",
                code="EVENTS.ROLE",
                retryable=True,
            )
        roles = [
            RoleSpec(item.key, item.title, role_quotas.get(item.key, item.quota))
            for item in roles
        ]
    rule_version = None
    if template.rule_activity:
        published = await latest_rules_version(family.id, template.rule_activity)
        if published is not None:
            rule_version = published.version
    snapshot = {
        "template_key": template.key,
        "title": template.title,
        "required_upgrades": list(template.required_upgrades),
        "requires_mat_truck": template.requires_mat_truck,
        "capacity": resolved_capacity,
        "reserve_capacity": reserve_capacity,
        "min_participants": template.min_participants,
        "workshop_count": workshops,
        "attack_limit": template.attack_limit,
        "roles": [
            {"key": item.key, "title": item.title, "quota": item.quota}
            for item in roles
        ],
        "checklist": [
            {"kind": item.kind, "text": item.text} for item in template.checklist
        ],
        "notes": list(template.notes),
        "rule_activity": template.rule_activity,
        "rule_version": rule_version,
        "enabled_upgrades": sorted(profile.enabled_upgrades),
        "has_mat_truck": has_mat_truck(profile.enabled_upgrades),
    }
    start_text = start.isoformat()
    existing = await find_event_by_start(family.id, template.key, start_text)
    if existing is not None:
        await sync_event_reminders(existing)
        return existing
    event = await insert_event(
        guild_id=guild_id,
        family_id=family.id,
        template_key=template.key,
        title=template.title,
        starts_at=start_text,
        ends_at=end.isoformat(),
        commander_id=commander,
        meetup=place,
        capacity=resolved_capacity,
        reserve_capacity=reserve_capacity,
        resource_plan=(resource_plan or "").strip(),
        f2_event_ref=(f2_event_ref or "").strip() or None,
        snapshot=snapshot,
        created_by=_actor_id(actor),
    )
    await record_decision(
        guild_id=guild_id,
        action="EVENTS.CREATED",
        subject_type="family_event",
        subject_id=str(event.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=template.key,
        source="command",
    )
    await sync_event_reminders(event)
    return event


async def list_events(actor: Any, guild_id: int) -> list[FamilyEvent]:
    family, _member = await _family_member(actor, guild_id)
    return await list_scheduled_events(family.id)


async def show_event(actor: Any, guild_id: int, event_id: int) -> FamilyEvent:
    family, _member = await _family_member(actor, guild_id)
    event = await get_event(event_id)
    if event is None or event.family_id != family.id:
        raise EventError(
            "Событие не найдено. Карточка не открыта; проверьте номер.",
            code="EVENTS.NOT_FOUND",
            retryable=True,
        )
    return event


async def roster_lines(event: FamilyEvent, *, slot: str | None = None) -> list[str]:
    seats = await list_roster(event.id)
    reserve_order = {
        item.discord_id: index
        for index, item in enumerate(await list_reserve_queue(event.id), start=1)
    }
    lines: list[str] = []
    for seat in seats:
        if slot and seat.slot != slot:
            continue
        role = seat.role_key or "без роли"
        place = "основа" if seat.slot == "main" else "резерв"
        status = ATTENDANCE_LABELS.get(seat.attendance, seat.attendance)
        extra = ""
        if seat.slot == "reserve":
            extra = f" · {reserve_order.get(seat.discord_id, '?')} · {RESERVE_ORDER_BASIS}"
        voice = " · голос (подсказка)" if seat.voice_seen_at else ""
        lines.append(
            f"{place} · {role} · {status}{extra}{voice} · <@{seat.discord_id}>"
        )
    return lines


async def _overlap_warning(
    family_id: int, discord_id: int, event: FamilyEvent
) -> str | None:
    others = await list_member_events(family_id, discord_id)
    conflicts = [
        other
        for other, _seat in others
        if other.id != event.id
        and intervals_overlap(event.starts_at, event.ends_at, other.starts_at, other.ends_at)
    ]
    if not conflicts:
        return None
    refs = ", ".join(f"#{item.id}" for item in conflicts)
    return (
        f"Это время пересекается с событием {refs}. "
        "Запись сохранена; решите, в каком составе вы остаётесь."
    )


def _quota_for(event: FamilyEvent, role_key: str) -> int | None:
    for item in event.snapshot.get("roles") or []:
        if isinstance(item, dict) and str(item.get("key")) == role_key:
            return int(item.get("quota") or 0)
    return None


async def join_event(
    actor: Any,
    guild_id: int,
    event_id: int,
    *,
    role_key: str | None = None,
    confirm_conflict: bool = False,
) -> JoinResult:
    family, member = await _family_member(actor, guild_id)
    if not can_take_internal_tasks(member):
        raise EventError(
            "Записаться могут участники на испытательном сроке и в составе. "
            "Место не занято.",
            code="EVENTS.NOT_READY",
        )
    event = await get_event(event_id)
    if event is None or event.family_id != family.id:
        raise EventError(
            "Событие не найдено. Запись не создана.",
            code="EVENTS.NOT_FOUND",
            retryable=True,
        )
    if event.status != "scheduled":
        raise EventError(
            "Событие отменено. Запись не создана.",
            code="EVENTS.CANCELLED",
        )
    existing = await get_seat(event.id, member.discord_id)
    if existing is not None:
        return JoinResult(
            event,
            existing,
            await _overlap_warning(family.id, member.discord_id, event),
        )
    if not confirm_conflict:
        schedule = await active_schedule(guild_id)
        conflicts = await conflicting_schedule_events(
            family.id, member.discord_id, event, schedule
        )
        if conflicts:
            refs = ", ".join(f"#{item.id}" for item in conflicts)
            raise EventError(
                "Это время пересекается с лотереей или аукционом контейнеров "
                "и вечерней вербовкой дилеров. Одни и те же люди не записываются "
                "в оба состава без подтверждения. "
                f"Конфликт: {refs}. Место не занято; подтвердите запись.",
                code="EVENTS.SCHEDULE_CONFLICT",
                retryable=True,
            )
    role = (role_key or "").strip() or None
    if role is not None:
        quota = _quota_for(event, role)
        if quota is None:
            raise EventError(
                "Такой роли нет в снимке шаблона. Место не занято; "
                "выберите роль из карточки.",
                code="EVENTS.ROLE",
                retryable=True,
            )
    main, reserve = await count_slots(event.id)
    if main < event.capacity:
        slot = "main"
    elif reserve < event.reserve_capacity:
        slot = "reserve"
    else:
        raise EventError(
            "Состав и резерв заполнены. Место не занято.",
            code="EVENTS.FULL",
            retryable=True,
        )
    if slot == "main" and role is not None:
        quota = _quota_for(event, role) or 0
        if quota > 0 and await count_role(event.id, role) >= quota:
            raise EventError(
                f"Квота роли «{role}» уже заполнена. "
                "Место не занято; выберите другую роль или резерв без этой роли.",
                code="EVENTS.ROLE_FULL",
                retryable=True,
            )
    seat = await try_insert_seat(
        guild_id=guild_id,
        family_id=family.id,
        event_id=event.id,
        discord_id=member.discord_id,
        slot=slot,
        role_key=role,
    )
    if seat is None:
        again = await get_seat(event.id, member.discord_id)
        if again is not None:
            return JoinResult(
                event,
                again,
                await _overlap_warning(family.id, member.discord_id, event),
            )
        raise EventError(
            "Состав изменился. Место не занято; откройте карточку и повторите.",
            code="EVENTS.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="EVENTS.JOINED",
        subject_type="family_event",
        subject_id=str(event.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=seat.slot,
        source="command",
    )
    return JoinResult(
        event,
        seat,
        await _overlap_warning(family.id, member.discord_id, event),
    )


async def leave_event(actor: Any, guild_id: int, event_id: int) -> LeaveResult:
    family, member = await _family_member(actor, guild_id)
    event = await get_event(event_id)
    if event is None or event.family_id != family.id:
        raise EventError(
            "Событие не найдено. Запись не снята.",
            code="EVENTS.NOT_FOUND",
            retryable=True,
        )
    if event.roster_locked:
        raise EventError(
            "Состав зафиксирован. Сами снять запись нельзя; "
            f"командир делает замену через {cmd('event replace')} с причиной.",
            code="EVENTS.LOCKED",
        )
    removed, promoted = await release_seat(
        event.id,
        member.discord_id,
        actor_id=_actor_id(actor),
        auto_promote=True,
        reason="снял запись",
    )
    if removed is None:
        raise EventError(
            "Вас нет в составе этого события. Повтор не нужен.",
            code="EVENTS.NOT_SEATED",
            retryable=False,
        )
    await record_decision(
        guild_id=guild_id,
        action="EVENTS.LEFT",
        subject_type="family_event",
        subject_id=str(event.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        source="command",
    )
    return LeaveResult(event, promoted)


async def cancel_event(actor: Any, guild_id: int, event_id: int) -> FamilyEvent:
    family, _member = await _family_member(actor, guild_id)
    event = await get_event(event_id)
    if event is None or event.family_id != family.id:
        raise EventError(
            "Событие не найдено. Отмена не записана.",
            code="EVENTS.NOT_FOUND",
            retryable=True,
        )
    await _require_manage(actor, event)
    if event.status == "cancelled":
        return event
    updated = await update_event(
        event.id,
        expected_version=event.version,
        status="cancelled",
        cancelled_by=_actor_id(actor),
    )
    if updated is None:
        raise EventError(
            "Событие изменилось. Отмена не записана; откройте карточку и повторите.",
            code="EVENTS.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="EVENTS.CANCELLED",
        subject_type="family_event",
        subject_id=str(event.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        source="command",
    )
    await cancel_event_reminders(guild_id, event.id, reason="cancelled")
    from services.inventory import release_event_reserves

    await release_event_reserves(actor, guild_id, event.id)
    return updated


async def reschedule_event(
    actor: Any,
    guild_id: int,
    event_id: int,
    *,
    starts_at: str,
    ends_at: str,
) -> FamilyEvent:
    family, _member = await _family_member(actor, guild_id)
    event = await get_event(event_id)
    if event is None or event.family_id != family.id:
        raise EventError(
            "Событие не найдено. Перенос не записан.",
            code="EVENTS.NOT_FOUND",
            retryable=True,
        )
    await _require_manage(actor, event)
    if event.status != "scheduled":
        raise EventError(
            "Отменённое событие нельзя перенести.",
            code="EVENTS.CANCELLED",
        )
    start = parse_stamp(starts_at)
    end = parse_stamp(ends_at)
    if end <= start:
        raise EventError(
            "Окончание должно быть позже начала. Перенос не записан.",
            code="EVENTS.BAD_TIME",
            retryable=True,
        )
    updated = await update_event(
        event.id,
        expected_version=event.version,
        starts_at=start.isoformat(),
        ends_at=end.isoformat(),
    )
    if updated is None:
        raise EventError(
            "Событие изменилось. Перенос не записан; откройте карточку и повторите.",
            code="EVENTS.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="EVENTS.RESCHEDULED",
        subject_type="family_event",
        subject_id=str(event.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=event.starts_at,
        after=updated.starts_at,
        source="command",
    )
    await cancel_event_reminders(guild_id, event.id, reason="reschedule")
    await sync_event_reminders(updated)
    return updated


async def record_event_outcome(
    actor: Any,
    guild_id: int,
    event_id: int,
    result: str,
) -> FamilyEvent:
    family, _member = await _family_member(actor, guild_id)
    event = await get_event(event_id)
    if event is None or event.family_id != family.id:
        raise EventError(
            "Событие не найдено. Итог не записан.",
            code="EVENTS.NOT_FOUND",
            retryable=True,
        )
    await _require_manage(actor, event)
    if event.status == "cancelled":
        raise EventError(
            "Отменённое событие не получает итог. Запись не создана.",
            code="EVENTS.CANCELLED",
        )
    normalized = result.strip().casefold()
    if normalized not in {"won", "lost"}:
        raise EventError(
            "Итог — won или lost. Значение не записано; исправьте и повторите.",
            code="EVENTS.OUTCOME",
            retryable=True,
        )
    if str(event.snapshot.get("outcome") or "") == normalized:
        return event
    snapshot = dict(event.snapshot)
    won_at = datetime.now(UTC).isoformat()
    snapshot["outcome"] = normalized
    snapshot["outcome_at"] = won_at
    snapshot["outcome_by"] = _actor_id(actor)
    updated = await update_event(
        event.id,
        expected_version=event.version,
        snapshot=snapshot,
    )
    if updated is None:
        raise EventError(
            "Событие изменилось. Итог не записан; откройте карточку и повторите.",
            code="EVENTS.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="EVENTS.OUTCOME",
        subject_type="family_event",
        subject_id=str(event.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=normalized,
        source="command",
    )
    if normalized == "won":
        await schedule_win_reminders(updated, won_at=won_at)
    return updated


async def replace_commander(
    actor: Any, guild_id: int, event_id: int, commander_id: int
) -> FamilyEvent:
    family, _member = await _family_member(actor, guild_id)
    event = await get_event(event_id)
    if event is None or event.family_id != family.id:
        raise EventError(
            "Событие не найдено. Командир не изменён.",
            code="EVENTS.NOT_FOUND",
            retryable=True,
        )
    await _require_manage(actor, event)
    if await get_current_member(family.id, commander_id) is None:
        raise EventError(
            "Новый командир должен быть в текущем составе. Замена не записана.",
            code="EVENTS.COMMANDER",
            retryable=True,
        )
    if event.commander_id == commander_id:
        return event
    updated = await update_event(
        event.id, expected_version=event.version, commander_id=commander_id
    )
    if updated is None:
        raise EventError(
            "Событие изменилось. Замена не записана; откройте карточку и повторите.",
            code="EVENTS.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="EVENTS.COMMANDER",
        subject_type="family_event",
        subject_id=str(event.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=str(event.commander_id),
        after=str(commander_id),
        source="command",
    )
    return updated


async def confirm_ready(actor: Any, guild_id: int, event_id: int) -> EventSeat:
    family, member = await _family_member(actor, guild_id)
    event = await show_event(actor, guild_id, event_id)
    if event.status != "scheduled":
        raise EventError(
            "Событие отменено. Готовность не записана.",
            code="EVENTS.CANCELLED",
        )
    seat = await get_seat(event.id, member.discord_id)
    if seat is None:
        raise EventError(
            "Вас нет в составе. Готовность не записана; сначала запишитесь.",
            code="EVENTS.NOT_SEATED",
            retryable=True,
        )
    if seat.attendance in {"attended", "no_show", "excused"}:
        raise EventError(
            "По этому месту явка уже закрыта. Готовность не меняется.",
            code="EVENTS.CLOSED",
        )
    if seat.attendance == "ready":
        return seat
    updated = await set_seat_attendance(
        event.id,
        member.discord_id,
        attendance="ready",
        actor_id=_actor_id(actor),
        reason="подтвердил готовность",
    )
    if updated is None:
        raise EventError(
            "Состав изменился. Готовность не записана; откройте карточку и повторите.",
            code="EVENTS.CONFLICT",
            retryable=True,
        )
    return updated


async def lock_roster(actor: Any, guild_id: int, event_id: int) -> FamilyEvent:
    event = await show_event(actor, guild_id, event_id)
    await _require_manage(actor, event)
    if event.status != "scheduled":
        raise EventError(
            "Отменённое событие нельзя зафиксировать.",
            code="EVENTS.CANCELLED",
        )
    if event.roster_locked:
        return event
    updated = await update_event(
        event.id,
        expected_version=event.version,
        roster_locked_by=_actor_id(actor),
    )
    if updated is None:
        raise EventError(
            "Событие изменилось. Фиксация не записана; откройте карточку и повторите.",
            code="EVENTS.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="EVENTS.ROSTER_LOCKED",
        subject_type="family_event",
        subject_id=str(event.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        source="command",
    )
    return updated


async def confirm_attendance(
    actor: Any,
    guild_id: int,
    event_id: int,
    member_id: int,
    result: str,
) -> EventSeat:
    event = await show_event(actor, guild_id, event_id)
    await _require_event_commander(actor, event)
    if result not in {"attended", "no_show"}:
        raise EventError(
            "Явка бывает только «участвовал» или «не явился». Отметка не записана.",
            code="EVENTS.ATTENDANCE",
            retryable=True,
        )
    seat = await get_seat(event.id, member_id)
    if seat is None:
        raise EventError(
            "Этого человека нет в составе. Отметка не записана.",
            code="EVENTS.NOT_SEATED",
            retryable=True,
        )
    if seat.attendance == "excused":
        raise EventError(
            "Участник освобождён от участия. В статистику явки это не идёт.",
            code="EVENTS.EXCUSED",
        )
    if seat.attendance == result:
        return seat
    previous = seat.attendance
    updated = await set_seat_attendance(
        event.id,
        member_id,
        attendance=result,
        actor_id=_actor_id(actor),
        reason="итог командира",
    )
    if updated is None:
        raise EventError(
            "Состав изменился. Отметка не записана; повторите.",
            code="EVENTS.CONFLICT",
            retryable=True,
        )
    from services.contribution import award_from_attendance, reverse_from_attendance

    if result == "attended":
        await award_from_attendance(actor, guild_id, event.id, member_id)
    elif previous == "attended":
        await reverse_from_attendance(
            actor,
            guild_id,
            event.id,
            member_id,
            reason="командир снял отметку явки",
        )
    return updated


async def excuse_member(
    actor: Any,
    guild_id: int,
    event_id: int,
    member_id: int,
    reason: str,
) -> EventSeat:
    event = await show_event(actor, guild_id, event_id)
    await _require_manage(actor, event)
    text = reason.strip()
    if not text:
        raise EventError(
            "Укажите причину освобождения. Отметка не записана.",
            code="EVENTS.REASON",
            retryable=True,
        )
    seat = await get_seat(event.id, member_id)
    if seat is None:
        raise EventError(
            "Этого человека нет в составе. Освобождение не записано.",
            code="EVENTS.NOT_SEATED",
            retryable=True,
        )
    if seat.attendance == "excused":
        return seat
    updated = await set_seat_attendance(
        event.id,
        member_id,
        attendance="excused",
        actor_id=_actor_id(actor),
        reason=text,
        last_reason=text,
    )
    if updated is None:
        raise EventError(
            "Состав изменился. Освобождение не записано; повторите.",
            code="EVENTS.CONFLICT",
            retryable=True,
        )
    return updated


async def mark_voice_hint(
    actor: Any, guild_id: int, event_id: int, member_id: int
) -> EventSeat:
    event = await show_event(actor, guild_id, event_id)
    await _require_manage(actor, event)
    updated = await note_voice_seen(
        event.id, member_id, actor_id=_actor_id(actor)
    )
    if updated is None:
        raise EventError(
            "Этого человека нет в составе. Подсказка не записана.",
            code="EVENTS.NOT_SEATED",
            retryable=True,
        )
    return updated


async def reserve_queue(event: FamilyEvent) -> list[ReservePlace]:
    seats = await list_reserve_queue(event.id)
    return [
        ReservePlace(position=index, seat=seat, basis=RESERVE_ORDER_BASIS)
        for index, seat in enumerate(seats, start=1)
    ]


def _on_leave_during(events: list[Any], starts_at: str) -> bool:
    relevant = [item for item in events if item.created_at <= starts_at]
    if not relevant:
        return False
    return relevant[-1].event_type == "leave"


async def attendance_stats(actor: Any, guild_id: int, member_id: int) -> AttendanceStats:
    family, _viewer = await _family_member(actor, guild_id)
    member = await get_current_member(family.id, member_id)
    if member is None:
        raise EventError(
            "Участник не найден в текущем составе. Статистика не собрана.",
            code="EVENTS.NOT_A_MEMBER",
            retryable=True,
        )
    lifecycle = await list_lifecycle(member.id)
    rows = await list_member_event_history(family.id, member_id)
    obligated = 0
    attended = 0
    no_show = 0
    pending = 0
    cancelled_excluded = 0
    excused_excluded = 0
    leave_excluded = 0
    for event, seat in rows:
        if event.status == "cancelled":
            cancelled_excluded += 1
            continue
        if seat.attendance == "excused":
            excused_excluded += 1
            continue
        if member.status == "leave" or _on_leave_during(lifecycle, event.starts_at):
            leave_excluded += 1
            continue
        obligated += 1
        if seat.attendance == "attended":
            attended += 1
        elif seat.attendance == "no_show":
            no_show += 1
        else:
            pending += 1
    return AttendanceStats(
        obligated=obligated,
        attended=attended,
        no_show=no_show,
        pending=pending,
        cancelled_excluded=cancelled_excluded,
        excused_excluded=excused_excluded,
        leave_excluded=leave_excluded,
    )


async def replace_after_lock(
    actor: Any,
    guild_id: int,
    event_id: int,
    outgoing_id: int,
    incoming_id: int | None,
    reason: str,
) -> tuple[EventSeat, EventSeat]:
    event = await show_event(actor, guild_id, event_id)
    await _require_manage(actor, event)
    if not event.roster_locked:
        raise EventError(
            "До фиксации резерв поднимается сам. "
            f"Зафиксируйте состав через {cmd('event lock')}.",
            code="EVENTS.NOT_LOCKED",
            retryable=True,
        )
    text = reason.strip()
    if not text:
        raise EventError(
            "После фиксации замена только с причиной. Замена не записана.",
            code="EVENTS.REASON",
            retryable=True,
        )
    incoming = incoming_id
    if incoming is None:
        waiting = await list_reserve_queue(event.id)
        if not waiting:
            raise EventError(
                "Резерв пуст. Замена не записана.",
                code="EVENTS.RESERVE_EMPTY",
                retryable=True,
            )
        incoming = waiting[0].discord_id
    swapped = await replace_main_with_reserve(
        event.id,
        outgoing_id,
        incoming,
        actor_id=_actor_id(actor),
        reason=text,
    )
    if swapped[0] is None or swapped[1] is None:
        raise EventError(
            "Замена не подошла: в основе должен быть уходящий, в резерве — входящий.",
            code="EVENTS.REPLACE",
            retryable=True,
        )
    await enqueue_operation(
        idempotency_key=f"event.replace:{event.id}:{outgoing_id}:{incoming}:{event.version}",
        guild_id=guild_id,
        kind="event.reserve_replace",
        aggregate_id=event.id,
        payload={
            "event_id": event.id,
            "title": event.title,
            "outgoing_id": outgoing_id,
            "incoming_id": incoming,
            "reason": text,
            "actor_id": _actor_id(actor),
        },
    )
    await record_decision(
        guild_id=guild_id,
        action="EVENTS.REPLACED",
        subject_type="family_event",
        subject_id=str(event.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        reason=text,
        source="command",
    )
    return swapped[0], swapped[1]


async def promote_reserve_leftover(actor: Any, guild_id: int, subject_id: str) -> str:
    del guild_id
    raw_event, _, departed = subject_id.partition(":")
    try:
        event_id = int(raw_event)
    except ValueError:
        return "Событие не найдено. Резерв не поднят."
    event = await get_event(event_id)
    if event is None:
        return "Событие не найдено. Резерв не поднят."
    if event.roster_locked:
        return (
            "Состав зафиксирован. Автоподъём не выполнен. "
            f"Замена — {cmd('event replace')} с причиной."
        )
    actor_id = _actor_id(actor) if actor is not None and getattr(actor, "id", None) else (
        event.commander_id
    )
    promoted: EventSeat | None = None
    if departed:
        try:
            departed_id = int(departed)
        except ValueError:
            departed_id = 0
        if departed_id:
            _removed, promoted = await release_seat(
                event.id,
                departed_id,
                actor_id=actor_id,
                auto_promote=True,
                reason="выход из Discord",
            )
    if promoted is None:
        main, _reserve = await count_slots(event.id)
        waiting = await list_reserve_queue(event.id)
        if main < event.capacity and waiting:
            promoted = await promote_into_main(
                event.id,
                waiting[0].discord_id,
                actor_id=actor_id,
                reason="подъём резерва",
            )
    if promoted is None:
        return "Поднимать некого: резерв пуст или основа занята."
    return f"Резерв поднят: <@{promoted.discord_id}> занял основу."


def event_card(event: FamilyEvent, *, schedule: Any | None = None) -> MessageSpec:
    snapshot = event.snapshot
    roles = snapshot.get("roles") or []
    role_text = ", ".join(
        f"{item.get('title')} ({item.get('quota') or 'без квоты'})"
        if isinstance(item, dict)
        else str(item)
        for item in roles
    ) or "не заданы"
    checklist = snapshot.get("checklist") or []
    check_lines = []
    for item in checklist:
        if isinstance(item, dict):
            kind = "запрещено" if item.get("kind") == "forbidden" else "нужно"
            check_lines.append(f"{kind}: {item.get('text')}")
    notes = " ".join(str(item) for item in (snapshot.get("notes") or ()))
    rule = snapshot.get("rule_version") or "не опубликовано"
    resource = event.resource_plan or "требует проверки"
    cal = schedule if schedule is not None else default_schedule()
    time_text = (
        f"{discord_timestamp(event.starts_at)} — {discord_timestamp(event.ends_at)}"
    )
    warning = conflict_note(cal, event.starts_at)
    outcome = snapshot.get("outcome")
    outcome_text = {
        "won": "победа",
        "lost": "поражение",
    }.get(str(outcome or ""), "ещё не зафиксирован")
    details = (
        ("Время", time_text),
        ("Командир", f"<@{event.commander_id}>"),
        ("Место сбора", event.meetup),
        ("Вместимость", f"{event.capacity} + резерв {event.reserve_capacity}"),
        ("Роли", role_text),
        ("Ресурсный план", resource),
        ("Правило", f"{snapshot.get('rule_activity') or 'нет'} · {rule}"),
        ("F2", event.f2_event_ref or "не привязано"),
        ("Снимок", f"вместимость {snapshot.get('capacity')}, шаблон {event.template_key}"),
        ("Чек-лист", "\n".join(check_lines) if check_lines else "нет"),
        ("Статус", event.status),
        (
            "Состав",
            "зафиксирован" if event.roster_locked else "открыт, резерв поднимается сам",
        ),
        ("Итог", outcome_text),
        (
            "Календарь",
            warning
            or "Конфликта 18:00/18:30 на этом старте нет.",
        ),
    )
    summary = notes or "Состав хранится числом в карточке, не числом кнопок."
    if warning:
        summary = f"{summary} {warning}"
    return notice(
        code="EVENTS.CARD",
        title=event.title,
        summary=summary,
        details=details,
        next_step=(
            f"Состав — {cmd('event roster')}. "
            f"Запись — {cmd('event join')}."
        ),
        reference=str(event.id),
        tone=MessageTone.NEUTRAL,
    )


def templates_spec(items: Sequence[EventTemplate]) -> MessageSpec:
    if not items:
        return notice(
            code="EVENTS.TEMPLATES_EMPTY",
            title="Шаблоны событий",
            summary="Нет шаблонов с открытыми улучшениями профиля.",
            next_step="Старший состав отмечает улучшения в профиле. Игровые числа не выдумываются.",
        )
    details = tuple(
        (
            item.key,
            f"{item.title}"
            + (
                f" · до {item.capacity}"
                if item.capacity is not None
                else " · вместимость задаёте вы"
            ),
        )
        for item in items
    )
    return notice(
        code="EVENTS.TEMPLATES",
        title="Доступные шаблоны",
        summary="Показаны только шаблоны с открытым улучшением. ВЗА требует ещё матовозку.",
        details=details,
        next_step=f"Создать — {cmd('event create')}.",
    )


class EventPanelSource:
    async def list_items(self, guild_id: int, member: Any) -> tuple[PanelItem, ...]:
        family = await ensure_family(guild_id)
        rows = await list_member_events(family.id, member.discord_id)
        return tuple(
            PanelItem(
                event.title,
                f"{seat.slot} · {seat.role_key or 'без роли'} · {event.starts_at}",
                item_id=str(event.id),
            )
            for event, seat in rows
        )


class EventObligationSource:
    async def list_open(self, guild_id: int, discord_id: int) -> list[OpenObligation]:
        family = await ensure_family(guild_id)
        rows = await list_member_events(family.id, discord_id)
        items: list[OpenObligation] = []
        for event, seat in rows:
            action = None
            subject = str(event.id)
            _main, reserve = await count_slots(event.id)
            if (
                seat.slot == "main"
                and not event.roster_locked
                and event.status == "scheduled"
                and reserve > 0
            ):
                action = "promote_reserve"
                subject = f"{event.id}:{discord_id}"
            items.append(
                OpenObligation(
                    kind="event",
                    subject_id=subject,
                    summary=(
                        f"{event.title}: {ATTENDANCE_LABELS.get(seat.attendance, seat.slot)}"
                        f"{' · ' + seat.role_key if seat.role_key else ''}"
                    ),
                    outstanding=seat.attendance in {"joined", "ready", "reserve"},
                    action=action,
                    reference=str(event.id),
                )
            )
        return items


def ensure_event_integrations() -> None:
    register_panel_source(SECTION_EVENTS, EventPanelSource())
    ensure_obligation_source(EventObligationSource())
    register_leftover_action("promote_reserve", promote_reserve_leftover)


ensure_event_integrations()
