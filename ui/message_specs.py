from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum, StrEnum

import discord

from .command_registry import code
from .design import (
    Accent,
    Status,
    debug_validate_layout,
    divider,
    heading,
    key_values,
    status_badge,
    text_budget,
    validate_layout,
)
from .layouts import ModernLayoutView, _TextBudget


class MessageTone(Enum):
    NEUTRAL = ("", 0x2B2D31)
    INFO = ("ℹ️", 0x3498DB)
    SUCCESS = ("✅", 0x2ECC71)
    WARNING = ("⚠️", 0xF1C40F)
    ERROR = ("❌", 0xE74C3C)

    @property
    def icon(self) -> str:
        return self.value[0]

    @property
    def color(self) -> int:
        return self.value[1]

    @property
    def accent(self) -> Accent:
        return _TONE_ACCENT[self]


class Outcome(Enum):
    NEUTRAL = "neutral"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class MessageKind(StrEnum):
    GENERIC = "generic"
    PUBLIC_PANEL = "public_panel"
    STAFF_WORK_ITEM = "staff_work_item"
    ADMIN_RECEIPT = "admin_receipt"
    SYSTEM_ERROR = "system_error"
    USER_RESULT = "user_result"


_TONE_ACCENT: dict[MessageTone, Accent] = {
    MessageTone.NEUTRAL: Accent.SERVICE,
    MessageTone.INFO: Accent.MEMBER,
    MessageTone.SUCCESS: Accent.SUCCESS,
    MessageTone.WARNING: Accent.WARNING,
    MessageTone.ERROR: Accent.ERROR,
}

_STATUS_HINTS: tuple[tuple[Status, tuple[str, ...]], ...] = (
    (Status.OVERDUE, ("просроч", "overdue")),
    (Status.CANCELLED, ("отмен", "cancel")),
    (Status.REJECTED, ("отклон", "reject", "denied")),
    (Status.ACCEPTED, ("принят", "одобр", "accept", "approved")),
    (Status.REWORK, ("доработ", "rework")),
    (Status.IN_PROGRESS, ("в работе", "in progress", "in_progress")),
    (Status.WAITING, ("ожида", "waiting", "очеред")),
    (Status.DRAFT, ("черновик", "draft")),
)


@dataclass(frozen=True, slots=True)
class MessageSpec:
    code: str
    title: str
    summary: str | None = None
    details: tuple[tuple[str, str], ...] = ()
    next_step: str | None = None
    tone: MessageTone = MessageTone.NEUTRAL
    reference: str | None = None
    kind: MessageKind = MessageKind.GENERIC
    status: str | None = None
    retryable: bool | None = None
    actor: str | None = None
    changed: str | None = None
    before: str | None = None
    after: str | None = None
    reason: str | None = None
    number: str | None = None
    author: str | None = None
    category: str | None = None
    deadline: str | None = None
    history: str | None = None
    assignee: str | None = None
    accent: Accent | int | None = None
    source_label: str | None = None
    source_fetched_at: str | None = None
    source_stale: bool = False
    source_attribution: str | None = None

    def to_layout(self) -> ModernLayoutView:
        """Assemble a native Components V2 view from design primitives."""

        container = _container_for(self)
        timeout = None if self.kind is MessageKind.PUBLIC_PANEL else 180.0
        layout = ModernLayoutView(timeout=timeout)
        layout.add_item(container)
        validate_layout(layout)
        return layout


def resolve_status(value: str) -> Status | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return Status(cleaned)
    except ValueError:
        pass
    folded = cleaned.casefold()
    for status, hints in _STATUS_HINTS:
        if any(hint in folded for hint in hints):
            return status
    return None


_MSK = timezone(timedelta(hours=3))


def format_fetched_clock(fetched_at: str | None) -> str | None:
    if not fetched_at:
        return None
    try:
        parsed = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(_MSK)
    return f"{local.hour:02d}:{local.minute:02d}"


def format_source_attribution(
    *,
    attribution: str | None = None,
    source_label: str | None = None,
    fetched_at: str | None = None,
    stale: bool = False,
) -> str | None:
    label = (attribution or "").strip()
    if not label:
        named = (source_label or "").strip()
        label = f"Источник: {named}" if named else ""
    if not label:
        return None
    clock = format_fetched_clock(fetched_at)
    if clock and stale:
        return f"{label} • данные от {clock}, обновление не удалось"
    if clock:
        return f"{label} • данные от {clock}"
    return label


def _footnote(spec: MessageSpec, *, include_reference: bool) -> str:
    parts = ["Guinness Family", spec.code]
    if include_reference and spec.reference:
        parts.append(spec.reference)
    attribution = format_source_attribution(
        attribution=spec.source_attribution,
        source_label=spec.source_label,
        fetched_at=spec.source_fetched_at,
        stale=spec.source_stale,
    )
    if attribution:
        parts.append(attribution)
    return " • ".join(parts)


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _pairs(*items: tuple[str, str | None]) -> list[tuple[str, str]]:
    return [(name, value) for name, value in items if value and value.strip()]


def _container_for(spec: MessageSpec) -> discord.ui.Container:
    builders = {
        MessageKind.PUBLIC_PANEL: _public_panel_container,
        MessageKind.STAFF_WORK_ITEM: _staff_work_item_container,
        MessageKind.ADMIN_RECEIPT: _admin_receipt_container,
        MessageKind.SYSTEM_ERROR: _system_error_container,
        MessageKind.USER_RESULT: _user_result_container,
    }
    builder = builders.get(spec.kind, _generic_container)
    return builder(spec)


def _spec_accent(spec: MessageSpec, default: Accent) -> Accent | int:
    if spec.accent is not None:
        return spec.accent
    return default


def _layout_card(
    *,
    title: str,
    footnote: str,
    summary: str | None = None,
) -> tuple[_TextBudget, discord.ui.TextDisplay, str, str]:
    """Reserve heading and footnote, then return leftover budget for optional blocks."""

    allocator = text_budget()
    heading_item = heading(title, 2, budget=allocator)
    note = allocator.reserve(f"-# {footnote}") if footnote.strip() else ""
    body = allocator.take(summary) if summary and summary.strip() else ""
    return allocator, heading_item, body, note


def _finish_card(
    *,
    accent: Accent | int,
    heading_item: discord.ui.TextDisplay,
    body: str,
    note: str,
    stats: list[discord.ui.Item[discord.ui.LayoutView]],
    details: list[discord.ui.Item[discord.ui.LayoutView]],
    actions: list[discord.ui.Item[discord.ui.LayoutView]],
) -> discord.ui.Container:
    children: list[discord.ui.Item[discord.ui.LayoutView]] = [heading_item]
    if body:
        children.append(discord.ui.TextDisplay(body))
    children.extend(stats)
    details = [
        item
        for item in details
        if not isinstance(item, discord.ui.TextDisplay) or item.content
    ]
    actions = [
        item
        for item in actions
        if not isinstance(item, discord.ui.TextDisplay) or item.content
    ]
    if details:
        children.append(divider())
        children.extend(details)
    if actions:
        children.append(divider())
        children.extend(actions)
    if note:
        children.append(discord.ui.TextDisplay(note))
    container = discord.ui.Container(*children, accent_color=int(accent))
    debug_validate_layout(container)
    return container


def _public_panel_container(spec: MessageSpec) -> discord.ui.Container:
    budget, heading_item, body, note = _layout_card(
        title=spec.title,
        summary=spec.summary,
        footnote=_footnote(spec, include_reference=False),
    )
    details: list[discord.ui.Item[discord.ui.LayoutView]] = []
    actions: list[discord.ui.Item[discord.ui.LayoutView]] = []
    if spec.details:
        details.append(key_values(spec.details, budget=budget))
    next_step = _optional_text(spec.next_step)
    if next_step:
        action = budget.take(f"**Что дальше:** {next_step}")
        if action:
            actions.append(discord.ui.TextDisplay(action))
    return _finish_card(
        accent=_spec_accent(spec, Accent.SERVICE),
        heading_item=heading_item,
        body=body,
        note=note,
        stats=[],
        details=details,
        actions=actions,
    )


def _staff_work_item_container(spec: MessageSpec) -> discord.ui.Container:
    budget, heading_item, body, note = _layout_card(
        title=spec.title,
        footnote=_footnote(spec, include_reference=True),
    )
    stats: list[discord.ui.Item[discord.ui.LayoutView]] = []
    number = _optional_text(spec.number)
    if number:
        stats.append(heading(number, 3, budget=budget))
    status_text = _optional_text(spec.status)
    if status_text:
        resolved = resolve_status(status_text)
        if resolved is not None:
            stats.append(status_badge(resolved, budget=budget))
        else:
            stats.append(key_values((("Статус", status_text),), budget=budget))

    details: list[discord.ui.Item[discord.ui.LayoutView]] = []
    identity = _pairs(("Заявитель", spec.author), ("Категория", spec.category))
    if identity:
        details.append(key_values(identity, columns=2, budget=budget))
    gist = _optional_text(spec.summary)
    if gist:
        taken = budget.take(gist)
        if taken:
            details.append(discord.ui.TextDisplay(taken))
    extra = _pairs(("Ответственный", spec.assignee), ("Срок", spec.deadline))
    if extra:
        details.append(key_values(extra, budget=budget))
    history = _optional_text(spec.history)
    if history:
        collapsed = budget.take(f"-# История: {history}")
        if collapsed:
            details.append(discord.ui.TextDisplay(collapsed))

    actions: list[discord.ui.Item[discord.ui.LayoutView]] = []
    next_step = _optional_text(spec.next_step)
    if next_step:
        taken = budget.take(f"**Решение:** {next_step}")
        if taken:
            actions.append(discord.ui.TextDisplay(taken))

    return _finish_card(
        accent=_spec_accent(spec, Accent.STAFF),
        heading_item=heading_item,
        body=body,
        note=note,
        stats=stats,
        details=details,
        actions=actions,
    )


def _admin_receipt_container(spec: MessageSpec) -> discord.ui.Container:
    budget, heading_item, body, note = _layout_card(
        title=spec.title,
        footnote=_footnote(spec, include_reference=True),
    )
    details: list[discord.ui.Item[discord.ui.LayoutView]] = []
    changed = _optional_text(spec.changed)
    if changed:
        details.append(key_values((("Изменено", changed),), budget=budget))
    if spec.before is not None or spec.after is not None:
        details.append(heading("Было → стало", 3, budget=budget))
        details.append(
            key_values(
                (
                    ("Было", spec.before if spec.before is not None else "—"),
                    ("Стало", spec.after if spec.after is not None else "—"),
                ),
                budget=budget,
            )
        )
    meta = _pairs(("Кем", spec.actor), ("Основание", spec.reason))
    if meta:
        details.append(key_values(meta, budget=budget))

    actions: list[discord.ui.Item[discord.ui.LayoutView]] = []
    if spec.reference:
        link = budget.take(
            f"Запись журнала решений: {code('audit show')} • `{spec.reference}`"
        )
        if link:
            actions.append(discord.ui.TextDisplay(link))

    return _finish_card(
        accent=_spec_accent(spec, spec.tone.accent),
        heading_item=heading_item,
        body=body,
        note=note,
        stats=[],
        details=details,
        actions=actions,
    )


def _system_error_container(spec: MessageSpec) -> discord.ui.Container:
    budget, heading_item, body, note = _layout_card(
        title=spec.title,
        summary=spec.summary,
        footnote=_footnote(spec, include_reference=True),
    )
    details: list[discord.ui.Item[discord.ui.LayoutView]] = []
    if spec.retryable is True:
        retry = "Повторять безопасно. Изменения не сохранены."
    elif spec.retryable is False:
        retry = "Не повторяйте это действие. Сообщите владельцу код и reference."
    else:
        retry = None
    if retry:
        details.append(key_values((("Повтор", retry),), budget=budget))

    actions: list[discord.ui.Item[discord.ui.LayoutView]] = []
    next_step = _optional_text(spec.next_step)
    if next_step:
        taken = budget.take(f"**Что дальше:** {next_step}")
        if taken:
            actions.append(discord.ui.TextDisplay(taken))

    return _finish_card(
        accent=_spec_accent(spec, Accent.ERROR),
        heading_item=heading_item,
        body=body,
        note=note,
        stats=[],
        details=details,
        actions=actions,
    )


def _user_result_container(spec: MessageSpec) -> discord.ui.Container:
    budget, heading_item, body, note = _layout_card(
        title=spec.title,
        summary=spec.summary,
        footnote=_footnote(spec, include_reference=True),
    )
    details: list[discord.ui.Item[discord.ui.LayoutView]] = []
    if spec.details:
        details.append(key_values(spec.details, budget=budget))
    actions: list[discord.ui.Item[discord.ui.LayoutView]] = []
    next_step = _optional_text(spec.next_step)
    if next_step:
        action = budget.take(f"**Что дальше:** {next_step}")
        if action:
            actions.append(discord.ui.TextDisplay(action))
    return _finish_card(
        accent=_spec_accent(spec, spec.tone.accent),
        heading_item=heading_item,
        body=body,
        note=note,
        stats=[],
        details=details,
        actions=actions,
    )


def _generic_container(spec: MessageSpec) -> discord.ui.Container:
    budget, heading_item, body, note = _layout_card(
        title=spec.title,
        summary=spec.summary,
        footnote=_footnote(spec, include_reference=True),
    )
    details: list[discord.ui.Item[discord.ui.LayoutView]] = []
    if spec.details:
        details.append(key_values(spec.details, budget=budget))
    actions: list[discord.ui.Item[discord.ui.LayoutView]] = []
    next_step = _optional_text(spec.next_step)
    if next_step:
        action = budget.take(f"**Что дальше:** {next_step}")
        if action:
            actions.append(discord.ui.TextDisplay(action))
    return _finish_card(
        accent=_spec_accent(spec, spec.tone.accent),
        heading_item=heading_item,
        body=body,
        note=note,
        stats=[],
        details=details,
        actions=actions,
    )
