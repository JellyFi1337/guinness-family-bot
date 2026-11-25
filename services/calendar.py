"""Memphis schedule as profile data and preparatory event reminders.

Times come from TOML / live settings. Code parses them and does not invent
slots. In-game F2 start reminders (3h / 1h / 10min / 1min) are never copied.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.profile import DEFAULT_TIMEZONE, active_profile
from repositories.events import FamilyEvent, count_slots, list_member_events, list_roster
from repositories.members import get_current_member, list_clearances
from repositories.operations import (
    cancel_pending_operations,
    enqueue_operation,
    list_operations_for_aggregate,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.templates import notice

REMIND_KIND_PREFIX = "event.remind"
REMIND_KIND_PREP = "event.remind.prep"
REMIND_KIND_AFTERMATH = "event.remind.aftermath"
REMIND_KIND_WAREHOUSE = "event.remind.warehouse"
REMIND_KIND_DELIVERY = "event.remind.delivery"

FORBIDDEN_START_REMINDER_DELTAS = (
    timedelta(hours=3),
    timedelta(hours=1),
    timedelta(minutes=10),
    timedelta(minutes=1),
)

DAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_INDEX = {name: index for index, name in enumerate(DAY_ORDER)}
DAY_LABELS_RU: dict[str, str] = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}
DAY_ALIASES: dict[str, str] = {
    "mon": "mon",
    "monday": "mon",
    "пн": "mon",
    "tue": "tue",
    "tuesday": "tue",
    "вт": "tue",
    "wed": "wed",
    "wednesday": "wed",
    "ср": "wed",
    "thu": "thu",
    "thursday": "thu",
    "чт": "thu",
    "fri": "fri",
    "friday": "fri",
    "пт": "fri",
    "sat": "sat",
    "saturday": "sat",
    "сб": "sat",
    "sun": "sun",
    "sunday": "sun",
    "вс": "sun",
}

_RUNTIME_KEYS = {
    "schedule_org_days": "org_days",
    "schedule_rest_days": "rest_days",
    "schedule_air_cargo": "air_cargo_times",
    "schedule_dealers": "dealers_times",
    "schedule_workshops": "workshops_times",
    "schedule_influence_window": "influence_window",
    "schedule_hold_influence": "hold_influence_time",
    "schedule_lottery_days": "lottery_days",
    "schedule_lottery_time": "lottery_time",
    "schedule_auction_days": "auction_days",
    "schedule_auction_time": "auction_time",
    "schedule_warehouse_tag_hours": "warehouse_tag_hours",
}

_GUILD_FIELDS = frozenset(_RUNTIME_KEYS.values()) | frozenset({"timezone"})


class ScheduleConfigError(ValueError):
    """Invalid or incomplete Memphis schedule configuration."""


@dataclass(frozen=True, slots=True)
class ServerSchedule:
    timezone: str
    org_days: tuple[str, ...]
    rest_days: tuple[str, ...]
    air_cargo_times: tuple[str, ...]
    dealers_times: tuple[str, ...]
    workshops_times: tuple[str, ...]
    influence_window: tuple[str, str] | None
    hold_influence_time: str | None
    lottery_days: tuple[str, ...]
    lottery_time: str | None
    auction_days: tuple[str, ...]
    auction_time: str | None
    warehouse_tag_hours: int | None

    @property
    def configured(self) -> bool:
        return bool(self.org_days and self.rest_days)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class PrepGap:
    kind: str
    text: str


def parse_day_keys(value: Any) -> tuple[str, ...]:
    items = _split_csv(value)
    days: list[str] = []
    for item in items:
        key = DAY_ALIASES.get(item.casefold())
        if key is None:
            raise ValueError(
                f"Неизвестный день расписания {item!r}. "
                "Допустимы mon…sun или Пн…Вс."
            )
        if key not in days:
            days.append(key)
    return tuple(days)


def parse_clock_times(value: Any) -> tuple[str, ...]:
    items = _split_csv(value)
    times: list[str] = []
    for item in items:
        normalized = _parse_hhmm(item)
        if normalized not in times:
            times.append(normalized)
    return tuple(times)


def parse_clock_window(value: Any) -> tuple[str, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    separator = "-" if "-" in text else ("–" if "–" in text else None)
    if separator is None:
        raise ValueError("Окно времени пишется как ЧЧ:ММ-ЧЧ:ММ.")
    left, right = text.split(separator, 1)
    start = _parse_hhmm(left)
    end = _parse_hhmm(right)
    return start, end


def parse_schedule(
    values: Mapping[str, Any], *, timezone: str | None = None
) -> ServerSchedule:
    zone_name = _validate_timezone(
        str(timezone or values.get("timezone") or DEFAULT_TIMEZONE).strip()
        or DEFAULT_TIMEZONE
    )
    org_days = parse_day_keys(values.get("org_days"))
    rest_days = parse_day_keys(values.get("rest_days"))
    overlap = sorted(set(org_days) & set(rest_days))
    if overlap:
        raise ScheduleConfigError(
            "Игровые и выходные дни организаций не должны пересекаться: "
            + ", ".join(overlap)
        )
    window_raw = values.get("influence_window")
    window = parse_clock_window(window_raw) if window_raw not in (None, "") else None
    hold_raw = values.get("hold_influence_time")
    hold = (
        parse_clock_times(hold_raw)[0]
        if hold_raw not in (None, "")
        else None
    )
    lottery_time_raw = values.get("lottery_time")
    lottery_time = (
        parse_clock_times(lottery_time_raw)[0]
        if lottery_time_raw not in (None, "")
        else None
    )
    auction_time_raw = values.get("auction_time")
    auction_time = (
        parse_clock_times(auction_time_raw)[0]
        if auction_time_raw not in (None, "")
        else None
    )
    hours = _parse_hours(values.get("warehouse_tag_hours"))
    return ServerSchedule(
        timezone=zone_name,
        org_days=org_days,
        rest_days=rest_days,
        air_cargo_times=parse_clock_times(values.get("air_cargo_times")),
        dealers_times=parse_clock_times(values.get("dealers_times")),
        workshops_times=parse_clock_times(values.get("workshops_times")),
        influence_window=window,
        hold_influence_time=hold,
        lottery_days=parse_day_keys(values.get("lottery_days")),
        lottery_time=lottery_time,
        auction_days=parse_day_keys(values.get("auction_days")),
        auction_time=auction_time,
        warehouse_tag_hours=hours,
    )


def default_schedule() -> ServerSchedule:
    from config import schedule_toml_values

    return parse_schedule(schedule_toml_values())


async def active_schedule(guild_id: int) -> ServerSchedule:
    from config import schedule_toml_values
    from repositories.settings import list_runtime_settings
    from repositories.static_messages import list_guild_settings

    merged: dict[str, Any] = dict(schedule_toml_values())
    raw_guild = await list_guild_settings(guild_id, prefix="schedule.")
    for key, value in raw_guild.items():
        field = key.removeprefix("schedule.")
        field = _RUNTIME_KEYS.get(f"schedule_{field}", field)
        if field in _GUILD_FIELDS:
            merged[field] = value
    runtime = await list_runtime_settings(guild_id)
    for key, value in runtime.items():
        mapped = _RUNTIME_KEYS.get(key)
        if mapped is not None:
            merged[mapped] = value
    profile = await active_profile(guild_id)
    return parse_schedule(merged, timezone=profile.timezone)


def localize(schedule: ServerSchedule, when: datetime | str) -> datetime:
    stamp = _as_datetime(when)
    return stamp.astimezone(schedule.zone)


def weekday_key(when: datetime) -> str:
    return DAY_ORDER[when.weekday()]


def is_org_day(schedule: ServerSchedule, when: datetime | str) -> bool:
    if not schedule.org_days:
        raise ScheduleConfigError(
            "Игровые дни организаций требуют настройки в расписании профиля."
        )
    return weekday_key(localize(schedule, when)) in schedule.org_days


def is_rest_day(schedule: ServerSchedule, when: datetime | str) -> bool:
    if not schedule.rest_days:
        raise ScheduleConfigError(
            "Выходные дни организаций требуют настройки в расписании профиля."
        )
    return weekday_key(localize(schedule, when)) in schedule.rest_days


def day_kind(schedule: ServerSchedule, when: datetime | str) -> str:
    if not schedule.configured:
        return "unknown"
    key = weekday_key(localize(schedule, when))
    if key in schedule.org_days:
        return "org"
    if key in schedule.rest_days:
        return "rest"
    return "unknown"


def prime_days(schedule: ServerSchedule | None = None) -> tuple[str, ...]:
    item = schedule if schedule is not None else default_schedule()
    return item.org_days


def prime_day_labels(schedule: ServerSchedule | None = None) -> dict[str, str]:
    return {day: DAY_LABELS_RU[day] for day in prime_days(schedule)}


def rest_activity_days(schedule: ServerSchedule | None = None) -> tuple[str, ...]:
    item = schedule if schedule is not None else default_schedule()
    return item.rest_days


def discord_timestamp(when: datetime | str, *, style: str = "F") -> str:
    stamp = _as_datetime(when)
    return f"<t:{int(stamp.timestamp())}:{style}>"


def evening_dealers_times(schedule: ServerSchedule) -> tuple[str, ...]:
    evening: list[str] = []
    for item in schedule.dealers_times:
        hour, _minute = _hhmm_parts(item)
        if hour >= 12:
            evening.append(item)
    return tuple(evening)


def slot_kind(schedule: ServerSchedule, when: datetime | str) -> str | None:
    local = localize(schedule, when)
    clock = local.strftime("%H:%M")
    day = weekday_key(local)
    if (
        schedule.lottery_time
        and clock == schedule.lottery_time
        and day in schedule.lottery_days
    ):
        return "lottery"
    if (
        schedule.auction_time
        and clock == schedule.auction_time
        and day in schedule.auction_days
    ):
        return "auction"
    if clock in evening_dealers_times(schedule):
        return "dealers_evening"
    if clock in schedule.dealers_times:
        return "dealers"
    return None


def events_conflict_1830(
    schedule: ServerSchedule,
    start_a: datetime | str,
    start_b: datetime | str,
) -> bool:
    local_a = localize(schedule, start_a)
    local_b = localize(schedule, start_b)
    if local_a.date() != local_b.date():
        return False
    kinds = {slot_kind(schedule, local_a), slot_kind(schedule, local_b)}
    container = {"lottery", "auction"}
    return bool(kinds & container) and "dealers_evening" in kinds


def conflict_note(schedule: ServerSchedule, when: datetime | str) -> str | None:
    kind = slot_kind(schedule, when)
    if kind in {"lottery", "auction"}:
        return (
            "Это время лотереи или аукциона контейнеров. "
            "Вечерняя вербовка дилеров в тот же день требует явного подтверждения "
            "записи в оба состава."
        )
    if kind == "dealers_evening":
        return (
            "Это время вечерней вербовки дилеров. "
            "Лотерея или аукцион контейнеров в тот же день требует явного "
            "подтверждения записи в оба состава."
        )
    return None


def warehouse_deadline(
    won_at: datetime | str, schedule: ServerSchedule
) -> datetime:
    if schedule.warehouse_tag_hours is None:
        raise ScheduleConfigError(
            "Срок метки склада требует настройки в расписании профиля."
        )
    stamp = _as_datetime(won_at).astimezone(schedule.zone)
    return stamp + timedelta(hours=schedule.warehouse_tag_hours)


def reminder_idempotency_key(event_id: int, phase: str, stamp: str) -> str:
    return f"event.remind:{event_id}:{phase}:{stamp}"


async def cancel_event_reminders(
    guild_id: int, event_id: int, *, reason: str
) -> int:
    return await cancel_pending_operations(
        guild_id=guild_id,
        aggregate_id=str(event_id),
        kind_prefix=REMIND_KIND_PREFIX,
        reason=reason,
    )


async def sync_event_reminders(event: FamilyEvent) -> None:
    """Queue preparatory and aftermath reminders. Never copies F2 start pings."""

    if event.status == "cancelled":
        await cancel_event_reminders(event.guild_id, event.id, reason="cancelled")
        return
    await _enqueue_reminder(
        event,
        kind=REMIND_KIND_PREP,
        phase="prep",
        stamp=event.starts_at,
        available_at=None,
    )
    await _enqueue_reminder(
        event,
        kind=REMIND_KIND_AFTERMATH,
        phase="aftermath",
        stamp=event.ends_at,
        available_at=event.ends_at,
    )


async def schedule_win_reminders(
    event: FamilyEvent, *, won_at: datetime | str
) -> None:
    schedule = await active_schedule(event.guild_id)
    won_stamp = _as_datetime(won_at).astimezone(UTC).isoformat()
    template = str(event.template_key)
    if template == "dealers":
        deadline = warehouse_deadline(won_at, schedule)
        await _enqueue_reminder(
            event,
            kind=REMIND_KIND_WAREHOUSE,
            phase="warehouse",
            stamp=won_stamp,
            available_at=None,
            extra={"deadline": deadline.astimezone(UTC).isoformat()},
        )
        return
    if template == "workshops":
        await _enqueue_reminder(
            event,
            kind=REMIND_KIND_DELIVERY,
            phase="delivery",
            stamp=won_stamp,
            available_at=None,
        )


async def list_event_reminders(guild_id: int, event_id: int):
    return await list_operations_for_aggregate(
        guild_id=guild_id,
        aggregate_id=str(event_id),
        kind_prefix=REMIND_KIND_PREFIX,
    )


async def evaluate_prep_gaps(event: FamilyEvent) -> tuple[PrepGap, ...]:
    gaps: list[PrepGap] = []
    main, _reserve = await count_slots(event.id)
    need = max(0, int(event.capacity) - main)
    if need:
        gaps.append(
            PrepGap("roster", f"В основе не хватает ещё {need} человек.")
        )
    seats = await list_roster(event.id)
    if event.commander_id <= 0:
        gaps.append(PrepGap("commander", "Командир не назначен."))
    elif not any(seat.discord_id == event.commander_id for seat in seats):
        gaps.append(PrepGap("commander", "Командир ещё не записан в состав."))
    from services.inventory import event_kit_gap_text

    kit_text = await event_kit_gap_text(event.guild_id, event.id)
    if kit_text:
        gaps.append(PrepGap("kit", kit_text))
    activity = event.snapshot.get("rule_activity")
    if isinstance(activity, str) and activity.strip():
        missing = 0
        for seat in seats:
            member = await get_current_member(event.family_id, seat.discord_id)
            if member is None:
                missing += 1
                continue
            clearances = await list_clearances(member.id)
            if not any(
                item.activity == activity and item.revoked_at is None
                for item in clearances
            ):
                missing += 1
        if missing:
            gaps.append(
                PrepGap(
                    "clearance",
                    f"У {missing} человек нет допуска «{activity}».",
                )
            )
    return tuple(gaps)


async def conflicting_schedule_events(
    family_id: int,
    discord_id: int,
    event: FamilyEvent,
    schedule: ServerSchedule,
) -> list[FamilyEvent]:
    others = await list_member_events(family_id, discord_id)
    conflicts: list[FamilyEvent] = []
    for other, _seat in others:
        if other.id == event.id:
            continue
        if events_conflict_1830(schedule, event.starts_at, other.starts_at):
            conflicts.append(other)
    return conflicts


async def reminder_message(event: FamilyEvent, payload: Mapping[str, Any]) -> MessageSpec:
    phase = str(payload.get("phase") or "")
    if phase == "prep":
        gaps = await evaluate_prep_gaps(event)
        summary = (
            "Подготовка к событию. Игра сама напомнит о старте; "
            "бот пишет только про состав и снаряжение."
        )
        details = tuple((item.kind, item.text) for item in gaps) or (
            ("Готовность", "Пробелов подготовки сейчас нет."),
        )
        return notice(
            code="EVENTS.REMIND_PREP",
            title=event.title,
            summary=summary,
            details=details,
            next_step=f"Состав — {cmd('event roster')}.",
            reference=str(event.id),
        )
    if phase == "aftermath":
        return notice(
            code="EVENTS.REMIND_AFTERMATH",
            title=event.title,
            summary=(
                "Событие закончилось. Сдайте отчёт и верните комплект."
            ),
            next_step=f"Карточка — {cmd('event show')}.",
            reference=str(event.id),
        )
    if phase == "warehouse":
        deadline_raw = payload.get("deadline")
        deadline_text = (
            discord_timestamp(str(deadline_raw))
            if deadline_raw
            else "срок метки склада требует настройки"
        )
        return notice(
            code="EVENTS.REMIND_WAREHOUSE",
            title=event.title,
            summary=(
                "После победы в вербовке дилеров метка склада активна до "
                f"{deadline_text}. Вывезите награду до истечения срока. "
                "Ответственный — командир события."
            ),
            next_step=f"Карточка — {cmd('event show')}.",
            reference=str(event.id),
        )
    if phase == "delivery":
        return notice(
            code="EVENTS.REMIND_DELIVERY",
            title=event.title,
            summary=(
                "После захвата цехов награда засчитывается только если ресурсы "
                "доставлены на свой склад организации."
            ),
            next_step=f"Карточка — {cmd('event show')}.",
            reference=str(event.id),
        )
    raise ValueError(f"unknown reminder phase {phase!r}")


async def calendar_day_spec(
    guild_id: int, when: datetime | str | None = None
) -> MessageSpec:
    schedule = await active_schedule(guild_id)
    if when is None:
        stamp = datetime.now(UTC)
    elif isinstance(when, str) and "T" not in when.strip() and " " not in when.strip():
        local_date = date.fromisoformat(when.strip())
        stamp = datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            12,
            0,
            tzinfo=schedule.zone,
        )
    else:
        stamp = localize(schedule, when)
    stamp = localize(schedule, stamp)
    kind = day_kind(schedule, stamp)
    if kind == "org":
        kind_label = "игровой день организаций (капты)"
    elif kind == "rest":
        kind_label = (
            "выходной день организаций (игровой день фракций): "
            "обучение, наставничество, инвентаризация и разборы"
        )
    else:
        kind_label = "день не задан в расписании профиля — требует настройки"
    local_date = stamp.date()
    details = [
        ("День", f"{DAY_LABELS_RU.get(weekday_key(stamp), weekday_key(stamp))} · {kind_label}"),
        ("Пояс", schedule.timezone),
        ("Прайм", _join_days(schedule.org_days) or "требует настройки"),
        ("Выходные организаций", _join_days(schedule.rest_days) or "требует настройки"),
        ("ВЗА", _times_as_stamps(schedule, local_date, schedule.air_cargo_times)),
        (
            "Вербовка дилеров",
            _times_as_stamps(schedule, local_date, schedule.dealers_times),
        ),
        (
            "Захват цехов",
            _times_as_stamps(schedule, local_date, schedule.workshops_times),
        ),
    ]
    if schedule.influence_window:
        details.append(
            (
                "Окно влияния",
                "–".join(schedule.influence_window),
            )
        )
    if schedule.hold_influence_time:
        details.append(
            (
                "Удержание квадратов",
                _times_as_stamps(
                    schedule, local_date, (schedule.hold_influence_time,)
                ),
            )
        )
    container = _container_label(schedule, stamp)
    if container:
        details.append(("Контейнеры", container))
    evening = evening_dealers_times(schedule)
    note = None
    if evening:
        note = conflict_note(
            schedule, _combine_local(schedule, local_date, evening[:1])
        )
    summary = (
        "Расписание Memphis из профиля. Бот не повторяет внутриигровые "
        "напоминания о старте."
    )
    if note:
        summary = f"{summary} {note}"
    return notice(
        code="EVENTS.CALENDAR",
        title="Календарь Memphis",
        summary=summary,
        details=tuple(details),
        next_step=f"Создать событие — {cmd('event create')}.",
        reference=local_date.isoformat(),
    )


def _container_label(schedule: ServerSchedule, local: datetime) -> str:
    day = weekday_key(local)
    if day in schedule.lottery_days and schedule.lottery_time:
        when = _combine_local(schedule, local.date(), (schedule.lottery_time,))
        return f"лотерея · {discord_timestamp(when)}"
    if day in schedule.auction_days and schedule.auction_time:
        when = _combine_local(schedule, local.date(), (schedule.auction_time,))
        return f"аукцион · {discord_timestamp(when)}"
    return ""


def _join_days(days: Sequence[str]) -> str:
    return ", ".join(DAY_LABELS_RU.get(day, day) for day in days)


def _times_as_stamps(
    schedule: ServerSchedule, local_date, times: Sequence[str]
) -> str:
    if not times:
        return "требует настройки"
    stamps = [
        discord_timestamp(_combine_local(schedule, local_date, (item,)))
        for item in times
    ]
    return ", ".join(stamps)


def _combine_local(schedule: ServerSchedule, local_date, times: Sequence[str]) -> datetime:
    clock = times[0]
    hour, minute = _hhmm_parts(clock)
    return datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        hour,
        minute,
        tzinfo=schedule.zone,
    )


async def _enqueue_reminder(
    event: FamilyEvent,
    *,
    kind: str,
    phase: str,
    stamp: str,
    available_at: str | datetime | None,
    extra: dict[str, Any] | None = None,
) -> int:
    if kind.startswith("event.remind.start"):
        raise RuntimeError("in-game start reminders must not be queued")
    payload: dict[str, Any] = {
        "event_id": event.id,
        "title": event.title,
        "phase": phase,
        "commander_id": event.commander_id,
    }
    if extra:
        payload.update(extra)
    return await enqueue_operation(
        idempotency_key=reminder_idempotency_key(event.id, phase, stamp),
        guild_id=event.guild_id,
        kind=kind,
        payload=payload,
        aggregate_id=str(event.id),
        available_at=available_at,
    )


def _as_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def _validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleConfigError(
            f"Неизвестный часовой пояс {name!r}. "
            "Укажите идентификатор IANA, например Europe/Moscow."
        ) from exc
    return name


def _split_csv(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item).strip() for item in value]
    else:
        items = [part.strip() for part in str(value).replace(";", ",").split(",")]
    return [item for item in items if item]


def _parse_hhmm(value: str) -> str:
    text = value.strip()
    hour, minute = _hhmm_parts(text)
    return f"{hour:02d}:{minute:02d}"


def _hhmm_parts(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("Время пишется как ЧЧ:ММ.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("Время пишется как ЧЧ:ММ.") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("Время пишется как ЧЧ:ММ.")
    return hour, minute


def _parse_hours(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ScheduleConfigError(
            "Срок метки склада должен быть положительным целым числом часов."
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleConfigError(
            "Срок метки склада должен быть положительным целым числом часов."
        ) from exc
    if parsed <= 0:
        raise ScheduleConfigError(
            "Срок метки склада должен быть положительным целым числом часов."
        )
    return parsed
