"""Slash surface for persistent panels: list, deploy, redeploy and repair."""

from __future__ import annotations

from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.panels import (
    PanelChangeResult,
    PanelError,
    autocomplete_panel_keys,
    deploy_panel,
    format_panel_lines,
    list_panels,
    panel_definition,
    repair_panels,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.messages import followup_spec, respond_spec
from ui.templates import admin_receipt, notice, system_error
from utils.access import requires
from utils.discord_lookup import get_text_channel


def _error_spec(exc: PanelError) -> MessageSpec:
    return system_error(
        code=exc.code,
        summary=str(exc),
        retryable=False,
        reference=current_reference(),
    )


def _not_found() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(response, {"code": 10008, "message": "Unknown Message"})


def _resolve_channel(guild: discord.Guild, key: str):
    return get_text_channel(guild, key)


def _fetch_message(guild: discord.Guild):
    async def fetch(channel_id: int, message_id: int):
        channel = guild.get_channel(channel_id)
        getter = getattr(channel, "fetch_message", None)
        if channel is None or not callable(getter):
            raise _not_found()
        return await getter(message_id)

    return fetch


class PanelsCog(commands.Cog):
    panels = app_commands.Group(
        name="panels",
        description="Управление постоянными панелями",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @panels.command(name="list", description="Показать состояние постоянных панелей")
    @requires(Permission.CONFIG_MANAGE)
    async def list_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="PANELS.GUILD_ONLY",
                    summary="Список панелей доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        items = await list_panels(
            guild.id,
            resolve_channel=lambda key: _resolve_channel(guild, key),
            fetch_message=_fetch_message(guild),
        )
        await respond_spec(
            interaction,
            notice(
                code="PANELS.LIST",
                title="Постоянные панели",
                summary="Состояние панелей текущего сервера. Секреты не показываются.",
                details=tuple(
                    (item.key, line.split("\n", 1)[-1])
                    for item, line in zip(items, format_panel_lines(items), strict=True)
                ),
                next_step=(
                    f"Развернуть одну панель: {cmd('panels deploy')}. "
                    f"Починить расхождения: {cmd('panels repair')}."
                ),
                reference=current_reference(),
            ),
            ephemeral=True,
        )

    @panels.command(name="deploy", description="Развернуть одну постоянную панель")
    @app_commands.describe(панель="Ключ панели из реестра")
    @requires(Permission.CONFIG_MANAGE)
    async def deploy_command(
        self, interaction: discord.Interaction, панель: str
    ) -> None:
        await self._deploy(interaction, панель, title="Панель развёрнута")

    @deploy_command.autocomplete("панель")
    async def deploy_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        del interaction
        return [
            app_commands.Choice(name=key, value=key)
            for key in autocomplete_panel_keys(current)
        ]

    @panels.command(name="redeploy", description="Пересобрать одну постоянную панель")
    @app_commands.describe(панель="Ключ панели из реестра")
    @requires(Permission.CONFIG_MANAGE)
    async def redeploy_command(
        self, interaction: discord.Interaction, панель: str
    ) -> None:
        await self._deploy(interaction, панель, title="Панель пересобрана")

    @redeploy_command.autocomplete("панель")
    async def redeploy_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        del interaction
        return [
            app_commands.Choice(name=key, value=key)
            for key in autocomplete_panel_keys(current)
        ]

    @panels.command(name="repair", description="Починить расхождения постоянных панелей")
    @requires(Permission.CONFIG_MANAGE)
    async def repair_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="PANELS.GUILD_ONLY",
                    summary="Починка панелей доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            results = await repair_panels(
                guild,
                resolve_channel=lambda key: _resolve_channel(guild, key),
                fetch_message=_fetch_message(guild),
            )
        except PanelError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        changed = [item for item in results if item.changed]
        await followup_spec(interaction, _repair_spec(results, changed))

    async def _deploy(
        self,
        interaction: discord.Interaction,
        key: str,
        *,
        title: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="PANELS.GUILD_ONLY",
                    summary="Управление панелями доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            panel_definition(key)
            result = await deploy_panel(guild, key)
        except PanelError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            admin_receipt(
                code="PANELS.DEPLOY",
                title=title,
                actor=interaction.user.display_name,
                changed=result.title,
                after=result.detail,
                reference=current_reference(),
            ),
        )


def _repair_spec(
    results: tuple[PanelChangeResult, ...],
    changed: list[PanelChangeResult],
) -> MessageSpec:
    if not changed:
        return notice(
            code="PANELS.REPAIR_EMPTY",
            title="Починка панелей",
            summary="Расхождений, которые можно починить без удаления сообщений, нет.",
            next_step=f"Состояние панелей: {cmd('panels list')}.",
            reference=current_reference(),
        )
    details = tuple((item.key, item.detail) for item in changed)
    return notice(
        code="PANELS.REPAIR",
        title="Починка панелей",
        summary=(
            f"Исправлено панелей: {len(changed)}. "
            "Существующие сообщения не удалялись."
        ),
        details=details,
        next_step=f"Проверьте результат: {cmd('panels list')}.",
        reference=current_reference(),
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelsCog(bot))
