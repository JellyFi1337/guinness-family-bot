"""Slash surface for family warehouse, kits and irreversible storage warnings."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from repositories.operations import OutboxOperation
from services.inventory import (
    InventoryError,
    adjust_stock,
    alerts_card,
    consume_kit_item,
    create_item,
    issue_kit,
    item_spec,
    kit_card,
    list_kit_lines,
    list_items,
    list_storage,
    mark_warehouse_auction,
    movement_spec,
    receive_stock,
    reconcile_location,
    release_event_reserves,
    request_kit,
    reserve_for_event,
    reserve_kit,
    return_kit_item,
    sell_warehouse,
    set_tax_duty,
    show_kit,
    tax_reminder_spec,
    upgrade_mat_truck,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.messages import followup_spec, send_spec
from ui.templates import missing_permission, notice, system_error
from utils.access import requires
from ui.design import Accent


def _error_spec(exc: InventoryError) -> MessageSpec:
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


class LogisticsCog(commands.Cog):
    stock = app_commands.Group(
        name="stock",
        description="Склад организации и комплекты",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.register("inventory.tax.remind", self._notify_tax)

    async def cog_unload(self) -> None:
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.unregister("inventory.tax.remind")

    async def _notify_tax(self, operation: OutboxOperation) -> int | None:
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {operation.guild_id} is unavailable")
        spec = tax_reminder_spec(operation.payload)
        sent = 0
        for user_id in (
            int(operation.payload.get("responsible_id") or 0),
            int(operation.payload.get("backup_id") or 0),
        ):
            if user_id <= 0:
                continue
            member = guild.get_member(user_id)
            if member is None:
                continue
            await send_spec(member, spec)
            sent += 1
        return sent or None

    @stock.command(name="locations", description="Места хранения и сверка")
    @requires(Permission.INVENTORY_MANAGE)
    async def locations_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Склад доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            spec = await list_storage(interaction.user, guild_id)
        except InventoryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, spec)

    @stock.command(name="item", description="Добавить предмет в каталог семьи")
    @app_commands.describe(
        sku="Артикул",
        title="Название",
        unit="Единица измерения",
        low_stock="Порог оповещения; 0 — не задан",
    )
    @requires(Permission.INVENTORY_MANAGE)
    async def item_command(
        self,
        interaction: discord.Interaction,
        sku: str,
        title: str,
        unit: str,
        low_stock: int = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Каталог ведут только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await create_item(
                interaction.user,
                guild_id,
                sku=sku,
                title=title,
                unit=unit,
                low_stock=low_stock,
            )
        except InventoryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, item_spec(item))

    @stock.command(name="receive", description="Принять ресурс на место хранения")
    @app_commands.describe(
        sku="Артикул",
        quantity="Количество",
        location="warehouse, office_safe, gun_cabinet, pantry, wardrobe, garage, mat_truck",
        basis="Основание, если повтор должен быть идемпотентным",
    )
    @requires(Permission.INVENTORY_MANAGE)
    async def receive_command(
        self,
        interaction: discord.Interaction,
        sku: str,
        quantity: int,
        location: str = "warehouse",
        basis: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Поступление записывают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            movement = await receive_stock(
                interaction.user,
                guild_id,
                sku=sku,
                quantity=quantity,
                location_kind=location,
                basis=basis,
            )
        except InventoryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            movement_spec(movement, title="Поступление", sku=sku),
        )

    @stock.command(name="reserve", description="Зарезервировать остаток под событие")
    @app_commands.describe(
        event_id="Номер события",
        sku="Артикул",
        quantity="Количество",
        location="Место хранения, по умолчанию склад",
    )
    @requires(Permission.INVENTORY_MANAGE)
    async def reserve_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        sku: str,
        quantity: int,
        location: str = "warehouse",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Резерв создают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            reservation = await reserve_for_event(
                interaction.user,
                guild_id,
                event_id=event_id,
                sku=sku,
                quantity=quantity,
                location_kind=location,
            )
        except InventoryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="INV.RESERVE",
                title="Резерв",
                summary=f"{sku}: {reservation.quantity} под событие {reservation.event_id}.",
                next_step=f"Отмена события снимет резерв. Карточка — {cmd('stock locations')}.",
                accent=Accent.LOGISTICS,
                reference=str(reservation.id),
            ),
        )

    @stock.command(name="release", description="Снять резерв события")
    @app_commands.describe(event_id="Номер события")
    @requires(Permission.INVENTORY_MANAGE)
    async def release_command(
        self, interaction: discord.Interaction, event_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Резерв снимают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            released = await release_event_reserves(
                interaction.user, guild_id, event_id
            )
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(
            interaction,
            notice(
                code="INV.RELEASE",
                title="Резерв снят",
                summary=f"Освобождено позиций: {len(released)}.",
                next_step=f"Остатки — {cmd('stock locations')}.",
                accent=Accent.LOGISTICS,
                reference=str(event_id),
            ),
        )

    @stock.command(name="kit_request", description="Заказать комплект под задачу")
    @app_commands.describe(event_id="Номер события", sku="Артикул", quantity="Количество")
    async def kit_request_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        sku: str,
        quantity: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Комплект заказывают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            kit = await request_kit(
                interaction.user,
                guild_id,
                event_id=event_id,
                sku=sku,
                quantity=quantity,
            )
            catalog = {item.id: item for item in await list_items(kit.family_id)}
            spec = kit_card(kit, await list_kit_lines(kit.id), catalog)
        except InventoryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, spec)

    @stock.command(name="kit_reserve", description="Зарезервировать комплект")
    @app_commands.describe(kit_id="Номер комплекта")
    @requires(Permission.INVENTORY_MANAGE)
    async def kit_reserve_command(
        self, interaction: discord.Interaction, kit_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Резерв комплекта делают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            kit = await reserve_kit(interaction.user, guild_id, kit_id)
            catalog = {item.id: item for item in await list_items(kit.family_id)}
            spec = kit_card(kit, await list_kit_lines(kit.id), catalog)
        except InventoryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, spec)

    @stock.command(name="kit_issue", description="Выдать зарезервированный комплект")
    @app_commands.describe(kit_id="Номер комплекта")
    @requires(Permission.INVENTORY_MANAGE)
    async def kit_issue_command(
        self, interaction: discord.Interaction, kit_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Выдачу делают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            kit = await issue_kit(interaction.user, guild_id, kit_id)
            catalog = {item.id: item for item in await list_items(kit.family_id)}
            spec = kit_card(kit, await list_kit_lines(kit.id), catalog)
        except InventoryError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, spec)

    @stock.command(name="kit_return", description="Принять остаток комплекта на склад")
    @app_commands.describe(kit_id="Номер комплекта", sku="Артикул", quantity="Количество")
    @requires(Permission.INVENTORY_MANAGE)
    async def kit_return_command(
        self,
        interaction: discord.Interaction,
        kit_id: int,
        sku: str,
        quantity: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Возврат принимают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            kit = await return_kit_item(
                interaction.user, guild_id, kit_id=kit_id, sku=sku, quantity=quantity
            )
            catalog = {item.id: item for item in await list_items(kit.family_id)}
            spec = kit_card(kit, await list_kit_lines(kit.id), catalog)
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(interaction, spec)

    @stock.command(name="kit_show", description="Готовность снабжения")
    @app_commands.describe(kit_id="Номер комплекта", event_id="Номер события")
    async def kit_show_command(
        self,
        interaction: discord.Interaction,
        kit_id: int | None = None,
        event_id: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Карточка комплекта доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        if kit_id is None and event_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.KIT_REF",
                    summary="Укажите номер комплекта или события.",
                    retryable=True,
                    reference=current_reference(),
                ),
            )
            return
        try:
            spec = await show_kit(
                interaction.user, guild_id, kit_id=kit_id, event_id=event_id
            )
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(interaction, spec)

    @stock.command(name="consume", description="Списать расход по результату")
    @app_commands.describe(kit_id="Номер комплекта", sku="Артикул", quantity="Количество")
    @requires(Permission.INVENTORY_MANAGE)
    async def consume_command(
        self,
        interaction: discord.Interaction,
        kit_id: int,
        sku: str,
        quantity: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Расход записывают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            kit = await consume_kit_item(
                interaction.user, guild_id, kit_id=kit_id, sku=sku, quantity=quantity
            )
            catalog = {item.id: item for item in await list_items(kit.family_id)}
            spec = kit_card(kit, await list_kit_lines(kit.id), catalog)
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(interaction, spec)

    @stock.command(name="adjust", description="Корректировка остатка с причиной")
    @app_commands.describe(
        sku="Артикул",
        quantity="Количество",
        reason="Причина, попадёт в журнал решений",
        location="Место хранения",
        add="true — приход, false — списание без выдачи",
    )
    @requires(Permission.INVENTORY_MANAGE)
    async def adjust_command(
        self,
        interaction: discord.Interaction,
        sku: str,
        quantity: int,
        reason: str,
        location: str = "warehouse",
        add: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Корректировку делают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            movement = await adjust_stock(
                interaction.user,
                guild_id,
                sku=sku,
                quantity=quantity,
                reason=reason,
                location_kind=location,
                add=add,
            )
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(
            interaction,
            movement_spec(movement, title="Корректировка", sku=sku),
        )

    @stock.command(name="reconcile", description="Сверка места хранения с игрой")
    @app_commands.describe(location="Место хранения", note="Что увидели в игре")
    @requires(Permission.INVENTORY_MANAGE)
    async def reconcile_command(
        self,
        interaction: discord.Interaction,
        location: str,
        note: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Сверку делают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            spec = await reconcile_location(
                interaction.user, guild_id, location_kind=location, note=note
            )
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(interaction, spec)

    @stock.command(name="alerts", description="Оповещения склада")
    @requires(Permission.INVENTORY_MANAGE)
    async def alerts_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Оповещения склада доступны только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            spec = await alerts_card(interaction.user, guild_id)
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(interaction, spec)

    @stock.command(name="mat_upgrade", description="Отметить покупку следующей матовозки")
    @app_commands.describe(
        confirm_unload="Чек-лист: выгрузить матовозку",
        confirm="Подтвердить уничтожение текущей машины",
    )
    @requires(Permission.INVENTORY_MANAGE)
    async def mat_upgrade_command(
        self,
        interaction: discord.Interaction,
        confirm_unload: bool,
        confirm: bool,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Уровень матовозки отмечают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            truck = await upgrade_mat_truck(
                interaction.user,
                guild_id,
                confirm_unload=confirm_unload,
                confirm=confirm,
            )
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(
            interaction,
            notice(
                code="INV.MAT_UPGRADE",
                title="Матовозка",
                summary=f"Уровень {truck.mat_level}, ёмкость {truck.capacity} кг.",
                next_step=f"Погрузка — {cmd('stock receive')}.",
                accent=Accent.LOGISTICS,
                reference=str(truck.id),
            ),
        )

    @stock.command(name="sell", description="Продать склад государству")
    @app_commands.describe(confirm="Подтвердить удаление всех предметов")
    @requires(Permission.INVENTORY_MANAGE)
    async def sell_command(
        self, interaction: discord.Interaction, confirm: bool
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Продажу склада отмечают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            warehouse = await sell_warehouse(
                interaction.user, guild_id, confirm=confirm
            )
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(
            interaction,
            notice(
                code="INV.SELL",
                title="Склад продан",
                summary="Предметы удалены. Это не исход неуплаты налога.",
                details=(("Статус", "продан" if warehouse.sold else "активен"),),
                next_step=f"Карточка — {cmd('stock locations')}.",
                accent=Accent.LOGISTICS,
                reference=str(warehouse.id),
            ),
        )

    @stock.command(name="auction", description="Отметить склад на аукционе за налог")
    @app_commands.describe(confirm="Подтвердить: предметы остаются")
    @requires(Permission.INVENTORY_MANAGE)
    async def auction_command(
        self, interaction: discord.Interaction, confirm: bool
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Аукцион отмечают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            warehouse = await mark_warehouse_auction(
                interaction.user, guild_id, confirm=confirm
            )
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(
            interaction,
            notice(
                code="INV.AUCTION",
                title="Склад на аукционе",
                summary="Предметы остаются. Это не продажа государству.",
                details=(("Аукцион", "да" if warehouse.auctioned else "нет"),),
                next_step=f"Карточка — {cmd('stock locations')}.",
                accent=Accent.LOGISTICS,
                reference=str(warehouse.id),
            ),
        )

    @stock.command(name="tax", description="Назначить срок налога и исполнителей")
    @app_commands.describe(
        due_at="Срок в ISO; интервал бот не выдумывает",
        responsible="Ответственный",
        backup="Резервный исполнитель",
    )
    @requires(Permission.INVENTORY_MANAGE)
    async def tax_command(
        self,
        interaction: discord.Interaction,
        due_at: str,
        responsible: discord.Member,
        backup: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="INV.GUILD_ONLY",
                    summary="Налог назначают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            duty = await set_tax_duty(
                interaction.user,
                guild_id,
                due_at=due_at,
                responsible_id=responsible.id,
                backup_id=backup.id,
            )
        except InventoryError as extra:
            await followup_spec(interaction, _error_spec(extra))
            return
        await followup_spec(
            interaction,
            notice(
                code="INV.TAX",
                title="Налог за имущество",
                summary=f"Срок {duty.due_at}.",
                details=(
                    ("Ответственный", f"<@{duty.responsible_id}>"),
                    ("Резерв", f"<@{duty.backup_id}>"),
                ),
                next_step="Напоминание уйдёт в срок. Интервал задаёте вы, не бот.",
                accent=Accent.LOGISTICS,
                reference=str(duty.id),
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LogisticsCog(bot))
