"""Native Components V2 primitives. No business logic.

Accent colours are tokens: the same meaning always uses the same colour.
Length limits go through ``ui.layouts._TextBudget`` — this module does not
reimplement the allocator.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import IntEnum, StrEnum
from typing import Final
from urllib.parse import urlparse

import discord

from .layouts import MAX_TEXT_DISPLAY_LENGTH, _TextBudget


MAX_COMPONENTS: Final[int] = 40
MAX_TEXT_CHARS: Final[int] = MAX_TEXT_DISPLAY_LENGTH
MAX_ACTION_ROW_BUTTONS: Final[int] = 5
MAX_MEDIA_GALLERY_ITEMS: Final[int] = 10
MAX_SECTION_TEXT_DISPLAYS: Final[int] = 3
DEFAULT_PROGRESS_WIDTH: Final[int] = 12

_HEADING_MARKERS: Final[dict[int, str]] = {2: "##", 3: "###"}
_COLUMN_JOIN: Final[str] = "　•　"
_BAR_FILLED: Final[str] = "█"
_BAR_EMPTY: Final[str] = "░"
_ALLOWED_MEDIA_HOST_SUFFIXES: Final[tuple[str, ...]] = (
    "cdn.discordapp.com",
    "media.discordapp.net",
    "discordapp.net",
)


class Accent(IntEnum):
    """Domain accent colours. One token per meaning."""

    MEMBER = 0x5865F2
    STAFF = 0x9B59B6
    TREASURY = 0xC9A227
    EVENTS = 0x3498DB
    LOGISTICS = 0x1ABC9C
    PROGRESSION = 0xE67E22
    SERVICE = 0x2B2D31
    SUCCESS = 0x57F287
    WARNING = 0xF1C40F
    ERROR = 0xED4245


class Status(StrEnum):
    DRAFT = "draft"
    WAITING = "waiting"
    IN_PROGRESS = "in_progress"
    REWORK = "rework"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    OVERDUE = "overdue"


class LayoutSlot(StrEnum):
    HEADING = "heading"
    SUMMARY = "summary"
    STATS = "stats"
    DIVIDER = "divider"
    DETAILS = "details"
    ACTIONS = "actions"
    FOOTNOTE = "footnote"


# heading → summary → stats → divider → details → divider → actions → footnote
LAYOUT_SEQUENCE: Final[tuple[LayoutSlot, ...]] = (
    LayoutSlot.HEADING,
    LayoutSlot.SUMMARY,
    LayoutSlot.STATS,
    LayoutSlot.DIVIDER,
    LayoutSlot.DETAILS,
    LayoutSlot.DIVIDER,
    LayoutSlot.ACTIONS,
    LayoutSlot.FOOTNOTE,
)

STATUS_BADGES: Final[dict[Status, tuple[str, str]]] = {
    Status.DRAFT: ("○", "Черновик"),
    Status.WAITING: ("◷", "Ожидает"),
    Status.IN_PROGRESS: ("◐", "В работе"),
    Status.REWORK: ("✎", "На доработке"),
    Status.ACCEPTED: ("✓", "Принято"),
    Status.REJECTED: ("✕", "Отклонено"),
    Status.CANCELLED: ("—", "Отменено"),
    Status.OVERDUE: ("!", "Просрочено"),
}


class LayoutLimitError(ValueError):
    """A Components V2 layout exceeds a Discord hard limit from section 0.6."""


def text_budget(limit: int = MAX_TEXT_CHARS) -> _TextBudget:
    """Return the shared character allocator from ``ui.layouts``."""

    return _TextBudget(limit)


def heading(
    text: str,
    level: int = 2,
    *,
    budget: _TextBudget | None = None,
) -> discord.ui.TextDisplay:
    marker = _HEADING_MARKERS.get(level)
    if marker is None:
        raise ValueError("Заголовок может быть только уровня 2 (##) или 3 (###).")
    cleaned = text.strip().lstrip("#").strip()
    if not cleaned:
        raise ValueError("Заголовок не может быть пустым.")
    allocator = budget if budget is not None else text_budget()
    return discord.ui.TextDisplay(allocator.reserve(f"{marker} {cleaned}"))


def key_values(
    pairs: Sequence[tuple[str, str]],
    columns: int = 1,
    *,
    budget: _TextBudget | None = None,
) -> discord.ui.TextDisplay:
    if columns not in (1, 2):
        raise ValueError("key_values поддерживает только columns=1 или columns=2.")
    lines = [_key_value_line(name, value) for name, value in pairs if str(name).strip()]
    if columns == 2:
        merged: list[str] = []
        for index in range(0, len(lines), 2):
            chunk = lines[index : index + 2]
            merged.append(_COLUMN_JOIN.join(chunk))
        lines = merged
    allocator = budget if budget is not None else text_budget()
    return discord.ui.TextDisplay(allocator.take("\n".join(lines)))


def stat_tile(
    label: str,
    value: object,
    delta: object | None = None,
    *,
    budget: _TextBudget | None = None,
) -> discord.ui.TextDisplay:
    parts = [f"**{str(label).strip()}**", str(value).strip()]
    if delta is not None:
        parts.append(f"-# {_format_delta(delta)}")
    allocator = budget if budget is not None else text_budget()
    return discord.ui.TextDisplay(allocator.take("\n".join(part for part in parts if part)))


def progress(
    value: int,
    total: int,
    width: int = DEFAULT_PROGRESS_WIDTH,
    *,
    budget: _TextBudget | None = None,
) -> discord.ui.TextDisplay:
    if width < 1:
        raise ValueError("Ширина шкалы прогресса должна быть не меньше 1.")
    filled = _filled_units(value, total, width)
    bar = f"{_BAR_FILLED * filled}{_BAR_EMPTY * (width - filled)}"
    allocator = budget if budget is not None else text_budget()
    return discord.ui.TextDisplay(allocator.take(f"{bar} {value} / {total}"))


def status_badge(state: Status | str, *, budget: _TextBudget | None = None) -> discord.ui.TextDisplay:
    status = state if isinstance(state, Status) else Status(state)
    icon, word = STATUS_BADGES[status]
    allocator = budget if budget is not None else text_budget()
    return discord.ui.TextDisplay(allocator.reserve(f"{icon} **{word}**"))


def divider(
    spacing: discord.SeparatorSpacing | str = discord.SeparatorSpacing.small,
    visible: bool = True,
) -> discord.ui.Separator:
    resolved = _resolve_spacing(spacing)
    return discord.ui.Separator(visible=visible, spacing=resolved)


def section_with_action(
    text: str,
    button: discord.ui.Button[discord.ui.LayoutView],
    *,
    budget: _TextBudget | None = None,
) -> discord.ui.Section:
    content = _take_or_reserve(text, budget, reserve=False)
    if not content:
        raise ValueError("Секция с действием не может быть пустой.")
    return discord.ui.Section(content, accessory=button)


def section_with_thumbnail(
    text: str,
    url: str,
    *,
    budget: _TextBudget | None = None,
) -> discord.ui.Section:
    media = assert_remote_media_url(url)
    content = _take_or_reserve(text, budget, reserve=False)
    if not content:
        raise ValueError("Секция с миниатюрой не может быть пустой.")
    return discord.ui.Section(
        content,
        accessory=discord.ui.Thumbnail(media, description=content[:256]),
    )


def card(
    title: str,
    body: str,
    *,
    accent: Accent | int,
    footer: str | None = None,
    budget: _TextBudget | None = None,
) -> discord.ui.Container:
    allocator = budget if budget is not None else text_budget()
    title_item = heading(title, 2, budget=allocator)
    footnote = allocator.reserve(f"-# {footer.strip()}") if footer and footer.strip() else ""
    body_text = allocator.take(body)
    children: list[discord.ui.Item[discord.ui.LayoutView]] = [title_item]
    if body_text:
        children.append(discord.ui.TextDisplay(body_text))
    if footnote:
        children.append(discord.ui.TextDisplay(footnote))
    container = discord.ui.Container(*children, accent_color=int(accent))
    debug_validate_layout(container)
    return container


def empty_state(
    title: str,
    hint: str,
    action: discord.ui.Button[discord.ui.LayoutView] | str | None = None,
    *,
    accent: Accent | int = Accent.SERVICE,
    budget: _TextBudget | None = None,
) -> discord.ui.Container:
    allocator = budget if budget is not None else text_budget()
    children: list[discord.ui.Item[discord.ui.LayoutView]] = [
        heading(title, 2, budget=allocator),
    ]
    hint_text = allocator.take(f"-# {hint.strip()}" if hint.strip() else "")
    if isinstance(action, discord.ui.Button):
        section_text = hint_text or allocator.take(title)
        children.append(discord.ui.Section(section_text, accessory=action))
    else:
        if hint_text:
            children.append(discord.ui.TextDisplay(hint_text))
        if isinstance(action, str) and action.strip():
            next_step = allocator.take(f"**Что дальше:** {action.strip()}")
            if next_step:
                children.append(discord.ui.TextDisplay(next_step))
    container = discord.ui.Container(*children, accent_color=int(accent))
    debug_validate_layout(container)
    return container


def compose(
    *,
    accent: Accent | int,
    heading_text: str | None = None,
    summary: str | None = None,
    stats: Sequence[discord.ui.Item[discord.ui.LayoutView]] = (),
    details: Sequence[discord.ui.Item[discord.ui.LayoutView]] = (),
    actions: Sequence[discord.ui.Item[discord.ui.LayoutView]] = (),
    footnote: str | None = None,
    budget: _TextBudget | None = None,
) -> discord.ui.Container:
    """Assemble a card in ``LAYOUT_SEQUENCE`` order, skipping empty slots."""

    allocator = budget if budget is not None else text_budget()
    heading_item = heading(heading_text, 2, budget=allocator) if heading_text else None
    note = allocator.reserve(f"-# {footnote.strip()}") if footnote and footnote.strip() else ""
    children: list[discord.ui.Item[discord.ui.LayoutView]] = []
    if heading_item is not None:
        children.append(heading_item)
    if summary and summary.strip():
        text = allocator.take(summary)
        if text:
            children.append(discord.ui.TextDisplay(text))
    children.extend(stats)
    if details:
        children.append(divider(discord.SeparatorSpacing.small, True))
        children.extend(details)
    if actions:
        children.append(divider(discord.SeparatorSpacing.small, True))
        children.extend(actions)
    if note:
        children.append(discord.ui.TextDisplay(note))
    container = discord.ui.Container(*children, accent_color=int(accent))
    debug_validate_layout(container)
    return container


def assert_remote_media_url(url: str) -> str:
    """Accept only Discord-hosted HTTP(S) URLs. Never a local file or attachment."""

    if not isinstance(url, str):
        raise ValueError("Миниатюра должна быть URL Discord, не локальный файл.")
    value = url.strip()
    lowered = value.casefold()
    if not value:
        raise ValueError("URL миниатюры пуст.")
    if lowered.startswith(("attachment://", "file://")):
        raise ValueError(
            "Миниатюра не может быть локальным файлом или вложением attachment://."
        )
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError(
            "Миниатюра должна быть URL Discord (аватар или иконка сервера), не путь к файлу."
        )
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in _ALLOWED_MEDIA_HOST_SUFFIXES):
        raise ValueError("Миниатюра должна быть URL Discord (аватар или иконка сервера).")
    return value


def validate_layout(view: discord.ui.LayoutView | discord.ui.Item[discord.ui.LayoutView]) -> None:
    """Check section 0.6 limits at build time. Raises ``LayoutLimitError``."""

    items = list(_walk_items(view))
    if len(items) > MAX_COMPONENTS:
        raise LayoutLimitError(
            f"Максимум {MAX_COMPONENTS} компонентов в сообщении, сейчас {len(items)}."
        )

    text_length = sum(
        len(item.content) for item in items if isinstance(item, discord.ui.TextDisplay)
    )
    if text_length > MAX_TEXT_CHARS:
        raise LayoutLimitError(
            f"Суммарно не больше {MAX_TEXT_CHARS} символов в TextDisplay, сейчас {text_length}."
        )

    for item in items:
        if isinstance(item, discord.ui.ActionRow):
            buttons = [child for child in item.children if isinstance(child, discord.ui.Button)]
            if len(buttons) > MAX_ACTION_ROW_BUTTONS:
                raise LayoutLimitError(
                    f"В одном ActionRow не больше {MAX_ACTION_ROW_BUTTONS} кнопок, "
                    f"сейчас {len(buttons)}."
                )
        elif isinstance(item, discord.ui.MediaGallery):
            count = len(item.items)
            if count > MAX_MEDIA_GALLERY_ITEMS:
                raise LayoutLimitError(
                    f"В одной MediaGallery не больше {MAX_MEDIA_GALLERY_ITEMS} элементов, "
                    f"сейчас {count}."
                )
        elif isinstance(item, discord.ui.Section):
            texts = [child for child in item.children if isinstance(child, discord.ui.TextDisplay)]
            if not 1 <= len(texts) <= MAX_SECTION_TEXT_DISPLAYS:
                raise LayoutLimitError(
                    f"Section содержит от 1 до {MAX_SECTION_TEXT_DISPLAYS} TextDisplay, "
                    f"сейчас {len(texts)}."
                )
            accessory = item.accessory
            if accessory is None or not isinstance(
                accessory, (discord.ui.Button, discord.ui.Thumbnail)
            ):
                raise LayoutLimitError(
                    "Section должна содержать ровно один accessory: кнопку или миниатюру."
                )
            if isinstance(accessory, discord.ui.Thumbnail):
                _validate_thumbnail_media(accessory)
        elif isinstance(item, discord.ui.Thumbnail):
            _validate_thumbnail_media(item)


def debug_validate_layout(view: discord.ui.LayoutView | discord.ui.Item[discord.ui.LayoutView]) -> None:
    """Run ``validate_layout`` only while Python assertions are enabled."""

    if __debug__:
        validate_layout(view)


def _key_value_line(name: object, value: object) -> str:
    return f"**{str(name).strip()}:** {str(value).strip()}"


def _format_delta(delta: object) -> str:
    if isinstance(delta, int):
        if delta > 0:
            return f"+{delta}"
        return str(delta)
    return str(delta).strip()


def _filled_units(value: int, total: int, width: int) -> int:
    if value <= 0:
        return 0
    if total <= 0 or value >= total:
        return width
    return max(0, min(width, (value * width) // total))


def _resolve_spacing(spacing: discord.SeparatorSpacing | str) -> discord.SeparatorSpacing:
    if isinstance(spacing, str):
        try:
            return discord.SeparatorSpacing[spacing]
        except KeyError as exc:
            raise ValueError("Разделитель принимает spacing 'small' или 'large'.") from exc
    return spacing


def _take_or_reserve(text: str, budget: _TextBudget | None, *, reserve: bool) -> str:
    allocator = budget if budget is not None else text_budget()
    cleaned = text.strip()
    if reserve:
        return allocator.reserve(cleaned)
    return allocator.take(cleaned)


def _walk_items(
    root: discord.ui.LayoutView | discord.ui.Item[discord.ui.LayoutView],
) -> Iterable[discord.ui.Item[discord.ui.LayoutView]]:
    seen: set[int] = set()

    def visit(item: discord.ui.Item[discord.ui.LayoutView]) -> Iterable[discord.ui.Item[discord.ui.LayoutView]]:
        ident = id(item)
        if ident in seen:
            return
        seen.add(ident)
        yield item
        children = getattr(item, "children", None)
        if children:
            for child in children:
                yield from visit(child)
        accessory = getattr(item, "accessory", None)
        if accessory is not None:
            yield from visit(accessory)

    if isinstance(root, discord.ui.LayoutView):
        for child in root.children:
            yield from visit(child)
        return
    yield from visit(root)


def _validate_thumbnail_media(thumbnail: discord.ui.Thumbnail[discord.ui.LayoutView]) -> None:
    media = thumbnail.media
    url = getattr(media, "url", None) or str(media)
    assert_remote_media_url(str(url))
