"""Persistent Components V2 navigation: page, tabs, filters, confirm.

Page, filter, sort, owner and the confirm-used flag live in ``custom_id``.
This module does not keep that state in process-wide dictionaries.
Handler tables (which domain knows how to redraw a panel) are configuration.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

import discord

from core.log_context import bind_interaction, current_reference
from ui.custom_ids import (
    CURRENT_VERSION,
    CustomId,
    CustomIdError,
    parse_custom_id,
)
from ui.design import Accent, compose, debug_validate_layout
from ui.layouts import ModernLayoutView
from ui.messages import edit_interaction_message_v2, respond_spec
from ui.pagination import PAGE_CHARACTER_LIMIT, format_page_caption, paginate_lines
from ui.templates import system_error, unchanged
from utils.locks import KeyedLockPool

NAV_ACTIONS: tuple[str, ...] = ("first", "prev", "next", "last", "refresh")
_RESERVED_STATE_KEYS = frozenset({"p", "f", "s", "o", "k", "t", "n", "u"})
_NAV_LABELS: dict[str, str] = {
    "first": "Первая",
    "prev": "Назад",
    "next": "Вперёд",
    "last": "Последняя",
    "refresh": "Обновить",
}
_DYNAMIC_TEMPLATE = (
    r"g:(?P<version>\d+):(?P<domain>[a-z0-9_]+):(?P<action>[a-z0-9_]+):"
    r"(?P<object>[A-Za-z0-9_-]+):(?P<params>.*)"
)

_PAGE_SOURCES: dict[str, PageSource] = {}
_TAB_SOURCES: dict[str, TabSource] = {}
_CONFIRM_LOCKS: KeyedLockPool[tuple[str, str, str]] = KeyedLockPool()


class PageSource(Protocol):
    async def get_lines(
        self,
        interaction: discord.Interaction,
        parsed: CustomId,
    ) -> Sequence[str]: ...

    async def allowed(
        self,
        interaction: discord.Interaction,
        parsed: CustomId,
    ) -> bool: ...

    def title(self, parsed: CustomId) -> str: ...


class TabSource(Protocol):
    def tabs(self, parsed: CustomId) -> Sequence[Tab]: ...

    async def render(
        self,
        interaction: discord.Interaction,
        parsed: CustomId,
        tab: str,
    ) -> str: ...

    async def allowed(
        self,
        interaction: discord.Interaction,
        parsed: CustomId,
    ) -> bool: ...

    def title(self, parsed: CustomId) -> str: ...

    async def get_lines(
        self,
        interaction: discord.Interaction,
        parsed: CustomId,
        tab: str,
    ) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class Tab:
    key: str
    label: str


@dataclass(frozen=True, slots=True)
class FilterSpec:
    key: str
    placeholder: str
    options: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PageState:
    page: int = 1
    filter: str = ""
    sort: str = ""
    owner_id: int | None = None
    extra: tuple[tuple[str, str], ...] = ()

    def as_params(self) -> dict[str, str | int]:
        params: dict[str, str | int] = {"p": self.page}
        if self.filter:
            params["f"] = self.filter
        if self.sort:
            params["s"] = self.sort
        if self.owner_id is not None:
            params["o"] = self.owner_id
        for key, value in self.extra:
            if value:
                params[key] = value
        return params


def register_page_source(domain: str, source: PageSource) -> None:
    _PAGE_SOURCES[domain] = source


def register_tab_source(domain: str, source: TabSource) -> None:
    _TAB_SOURCES[domain] = source


def unregister_sources(domain: str | None = None) -> None:
    if domain is None:
        _PAGE_SOURCES.clear()
        _TAB_SOURCES.clear()
        return
    _PAGE_SOURCES.pop(domain, None)
    _TAB_SOURCES.pop(domain, None)


def apply_page_action(action: str, page: int, total: int) -> int:
    """Move the 1-based page. ``next`` / ``prev`` wrap around; first/last do not."""

    if total <= 0:
        return 1
    current = ((page - 1) % total) + 1
    if action == "first":
        return 1
    if action == "last":
        return total
    if action == "refresh":
        return current
    if action == "prev":
        return total if current <= 1 else current - 1
    if action == "next":
        return 1 if current >= total else current + 1
    raise CustomIdError(f"Неизвестное действие навигации: {action}")


def state_from_id(parsed: CustomId) -> PageState:
    extra = tuple(
        (key, value)
        for key, value in parsed.params
        if key not in _RESERVED_STATE_KEYS and value
    )
    return PageState(
        page=parsed.page(),
        filter=parsed.param("f"),
        sort=parsed.param("s"),
        owner_id=parsed.owner_id(),
        extra=extra,
    )


def apply_filter_to_state(state: PageState, key: str, value: str) -> PageState:
    """Write a FilterBar selection into page state and return to page 1."""

    if key == "s":
        return replace(state, page=1, sort=value)
    if key == "f":
        return replace(state, page=1, filter=value)
    merged = dict(state.extra)
    if value and value != "all":
        merged[key] = value
    else:
        merged.pop(key, None)
    return replace(state, page=1, extra=tuple(sorted(merged.items())))


async def reject_stale_button(interaction: discord.Interaction) -> None:
    await respond_spec(
        interaction,
        unchanged(
            code="UI.STALE_BUTTON",
            title="Кнопка устарела",
            summary="Эта кнопка больше не действует. Откройте панель заново.",
            next_step="Откройте панель заново.",
            reference=current_reference(),
        ),
        ephemeral=True,
    )


async def reject_foreign_user(interaction: discord.Interaction) -> None:
    await respond_spec(
        interaction,
        unchanged(
            code="UI.PANEL_OWNER",
            title="Панель другого участника",
            summary="Эта панель открыта другим человеком. Действие не выполнено.",
            next_step="Откройте свою копию панели.",
            reference=current_reference(),
        ),
        ephemeral=True,
    )


async def reject_already_done(interaction: discord.Interaction) -> None:
    await respond_spec(
        interaction,
        unchanged(
            code="UI.ALREADY_DONE",
            title="Действие уже выполнено",
            summary="Подтверждение уже принято. Повторно выполнять его не нужно.",
            next_step="Дополнительных действий не требуется.",
            reference=current_reference(),
        ),
        ephemeral=True,
    )


async def reject_forbidden(interaction: discord.Interaction) -> None:
    await respond_spec(
        interaction,
        system_error(
            code="UI.NAV_FORBIDDEN",
            title="Действие недоступно",
            summary="Сейчас у вас нет права пользоваться этой панелью.",
            retryable=False,
            reference=current_reference(),
        ),
        ephemeral=True,
    )


async def reject_if_blocked(
    interaction: discord.Interaction,
    parsed: CustomId,
    *,
    can_use: Callable[[discord.Interaction], Awaitable[bool]] | None = None,
) -> bool:
    """Return True when the click must stop. Rights are checked now, not at render."""

    owner_id = parsed.owner_id()
    if owner_id is not None and interaction.user.id != owner_id:
        await reject_foreign_user(interaction)
        return True
    if can_use is not None and not await can_use(interaction):
        await reject_forbidden(interaction)
        return True
    return False


async def _replace_nav_view(
    interaction: discord.Interaction,
    view: discord.ui.LayoutView,
) -> None:
    # content=None forces a full V2 rebuild; otherwise the transport keeps the
    # previous body and only swaps the control row.
    await edit_interaction_message_v2(interaction, content=None, view=view)


class PersistentNavView(ModernLayoutView):
    """LayoutView that lives as long as the channel message when timeout is None."""

    def __init__(
        self,
        *,
        timeout: float | None = None,
        can_use: Callable[[discord.Interaction], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.can_use = can_use

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bind_interaction(interaction, source="component")
        raw = ""
        if interaction.data:
            raw = str(interaction.data.get("custom_id") or "")
        try:
            parsed = parse_custom_id(raw, allow_stale=True)
        except CustomIdError:
            await reject_stale_button(interaction)
            return False
        if parsed.version != CURRENT_VERSION:
            await reject_stale_button(interaction)
            return False
        if parsed.action in {"confirm", "cancel"} and parsed.used():
            await reject_already_done(interaction)
            return False
        if await reject_if_blocked(interaction, parsed, can_use=self.can_use):
            return False
        return True


class Paginator(PersistentNavView):
    """Shared-channel pager. Page, filter and sort are stored on the buttons."""

    def __init__(
        self,
        *,
        domain: str,
        object_id: str,
        lines: Sequence[str],
        state: PageState | None = None,
        title: str = "Список",
        accent: Accent | int = Accent.SERVICE,
        page_limit: int = PAGE_CHARACTER_LIMIT,
        filters: Sequence[FilterSpec] = (),
        can_use: Callable[[discord.Interaction], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__(timeout=None, can_use=can_use)
        self.domain = domain
        self.object_id = object_id
        self.lines = list(lines)
        self.state = state or PageState()
        self.title = title
        self.accent = accent
        self.page_limit = page_limit
        self.filters = tuple(filters)
        self.pages = paginate_lines(self.lines, limit=page_limit)
        total = len(self.pages)
        page = apply_page_action("refresh", self.state.page, total)
        self.state = PageState(
            page=page,
            filter=self.state.filter,
            sort=self.state.sort,
            owner_id=self.state.owner_id,
            extra=self.state.extra,
        )
        self._mount()

    def _base_id(self, action: str) -> CustomId:
        return CustomId(
            version=CURRENT_VERSION,
            domain=self.domain,
            action=action,
            object_id=self.object_id,
            params=tuple(
                sorted((key, str(value)) for key, value in self.state.as_params().items())
            ),
        )

    def _mount(self) -> None:
        details: list[discord.ui.Item[discord.ui.LayoutView]] = []
        body = self.pages[self.state.page - 1] if self.pages else ""
        if body:
            details.append(discord.ui.TextDisplay(body))
        actions: list[discord.ui.Item[discord.ui.LayoutView]] = []
        if self.filters:
            actions.extend(self._filter_rows())
        if len(self.pages) > 1:
            actions.append(self._nav_row())
        footnote = (
            format_page_caption(self.state.page, len(self.pages))
            if len(self.pages) > 1
            else None
        )
        container = compose(
            accent=self.accent,
            heading_text=self.title,
            details=details,
            actions=actions,
            footnote=footnote,
        )
        self.add_item(container)
        debug_validate_layout(self)

    def _nav_row(self) -> discord.ui.ActionRow:
        row: discord.ui.ActionRow = discord.ui.ActionRow()
        for action in NAV_ACTIONS:
            button: discord.ui.Button = discord.ui.Button(
                label=_NAV_LABELS[action],
                style=discord.ButtonStyle.secondary,
                custom_id=self._base_id(action).render(),
            )
            button.callback = self._make_nav_callback(action)  # type: ignore[method-assign, assignment]
            row.add_item(button)
        return row

    def _filter_rows(self) -> list[discord.ui.ActionRow]:
        rows: list[discord.ui.ActionRow] = []
        current = self.state.as_params()
        for spec in self.filters:
            selected = str(current.get(spec.key, self.state.filter if spec.key == "f" else ""))
            select: discord.ui.Select = discord.ui.Select(
                placeholder=spec.placeholder,
                custom_id=self._base_id("filter").with_params(k=spec.key).render(),
                options=[
                    discord.SelectOption(
                        label=label,
                        value=value,
                        default=value == selected,
                    )
                    for value, label in spec.options
                ],
                min_values=1,
                max_values=1,
            )
            select.callback = self._make_filter_callback(spec.key)  # type: ignore[method-assign, assignment]
            rows.append(discord.ui.ActionRow(select))
        return rows

    def _make_nav_callback(self, action: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def callback(interaction: discord.Interaction) -> None:
            await self._handle_nav(interaction, action)

        return callback

    def _make_filter_callback(
        self, key: str
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def callback(interaction: discord.Interaction) -> None:
            values = _selected_values(interaction)
            chosen = values[0] if values else ""
            await self._handle_filter(interaction, key, chosen)

        return callback

    def _clone(self, state: PageState) -> Paginator:
        return Paginator(
            domain=self.domain,
            object_id=self.object_id,
            lines=self.lines,
            state=state,
            title=self.title,
            accent=self.accent,
            page_limit=self.page_limit,
            filters=self.filters,
            can_use=self.can_use,
        )

    async def _handle_nav(self, interaction: discord.Interaction, action: str) -> None:
        next_page = apply_page_action(action, self.state.page, len(self.pages))
        await _replace_nav_view(
            interaction,
            self._clone(
                PageState(
                    page=next_page,
                    filter=self.state.filter,
                    sort=self.state.sort,
                    owner_id=self.state.owner_id,
                    extra=self.state.extra,
                )
            ),
        )

    async def _handle_filter(
        self, interaction: discord.Interaction, key: str, value: str
    ) -> None:
        await _replace_nav_view(
            interaction,
            self._clone(apply_filter_to_state(self.state, key, value)),
        )


class TabbedPanel(PersistentNavView):
    """Section switcher. The active tab is stored on the select ``custom_id``."""

    def __init__(
        self,
        *,
        domain: str,
        object_id: str,
        tabs: Sequence[Tab],
        active: str,
        body: str,
        title: str,
        owner_id: int | None = None,
        accent: Accent | int = Accent.SERVICE,
        can_use: Callable[[discord.Interaction], Awaitable[bool]] | None = None,
        page: int = 1,
        page_count: int = 1,
    ) -> None:
        super().__init__(timeout=None, can_use=can_use)
        if not tabs:
            raise ValueError("TabbedPanel требует хотя бы один раздел.")
        keys = {tab.key for tab in tabs}
        self.domain = domain
        self.object_id = object_id
        self.tabs = tuple(tabs)
        self.active = active if active in keys else self.tabs[0].key
        self.body = body
        self.title = title
        self.owner_id = owner_id
        self.accent = accent
        self.page = max(1, page)
        self.page_count = max(1, page_count)
        self._mount()

    def _parsed(self, action: str) -> CustomId:
        params: dict[str, str | int] = {"t": self.active, "p": self.page}
        if self.owner_id is not None:
            params["o"] = self.owner_id
        return CustomId(
            version=CURRENT_VERSION,
            domain=self.domain,
            action=action,
            object_id=self.object_id,
            params=tuple(sorted((key, str(value)) for key, value in params.items())),
        )

    def _mount(self) -> None:
        select: discord.ui.Select = discord.ui.Select(
            placeholder="Раздел",
            custom_id=self._parsed("tab").render(),
            options=[
                discord.SelectOption(
                    label=f"• {tab.label}" if tab.key == self.active else tab.label,
                    value=tab.key,
                    default=tab.key == self.active,
                )
                for tab in self.tabs
            ],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_tab  # type: ignore[method-assign]
        details: list[discord.ui.Item[discord.ui.LayoutView]] = []
        if self.body.strip():
            details.append(discord.ui.TextDisplay(self.body))
        actions: list[discord.ui.Item[discord.ui.LayoutView]] = [discord.ui.ActionRow(select)]
        if self.page_count > 1:
            actions.append(self._nav_row())
        footnote = (
            format_page_caption(self.page, self.page_count)
            if self.page_count > 1
            else None
        )
        container = compose(
            accent=self.accent,
            heading_text=self.title,
            details=details,
            actions=actions,
            footnote=footnote,
        )
        self.add_item(container)
        debug_validate_layout(self)

    def _nav_row(self) -> discord.ui.ActionRow:
        row: discord.ui.ActionRow = discord.ui.ActionRow()
        for action in NAV_ACTIONS:
            button: discord.ui.Button = discord.ui.Button(
                label=_NAV_LABELS[action],
                style=discord.ButtonStyle.secondary,
                custom_id=self._parsed(action).render(),
            )
            button.callback = self._make_nav_callback(action)  # type: ignore[method-assign, assignment]
            row.add_item(button)
        return row

    def _make_nav_callback(
        self, action: str
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def callback(interaction: discord.Interaction) -> None:
            await self._handle_nav(interaction, action)

        return callback

    async def _handle_nav(self, interaction: discord.Interaction, action: str) -> None:
        replacement = await build_tabbed_panel(
            interaction,
            domain=self.domain,
            object_id=self.object_id,
            tab=self.active,
            page=apply_page_action(action, self.page, self.page_count),
            owner_id=self.owner_id,
            accent=self.accent,
            can_use=self.can_use,
            fallback_body=self.body,
            fallback_tabs=self.tabs,
            fallback_title=self.title,
            fallback_page_count=self.page_count,
        )
        await _replace_nav_view(interaction, replacement)

    async def _on_tab(self, interaction: discord.Interaction) -> None:
        values = _selected_values(interaction)
        chosen = values[0] if values else self.active
        replacement = await build_tabbed_panel(
            interaction,
            domain=self.domain,
            object_id=self.object_id,
            tab=chosen,
            page=1,
            owner_id=self.owner_id,
            accent=self.accent,
            can_use=self.can_use,
            fallback_body=self.body,
            fallback_tabs=self.tabs,
            fallback_title=self.title,
        )
        await _replace_nav_view(interaction, replacement)


async def build_tabbed_panel(
    interaction: discord.Interaction,
    *,
    domain: str,
    object_id: str,
    tab: str,
    page: int = 1,
    owner_id: int | None = None,
    accent: Accent | int = Accent.SERVICE,
    can_use: Callable[[discord.Interaction], Awaitable[bool]] | None = None,
    fallback_body: str = "",
    fallback_tabs: Sequence[Tab] = (),
    fallback_title: str = "",
    fallback_page_count: int = 1,
) -> TabbedPanel:
    """Rebuild a tab panel from the registered source, or from fallbacks."""

    source = _TAB_SOURCES.get(domain)
    params: dict[str, str | int] = {"t": tab, "p": max(1, page)}
    if owner_id is not None:
        params["o"] = owner_id
    parsed = CustomId(
        version=CURRENT_VERSION,
        domain=domain,
        action="tab",
        object_id=object_id,
        params=tuple(sorted((key, str(value)) for key, value in params.items())),
    )
    tabs = tuple(source.tabs(parsed)) if source is not None else tuple(fallback_tabs)
    title = source.title(parsed) if source is not None else fallback_title
    body = fallback_body
    page_count = max(1, fallback_page_count)
    safe_page = max(1, page)
    if source is not None:
        getter = getattr(source, "get_lines", None)
        if callable(getter):
            lines = await getter(interaction, parsed, tab)
            pages = paginate_lines(list(lines))
            page_count = max(1, len(pages))
            safe_page = ((page - 1) % page_count) + 1
            body = pages[safe_page - 1]
        else:
            body = await source.render(interaction, parsed, tab)
            page_count = 1
            safe_page = 1
    else:
        safe_page = min(safe_page, page_count)
    if not tabs:
        raise ValueError("TabbedPanel требует хотя бы один раздел.")
    return TabbedPanel(
        domain=domain,
        object_id=object_id,
        tabs=tabs,
        active=tab,
        body=body,
        title=title or fallback_title,
        owner_id=owner_id,
        accent=accent,
        can_use=can_use,
        page=safe_page,
        page_count=page_count,
    )


class FilterBar:
    """Selects that write the current filter into ``custom_id`` and reset the page."""

    def __init__(self, specs: Sequence[FilterSpec]) -> None:
        self.specs = tuple(specs)

    def attach(self, paginator: Paginator) -> Paginator:
        if paginator.filters:
            return paginator
        return Paginator(
            domain=paginator.domain,
            object_id=paginator.object_id,
            lines=paginator.lines,
            state=paginator.state,
            title=paginator.title,
            accent=paginator.accent,
            page_limit=paginator.page_limit,
            filters=self.specs,
            can_use=paginator.can_use,
        )


class ConfirmDialog(PersistentNavView):
    """Irreversible action: names the effect, the initiator, confirm and cancel."""

    def __init__(
        self,
        *,
        domain: str,
        object_id: str,
        summary: str,
        actor: str,
        on_confirm: Callable[[discord.Interaction], Awaitable[None]],
        owner_id: int | None = None,
        token: str | None = None,
        used: bool = False,
        timeout: float | None = 60.0,
        accent: Accent | int = Accent.WARNING,
        can_use: Callable[[discord.Interaction], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__(timeout=timeout, can_use=can_use)
        self.domain = domain
        self.object_id = object_id
        self.summary = summary
        self.actor = actor
        self.on_confirm = on_confirm
        self.owner_id = owner_id
        self.token = token or secrets.token_hex(4)
        self.used = used
        self.accent = accent
        self._consumed = used
        self._mount()

    def _id(self, action: str, *, used: bool | None = None) -> str:
        flag = self.used if used is None else used
        params: dict[str, str | int] = {"n": self.token, "u": 1 if flag else 0}
        if self.owner_id is not None:
            params["o"] = self.owner_id
        return CustomId(
            version=CURRENT_VERSION,
            domain=self.domain,
            action=action,
            object_id=self.object_id,
            params=tuple(sorted((key, str(value)) for key, value in params.items())),
        ).render()

    def _clone(self, *, used: bool) -> ConfirmDialog:
        return ConfirmDialog(
            domain=self.domain,
            object_id=self.object_id,
            summary=self.summary,
            actor=self.actor,
            on_confirm=self.on_confirm,
            owner_id=self.owner_id,
            token=self.token,
            used=used,
            timeout=self.timeout,
            accent=self.accent,
            can_use=self.can_use,
        )

    def _mount(self) -> None:
        confirm: discord.ui.Button = discord.ui.Button(
            label="Подтвердить",
            style=discord.ButtonStyle.danger,
            custom_id=self._id("confirm"),
            disabled=self.used,
        )
        cancel: discord.ui.Button = discord.ui.Button(
            label="Отмена",
            style=discord.ButtonStyle.secondary,
            custom_id=self._id("cancel"),
            disabled=self.used,
        )
        confirm.callback = self._on_confirm_click  # type: ignore[method-assign]
        cancel.callback = self._on_cancel_click  # type: ignore[method-assign]
        container = compose(
            accent=self.accent,
            heading_text="Подтверждение",
            summary=self.summary,
            details=(discord.ui.TextDisplay(f"**Кто запросил:** {self.actor}"),),
            actions=(discord.ui.ActionRow(confirm, cancel),),
        )
        self.add_item(container)
        debug_validate_layout(self)

    async def _on_confirm_click(self, interaction: discord.Interaction) -> None:
        await self.handle_confirm(interaction)

    async def _on_cancel_click(self, interaction: discord.Interaction) -> None:
        async with _CONFIRM_LOCKS.lock((self.domain, self.object_id, self.token)):
            if self._consumed or self.used:
                await reject_already_done(interaction)
                return
            self._consumed = True
            self.used = True
        await _replace_nav_view(interaction, self._clone(used=True))
        await respond_spec(
            interaction,
            unchanged(
                code="UI.CONFIRM_CANCELLED",
                title="Действие отменено",
                summary="Подтверждение снято. Изменения не применены.",
                next_step="Если действие всё же нужно — запустите его заново.",
                reference=current_reference(),
            ),
            ephemeral=True,
        )

    async def handle_confirm(self, interaction: discord.Interaction) -> None:
        async with _CONFIRM_LOCKS.lock((self.domain, self.object_id, self.token)):
            if self._consumed or self.used:
                await reject_already_done(interaction)
                return
            self._consumed = True
            self.used = True
        await _replace_nav_view(interaction, self._clone(used=True))
        await self.on_confirm(interaction)


class _NavButton(
    discord.ui.DynamicItem,
    template=_DYNAMIC_TEMPLATE,
):
    def __init__(
        self,
        item: discord.ui.Button,
        parsed: CustomId | None,
    ) -> None:
        super().__init__(item)
        self.parsed = parsed

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> _NavButton:
        try:
            parsed = parse_custom_id(str(item.custom_id or ""), allow_stale=True)
        except CustomIdError:
            parsed = None
        return cls(item, parsed)

    async def callback(self, interaction: discord.Interaction) -> None:
        await dispatch_dynamic_click(interaction, self.parsed, selected=())


class _NavSelect(
    discord.ui.DynamicItem,
    template=_DYNAMIC_TEMPLATE,
):
    def __init__(
        self,
        item: discord.ui.Select,
        parsed: CustomId | None,
    ) -> None:
        super().__init__(item)
        self.parsed = parsed

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Select,
        match: re.Match[str],
    ) -> _NavSelect:
        try:
            parsed = parse_custom_id(str(item.custom_id or ""), allow_stale=True)
        except CustomIdError:
            parsed = None
        return cls(item, parsed)

    async def callback(self, interaction: discord.Interaction) -> None:
        await dispatch_dynamic_click(
            interaction, self.parsed, selected=_selected_values(interaction)
        )


async def dispatch_dynamic_click(
    interaction: discord.Interaction,
    parsed: CustomId | None,
    *,
    selected: Sequence[str],
) -> None:
    bind_interaction(interaction, source="component")
    if parsed is None or parsed.version != CURRENT_VERSION:
        await reject_stale_button(interaction)
        return
    if parsed.action in {"confirm", "cancel"}:
        if parsed.used() or parsed.action == "cancel":
            await reject_already_done(interaction)
            return
        await reject_stale_button(interaction)
        return
    if await reject_if_blocked(interaction, parsed):
        return
    tab_source = _TAB_SOURCES.get(parsed.domain)
    if parsed.action == "tab" or (
        parsed.action in NAV_ACTIONS
        and tab_source is not None
        and parsed.domain not in _PAGE_SOURCES
    ):
        if tab_source is None:
            await reject_stale_button(interaction)
            return
        if not await tab_source.allowed(interaction, parsed):
            await reject_forbidden(interaction)
            return
        tab = selected[0] if selected else parsed.param("t")
        page = 1 if parsed.action == "tab" else parsed.page()
        getter = getattr(tab_source, "get_lines", None)
        if parsed.action in NAV_ACTIONS and callable(getter):
            lines = await getter(interaction, parsed, tab)
            page = apply_page_action(parsed.action, parsed.page(), len(paginate_lines(list(lines))))
        tab_view = await build_tabbed_panel(
            interaction,
            domain=parsed.domain,
            object_id=parsed.object_id,
            tab=tab,
            page=page,
            owner_id=parsed.owner_id(),
        )
        await _replace_nav_view(interaction, tab_view)
        return
    source_page = _PAGE_SOURCES.get(parsed.domain)
    if source_page is None:
        await reject_stale_button(interaction)
        return
    if not await source_page.allowed(interaction, parsed):
        await reject_forbidden(interaction)
        return
    state = state_from_id(parsed)
    if parsed.action == "filter" and selected:
        state = apply_filter_to_state(state, parsed.param("k", "f"), selected[0])
    elif parsed.action in NAV_ACTIONS:
        lines = await source_page.get_lines(interaction, parsed)
        pages = paginate_lines(list(lines))
        state = PageState(
            page=apply_page_action(parsed.action, state.page, len(pages)),
            filter=state.filter,
            sort=state.sort,
            owner_id=state.owner_id,
            extra=state.extra,
        )
    lines = await source_page.get_lines(
        interaction, parsed.with_params(**state.as_params())
    )
    page_view = Paginator(
        domain=parsed.domain,
        object_id=parsed.object_id,
        lines=lines,
        state=state,
        title=source_page.title(parsed),
    )
    await _replace_nav_view(interaction, page_view)


def _selected_values(interaction: discord.Interaction) -> list[str]:
    data = getattr(interaction, "data", None)
    if isinstance(data, Mapping):
        values = data.get("values")
        if values:
            return [str(item) for item in values]
    raw = getattr(interaction, "values", None)
    if raw:
        return [str(item) for item in raw]
    return []


def register_navigation(client: discord.Client) -> None:
    """Allow persistent nav custom_ids to work after a process restart."""

    client.add_dynamic_items(_NavButton, _NavSelect)


def _debug_source_domains() -> tuple[str, ...]:
    return tuple(sorted(_PAGE_SOURCES))
