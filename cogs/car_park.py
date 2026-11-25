import logging
import re
from datetime import UTC, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from infrastructure.database import DATABASE_INTEGRITY_ERRORS
from repositories.cars import STATE_ACTIVE, get_car, is_bookable
from repositories.operations import OutboxOperation, enqueue_operation
from repositories.watermarks import get_watermark, set_watermark
from services.static_messages import StaticPayload, upsert_static_message
from services.vehicles import (
    VehicleError,
    assign_vehicle,
    book_vehicle,
    execute_property,
    property_card,
    recon_vehicle,
    register_vehicle,
    return_booking,
    upsert_property,
    vehicle_card,
)
from ui.command_registry import cmd
from ui.messages import followup_spec, followup_v2, respond_v2, send_v2
from ui.pagination import send_paged_list
from ui.results import queued
from ui.templates import missing_permission, notice, system_error

from config import CHANNELS
from core.log_context import current_reference
from utils.discord_lookup import get_text_channel
from ui.design import Accent
from ui.message_specs import MessageSpec, MessageTone
from utils.embeds import WHITE_COLOR, apply_accent
from utils.views import SafeModal, SafeView
from utils.access import administrator_or_owner
from utils.database import (
    cancel_car_booking,
    force_cancel_car_booking,
    create_car_booking,
    get_active_car_bookings,
    get_active_user_car_bookings,
    get_cars_with_active_bookings,
    car_bookings_ended_since,
    remove_car,
    edit_car,
    init_db,
    set_setting,
)


logger = logging.getLogger(__name__)
MOSCOW = timezone(timedelta(hours=3), name="MSK")
CAR_MESSAGE_KEY = "car_list_message_id"
_EXPIRY_JOB = "car-park-expiry"


def _until(value: str) -> datetime:
    raw = value.strip().lower().replace(" ", "")
    now = datetime.now(MOSCOW)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if match:
        hour, minute = map(int, match.groups())
        if hour > 23 or minute > 59:
            raise ValueError
        result = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if result <= now:
            result += timedelta(days=1)
    else:
        match = re.fullmatch(r"(\d+)(м|min|ч|h)", raw)
        if not match:
            raise ValueError
        amount = int(match.group(1))
        if amount < 1:
            raise ValueError
        result = now + (timedelta(hours=amount) if match.group(2) in {"ч", "h"} else timedelta(minutes=amount))
    if result - now > timedelta(days=7):
        raise ValueError
    return result.astimezone(UTC)


_PANEL_CAR_LIMIT = 15


def _vehicle_error_spec(exc: VehicleError) -> MessageSpec:
    if exc.decision is not None:
        return missing_permission(exc.decision, reference=current_reference())
    return system_error(
        code=exc.code,
        summary=str(exc),
        retryable=exc.retryable,
        reference=current_reference(),
    )


def _row_state(row) -> str:
    if len(row) > 8 and row[8]:
        return str(row[8])
    return STATE_ACTIVE


def _is_free(row) -> bool:
    return is_bookable(_row_state(row)) and not row[5]


def _car_lines(rows) -> list[str]:
    lines = []
    for row in rows:
        _, name, plate, owner, _, user_id, ic_name, ends_at = row[:8]
        state = _row_state(row)
        line = f"**{name}** · `{plate}` · {owner}"
        if state == "needs_recon":
            line += (
                "\n⚠️ Требует сверки: владелец вышел, в игре транспорт вернулся ему. "
                "Не считается свободной."
            )
        elif state == "returned_to_owner":
            line += "\n↩ Вернулся владельцу — не считается доступным."
        if user_id:
            timestamp = int(datetime.fromisoformat(ends_at).timestamp())
            line += (
                f"\n🔴 Забронирована <@{user_id}> ({ic_name}) до <t:{timestamp}:t>"
            )
        elif is_bookable(state):
            line += "\n🟢 Свободна"
        lines.append(line)
    return lines


async def car_list_embed() -> MessageSpec:
    """Panel view: free cars first, the rest behind the full-list button.

    The old version joined every car into one string and cut it at 4000
    characters with "…список сокращён" — everything past the cut was invisible
    and unreachable. Showing free cars first means the cut, when it happens,
    hides the least useful part, and the complete list is one button away.
    """

    rows = await get_cars_with_active_bookings()
    free = [row for row in rows if _is_free(row)]
    booked = [row for row in rows if row[5] and is_bookable(_row_state(row))]
    recon = [row for row in rows if not is_bookable(_row_state(row))]
    ordered = free + booked + recon
    lines = _car_lines(ordered[:_PANEL_CAR_LIMIT])
    description = "\n\n".join(lines) if lines else "Машины ещё не добавлены."
    details: list[tuple[str, str]] = []
    if len(ordered) > _PANEL_CAR_LIMIT:
        details.append(
            (
                "Полный список",
                f"Показаны первые **{_PANEL_CAR_LIMIT}** из **{len(ordered)}** "
                "(свободные выше). Нажмите «Все машины», чтобы открыть список целиком.",
            )
        )
    details.append(
        (
            "Статус",
            (
                f"🟢 Свободно: **{len(free)}** · 🔴 Занято: **{len(booked)}**"
                + (f" · ⚠️ Сверка: **{len(recon)}**" if recon else "")
            ),
        )
    )
    return apply_accent(
        notice(
            code="CAR.LIST",
            title="🚘 Список машин",
            summary=description,
            details=tuple(details),
            next_step="Забронируйте свободную машину или откройте полный список кнопками ниже.",
        ),
        Accent.LOGISTICS,
    )


def _is_car_list_message(message: discord.Message, bot_id: int) -> bool:
    return bool(
        message.author.id == bot_id
        and message.embeds
        and message.embeds[0].title == "🚘 Список машин"
    )


async def update_car_list_message(guild: discord.Guild) -> discord.Message | None:
    channel = get_text_channel(guild, "car_list")
    if channel is None:
        raise RuntimeError(f"Канал #{CHANNELS['car_list']} не найден")

    async def build() -> StaticPayload:
        embed = await car_list_embed()
        return StaticPayload(
            embed=apply_accent(embed, Accent.LOGISTICS),
            view=CarParkView(),
            revision=2,
        )

    message = await upsert_static_message(
        guild=guild,
        channel=channel,
        panel_key="car-park",
        build=build,
        legacy_setting_key=CAR_MESSAGE_KEY,
        legacy_match=lambda item: _is_car_list_message(item, guild.me.id),
    )
    if message is None:
        return None
    # Temporary dual-write keeps older deployments/cogs compatible during a
    # rolling update; the authoritative mapping is guild-scoped.
    await set_setting(CAR_MESSAGE_KEY, str(message.id))
    return message


async def _request_panel_refresh(guild_id: int, aggregate: str) -> None:
    """Queue a panel redraw so a Discord failure never rolls back a booking."""

    await enqueue_operation(
        idempotency_key=f"car.panel_refresh:{aggregate}",
        guild_id=guild_id,
        kind="car.panel_refresh",
        aggregate_id="car-park-panel",
        payload={},
    )


async def _booking_log(guild: discord.Guild, spec: MessageSpec) -> None:
    channel = get_text_channel(guild, "car_bookings")
    if channel is None:
        raise RuntimeError(f"Канал #{CHANNELS['car_bookings']} не найден")
    await send_v2(channel, embed=spec)


class BookingModal(SafeModal, title="Бронирование машины"):
    ic_name = discord.ui.TextInput(label="IC-ник персонажа", max_length=80)
    until = discord.ui.TextInput(
        label="Время",
        placeholder="Например: 21:30, 2ч или 90м",
        max_length=20,
    )

    def __init__(self, car_id: int):
        super().__init__()
        self.car_id = car_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            ends_at = _until(str(self.until))
        except ValueError:
            await respond_v2(interaction,
                "Укажите время как `21:30`, `2ч` или `90м` (не более 7 дней).",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        starts_at = datetime.now(UTC)
        result, booking = await create_car_booking(
            self.car_id,
            interaction.user.id,
            str(self.ic_name),
            starts_at.isoformat(),
            ends_at.isoformat(),
        )
        if result == "unavailable":
            await followup_spec(
                interaction,
                system_error(
                    code="VEH.UNAVAILABLE",
                    summary=(
                        "Эта машина требует сверки или вернулась владельцу "
                        "и не считается свободной. Бронь не создана."
                    ),
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        if result != "booked":
            await followup_v2(interaction,
                "Машина уже занята или была удалена. Обновите выбор.", ephemeral=True
            )
            return
        number, name, plate, owner = booking
        timestamp = int(ends_at.timestamp())
        # The booking is committed. Publishing the log used to run inline and
        # raised RuntimeError when the log channel was missing, so the user was
        # told the action failed while their car was in fact booked — and
        # booked it again. Both side effects now retry in the background.
        await enqueue_operation(
            idempotency_key=f"car.booking_log:{number}",
            guild_id=interaction.guild.id,
            kind="car.booking_log",
            aggregate_id=f"booking:{number}",
            payload={
                "number": number,
                "car_name": name,
                "plate": plate,
                "owner": owner,
                "booked_by": interaction.user.id,
                "ic_name": str(self.ic_name),
                "ends_at_ts": timestamp,
            },
        )
        await _request_panel_refresh(interaction.guild.id, f"booking:{number}")
        await followup_spec(
            interaction,
            queued(
                code="CAR.BOOKED",
                title="Машина забронирована",
                summary=f"{name} закреплена за вами до <t:{timestamp}:t>.",
                details=(("Бронирование", f"№{number:04d}"),),
                next_step="Панель автопарка обновится в течение минуты.",
                reference=f"№{number:04d}",
            ),
            ephemeral=True,
        )


class CarSelect(discord.ui.Select):
    def __init__(self, cars):
        # Discord allows at most 25 select options. Silently listing the first
        # 25 made the rest look non-existent; the placeholder now says how many
        # are shown so the operator knows to search the full list instead.
        visible = cars[:25]
        placeholder = (
            "Выберите свободную машину"
            if len(cars) <= 25
            else f"Свободных {len(cars)}, показаны первые 25"
        )
        super().__init__(
            placeholder=placeholder,
            options=[
                discord.SelectOption(label=name[:100], description=f"{plate} · {owner}"[:100], value=str(car_id))
                for car_id, name, plate, owner, *_ in visible
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BookingModal(int(self.values[0])))


class CarSelectView(SafeView):
    def __init__(self, cars):
        super().__init__(timeout=180)
        self.add_item(CarSelect(cars))


class CancelSelect(discord.ui.Select):
    def __init__(self, bookings):
        super().__init__(
            placeholder="Выберите бронь для отмены",
            options=[
                discord.SelectOption(label=name[:100], description=plate[:100], value=str(booking_id))
                for booking_id, name, plate, _ in bookings[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        booking = await cancel_car_booking(int(self.values[0]), interaction.user.id)
        if booking is None:
            await followup_v2(interaction, "Бронь уже неактивна.", ephemeral=True)
            return
        number, name, plate, _ = booking
        spec = notice(
            code="CAR.CANCELLED",
            title=f"Отмена бронирования №{number:04d}",
            summary=f"{interaction.user.mention} отменил бронь **{name}** · `{plate}`.",
            next_step="Дополнительных действий не требуется.",
            reference=f"№{number:04d}",
        )
        await _booking_log(interaction.guild, spec)
        await update_car_list_message(interaction.guild)
        await followup_v2(interaction, f"Бронь {name} отменена.", ephemeral=True)


class CancelSelectView(SafeView):
    def __init__(self, bookings):
        super().__init__(timeout=180)
        self.add_item(CancelSelect(bookings))


class CarParkView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Забронировать", style=discord.ButtonStyle.success, custom_id="car_park:book")
    async def book(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cars = [
            row
            for row in await get_cars_with_active_bookings()
            if row[4] is None and is_bookable(_row_state(row))
        ]
        if not cars:
            await followup_v2(interaction, "Свободных машин сейчас нет.", ephemeral=True)
            return
        await followup_v2(interaction,
            "Выберите машину:", view=CarSelectView(cars), ephemeral=True
        )

    @discord.ui.button(
        label="Все машины",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
        custom_id="car_park:full_list",
    )
    async def full_list(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await get_cars_with_active_bookings()
        free = [row for row in rows if _is_free(row)]
        booked = [row for row in rows if row[5] and is_bookable(_row_state(row))]
        recon = [row for row in rows if not is_bookable(_row_state(row))]
        await send_paged_list(
            interaction,
            _car_lines(free + booked + recon),
            title="🚘 Список машин",
            colour=WHITE_COLOR,
            empty="Машины ещё не добавлены.",
            footer=(
                f"Свободно: {len(free)} · занято: {len(booked)}"
                + (f" · сверка: {len(recon)}" if recon else "")
            ),
        )

    @discord.ui.button(label="Отменить бронь", style=discord.ButtonStyle.danger, custom_id="car_park:cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        bookings = await get_active_user_car_bookings(interaction.user.id)
        if not bookings:
            await followup_v2(interaction, "У вас нет активных броней.", ephemeral=True)
            return
        await followup_v2(interaction,
            "Выберите бронь:", view=CancelSelectView(bookings), ephemeral=True
        )


class CarParkCog(
    commands.GroupCog,
    group_name="car",
    group_description="Управление семейным автопарком",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await init_db()
        self.bot.add_view(CarParkView())
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.register("car.booking_log", self._publish_booking_log)
            worker.register("car.audit", self._publish_car_audit)
            worker.register("car.panel_refresh", self._refresh_panel_outbox)
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                "car-park-refresh",
                self.refresh_expired_bookings,
                interval=60,
                timeout=45,
            )

    async def cog_unload(self):
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("car-park-refresh")
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.unregister("car.booking_log")
            worker.unregister("car.audit")
            worker.unregister("car.panel_refresh")

    def _guild(self, operation: OutboxOperation) -> discord.Guild:
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {operation.guild_id} is unavailable")
        return guild

    async def _publish_booking_log(self, operation: OutboxOperation) -> int | None:
        guild = self._guild(operation)
        payload = operation.payload
        number = int(payload["number"])
        spec = notice(
            code="CAR.BOOKED",
            title=f"Бронирование №{number:04d}",
            details=(
                ("Машина", f"{payload['car_name']} · `{payload['plate']}`"),
                ("Принадлежит", str(payload["owner"])),
                ("Забронировал", f"<@{int(payload['booked_by'])}>"),
                ("IC-ник", str(payload["ic_name"])),
                ("До", f"<t:{int(payload['ends_at_ts'])}:F>"),
            ),
            next_step="Дополнительных действий не требуется.",
            reference=f"№{number:04d}",
        )
        await _booking_log(guild, spec)
        return None

    async def _publish_car_audit(self, operation: OutboxOperation) -> int | None:
        guild = self._guild(operation)
        spec = notice(
            code="CAR.UPDATED",
            title=str(operation.payload["title"]),
            summary=str(operation.payload["text"]),
            next_step="Дополнительных действий не требуется.",
        )
        await _booking_log(guild, spec)
        return None

    async def _refresh_panel_outbox(self, operation: OutboxOperation) -> int | None:
        guild = self._guild(operation)
        await update_car_list_message(guild)
        return None

    async def refresh_expired_bookings(self):
        """Refresh the panel for bookings that expired since the last good pass.

        The watermark advances only after a guild's refresh succeeded and is
        stored in the database. Moving it first — and in memory — meant a failed
        refresh permanently skipped that window, leaving expired bookings on the
        panel until an unrelated event happened to redraw it.
        """

        now = datetime.now(UTC)
        for guild in self.bot.guilds:
            if not get_text_channel(guild, "car_list"):
                continue
            previous = await get_watermark(guild.id, _EXPIRY_JOB)
            if previous and not await car_bookings_ended_since(
                previous,
                now.isoformat(),
            ):
                continue
            try:
                await update_car_list_message(guild)
            except (RuntimeError, discord.HTTPException):
                logger.exception("Failed to refresh car list for guild %s", guild.id)
                continue
            await set_watermark(guild.id, _EXPIRY_JOB, now.isoformat())

    @app_commands.command(name="add", description="Добавить машину в автопарк")
    @administrator_or_owner()
    async def car_add(
        self,
        interaction: discord.Interaction,
        название: app_commands.Range[str, 1, 100],
        номера: app_commands.Range[str, 1, 30],
        принадлежит: app_commands.Range[str, 1, 100],
        владелец: discord.Member | None = None,
        назначение: app_commands.Range[str, 1, 200] | None = None,
        грузоподъёмность: app_commands.Range[int, 0, 2_147_483_647] | None = None,
        уровень_матовозки: app_commands.Range[int, 1, 3] | None = None,
        ограничения: app_commands.Range[str, 1, 200] | None = None,
        доступ_по_рангам: app_commands.Range[str, 1, 200] | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            await followup_v2(
                interaction, "Автопарк ведут только на сервере.", ephemeral=True
            )
            return
        try:
            car = await register_vehicle(
                interaction.user,
                interaction.guild_id,
                name=название,
                plate=номера,
                owner=принадлежит,
                owner_discord_id=None if владелец is None else владелец.id,
                purpose="" if назначение is None else назначение,
                cargo_kg=грузоподъёмность,
                mat_level=уровень_матовозки,
                restrictions="" if ограничения is None else ограничения,
                access_note="" if доступ_по_рангам is None else доступ_по_рангам,
            )
        except VehicleError as exc:
            await followup_spec(interaction, _vehicle_error_spec(exc))
            return
        except DATABASE_INTEGRITY_ERRORS:
            await followup_v2(interaction, "Машина с такими номерами уже существует.", ephemeral=True)
            return
        await _request_panel_refresh(interaction.guild.id, f"car:{car.plate}")
        await enqueue_operation(
            idempotency_key=f"car.audit:{interaction.id}",
            guild_id=interaction.guild.id,
            kind="car.audit",
            aggregate_id=f"car:{car.plate}",
            payload={
                "title": "Машина добавлена",
                "text": (
                    f"{interaction.user.mention} добавил **{car.name}** · "
                    f"`{car.plate}`\nПринадлежит: **{car.owner}**"
                ),
            },
        )
        await followup_spec(
            interaction,
            notice(
                code="CAR.ADDED",
                title="Машина добавлена",
                summary=f"**{car.name}** (`{car.plate}`) записана в автопарк.",
                details=(
                    ("Владелец", car.owner),
                    (
                        "Грузоподъёмность",
                        "не подтверждена"
                        if car.cargo_kg <= 0
                        else f"{car.cargo_kg} кг",
                    ),
                ),
                next_step=f"Карточка — {cmd('car show')}.",
                accent=Accent.LOGISTICS,
                reference=car.plate,
            ),
        )

    async def car_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        rows = await get_cars_with_active_bookings()
        query = current.casefold()
        return [
            app_commands.Choice(
                name=f"{name} · {plate}"[:100], value=car_id
            )
            for car_id, name, plate, *_ in rows
            if query in name.casefold() or query in plate.casefold()
        ][:25]

    async def active_booking_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        rows = await get_active_car_bookings()
        query = current.casefold().strip()
        return [
            app_commands.Choice(
                name=f"{name} · {plate} · {ic_name}"[:100],
                value=booking_id,
            )
            for booking_id, name, plate, _, ic_name, _ in rows
            if not query
            or query in name.casefold()
            or query in plate.casefold()
            or query in str(ic_name).casefold()
        ][:25]

    @app_commands.command(name="remove", description="Удалить машину из автопарка")
    @administrator_or_owner()
    @app_commands.autocomplete(машина=car_autocomplete)
    async def car_remove(self, interaction: discord.Interaction, машина: int):
        await interaction.response.defer(ephemeral=True)
        result, car = await remove_car(машина)
        if result == "missing":
            await followup_v2(interaction, "Машина не найдена.", ephemeral=True)
            return
        if result == "busy":
            await followup_v2(interaction,
                "Сначала отмените активную бронь этой машины.", ephemeral=True
            )
            return
        _, name, plate, owner = car
        await update_car_list_message(interaction.guild)
        spec = notice(
            code="CAR.DELETED",
            title="Машина удалена",
            summary=(
                f"{interaction.user.mention} удалил **{name}** · `{plate}`\n"
                f"Принадлежала: **{owner}**"
            ),
            next_step="Дополнительных действий не требуется.",
            reference=plate,
        )
        await _booking_log(interaction.guild, spec)
        await followup_v2(interaction,
            f"Машина **{name}** (`{plate}`) удалена.", ephemeral=True
        )

    @app_commands.command(name="edit", description="Изменить данные машины в автопарке")
    @administrator_or_owner()
    @app_commands.autocomplete(машина=car_autocomplete)
    async def car_edit(
        self,
        interaction: discord.Interaction,
        машина: int,
        название: str | None = None,
        номера: str | None = None,
        принадлежит: str | None = None,
    ):
        if not any((название, номера, принадлежит)):
            await respond_v2(interaction,
                "Укажите хотя бы одно новое значение.", ephemeral=True
            )
            return
        if название is not None and not 1 <= len(название.strip()) <= 100:
            await respond_v2(interaction,
                "Название должно содержать от 1 до 100 символов.", ephemeral=True
            )
            return
        if номера is not None and not 1 <= len(номера.strip()) <= 30:
            await respond_v2(interaction,
                "Номера должны содержать от 1 до 30 символов.", ephemeral=True
            )
            return
        if принадлежит is not None and not 1 <= len(принадлежит.strip()) <= 100:
            await respond_v2(interaction,
                "Поле принадлежности должно содержать от 1 до 100 символов.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await edit_car(
                машина, name=название, plate=номера, owner=принадлежит
            )
        except DATABASE_INTEGRITY_ERRORS:
            await followup_v2(interaction,
                "Машина с такими номерами уже существует.", ephemeral=True
            )
            return
        if result is None:
            await followup_v2(interaction, "Машина не найдена.", ephemeral=True)
            return
        old, new = result
        await update_car_list_message(interaction.guild)
        spec = notice(
            code="CAR.UPDATED",
            title="Данные машины изменены",
            summary=(
                f"Изменил: {interaction.user.mention}\n"
                f"**Было:** {old[1]} · `{old[2]}` · {old[3]}\n"
                f"**Стало:** {new[1]} · `{new[2]}` · {new[3]}"
            ),
            next_step="Дополнительных действий не требуется.",
            reference=str(new[2]),
        )
        await _booking_log(interaction.guild, spec)
        await followup_v2(interaction,
            f"Данные машины **{new[1]}** обновлены.", ephemeral=True
        )

    @app_commands.command(
        name="booking_cancel",
        description="Принудительно отменить активную бронь",
    )
    @administrator_or_owner()
    @app_commands.autocomplete(бронь=active_booking_autocomplete)
    async def car_booking_cancel(
        self,
        interaction: discord.Interaction,
        бронь: int,
        причина: app_commands.Range[str, 1, 200] | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        booking = await force_cancel_car_booking(бронь, interaction.user.id)
        if booking is None:
            await followup_v2(
                interaction,
                "Активная бронь не найдена или уже завершена.",
                ephemeral=True,
            )
            return
        number, name, plate, user_id, ic_name, _ = booking
        details: list[tuple[str, str]] = [
            ("Машина", f"**{name}** · `{plate}`"),
            ("Бронь пользователя", f"<@{user_id}>"),
            ("IC-ник", str(ic_name)),
            ("Отменил", interaction.user.mention),
        ]
        if причина:
            details.append(("Причина", str(причина)))
        spec = notice(
            code="CAR.CANCELLED",
            title=f"Принудительная отмена бронирования №{number:04d}",
            details=tuple(details),
            next_step="Дополнительных действий не требуется.",
            tone=MessageTone.ERROR,
            reference=f"№{number:04d}",
        )
        await _booking_log(interaction.guild, spec)
        await update_car_list_message(interaction.guild)
        await followup_v2(
            interaction,
            f"Бронь **{name}** (`{plate}`) принудительно отменена.",
            ephemeral=True,
        )

    @app_commands.command(name="show", description="Карточка машины: владелец, состояние, груз")
    @administrator_or_owner()
    @app_commands.autocomplete(машина=car_autocomplete)
    async def car_show(self, interaction: discord.Interaction, машина: int) -> None:
        await interaction.response.defer(ephemeral=True)
        car = await get_car(машина)
        if car is None:
            await followup_v2(interaction, "Машина не найдена.", ephemeral=True)
            return
        await followup_spec(interaction, vehicle_card(car))

    @app_commands.command(name="assign", description="Назначить владельца, груз и ограничения")
    @administrator_or_owner()
    @app_commands.autocomplete(машина=car_autocomplete)
    async def car_assign(
        self,
        interaction: discord.Interaction,
        машина: int,
        владелец: discord.Member | None = None,
        назначение: app_commands.Range[str, 1, 200] | None = None,
        грузоподъёмность: app_commands.Range[int, 0, 2_147_483_647] | None = None,
        уровень_матовозки: app_commands.Range[int, 1, 3] | None = None,
        ограничения: app_commands.Range[str, 1, 200] | None = None,
        доступ_по_рангам: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        if not any(
            (
                владелец,
                назначение,
                грузоподъёмность is not None,
                уровень_матовозки,
                ограничения,
                доступ_по_рангам,
            )
        ):
            await respond_v2(
                interaction,
                "Укажите хотя бы одно поле. Назначение не записано.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            await followup_v2(
                interaction, "Автопарк ведут только на сервере.", ephemeral=True
            )
            return
        try:
            car = await assign_vehicle(
                interaction.user,
                interaction.guild_id,
                машина,
                owner_discord_id=None if владелец is None else владелец.id,
                purpose=назначение,
                cargo_kg=грузоподъёмность,
                mat_level=уровень_матовозки,
                restrictions=ограничения,
                access_note=доступ_по_рангам,
            )
        except VehicleError as exc:
            await followup_spec(interaction, _vehicle_error_spec(exc))
            return
        await update_car_list_message(interaction.guild)
        await followup_spec(interaction, vehicle_card(car))

    @app_commands.command(name="recon", description="Закрыть сверку после выхода владельца")
    @administrator_or_owner()
    @app_commands.autocomplete(машина=car_autocomplete)
    @app_commands.choices(
        решение=[
            app_commands.Choice(name="Оставить семье", value="keep"),
            app_commands.Choice(name="Вернулся владельцу", value="returned"),
        ]
    )
    async def car_recon(
        self,
        interaction: discord.Interaction,
        машина: int,
        решение: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            await followup_v2(
                interaction, "Автопарк ведут только на сервере.", ephemeral=True
            )
            return
        try:
            car = await recon_vehicle(
                interaction.user,
                interaction.guild_id,
                машина,
                returned_to_owner=решение.value == "returned",
            )
        except VehicleError as exc:
            await followup_spec(interaction, _vehicle_error_spec(exc))
            return
        await update_car_list_message(interaction.guild)
        await followup_spec(interaction, vehicle_card(car))

    @app_commands.command(name="reserve", description="Забронировать машину под событие")
    @administrator_or_owner()
    @app_commands.autocomplete(машина=car_autocomplete)
    async def car_reserve(
        self,
        interaction: discord.Interaction,
        машина: int,
        событие: int,
        ic_ник: app_commands.Range[str, 1, 80],
        водитель: discord.Member | None = None,
        назначение: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            await followup_v2(
                interaction, "Автопарк ведут только на сервере.", ephemeral=True
            )
            return
        try:
            result = await book_vehicle(
                interaction.user,
                interaction.guild_id,
                машина,
                ic_name=ic_ник,
                starts_at="",
                ends_at="",
                purpose="" if назначение is None else назначение,
                event_id=событие,
                driver_discord_id=None if водитель is None else водитель.id,
            )
        except VehicleError as exc:
            await followup_spec(interaction, _vehicle_error_spec(exc))
            return
        booking = result.booking
        car = result.car
        if booking is None or car is None:
            await followup_v2(interaction, "Бронь не создана.", ephemeral=True)
            return
        await _request_panel_refresh(interaction.guild.id, f"booking:{booking.id}")
        await followup_spec(
            interaction,
            queued(
                code="CAR.RESERVED",
                title="Машина зарезервирована под событие",
                summary=f"{car.name} закреплена на интервал события.",
                details=(
                    ("Бронирование", f"№{booking.id:04d}"),
                    ("Событие", str(booking.event_id)),
                ),
                next_step="Панель автопарка обновится в течение минуты.",
                reference=f"№{booking.id:04d}",
            ),
        )

    @app_commands.command(name="return", description="Отметить факт возврата машины после брони")
    @administrator_or_owner()
    @app_commands.autocomplete(бронь=active_booking_autocomplete)
    async def car_return(self, interaction: discord.Interaction, бронь: int) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            booking = await return_booking(interaction.user, бронь)
        except VehicleError as exc:
            await followup_spec(interaction, _vehicle_error_spec(exc))
            return
        if interaction.guild is not None:
            await update_car_list_message(interaction.guild)
        await followup_spec(
            interaction,
            notice(
                code="CAR.RETURNED",
                title=f"Возврат брони №{booking.id:04d}",
                summary="Факт возврата записан. Интервал больше не занимает машину.",
                next_step="Дополнительных действий не требуется.",
                accent=Accent.LOGISTICS,
                reference=f"№{booking.id:04d}",
            ),
        )

    @app_commands.command(name="property", description="Записать недвижимость семьи")
    @administrator_or_owner()
    @app_commands.choices(
        вид=[
            app_commands.Choice(name="Офис", value="office"),
            app_commands.Choice(name="Склад", value="warehouse"),
            app_commands.Choice(name="Другое", value="other"),
        ]
    )
    async def car_property(
        self,
        interaction: discord.Interaction,
        вид: app_commands.Choice[str],
        название: app_commands.Range[str, 1, 100],
        ответственный: discord.Member,
        резервный: discord.Member,
        срок: app_commands.Range[str, 1, 40],
        дата_оплаты: app_commands.Range[str, 1, 40] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            await followup_v2(
                interaction, "Имущество ведут только на сервере.", ephemeral=True
            )
            return
        try:
            item = await upsert_property(
                interaction.user,
                interaction.guild_id,
                kind=вид.value,
                title=название,
                responsible_id=ответственный.id,
                backup_id=резервный.id,
                due_at=срок,
                last_paid_at=None if дата_оплаты is None else дата_оплаты,
            )
        except VehicleError as exc:
            await followup_spec(interaction, _vehicle_error_spec(exc))
            return
        await followup_spec(interaction, property_card(item))

    @app_commands.command(name="property_mark", description="Отметить исполнение оплаты имущества")
    @administrator_or_owner()
    @app_commands.choices(
        вид=[
            app_commands.Choice(name="Офис", value="office"),
            app_commands.Choice(name="Склад", value="warehouse"),
            app_commands.Choice(name="Другое", value="other"),
        ]
    )
    async def car_property_mark(
        self,
        interaction: discord.Interaction,
        вид: app_commands.Choice[str],
        название: app_commands.Range[str, 1, 100],
        дата_оплаты: app_commands.Range[str, 1, 40] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            await followup_v2(
                interaction, "Имущество ведут только на сервере.", ephemeral=True
            )
            return
        try:
            item = await execute_property(
                interaction.user,
                interaction.guild_id,
                kind=вид.value,
                title=название,
                paid_at=None if дата_оплаты is None else дата_оплаты,
            )
        except VehicleError as exc:
            await followup_spec(interaction, _vehicle_error_spec(exc))
            return
        await followup_spec(interaction, property_card(item))


async def setup(bot: commands.Bot):
    await bot.add_cog(CarParkCog(bot))
