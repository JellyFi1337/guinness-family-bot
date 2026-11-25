"""Slash surface for live settings: view, change, reset, diff and export."""

from __future__ import annotations

import io
from collections.abc import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.config_panel import (
    ConfigError,
    SettingChangeResult,
    SettingDiff,
    assert_config_manage,
    autocomplete_keys,
    diff_settings,
    export_settings,
    format_setting_lines,
    group_label,
    known_groups,
    list_settings,
    reset_setting,
    set_setting,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.messages import followup_spec, followup_v2, respond_spec, respond_v2
from ui.navigation import (
    Tab,
    build_tabbed_panel,
    register_tab_source,
    unregister_sources,
)
from ui.pagination import paginate_lines
from ui.templates import admin_receipt, notice, system_error
from utils.access import requires

_CONFIG_DOMAIN = "config"


def _error_spec(exc: ConfigError) -> MessageSpec:
    return system_error(
        code=exc.code,
        summary=str(exc),
        retryable=False,
        reference=current_reference(),
    )


def _group_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=group_label(group), value=group)
        for group in known_groups()
    ]


def _tabs() -> tuple[Tab, ...]:
    return tuple(Tab(group, group_label(group)) for group in known_groups())


async def _can_manage(interaction: discord.Interaction) -> bool:
    try:
        await assert_config_manage(interaction.user)
    except ConfigError:
        return False
    return True


class ConfigTabSource:
    def tabs(self, parsed) -> Sequence[Tab]:
        del parsed
        return _tabs()

    async def get_lines(self, interaction: discord.Interaction, parsed, tab: str):
        del parsed
        guild_id = interaction.guild_id
        if guild_id is None:
            return ["Настройки доступны только на сервере."]
        views = await list_settings(guild_id, group=tab)
        return format_setting_lines(views) or [
            "В этой группе нет видимых настроек."
        ]

    async def render(
        self,
        interaction: discord.Interaction,
        parsed,
        tab: str,
    ) -> str:
        lines = await self.get_lines(interaction, parsed, tab)
        return paginate_lines(list(lines))[0]

    async def allowed(self, interaction: discord.Interaction, parsed) -> bool:
        del parsed
        return await _can_manage(interaction)

    def title(self, parsed) -> str:
        del parsed
        return "Настройки бота"


class ConfigPanelCog(commands.Cog):
    config = app_commands.Group(
        name="config",
        description="Просмотр и изменение настроек без перезапуска",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        register_tab_source(_CONFIG_DOMAIN, ConfigTabSource())

    async def cog_unload(self) -> None:
        unregister_sources(_CONFIG_DOMAIN)

    @config.command(name="show", description="Показать настройки группы")
    @app_commands.describe(группа="Группа настроек")
    @app_commands.choices(группа=_group_choices())
    @requires(Permission.CONFIG_MANAGE)
    async def show_command(
        self,
        interaction: discord.Interaction,
        группа: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="CONFIG.GUILD_ONLY",
                    summary="Настройки доступны только на сервере. Ничего не показано.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        active = группа or known_groups()[0]
        try:
            panel = await build_tabbed_panel(
                interaction,
                domain=_CONFIG_DOMAIN,
                object_id=str(guild.id),
                tab=active,
                owner_id=interaction.user.id,
                can_use=_can_manage,
                fallback_tabs=_tabs(),
                fallback_title="Настройки бота",
            )
        except ConfigError as exc:
            await respond_spec(interaction, _error_spec(exc), ephemeral=True)
            return
        await respond_v2(interaction, view=panel, ephemeral=True)

    @config.command(name="set", description="Изменить настройку без перезапуска")
    @app_commands.describe(ключ="Ключ настройки", значение="Новое значение")
    @requires(Permission.CONFIG_MANAGE)
    async def set_command(
        self,
        interaction: discord.Interaction,
        ключ: str,
        значение: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="CONFIG.GUILD_ONLY",
                    summary="Настройки доступны только на сервере. Значение не записано.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        try:
            result = await set_setting(
                guild.id,
                ключ,
                значение,
                actor_id=interaction.user.id,
                actor_name=interaction.user.display_name,
                guild=guild,
            )
        except ConfigError as exc:
            await respond_spec(interaction, _error_spec(exc), ephemeral=True)
            return
        await respond_spec(
            interaction,
            _change_spec(result, interaction.user.display_name),
            ephemeral=True,
        )

    @set_command.autocomplete("ключ")
    async def set_key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        del interaction
        return [
            app_commands.Choice(name=key, value=key)
            for key in autocomplete_keys(current, writable=True)
        ]

    @config.command(name="reset", description="Вернуть настройку к файлу или умолчанию")
    @app_commands.describe(ключ="Ключ настройки")
    @requires(Permission.CONFIG_MANAGE)
    async def reset_command(
        self,
        interaction: discord.Interaction,
        ключ: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="CONFIG.GUILD_ONLY",
                    summary="Настройки доступны только на сервере. Сброс не выполнен.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        try:
            result = await reset_setting(
                guild.id,
                ключ,
                actor_id=interaction.user.id,
                actor_name=interaction.user.display_name,
            )
        except ConfigError as exc:
            await respond_spec(interaction, _error_spec(exc), ephemeral=True)
            return
        await respond_spec(
            interaction,
            _change_spec(result, interaction.user.display_name, reset=True),
            ephemeral=True,
        )

    @reset_command.autocomplete("ключ")
    async def reset_key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        del interaction
        return [
            app_commands.Choice(name=key, value=key)
            for key in autocomplete_keys(current, writable=True)
        ]

    @config.command(name="diff", description="Показать расхождения файла и базы")
    @requires(Permission.CONFIG_MANAGE)
    async def diff_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="CONFIG.GUILD_ONLY",
                    summary="Сравнение настроек доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        try:
            diffs = await diff_settings(guild.id)
        except ConfigError as exc:
            await respond_spec(interaction, _error_spec(exc), ephemeral=True)
            return
        await respond_spec(interaction, _diff_spec(diffs), ephemeral=True)

    @config.command(name="export", description="Выгрузить актуальные настройки файлом")
    @requires(Permission.CONFIG_MANAGE)
    async def export_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="CONFIG.GUILD_ONLY",
                    summary="Выгрузка настроек доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            text = await export_settings(guild.id)
        except ConfigError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await deliver_settings_export(
            interaction,
            text,
            summary="Актуальные значения выгружены. Секреты в файл не попали.",
        )


def _change_spec(
    result: SettingChangeResult,
    actor: str,
    *,
    reset: bool = False,
) -> MessageSpec:
    if not result.changed:
        return notice(
            code="CONFIG.UNCHANGED",
            title="Настройка не изменилась",
            summary=(
                f"`{result.key}` уже имеет значение `{result.after}`. "
                "Запись в журнал не нужна."
            ),
            next_step=f"Проверить группу: {cmd('config show')}.",
            reference=current_reference(),
        )
    title = "Настройка сброшена" if reset else "Настройка изменена"
    return admin_receipt(
        code="CONFIG.RESET" if reset else "CONFIG.SET",
        title=title,
        actor=actor,
        changed=result.key,
        before=result.before,
        after=result.after,
        reference=current_reference(),
    )


def _diff_spec(diffs: tuple[SettingDiff, ...]) -> MessageSpec:
    if not diffs:
        return notice(
            code="CONFIG.DIFF_EMPTY",
            title="Расхождения с файлом",
            summary="Значения в базе совпадают с bot_config.toml.",
            next_step=f"Выгрузить снимок: {cmd('config export')}.",
            reference=current_reference(),
        )
    details = tuple(
        (item.key, f"{item.file_text} → {item.database_text}") for item in diffs
    )
    return notice(
        code="CONFIG.DIFF",
        title="Расхождения с файлом",
        summary=(
            "В базе есть значения, которых нет в bot_config.toml. "
            "Перенесите нужные строки в файл, чтобы они пережили сброс."
        ),
        details=details,
        next_step=f"Снимок актуальных значений: {cmd('config export')}.",
        reference=current_reference(),
    )


async def deliver_settings_export(
    interaction: discord.Interaction,
    text: str,
    *,
    summary: str,
) -> None:
    payload = discord.File(
        io.BytesIO(text.encode("utf-8")),
        filename="bot_config.toml",
    )
    spec = notice(
        code="CONFIG.EXPORT",
        title="Выгрузка настроек",
        summary=summary,
        next_step="Перенесите нужные строки в bot_config.toml.",
        reference=current_reference(),
    )
    await followup_spec(interaction, spec)
    await followup_v2(interaction, spec, file=payload, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ConfigPanelCog(bot))
