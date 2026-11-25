import asyncio
import json
import logging
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from repositories.operations import OutboxOperation
from repositories.tickets import complete_ticket_close
from repositories.requests import (
    get_pending_request_publications,
    get_request_decision,
    mark_request_presentation_complete,
    set_request_publication,
)
from services.panels import known_panel_keys, register_deployer, unavailable_reason
from services.static_messages import StaticPayload, upsert_static_message
from ui.layouts import marker_component_id
from ui.messages import edit_message_v2, edit_status_message_v2, send_v2

from cogs.contracts import upsert_contracts_panel
from cogs.member_panel import PersistentMeButton, upsert_me_panel
from cogs.applications import FamilyApprovalView, family_application_embed
from cogs.forms import (
    AddRolesView,
    BirthdayApprovalView,
    BirthdayListView,
    LegacyTicketUnassignView,
    PersistentBirthdayButton,
    PersistentFamilyButton,
    PersistentHelpButton,
    PersistentSSButton,
    PersistentVZPButton,
    NicknameChangedView,
    RoleApprovalView,
    TicketCloseView,
    TreasuryView,
    VZPApprovalView,
    _family_request_status,
    update_happy_birthday_message,
)
from config import CHANNELS
from utils.discord_lookup import (
    get_text_channel,
    request_ping_content,
    request_ping_mentions,
)
from ui.design import Accent
from ui.templates import notice
from utils.embeds import (
    STAFF_COLOR,
    MessageSpec,
    apply_accent,
    panel_embed,
    request_embed,
    role_request_embed,
)
from utils.views import stop_registered_view


logger = logging.getLogger(__name__)

class InteractionsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._approval_restore_lock = asyncio.Lock()
        self._registered_approvals: dict[
            tuple[str, int], discord.ui.View
        ] = {}

    async def cog_load(self):
        self.bot.add_view(PersistentHelpButton())
        self.bot.add_view(PersistentMeButton())
        self.bot.add_view(PersistentSSButton())
        self.bot.add_view(PersistentVZPButton())
        self.bot.add_view(PersistentFamilyButton())
        self.bot.add_view(PersistentBirthdayButton())
        self.bot.add_view(AddRolesView())
        self.bot.add_view(BirthdayListView())
        self.bot.add_view(NicknameChangedView())
        self.bot.add_view(TreasuryView())
        self.bot.add_view(TicketCloseView(0, "ticket"))
        self.bot.add_view(LegacyTicketUnassignView())
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.register("request.present", self._present_request_decision)
            worker.register("ticket.notify", self._notify_ticket_created)
            worker.register("ticket.audit", self._audit_ticket_event)
            worker.register("ticket.delete_channel", self._delete_ticket_channel)
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                "approval-view-reconcile",
                self._restore_pending_approvals,
                interval=300,
                timeout=120,
            )
        logger.info("Registered persistent views")

    async def cog_unload(self) -> None:
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("approval-view-reconcile")
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.unregister("request.present")
            worker.unregister("ticket.notify")
            worker.unregister("ticket.audit")
            worker.unregister("ticket.delete_channel")

    async def _notify_ticket_created(self, operation: OutboxOperation) -> int | None:
        """Publish the staff notification for a ticket that already exists."""

        from cogs.forms import _ticket_notification, _ticket_review_roles

        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {operation.guild_id} is unavailable")
        channel = guild.get_channel(int(operation.payload["channel_id"]))
        target = get_text_channel(guild, "vzp_invite")
        if target is None or (channel is not None and target.id == channel.id):
            # Nothing to notify: the review channel is the ticket itself.
            return None
        number = str(operation.payload["number"])
        clean_type = str(operation.payload["clean_type"])
        review_roles = _ticket_review_roles(guild, clean_type)
        notification = notice(
            code="TICKET.NOTIFY",
            title="🎫 Новое обращение",
            summary=(
                f"Создан новый тикет: {channel.mention}"
                if channel is not None
                else f"Создан новый тикет #{number}"
            ),
            details=(
                ("Номер", number),
                ("Тип", str(operation.payload["ticket_type"])),
                ("Автор", f"<@{int(operation.payload['author_id'])}>"),
            ),
            next_step="Откройте тикет и возьмите обращение в работу.",
            reference=number,
        )
        ping, mentions = _ticket_notification(guild, clean_type, review_roles)
        message = await send_v2(
            target,
            content=ping,
            embed=notification,
            allowed_mentions=mentions,
            component_marker=f"ticket-notify:{number}",
        )
        return message.id

    async def _audit_ticket_event(self, operation: OutboxOperation) -> int | None:
        from cogs.forms import _log_request_action

        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {operation.guild_id} is unavailable")
        await _log_request_action(guild, str(operation.payload["text"]))
        return None

    async def _delete_ticket_channel(self, operation: OutboxOperation) -> int | None:
        """Delete a closed ticket channel, then finalise its record.

        Runs after the statistics and assignment writes have committed, so a
        failure here retries a deletion instead of stranding a closed ticket.
        """

        channel_id = int(operation.payload["channel_id"])
        number = str(operation.payload.get("number") or channel_id)
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                channel = None
        if channel is not None:
            await channel.delete(reason=f"Тикет #{number} закрыт")
        await complete_ticket_close(channel_id)
        return None

    @staticmethod
    def _has_marker(message: discord.Message, marker: str) -> bool:
        expected = marker_component_id(marker)

        def visit(component) -> bool:
            return getattr(component, "id", None) == expected or any(
                visit(child) for child in getattr(component, "children", ())
            )

        return any(visit(component) for component in message.components)

    async def _find_marker_message(
        self, channel, marker: str, *, limit: int = 250
    ) -> discord.Message | None:
        async for candidate in channel.history(limit=limit):
            if (
                self.bot.user is not None
                and candidate.author.id == self.bot.user.id
                and self._has_marker(candidate, marker)
            ):
                return candidate
        return None

    async def _present_request_decision(
        self, operation: OutboxOperation
    ) -> int | None:
        number = str(operation.payload.get("number") or operation.aggregate_id or "")
        decision = await get_request_decision(number)
        if decision is None:
            raise RuntimeError(f"Request {number} no longer exists")
        if decision.presentation_state == "completed":
            return decision.message_id
        if decision.status not in {"approved", "rejected"}:
            raise RuntimeError(f"Request {number} is not finalized")

        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {operation.guild_id} is unavailable")
        channel_keys = {
            "role_request": "role_request",
            "vzp": "vzp_invite",
            "birthday": "birthday_request",
            "family_invite": "family_invite_logs",
        }
        channel = (
            guild.get_channel(decision.channel_id)
            if decision.channel_id is not None
            else None
        )
        if not isinstance(channel, discord.TextChannel):
            channel = get_text_channel(guild, channel_keys[decision.kind])
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"Review channel for {number} is unavailable")

        message = None
        if decision.message_id:
            try:
                message = await channel.fetch_message(decision.message_id)
            except discord.NotFound:
                message = None
        if message is None:
            message = await self._find_marker_message(channel, f"request:{number}")

        approved = decision.status == "approved"
        reviewer = (
            f"<@{decision.reviewer_id}>"
            if decision.reviewer_id is not None
            else "старший состав"
        )
        if decision.kind == "family_invite":
            status = _family_request_status(decision)
        else:
            status = (
                f"✅ Одобрено • {reviewer}"
                + (
                    f"\nВыдано: **{decision.reason}**"
                    if approved and decision.reason
                    else ""
                )
                if approved
                else f"❌ Отклонено • {reviewer}\nПричина: {decision.reason or 'Не указана'}"
            )
        accent = discord.Color(int(Accent.SUCCESS if approved else Accent.ERROR))
        if message is None:
            embed = apply_accent(
                notice(
                    code="REQUEST.STATUS",
                    title={
                        "role_request": "Запрос роли",
                        "vzp": "Заявка в боевую команду",
                        "birthday": "Запрос дня рождения",
                        "family_invite": "Заявка в семью",
                    }[decision.kind],
                    summary=(
                        f"Пользователь: <@{decision.user_id}>\n"
                        f"Номер: **{number}**\n\n{decision.content}"
                    ),
                    details=(("Статус", status),),
                    next_step="Решение уже зафиксировано.",
                    reference=number,
                ),
                Accent.SUCCESS if approved else Accent.ERROR,
            )
            message = await send_v2(
                channel,
                embed=embed,
                component_marker=f"request:{number}",
            )
            await set_request_publication(number, guild.id, channel.id, message.id)
        else:
            await edit_status_message_v2(
                message,
                status=status,
                accent_color=accent,
                view=None,
            )

        log_channel = get_text_channel(guild, "request_actions")
        if log_channel is None:
            raise RuntimeError("Request actions channel is unavailable")
        log_marker = f"request-decision:{number}"
        log_message = await self._find_marker_message(log_channel, log_marker)
        if log_message is None:
            log_embed = apply_accent(
                notice(
                    code="REQUEST.DECISION",
                    title="Заявка одобрена" if approved else "Заявка отклонена",
                    summary=(
                        f"Заявка: **{number}**\n"
                        f"Пользователь: <@{decision.user_id}>\n"
                        f"Обработал: {reviewer}"
                        + (
                            f"\nПричина: **{decision.reason or 'Не указана'}**"
                            if not approved
                            else ""
                        )
                    ),
                    next_step="Запись сохранена в журнале решений.",
                    reference=number,
                ),
                Accent.SUCCESS if approved else Accent.ERROR,
            )
            log_message = await send_v2(
                log_channel,
                embed=log_embed,
                component_marker=log_marker,
            )

        if not approved:
            member = guild.get_member(decision.user_id)
            if member is not None:
                try:
                    dm = member.dm_channel or await member.create_dm()
                    dm_marker = f"request-rejection-dm:{number}"
                    if await self._find_marker_message(dm, dm_marker, limit=100) is None:
                        await send_v2(
                            dm,
                            f"Ваша заявка **{number}** отклонена.\n"
                            f"Причина: **{decision.reason or 'Не указана'}**",
                            component_marker=dm_marker,
                        )
                except discord.Forbidden:
                    pass

        # Recruiting is part of the durable decision presentation.  Keeping it
        # before the completion flag means a restart retries this side effect
        # instead of permanently losing recruiter statistics/logging after the
        # family role itself was granted.
        if decision.kind == "family_invite" and decision.reviewer_id is not None:
            from cogs.recruiting import record_family_decision

            member = guild.get_member(decision.user_id) or discord.Object(
                id=decision.user_id
            )
            reviewer = guild.get_member(decision.reviewer_id) or discord.Object(
                id=decision.reviewer_id
            )
            await record_family_decision(
                self.bot,
                guild,
                member,
                number,
                approved,
                reviewer,
                (decision.reason or "Семейная роль")
                if approved
                else (decision.reason or "Не указана"),
            )

        await mark_request_presentation_complete(number)
        registry_key = (number, message.id)
        view = self._registered_approvals.pop(registry_key, None)
        if view is not None:
            stop_registered_view(view)
        for persistent_view in tuple(self.bot.persistent_views):
            legacy = getattr(persistent_view, "legacy_view", None)
            if (
                getattr(persistent_view, "number", None) == number
                or getattr(legacy, "number", None) == number
            ):
                stop_registered_view(persistent_view)
        return log_message.id

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._restore_pending_approvals()

    async def _restore_pending_approvals(self) -> None:
        """Idempotently restore message-bound approval controls after a restart."""

        async with self._approval_restore_lock:
            restored = 0
            active_keys: set[tuple[str, int]] = set()
            channel_keys = {
                "role_request": "role_request",
                "vzp": "vzp_invite",
                "birthday": "birthday_request",
                "family_invite": "family_invite_logs",
            }
            publications = await get_pending_request_publications()
            for publication in publications:
                (
                    number,
                    kind,
                    user_id,
                    content,
                    guild_id,
                    channel_id,
                    message_id,
                    payload_json,
                    nickname_status,
                    nickname_requested_by,
                ) = publication
                guild = self.bot.get_guild(guild_id) if guild_id else None
                if guild is None and not guild_id and len(self.bot.guilds) == 1:
                    guild = self.bot.guilds[0]
                if guild is None:
                    logger.warning("Cannot infer guild for pending request %s", number)
                    continue

                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        logger.info(
                            "Skipping pending request %s: member %s left",
                            number,
                            user_id,
                        )
                        continue

                channel = guild.get_channel(channel_id) if channel_id else None
                if channel is None:
                    channel = get_text_channel(guild, channel_keys[kind])
                if not isinstance(channel, discord.TextChannel):
                    raise RuntimeError(
                        f"Review channel is unavailable for request {number}"
                    )

                payload: dict = {}
                if payload_json:
                    try:
                        loaded = json.loads(payload_json)
                        if isinstance(loaded, dict):
                            payload = loaded
                    except (TypeError, ValueError):
                        logger.warning("Invalid request payload for %s", number)

                message: discord.Message | None = None
                if message_id:
                    try:
                        message = await channel.fetch_message(message_id)
                    except discord.NotFound:
                        message_id = None

                if not message_id:
                    async for candidate in channel.history(limit=100):
                        if candidate.author == guild.me and self._has_marker(
                            candidate, f"request:{number}"
                        ):
                            message = candidate
                            message_id = candidate.id
                            await set_request_publication(
                                number, guild.id, channel.id, candidate.id
                            )
                            break

                cutoff_summary = None
                if kind == "family_invite":
                    from services.onboarding import application_cutoff

                    cutoff_summary = (await application_cutoff(guild.id)).summary
                embed, view = self._approval_presentation(
                    kind,
                    member,
                    number,
                    content,
                    payload,
                    nickname_status=str(nickname_status or "idle"),
                    cutoff_summary=cutoff_summary,
                    nickname_requested_by=(
                        int(nickname_requested_by)
                        if nickname_requested_by is not None
                        else None
                    ),
                )
                if not message_id:
                    message = await send_v2(
                        channel,
                        content=request_ping_content(guild),
                        embed=embed,
                        view=view,
                        component_marker=f"request:{number}",
                        allowed_mentions=request_ping_mentions(guild),
                    )
                    message_id = message.id
                    await set_request_publication(
                        number, guild.id, channel.id, message.id
                    )
                elif message is not None and (
                    not message.flags.components_v2
                    or not self._has_marker(message, f"request:{number}")
                ):
                    # Merely restoring a callback leaves pre-v2 approval cards
                    # visually legacy and without a crash-recovery marker.
                    # Upgrade the canonical message in place and preserve its ID.
                    message = await edit_message_v2(
                        message,
                        content=request_ping_content(guild),
                        embed=embed,
                        view=view,
                        component_marker=f"request:{number}",
                        allowed_mentions=request_ping_mentions(guild),
                    )
                    message_id = message.id
                    await set_request_publication(
                        number, guild.id, channel.id, message.id
                    )

                registry_key = (number, message_id)
                active_keys.add(registry_key)
                if registry_key not in self._registered_approvals:
                    self.bot.add_view(view, message_id=message_id)
                    self._registered_approvals[registry_key] = view
                    restored += 1

            for stale_key in set(self._registered_approvals) - active_keys:
                stale_view = self._registered_approvals.pop(stale_key)
                stop_registered_view(stale_view)
            if restored:
                logger.info("Restored %d pending approval view(s)", restored)

    @staticmethod
    def _approval_presentation(
        kind: str,
        member: discord.Member,
        number: str,
        content: str,
        payload: dict,
        *,
        nickname_status: str = "idle",
        nickname_requested_by: int | None = None,
        cutoff_summary: str | None = None,
    ) -> tuple[MessageSpec, discord.ui.View]:
        if kind == "role_request":
            role_name = str(payload.get("role_name") or content)
            embed = apply_accent(role_request_embed(member, role_name, number), Accent.MEMBER)
            return embed, RoleApprovalView(member, role_name, number)
        if kind == "vzp":
            embed = apply_accent(
                request_embed("Заявка в боевую команду", member, content, number),
                Accent.EVENTS,
            )
            return embed, VZPApprovalView(member, number, embed)
        if kind == "birthday":
            birth_date = str(payload.get("birth_date") or content)
            embed = apply_accent(
                notice(
                    code="BIRTHDAY.REQUEST",
                    title="🎂 Запрос на добавление дня рождения",
                    summary=f"**Дата:** {birth_date}",
                    details=(("Пользователь", member.mention),),
                    next_step="Подтвердите дату или отклоните запрос.",
                    reference=number,
                ),
                Accent.MEMBER,
            )
            return embed, BirthdayApprovalView(member, number, birth_date)

        ic_name = str(payload.get("ic_name") or "Не сохранено")
        ooc_name = str(payload.get("ooc_name") or "Не сохранено")
        nickname_line = None
        if nickname_status == "requested":
            actor = (
                f" • <@{nickname_requested_by}>"
                if nickname_requested_by is not None
                else ""
            )
            nickname_line = f"📝 Ожидается смена ника{actor}"
        elif nickname_status == "confirmed":
            nickname_line = "✅ Пользователь сообщил, что сменил ник"
        family_card = family_application_embed(
            member,
            number,
            payload,
            status=nickname_line,
            cutoff_summary=cutoff_summary,
        )
        embed = apply_accent(family_card, Accent.MEMBER)
        return (
            embed,
            FamilyApprovalView(
                member,
                number,
                embed,
                ic_name,
                ooc_name,
                nickname_status=nickname_status,
            ),
        )


@dataclass(frozen=True, slots=True)
class PanelOutcome:
    """Result of provisioning one persistent panel."""

    key: str
    title: str
    status: str  # "updated" | "merged" | "missing" | "failed"
    detail: str = ""

    @property
    def is_problem(self) -> bool:
        return self.status in {"missing", "failed"}


@dataclass(slots=True)
class SetupReport:
    """Per-panel outcome of ``setup_all_persistent_messages``.

    The command used to report "Настройка завершена" whenever no exception
    escaped, even though every panel whose channel was missing had been skipped
    by a bare ``if channel:``. Callers now receive the outcome of each panel and
    can fail loudly instead of claiming a success that did not happen.
    """

    outcomes: list[PanelOutcome] = field(default_factory=list)

    def record(self, key: str, title: str, status: str, detail: str = "") -> None:
        self.outcomes.append(PanelOutcome(key, title, status, detail))

    @property
    def updated(self) -> list[PanelOutcome]:
        return [item for item in self.outcomes if item.status == "updated"]

    @property
    def merged(self) -> list[PanelOutcome]:
        return [item for item in self.outcomes if item.status == "merged"]

    @property
    def missing(self) -> list[PanelOutcome]:
        return [item for item in self.outcomes if item.status == "missing"]

    @property
    def failed(self) -> list[PanelOutcome]:
        return [item for item in self.outcomes if item.status == "failed"]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.failed


async def _provision_panel(
    report: SetupReport,
    key: str,
    title: str,
    channel: discord.abc.GuildChannel | None,
    action,
    *,
    channel_name: str,
    merged_into: discord.TextChannel | None = None,
    panel_keys: frozenset[str] | None = None,
) -> None:
    """Run one panel step and record precisely what happened to it."""

    if panel_keys is not None and key not in panel_keys:
        return
    if channel is None:
        report.record(key, title, "missing", f"канал #{channel_name} не найден")
        logger.error("Setup skipped %s: channel %s is unresolved", key, channel_name)
        return
    if merged_into is not None and channel.id == merged_into.id:
        report.record(
            key, title, "merged",
            f"размещается в общей панели #{merged_into.name}",
        )
        return
    try:
        await action()
    except (discord.HTTPException, RuntimeError, ValueError) as exc:
        report.record(key, title, "failed", f"{type(exc).__name__}: {exc}")
        logger.exception("Setup failed for panel %s", key)
        return
    report.record(key, title, "updated", f"#{getattr(channel, 'name', channel.id)}")


async def setup_all_persistent_messages(
    guild: discord.Guild,
    *,
    panel_keys: frozenset[str] | None = None,
) -> SetupReport:
    """Размещает все постоянные сообщения с кнопками и картинками.

    Возвращает отчёт по каждой панели: обновлена / объединена / канал не найден
    / ошибка. Ранее пропуск ненайденного канала был неотличим от успеха.
    ``panel_keys`` ограничивает набор панелей; ``None`` — все, как раньше.
    """

    report = SetupReport()

    async def provision(*args, **kwargs):
        kwargs.setdefault("panel_keys", panel_keys)
        key = args[1] if len(args) > 1 else kwargs.get("key")
        title = args[2] if len(args) > 2 else kwargs.get("title", "")
        if isinstance(key, str):
            blocked = await unavailable_reason(guild.id, key)
            if blocked:
                if panel_keys is None or key in panel_keys:
                    report.record(key, title, "unavailable", blocked)
                return
        await _provision_panel(*args, **kwargs)

    features_ch = get_text_channel(guild, "family_features")
    if features_ch is None and (panel_keys is None or "family-features" in panel_keys):
        report.record(
            "family-features",
            "Семейные фичи",
            "missing",
            f"канал #{CHANNELS['family_features']} не найден",
        )

    # 1. запрос-помощи
    help_ch = get_text_channel(guild, "help")
    help_panel = panel_embed(
        "Поддержка семьи",
        "Расскажите о ситуации в одной форме — команда Help получит "
        "структурированное обращение и продолжит общение в ветке.",
        sections=(
            (
                "Когда использовать",
                "Вопросы по кармиту, организации, Discord или внутренним "
                "процессам семьи.",
            ),
            (
                "Перед отправкой",
                "Кратко опишите нужный результат. При необходимости приложите "
                "до пяти изображений.",
            ),
        ),
        color=Accent.SERVICE,
        footer="Guinness Family • Поддержка",
        code="PANEL.HELP",
    )
    await provision(
        report,
        "help",
        "Поддержка семьи",
        help_ch,
        lambda: _upsert_feature_message(
            help_ch,
            "setup_help_message_id",
            help_panel,
            PersistentHelpButton(),
        ),
        channel_name=CHANNELS["help"],
        merged_into=features_ch,
    )

    # 2. запрос-к-сс
    ss_ch = get_text_channel(guild, "request_ss")
    ss_panel = panel_embed(
        "Обращение к старшему составу",
        "Выберите подходящую категорию и подробно опишите ситуацию. "
        "Бот создаст отдельный тикет и откроет доступ только тем сотрудникам, "
        "которые отвечают за выбранное направление.",
        sections=(
            (
                "Обычные обращения",
                "**Жалоба** — конфликт, нарушение или замечание\n"
                "**Предложение** — идея по Discord или работе семьи\n"
                "**Работа бота** — ошибка или предложение по функционалу",
            ),
            (
                "Конфиденциальность",
                "**Owner-request** видят только уполномоченные лица.\n"
                "**Анонимное обращение** не раскрывает автора и поддерживает "
                "безопасную переписку через бота.",
            ),
            (
                "После отправки",
                "Ответственный сотрудник получит уведомление, возьмёт тикет "
                "в работу и сообщит итог в созданном канале.",
            ),
        ),
        color=STAFF_COLOR,
        footer="Guinness Family • Обращения",
        code="PANEL.SS",
    )
    await provision(
        report,
        "request-ss",
        "Обращение к старшему составу",
        ss_ch,
        lambda: _upsert_feature_message(
            ss_ch, "setup_ss_message_id", ss_panel, PersistentSSButton()
        ),
        channel_name=CHANNELS["request_ss"],
    )

    # 3. заявка-vzp-team
    vzp_ch = get_text_channel(guild, "vzp")
    vzp_panel = panel_embed(
        "Заявка в боевую команду",
        "Подайте заявку, если готовы участвовать в командных выездах и "
        "соблюдать правила боевого направления.",
        sections=(
            (
                "Что указать",
                "Опыт, сильные стороны, доступное время и мотивацию.",
            ),
            (
                "Что произойдёт дальше",
                "Старший состав рассмотрит одну обновляемую карточку и "
                "зафиксирует итоговое решение.",
            ),
        ),
        color=Accent.EVENTS,
        footer="Guinness Family • Боевая команда",
        code="PANEL.COMBAT",
    )
    await provision(
        report,
        "vzp",
        "Заявка в боевую команду",
        vzp_ch,
        lambda: _upsert_feature_message(
            vzp_ch, "setup_vzp_message_id", vzp_panel, PersistentVZPButton()
        ),
        channel_name=CHANNELS["vzp"],
        merged_into=features_ch,
    )

    # 4. заявка-в-семью
    family_ch = get_text_channel(guild, "family_invite")
    family_panel = panel_embed(
        "Вступление в Guinness Family",
        "Хотите стать частью Guinness Family? Ответьте на несколько вопросов "
        "о себе и игровом персонаже — это поможет нам познакомиться до беседы.",
        sections=(
            (
                "Перед отправкой",
                "Укажите актуальные IC/OOC-данные и честно расскажите, почему "
                "хотите присоединиться к семье.",
            ),
            (
                "Что произойдёт дальше",
                "Заявка поступит старшему составу. После проверки с вами "
                "свяжутся и сообщат решение или дальнейшие шаги.",
            ),
        ),
        color=Accent.MEMBER,
        footer="Guinness Family • Вступление",
        code="PANEL.FAMILY",
    )
    await provision(
        report,
        "family-invite",
        "Вступление в Guinness Family",
        family_ch,
        lambda: _upsert_feature_message(
            family_ch,
            "setup_family_application_message_id",
            family_panel,
            PersistentFamilyButton(),
        ),
        channel_name=CHANNELS["family_invite"],
    )

    # 5. add-roles
    roles_ch = get_text_channel(guild, "add_roles")
    roles_panel = panel_embed(
        "Роли и доступы",
        (
            "Управляйте общедоступными ролями самостоятельно или отправьте "
            "заявку на роль, требующую подтверждения."
        ),
        sections=(
            (
                "Мгновенное управление",
                "**Guest** и **Help** доступны всем участникам.\n"
                "**Contract** доступна участникам семьи с ролью Young, Main "
                "или выше.\n"
                "Серые кнопки сразу включают или выключают выбранную роль.",
            ),
            (
                "Роли по заявке",
                "**Young / Main** и **Friend / Super Friend** выдаются после "
                "рассмотрения старшим составом.\n"
                "Синие кнопки открывают выбор роли и форму заявки.",
            ),
        ),
        color=Accent.MEMBER,
        footer="Guinness Family • Повторное нажатие снимает быструю роль",
        code="PANEL.ROLES",
    )
    await provision(
        report,
        "add-roles",
        "Роли и доступы",
        roles_ch,
        lambda: _upsert_feature_message(
            roles_ch, "setup_roles_message_id", roles_panel, AddRolesView()
        ),
        channel_name=CHANNELS["add_roles"],
    )

    # 6. happy-birthday (создать или обновить одно сохранённое сообщение)
    happy_ch = get_text_channel(guild, "happy_birthday")
    await provision(
        report,
        "happy-birthday",
        "Дни рождения",
        happy_ch,
        lambda: update_happy_birthday_message(guild),
        channel_name=CHANNELS["happy_birthday"],
    )

    # 7. add-birthday (стартовое сообщение)
    add_bd_ch = get_text_channel(guild, "add_birthday")
    add_bd_panel = panel_embed(
        "Добавить день рождения",
        "Сохраните дату, чтобы семья могла поздравить вас вовремя.",
        sections=(
            (
                "Формат",
                "Укажите день и месяц. Запрос будет подтверждён старшим "
                "составом перед публикацией.",
            ),
        ),
        color=Accent.MEMBER,
        footer="Guinness Family • Дни рождения",
        code="PANEL.BIRTHDAY_ADD",
    )
    await provision(
        report,
        "add-birthday",
        "Добавить день рождения",
        add_bd_ch,
        lambda: _upsert_feature_message(
            add_bd_ch,
            "setup_add_birthday_message_id",
            add_bd_panel,
            PersistentBirthdayButton(),
        ),
        channel_name=CHANNELS["add_birthday"],
        merged_into=features_ch,
    )

    # 8. Учёт казны
    from cogs.forms import update_treasury_message

    treasury_ch = get_text_channel(guild, "treasury")
    await provision(
        report,
        "treasury",
        "Казна",
        treasury_ch,
        lambda: update_treasury_message(guild),
        channel_name=CHANNELS["treasury"],
    )

    # 9. Автопарк (создать или обновить одно сохранённое сообщение)
    from cogs.car_park import update_car_list_message

    car_list_ch = get_text_channel(guild, "car_list")
    await provision(
        report,
        "car-park",
        "Автопарк",
        car_list_ch,
        lambda: update_car_list_message(guild),
        channel_name=CHANNELS["car_list"],
    )

    # 10. Еженедельная статистика (отдельное сообщение на каждую неделю)
    from cogs.weekly_stats import update_weekly_stats_message

    weekly_ch = get_text_channel(guild, "weekly_stats")
    await provision(
        report,
        "weekly-stats",
        "Еженедельная статистика",
        weekly_ch,
        lambda: update_weekly_stats_message(guild),
        channel_name=CHANNELS["weekly_stats"],
    )

    # 11. Магазин Guinn Coins (одно обновляемое сообщение)
    from cogs.shop import update_shop_message

    shop_ch = get_text_channel(guild, "shop")
    await provision(
        report,
        "shop",
        "Магазин Guinn Coins",
        shop_ch,
        lambda: update_shop_message(guild),
        channel_name=CHANNELS["shop"],
        merged_into=features_ch,
    )

    # 12. Общая панель семейных фич
    if features_ch is not None:
        await provision(
            report,
            "family-features",
            "Семейные фичи",
            features_ch,
            lambda: _setup_family_features(guild, features_ch),
            channel_name=CHANNELS["family_features"],
        )

    capt_ch = features_ch
    capt_panel = notice(
        code="PANEL.CAPT",
        title="Капт",
        summary=(
            "Захват территории. Сборы и расписание появятся здесь, "
            "когда модуль событий будет включён."
        ),
        next_step="Следите за объявлениями старшего состава.",
    )
    await provision(
        report,
        "capt",
        "Капт",
        capt_ch,
        lambda: _upsert_capt_panel(capt_ch, capt_panel),
        channel_name=CHANNELS["family_features"],
    )

    await provision(
        report,
        "me",
        "Моя панель",
        features_ch,
        lambda: upsert_me_panel(features_ch),
        channel_name=CHANNELS["family_features"],
    )

    contracts_ch = get_text_channel(guild, "summit_contract")
    await provision(
        report,
        "contracts",
        "Контракты",
        contracts_ch,
        lambda: upsert_contracts_panel(contracts_ch),
        channel_name=CHANNELS["summit_contract"],
    )

    from cogs.insights import (
        update_battles_panel,
        update_market_panel,
        update_property_panel,
        update_rating_panel,
    )

    rating_ch = get_text_channel(guild, "insights_rating")
    await provision(
        report,
        "insights-rating",
        "Рейтинг Memphis",
        rating_ch,
        lambda: update_rating_panel(guild),
        channel_name=CHANNELS["insights_rating"],
    )
    battles_ch = get_text_channel(guild, "insights_battles")
    await provision(
        report,
        "insights-battles",
        "Разборы каптов и войн семей",
        battles_ch,
        lambda: update_battles_panel(guild),
        channel_name=CHANNELS["insights_battles"],
    )
    market_ch = get_text_channel(guild, "insights_market")
    await provision(
        report,
        "insights-market",
        "Рынок Memphis",
        market_ch,
        lambda: update_market_panel(guild),
        channel_name=CHANNELS["insights_market"],
    )
    property_ch = get_text_channel(guild, "insights_property")
    await provision(
        report,
        "insights-property",
        "ПСН и особняки",
        property_ch,
        lambda: update_property_panel(guild),
        channel_name=CHANNELS["insights_property"],
    )

    return report


async def _upsert_capt_panel(channel: discord.TextChannel, spec: MessageSpec) -> None:
    def build() -> StaticPayload:
        return StaticPayload(embed=spec, revision=1)

    await upsert_static_message(
        guild=channel.guild,
        channel=channel,
        panel_key="capt",
        build=build,
    )


async def deploy_persistent_panel(guild: discord.Guild, key: str) -> SetupReport:
    """Deploy or rebuild one registry panel without touching the others."""

    return await setup_all_persistent_messages(guild, panel_keys=frozenset({key}))


def _register_panel_deployers() -> None:
    for key in known_panel_keys():
        async def _deploy(guild: discord.Guild, panel_key: str = key) -> SetupReport:
            return await deploy_persistent_panel(guild, panel_key)

        register_deployer(key, _deploy)


_register_panel_deployers()



async def _upsert_feature_message(
    channel: discord.TextChannel,
    setting_key: str,
    embed: MessageSpec,
    view: discord.ui.View,
):
    from utils.database import set_setting

    title = embed.title

    def build() -> StaticPayload:
        return StaticPayload(
            embed=embed,
            view=view,
            revision=3,
        )

    message = await upsert_static_message(
        guild=channel.guild,
        channel=channel,
        panel_key=setting_key.replace("_message_id", "").replace("_", "-"),
        build=build,
        legacy_setting_key=setting_key,
        legacy_match=lambda item: bool(
            title and item.embeds and item.embeds[0].title == title
        ),
    )
    if message is None:
        return None
    await set_setting(setting_key, str(message.id))
    return message


async def _setup_family_features(
    guild: discord.Guild, channel: discord.TextChannel
) -> None:
    """Keep the requested order: shop, birthday, combat application, help."""
    from cogs.shop import update_shop_message

    await update_shop_message(guild)

    birthday = panel_embed(
        "Добавить день рождения",
        "Сохраните день и месяц, чтобы семья могла поздравить вас вовремя.",
        sections=(
            (
                "Как это работает",
                "Дата появится в общем календаре после подтверждения старшим "
                "составом.",
            ),
        ),
        color=Accent.MEMBER,
        footer="Guinness Family • Дни рождения",
        code="PANEL.BIRTHDAYS",
    )
    await _upsert_feature_message(
        channel,
        "family_features_birthday_message_id",
        birthday,
        PersistentBirthdayButton(),
    )

    vzp = panel_embed(
        "Заявка в боевую команду",
        "Расскажите об опыте и мотивации, если готовы участвовать в командных "
        "выездах.",
        sections=(
            (
                "Рассмотрение",
                "Итоговый статус будет сохранён в одной карточке заявки.",
            ),
        ),
        color=Accent.EVENTS,
        footer="Guinness Family • Боевая команда",
        code="PANEL.COMBAT_INFO",
    )
    await _upsert_feature_message(
        channel,
        "family_features_vzp_message_id",
        vzp,
        PersistentVZPButton(),
    )

    help_embed = panel_embed(
        "Поддержка семьи",
        "Опишите задачу и при необходимости приложите изображения. Команда Help "
        "продолжит общение в отдельной ветке.",
        sections=(
            (
                "Подойдёт для",
                "Кармит, организация, Discord и внутренние вопросы семьи.",
            ),
        ),
        color=Accent.SERVICE,
        footer="Guinness Family • Поддержка",
        code="PANEL.HELP",
    )
    await _upsert_feature_message(
        channel,
        "family_features_help_message_id",
        help_embed,
        PersistentHelpButton(),
    )


async def setup(bot):
    await bot.add_cog(InteractionsCog(bot))
