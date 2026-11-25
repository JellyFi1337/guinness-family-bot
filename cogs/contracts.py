"""Slash surface for the Majestic family contract catalog and runs."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from services.cooldowns import cooldown_claim, cooldown_retry_text
from services.contracts import (
    CATALOG_LABELS,
    ContractError,
    add_catalog_item,
    build_catalog,
    build_public_panel,
    catalog_spec,
    change_catalog_status,
    ensure_default_integrations,
    my_runs,
    run_spec,
    staff_overview,
    start_run,
    take_run,
)
from services.static_messages import StaticPayload, upsert_static_message
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.messages import followup_spec
from ui.templates import missing_permission, notice, system_error
from utils.access import has_administrator_access, requires


def _error_spec(exc: ContractError) -> MessageSpec:
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


class ContractsCog(commands.Cog):
    contract = app_commands.Group(
        name="contract",
        description="Каталог и запуски семейных контрактов",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        ensure_default_integrations()

    @contract.command(name="catalog", description="Каталог контрактов")
    @app_commands.describe(
        action="list — обзор, add — черновик, publish — открыть, archive — в архив",
        title="Название новой записи",
        direction="legal или criminal",
        team_max="Максимум человек в запуске",
        team_min="Минимум человек",
        upgrades="Ключи улучшений через запятую",
        resources="Требуемые ресурсы через запятую",
        instruction="Ссылка на инструкцию",
        verified="Дата проверки сведений",
        reward="Ожидаемая внутренняя награда, если уже проверена",
        catalog_id="Номер записи для publish или archive",
        profit_fields="Поля расчёта через запятую: purchase,expenses,cash,items,reputation",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="list", value="list"),
            app_commands.Choice(name="add", value="add"),
            app_commands.Choice(name="publish", value="publish"),
            app_commands.Choice(name="archive", value="archive"),
        ],
        direction=[
            app_commands.Choice(name="legal", value="legal"),
            app_commands.Choice(name="criminal", value="criminal"),
        ],
    )
    @requires(Permission.CONTRACTS_MANAGE)
    async def catalog_command(
        self,
        interaction: discord.Interaction,
        action: str = "list",
        title: str | None = None,
        direction: str | None = None,
        team_max: app_commands.Range[int, 1, 99] | None = None,
        team_min: app_commands.Range[int, 1, 99] | None = None,
        upgrades: str | None = None,
        resources: str | None = None,
        instruction: str | None = None,
        verified: str | None = None,
        reward: str | None = None,
        catalog_id: int | None = None,
        profit_fields: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="CONTRACTS.GUILD_ONLY",
                    summary="Каталог доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            if action == "add":
                if not title or not direction or team_max is None:
                    raise ContractError(
                        "Для новой записи нужны название, направление и размер команды. "
                        "Запись не создана.",
                        code="CONTRACTS.FIELDS",
                    )
                item = await add_catalog_item(
                    interaction.user,
                    guild_id,
                    title=title,
                    direction=direction,
                    team_max=int(team_max),
                    team_min=int(team_min or 1),
                    upgrades=upgrades,
                    resources=resources,
                    instruction=instruction,
                    verified_at=verified,
                    reward_note=reward,
                    profit_fields=profit_fields,
                )
                await followup_spec(
                    interaction,
                    notice(
                        code="CONTRACTS.ADDED",
                        title="Черновик каталога",
                        summary=(
                            f"«{item.title}» записан как черновик #{item.id}. "
                            "Запуск откроется после публикации."
                        ),
                        next_step=(
                            f"Публикация — {cmd('contract catalog')} "
                            "с action publish и номером записи."
                        ),
                        reference=str(item.id),
                    ),
                )
                return
            if action in {"publish", "archive"}:
                if catalog_id is None:
                    raise ContractError(
                        "Укажите номер записи каталога. Статус не изменён.",
                        code="CONTRACTS.FIELDS",
                    )
                item = await change_catalog_status(
                    interaction.user,
                    guild_id,
                    int(catalog_id),
                    "available" if action == "publish" else "archived",
                )
                await followup_spec(
                    interaction,
                    notice(
                        code="CONTRACTS.CATALOG_STATUS",
                        title="Каталог обновлён",
                        summary=(
                            f"«{item.title}» теперь "
                            f"{CATALOG_LABELS[item.status]}."
                        ),
                        next_step=f"Запуск доступной записи — {cmd('contract start')}.",
                        reference=str(item.id),
                    ),
                )
                return
            view = await build_catalog(interaction.user, guild_id)
            queue = await staff_overview(guild_id)
            await followup_spec(interaction, catalog_spec(view, queue))
        except ContractError as exc:
            await followup_spec(interaction, _error_spec(exc))

    @contract.command(name="start", description="Запустить контракт из каталога")
    @app_commands.describe(
        catalog_id="Номер записи каталога",
        share="Ваша доля от 0 до 10000; пусто — вся выручка, если команда из одного",
    )
    async def start_command(
        self,
        interaction: discord.Interaction,
        catalog_id: int,
        share: app_commands.Range[int, 0, 10000] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="CONTRACTS.GUILD_ONLY",
                    summary="Запуск создаётся только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        async with cooldown_claim(
            guild_id,
            "contract_start",
            interaction.user.id,
            bypass=has_administrator_access(interaction.user),
        ) as claim:
            if not claim.allowed:
                await followup_spec(
                    interaction,
                    system_error(
                        code="CONTRACTS.COOLDOWN",
                        summary=cooldown_retry_text(claim.retry_after),
                        retryable=True,
                        reference=current_reference(),
                    ),
                )
                return
            try:
                run = await start_run(
                    interaction.user,
                    guild_id,
                    catalog_id,
                    share_bps=int(share) if share is not None else None,
                )
            except ContractError as exc:
                await followup_spec(interaction, _error_spec(exc))
                return
            claim.keep()
        await followup_spec(
            interaction,
            run_spec(
                run,
                title="Запуск открыт",
                summary=(
                    f"Контракт «{run.snapshot.get('title')}» в работе. "
                    "Последующая правка каталога эти условия не изменит."
                ),
            ),
        )

    @contract.command(name="take", description="Присоединиться к запуску")
    @app_commands.describe(
        run_id="Номер запуска",
        share="Доля от 0 до 10000, чтобы не заявлять всю выручку сразу",
    )
    async def take_command(
        self,
        interaction: discord.Interaction,
        run_id: int,
        share: app_commands.Range[int, 0, 10000],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="CONTRACTS.GUILD_ONLY",
                    summary="Присоединиться можно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            run = await take_run(
                interaction.user, guild_id, run_id, share_bps=int(share)
            )
        except ContractError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            run_spec(
                run,
                title="Вы в составе",
                summary="Доля зафиксирована. Полную выручку за всех сразу заявить нельзя.",
            ),
        )

    @contract.command(name="my", description="Ваши активные запуски")
    async def my_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="CONTRACTS.GUILD_ONLY",
                    summary="Список запусков доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            runs = await my_runs(interaction.user, guild_id)
        except ContractError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        if not runs:
            await followup_spec(
                interaction,
                notice(
                    code="CONTRACTS.MY_EMPTY",
                    title="Своих запусков нет",
                    summary="Сейчас за вами нет открытого контракта.",
                    next_step=f"Доступные задачи — {cmd('contract start')}.",
                ),
            )
            return
        details = tuple(
            (
                f"#{run.id} {run.snapshot.get('title')}",
                run.status,
            )
            for run in runs
        )
        await followup_spec(
            interaction,
            notice(
                code="CONTRACTS.MY",
                title="Ваши запуски",
                summary="Показаны незакрытые запуски, в которых вы участвуете.",
                details=details,
                next_step=f"Сдать отчёт — {cmd('report submit')}.",
            ),
        )


async def upsert_contracts_panel(channel: discord.TextChannel) -> None:
    spec = await build_public_panel(channel.guild.id)

    def build() -> StaticPayload:
        return StaticPayload(embed=spec, revision=1)

    await upsert_static_message(
        guild=channel.guild,
        channel=channel,
        panel_key="contracts",
        build=build,
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ContractsCog(bot))
