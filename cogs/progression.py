"""Slash surface for organization development and seasonal right."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.progression import (
    ProgressionError,
    advance_upgrade,
    advanced_spec,
    announce_season,
    confirm_direction_change,
    defined_spec,
    define_upgrade,
    development_card,
    direction_done_spec,
    direction_preview_spec,
    goal_spec,
    hours_spec,
    lore_spec,
    mark_lore_reviewed,
    org_level_spec,
    preview_direction_change,
    publish_lore_revision,
    review_spec,
    season_announced_spec,
    season_card,
    set_goal,
    set_org_level,
    set_play_hours,
    tree_card,
)
from ui.message_specs import MessageSpec
from ui.messages import followup_spec
from ui.templates import missing_permission, system_error
from utils.access import requires


def _error_spec(exc: ProgressionError) -> MessageSpec:
    if exc.decision is not None:
        return missing_permission(exc.decision, reference=current_reference())
    return system_error(
        code=exc.code,
        summary=str(exc),
        retryable=exc.retryable,
        reference=current_reference(),
    )


class ProgressionCog(commands.Cog):
    progress = app_commands.Group(
        name="progress",
        description="Развитие семьи, сезон и сезонное право",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _guild(self, interaction: discord.Interaction) -> int | None:
        guild_id = interaction.guild_id
        if guild_id is not None:
            return guild_id
        await followup_spec(
            interaction,
            system_error(
                code="PROG.GUILD_ONLY",
                summary="Развитие семьи доступно только на сервере.",
                retryable=False,
                reference=current_reference(),
            ),
        )
        return None

    @progress.command(name="tree", description="Показать дерево улучшений")
    @requires(Permission.PROGRESSION_MANAGE)
    async def tree_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            spec = await tree_card(interaction.user, guild_id)
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, spec)

    @progress.command(name="define", description="Добавить узел дерева улучшений")
    @app_commands.describe(
        key="Ключ узла",
        title="Название",
        track="Ветка",
        max_level="Максимум уровней, по умолчанию из настройки",
        depends_on="Ключ зависимости",
        depends_level="Нужный уровень зависимости",
        requires_org_level="Нужный уровень организации, 0 — не задан",
        requires_contracts="Нужно принятых контрактов",
        requires_sku="Артикул ресурса",
        requires_qty="Количество ресурса",
        cost_reputation="Стоимость репутации, 0 — не подтверждена",
        source="Источник сведений",
        verified_at="Дата проверки ГГГГ-ММ-ДД",
    )
    @app_commands.choices(
        track=[
            app_commands.Choice(name="легальные контракты", value="legal_contracts"),
            app_commands.Choice(
                name="нелегальные контракты", value="illegal_contracts"
            ),
            app_commands.Choice(name="другое", value="other"),
        ]
    )
    @requires(Permission.PROGRESSION_MANAGE)
    async def define_command(
        self,
        interaction: discord.Interaction,
        key: str,
        title: str,
        track: str,
        max_level: int | None = None,
        depends_on: str | None = None,
        depends_level: int = 0,
        requires_org_level: int = 0,
        requires_contracts: int = 0,
        requires_sku: str | None = None,
        requires_qty: int = 0,
        cost_reputation: int = 0,
        source: str | None = None,
        verified_at: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            item = await define_upgrade(
                interaction.user,
                guild_id,
                key=key,
                title=title,
                track=track,
                max_level=max_level,
                depends_on=depends_on or "",
                depends_level=depends_level,
                requires_org_level=requires_org_level,
                requires_contracts=requires_contracts,
                requires_sku=requires_sku or "",
                requires_qty=requires_qty,
                cost_reputation=cost_reputation,
                source=source or "",
                verified_at=verified_at,
            )
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, defined_spec(item))

    @progress.command(name="advance", description="Поднять уровень улучшения")
    @app_commands.describe(key="Ключ узла")
    @requires(Permission.PROGRESSION_MANAGE)
    async def advance_command(
        self, interaction: discord.Interaction, key: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            item, progress = await advance_upgrade(interaction.user, guild_id, key)
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, advanced_spec(item, progress))

    @progress.command(name="goal", description="Выбрать текущую цель развития")
    @app_commands.describe(
        key="Ключ узла",
        assignee="Кто назначен",
        note="Заметка координатору",
    )
    @requires(Permission.PROGRESSION_MANAGE)
    async def goal_command(
        self,
        interaction: discord.Interaction,
        key: str,
        assignee: discord.Member | None = None,
        note: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            _development, node = await set_goal(
                interaction.user,
                guild_id,
                key,
                assignee_discord_id=None if assignee is None else assignee.id,
                note=note or "",
            )
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, goal_spec(node))

    @progress.command(name="show", description="Карточка развития семьи")
    async def show_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            spec = await development_card(interaction.user, guild_id)
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, spec)

    @progress.command(
        name="direction_preview",
        description="Показать последствия смены направления",
    )
    @app_commands.describe(
        target="legal или criminal",
        spent_reputation="Потраченная репутация, если отличается от учёта",
    )
    @app_commands.choices(
        target=[
            app_commands.Choice(name="легальное", value="legal"),
            app_commands.Choice(name="нелегальное", value="criminal"),
        ]
    )
    @requires(Permission.PROGRESSION_MANAGE)
    async def direction_preview_command(
        self,
        interaction: discord.Interaction,
        target: str,
        spent_reputation: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            preview = await preview_direction_change(
                interaction.user,
                guild_id,
                target,
                spent_reputation=spent_reputation,
            )
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, direction_preview_spec(preview))

    @progress.command(name="direction", description="Подтвердить направление семьи")
    @app_commands.describe(
        target="legal или criminal",
        confirm="Подтвердить после предпросмотра",
        expected_version="Версия из предпросмотра",
    )
    @app_commands.choices(
        target=[
            app_commands.Choice(name="легальное", value="legal"),
            app_commands.Choice(name="нелегальное", value="criminal"),
        ]
    )
    @requires(Permission.PROGRESSION_MANAGE)
    async def direction_command(
        self,
        interaction: discord.Interaction,
        target: str,
        confirm: bool,
        expected_version: int = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            change = await confirm_direction_change(
                interaction.user,
                guild_id,
                target,
                confirm=confirm,
                expected_version=expected_version,
            )
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, direction_done_spec(change))

    @progress.command(name="hours", description="Ввести часы игры за сезон")
    @app_commands.describe(
        hours="Часы игры. Голосовой канал сюда не пишется",
        member="Чьи часы, по умолчанию ваши",
    )
    async def hours_command(
        self,
        interaction: discord.Interaction,
        hours: int,
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            row = await set_play_hours(
                interaction.user,
                guild_id,
                hours,
                for_discord_id=None if member is None else member.id,
            )
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, hours_spec(row))

    @progress.command(name="season", description="Сезонное право и отсечка приёма")
    @app_commands.describe(member="Чьё право показать, по умолчанию ваше")
    async def season_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        target_id = interaction.user.id if member is None else member.id
        try:
            spec = await season_card(interaction.user, guild_id, target_id)
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, spec)

    @progress.command(name="lore", description="Записать сборку или дату сведений")
    @app_commands.describe(
        revision="Номер сборки или дата публикации",
        published_at="Дата публикации ГГГГ-ММ-ДД",
    )
    @requires(Permission.PROGRESSION_MANAGE)
    async def lore_command(
        self,
        interaction: discord.Interaction,
        revision: str,
        published_at: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            _updated, flagged = await publish_lore_revision(
                interaction.user,
                guild_id,
                revision,
                published_at=published_at,
            )
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, lore_spec(revision.strip(), flagged))

    @progress.command(name="review", description="Отметить пересмотр шаблона или допуска")
    @app_commands.describe(kind="Тип объекта", subject_key="Ключ улучшения, шаблона или допуска")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="улучшение", value="upgrade"),
            app_commands.Choice(name="шаблон", value="template"),
            app_commands.Choice(name="допуск", value="clearance"),
        ]
    )
    @requires(Permission.PROGRESSION_MANAGE)
    async def review_command(
        self, interaction: discord.Interaction, kind: str, subject_key: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            await mark_lore_reviewed(interaction.user, guild_id, kind, subject_key)
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, review_spec(kind, subject_key))

    @progress.command(name="announce_season", description="Объявить следующий сезон")
    @app_commands.describe(
        season_key="Ключ сезона",
        starts_on="Начало ГГГГ-ММ-ДД",
        ends_on="Конец ГГГГ-ММ-ДД",
        influence_daily_limit="Дневной лимит влияния, 0 — не подтверждён",
        prize_places="Призовые места текстом, пусто — не подтверждены",
        half_loss_threshold="Порог половинных потерь, 0 — не подтверждён",
    )
    @requires(Permission.PROGRESSION_MANAGE)
    async def announce_season_command(
        self,
        interaction: discord.Interaction,
        season_key: str,
        starts_on: str,
        ends_on: str,
        influence_daily_limit: int = 0,
        prize_places: str | None = None,
        half_loss_threshold: int = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            row = await announce_season(
                interaction.user,
                guild_id,
                season_key=season_key,
                starts_on=starts_on,
                ends_on=ends_on,
                influence_daily_limit=influence_daily_limit,
                prize_places=prize_places or "",
                half_loss_threshold=half_loss_threshold,
            )
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, season_announced_spec(row))

    @progress.command(name="org_level", description="Записать уровень организации")
    @app_commands.describe(org_level="Уровень из игры, 0 — не подтверждён")
    @requires(Permission.PROGRESSION_MANAGE)
    async def org_level_command(
        self, interaction: discord.Interaction, org_level: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = await self._guild(interaction)
        if guild_id is None:
            return
        try:
            development = await set_org_level(interaction.user, guild_id, org_level)
        except ProgressionError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, org_level_spec(development))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProgressionCog(bot))
