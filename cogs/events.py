"""Slash surface for universal family events."""

from __future__ import annotations

from collections.abc import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from repositories.events import get_event
from repositories.operations import OutboxOperation
from services.calendar import calendar_day_spec, reminder_message
from services.event_planning import (
    ATTENDANCE_LABELS,
    EventError,
    attendance_stats,
    cancel_event,
    confirm_attendance,
    confirm_ready,
    create_event,
    ensure_event_integrations,
    event_card,
    excuse_member,
    join_event,
    leave_event,
    list_events,
    list_templates,
    lock_roster,
    mark_voice_hint,
    parse_role_quotas,
    record_event_outcome,
    replace_after_lock,
    replace_commander,
    reschedule_event,
    reserve_queue,
    roster_lines,
    show_event,
    templates_spec,
)
from services.maps import (
    MapError,
    add_map,
    archive_map,
    catalog_spec,
    list_maps,
)
from services.cooldowns import cooldown_claim, cooldown_retry_text
from ui.command_registry import cmd
from ui.design import Accent
from ui.message_specs import MessageSpec
from ui.messages import followup_spec, send_spec
from ui.navigation import (
    ConfirmDialog,
    FilterSpec,
    PageState,
    Paginator,
    register_page_source,
    state_from_id,
    unregister_sources,
)
from ui.templates import missing_permission, notice, system_error
from utils.access import has_administrator_access, requires

_DOMAIN = "events"


def _error_spec(exc: EventError | MapError) -> MessageSpec:
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


class EventsPageSource:
    async def get_lines(self, interaction: discord.Interaction, parsed) -> Sequence[str]:
        guild_id = interaction.guild_id
        if guild_id is None:
            return ("Состав события доступен только на сервере.",)
        try:
            event_id = int(parsed.object_id)
        except ValueError:
            return ("Некорректный номер события.",)
        try:
            event = await show_event(interaction.user, guild_id, event_id)
        except EventError as exc:
            return (str(exc),)
        state = state_from_id(parsed)
        slot = state.filter if state.filter in {"main", "reserve"} else None
        lines = await roster_lines(event, slot=slot)
        return lines or ("Пока никого нет в составе.",)

    async def allowed(self, interaction: discord.Interaction, parsed) -> bool:
        del parsed
        return interaction.guild_id is not None

    def title(self, parsed) -> str:
        return f"Состав события #{parsed.object_id}"


class EventsCog(commands.Cog):
    event = app_commands.Group(
        name="event",
        description="События семьи и состав",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        ensure_event_integrations()
        register_page_source(_DOMAIN, EventsPageSource())
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.register("event.reserve_replace", self._notify_replace)
            worker.register("event.remind.prep", self._notify_reminder)
            worker.register("event.remind.aftermath", self._notify_reminder)
            worker.register("event.remind.warehouse", self._notify_reminder)
            worker.register("event.remind.delivery", self._notify_reminder)

    async def cog_unload(self) -> None:
        unregister_sources(_DOMAIN)
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.unregister("event.reserve_replace")
            worker.unregister("event.remind.prep")
            worker.unregister("event.remind.aftermath")
            worker.unregister("event.remind.warehouse")
            worker.unregister("event.remind.delivery")

    async def _notify_replace(self, operation: OutboxOperation) -> int | None:
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {operation.guild_id} is unavailable")
        spec = notice(
            code="EVENTS.REPLACED",
            title=str(operation.payload.get("title") or "Событие"),
            summary=(
                f"Замена в составе: <@{int(operation.payload['outgoing_id'])}> "
                f"уступает место <@{int(operation.payload['incoming_id'])}>."
            ),
            details=(("Причина", str(operation.payload.get("reason") or "—")),),
            next_step=f"Состав — {cmd('event roster')}.",
            reference=str(operation.payload.get("event_id")),
        )
        sent = 0
        for user_id in (
            int(operation.payload["outgoing_id"]),
            int(operation.payload["incoming_id"]),
        ):
            member = guild.get_member(user_id)
            if member is None:
                continue
            await send_spec(member, spec)
            sent += 1
        return sent or None

    async def _notify_reminder(self, operation: OutboxOperation) -> int | None:
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {operation.guild_id} is unavailable")
        event_id = int(operation.payload["event_id"])
        event = await get_event(event_id)
        if event is None:
            return None
        spec = await reminder_message(event, operation.payload)
        target_id = int(operation.payload.get("commander_id") or event.commander_id)
        member = guild.get_member(target_id)
        if member is None:
            return None
        await send_spec(member, spec)
        return 1

    @event.command(name="templates", description="Доступные шаблоны событий")
    @requires(Permission.EVENTS_CREATE)
    async def templates_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Шаблоны доступны только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            items = await list_templates(interaction.user, guild_id)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, templates_spec(items))

    @event.command(name="map_add", description="Добавить карту в каталог семьи")
    @app_commands.describe(
        name="Название карты",
        event_type="Тип события, для которого карта проверена",
        version="Версия карты",
        source="Источник, по которому запись можно проверить",
    )
    @requires(Permission.EVENTS_MANAGE_ALL)
    async def map_add_command(
        self,
        interaction: discord.Interaction,
        name: str,
        event_type: str,
        version: str,
        source: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MAPS.GUILD_ONLY",
                    summary="Каталог карт ведётся только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await add_map(
                interaction.user,
                guild_id,
                name=name,
                event_type=event_type,
                version=version,
                source=source,
            )
        except MapError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="MAPS.ADDED",
                title="Карта записана",
                summary=(
                    f"«{item.name}» добавлена в каталог. "
                    f"Тип: {item.event_type}. Версия: {item.version}."
                ),
                details=(("Источник", item.source),),
                next_step=f"Список — {cmd('event map_list')}.",
                reference=str(item.id),
            ),
        )

    @event.command(name="map_list", description="Показать каталог карт")
    @requires(Permission.EVENTS_CREATE)
    async def map_list_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MAPS.GUILD_ONLY",
                    summary="Каталог карт доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            items = await list_maps(interaction.user, guild_id)
        except MapError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, catalog_spec(items))

    @event.command(name="map_archive", description="Архивировать карту каталога")
    @app_commands.describe(map_id="Номер записи каталога")
    @requires(Permission.EVENTS_MANAGE_ALL)
    async def map_archive_command(
        self,
        interaction: discord.Interaction,
        map_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MAPS.GUILD_ONLY",
                    summary="Каталог карт ведётся только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            item = await archive_map(interaction.user, guild_id, map_id)
        except MapError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="MAPS.ARCHIVED",
                title="Карта в архиве",
                summary=f"«{item.name}» больше не предлагается при выборе.",
                next_step=f"Актуальный список — {cmd('event map_list')}.",
                reference=str(item.id),
            ),
        )

    @event.command(name="create", description="Создать событие по шаблону")
    @app_commands.describe(
        template="Ключ шаблона, например capt_mansion",
        starts_at="Начало в ISO",
        ends_at="Окончание в ISO",
        meetup="Место сбора",
        capacity="Вместимость, если шаблон её не задаёт",
        reserve="Число мест в резерве",
        workshop_count="Сколько цехов, только для захвата цехов",
        resource_plan="Ресурсный план",
        f2_ref="Идентификатор события во вкладке F2",
        commander="Командир; по умолчанию вы",
        role_quotas="Квоты ролей, например assault:2,support:3",
    )
    @requires(Permission.EVENTS_CREATE)
    async def create_command(
        self,
        interaction: discord.Interaction,
        template: str,
        starts_at: str,
        ends_at: str,
        meetup: str,
        capacity: int | None = None,
        reserve: int = 0,
        workshop_count: int | None = None,
        resource_plan: str | None = None,
        f2_ref: str | None = None,
        commander: discord.Member | None = None,
        role_quotas: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Событие создаётся только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        async with cooldown_claim(
            guild_id,
            "event_create",
            interaction.user.id,
            bypass=has_administrator_access(interaction.user),
        ) as claim:
            if not claim.allowed:
                await followup_spec(
                    interaction,
                    system_error(
                        code="EVENTS.COOLDOWN",
                        summary=cooldown_retry_text(claim.retry_after),
                        retryable=True,
                        reference=current_reference(),
                    ),
                )
                return
            try:
                event = await create_event(
                    interaction.user,
                    guild_id,
                    template,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    meetup=meetup,
                    capacity=capacity,
                    reserve_capacity=reserve,
                    workshop_count=workshop_count,
                    resource_plan=resource_plan,
                    f2_event_ref=f2_ref,
                    commander_id=None if commander is None else commander.id,
                    role_quotas=parse_role_quotas(role_quotas),
                )
            except EventError as exc:
                await followup_spec(interaction, _error_spec(exc))
                return
            claim.keep()
        await followup_spec(interaction, event_card(event))

    @event.command(name="list", description="Ближайшие события")
    async def list_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Список доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            items = await list_events(interaction.user, guild_id)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        if not items:
            await followup_spec(
                interaction,
                notice(
                    code="EVENTS.EMPTY",
                    title="Событий нет",
                    summary="Запланированных событий сейчас нет.",
                    next_step=f"Создать — {cmd('event create')}.",
                    reference=current_reference(),
                ),
            )
            return
        details = tuple(
            (f"#{item.id}", f"{item.title} · {item.starts_at} · до {item.capacity}")
            for item in items[:12]
        )
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.LIST",
                title="События семьи",
                summary=f"Показано {min(len(items), 12)} из {len(items)}.",
                details=details,
                next_step=f"Карточка — {cmd('event show')}.",
                reference=current_reference(),
            ),
        )

    @event.command(name="show", description="Карточка события")
    @app_commands.describe(event_id="Номер события")
    async def show_command(
        self, interaction: discord.Interaction, event_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Карточка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            event = await show_event(interaction.user, guild_id, event_id)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, event_card(event))

    @event.command(name="roster", description="Состав события по страницам")
    @app_commands.describe(event_id="Номер события", slot="основа или резерв")
    async def roster_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        slot: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Состав доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            event = await show_event(interaction.user, guild_id, event_id)
            lines = await roster_lines(event, slot=slot)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        pager = Paginator(
            domain=_DOMAIN,
            object_id=str(event.id),
            lines=lines or ["Пока никого нет в составе."],
            state=PageState(
                page=1,
                filter=slot if slot in {"main", "reserve"} else "all",
                owner_id=interaction.user.id,
            ),
            title=f"Состав: {event.title}",
            accent=Accent.EVENTS,
            filters=(
                FilterSpec(
                    key="f",
                    placeholder="Слот",
                    options=(
                        ("all", "Все"),
                        ("main", "Основа"),
                        ("reserve", "Резерв"),
                    ),
                ),
            ),
        )
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.ROSTER",
                title=f"Состав события #{event.id}",
                summary=(
                    f"Вместимость {event.capacity}, резерв {event.reserve_capacity}. "
                    "Число мест не зависит от кнопок Discord."
                ),
                next_step=f"Запись — {cmd('event join')}.",
                reference=str(event.id),
            ),
            view=pager,
        )

    @event.command(name="join", description="Записаться в состав")
    @app_commands.describe(event_id="Номер события", role="Роль из шаблона")
    async def join_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        role: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Запись доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            result = await join_event(
                interaction.user, guild_id, event_id, role_key=role
            )
        except EventError as exc:
            if exc.code == "EVENTS.SCHEDULE_CONFLICT":

                async def _confirm(follow_up: discord.Interaction) -> None:
                    try:
                        confirmed = await join_event(
                            follow_up.user,
                            guild_id,
                            event_id,
                            role_key=role,
                            confirm_conflict=True,
                        )
                    except EventError as inner:
                        await followup_spec(follow_up, _error_spec(inner))
                        return
                    summary = f"Слот: {confirmed.seat.slot}. Конфликт расписания подтверждён."
                    if confirmed.overlap_warning:
                        summary = f"{summary} {confirmed.overlap_warning}"
                    await followup_spec(
                        follow_up,
                        notice(
                            code="EVENTS.JOINED",
                            title=confirmed.event.title,
                            summary=summary,
                            next_step=f"Состав — {cmd('event roster')}.",
                            reference=str(confirmed.event.id),
                        ),
                    )

                dialog = ConfirmDialog(
                    domain=_DOMAIN,
                    object_id=str(event_id),
                    summary=str(exc),
                    actor=str(interaction.user),
                    on_confirm=_confirm,
                    owner_id=interaction.user.id,
                )
                await followup_spec(
                    interaction,
                    notice(
                        code="EVENTS.SCHEDULE_CONFLICT",
                        title="Конфликт 18:00 и 18:30",
                        summary=str(exc),
                        next_step="Подтвердите, если сознательно идёте в оба состава.",
                        reference=current_reference(),
                    ),
                    view=dialog,
                )
                return
            await followup_spec(interaction, _error_spec(exc))
            return
        summary = f"Слот: {result.seat.slot}."
        if result.overlap_warning:
            summary = f"{summary} {result.overlap_warning}"
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.JOINED",
                title=result.event.title,
                summary=summary,
                next_step=f"Состав — {cmd('event roster')}.",
                reference=str(result.event.id),
            ),
        )

    @event.command(name="leave", description="Снять запись с события")
    @app_commands.describe(event_id="Номер события")
    async def leave_command(
        self, interaction: discord.Interaction, event_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Снять запись можно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            result = await leave_event(interaction.user, guild_id, event_id)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        summary = "Вас больше нет в составе."
        if result.promoted is not None:
            summary = (
                f"{summary} Из резерва по времени записи подняли "
                f"<@{result.promoted.discord_id}>."
            )
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.LEFT",
                title=result.event.title,
                summary=summary,
                next_step=f"Записаться снова — {cmd('event join')}.",
                reference=str(result.event.id),
            ),
        )

    @event.command(name="cancel", description="Отменить событие")
    @app_commands.describe(event_id="Номер события")
    async def cancel_command(
        self, interaction: discord.Interaction, event_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Отмена доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            event = await cancel_event(interaction.user, guild_id, event_id)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, event_card(event))

    @event.command(name="reschedule", description="Перенести событие")
    @app_commands.describe(
        event_id="Номер события",
        starts_at="Новое начало в ISO",
        ends_at="Новое окончание в ISO",
    )
    async def reschedule_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        starts_at: str,
        ends_at: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Перенос доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            event = await reschedule_event(
                interaction.user,
                guild_id,
                event_id,
                starts_at=starts_at,
                ends_at=ends_at,
            )
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, event_card(event))

    @event.command(name="commander", description="Заменить командира")
    @app_commands.describe(event_id="Номер события", commander="Новый командир")
    async def commander_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        commander: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Замена командира доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            event = await replace_commander(
                interaction.user, guild_id, event_id, commander.id
            )
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, event_card(event))

    @event.command(name="ready", description="Подтвердить готовность")
    @app_commands.describe(event_id="Номер события")
    async def ready_command(
        self, interaction: discord.Interaction, event_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Готовность отмечают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            seat = await confirm_ready(interaction.user, guild_id, event_id)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.READY",
                title="Готовность",
                summary=f"Статус: {ATTENDANCE_LABELS.get(seat.attendance, seat.attendance)}.",
                next_step="Итоговую явку ставит командир события, не голос.",
                reference=str(event_id),
            ),
        )

    @event.command(name="lock", description="Зафиксировать состав")
    @app_commands.describe(event_id="Номер события")
    async def lock_command(
        self, interaction: discord.Interaction, event_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Фиксация доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            event = await lock_roster(interaction.user, guild_id, event_id)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, event_card(event))

    @event.command(name="attendance", description="Итог явки от командира события")
    @app_commands.describe(
        event_id="Номер события",
        member="Участник состава",
        result="участвовал или не явился",
    )
    @app_commands.choices(
        result=[
            app_commands.Choice(name="участвовал", value="attended"),
            app_commands.Choice(name="не явился", value="no_show"),
        ]
    )
    async def attendance_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        member: discord.Member,
        result: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Явку отмечают только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            seat = await confirm_attendance(
                interaction.user, guild_id, event_id, member.id, result.value
            )
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.ATTENDANCE",
                title="Явка",
                summary=(
                    f"<@{seat.discord_id}>: "
                    f"{ATTENDANCE_LABELS.get(seat.attendance, seat.attendance)}. "
                    "Голос и кнопка готовности — только сверка, не доказательство."
                ),
                next_step="Неявка не наказывается ботом.",
                reference=str(event_id),
            ),
        )

    @event.command(name="excuse", description="Освободить от участия")
    @app_commands.describe(
        event_id="Номер события",
        member="Участник",
        reason="Причина согласованного отсутствия",
    )
    async def excuse_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        member: discord.Member,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Освобождение доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            seat = await excuse_member(
                interaction.user, guild_id, event_id, member.id, reason
            )
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.EXCUSED",
                title="Освобождён от участия",
                summary=f"<@{seat.discord_id}> не входит в статистику явки.",
                next_step=f"Состав — {cmd('event roster')}.",
                reference=str(event_id),
            ),
        )

    @event.command(name="replace", description="Замена после фиксации состава")
    @app_commands.describe(
        event_id="Номер события",
        outgoing="Кто уходит из основы",
        reason="Причина замены",
        incoming="Кто из резерва; пусто — первый по времени записи",
    )
    async def replace_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        outgoing: discord.Member,
        reason: str,
        incoming: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Замена доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            left, entered = await replace_after_lock(
                interaction.user,
                guild_id,
                event_id,
                outgoing.id,
                None if incoming is None else incoming.id,
                reason,
            )
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.REPLACED",
                title="Замена",
                summary=(
                    f"<@{left.discord_id}> уступил основу <@{entered.discord_id}>. "
                    "Обе стороны получат уведомление."
                ),
                next_step=f"Очередь резерва — {cmd('event reserve')}.",
                reference=str(event_id),
            ),
        )

    @event.command(name="reserve", description="Очередь резерва")
    @app_commands.describe(event_id="Номер события")
    async def reserve_command(
        self, interaction: discord.Interaction, event_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Очередь доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            event = await show_event(interaction.user, guild_id, event_id)
            places = await reserve_queue(event)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        if not places:
            await followup_spec(
                interaction,
                notice(
                    code="EVENTS.RESERVE_EMPTY",
                    title="Резерв",
                    summary="Очередь пуста.",
                    next_step=f"Запись — {cmd('event join')}.",
                    reference=str(event.id),
                ),
            )
            return
        details = tuple(
            (
                f"{place.position}",
                f"<@{place.seat.discord_id}> · {place.basis}",
            )
            for place in places[:12]
        )
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.RESERVE",
                title=f"Резерв события #{event.id}",
                summary=(
                    "Порядок — время записи. Допуск и роль в группе "
                    "не дают старшинства в очереди."
                ),
                details=details,
                next_step=(
                    "До фиксации подъём автоматический. "
                    f"После — {cmd('event replace')}."
                ),
                reference=str(event.id),
            ),
        )

    @event.command(name="stats", description="Статистика явки участника")
    @app_commands.describe(member="Участник; по умолчанию вы")
    async def stats_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Статистика доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        target = member or interaction.user
        try:
            stats = await attendance_stats(interaction.user, guild_id, target.id)
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.STATS",
                title="Явка",
                summary=(
                    "Считаются только события, на которые человек записался. "
                    "Отмена, отпуск и освобождение исключены. Неявка не наказывается."
                ),
                details=(
                    ("Обязательства", str(stats.obligated)),
                    ("Участвовал", str(stats.attended)),
                    ("Не явился", str(stats.no_show)),
                    ("Ещё без итога", str(stats.pending)),
                    ("Исключено: отмена", str(stats.cancelled_excluded)),
                    ("Исключено: освобождение", str(stats.excused_excluded)),
                    ("Исключено: отпуск", str(stats.leave_excluded)),
                ),
                next_step="Голос не доказывает участие.",
                reference=current_reference(),
            ),
        )

    @event.command(name="voice_hint", description="Подсказка «был в голосе», не явка")
    @app_commands.describe(event_id="Номер события", member="Кого отметить")
    async def voice_hint_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        member: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Подсказку пишут только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            seat = await mark_voice_hint(
                interaction.user, guild_id, event_id, member.id
            )
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="EVENTS.VOICE_HINT",
                title="Подсказка",
                summary=(
                    f"<@{seat.discord_id}> отмечен как бывший в голосе. "
                    "Это сверка, не доказательство участия."
                ),
                next_step=f"Итог явки — {cmd('event attendance')}.",
                reference=str(event_id),
            ),
        )

    @event.command(name="calendar", description="Расписание Memphis на день")
    @app_commands.describe(when="Дата ГГГГ-ММ-ДД; пусто — сегодня в поясе профиля")
    async def calendar_command(
        self, interaction: discord.Interaction, when: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Календарь доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            spec = await calendar_day_spec(guild_id, when)
        except (ValueError, EventError) as exc:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.BAD_TIME",
                    summary=(
                        f"{exc} Дата не разобрана. Напишите ГГГГ-ММ-ДД и повторите."
                    ),
                    retryable=True,
                    reference=current_reference(),
                ),
            )
            return
        await followup_spec(interaction, spec)

    @event.command(name="outcome", description="Зафиксировать победу или поражение")
    @app_commands.describe(event_id="Номер события", result="won или lost")
    @app_commands.choices(
        result=[
            app_commands.Choice(name="победа", value="won"),
            app_commands.Choice(name="поражение", value="lost"),
        ]
    )
    @requires(Permission.EVENTS_MANAGE_OWN)
    async def outcome_command(
        self,
        interaction: discord.Interaction,
        event_id: int,
        result: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="EVENTS.GUILD_ONLY",
                    summary="Итог события пишут только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            event = await record_event_outcome(
                interaction.user, guild_id, event_id, result
            )
        except EventError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, event_card(event))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsCog(bot))
