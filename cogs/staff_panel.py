"""Ephemeral senior-staff panel with the six operational sections."""

from __future__ import annotations

from collections.abc import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.staff_panel import (
    SECTION_ORDER,
    staff_section_lines,
    staff_tabs,
    visible_staff_keys,
)
from ui.messages import respond_spec, respond_v2
from ui.navigation import (
    Tab,
    build_tabbed_panel,
    register_tab_source,
    unregister_sources,
)
from ui.pagination import paginate_lines
from ui.templates import missing_permission, system_error
from utils.access import requires

_DOMAIN = "staff"


async def _can_open(interaction: discord.Interaction) -> bool:
    if interaction.guild_id is None:
        return False
    keys = await visible_staff_keys(interaction.user)
    return bool(keys)


class StaffTabSource:
    def tabs(self, parsed) -> Sequence[Tab]:
        del parsed
        return staff_tabs(SECTION_ORDER)

    async def get_lines(self, interaction: discord.Interaction, parsed, tab: str):
        del parsed
        guild_id = interaction.guild_id
        if guild_id is None:
            return ["Панель доступна только на сервере."]
        visible = await visible_staff_keys(interaction.user)
        if tab not in visible:
            tab = visible[0] if visible else tab
        return list(await staff_section_lines(interaction.user, guild_id, tab))

    async def render(self, interaction: discord.Interaction, parsed, tab: str) -> str:
        lines = await self.get_lines(interaction, parsed, tab)
        return paginate_lines(list(lines))[0]

    async def allowed(self, interaction: discord.Interaction, parsed) -> bool:
        del parsed
        return await _can_open(interaction)

    def title(self, parsed) -> str:
        del parsed
        return "Панель старшего состава"


class StaffPanelCog(commands.Cog):
    staff = app_commands.Group(
        name="staff",
        description="Панель старшего состава",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        register_tab_source(_DOMAIN, StaffTabSource())

    async def cog_unload(self) -> None:
        unregister_sources(_DOMAIN)

    @staff.command(name="panel", description="Требует решения, состав, план, выплаты, снабжение, развитие")
    @requires(Permission.QUEUE_ASSIGN)
    async def panel_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="STAFF.GUILD_ONLY",
                    summary="Панель старшего состава доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        visible = await visible_staff_keys(interaction.user)
        if not visible:
            from core.permissions import evaluate_access

            decision = await evaluate_access(interaction.user, Permission.QUEUE_ASSIGN)
            await respond_spec(
                interaction,
                missing_permission(decision, reference=current_reference()),
                ephemeral=True,
            )
            return
        panel = await build_tabbed_panel(
            interaction,
            domain=_DOMAIN,
            object_id=str(guild.id),
            tab=visible[0],
            owner_id=interaction.user.id,
            can_use=_can_open,
            fallback_tabs=staff_tabs(visible),
            fallback_title="Панель старшего состава",
        )
        await respond_v2(interaction, view=panel, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffPanelCog(bot))
