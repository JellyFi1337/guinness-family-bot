"""Slash surface for period bonus proposals and approval."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.bonus import (
    BonusError,
    adjust_share,
    approve_period,
    build_proposal,
    declare_period,
    member_breakdown_lines,
    member_card_spec,
    member_view,
    period_card_spec,
    preview_period,
    ranks_card_spec,
    ranks_view,
    show_period,
)
from ui.message_specs import MessageSpec
from ui.messages import followup_spec
from ui.templates import missing_permission, system_error
from utils.access import requires


def _error_spec(exc: BonusError) -> MessageSpec:
    if exc.decision is not None:
        return missing_permission(exc.decision, reference=current_reference())
    return system_error(
        code=exc.code,
        summary=str(exc),
        retryable=exc.retryable,
        reference=current_reference(),
    )


def _guild_id(interaction: discord.Interaction) -> int | None:
    return interaction.guild_id


class BonusCog(commands.Cog):
    bonus = app_commands.Group(
        name="bonus",
        description="Премия по итогам периода",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @bonus.command(name="declare", description="Объявить премиальный фонд периода")
    @app_commands.describe(
        period_key="Ключ периода, например 2026-W36",
        fund="Боевая строка в игровых деньгах",
        mentor_fund="Отдельная строка для наставников",
        logistics_fund="Отдельная строка для логистов",
        starts_at="Начало периода, если не неделя",
        ends_at="Конец периода, если не неделя",
    )
    @requires(Permission.TREASURY_APPROVE)
    async def declare_command(
        self,
        interaction: discord.Interaction,
        period_key: str,
        fund: int,
        mentor_fund: int = 0,
        logistics_fund: int = 0,
        starts_at: str | None = None,
        ends_at: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="BONUS.GUILD_ONLY",
                    summary="Премию объявляют только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            period = await declare_period(
                interaction.user,
                guild_id,
                period_key=period_key,
                fund=fund,
                mentor_fund=mentor_fund,
                logistics_fund=logistics_fund,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            bundle = await build_proposal(guild_id, period.period_key)
        except BonusError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, period_card_spec(bundle))

    @bonus.command(name="preview", description="Предложить распределение фонда")
    @app_commands.describe(period_key="Ключ периода")
    @requires(Permission.TREASURY_VIEW)
    async def preview_command(
        self, interaction: discord.Interaction, period_key: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="BONUS.GUILD_ONLY",
                    summary="Предложение доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            bundle = await preview_period(interaction.user, guild_id, period_key)
        except BonusError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, period_card_spec(bundle))

    @bonus.command(name="adjust", description="Изменить долю до утверждения")
    @app_commands.describe(
        period_key="Ключ периода",
        member="Участник",
        pool="combat, mentoring или logistics",
        amount="Игровые деньги после правки",
    )
    @requires(Permission.TREASURY_APPROVE)
    async def adjust_command(
        self,
        interaction: discord.Interaction,
        period_key: str,
        member: discord.Member,
        pool: str,
        amount: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="BONUS.GUILD_ONLY",
                    summary="Долю меняют только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            bundle = await adjust_share(
                interaction.user,
                guild_id,
                period_key,
                member_discord_id=member.id,
                pool=pool,
                amount=amount,
            )
        except BonusError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, period_card_spec(bundle))

    @bonus.command(name="approve", description="Утвердить период и создать обязательства")
    @app_commands.describe(period_key="Ключ периода")
    @requires(Permission.TREASURY_APPROVE)
    async def approve_command(
        self, interaction: discord.Interaction, period_key: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="BONUS.GUILD_ONLY",
                    summary="Утверждение доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            bundle = await approve_period(interaction.user, guild_id, period_key)
        except BonusError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, period_card_spec(bundle))

    @bonus.command(name="show", description="Карточка премиального периода")
    @app_commands.describe(period_key="Ключ периода")
    @requires(Permission.TREASURY_VIEW)
    async def show_command(
        self, interaction: discord.Interaction, period_key: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="BONUS.GUILD_ONLY",
                    summary="Карточка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            bundle = await show_period(interaction.user, guild_id, period_key)
        except BonusError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, period_card_spec(bundle))

    @bonus.command(name="my", description="Ваши заслуги и доля премии")
    @app_commands.describe(period_key="Ключ периода, пусто — последний")
    async def my_command(
        self, interaction: discord.Interaction, period_key: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="BONUS.GUILD_ONLY",
                    summary="Личная премия доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            period, shares, categories, place = await member_view(
                interaction.user, guild_id, period_key
            )
            breakdown = await member_breakdown_lines(
                guild_id, interaction.user.id, period
            )
        except BonusError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            member_card_spec(period, shares, categories, place, breakdown),
        )

    @bonus.command(name="ranks", description="Справочник ранга по влиянию")
    async def ranks_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="BONUS.GUILD_ONLY",
                    summary="Справочник ранга доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        ranks, influence, current = await ranks_view(guild_id)
        await followup_spec(interaction, ranks_card_spec(ranks, influence, current))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BonusCog(bot))
