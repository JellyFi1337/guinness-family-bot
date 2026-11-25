"""Slash surface for family accounts, obligations and game reconciliation."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.treasury import (
    TreasuryError,
    account_card_spec,
    approve_obligation,
    batch_approve,
    cancel_obligation,
    confirm_obligation,
    create_from_accepted_report,
    legacy_history,
    list_live_accounts,
    mark_paid,
    obligation_card_spec,
    reconcile_account,
    reverse_ledger,
    show_account,
    show_obligation,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.messages import followup_spec
from ui.templates import missing_permission, notice, system_error
from utils.access import requires


def _error_spec(exc: TreasuryError) -> MessageSpec:
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


class TreasuryV2Cog(commands.Cog):
    treasury = app_commands.Group(
        name="treasury",
        description="Счета, обязательства и сверка казны",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @treasury.command(name="accounts", description="Живые счета Memphis")
    @requires(Permission.TREASURY_VIEW)
    async def accounts_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Счета доступны только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            cards = await list_live_accounts(interaction.user, guild_id)
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        details = tuple(
            (
                card.account.name,
                f"{card.balance} · сверка: "
                f"{card.last_reconciliation.created_at if card.last_reconciliation else 'нет'}",
            )
            for card in cards
        )
        await followup_spec(
            interaction,
            notice(
                code="TREASURY.ACCOUNTS",
                title="Счета семьи",
                summary=(
                    "Пять сущностей разделены: игровые деньги, Guinn Coins, "
                    "очки вклада, репутация и влияние, Dark Coins. "
                    "Автоматического обмена нет."
                ),
                details=details,
                next_step=f"Карточка — {cmd('treasury show')}.",
                reference=current_reference(),
            ),
        )

    @treasury.command(name="show", description="Карточка счёта")
    @app_commands.describe(account_id="Номер счёта")
    @requires(Permission.TREASURY_VIEW)
    async def show_command(
        self, interaction: discord.Interaction, account_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Карточка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            card = await show_account(interaction.user, guild_id, account_id)
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, account_card_spec(card))

    @treasury.command(name="legacy", description="Старая казна только на чтение")
    @requires(Permission.TREASURY_VIEW)
    async def legacy_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="История доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            balance, rows = await legacy_history(interaction.user, guild_id)
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        details = tuple(
            (f"{amount}", f"<@{user_id}> · {reason}")
            for user_id, _name, amount, _action, reason, _dt in rows[:12]
        ) or (("История", "записей нет"),)
        await followup_spec(
            interaction,
            notice(
                code="TREASURY.LEGACY",
                title="Старая казна",
                summary=(
                    f"Баланс старой модели: {balance}. "
                    "Новые операции идут в счета Memphis, история предыдущего проекта сохранена."
                ),
                details=details,
                next_step=f"Новые выплаты — {cmd('treasury obligation')}.",
                reference=current_reference(),
            ),
        )

    @treasury.command(name="from_report", description="Создать обязательство из принятого отчёта")
    @app_commands.describe(report_id="Номер принятого отчёта")
    @requires(Permission.TREASURY_APPROVE)
    async def from_report_command(
        self, interaction: discord.Interaction, report_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Обязательство создают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await create_from_accepted_report(
                interaction.user, guild_id, report_id
            )
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, obligation_card_spec(item))

    @treasury.command(name="obligation", description="Карточка обязательства")
    @app_commands.describe(obligation_id="Номер обязательства")
    @requires(Permission.TREASURY_VIEW)
    async def obligation_command(
        self, interaction: discord.Interaction, obligation_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Карточка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await show_obligation(interaction.user, guild_id, obligation_id)
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, obligation_card_spec(item))

    @treasury.command(name="approve", description="Согласовать выплату")
    @app_commands.describe(obligation_id="Номер обязательства")
    @requires(Permission.TREASURY_APPROVE)
    async def approve_command(
        self, interaction: discord.Interaction, obligation_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Согласование доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await approve_obligation(
                interaction.user, guild_id, obligation_id
            )
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, obligation_card_spec(item))

    @treasury.command(name="batch", description="Пакетно согласовать мелкие выплаты")
    @app_commands.describe(obligation_ids="Номера через запятую")
    @requires(Permission.TREASURY_APPROVE)
    async def batch_command(
        self, interaction: discord.Interaction, obligation_ids: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Пакет доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            parsed = tuple(
                int(part.strip())
                for part in obligation_ids.replace(";", ",").split(",")
                if part.strip()
            )
            items = await batch_approve(interaction.user, guild_id, parsed)
        except (TreasuryError, ValueError) as exc:
            if isinstance(exc, TreasuryError):
                await followup_spec(interaction, _error_spec(exc))
                return
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.IDS",
                    summary="Номера обязательств — целые через запятую. Пакет не записан.",
                    retryable=True,
                    reference=current_reference(),
                ),
            )
            return
        await followup_spec(
            interaction,
            notice(
                code="TREASURY.BATCH",
                title="Пакет согласован",
                summary=f"Обработано {len(items)} обязательств.",
                next_step=f"Перевод отмечает казначей через {cmd('treasury pay')}.",
                reference=current_reference(),
            ),
        )

    @treasury.command(name="pay", description="Отметить игровой перевод и приложить подтверждение")
    @app_commands.describe(
        obligation_id="Номер обязательства",
        proof="Ссылка или номер подтверждения перевода",
    )
    @requires(Permission.TREASURY_PAY)
    async def pay_command(
        self,
        interaction: discord.Interaction,
        obligation_id: int,
        proof: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Отметку перевода пишут только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await mark_paid(
                interaction.user, guild_id, obligation_id, proof_ref=proof
            )
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, obligation_card_spec(item))

    @treasury.command(name="confirm", description="Подтвердить исполнение получателем или сверкой")
    @app_commands.describe(obligation_id="Номер обязательства")
    async def confirm_command(
        self, interaction: discord.Interaction, obligation_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Подтверждение доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await confirm_obligation(
                interaction.user, guild_id, obligation_id
            )
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, obligation_card_spec(item))

    @treasury.command(name="cancel", description="Отменить ещё не выплаченное обязательство")
    @app_commands.describe(obligation_id="Номер обязательства")
    @requires(Permission.TREASURY_APPROVE)
    async def cancel_command(
        self, interaction: discord.Interaction, obligation_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Отмена доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await cancel_obligation(
                interaction.user, guild_id, obligation_id
            )
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, obligation_card_spec(item))

    @treasury.command(name="reconcile", description="Сверить книжный остаток с игрой")
    @app_commands.describe(
        account_id="Номер счёта",
        game_balance="Остаток в игре, целое число",
        explanation="Почему есть расхождение",
    )
    @requires(Permission.TREASURY_PAY)
    async def reconcile_command(
        self,
        interaction: discord.Interaction,
        account_id: int,
        game_balance: int,
        explanation: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Сверку пишут только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            recon = await reconcile_account(
                interaction.user,
                guild_id,
                account_id,
                game_balance=game_balance,
                explanation=explanation,
            )
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="TREASURY.RECONCILED",
                title="Сверка записана",
                summary=(
                    f"Игра {recon.game_balance}, книга {recon.book_balance}, "
                    f"расхождение {recon.delta}."
                ),
                next_step=f"Карточка счёта — {cmd('treasury show')}.",
                reference=str(recon.id),
            ),
        )

    @treasury.command(name="reverse", description="Отменяющая запись журнала")
    @app_commands.describe(entry_id="Номер записи журнала", reason="Причина")
    @requires(Permission.TREASURY_PAY)
    async def reverse_command(
        self,
        interaction: discord.Interaction,
        entry_id: int,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="TREASURY.GUILD_ONLY",
                    summary="Отмену журнала пишут только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            entry = await reverse_ledger(
                interaction.user, guild_id, entry_id, reason=reason
            )
        except TreasuryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="TREASURY.REVERSED",
                title="Отменяющая запись",
                summary=(
                    f"Запись #{entry.reversal_of} отменена новой строкой #{entry.id}. "
                    "Старая строка не редактировалась."
                ),
                next_step=f"Счёт — {cmd('treasury show')}.",
                reference=str(entry.id),
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TreasuryV2Cog(bot))
