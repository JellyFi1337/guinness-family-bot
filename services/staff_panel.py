"""Senior-staff hub: six live sections, no second copy of domain data."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.permissions import Permission, evaluate_access
from repositories.accounts import list_accounts, list_obligations
from repositories.events import list_scheduled_events
from repositories.members import list_current_roster
from repositories.progression import ensure_development, list_upgrades
from services.inventory import list_alert_items
from services.membership import ensure_family
from services.queue import collect_items, ensure_queue_integrations
from ui.command_registry import cmd

SECTION_QUEUE = "queue"
SECTION_ROSTER = "roster"
SECTION_EVENTS = "events"
SECTION_PAYOUTS = "payouts"
SECTION_SUPPLY = "supply"
SECTION_PROGRESS = "progress"

SECTION_LABELS: dict[str, str] = {
    SECTION_QUEUE: "Требует решения",
    SECTION_ROSTER: "Состав",
    SECTION_EVENTS: "План событий",
    SECTION_PAYOUTS: "Выплаты",
    SECTION_SUPPLY: "Снабжение",
    SECTION_PROGRESS: "Развитие",
}
SECTION_PERMISSION: dict[str, Permission] = {
    SECTION_QUEUE: Permission.QUEUE_ASSIGN,
    SECTION_ROSTER: Permission.MEMBERS_VIEW,
    SECTION_EVENTS: Permission.EVENTS_CREATE,
    SECTION_PAYOUTS: Permission.TREASURY_VIEW,
    SECTION_SUPPLY: Permission.INVENTORY_MANAGE,
    SECTION_PROGRESS: Permission.PROGRESSION_MANAGE,
}
SECTION_COMMAND: dict[str, str] = {
    SECTION_QUEUE: "queue list",
    SECTION_ROSTER: "members list",
    SECTION_EVENTS: "event list",
    SECTION_PAYOUTS: "treasury accounts",
    SECTION_SUPPLY: "stock alerts",
    SECTION_PROGRESS: "progress tree",
}
SECTION_ORDER = (
    SECTION_QUEUE,
    SECTION_ROSTER,
    SECTION_EVENTS,
    SECTION_PAYOUTS,
    SECTION_SUPPLY,
    SECTION_PROGRESS,
)


@dataclass(frozen=True, slots=True)
class StaffSection:
    key: str
    label: str
    lines: tuple[str, ...]
    next_step: str
    allowed: bool


@dataclass(frozen=True, slots=True)
class StaffPanel:
    sections: tuple[StaffSection, ...]

    @property
    def visible_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.sections if item.allowed)

    def section(self, key: str) -> StaffSection:
        for item in self.sections:
            if item.key == key:
                return item
        raise KeyError(key)


async def visible_staff_keys(actor: object) -> tuple[str, ...]:
    keys: list[str] = []
    for key in SECTION_ORDER:
        decision = await evaluate_access(actor, SECTION_PERMISSION[key])
        if decision.allowed:
            keys.append(key)
    return tuple(keys)


async def staff_section_lines(
    actor: object, guild_id: int, key: str
) -> tuple[str, ...]:
    decision = await evaluate_access(actor, SECTION_PERMISSION[key])
    next_cmd = cmd(SECTION_COMMAND[key])
    if not decision.allowed:
        return (
            decision.summary,
            f"Раздел откроется, когда выдадут полномочие. Команда: {next_cmd}.",
        )
    family = await ensure_family(guild_id)
    if key == SECTION_QUEUE:
        ensure_queue_integrations()
        items = await collect_items(guild_id, family.id)
        if not items:
            return ("Нет задач, которые ждут решения.", f"Обновить — {next_cmd}.")
        lines = [
            f"{item.ref} · {item.title} · "
            + ("свободно" if item.assignee_id is None else f"<@{item.assignee_id}>")
            for item in items[:12]
        ]
        if len(items) > 12:
            lines.append(f"Ещё {len(items) - 12}. Полный список — {next_cmd}.")
        return tuple(lines)
    if key == SECTION_ROSTER:
        roster = await list_current_roster(family.id)
        if not roster:
            return ("В текущем составе никого нет.", f"Реестр — {next_cmd}.")
        return (
            f"В составе {len(roster)} человек.",
            f"Карточка участника и фильтры — {next_cmd}.",
        )
    if key == SECTION_EVENTS:
        events = await list_scheduled_events(family.id)
        if not events:
            return (
                "Запланированных событий нет.",
                f"Календарь и создание — {next_cmd}, {cmd('event create')}.",
            )
        lines = [f"{event.title} · {event.starts_at}" for event in events[:12]]
        if len(events) > 12:
            lines.append(f"Ещё {len(events) - 12}. Полный план — {next_cmd}.")
        return tuple(lines)
    if key == SECTION_PAYOUTS:
        open_pay = [
            row
            for row in await list_obligations(family.id)
            if row.status in {"promised", "approved"}
        ]
        accounts = [
            row
            for row in await list_accounts(family.id, include_archived=False)
            if row.context == "memphis"
        ]
        if not open_pay and not accounts:
            return ("Открытых выплат нет.", f"Счета — {next_cmd}.")
        lines = [f"Счетов Memphis: {len(accounts)}."]
        if open_pay:
            lines.append(f"Обязательств к выплате: {len(open_pay)}.")
            lines.extend(
                f"#{row.id} · {row.status} · <@{row.payee_id}>"
                for row in open_pay[:8]
            )
        lines.append(f"Касса — {next_cmd}.")
        return tuple(lines)
    if key == SECTION_SUPPLY:
        alerts = await list_alert_items(guild_id, family.id)
        if not alerts:
            return ("Открытых оповещений склада нет.", f"Сверка — {cmd('stock reconcile')}.")
        lines = [item.title for item in alerts[:12]]
        lines.append(f"Оповещения — {next_cmd}.")
        return tuple(lines)
    await ensure_development(guild_id, family.id)
    nodes = await list_upgrades(family.id)
    if not nodes:
        return (
            "Старший состав ещё не добавил узлы дерева.",
            f"Дерево — {next_cmd}. Игровые числа не выдумываются.",
        )
    lines = [f"{node.title} ({node.track})" for node in nodes[:12]]
    lines.append(f"Карточка развития — {next_cmd}.")
    return tuple(lines)


async def build_staff_panel(actor: object, guild_id: int) -> StaffPanel:
    sections: list[StaffSection] = []
    for key in SECTION_ORDER:
        decision = await evaluate_access(actor, SECTION_PERMISSION[key])
        lines = await staff_section_lines(actor, guild_id, key)
        sections.append(
            StaffSection(
                key=key,
                label=SECTION_LABELS[key],
                lines=lines,
                next_step=f"Подробнее — {cmd(SECTION_COMMAND[key])}.",
                allowed=decision.allowed,
            )
        )
    return StaffPanel(sections=tuple(sections))


def staff_tabs(keys: Sequence[str]):
    from ui.navigation import Tab

    return tuple(Tab(key, SECTION_LABELS[key]) for key in keys if key in SECTION_LABELS)
