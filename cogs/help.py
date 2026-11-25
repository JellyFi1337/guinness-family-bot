import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission, evaluate_access
from core.profile import active_profile
from services.cooldowns import (
    command_cooldowns,
    cooldown_claim,
    cooldown_retry_text,
)
from services.help import (
    AUDIENCE_MEMBER,
    AUDIENCE_STAFF,
    build_help_spec,
    help_index_spec,
)
from ui.command_registry import cmd
from ui.messages import followup_v2, respond_spec, respond_v2
from ui.templates import missing_permission, system_error
from utils.access import is_bot_owner, staff_or_owner


class HelpCog(
    commands.GroupCog,
    group_name="help",
    group_description="Справка и настройки команд",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="overview", description="Показать справку по функциям бота")
    @app_commands.describe(аудитория="Участник или старший состав")
    @app_commands.choices(
        аудитория=[
            app_commands.Choice(name="Участник", value=AUDIENCE_MEMBER),
            app_commands.Choice(name="Старший состав", value=AUDIENCE_STAFF),
        ]
    )
    async def help(
        self,
        interaction: discord.Interaction,
        аудитория: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="HELP.GUILD_ONLY",
                    summary="Справка доступна только на сервере Guinness.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        async with cooldown_claim(
            interaction.guild.id,
            "help",
            interaction.user.id,
            bypass=is_bot_owner(interaction.user),
        ) as claim:
            if not claim.allowed:
                await respond_v2(
                    interaction,
                    cooldown_retry_text(claim.retry_after),
                    ephemeral=True,
                )
                return
            claim.keep()

        if аудитория is None:
            await respond_spec(interaction, help_index_spec(), ephemeral=True)
            return

        if аудитория.value == AUDIENCE_STAFF:
            decision = await evaluate_access(interaction.user, Permission.QUEUE_ASSIGN)
            if not decision.allowed:
                await respond_spec(
                    interaction,
                    missing_permission(decision, reference=current_reference()),
                    ephemeral=True,
                )
                return

        profile = await active_profile(interaction.guild.id)
        spec = build_help_spec(profile, аудитория.value)
        await respond_spec(interaction, spec, ephemeral=True)

    @app_commands.command(
        name="cooldown",
        description="Изменить кулдаун команды или формы помощи",
    )
    @app_commands.describe(
        команда="Команда или действие",
        секунды="Задержка от 0 до 86400 секунд; 0 отключает кулдаун",
    )
    @app_commands.choices(
        команда=[
            app_commands.Choice(name="Форма «Нужна помощь»", value="help_request"),
            app_commands.Choice(name="Справка", value="help"),
            app_commands.Choice(name="Создание события", value="event_create"),
            app_commands.Choice(name="Запуск контракта", value="contract_start"),
            app_commands.Choice(name="Обычный сбор", value="summit_list"),
        ]
    )
    @staff_or_owner()
    async def set_command_cooldown(
        self,
        interaction: discord.Interaction,
        команда: app_commands.Choice[str],
        секунды: app_commands.Range[int, 0, 86_400],
    ) -> None:
        if interaction.guild_id is None:
            await respond_v2(
                interaction,
                "Настройка доступна только на сервере.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await command_cooldowns.configure(
            interaction.guild_id,
            команда.value,
            int(секунды),
        )
        state = (
            "отключён"
            if секунды == 0
            else f"установлен на **{int(секунды)} сек.**"
        )
        await followup_v2(
            interaction,
            f"Кулдаун для **{команда.name}** {state}. "
            f"Справка: {cmd('help overview')}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
