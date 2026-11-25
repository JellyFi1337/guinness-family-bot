"""Public entry and ephemeral /me panel for a roster member."""

from __future__ import annotations

from collections.abc import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from services.member_panel import (
    SECTION_LABELS,
    MemberPanel,
    PanelSection,
    build_member_panel,
    confirm_event_ready,
    decode_visible_tabs,
    empty_section_container,
    public_me_spec,
)
from services.membership import MembershipError
from services.static_messages import StaticPayload, upsert_static_message
from ui.command_registry import cmd
from ui.custom_ids import CURRENT_VERSION, CustomId
from ui.design import Accent, compose, debug_validate_layout
from ui.layouts import ModernLayoutView
from ui.message_specs import MessageSpec
from ui.messages import followup_spec, followup_v2, respond_spec
from ui.navigation import (
    Tab,
    TabbedPanel,
    _replace_nav_view,
    _selected_values,
    register_tab_source,
    unregister_sources,
)
from ui.pagination import paginate_lines
from ui.templates import system_error
from utils.views import SafeView

_DOMAIN = "me"
_OPEN_ID = "me_open_btn"


def _error_spec(exc: MembershipError) -> MessageSpec:
    return system_error(
        code=exc.code,
        summary=str(exc),
        retryable=exc.retryable,
        reference=current_reference(),
    )


def _guild_only_spec() -> MessageSpec:
    return system_error(
        code="ME.GUILD_ONLY",
        summary=(
            "Личная панель доступна только на сервере. Данные не показаны. "
            f"Вызовите {cmd('me')} в канале семьи."
        ),
        retryable=False,
        reference=current_reference(),
    )


def _tabs_for(keys: Sequence[str]) -> tuple[Tab, ...]:
    return tuple(Tab(key, SECTION_LABELS[key]) for key in keys if key in SECTION_LABELS)


def _ready_custom_id(object_id: str, owner_id: int, event_id: str) -> str:
    return CustomId(
        version=CURRENT_VERSION,
        domain=_DOMAIN,
        action="ready",
        object_id=object_id,
        params=tuple(
            sorted((("e", event_id), ("o", str(owner_id))))
        ),
    ).render()


async def _snapshot(
    interaction: discord.Interaction,
) -> MemberPanel | MembershipError | None:
    guild_id = interaction.guild_id
    if guild_id is None:
        return None
    try:
        return await build_member_panel(guild_id, interaction.user.id)
    except MembershipError as exc:
        return exc


class MeTabSource:
    def tabs(self, parsed) -> Sequence[Tab]:
        return _tabs_for(decode_visible_tabs(parsed.object_id))

    async def get_lines(self, interaction: discord.Interaction, parsed, tab: str):
        del parsed
        panel = await _snapshot(interaction)
        if not isinstance(panel, MemberPanel):
            return ["Личная панель недоступна. Откройте её заново."]
        visible = panel.visible_keys
        if tab not in visible:
            tab = visible[0] if visible else tab
        section = panel.section(tab)
        if section.empty:
            return [section.hint, section.next_step]
        return list(section.lines)

    async def render(self, interaction: discord.Interaction, parsed, tab: str) -> str:
        lines = await self.get_lines(interaction, parsed, tab)
        return paginate_lines(list(lines))[0]

    async def allowed(self, interaction: discord.Interaction, parsed) -> bool:
        owner_id = parsed.owner_id()
        if owner_id is not None and interaction.user.id != owner_id:
            return False
        panel = await _snapshot(interaction)
        return isinstance(panel, MemberPanel)

    def title(self, parsed) -> str:
        del parsed
        return "Моя панель"


class MeTabbedPanel(TabbedPanel):
    """Filled personal tab. Empty tabs rebuild through empty_state."""

    def __init__(
        self,
        *args,
        ready_item_id: str | None = None,
        **kwargs,
    ) -> None:
        self.ready_item_id = ready_item_id
        super().__init__(*args, **kwargs)
        if ready_item_id:
            ready = discord.ui.Button(
                label="Подтвердить готовность",
                style=discord.ButtonStyle.primary,
                custom_id=_ready_custom_id(
                    self.object_id, self.owner_id or 0, ready_item_id
                ),
            )
            ready.callback = self._on_ready  # type: ignore[method-assign]
            self.add_item(
                compose(
                    accent=Accent.EVENTS,
                    actions=(discord.ui.ActionRow(ready),),
                    footnote="Подтверждение видите только вы.",
                )
            )
            debug_validate_layout(self)

    async def _on_tab(self, interaction: discord.Interaction) -> None:
        chosen = _selected_values(interaction)
        tab = chosen[0] if chosen else self.active
        await _show_personal(interaction, tab=tab, replace=True)

    async def _handle_nav(self, interaction: discord.Interaction, action: str) -> None:
        from ui.navigation import apply_page_action

        await _show_personal(
            interaction,
            tab=self.active,
            page=apply_page_action(action, self.page, self.page_count),
            replace=True,
        )

    async def _on_ready(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None or not self.ready_item_id:
            await respond_spec(
                interaction,
                system_error(
                    code="ME.READY_GONE",
                    summary=(
                        "Подтверждать уже нечего. Панель не меняла чужие данные. "
                        f"Откройте {cmd('me')} заново, если событие ещё актуально."
                    ),
                    retryable=True,
                    reference=current_reference(),
                ),
            )
            return
        confirm_event_ready(guild_id, interaction.user.id, self.ready_item_id)
        await _show_personal(interaction, tab=self.active, replace=True)


class MeEmptyView(ModernLayoutView):
    """Available but empty section: empty_state from 2.1 plus the tab select."""

    def __init__(
        self,
        section: PanelSection,
        *,
        tabs: Sequence[Tab],
        object_id: str,
        owner_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.active = section.key
        self.add_item(empty_section_container(section))
        select: discord.ui.Select[discord.ui.LayoutView] = discord.ui.Select(
            placeholder="Раздел",
            custom_id=CustomId(
                version=CURRENT_VERSION,
                domain=_DOMAIN,
                action="tab",
                object_id=object_id,
                params=tuple(
                    sorted(
                        (
                            ("o", str(owner_id)),
                            ("p", "1"),
                            ("t", section.key),
                        )
                    )
                ),
            ).render(),
            options=[
                discord.SelectOption(
                    label=f"• {tab.label}" if tab.key == section.key else tab.label,
                    value=tab.key,
                    default=tab.key == section.key,
                )
                for tab in tabs
            ],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_tab  # type: ignore[method-assign]
        self.add_item(
            compose(
                accent=Accent.SERVICE,
                actions=(discord.ui.ActionRow(select),),
                footnote="Эти сведения видите только вы.",
            )
        )
        debug_validate_layout(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await respond_spec(
                interaction,
                system_error(
                    code="ME.FOREIGN",
                    summary=(
                        "Это чужая личная панель. Данные не показаны; "
                        f"откройте свою через {cmd('me')}."
                    ),
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return False
        return True

    async def _on_tab(self, interaction: discord.Interaction) -> None:
        chosen = _selected_values(interaction)
        tab = chosen[0] if chosen else self.active
        await _show_personal(interaction, tab=tab, replace=True)


def build_me_view(
    panel: MemberPanel,
    *,
    active: str,
    owner_id: int,
    page: int = 1,
) -> discord.ui.LayoutView:
    section = panel.section(active)
    tabs = _tabs_for(panel.visible_keys)
    object_id = panel.object_id()
    if section.empty:
        return MeEmptyView(
            section, tabs=tabs, object_id=object_id, owner_id=owner_id
        )
    pages = paginate_lines(list(section.lines))
    page_count = max(1, len(pages))
    safe_page = ((page - 1) % page_count) + 1
    body = pages[safe_page - 1] if pages else ""
    return MeTabbedPanel(
        domain=_DOMAIN,
        object_id=object_id,
        tabs=tabs,
        active=section.key,
        body=body,
        title=section.label,
        owner_id=owner_id,
        accent=Accent.MEMBER,
        page=safe_page,
        page_count=page_count,
        ready_item_id=section.ready_item_id,
    )


async def _show_personal(
    interaction: discord.Interaction,
    *,
    tab: str,
    page: int = 1,
    replace: bool,
) -> None:
    panel = await _snapshot(interaction)
    if panel is None:
        spec = _guild_only_spec()
        if replace:
            await respond_spec(interaction, spec)
        else:
            await followup_spec(interaction, spec)
        return
    if isinstance(panel, MembershipError):
        spec = _error_spec(panel)
        if replace:
            await respond_spec(interaction, spec)
        else:
            await followup_spec(interaction, spec)
        return
    view = build_me_view(
        panel, active=tab, owner_id=interaction.user.id, page=page
    )
    if replace:
        await _replace_nav_view(interaction, view)
        return
    await followup_v2(interaction, None, view=view)


async def open_personal_panel(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    panel = await _snapshot(interaction)
    if panel is None:
        await followup_spec(interaction, _guild_only_spec())
        return
    if isinstance(panel, MembershipError):
        await followup_spec(interaction, _error_spec(panel))
        return
    await followup_v2(
        interaction,
        None,
        view=build_me_view(
            panel, active=panel.visible[0].key, owner_id=interaction.user.id
        ),
    )


class PersistentMeButton(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Открыть мою панель",
        style=discord.ButtonStyle.primary,
        custom_id=_OPEN_ID,
    )
    async def open_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await open_personal_panel(interaction)


async def upsert_me_panel(channel: discord.TextChannel) -> None:
    spec = public_me_spec()

    def build() -> StaticPayload:
        return StaticPayload(embed=spec, view=PersistentMeButton(), revision=1)

    await upsert_static_message(
        guild=channel.guild,
        channel=channel,
        panel_key="me",
        build=build,
    )


class MemberPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        from services.contribution import ensure_contribution_integrations

        ensure_contribution_integrations()
        register_tab_source(_DOMAIN, MeTabSource())

    async def cog_unload(self) -> None:
        unregister_sources(_DOMAIN)

    @app_commands.command(name="me", description="Открыть личную панель")
    @app_commands.guild_only()
    async def me_command(self, interaction: discord.Interaction) -> None:
        await open_personal_panel(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemberPanelCog(bot))
