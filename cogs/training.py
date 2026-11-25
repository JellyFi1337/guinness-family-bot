"""Mentor isolation, training tasks, clearances and promotion proposals."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.onboarding import (
    STEP_LABELS,
    OnboardingError,
    advance_track,
    assign_mentor,
    assign_training,
    complete_training,
    decide_promotion,
    grant_activity_clearance,
    list_mentees,
    propose_promotion,
    publish_rules,
    set_promotion_criteria,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.messages import followup_spec
from ui.templates import missing_permission, notice, system_error
from utils.access import requires


def _error_spec(exc: OnboardingError) -> MessageSpec:
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


class TrainingCog(commands.Cog):
    training = app_commands.Group(
        name="training",
        description="Адаптация, наставники и допуски",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @training.command(name="mentees", description="Ваши подопечные и следующий шаг")
    async def mentees_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Список подопечных доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            cards = await list_mentees(interaction.user, guild_id)
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        if not cards:
            await followup_spec(
                interaction,
                notice(
                    code="TRAINING.MENTEES_EMPTY",
                    title="Подопечных нет",
                    summary="У вас нет активных маршрутов адаптации.",
                    next_step=(
                        "Кадровик назначает наставника командой "
                        f"{cmd('training mentor')}."
                    ),
                ),
            )
            return
        details = []
        for card in cards:
            open_tasks = ", ".join(item.title for item in card.assignments) or "нет"
            details.append(
                (
                    card.member.ic_name,
                    f"<@{card.member.discord_id}> · шаг: {card.next_step} · задания: {open_tasks}",
                )
            )
        await followup_spec(
            interaction,
            notice(
                code="TRAINING.MENTEES",
                title="Ваши подопечные",
                summary="Показаны только ваши маршруты. Чужих подопечных здесь нет.",
                details=tuple(details),
                next_step=f"Следующий шаг и задания — через {cmd('training assign')}.",
            ),
        )

    @training.command(name="assign", description="Выдать задание по специализации")
    @app_commands.describe(
        member="Подопечный",
        title="Название задания",
        step="Шаг маршрута, если не текущий",
    )
    async def assign_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        title: str,
        step: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Задание можно выдать только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await assign_training(
                interaction.user,
                guild_id,
                discord_id=member.id,
                title=title,
                step_key=step,
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="TRAINING.ASSIGNED",
                title="Задание выдано",
                summary=(
                    f"«{item.title}» для {member.mention}. "
                    f"Специализация маршрута: {item.specialization}."
                ),
                next_step=f"Закройте задание командой {cmd('training complete')}.",
                reference=str(item.id),
            ),
        )

    @training.command(name="complete", description="Закрыть учебное задание")
    @app_commands.describe(assignment_id="Номер задания")
    async def complete_command(
        self, interaction: discord.Interaction, assignment_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Задание закрывается только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await complete_training(
                interaction.user, guild_id, assignment_id=assignment_id
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="TRAINING.DONE",
                title="Задание закрыто",
                summary=f"«{item.title}» отмечено выполненным.",
                next_step=f"Сдвиньте шаг маршрута через {cmd('training advance')}.",
                reference=str(item.id),
            ),
        )

    @training.command(name="advance", description="Перейти к следующему шагу адаптации")
    @app_commands.describe(member="Подопечный")
    async def advance_command(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Шаг сдвигается только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            track = await advance_track(
                interaction.user, guild_id, discord_id=member.id
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        if track.status == "completed":
            await followup_spec(
                interaction,
                notice(
                    code="TRAINING.ROUTE_DONE",
                    title="Маршрут адаптации завершён",
                    summary=(
                        f"{member.mention} прошёл все шаги. "
                        "Это ещё не членство в организации."
                    ),
                    next_step=(
                        f"Подтверждение в игре — {cmd('members verify')}. "
                        "Автоповышения нет."
                    ),
                ),
            )
            return
        await followup_spec(
            interaction,
            notice(
                code="TRAINING.ADVANCED",
                title="Шаг сдвинут",
                summary=(
                    f"Текущий шаг: {STEP_LABELS.get(track.current_step, track.current_step)}."
                ),
                next_step="Выдайте задание этого шага, когда будете готовы.",
            ),
        )

    @training.command(name="mentor", description="Назначить наставника маршрута")
    @app_commands.describe(member="Подопечный", mentor="Наставник из состава")
    @requires(Permission.TRAINING_MANAGE)
    async def mentor_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        mentor: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Наставника назначают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            await assign_mentor(
                interaction.user,
                guild_id,
                discord_id=member.id,
                mentor_discord_id=mentor.id,
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="TRAINING.MENTOR",
                title="Наставник записан",
                summary=f"{mentor.mention} видит только маршрут {member.mention}.",
                next_step=f"Список подопечных — {cmd('training mentees')}.",
            ),
        )

    @training.command(name="clearance", description="Выдать допуск по направлению")
    @app_commands.describe(
        member="Участник",
        activity="Направление допуска",
        version="Версия правил",
    )
    @requires(Permission.TRAINING_MANAGE)
    async def clearance_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        activity: str,
        version: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Допуск выдаётся только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await grant_activity_clearance(
                interaction.user,
                guild_id,
                discord_id=member.id,
                activity=activity,
                rules_version=version,
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="CLEARANCE.GRANTED",
                title="Допуск выдан",
                summary=(
                    f"{member.mention}: {item.activity}, версия {item.rules_version}. "
                    "Общие права администратора не выдаются."
                ),
                next_step=(
                    "После смены версии правил допуск потребует повторного ознакомления."
                ),
                reference=str(item.id),
            ),
        )

    @training.command(name="rules", description="Опубликовать версию правил направления")
    @app_commands.describe(activity="Направление", version="Новая версия")
    @requires(Permission.TRAINING_MANAGE)
    async def rules_command(
        self, interaction: discord.Interaction, activity: str, version: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Версию правил публикуют только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            published, marked = await publish_rules(
                interaction.user, guild_id, activity=activity, version=version
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="RULES.PUBLISHED",
                title="Версия правил записана",
                summary=(
                    f"{published.activity} · {published.version}. "
                    f"Допусков к повторному ознакомлению: {marked}."
                ),
                next_step="Старые допуски этого направления помечены, новые — нет.",
            ),
        )

    @training.command(name="criteria", description="Опубликовать критерии повышения")
    @app_commands.describe(title="Заголовок", body="Текст критериев")
    @requires(Permission.MEMBERS_MANAGE)
    async def criteria_command(
        self, interaction: discord.Interaction, title: str, body: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Критерии публикуют только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await set_promotion_criteria(
                interaction.user, guild_id, title=title, body=body
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="PROMOTION.CRITERIA",
                title="Критерии опубликованы",
                summary=f"«{item.title}». Автоматического повышения нет.",
                next_step=f"Предложение — {cmd('training propose')}.",
                reference=str(item.id),
            ),
        )

    @training.command(name="propose", description="Предложить повышение по критериям")
    @app_commands.describe(member="Кого предложить")
    @requires(Permission.TRAINING_MANAGE)
    async def propose_command(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Предложение создаётся только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await propose_promotion(
                interaction.user, guild_id, discord_id=member.id
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="PROMOTION.PROPOSED",
                title="Предложение записано",
                summary=(
                    f"{member.mention} предложен по опубликованным критериям. "
                    "Статус в составе не изменён."
                ),
                next_step=f"Решение принимает кадровик через {cmd('training decide')}.",
                reference=str(item.id),
            ),
        )

    @training.command(name="decide", description="Решить предложение о повышении")
    @app_commands.describe(
        proposal_id="Номер предложения",
        accepted="Принять предложение",
        reason="Основание",
    )
    @requires(Permission.MEMBERS_MANAGE)
    async def decide_command(
        self,
        interaction: discord.Interaction,
        proposal_id: int,
        accepted: bool,
        reason: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TRAINING.GUILD_ONLY",
                    summary="Решение записывается только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await decide_promotion(
                interaction.user,
                guild_id,
                proposal_id=proposal_id,
                accepted=accepted,
                reason=reason,
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="PROMOTION.DECIDED",
                title="Решение записано",
                summary=(
                    f"Предложение {item.id}: "
                    f"{'принято' if item.status == 'accepted' else 'отклонено'}. "
                    "Статус членства и должность не менялись."
                ),
                next_step=(
                    f"В основной состав — только {cmd('members verify')}. "
                    "Автоповышения нет."
                ),
                reference=str(item.id),
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TrainingCog(bot))
