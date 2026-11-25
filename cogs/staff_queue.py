"""Slash surface for the senior-staff work queue."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.queue import (
    QueueError,
    appeal_spec,
    claim_item,
    ensure_queue_integrations,
    escalate_item,
    file_appeal,
    leader_summary,
    list_queue,
    queue_list_spec,
    show_item,
    summary_spec,
    transfer_item,
    work_item_spec,
)
from ui.message_specs import MessageSpec
from ui.messages import followup_spec
from ui.templates import missing_permission, system_error
from utils.access import requires
from utils.views import SafeView


def _error_spec(exc: QueueError) -> MessageSpec:
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


class QueueClaimView(SafeView):
    def __init__(self, ref: str) -> None:
        super().__init__(timeout=300)
        self.ref = ref

    @discord.ui.button(label="Взять в работу", style=discord.ButtonStyle.primary)
    async def claim_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="QUEUE.GUILD_ONLY",
                    summary="Очередь доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await claim_item(interaction.user, guild_id, self.ref)
        except QueueError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction, work_item_spec(item, ping=True), view=QueueClaimView(item.ref)
        )


class StaffQueueCog(commands.Cog):
    queue = app_commands.Group(
        name="queue",
        description="Очередь старшего состава",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        ensure_queue_integrations()

    @queue.command(name="list", description="Что требует решения")
    @app_commands.describe(
        kind="Тип: report, application, appeal, help, promotion",
        assignee="Ответственный",
        free="Только свободные",
        age_min="Минимальный возраст в минутах",
    )
    @requires(Permission.QUEUE_ASSIGN)
    async def list_command(
        self,
        interaction: discord.Interaction,
        kind: str | None = None,
        assignee: discord.Member | None = None,
        free: bool = False,
        age_min: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="QUEUE.GUILD_ONLY",
                    summary="Очередь доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            items = await list_queue(
                interaction.user,
                guild_id,
                kind=kind,
                assignee_id=None if assignee is None else assignee.id,
                unassigned=free,
                age_min=age_min,
            )
        except QueueError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        view = QueueClaimView(items[0].ref) if items else None
        await followup_spec(interaction, queue_list_spec(items), view=view)

    @queue.command(name="claim", description="Взять задачу в работу")
    @app_commands.describe(ref="Ссылка вида report:12")
    @requires(Permission.QUEUE_ASSIGN)
    async def claim_command(
        self, interaction: discord.Interaction, ref: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="QUEUE.GUILD_ONLY",
                    summary="Назначение доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await claim_item(interaction.user, guild_id, ref)
        except QueueError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction, work_item_spec(item, ping=True), view=QueueClaimView(item.ref)
        )

    @queue.command(name="transfer", description="Передать задачу с причиной")
    @app_commands.describe(
        ref="Ссылка вида report:12",
        target="Кому передать",
        reason="Причина передачи",
    )
    @requires(Permission.QUEUE_ASSIGN)
    async def transfer_command(
        self,
        interaction: discord.Interaction,
        ref: str,
        target: discord.Member,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="QUEUE.GUILD_ONLY",
                    summary="Передача доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await transfer_item(
                interaction.user, guild_id, ref, target, reason
            )
        except QueueError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction, work_item_spec(item, ping=True), view=QueueClaimView(item.ref)
        )

    @queue.command(name="escalate", description="Эскалация заместителю по сроку")
    @app_commands.describe(ref="Ссылка вида report:12")
    @requires(Permission.QUEUE_ASSIGN)
    async def escalate_command(
        self, interaction: discord.Interaction, ref: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="QUEUE.GUILD_ONLY",
                    summary="Эскалация доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await escalate_item(interaction.user, guild_id, ref)
        except QueueError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction, work_item_spec(item, ping=True), view=QueueClaimView(item.ref)
        )

    @queue.command(name="appeal", description="Подать обращение на руководителя")
    @app_commands.describe(accused="На кого жалоба", reason="Суть обращения")
    async def appeal_command(
        self,
        interaction: discord.Interaction,
        accused: discord.Member,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="QUEUE.GUILD_ONLY",
                    summary="Обращение подаётся только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            appeal = await file_appeal(
                interaction.user, guild_id, accused.id, reason
            )
        except QueueError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, appeal_spec(appeal))

    @queue.command(name="show", description="Карточка элемента очереди")
    @app_commands.describe(ref="Ссылка вида report:12")
    async def show_command(
        self, interaction: discord.Interaction, ref: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="QUEUE.GUILD_ONLY",
                    summary="Карточка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await show_item(interaction.user, guild_id, ref)
        except QueueError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction, work_item_spec(item), view=QueueClaimView(item.ref)
        )

    @queue.command(name="summary", description="Сводка нагрузки и задержек")
    @requires(Permission.CONFIG_MANAGE)
    async def summary_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="QUEUE.GUILD_ONLY",
                    summary="Сводка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            summary = await leader_summary(interaction.user, guild_id)
        except QueueError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, summary_spec(summary))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffQueueCog(bot))
