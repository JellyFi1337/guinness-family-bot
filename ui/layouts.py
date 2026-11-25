from __future__ import annotations

import logging
import zlib
from collections import defaultdict
from datetime import datetime
from typing import Iterable, Protocol

import discord

from services.metrics import metrics


logger = logging.getLogger(__name__)

WHITE = 0xFFFFFF
APPROVED = 0x57F287
DENIED = 0xED4245
CONTROL_CONTAINER_ID = 0x4755494E
STATUS_COMPONENT_ID = 0x53544154
MAX_TEXT_DISPLAY_LENGTH = 4_000
_TRUNCATION_SUFFIX = "\n\n-# Часть текста скрыта из-за ограничения Discord."


class _EmbedField(Protocol):
    name: str | None
    value: str | None
    inline: bool


class _TextBudget:
    """Allocates the Components v2 character budget, critical parts first.

    The old order spent the budget as it walked the embed: title, description,
    every field, then status, then footer. On a long message the parts that ran
    out were the last ones — the status, the reference id and the "what next"
    line, i.e. exactly what the reader needs and what support needs to trace
    the case. Long prose survived; the identifier did not.

    :meth:`reserve` claims room before any optional text is measured, so a
    critical block is never the thing that gets dropped.
    """

    def __init__(self, limit: int = MAX_TEXT_DISPLAY_LENGTH) -> None:
        self.remaining = limit
        self.reserved = 0
        self.truncated = False

    def reserve(self, value: object | None) -> str:
        """Claim room for text that must not be dropped.

        Reserved text is charged to the budget immediately, so every later
        :meth:`take` competes only for what is genuinely spare.
        """

        text = _clean(value)
        if not text or self.remaining <= 0:
            return ""
        if len(text) > self.remaining:
            # Even a critical block cannot exceed Discord's hard limit. Keeping
            # the head of it beats dropping it entirely.
            text = text[: self.remaining]
            self._note_truncation()
        self.remaining -= len(text)
        self.reserved += len(text)
        return text

    def take(self, value: object | None) -> str:
        if value is None or self.remaining <= 0:
            if _clean(value):
                self._note_truncation()
            return ""
        text = _clean(value)
        if not text:
            return ""
        if len(text) <= self.remaining:
            self.remaining -= len(text)
            return text
        if self.remaining <= len(_TRUNCATION_SUFFIX):
            result = text[: self.remaining]
        else:
            available = self.remaining - len(_TRUNCATION_SUFFIX)
            result = text[:available].rstrip() + _TRUNCATION_SUFFIX
        self.remaining = 0
        self._note_truncation()
        return result

    def join(self, parts: Iterable[str], separator: str = "\n\n") -> str:
        """Join already-budgeted blocks, charging the separators too.

        A separator is payload like any other character. Joining a reserved
        heading onto a description that had just spent the last of the budget
        pushed the message two characters past Discord's hard limit — the
        classic off-by-a-separator that only shows up on the longest message
        anyone ever sends.
        """

        kept = [part for part in parts if part]
        if len(kept) < 2:
            return kept[0] if kept else ""
        cost = len(separator) * (len(kept) - 1)
        if cost <= self.remaining:
            self.remaining -= cost
            return separator.join(kept)
        # Pay for the separators out of the last block: the earlier ones are
        # the reserved, more important ones.
        deficit = cost - self.remaining
        self.remaining = 0
        kept[-1] = kept[-1][: max(0, len(kept[-1]) - deficit)].rstrip()
        self._note_truncation()
        kept = [part for part in kept if part]
        return separator.join(kept)

    def _note_truncation(self) -> None:
        if self.truncated:
            return
        self.truncated = True
        metrics.increment("ui.payload_truncated")
        logger.warning(
            "Components v2 payload exceeded %d characters and was truncated; "
            "critical fields were reserved first",
            MAX_TEXT_DISPLAY_LENGTH,
        )


def _clean(value: object | None) -> str:
    return str(value).strip() if value is not None else ""


class ModernLayoutView(discord.ui.LayoutView):
    """A Components v2 view which can host callbacks from a legacy ``View``.

    The compatibility bridge lets the project migrate its presentation in one
    release without invalidating persistent component custom IDs.  New views
    should be authored directly with ``LayoutView``/``ActionRow``.
    """

    def __init__(
        self,
        *,
        timeout: float | None = 180.0,
        legacy_view: discord.ui.View | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.legacy_view = legacy_view

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.legacy_view is not None:
            return await self.legacy_view.interaction_check(interaction)
        return True

    async def on_timeout(self) -> None:
        if self.legacy_view is not None:
            await self.legacy_view.on_timeout()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        if self.legacy_view is not None:
            await self.legacy_view.on_error(interaction, error, item)
            return
        logger.error(
            "Components v2 callback failed for item=%s",
            getattr(item, "custom_id", item.__class__.__name__),
            exc_info=(type(error), error, error.__traceback__),
        )


def _accent_for(embeds: Iterable[discord.Embed]) -> int:
    for embed in embeds:
        if embed.color is not None:
            return embed.color.value
    return WHITE


def _asset_url(proxy: object | None) -> str | None:
    if proxy is None:
        return None
    if isinstance(proxy, str):
        value = proxy.strip()
        return value or None
    url = getattr(proxy, "url", None)
    if not url:
        return None
    value = str(url).strip()
    return value or None


def _header(embed: discord.Embed) -> str:
    parts: list[str] = []
    author_name = getattr(embed.author, "name", None)
    if author_name:
        parts.append(f"-# {author_name}")
    if embed.title:
        parts.append(f"## {embed.title}")
    return "\n".join(parts)


def _footer(embed: discord.Embed) -> str:
    values = [
        str(embed.footer.text)
        if embed.footer.text
        else "Guinness Family"
    ]
    if isinstance(embed.timestamp, datetime):
        values.append(f"<t:{int(embed.timestamp.timestamp())}:f>")
    return " • ".join(values)


def _compact_fields(fields: Iterable[_EmbedField]) -> str:
    """Keep all embed fields while avoiding a tall heading per short value."""

    lines: list[str] = []
    inline: list[str] = []
    for field in fields:
        name = str(field.name or "").strip()
        value = str(field.value or "").strip()
        short = bool(field.inline) and "\n" not in value and len(name) + len(value) <= 96
        if short:
            inline.append(f"**{name}:** {value}")
            if len(inline) == 2:
                lines.append("　•　".join(inline))
                inline.clear()
            continue
        if inline:
            lines.append("　•　".join(inline))
            inline.clear()
        lines.append(f"**{name}**\n{value}")
    if inline:
        lines.append("　•　".join(inline))
    return "\n\n".join(lines)


# Fields that carry the outcome or the identifier a reader needs to act on.
# They are reserved before any descriptive text competes for the budget.
STATUS_FIELD_NAMES = frozenset({"статус", "status"})
NEXT_STEP_FIELD_NAMES = frozenset({"что дальше", "следующий шаг", "next step"})


def _is_status_field(field: _EmbedField) -> bool:
    return (field.name or "").strip().casefold() in STATUS_FIELD_NAMES


def _is_next_step_field(field: _EmbedField) -> bool:
    return (field.name or "").strip().casefold() in NEXT_STEP_FIELD_NAMES


def marker_component_id(marker: str) -> int:
    """Return a deterministic, non-zero component id for reconciliation."""

    return (zlib.crc32(marker.encode("utf-8")) & 0x7FFFFFFF) or 1


def _add_embed(
    container: discord.ui.Container,
    embed: discord.Embed,
    budget: _TextBudget,
    media_urls: Iterable[str] = (),
    status_component_id: int | None = None,
) -> None:
    heading = _header(embed)
    description = str(embed.description).strip() if embed.description else ""
    thumbnail = _asset_url(embed.thumbnail) or _asset_url(
        getattr(embed.author, "icon_url", None)
    )

    status_fields = [
        field for field in embed.fields if _is_status_field(field)
    ]
    next_step_fields = [
        field for field in embed.fields if _is_next_step_field(field)
    ]
    other_fields = [
        field
        for field in embed.fields
        if not _is_status_field(field) and not _is_next_step_field(field)
    ]

    # Reserve, in order of importance, before anything optional is measured.
    # The heading identifies the message, the footer carries the code and the
    # reference id, and status/next step are the actionable part.
    reserved_heading = budget.reserve(heading)
    footer_text = budget.reserve(f"-# {_footer(embed)}")
    status_text = budget.reserve(
        "\n\n".join(f"**{field.name}:** {field.value}" for field in status_fields)
    )
    next_step_text = budget.reserve(
        "\n\n".join(f"**{field.name}:** {field.value}" for field in next_step_fields)
    )

    # Whatever is left is spent on the descriptive parts, longest-lived first.
    description_text = budget.take(description)
    field_text = budget.take(_compact_fields(other_fields))

    intro = budget.join((reserved_heading, description_text))
    if thumbnail and intro:
        container.add_item(
            discord.ui.Section(
                intro,
                accessory=discord.ui.Thumbnail(
                    thumbnail,
                    description=str(embed.title or "Guinness Family"),
                ),
            )
        )
    else:
        if intro:
            container.add_item(discord.ui.TextDisplay(intro))

    if field_text:
        container.add_item(discord.ui.TextDisplay(field_text))
    if next_step_text:
        container.add_item(discord.ui.TextDisplay(next_step_text))
    if status_text:
        container.add_item(
            discord.ui.TextDisplay(status_text, id=status_component_id)
        )

    image = _asset_url(embed.image)
    gallery_urls = list(dict.fromkeys([item for item in (image, *media_urls) if item]))
    if gallery_urls:
        gallery: discord.ui.MediaGallery[ModernLayoutView] = (
            discord.ui.MediaGallery()
        )
        for index, media in enumerate(gallery_urls[:10], start=1):
            gallery.add_item(
                media=media,
                description=(
                    str(embed.title or "Иллюстрация Guinness Family")[:256]
                    if index == 1
                    else f"Приложение {index}"
                ),
            )
        container.add_item(gallery)

    if footer_text:
        container.add_item(discord.ui.TextDisplay(footer_text))


def _legacy_rows(view: discord.ui.View) -> list[discord.ui.ActionRow]:
    rows: dict[int, list[discord.ui.Item]] = defaultdict(list)
    for item in view.children:
        rendered = getattr(item, "_rendered_row", None)
        row = rendered if rendered is not None else (getattr(item, "row", None) or 0)
        rows[int(row)].append(item)

    result: list[discord.ui.ActionRow] = []
    for row_index in sorted(rows):
        row = discord.ui.ActionRow()
        for item in rows[row_index]:
            row.add_item(item)
        result.append(row)
    return result


def _append_controls(
    layout: ModernLayoutView,
    container: discord.ui.Container,
    view: discord.ui.View | discord.ui.LayoutView | None,
) -> None:
    if view is None:
        return
    if isinstance(view, discord.ui.LayoutView):
        for child in list(view.children):
            view.remove_item(child)
            layout.add_item(child)
        return
    rows = _legacy_rows(view)
    for row in rows:
        container.add_item(row)


def _attach_legacy_controls(
    layout: ModernLayoutView,
    container: discord.ui.Container,
    view: discord.ui.View,
) -> None:
    """Place controls in the body card; keep a safe overflow fallback."""

    rows = _legacy_rows(view)
    if len(container.children) + len(rows) <= 10:
        for row in rows:
            container.add_item(row)
        return

    # This is only reachable for unusually complex, multi-part cards.  It is
    # still one Discord message and avoids violating the 10-child API limit.
    overflow: discord.ui.Container[ModernLayoutView] = discord.ui.Container(
        discord.ui.TextDisplay("-# Управление"),
        accent_color=container.accent_color,
        id=CONTROL_CONTAINER_ID,
    )
    for row in rows:
        overflow.add_item(row)
    layout.add_item(overflow)


def build_layout(
    *,
    content: object | None = None,
    embed: discord.Embed | None = None,
    embeds: Iterable[discord.Embed] | None = None,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
    marker: str | None = None,
    attachment_urls: Iterable[str] = (),
    attachment_files: Iterable[str] = (),
) -> ModernLayoutView:
    """Convert legacy message data into a modern Components v2 layout."""

    all_embeds = list(embeds or ())
    if embed is not None:
        all_embeds.insert(0, embed)

    if isinstance(view, ModernLayoutView) and content is None and not all_embeds:
        return view

    timeout = getattr(view, "timeout", 180.0) if view is not None else 180.0
    legacy = view if isinstance(view, discord.ui.View) else None
    layout = ModernLayoutView(timeout=timeout, legacy_view=legacy)
    budget = _TextBudget()
    raw_content = str(content).strip() if content is not None else ""

    containers: list[discord.ui.Container] = []
    container: discord.ui.Container[ModernLayoutView] = discord.ui.Container(
        accent_color=_accent_for(all_embeds)
    )
    if raw_content and not all_embeds:
        container.add_item(discord.ui.TextDisplay(budget.take(raw_content)))
    elif raw_content:
        # Keep mentions and short routing notes visually outside the embed
        # card.  This mirrors Discord's classic ``content + embed`` layout and
        # makes staff-role pings unambiguous without sacrificing Components v2.
        routed_content = budget.take(raw_content)
        if routed_content:
            layout.add_item(discord.ui.TextDisplay(routed_content))

    for index, item in enumerate(all_embeds):
        embed_container: discord.ui.Container[ModernLayoutView] = discord.ui.Container(
            accent_color=_accent_for((item,)),
            id=marker_component_id(marker) if index == 0 and marker else None,
        )
        _add_embed(
            embed_container,
            item,
            budget,
            attachment_urls if index == 0 else (),
            STATUS_COMPONENT_ID if index == 0 else None,
        )
        if embed_container.children:
            containers.append(embed_container)

    if not all_embeds and container.children:
        containers.append(container)
    for body in containers:
        layout.add_item(body)

    media_only = tuple(attachment_urls) if not all_embeds else ()
    if media_only:
        gallery: discord.ui.MediaGallery[ModernLayoutView] = (
            discord.ui.MediaGallery()
        )
        for index, media in enumerate(media_only[:10], start=1):
            gallery.add_item(media=media, description=f"Приложение {index}")
        layout.add_item(discord.ui.Container(gallery, accent_color=WHITE))

    file_items = tuple(attachment_files)
    for offset in range(0, len(file_items), 10):
        layout.add_item(
            discord.ui.Container(
                *(discord.ui.File(media) for media in file_items[offset : offset + 10]),
                accent_color=WHITE,
            )
        )

    primary = containers[0] if containers else None
    if isinstance(view, discord.ui.LayoutView):
        _append_controls(layout, discord.ui.Container(), view)
    elif view is not None:
        if primary is None:
            primary = discord.ui.Container(accent_color=WHITE)
            layout.add_item(primary)
        _attach_legacy_controls(layout, primary, view)

    if marker:
        marked = next(
            (item for item in layout.children if isinstance(item, discord.ui.Container)),
            None,
        )
        if marked is not None and marked.id is None:
            marked.id = marker_component_id(marker)

    if not layout.children:
        layout.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("-# Guinness Family"),
                accent_color=WHITE,
                id=marker_component_id(marker) if marker else None,
            )
        )
    return layout


def remove_action_rows(layout: discord.ui.LayoutView) -> None:
    """Remove interactive rows while preserving all Components v2 content."""

    for child in list(layout.children):
        if isinstance(child, discord.ui.ActionRow):
            layout.remove_item(child)
        elif isinstance(child, discord.ui.Container):
            removed = False
            for nested in list(child.children):
                if isinstance(nested, discord.ui.ActionRow):
                    child.remove_item(nested)
                    removed = True
            if removed and child.children and isinstance(
                child.children[-1], discord.ui.Separator
            ):
                child.remove_item(child.children[-1])


def _prune_empty_containers(layout: discord.ui.LayoutView) -> None:
    """Remove containers rejected by Discord after incremental v2 edits."""

    for child in list(layout.children):
        if isinstance(child, discord.ui.Container) and not child.children:
            layout.remove_item(child)


def replace_controls(
    current: discord.ui.LayoutView,
    view: discord.ui.View | discord.ui.LayoutView | None,
) -> discord.ui.LayoutView:
    """Replace callbacks without flattening an existing Components v2 card."""

    remove_action_rows(current)
    timeout = getattr(view, "timeout", None) if view is not None else None
    legacy = view if isinstance(view, discord.ui.View) else None
    result = ModernLayoutView(timeout=timeout, legacy_view=legacy)
    for child in list(current.children):
        current.remove_item(child)
        if (
            isinstance(child, discord.ui.Container)
            and child.id == CONTROL_CONTAINER_ID
        ):
            continue
        result.add_item(child)
    _prune_empty_containers(result)
    if view is None:
        if not result.children:
            result.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("-# Guinness Family"),
                    accent_color=WHITE,
                )
            )
        return result

    primary = next(
        (item for item in result.children if isinstance(item, discord.ui.Container)),
        None,
    )
    if isinstance(view, discord.ui.LayoutView):
        empty: discord.ui.Container[ModernLayoutView] = discord.ui.Container(
            accent_color=WHITE
        )
        _append_controls(result, empty, view)
        if empty.children:
            result.add_item(empty)
    else:
        if primary is None:
            primary = discord.ui.Container(accent_color=WHITE)
            result.add_item(primary)
        _attach_legacy_controls(result, primary, view)
    _prune_empty_containers(result)
    return result
