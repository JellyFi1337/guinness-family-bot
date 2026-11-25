"""Slash surface for contract reports and review."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.report_review import (
    EvidenceDraft,
    ReportError,
    ensure_report_integrations,
    report_card,
    reverse_report,
    review_report,
    show_family_profit,
    show_report,
    submit_report,
)
from ui.message_specs import MessageSpec
from ui.messages import followup_spec
from ui.templates import missing_permission, system_error
from utils.access import requires


def _error_spec(exc: ReportError) -> MessageSpec:
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


class ReportsCog(commands.Cog):
    report = app_commands.Group(
        name="report",
        description="Отчёты по контрактам и проверка результата",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        ensure_report_integrations()

    @report.command(name="submit", description="Сдать отчёт по запуску")
    @app_commands.describe(
        run_id="Номер запуска",
        result="Результат группы, целое число",
        shares="Доли discord_id:сумма через запятую",
        duration="Затраченное время в минутах",
        volumes="Фактические объёмы через запятую",
        links="Ссылки-подтверждения через запятую",
        note="Комментарий",
        disputed="Спорный результат — нужна подробная форма",
        attachment="Вложение; хеш хранится, ссылка Discord не считается доказательством",
        purchases="Закупки имя:количество:цена через запятую",
        expenses="Расходы статья:сумма через запятую",
        cash="Выплата деньгами",
        items="Предметы имя:количество:оценка через запятую; это оценка, не деньги",
        reputation="Изменение репутации, отдельно от денег",
    )
    async def submit_command(
        self,
        interaction: discord.Interaction,
        run_id: int,
        result: int,
        shares: str,
        duration: int,
        volumes: str | None = None,
        links: str | None = None,
        note: str | None = None,
        disputed: bool = False,
        attachment: discord.Attachment | None = None,
        purchases: str | None = None,
        expenses: str | None = None,
        cash: int | None = None,
        items: str | None = None,
        reputation: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="REPORTS.GUILD_ONLY",
                    summary="Отчёт сдаётся только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        drafts: list[EvidenceDraft] = []
        if attachment is not None:
            payload = await attachment.read()
            drafts.append(
                EvidenceDraft(
                    kind="attachment",
                    payload=payload,
                    url=attachment.url,
                )
            )
        try:
            report = await submit_report(
                interaction.user,
                guild_id,
                run_id,
                result_amount=result,
                shares=shares,
                duration_minutes=duration,
                volumes=volumes,
                links=links,
                note=note,
                disputed=disputed,
                attachments=tuple(drafts),
                purchases=purchases,
                expenses=expenses,
                cash=cash,
                items=items,
                reputation=reputation,
            )
        except ReportError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, await report_card(report, interaction.user))

    @report.command(name="review", description="Принять, вернуть или отклонить отчёт")
    @app_commands.describe(
        report_id="Номер отчёта",
        decision="accept, rework или reject",
        reason="Причина решения",
    )
    @app_commands.choices(
        decision=[
            app_commands.Choice(name="accept", value="accept"),
            app_commands.Choice(name="rework", value="rework"),
            app_commands.Choice(name="reject", value="reject"),
        ]
    )
    @requires(Permission.REPORTS_REVIEW)
    async def review_command(
        self,
        interaction: discord.Interaction,
        report_id: int,
        decision: str,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="REPORTS.GUILD_ONLY",
                    summary="Проверка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            report = await review_report(
                interaction.user, guild_id, report_id, decision, reason=reason
            )
        except ReportError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, await report_card(report, interaction.user))

    @report.command(name="reverse", description="Отменить принятый отчёт без правки чисел")
    @app_commands.describe(report_id="Номер отчёта", reason="Причина отмены")
    @requires(Permission.REPORTS_REVIEW)
    async def reverse_command(
        self,
        interaction: discord.Interaction,
        report_id: int,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="REPORTS.GUILD_ONLY",
                    summary="Отмена доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            report = await reverse_report(
                interaction.user, guild_id, report_id, reason=reason
            )
        except ReportError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, await report_card(report, interaction.user))

    @report.command(name="stats", description="Сводка прибыли по типам контрактов")
    @requires(Permission.REPORTS_REVIEW)
    async def stats_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="REPORTS.GUILD_ONLY",
                    summary="Сводка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            spec = await show_family_profit(interaction.user, guild_id)
        except ReportError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, spec)

    @report.command(name="show", description="Карточка отчёта")
    @app_commands.describe(report_id="Номер отчёта")
    async def show_command(
        self, interaction: discord.Interaction, report_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="REPORTS.GUILD_ONLY",
                    summary="Карточка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            report = await show_report(interaction.user, guild_id, report_id)
        except ReportError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, await report_card(report, interaction.user))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReportsCog(bot))
