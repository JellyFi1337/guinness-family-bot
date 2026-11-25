import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import discord

from config import (
    BOT_OWNER_ID,
    CHANNELS,
    ROLES,
    ROLE_REJECTION_REASONS,
    TICKET_ASSIGNMENT_ROLE_HIERARCHY,
    TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS,
)
from repositories.requests import (
    claim_request_review,
    confirm_nickname_change,
    expire_request_review_attempt,
    fail_request_publication,
    finish_request_review,
    log_request,
    set_request_publication,
)
from repositories.anonymous import create_anonymous_conversation
from repositories.treasury import (
    add_treasury_transaction,
    get_last_transactions,
    get_treasury_balance,
)
from services.static_messages import StaticPayload, upsert_static_message
from services.cooldowns import command_cooldowns, cooldown_retry_text
from ui.layouts import marker_component_id
from repositories.operations import enqueue_operation
from repositories.tickets import (
    activate_ticket,
    attach_ticket_channel,
    begin_ticket,
    begin_ticket_close,
    channel_marker,
    fail_ticket,
)
from ui.message_specs import Outcome
from ui.pagination import send_paged_list
from ui.messages import (
    edit_interaction_status_v2,
    edit_status_message_v2,
    edit_original_response_v2,
    followup_spec,
    followup_v2,
    respond_spec,
    respond_v2,
    send_v2,
)
from ui.results import completed, failed, queued, unchanged
from utils.database import (
    add_birthday,
    assign_ticket,
    create_ticket_assignment,
    delete_ticket_assignment,
    get_next_number,
    get_ticket_assignment,
    record_stats_processed_action,
    set_ticket_assignee,
    unassign_ticket,
)
from utils.discord_lookup import (
    get_category,
    get_role,
    get_text_channel,
    has_high_tools_access,
    is_high_tools_interaction,
    member_has_high_tools_access,
    request_ping_content,
    request_ping_mentions,
    request_ping_roles,
)
from ui.design import Accent
from ui.templates import notice
from utils.embeds import (
    WHITE_COLOR,
    apply_accent,
    anonymous_request_embed,
    error_embed,
    request_embed,
    role_request_embed,
    success_embed,
    ticket_embed,
)
from utils.access import has_administrator_access, is_bot_owner
from utils.validation import normalize_birthday, normalize_member_role, normalize_positive_amount
from utils.views import SafeModal, SafeView


logger = logging.getLogger(__name__)
_STATIC_MESSAGE_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}
_HELP_UPLOAD_SEMAPHORE = asyncio.Semaphore(5)


def _static_message_lock(kind: str, guild_id: int) -> asyncio.Lock:
    return _STATIC_MESSAGE_LOCKS.setdefault((kind, guild_id), asyncio.Lock())


async def _claim_review(
    interaction: discord.Interaction, number: str, outcome: str
) -> bool:
    if await claim_request_review(
        number,
        interaction.user.id,
        outcome=outcome,
        token=str(interaction.id),
    ):
        return True
    message = (
        "Эта заявка уже рассматривается, была обработана или для неё "
        "зафиксировано другое решение."
    )
    if interaction.response.is_done():
        await followup_v2(interaction, message, ephemeral=True)
    else:
        await respond_v2(interaction, message, ephemeral=True)
    return False


async def _finish_review(
    interaction: discord.Interaction,
    number: str,
    outcome: str,
    *,
    reason: str | None = None,
) -> bool:
    completed = await finish_request_review(
        number,
        interaction.user.id,
        outcome,
        token=str(interaction.id),
        reason=reason,
    )
    if completed:
        return True
    await followup_v2(
        interaction,
        "Решение уже завершено другим обработчиком; повторная запись не выполнена.",
        ephemeral=True,
    )
    return False


async def _run_review_side_effect(
    interaction: discord.Interaction, number: str, operation
) -> None:
    try:
        await operation
    except BaseException:
        try:
            await expire_request_review_attempt(
                number,
                interaction.user.id,
                str(interaction.id),
            )
        except Exception:
            logger.exception("Failed to release request review attempt %s", number)
        raise


async def _log_request_action(
    guild: discord.Guild,
    text: str,
    *,
    outcome: Outcome = Outcome.NEUTRAL,
) -> None:
    if outcome in {Outcome.APPROVED, Outcome.REJECTED}:
        await record_stats_processed_action(guild.id, "application", outcome.value)
    channel = get_text_channel(guild, "request_actions")
    if channel is not None:
        try:
            if outcome is Outcome.APPROVED:
                title, color = "Заявка одобрена", Accent.SUCCESS
            elif outcome is Outcome.REJECTED:
                title, color = "Заявка отклонена", Accent.ERROR
            else:
                title, color = "Действие с обращением", WHITE_COLOR
            details = re.sub(r"^[✅❌🎫📩]\ufe0f?\s*", "", text).strip()
            details = re.sub(
                r"^(?:Одобрено|Отклонено):\s*", "", details, flags=re.IGNORECASE
            )
            details = details.replace(" by ", "\nОбработал: ")
            spec = notice(
                code="FORMS.SS_LOG",
                title=title,
                summary=details,
                next_step="Запись сохранена в журнале СС.",
                accent=color,
            )
            await send_v2(channel, embed=spec)
        except discord.HTTPException:
            logger.exception("Failed to write request action log")


async def _notify_rejection(
    member: discord.Member, application: str, number: str, reason: str
) -> None:
    try:
        await send_v2(member,
            f"Ваша заявка **{application}** (#{number}) отклонена.\n"
            f"Причина: **{reason}**"
        )
    except discord.HTTPException:
        pass


async def _edit_decision_message(message, status: str, accent) -> None:
    await edit_status_message_v2(
        message, status=status, accent_color=int(accent), view=None
    )


def _has_staff_access(interaction: discord.Interaction) -> bool:
    """Fail closed when the configured staff role is missing."""
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    if is_bot_owner(user):
        return True
    if is_high_tools_interaction(interaction):
        return has_high_tools_access(interaction)
    if has_administrator_access(user):
        return True
    return member_has_high_tools_access(user) or any(
        role.name == ROLES["staff"] for role in user.roles
    )


def _is_staff_member(member: discord.Member) -> bool:
    if member.bot:
        return False
    if has_administrator_access(member):
        return True
    return member_has_high_tools_access(member) or any(
        role.name == ROLES["staff"] for role in member.roles
    )


def _ticket_assignment_rank(member: discord.Member) -> int:
    """Return a configured staff level where a smaller value is more senior."""

    if is_bot_owner(member):
        return -1
    member_role_ids = {role.id for role in member.roles}
    member_role_names = {role.name.casefold() for role in member.roles}
    if TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS:
        ranks = [
            index
            for index, role_id in enumerate(TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS)
            if role_id in member_role_ids
        ]
        return min(ranks, default=len(TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS))
    ranks = [
        index
        for index, role_name in enumerate(TICKET_ASSIGNMENT_ROLE_HIERARCHY)
        if role_name.casefold() in member_role_names
    ]
    return min(ranks, default=len(TICKET_ASSIGNMENT_ROLE_HIERARCHY))


def can_assign_ticket_member(
    actor: discord.Member,
    target: discord.Member,
) -> bool:
    """Allow assignment only to the same or a lower configured staff level."""

    return _ticket_assignment_rank(target) >= _ticket_assignment_rank(actor)


def _configured_member_role(canonical_name: str) -> str:
    key = {
        "Young": "young",
        "Main": "main",
        "Friend": "friend",
        "Super Friend": "super_friend",
    }.get(canonical_name)
    return ROLES[key] if key is not None else canonical_name


class RejectionReasonSelect(discord.ui.Select):
    def __init__(self, handler):
        options = [
            discord.SelectOption(label=reason[:100], value=reason[:100])
            for reason in ROLE_REJECTION_REASONS
        ] or [discord.SelectOption(label="Другое", value="Другое")]
        super().__init__(placeholder="Выберите причину отклонения", options=options)
        self.handler = handler

    async def callback(self, interaction: discord.Interaction):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.handler(interaction, self.values[0])


class RejectionReasonView(SafeView):
    def __init__(self, handler):
        super().__init__(timeout=180)
        self.add_item(RejectionReasonSelect(handler))


# ==================== MODALS ====================

class TreasuryView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Пополнение",
        style=discord.ButtonStyle.success,
        custom_id="treasury_deposit",
    )
    async def deposit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(TreasuryContributionModal("add"))

    @discord.ui.button(
        label="Снятие",
        style=discord.ButtonStyle.danger,
        custom_id="treasury_withdraw",
    )
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        await interaction.response.send_modal(TreasuryContributionModal("remove"))

class HelpRequestModal(SafeModal, title="Запрос помощи"):
    cooldown_seconds = 2.0
    help_type = discord.ui.TextInput(
        label="Какой вид помощи вам нужен?",
        placeholder="Опишите кратко (кармит, финансы, организация и т.д.)",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True
    )

    def __init__(self):
        super().__init__()
        self.photos = discord.ui.FileUpload(
            custom_id="help_request:photos",
            required=False,
            min_values=0,
            max_values=5,
        )
        self.add_item(
            discord.ui.Label(
                text="Фотографии",
                description="Можно прикрепить до пяти изображений",
                component=self.photos,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            await followup_v2(
                interaction,
                "Запрос помощи можно отправить только на сервере.",
                ephemeral=True,
            )
            return
        cooldown = await command_cooldowns.acquire(
            interaction.guild_id,
            "help_request",
            interaction.user.id,
            bypass=_has_staff_access(interaction),
        )
        if not cooldown.allowed:
            await followup_v2(
                interaction,
                cooldown_retry_text(cooldown.retry_after),
                ephemeral=True,
            )
            return
        published = False
        try:
            number = await get_next_number("HR")
            content = self.help_type.value.strip()
            channel = get_text_channel(interaction.guild, "help_target")
            if channel is None:
                raise RuntimeError(f"Канал {CHANNELS['help_target']} не найден")
            help_role = get_role(interaction.guild, "help")
            if help_role is None:
                raise RuntimeError(f"Роль {ROLES['help']} не найдена")
            attachments = list(self.photos.values)
            invalid = [
                item.filename
                for item in attachments
                if not (item.content_type or "").casefold().startswith("image/")
            ]
            if invalid:
                await followup_v2(interaction,
                    "Можно прикреплять только изображения.", ephemeral=True
                )
                return
            total_size = sum(item.size for item in attachments)
            if total_size > 20 * 1024 * 1024 or any(
                item.size > 8 * 1024 * 1024 for item in attachments
            ):
                await followup_v2(interaction,
                    "Размер одного изображения не должен превышать 8 МБ, общий размер — 20 МБ.",
                    ephemeral=True,
                )
                return
            if _HELP_UPLOAD_SEMAPHORE.locked():
                await followup_v2(
                    interaction,
                    "Сейчас обрабатывается много фотографий. "
                    "Повторите отправку через несколько секунд.",
                    ephemeral=True,
                )
                return
            async with _HELP_UPLOAD_SEMAPHORE:
                async def load_photo(index: int, attachment: discord.Attachment):
                    suffix = Path(attachment.filename).suffix or ".png"
                    filename = f"help-{number}-{index}{suffix}"[:100]
                    return await attachment.to_file(
                        filename=filename,
                        use_cached=True,
                    )

                files = await asyncio.gather(
                    *(
                        load_photo(index, attachment)
                        for index, attachment in enumerate(attachments, 1)
                    )
                )

                spec = notice(
                    code="FORMS.HELP_REQUEST",
                    title="Запрос помощи",
                    summary=content,
                    details=(("Пользователь", interaction.user.mention),),
                    next_step="Команда Help ответит в этой ветке.",
                    accent=WHITE_COLOR,
                    reference=number,
                )
                message = await send_v2(channel,
                    content=help_role.mention,
                    embed=spec,
                    files=files,
                    allowed_mentions=discord.AllowedMentions(
                        roles=[help_role], users=False, everyone=False
                    ),
                )
                try:
                    thread = await message.create_thread(
                        name=f"помощь-{number.lower()}"[:100],
                        auto_archive_duration=1440,
                        reason=f"Help request {number}",
                    )
                    if isinstance(interaction.user, discord.Member):
                        try:
                            await thread.add_user(interaction.user)
                        except discord.HTTPException:
                            logger.warning(
                                "Could not add requester %s to help thread %s",
                                interaction.user.id,
                                thread.id,
                            )
                except discord.HTTPException:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        logger.exception(
                            "Could not roll back help message %s", message.id
                        )
                    raise
                published = True
            await log_request(number, "help", interaction.user.id, content)

            await followup_v2(interaction,
                embed=success_embed("✅ Заявка отправлена",
                                    f"Запрос помощи {number} опубликован для команды Help.\n"
                                    f"Ветка для обсуждения: {thread.mention}"),
                ephemeral=True
            )
        except Exception:
            logger.exception("Failed to submit help request")
            if interaction.response.is_done():
                await followup_v2(interaction,
                    embed=error_embed("❌ Ошибка", "Не удалось отправить запрос помощи."),
                    ephemeral=True,
                )
            else:
                await respond_v2(interaction,
                    embed=error_embed("❌ Ошибка", "Не удалось отправить запрос помощи."),
                    ephemeral=True
                )
        finally:
            if not published:
                command_cooldowns.release(
                    interaction.guild_id,
                    "help_request",
                    interaction.user.id,
                )


# ==================== НОВАЯ ЛОГИКА ДЛЯ ЗАПРОС-К-СС (Селектор + Модал) ====================

class SSContentModal(SafeModal, title="Текст обращения"):
    """Модалка только для текста обращения. Тип выбирается раньше через Select."""

    def __init__(self, selected_type: str):
        super().__init__()
        self.selected_type = selected_type  # сохраняем выбранный тип

    content = discord.ui.TextInput(
        label="Текст обращения",
        placeholder="Подробно опишите вашу жалобу, предложение или обращение...",
        style=discord.TextStyle.paragraph,
        max_length=2000,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        logger.debug("Submitting staff request type=%s user=%s", self.selected_type, interaction.user)
        try:
            await interaction.response.defer(ephemeral=True)
            req_type = self.selected_type.lower()
            is_anonymous = "аноним" in req_type

            if is_anonymous:
                number = await get_next_number("ANR")
            else:
                # Отдельный счётчик для каждого типа тикета (жалоба, предложение, owner_request)
                counter_key = req_type.replace("-", "_").replace(" ", "_").upper()
                number = await get_next_number(counter_key)

            if is_anonymous:
                # Анонимный запрос
                from cogs.anonymous_relay import (
                    AnonymousStaffReplyView,
                    AnonymousUserReplyView,
                )
                from repositories.anonymous import link_anonymous_message

                anon_embed = anonymous_request_embed(self.content.value, number)

                anon_channel = get_text_channel(interaction.guild, "anonymous_request")
                logs_channel = get_text_channel(interaction.guild, "anonymous_logs")
                if anon_channel is None or logs_channel is None:
                    missing = []
                    if anon_channel is None:
                        missing.append("канал анонимных обращений")
                    if logs_channel is None:
                        missing.append("канал анонимных логов")
                    await followup_v2(
                        interaction,
                        embed=error_embed(
                            "❌ Обращение не отправлено",
                            "Не настроен " + " и ".join(missing) + ". "
                            "Сообщите администрации сервера.",
                        ),
                        ephemeral=True,
                    )
                    return

                await log_request(
                    number,
                    "anonymous",
                    interaction.user.id,
                    self.content.value,
                    guild_id=interaction.guild.id,
                )
                public_message = None
                discussion_thread = None
                thread_message = None
                log_message = None
                try:
                    public_message = await send_v2(
                        anon_channel,
                        content=request_ping_content(interaction.guild),
                        embed=apply_accent(anon_embed, Accent.STAFF),
                        view=AnonymousStaffReplyView(),
                        component_marker=f"anonymous:{number}",
                        allowed_mentions=request_ping_mentions(interaction.guild),
                    )
                    discussion_thread = await public_message.create_thread(
                        name=f"обращение-{number.lower()}"[:100],
                        auto_archive_duration=1440,
                        reason="Обсуждение анонимного обращения",
                    )
                    thread_message = await send_v2(
                        discussion_thread,
                        f"Переписка по анонимному обращению **{number}**.",
                        component_marker=f"anonymous-thread:{number}",
                    )

                    log_embed = notice(
                        code="FORMS.ANONYMOUS_LOG",
                        title="Анонимное обращение зарегистрировано",
                        summary=self.content.value,
                        details=(
                            ("Автор", interaction.user.mention),
                            ("Номер", number),
                        ),
                        next_step="Переписка ведётся в ветке обращения.",
                        accent=WHITE_COLOR,
                        reference=number,
                    )
                    log_message = await send_v2(
                        logs_channel,
                        embed=log_embed,
                        component_marker=f"anonymous-log:{number}",
                    )
                    conversation = await create_anonymous_conversation(
                        guild_id=interaction.guild.id,
                        number=number,
                        author_id=interaction.user.id,
                        public_channel_id=anon_channel.id,
                        public_message_id=public_message.id,
                        log_channel_id=logs_channel.id,
                        log_message_id=log_message.id,
                        thread_id=discussion_thread.id,
                        thread_message_id=thread_message.id,
                    )
                    try:
                        dm_embed = notice(
                            code="FORMS.ANONYMOUS_DM",
                            title="Анонимное обращение зарегистрировано",
                            summary=(
                                f"Ваше обращение **{number}** принято.\n"
                                "Здесь можно продолжить переписку или закрыть обращение."
                            ),
                            next_step="Ответьте кнопкой под этим сообщением или закройте обращение.",
                            accent=WHITE_COLOR,
                            reference=number,
                        )
                        dm_message = await send_v2(
                            interaction.user,
                            embed=dm_embed,
                            view=AnonymousUserReplyView(),
                            component_marker=f"anonymous-dm:{number}",
                        )
                        await link_anonymous_message(
                            conversation.id,
                            message_id=dm_message.id,
                            channel_id=dm_message.channel.id,
                            direction="staff_dm",
                        )
                    except Exception:
                        logger.warning(
                            "Cannot open anonymous DM conversation for user %s",
                            interaction.user.id,
                            exc_info=True,
                        )
                except Exception as exc:
                    await fail_request_publication(
                        number,
                        f"Anonymous publication failed: {type(exc).__name__}",
                    )
                    for partial in (
                        log_message,
                        discussion_thread,
                        public_message,
                    ):
                        if partial is not None:
                            try:
                                await partial.delete()
                            except discord.HTTPException:
                                logger.warning(
                                    "Could not remove partial anonymous request %s",
                                    partial.id,
                                )
                    raise

                await followup_v2(interaction,
                    embed=success_embed("✅ Анонимный запрос отправлен",
                                        f"Ваш анонимный запрос #{number} зарегистрирован."),
                    ephemeral=True
                )
            else:
                # Обычный тикет
                await create_ticket(interaction, req_type, self.content.value, number)

        except Exception:
            logger.exception("Failed to submit staff request")
            if interaction.response.is_done():
                await followup_v2(interaction,
                    embed=error_embed("❌ Ошибка", "Произошла ошибка при обработке обращения."),
                    ephemeral=True,
                )
            else:
                await respond_v2(interaction,
                    embed=error_embed("❌ Ошибка", "Произошла ошибка при обработке обращения."),
                    ephemeral=True
                )


class SSRequestTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Жалоба", value="жалоба",
                                 description="Жалоба на члена семьи, конфликт, замечание"),
            discord.SelectOption(label="Предложение", value="предложение",
                                 description="Идеи по улучшению сервера или семьи"),
            discord.SelectOption(label="Owner-request", value="owner-request",
                                 description="Обращение непосредственно к создателю семьи"),
            discord.SelectOption(
                label="Обращение по работе бота",
                value="bot-request",
                description="Ошибка, сбой или предложение по функционалу бота",
            ),
            discord.SelectOption(label="Анонимный запрос", value="анонимный запрос",
                                 description="Анонимная жалоба/предложение/обращение"),
        ]
        super().__init__(
            placeholder="Выберите тип обращения...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ss_type_select"
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        logger.debug("Selected staff request type=%s user=%s", selected, interaction.user)

        # Открываем модалку только с полем текста
        modal = SSContentModal(selected_type=selected)
        await interaction.response.send_modal(modal)


class SSRequestTypeSelectView(SafeView):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(SSRequestTypeSelect())


class VZPModal(SafeModal, title="Заявка в боевую команду"):
    ic_name = discord.ui.TextInput(
        label="Имя Фамилия (IC)",
        placeholder="Например: Haku Guinness",
        max_length=50,
        required=True
    )
    experience = discord.ui.TextInput(
        label="Ваш опыт (IC/OOC)",
        placeholder="Опишите ваш опыт в RP, предыдущие семьи, навыки...",
        style=discord.TextStyle.paragraph,
        max_length=1500,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        number = await get_next_number("BT")
        content = f"**Имя Фамилия (IC):** {self.ic_name.value}\n\n{self.experience.value}"
        embed = request_embed("Заявка в боевую команду", interaction.user, content, number)
        await log_request(
            number,
            "vzp",
            interaction.user.id,
            content,
            guild_id=interaction.guild.id,
            payload={
                "ic_name": self.ic_name.value,
                "experience": self.experience.value,
            },
        )

        # Отправляем в vzp-invite с кнопками одобрения
        vzp_channel = get_text_channel(interaction.guild, "vzp_invite")
        if vzp_channel is None:
            await fail_request_publication(number, "Combat review channel is missing")
            raise RuntimeError(f"Канал {CHANNELS['vzp_invite']} не найден")
        view = VZPApprovalView(interaction.user, number, embed)
        try:
            message = await send_v2(vzp_channel,
                content=request_ping_content(interaction.guild),
                embed=apply_accent(embed, Accent.EVENTS),
                view=view,
                component_marker=f"request:{number}",
                allowed_mentions=request_ping_mentions(interaction.guild),
            )
        except Exception as exc:
            await fail_request_publication(number, f"{type(exc).__name__}: {exc}")
            raise
        await set_request_publication(
            number,
            interaction.guild.id,
            vzp_channel.id,
            message.id,
        )

        await followup_v2(interaction,
            embed=success_embed("✅ Заявка отправлена",
                                f"Ваша заявка #{number} отправлена старшему составу на рассмотрение."),
            ephemeral=True
        )


class BirthdayModal(SafeModal, title="Добавить день рождения"):
    birth_date = discord.ui.TextInput(
        label="Когда родился? (день.месяц)",
        placeholder="Например: 15.03",
        max_length=10,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        logger.debug("Submitting birthday request date=%s", self.birth_date.value)
        number = None
        message = None
        try:
            await interaction.response.defer(ephemeral=True)
            birth_date = normalize_birthday(self.birth_date.value)
            number = await get_next_number("AB")

            embed = notice(
                code="BIRTHDAY.REQUEST",
                title="🎂 Запрос на добавление дня рождения",
                summary=f"**Дата:** {birth_date}",
                details=(("Пользователь", interaction.user.mention),),
                next_step="Старший состав проверит дату и зафиксирует решение.",
                accent=WHITE_COLOR,
                reference=number,
            )
            await log_request(
                number,
                "birthday",
                interaction.user.id,
                birth_date,
                guild_id=interaction.guild.id,
                payload={"birth_date": birth_date},
            )

            bd_request_channel = get_text_channel(interaction.guild, "birthday_request")
            if bd_request_channel is None:
                raise RuntimeError(f"Канал {CHANNELS['birthday_request']} не найден")
            view = BirthdayApprovalView(interaction.user, number, birth_date)
            message = await send_v2(bd_request_channel,
                content=request_ping_content(interaction.guild),
                embed=apply_accent(embed, Accent.MEMBER),
                view=view,
                component_marker=f"request:{number}",
                allowed_mentions=request_ping_mentions(interaction.guild),
            )
            await set_request_publication(
                number,
                interaction.guild.id,
                bd_request_channel.id,
                message.id,
            )

            await followup_v2(interaction,
                embed=success_embed("✅ Заявка отправлена",
                                    f"Запрос на добавление дня рождения #{number} отправлен старшему составу на рассмотрение."),
                ephemeral=True
            )
        except Exception:
            if number is not None and message is None:
                await fail_request_publication(number, "Birthday request publication failed")
            logger.exception("Failed to submit birthday request")
            if interaction.response.is_done():
                await followup_v2(interaction,
                    embed=error_embed("❌ Ошибка", "Не удалось отправить запрос."),
                    ephemeral=True,
                )
            else:
                await respond_v2(interaction,
                    embed=error_embed("❌ Ошибка", "Не удалось отправить запрос."),
                    ephemeral=True
                )


async def _submit_role_request(
    interaction: discord.Interaction, requested_role: str
) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        role_name = normalize_member_role(requested_role)
    except ValueError as exc:
        await edit_original_response_v2(interaction, content=str(exc), view=None)
        return

    number = await get_next_number("AR")
    embed = role_request_embed(interaction.user, role_name, number)
    role_channel = get_text_channel(interaction.guild, "role_request")
    if role_channel is None:
        await edit_original_response_v2(
            interaction, content="Канал запросов ролей не настроен.", view=None
        )
        return
    view = RoleApprovalView(interaction.user, role_name, number)
    await log_request(
        number,
        "role_request",
        interaction.user.id,
        role_name,
        guild_id=interaction.guild.id,
        payload={"role_name": role_name},
    )
    try:
        message = await send_v2(
            role_channel,
            content=request_ping_content(interaction.guild),
            embed=apply_accent(embed, Accent.MEMBER),
            view=view,
            component_marker=f"request:{number}",
            allowed_mentions=request_ping_mentions(interaction.guild),
        )
    except Exception as exc:
        await fail_request_publication(number, f"{type(exc).__name__}: {exc}")
        raise
    await set_request_publication(
        number,
        interaction.guild.id,
        role_channel.id,
        message.id,
    )
    await edit_original_response_v2(
        interaction,
        content=(
            f"Запрос на роль {role_name} ({number}) отправлен "
            "старшему составу на рассмотрение."
        ),
        embed=None,
        view=None,
    )


class RoleRequestSelect(discord.ui.Select):
    def __init__(
        self,
        role_names: tuple[str, ...] = (
            "Young",
            "Main",
            "Friend",
            "Super Friend",
        ),
        *,
        custom_id: str = "role_request_select",
    ):
        super().__init__(
            placeholder="Выберите запрашиваемую роль",
            min_values=1,
            max_values=1,
            custom_id=custom_id,
            options=[
                discord.SelectOption(label=role_name, value=role_name)
                for role_name in role_names
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await _submit_role_request(interaction, self.values[0])


class RoleRequestSelectView(SafeView):
    def __init__(
        self,
        role_names: tuple[str, ...] = (
            "Young",
            "Main",
            "Friend",
            "Super Friend",
        ),
        *,
        custom_id: str = "role_request_select",
    ):
        super().__init__(timeout=60)
        self.add_item(RoleRequestSelect(role_names, custom_id=custom_id))


# ==================== VIEWS (Buttons) ====================

class PersistentHelpButton(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Нужна помощь", style=discord.ButtonStyle.primary, emoji="🆘", custom_id="help_request_btn")
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(HelpRequestModal())


class PersistentSSButton(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Отправить обращение", style=discord.ButtonStyle.secondary, emoji="📝",
                       custom_id="ss_request_btn")
    async def ss_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            logger.debug("Staff request button pressed by user=%s", interaction.user)
            # Открываем View с селектором типов
            view = SSRequestTypeSelectView()
            await respond_v2(interaction,
                embed=notice(
                    code="FORMS.SS_TYPE_SELECT",
                    title="Выберите тип обращения",
                    summary="Пожалуйста, выберите тип вашего обращения из списка ниже.",
                    next_step="Выберите тип обращения в списке ниже.",
                    accent=WHITE_COLOR,
                ),
                view=view,
                ephemeral=True
            )
        except Exception:
            logger.exception("Failed to open staff request selector")
            if not interaction.response.is_done():
                await respond_v2(interaction,
                    embed=error_embed("❌ Ошибка", "Не удалось открыть форму. Попробуйте ещё раз."),
                    ephemeral=True
                )


class PersistentVZPButton(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Оставить заявку в боевую команду", style=discord.ButtonStyle.success, emoji="📋",
                       custom_id="vzp_btn")
    async def vzp_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VZPModal())


class PersistentFamilyButton(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Подать заявку в семью", style=discord.ButtonStyle.primary, emoji="👨‍👩‍👧‍👦",
                       custom_id="family_btn")
    async def family_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        from cogs.applications import FamilyInviteModal

        await interaction.response.send_modal(FamilyInviteModal())


class PersistentBirthdayButton(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Добавить свой день рождения", style=discord.ButtonStyle.secondary, emoji="🎂",
                       custom_id="birthday_btn")
    async def birthday_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BirthdayModal())


class AddRolesView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Guest · включить / выключить",
        style=discord.ButtonStyle.secondary,
        emoji="👋",
        custom_id="guest_role_btn",
        row=0,
    )
    async def guest_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_public_role(interaction, "guest")

    @discord.ui.button(
        label="Help · включить / выключить",
        style=discord.ButtonStyle.secondary,
        emoji="🆘",
        custom_id="help_role_btn",
        row=0,
    )
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_public_role(interaction, "help")

    @discord.ui.button(
        label="Contract · включить / выключить",
        style=discord.ButtonStyle.secondary,
        emoji="📜",
        custom_id="contract_role_btn",
        row=0,
    )
    async def contract_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member):
            await respond_v2(interaction, "Команда доступна только на сервере.", ephemeral=True)
            return
        contract_role = get_role(interaction.guild, "contract")
        if contract_role is not None and contract_role in interaction.user.roles:
            await self._toggle_public_role(interaction, "contract")
            return
        family_roles = {
            ROLES["young"],
            ROLES["main"],
            ROLES["staff"],
            ROLES["high"],
            ROLES["dep_owner"],
            ROLES["owner"],
        }
        if not any(role.name in family_roles for role in interaction.user.roles):
            await respond_v2(interaction,
                "Роль Contract доступна только членам семьи с ролью Young, Main или выше.",
                ephemeral=True,
            )
            return
        await self._toggle_public_role(interaction, "contract")

    async def _toggle_public_role(
        self, interaction: discord.Interaction, role_key: str
    ) -> None:
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
        ):
            await respond_v2(
                interaction,
                "Панель ролей доступна только на сервере.",
                ephemeral=True,
            )
            return
        role = get_role(interaction.guild, role_key)
        if role is None:
            await respond_v2(interaction, "Роль не найдена. Обратитесь к администрации.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(
                    role,
                    reason="Пользователь отключил роль через панель ролей",
                )
                await followup_v2(
                    interaction,
                    embed=success_embed(
                        "Роль выключена",
                        f"Роль **{role.name}** больше не назначена.",
                    ),
                    ephemeral=True,
                )
                return
            await interaction.user.add_roles(
                role,
                reason="Пользователь получил роль через панель ролей",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Failed to toggle role %s for member %s",
                role.id,
                interaction.user.id,
            )
            await followup_v2(
                interaction,
                embed=error_embed(
                    "Не удалось изменить роль",
                    "Повторите попытку позже или сообщите администрации.",
                ),
                ephemeral=True,
            )
            return
        await followup_v2(
            interaction,
            embed=success_embed(
                "Роль включена",
                f"Роль **{role.name}** назначена.",
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Запросить Young / Main",
        style=discord.ButtonStyle.primary,
        emoji="⭐",
        custom_id="request_role_btn",
        row=1,
    )
    async def request_role_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await respond_v2(interaction,
            "Выберите роль для заявки. Решение примет старший состав.",
            view=RoleRequestSelectView(("Young", "Main")),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Запросить Friend / Super Friend",
        style=discord.ButtonStyle.primary,
        emoji="🤝",
        custom_id="request_friend_role_btn",
        row=1,
    )
    async def request_friend_role_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await respond_v2(
            interaction,
            "Выберите роль для заявки. Решение примет старший состав.",
            view=RoleRequestSelectView(
                ("Friend", "Super Friend"),
                custom_id="friend_role_request_select",
            ),
            ephemeral=True,
        )


class RoleApproveButton(discord.ui.Button):
    def __init__(self, controller: "RoleApprovalView", role_name: str):
        super().__init__(
            label=f"Одобрить + {role_name}",
            style=discord.ButtonStyle.success,
            custom_id=f"approve_role:{role_name.casefold().replace(' ', '_')}",
            row=0,
        )
        self.controller = controller
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.controller._approve(interaction, self.role_name)


class RoleApprovalView(SafeView):
    def __init__(self, target_user: discord.Member, role_name: str, number: str):
        super().__init__(timeout=None)
        self.target_user = target_user
        self.role_name = role_name
        self.number = number
        self.remove_item(self.deny)
        group = (
            ("Young", "Main")
            if role_name in {"Young", "Main"}
            else ("Friend", "Super Friend")
        )
        for approved_role in group:
            self.add_item(RoleApproveButton(self, approved_role))
        self.add_item(self.deny)

    async def _approve(
        self,
        interaction: discord.Interaction,
        approved_role_name: str,
    ) -> None:
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "У вас нет прав для этого действия.", ephemeral=True)
            return

        configured_role_name = _configured_member_role(approved_role_name)
        role = discord.utils.get(
            interaction.guild.roles,
            name=configured_role_name,
        )
        if role:
            await interaction.response.defer(ephemeral=True)
            if not await _claim_review(interaction, self.number, "approved"):
                return
            await _run_review_side_effect(
                interaction,
                self.number,
                self.target_user.add_roles(role),
            )
            if not await _finish_review(
                interaction,
                self.number,
                "approved",
                reason=role.name,
            ):
                return
            await _edit_decision_message(
                interaction.message,
                f"✅ Одобрено • {role.name} • {interaction.user.mention}",
                Accent.SUCCESS,
            )
            self.stop()
            await followup_v2(interaction,
                embed=success_embed("✅ Роль выдана",
                                    f"{self.target_user.mention} получил роль **{role.name}** (#{self.number})"),
                ephemeral=True
            )
        else:
            await respond_v2(
                interaction,
                f"Роль {configured_role_name} не найдена.",
                ephemeral=True,
            )

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger, custom_id="deny_role", row=0)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        source_message = interaction.message
        async def finish(selector_interaction: discord.Interaction, reason: str):
            if not await _claim_review(
                selector_interaction, self.number, "rejected"
            ):
                return
            if not await _finish_review(
                selector_interaction, self.number, "rejected", reason=reason
            ):
                return
            await _edit_decision_message(
                source_message,
                f"❌ Отклонено • {selector_interaction.user.mention}\nПричина: {reason}",
                Accent.ERROR,
            )
            self.stop()
            await edit_original_response_v2(selector_interaction,
                content=f"Запрос отклонён. Причина: **{reason}**", view=None
            )
        await respond_v2(interaction,
            "Укажите причину:", view=RejectionReasonView(finish), ephemeral=True
        )


class VZPApprovalView(SafeView):
    def __init__(
        self,
        target_user: discord.Member,
        number: str,
        original_embed: object | None = None,
    ):
        super().__init__(timeout=None)
        self.target_user = target_user
        self.number = number
        self.original_embed = original_embed

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success, custom_id="approve_vzp")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._approve(interaction)

    @discord.ui.button(label="Одобрить и выдать 1 приоритет", style=discord.ButtonStyle.success, custom_id="approve_vzp_1")
    async def approve_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._approve(interaction, "vzp_1")

    @discord.ui.button(label="Одобрить и выдать 2 приоритет", style=discord.ButtonStyle.success, custom_id="approve_vzp_2")
    async def approve_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._approve(interaction, "vzp_2")

    @discord.ui.button(label="Одобрить и выдать 3 приоритет", style=discord.ButtonStyle.success, custom_id="approve_vzp_3")
    async def approve_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._approve(interaction, "vzp_3")

    async def _approve(
        self, interaction: discord.Interaction, priority_key: str | None = None
    ):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "У вас нет прав для этого действия.", ephemeral=True)
            return

        vzp_role = get_role(interaction.guild, "vzp_team")
        priority_role = (
            get_role(interaction.guild, priority_key)
            if priority_key is not None
            else None
        )
        if vzp_role and (priority_key is None or priority_role is not None):
            await interaction.response.defer(ephemeral=True)
            if not await _claim_review(interaction, self.number, "approved"):
                return
            roles = [vzp_role]
            if priority_role is not None:
                roles.append(priority_role)
            await _run_review_side_effect(
                interaction,
                self.number,
                self.target_user.add_roles(*roles),
            )
            if not await _finish_review(interaction, self.number, "approved"):
                return

            await _edit_decision_message(
                interaction.message,
                f"✅ Одобрено • {interaction.user.mention}",
                Accent.SUCCESS,
            )
            self.stop()
            await followup_v2(interaction,
                embed=success_embed("✅ Заявка одобрена",
                                    f"{self.target_user.mention} получил "
                                    + (f"роли **{vzp_role.name}** и **{priority_role.name}**" if priority_role else f"роль **{vzp_role.name}**")
                                    + f" (#{self.number})"),
                ephemeral=True
            )
        else:
            await respond_v2(interaction, "Боевая роль или роль приоритета не найдена.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger, custom_id="deny_vzp")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return

        source_message = interaction.message
        async def finish(selector_interaction: discord.Interaction, reason: str):
            if not await _claim_review(
                selector_interaction, self.number, "rejected"
            ):
                return
            if not await _finish_review(
                selector_interaction, self.number, "rejected", reason=reason
            ):
                return
            await _edit_decision_message(
                source_message,
                f"❌ Отклонено • {selector_interaction.user.mention}\nПричина: {reason}",
                Accent.ERROR,
            )
            self.stop()
            await edit_original_response_v2(selector_interaction,
                content=f"Заявка отклонена. Причина: **{reason}**", view=None
            )
        await respond_v2(interaction,
            "Укажите причину:", view=RejectionReasonView(finish), ephemeral=True
        )


class BirthdayApprovalView(SafeView):
    def __init__(self, target_user: discord.Member, number: str, birth_date: str):
        super().__init__(timeout=None)
        self.target_user = target_user
        self.number = number
        self.birth_date = birth_date

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success, custom_id="approve_birthday")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not _has_staff_access(interaction):
                await respond_v2(interaction, "Нет прав.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            if not await _claim_review(interaction, self.number, "approved"):
                return

            await _run_review_side_effect(
                interaction,
                self.number,
                add_birthday(
                    self.target_user.id,
                    self.birth_date,
                    self.target_user.display_name,
                ),
            )
            from cogs.forms import update_happy_birthday_message
            await update_happy_birthday_message(interaction.guild)
            if not await _finish_review(interaction, self.number, "approved"):
                return

            await _edit_decision_message(
                interaction.message,
                f"✅ Одобрено • {interaction.user.mention}",
                Accent.SUCCESS,
            )
            self.stop()
            await followup_v2(interaction,
                embed=success_embed("✅ День рождения одобрен",
                                    f"День рождения {self.target_user.mention} ({self.birth_date}) добавлен."),
                ephemeral=True
            )
        except Exception:
            logger.exception("Failed to approve birthday request")
            if interaction.response.is_done():
                await followup_v2(interaction, "❌ Ошибка при одобрении.", ephemeral=True)
            else:
                await respond_v2(interaction, "❌ Ошибка при одобрении.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger, custom_id="deny_birthday")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return

        source_message = interaction.message
        async def finish(selector_interaction: discord.Interaction, reason: str):
            if not await _claim_review(
                selector_interaction, self.number, "rejected"
            ):
                return
            if not await _finish_review(
                selector_interaction, self.number, "rejected", reason=reason
            ):
                return
            await _edit_decision_message(
                source_message,
                f"❌ Отклонено • {selector_interaction.user.mention}\nПричина: {reason}",
                Accent.ERROR,
            )
            self.stop()
            await edit_original_response_v2(selector_interaction,
                content=f"Запрос отклонён. Причина: **{reason}**", view=None
            )
        await respond_v2(interaction,
            "Укажите причину:", view=RejectionReasonView(finish), ephemeral=True
        )


def _family_request_status(decision) -> str:
    if decision.status == "approved":
        reviewer = (
            f"<@{decision.reviewer_id}>"
            if decision.reviewer_id is not None
            else "старший состав"
        )
        role = f" • {decision.reason}" if decision.reason else ""
        status = f"✅ Одобрено{role} • {reviewer}"
    elif decision.status == "rejected":
        reviewer = (
            f"<@{decision.reviewer_id}>"
            if decision.reviewer_id is not None
            else "старший состав"
        )
        status = (
            f"❌ Отклонено • {reviewer}\n"
            f"Причина: {decision.reason or 'Не указана'}"
        )
    else:
        status = "🟡 На рассмотрении"
    if decision.nickname_status == "requested":
        requester = (
            f" • <@{decision.nickname_requested_by}>"
            if decision.nickname_requested_by is not None
            else ""
        )
        status += f"\n📝 Ожидается смена ника{requester}"
    elif decision.nickname_status == "confirmed":
        status += "\n✅ Пользователь сообщил, что сменил ник"
    return status


class NicknameChangedView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Поменял",
        style=discord.ButtonStyle.success,
        custom_id="family:nickname_changed",
    )
    async def changed(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.message is None:
            await followup_v2(interaction, "Сообщение запроса не найдено.", ephemeral=True)
            return
        decision = await confirm_nickname_change(
            interaction.message.id,
            interaction.user.id,
        )
        if decision is None:
            await followup_v2(
                interaction,
                "Запрос уже подтверждён или не относится к вашему аккаунту.",
                ephemeral=True,
            )
            return

        guild = (
            interaction.client.get_guild(decision.guild_id)
            if decision.guild_id is not None
            else None
        )
        if guild is not None:
            member = guild.get_member(decision.user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(decision.user_id)
                except discord.HTTPException:
                    member = None
            channel = (
                guild.get_channel(decision.channel_id)
                if decision.channel_id is not None
                else None
            )
            if (
                member is not None
                and isinstance(channel, discord.TextChannel)
                and decision.message_id is not None
            ):
                try:
                    message = await channel.fetch_message(decision.message_id)
                    from cogs.applications import (
                        FamilyApprovalView,
                        family_application_embed,
                    )

                    view = (
                        FamilyApprovalView(
                            member,
                            decision.number,
                            family_application_embed(
                                member,
                                decision.number,
                                decision.payload,
                            ),
                            str(decision.payload.get("ic_name") or ""),
                            str(decision.payload.get("ooc_name") or ""),
                            nickname_status=decision.nickname_status,
                        )
                        if decision.status == "pending"
                        else None
                    )
                    await edit_status_message_v2(
                        message,
                        status=_family_request_status(decision),
                        accent_color=(
                            discord.Color.green()
                            if decision.status == "approved"
                            else discord.Color.red()
                            if decision.status == "rejected"
                            else WHITE_COLOR
                        ),
                        view=view,
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Failed to update nickname status for %s",
                        decision.number,
                    )

            await _log_request_action(
                guild,
                f"📩 <@{decision.user_id}> подтвердил смену ника "
                f"по заявке **{decision.number}**",
            )
            if isinstance(channel, discord.TextChannel):
                confirmation = notice(
                    code="FAMILY.NICKNAME_CONFIRMED",
                    title="Подтверждение смены ника",
                    summary=(
                        f"<@{decision.user_id}> сообщил о смене ника на сервере.\n"
                        f"Заявка: **{decision.number}**"
                    ),
                    next_step="Проверьте ник на сервере и продолжите рассмотрение заявки.",
                    accent=WHITE_COLOR,
                    reference=decision.number,
                )
                try:
                    await send_v2(
                        channel,
                        content=request_ping_content(guild),
                        embed=confirmation,
                        component_marker=f"nickname-confirmed:{decision.number}",
                        allowed_mentions=request_ping_mentions(guild),
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Could not publish nickname confirmation for %s",
                        decision.number,
                    )

        await edit_original_response_v2(
            interaction,
            "Спасибо. Старший состав получил подтверждение смены ника.",
            view=None,
        )


# ==================== HELPER FUNCTIONS ====================

def _ticket_review_roles(
    guild: discord.Guild, ticket_type: str
) -> list[discord.Role]:
    clean_type = re.sub(r"[^\w-]+", "-", ticket_type.casefold()).strip("-_")
    if clean_type != "owner-request":
        return request_ping_roles(guild)
    owner_role = get_role(guild, "owner")
    return [owner_role] if owner_role is not None else []


def _ticket_ping_content(roles: list[discord.Role]) -> str | None:
    return " ".join(role.mention for role in roles) or None


def _ticket_ping_mentions(roles: list[discord.Role]) -> discord.AllowedMentions:
    return discord.AllowedMentions(roles=roles, users=False, everyone=False)


def _ticket_notification(
    guild: discord.Guild,
    ticket_type: str,
    roles: list[discord.Role],
) -> tuple[str | None, discord.AllowedMentions]:
    if ticket_type == "bot-request" and BOT_OWNER_ID is not None:
        return (
            f"<@{BOT_OWNER_ID}>",
            discord.AllowedMentions(
                roles=False,
                users=[discord.Object(id=BOT_OWNER_ID)],
                everyone=False,
            ),
        )
    return _ticket_ping_content(roles), _ticket_ping_mentions(roles)


def _ticket_overwrites(
    guild: discord.Guild,
    author: discord.Member,
    clean_type: str,
    review_roles: list[discord.Role],
) -> dict:
    overwrites: dict[
        discord.Role | discord.Member, discord.PermissionOverwrite
    ] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        author: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if clean_type == "bot-request":
        bot_member = guild.me
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                manage_channels=True,
            )
        owner_member = (
            guild.get_member(BOT_OWNER_ID) if BOT_OWNER_ID is not None else None
        )
        if owner_member is not None:
            overwrites[owner_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
            )
        return overwrites
    for staff_role in review_roles:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_messages=True
        )
    return overwrites


async def _find_or_create_ticket_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    *,
    marker: str,
    channel_name: str,
    topic: str,
    overwrites: dict,
) -> discord.TextChannel:
    """Return the ticket channel, reusing one left behind by a failed attempt.

    The marker lives in the channel topic, which survives a crash between
    "channel created" and "channel_id written". Without it a retry silently
    creates a second channel for the same ticket.
    """

    for channel in category.text_channels:
        if channel.topic and marker in channel.topic:
            return channel
    return await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        topic=topic,
    )


async def _respond_ticket(interaction: discord.Interaction, spec) -> None:
    if interaction.response.is_done():
        await followup_spec(interaction, spec, ephemeral=True)
    else:
        await respond_spec(interaction, spec, ephemeral=True)


async def create_ticket(
    interaction: discord.Interaction,
    ticket_type: str,
    content: str,
    number: str,
):
    """Provision a ticket as a resumable, step-by-step operation.

    Order matters: every step is recorded before the side effect it describes,
    so a failure leaves a row saying exactly how far the operation got. The
    staff notification and the audit line are queued in the outbox — they are
    not allowed to make a ticket that exists look like one that failed.
    """

    guild = interaction.guild
    category = get_category(guild, "tickets_category")
    if category is None:
        await _respond_ticket(
            interaction,
            unchanged(
                code="TICKET.CATEGORY_MISSING",
                title="Тикет не создан",
                summary=(
                    f"Категория «{CHANNELS['tickets_category']}» не найдена, "
                    "поэтому обращение не сохранено."
                ),
                next_step="Сообщите администрации — требуется настройка бота.",
                reference=str(interaction.id),
            ),
        )
        return

    clean_type = re.sub(r"[^\w-]+", "-", ticket_type.casefold()).strip("-_")
    review_roles = _ticket_review_roles(guild, clean_type)
    if not review_roles and clean_type != "bot-request":
        # Creating the channel anyway would produce a ticket only its author can
        # see — an owner-request that reaches nobody is worse than a refusal.
        await _respond_ticket(
            interaction,
            unchanged(
                code="TICKET.NO_REVIEWERS",
                title="Тикет не создан",
                summary=(
                    "Не удалось определить роли, которые должны видеть это "
                    "обращение, поэтому тикет не создан."
                ),
                next_step="Сообщите администрации: проверьте /admin preflight.",
                reference=str(interaction.id),
            ),
        )
        return

    username = re.sub(
        r"[^\w-]+", "-", interaction.user.name.casefold()
    ).strip("-_") or str(interaction.user.id)
    serial = number.rsplit("-", 1)[-1]
    public_number = f"{clean_type}-{serial}"
    channel_name = f"{clean_type}-{username}-{serial}"[:100]

    ticket = await begin_ticket(
        guild_id=guild.id,
        interaction_id=interaction.id,
        ticket_type=ticket_type,
        number=public_number,
        author_id=interaction.user.id,
    )
    if ticket.is_provisioned:
        # The same interaction was already provisioned; never build a second
        # channel for it.
        await _respond_ticket(
            interaction,
            completed(
                code="TICKET.ALREADY_CREATED",
                title="Тикет уже создан",
                summary=f"Обращение #{public_number} уже открыто: <#{ticket.channel_id}>.",
                reference=public_number,
            ),
        )
        return

    marker = channel_marker(interaction.id)
    try:
        ticket_channel = await _find_or_create_ticket_channel(
            guild,
            category,
            marker=marker,
            channel_name=channel_name,
            topic=(
                f"Тикет #{public_number} | {ticket_type} "
                f"от {interaction.user} | {marker}"
            ),
            overwrites=_ticket_overwrites(
                guild, interaction.user, clean_type, review_roles
            ),
        )
        await attach_ticket_channel(interaction.id, ticket_channel.id)

        embed = ticket_embed(
            f"Тикет: {public_number}",
            interaction.user,
            content,
            public_number,
            ticket_type,
        )
        ticket_ping, ticket_mentions = _ticket_notification(
            guild, clean_type, review_roles
        )
        message = await send_v2(
            ticket_channel,
            content=ticket_ping,
            embed=embed,
            view=TicketCloseView(interaction.user, public_number),
            allowed_mentions=ticket_mentions,
        )
        await activate_ticket(interaction.id, message.id)
        await create_ticket_assignment(
            ticket_channel.id, interaction.user.id, public_number
        )
    except Exception as exc:
        logger.exception("Failed to provision ticket %s", public_number)
        await fail_ticket(interaction.id, f"{type(exc).__name__}: {exc}")
        await _respond_ticket(
            interaction,
            failed(
                code="TICKET.PROVISIONING_FAILED",
                title="Тикет не создан",
                summary=(
                    "Обращение не сохранено. У бота может не хватать прав "
                    "Manage Channels."
                ),
                reference=str(interaction.id),
            ),
        )
        return

    # From here the ticket exists. Everything that follows is a side effect and
    # goes through the outbox, so a Discord failure retries in the background
    # instead of telling the author their ticket was not created.
    await enqueue_operation(
        idempotency_key=f"ticket.notify:{interaction.id}",
        guild_id=guild.id,
        kind="ticket.notify",
        aggregate_id=str(ticket_channel.id),
        payload={
            "channel_id": ticket_channel.id,
            "number": public_number,
            "ticket_type": ticket_type,
            "clean_type": clean_type,
            "author_id": interaction.user.id,
        },
    )
    await enqueue_operation(
        idempotency_key=f"ticket.audit:{interaction.id}",
        guild_id=guild.id,
        kind="ticket.audit",
        aggregate_id=str(ticket_channel.id),
        payload={
            "text": (
                f"🎫 Создан тикет {ticket_channel.mention} "
                f"(#{public_number}) — {ticket_type}"
            ),
        },
    )
    await _respond_ticket(
        interaction,
        completed(
            code="TICKET.CREATED",
            title="Тикет создан",
            summary=f"Обращение #{public_number} открыто: {ticket_channel.mention}",
            next_step="Опишите детали в созданном канале — ответственный получит уведомление.",
            reference=public_number,
        ),
    )



class TicketAssigneeSelect(discord.ui.UserSelect):
    def __init__(self, source_message: discord.Message):
        super().__init__(
            placeholder="Выберите сотрудника",
            min_values=1,
            max_values=1,
            custom_id="ticket:assignee:user",
        )
        self.source_message = source_message

    async def callback(self, interaction: discord.Interaction):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        selected = self.values[0]
        member = (
            selected
            if isinstance(selected, discord.Member)
            else interaction.guild.get_member(selected.id)
        )
        if member is None or not _is_staff_member(member):
            await followup_v2(interaction,
                "Выбранный участник больше не относится к старшему составу.",
                ephemeral=True,
            )
            return
        if not can_assign_ticket_member(interaction.user, member):
            await followup_v2(
                interaction,
                "Нельзя назначить сотрудника, чья учитываемая роль выше вашей.",
                ephemeral=True,
            )
            return
        result, _ = await set_ticket_assignee(
            self.source_message.channel.id, member.id
        )
        ticket = await get_ticket_assignment(self.source_message.channel.id)
        if result == "missing" or ticket is None:
            await followup_v2(interaction,
                content="Данные тикета не найдены.", view=None
            )
            return
        owner_id, number, _ = ticket
        await edit_status_message_v2(
            self.source_message,
            status=(
                "🟢 В работе\n"
                f"Ответственный: <@{member.id}>\n"
                f"Обновил: {interaction.user.mention}"
            ),
            accent_color=WHITE_COLOR,
            view=TicketCloseView(owner_id, number, member.id),
        )
        await _log_request_action(
            interaction.guild,
            f"🎫 Тикет {self.source_message.channel.mention} назначен на "
            f"{member.mention} пользователем {interaction.user.mention}",
        )
        await followup_v2(interaction,
            content=f"Тикет назначен на {member.mention}.", view=None
        )


class TicketAssigneeView(SafeView):
    def __init__(self, source_message: discord.Message):
        super().__init__(timeout=180)
        self.add_item(TicketAssigneeSelect(source_message))


class TicketCloseView(SafeView):
    def __init__(
        self,
        owner: discord.Member | int,
        number: str,
        assigned_user_id: int | None = None,
    ):
        super().__init__(timeout=None)
        self.owner_id = owner.id if isinstance(owner, discord.Member) else int(owner)
        self.number = number
        if assigned_user_id is not None:
            self.claim_ticket.label = "Сняться с тикета"
            self.claim_ticket.style = discord.ButtonStyle.secondary
            self.claim_ticket.emoji = "↩️"

    @discord.ui.button(
        label="Взять тикет",
        style=discord.ButtonStyle.success,
        emoji="✋",
        custom_id="claim_ticket",
    )
    async def claim_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not _has_staff_access(interaction):
            await respond_v2(interaction,
                "Только старший состав может управлять тикетом.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        ticket = await get_ticket_assignment(interaction.channel_id)
        if ticket is None:
            await respond_v2(interaction,
                "Данные тикета не найдены.", ephemeral=True
            )
            return
        owner_id, number, assigned_id = ticket
        if assigned_id is None:
            result, assigned_id = await assign_ticket(
                interaction.channel_id, interaction.user.id
            )
            if result == "assigned":
                await respond_v2(interaction,
                    f"Тикет уже назначен на <@{assigned_id}>.", ephemeral=True
                )
                return
            assigned_id = interaction.user.id
            text = "Тикет назначен на вас."
        else:
            result, current_id = await unassign_ticket(
                interaction.channel_id,
                interaction.user.id,
                force=has_administrator_access(interaction.user),
            )
            if result == "forbidden":
                await respond_v2(interaction,
                    f"Тикет назначен на <@{current_id}>. Сняться может назначенный сотрудник или администратор.",
                    ephemeral=True,
                )
                return
            if result != "ok":
                await respond_v2(interaction,
                    "На тикет сейчас никто не назначен.", ephemeral=True
                )
                return
            assigned_id = None
            text = "Вы снялись с тикета."
        await edit_status_message_v2(
            interaction.message,
            status=(
                "🟢 В работе\n"
                f"Ответственный: <@{assigned_id}>\n"
                f"Обновил: {interaction.user.mention}"
                if assigned_id is not None
                else (
                    "🟡 Ожидает назначения\n"
                    f"Обновил: {interaction.user.mention}"
                )
            ),
            accent_color=WHITE_COLOR,
            view=TicketCloseView(owner_id, number, assigned_id),
        )
        await _log_request_action(
            interaction.guild,
            f"🎫 {interaction.channel.mention}: "
            + (
                f"назначен {interaction.user.mention}"
                if assigned_id is not None
                else f"назначение снял {interaction.user.mention}"
            ),
        )
        await followup_v2(interaction, text, ephemeral=True)

    @discord.ui.button(
        label="Назначить на тикет",
        style=discord.ButtonStyle.primary,
        emoji="👤",
        custom_id="assign_ticket_member",
    )
    async def assign_member(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        await respond_v2(interaction,
            "Выберите сотрудника:",
            view=TicketAssigneeView(interaction.message),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Закрыть тикет",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="close_ticket",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        ticket = await get_ticket_assignment(interaction.channel_id)
        owner_id = ticket[0] if ticket else self.owner_id
        number = ticket[1] if ticket else self.number
        if interaction.user.id != owner_id and not _has_staff_access(interaction):
            await followup_v2(interaction,
                "Только автор тикета или старший состав может закрыть.",
                ephemeral=True,
            )
            return

        # Order reversed on purpose. Deleting the channel first meant a failure
        # in either of the two statements below left an assignment pointing at a
        # channel that no longer existed, with nothing to retry from. Now the
        # durable side is settled first and the deletion runs through the
        # outbox, which retries on its own.
        claimed = await begin_ticket_close(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            number=str(number),
            author_id=owner_id,
            ticket_type=str(number).rsplit("-", 1)[0],
            closed_by=interaction.user.id,
        )
        if not claimed:
            await followup_spec(
                interaction,
                unchanged(
                    code="TICKET.ALREADY_CLOSING",
                    title="Тикет уже закрывается",
                    summary="Закрытие этого обращения уже запущено.",
                    next_step="Дождитесь удаления канала.",
                    reference=str(number),
                ),
                ephemeral=True,
            )
            return

        await record_stats_processed_action(
            interaction.guild.id, "ticket", "closed", interaction.user.id
        )
        await delete_ticket_assignment(interaction.channel_id)
        await enqueue_operation(
            idempotency_key=f"ticket.close:{interaction.channel_id}",
            guild_id=interaction.guild.id,
            kind="ticket.delete_channel",
            aggregate_id=str(interaction.channel_id),
            payload={
                "channel_id": interaction.channel_id,
                "number": str(number),
                "closed_by": interaction.user.id,
            },
        )
        await followup_spec(
            interaction,
            queued(
                code="TICKET.CLOSING",
                title="Тикет закрыт",
                summary=f"Обращение #{number} закрыто и учтено в статистике.",
                next_step="Канал будет удалён в течение нескольких секунд.",
                reference=str(number),
            ),
            ephemeral=True,
        )


class LegacyTicketUnassignView(SafeView):
    """Handle the old separate button until existing tickets are updated."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Сняться с тикета",
        style=discord.ButtonStyle.secondary,
        custom_id="unassign_ticket",
    )
    async def unassign_legacy(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        ticket = await get_ticket_assignment(interaction.channel_id)
        if ticket is None:
            await respond_v2(interaction,
                "Данные тикета не найдены.", ephemeral=True
            )
            return
        owner_id, number, _ = ticket
        result, assigned_id = await unassign_ticket(
            interaction.channel_id,
            interaction.user.id,
            force=has_administrator_access(interaction.user),
        )
        if result == "forbidden":
            await respond_v2(interaction,
                f"Тикет назначен на <@{assigned_id}>.", ephemeral=True
            )
            return
        if result != "ok":
            await respond_v2(interaction,
                "На тикет сейчас никто не назначен.", ephemeral=True
            )
            return
        await edit_interaction_status_v2(
            interaction,
            status=(
                "🟡 Ожидает назначения\n"
                f"Обновил: {interaction.user.mention}"
            ),
            accent_color=WHITE_COLOR,
            view=TicketCloseView(owner_id, number),
        )
        await followup_v2(interaction, "Вы снялись с тикета.", ephemeral=True)


async def update_happy_birthday_embed(guild: discord.Guild):
    """Update the happy-birthday message"""
    from utils.database import get_all_birthdays
    from utils.embeds import birthday_list_embed

    channel = get_text_channel(guild, "happy_birthday")
    if not channel:
        return

    birthdays = await get_all_birthdays()
    embed = birthday_list_embed(birthdays)

    # Try to find existing message or send new
    # For simplicity, we send a new one or user can pin one. In production use message ID from env.
    await send_v2(channel, embed=embed)  # In real bot: edit existing message by ID


_BIRTHDAY_PANEL_LIMIT = 12
_MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def _upcoming_birthdays(grouped, *, limit: int, today=None):
    """The next ``limit`` birthdays, wrapping around the end of the year."""

    today = today or datetime.now(UTC).date()
    ordered = sorted(
        (
            (month, day, bd)
            for month, entries in grouped.items()
            for day, bd in entries
        ),
        key=lambda item: (
            (item[0], item[1]) < (today.month, today.day),  # past dates last
            item[0],
            item[1],
        ),
    )
    return ordered[:limit]


def _birthday_month_lines(grouped) -> list[str]:
    """Every birthday, grouped by month, as pageable lines."""

    lines: list[str] = []
    for month in sorted(grouped):
        lines.append(f"**{_MONTH_NAMES[month]}**")
        for day, bd in sorted(grouped[month], key=lambda item: item[0]):
            lines.append(f"{day:02d}.{month:02d} — <@{bd['user_id']}>")
    return lines


async def _grouped_birthdays():
    from collections import defaultdict

    from utils.database import get_all_birthdays

    grouped = defaultdict(list)
    for bd in await get_all_birthdays():
        try:
            day, month = map(int, bd["date"].split("."))
        except (ValueError, KeyError):
            continue
        grouped[month].append((day, bd))
    return grouped


class BirthdayListView(SafeView):
    """Opens the complete birthday list for one viewer, paged."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Все дни рождения",
        style=discord.ButtonStyle.secondary,
        emoji="📅",
        custom_id="birthdays:full_list",
    )
    async def full_list(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        grouped = await _grouped_birthdays()
        await send_paged_list(
            interaction,
            _birthday_month_lines(grouped),
            title="🎂 Дни рождения семьи Guinness",
            colour=WHITE_COLOR,
            empty="Пока нет записей.",
        )


async def _update_happy_birthday_message(guild: discord.Guild):
    """Обновляет сообщение с днями рождения, сгруппированными по месяцам"""
    from utils.database import set_setting

    channel = get_text_channel(guild, "happy_birthday")
    if not channel:
        return

    grouped = await _grouped_birthdays()

    # The panel shows what is actually useful at a glance — the birthdays that
    # are coming up. Cutting an alphabetically-ordered full list at 3800
    # characters used to hide everyone past the cut with no way to reach them;
    # the complete list now lives behind a button and is paged per viewer.
    total = sum(len(entries) for entries in grouped.values())
    upcoming = _upcoming_birthdays(grouped, limit=_BIRTHDAY_PANEL_LIMIT)
    if upcoming:
        description = "\n".join(
            f"{day:02d}.{month:02d} — <@{bd['user_id']}>"
            for month, day, bd in upcoming
        )
    else:
        description = "Пока нет записей."

    details = ()
    if total > len(upcoming):
        details = (
            (
                "Полный список",
                (
                    f"Всего записей: **{total}**. "
                    "Нажмите «Все дни рождения», чтобы открыть список по месяцам."
                ),
            ),
        )
    embed = notice(
        code="BIRTHDAY.PANEL",
        title="🎂 Дни рождения семьи Guinness",
        summary=description,
        details=details,
        next_step="Список обновляется автоматически.",
        accent=WHITE_COLOR,
    )

    def build() -> StaticPayload:
        return StaticPayload(
            embed=apply_accent(embed, Accent.MEMBER),
            view=BirthdayListView(),
            revision=3,
        )

    new_msg = await upsert_static_message(
        guild=guild,
        channel=channel,
        panel_key="family-birthdays",
        build=build,
        legacy_setting_key="happy_birthday_message_id",
        legacy_match=lambda item: bool(
            item.embeds and item.embeds[0].title == "🎂 Дни рождения семьи Guinness"
        ),
    )
    if new_msg is None:
        return
    await set_setting("happy_birthday_message_id", str(new_msg.id))


async def update_happy_birthday_message(guild: discord.Guild):
    async with _static_message_lock("birthdays", guild.id):
        await _update_happy_birthday_message(guild)

class TreasuryContributionModal(SafeModal, title="Транзакция казны"):
    def __init__(self, action: str):
        super().__init__()
        self.action = action

    amount = discord.ui.TextInput(label="Сумма", placeholder="100000", required=True)
    reason = discord.ui.TextInput(
        label="Причина",
        placeholder="На оргу",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            amount = normalize_positive_amount(self.amount.value)
            tx_number = await add_treasury_transaction(
                interaction.user.id,
                interaction.user.name,
                amount,
                self.action,
                self.reason.value,
                guild_id=interaction.guild.id,
                interaction_id=interaction.id,
            )
            await followup_v2(
                interaction,
                f"✅ Транзакция-{tx_number:04d} сохранена. "
                "Журнал и панель обновляются автоматически.",
                ephemeral=True,
            )
        except Exception:
            logger.exception("Failed to record treasury contribution")
            await followup_v2(interaction,
                "Не удалось записать транзакцию.", ephemeral=True
            )

async def _update_treasury_message(guild: discord.Guild):
    """Обновляет единственное сообщение казны в настроенном канале."""
    from utils.database import set_treasury_message_id
    from datetime import datetime, timezone, timedelta

    channel = get_text_channel(guild, "treasury")
    if channel is None:
        raise RuntimeError(f"Канал {CHANNELS['treasury']} не найден")

    balance = await get_treasury_balance()
    transactions = await get_last_transactions(10)

    msk = timezone(timedelta(hours=3))
    last_dt = datetime.now(timezone.utc).astimezone(msk).strftime('%d.%m %H:%M')

    description = f"Баланс: **{balance:,}$**\n\n**Последние 10 транзакций**\n"
    for user_id, _username, amount, _action, reason, _dt in transactions:
        if reason.startswith("Установка значения казны:"):
            raw_value = reason.partition(":")[2].strip()
            try:
                installed_value = int(raw_value)
            except ValueError:
                installed_value = abs(amount)
            description += (
                f"<@{user_id}> установил значение казны "
                f"**{installed_value:,}$**\n"
            )
        else:
            sign = "снял" if amount < 0 else "пополнил"
            safe_reason = reason if len(reason) <= 200 else reason[:197] + "…"
            line = f"<@{user_id}> {sign} {abs(amount):,}$ \"{safe_reason}\"\n"
            if len(description) + len(line) > 3_800:
                description += "…более ранние транзакции скрыты.\n"
                break
            description += line

    description += f"\nДата последней транзакции: {last_dt} (МСК)"
    from services.treasury import latest_live_reconciliation
    from ui.command_registry import cmd

    recon = await latest_live_reconciliation(guild.id)
    if recon is None:
        recon_line = "Сверка с игрой ещё не записывалась."
    else:
        recon_line = f"Последняя сверка с игрой: {recon.created_at}"
    description += f"\n{recon_line}"
    description += (
        f"\nНовые выплаты идут в счета Memphis через {cmd('treasury from_report')}."
    )

    embed = notice(
        code="TREASURY.PANEL",
        title="Казна Guinness Family",
        summary=description,
        next_step=(
            "Кнопки меняют старую казну. История сохранена. "
            f"Новые выплаты — {cmd('treasury obligation')}."
        ),
        accent=Accent.TREASURY,
    )
    def build() -> StaticPayload:
        return StaticPayload(
            embed=apply_accent(embed, Accent.TREASURY),
            view=TreasuryView(),
            revision=3,
        )

    msg = await upsert_static_message(
        guild=guild,
        channel=channel,
        panel_key="treasury",
        build=build,
        legacy_setting_key="treasury_message_id",
        legacy_match=lambda item: bool(
            item.embeds and item.embeds[0].title == "Казна Guinness Family"
        ),
    )
    if msg is None:
        return
    await set_treasury_message_id(msg.id)
    logger.debug("Treasury message reconciled id=%s", msg.id)


async def update_treasury_message(guild: discord.Guild):
    async with _static_message_lock("treasury", guild.id):
        await _update_treasury_message(guild)


async def send_treasury_transaction_message(
    guild: discord.Guild,
    embed,
    *,
    transaction_number: int | None = None,
) -> discord.Message:
    channel = get_text_channel(guild, "transactions")
    if channel is None:
        raise RuntimeError(f"Канал {CHANNELS['transactions']} не найден")
    marker = (
        f"treasury-transaction:{guild.id}:{transaction_number}"
        if transaction_number is not None
        else None
    )
    if marker is not None:
        expected = marker_component_id(marker)

        def has_marker(message: discord.Message) -> bool:
            def visit(component) -> bool:
                return getattr(component, "id", None) == expected or any(
                    visit(child) for child in getattr(component, "children", ())
                )

            return any(visit(component) for component in message.components)

        # This path is only used by outbox retries. A deeper bounded lookup is
        # deliberate: it closes the send-before-ack crash window without a
        # second public transaction message.
        async for candidate in channel.history(limit=1_000):
            if candidate.author == guild.me and has_marker(candidate):
                return candidate
    return await send_v2(channel, embed=embed, component_marker=marker)
