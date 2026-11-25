"""Personal member panel. Reads domain modules; does not keep a second copy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import discord

from repositories.members import Clearance, Member, get_current_member, get_member_profile
from services.membership import (
    MembershipError,
    OpenObligation,
    STATUS_LABELS,
    ensure_family,
    list_open_obligations,
)
from ui.command_registry import cmd
from ui.design import empty_state
from ui.message_specs import MessageSpec
from ui.templates import public_panel

SECTION_TASKS = "tasks"
SECTION_EVENTS = "events"
SECTION_REPORT = "report"
SECTION_RESOURCES = "resources"
SECTION_PROGRESS = "progress"
SECTION_HELP = "help"

SECTION_LABELS: dict[str, str] = {
    SECTION_TASKS: "Мои задачи",
    SECTION_EVENTS: "События",
    SECTION_REPORT: "Сдать отчёт",
    SECTION_RESOURCES: "Ресурсы",
    SECTION_PROGRESS: "Мой прогресс",
    SECTION_HELP: "Помощь",
}
# Tabs that stay hidden until the member has a matching clearance or live items.
SECTION_CLEARANCE: dict[str, str] = {
    SECTION_TASKS: "contracts",
    SECTION_EVENTS: "events",
    SECTION_REPORT: "reports",
}
ALWAYS_VISIBLE = (SECTION_RESOURCES, SECTION_PROGRESS, SECTION_HELP)
SECTION_ORDER = (
    SECTION_TASKS,
    SECTION_EVENTS,
    SECTION_REPORT,
    SECTION_RESOURCES,
    SECTION_PROGRESS,
    SECTION_HELP,
)
_TAB_CODE: dict[str, str] = {
    SECTION_TASKS: "t",
    SECTION_EVENTS: "e",
    SECTION_REPORT: "r",
    SECTION_RESOURCES: "s",
    SECTION_PROGRESS: "p",
    SECTION_HELP: "h",
}
_CODE_TAB: dict[str, str] = {code: key for key, code in _TAB_CODE.items()}


@dataclass(frozen=True, slots=True)
class PanelItem:
    title: str
    detail: str
    due: str | None = None
    needs_ready: bool = False
    item_id: str = ""


class PanelSource(Protocol):
    async def list_items(
        self, guild_id: int, member: Member
    ) -> Sequence[PanelItem]: ...


@dataclass(frozen=True, slots=True)
class PanelSection:
    key: str
    label: str
    available: bool
    empty: bool
    lines: tuple[str, ...]
    hint: str
    next_step: str
    ready_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemberPanel:
    member: Member
    sections: tuple[PanelSection, ...]

    @property
    def visible(self) -> tuple[PanelSection, ...]:
        return tuple(section for section in self.sections if section.available)

    @property
    def visible_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.visible)

    def object_id(self) -> str:
        return encode_visible_tabs(self.visible_keys)

    def section(self, key: str) -> PanelSection:
        for item in self.visible:
            if item.key == key:
                return item
        if not self.visible:
            raise MembershipError(
                "Нет разделов, которые можно показать. Панель не открыта.",
                code="ME.NO_SECTIONS",
            )
        return self.visible[0]


class _EmptySource:
    async def list_items(self, guild_id: int, member: Member) -> Sequence[PanelItem]:
        del guild_id, member
        return ()


class ResourceSource:
    async def list_items(self, guild_id: int, member: Member) -> Sequence[PanelItem]:
        rows = await list_open_obligations(guild_id, member.discord_id)
        items = [_obligation_item(row) for row in rows]
        from services.inventory import member_resource_lines

        for title, body in await member_resource_lines(guild_id, member.discord_id):
            items.append(PanelItem(title, body))
        return tuple(items)


class ProgressSource:
    async def list_items(self, guild_id: int, member: Member) -> Sequence[PanelItem]:
        profile = await get_member_profile(member.id)
        if profile is None:
            return ()
        rank = profile.rank.name if profile.rank else "не указана"
        lines = [
            PanelItem("Должность", rank),
            PanelItem("Статус", STATUS_LABELS.get(member.status, member.status)),
        ]
        if profile.clearances:
            for item in profile.clearances:
                until = "отозван" if item.revoked_at else (item.expires_at or "бессрочно")
                lines.append(PanelItem(f"Допуск {item.activity}", until))
        else:
            lines.append(
                PanelItem(
                    "Допуски",
                    "Пока нет. Их выдаёт уполномоченный после ознакомления с правилами.",
                )
            )
        from services.contribution import member_breakdown, panel_lines

        for title, body in panel_lines(
            await member_breakdown(member.guild_id, member.discord_id)
        ):
            lines.append(PanelItem(title, body))
        from services.progression import member_progress_items

        for title, body in await member_progress_items(guild_id, member.discord_id):
            lines.append(PanelItem(title, body))
        from services.bonus import member_progress_note

        note = await member_progress_note(member.guild_id, member.discord_id)
        lines.append(
            PanelItem(
                "Начисления и выплаты",
                note
                or "Когда лидер объявит премию периода, здесь появится ваша доля. "
                "Суммы не выдумываются.",
            )
        )
        lines.append(
            PanelItem(
                "Следующий шаг",
                "Критерии повышения публикует старший состав. "
                "Пока их нет — поле остаётся пустым.",
            )
        )
        return tuple(lines)


class HelpSource:
    async def list_items(self, guild_id: int, member: Member) -> Sequence[PanelItem]:
        del guild_id
        spec = member.direction or "не указана"
        return (
            PanelItem(
                "Поддержка",
                f"Личное обращение — кнопка на панели поддержки или {cmd('help overview')}. "
                "Инструкция к задаче живёт во вкладке «Мои задачи»: тема, сервер, "
                "источник, дата проверки, ответственный, версия.",
            ),
            PanelItem(
                "Специализация",
                f"{spec}. Справочник публикует старший состав; "
                "пока его нет, напишите в поддержку.",
            ),
        )


_SOURCES: dict[str, PanelSource] = {
    SECTION_TASKS: _EmptySource(),
    SECTION_EVENTS: _EmptySource(),
    SECTION_REPORT: _EmptySource(),
    SECTION_RESOURCES: ResourceSource(),
    SECTION_PROGRESS: ProgressSource(),
    SECTION_HELP: HelpSource(),
}
_READY: set[tuple[int, int, str]] = set()


def encode_visible_tabs(keys: Sequence[str]) -> str:
    codes = "".join(_TAB_CODE[key] for key in SECTION_ORDER if key in set(keys))
    return codes or "sph"


def decode_visible_tabs(object_id: str) -> tuple[str, ...]:
    keys = tuple(_CODE_TAB[char] for char in object_id if char in _CODE_TAB)
    return keys or ALWAYS_VISIBLE


def register_panel_source(key: str, source: PanelSource) -> None:
    if key not in SECTION_LABELS:
        raise ValueError(f"unknown panel section: {key}")
    _SOURCES[key] = source


def reset_panel_sources() -> None:
    _SOURCES[SECTION_TASKS] = _EmptySource()
    _SOURCES[SECTION_EVENTS] = _EmptySource()
    _SOURCES[SECTION_REPORT] = _EmptySource()
    _SOURCES[SECTION_RESOURCES] = ResourceSource()
    _SOURCES[SECTION_PROGRESS] = ProgressSource()
    _SOURCES[SECTION_HELP] = HelpSource()
    _READY.clear()


def _obligation_item(row: OpenObligation) -> PanelItem:
    extra = " · требует сверки" if row.needs_reconciliation else ""
    return PanelItem(row.kind, f"{row.summary}{extra}", item_id=row.subject_id)


def _active_clearances(items: Sequence[Clearance]) -> frozenset[str]:
    return frozenset(
        item.activity for item in items if item.revoked_at is None
    )


def _hints(key: str) -> tuple[str, str]:
    if key == SECTION_TASKS:
        return (
            "Сейчас нет взятых контрактов.",
            "Когда старший состав назначит задачу, она появится здесь.",
        )
    if key == SECTION_EVENTS:
        return (
            "Вас нет в основе или резерве ближайших событий.",
            "Запись на события откроется в календаре семьи.",
        )
    if key == SECTION_REPORT:
        return (
            "Нет активного запуска, по которому нужно сдать отчёт.",
            "Форма подставит известные данные, когда появится запуск контракта.",
        )
    if key == SECTION_RESOURCES:
        return (
            "За вами ничего не числится.",
            "Выданные комплекты и брони транспорта появятся в этом разделе.",
        )
    return ("Раздел пуст.", "Дополнительных действий не требуется.")


def _item_lines(items: Sequence[PanelItem]) -> tuple[str, ...]:
    lines: list[str] = []
    for item in items:
        due = f" · срок {item.due}" if item.due else ""
        ready = " · нужно подтвердить готовность" if item.needs_ready else ""
        lines.append(f"**{item.title}.** {item.detail}{due}{ready}")
    return tuple(lines)


def _section_available(
    key: str, *, items: Sequence[PanelItem], clearances: frozenset[str]
) -> bool:
    if key in ALWAYS_VISIBLE:
        return True
    needed = SECTION_CLEARANCE.get(key)
    if items:
        return True
    return bool(needed and needed in clearances)


async def build_member_panel(guild_id: int, discord_id: int) -> MemberPanel:
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, discord_id)
    if member is None:
        raise MembershipError(
            "Вас нет в текущем составе семьи. Личная панель не открыта; "
            "обратитесь к кадровику, чтобы вас завели в реестре.",
            code="ME.NOT_A_MEMBER",
        )
    profile = await get_member_profile(member.id)
    clearances = _active_clearances(profile.clearances if profile else ())
    sections: list[PanelSection] = []
    for key in SECTION_ORDER:
        source = _SOURCES[key]
        items = list(await source.list_items(guild_id, member))
        if key == SECTION_EVENTS:
            items = [
                PanelItem(
                    item.title,
                    item.detail,
                    due=item.due,
                    needs_ready=item.needs_ready
                    and (guild_id, discord_id, item.item_id) not in _READY,
                    item_id=item.item_id,
                )
                for item in items
            ]
        available = _section_available(key, items=items, clearances=clearances)
        hint, next_step = _hints(key)
        empty = available and not items
        ready_id = next(
            (item.item_id for item in items if item.needs_ready and item.item_id),
            None,
        )
        sections.append(
            PanelSection(
                key=key,
                label=SECTION_LABELS[key],
                available=available,
                empty=empty,
                lines=_item_lines(items),
                hint=hint,
                next_step=next_step,
                ready_item_id=ready_id,
            )
        )
    return MemberPanel(member=member, sections=tuple(sections))


def confirm_event_ready(guild_id: int, discord_id: int, event_id: str) -> None:
    _READY.add((guild_id, discord_id, event_id))


def public_me_spec() -> MessageSpec:
    """Public panel copy: no names, ranks, tasks or other personal fields."""

    return public_panel(
        code="PANEL.ME",
        title="Моя панель",
        purpose=(
            "Задачи, события, отчёты и прогресс открываются только вам. "
            "В этом сообщении нет чужих и ваших личных данных."
        ),
        rules=(
            (
                "Как открыть",
                f"Кнопка ниже или команда {cmd('me')}. Ответ придёт только вам.",
            ),
            (
                "Что внутри",
                "Только разделы, которые вам доступны. Пустой раздел подскажет, что делать.",
            ),
        ),
        action="Нажмите «Открыть мою панель».",
    )


def empty_section_container(section: PanelSection) -> discord.ui.Container:
    return empty_state(section.label, section.hint, section.next_step)


def public_copy_text() -> str:
    spec = public_me_spec()
    rules = " ".join(f"{title} {body}" for title, body in spec.details)
    return " ".join(
        part
        for part in (spec.title, spec.summary, rules, spec.next_step)
        if part
    )


def panel_contains_personal_leak(text: str, member: Member) -> bool:
    """Public copy must not mention this member's identity."""

    haystack = text.casefold()
    if member.ic_name.casefold() in haystack:
        return True
    return str(member.discord_id) in text
