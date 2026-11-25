"""Memphis family applications: form, review and interview."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import CHANNELS
from core.log_context import current_reference
from core.permissions import Permission
from repositories.requests import (
    fail_request_publication,
    get_request_decision,
    log_request,
    set_request_publication,
)
from utils.database import get_next_number
from services.onboarding import (
    PRIME_DAYS,
    SPECIALIZATIONS,
    ApplicationDraft,
    OnboardingError,
    application_cutoff,
    accept_application,
    build_application_draft,
    draft_from_payload,
    draft_payload,
    invite_to_interview,
    reject_application,
    surname_policy,
)
from ui.command_registry import cmd
from ui.design import Accent
from ui.message_specs import MessageSpec
from ui.messages import followup_spec, followup_v2, respond_spec, respond_v2, send_v2
from ui.templates import missing_permission, notice, system_error
from utils.access import requires
from utils.discord_lookup import (
    get_text_channel,
    request_ping_content,
    request_ping_mentions,
)
from utils.views import SafeModal, SafeView

logger = logging.getLogger(__name__)


def _error_spec(exc: OnboardingError) -> MessageSpec:
    if exc.decision is not None:
        return missing_permission(exc.decision, reference=current_reference())
    return system_error(
        code=exc.code,
        summary=str(exc),
        retryable=exc.retryable,
        reference=current_reference(),
    )


def family_application_embed(
    member: discord.Member | discord.User,
    number: str,
    payload: dict,
    *,
    status: str | None = None,
    cutoff_summary: str | None = None,
):
    details: list[tuple[str, str]] = [
        ("Имя (IC)", str(payload.get("ic_name") or "Не указано")),
        (
            "Идентификатор персонажа Memphis",
            str(payload.get("character_id") or "не указан"),
        ),
        ("Имя и возраст (OOC)", str(payload.get("ooc_name") or "Не указано")),
        (
            "Специализация",
            SPECIALIZATIONS.get(
                str(payload.get("specialization") or ""),
                str(payload.get("specialization") or "не указана"),
            ),
        ),
        ("Прайм", str(payload.get("prime_time") or "не указан")),
        ("Кто пригласил", str(payload.get("source") or "не указано")),
    ]
    if payload.get("change_surname"):
        details.append(
            (
                "Смена фамилии (политика Guinness)",
                str(payload.get("change_surname")),
            )
        )
    if payload.get("ic_passport") and not payload.get("character_id"):
        details.append(
            (
                "Идентификатор другого проекта",
                "есть в заявке и не переносится в идентификатор Memphis",
            )
        )
    details.extend(
        (
            ("Пользователь", member.mention),
            ("Username", discord.utils.escape_markdown(str(member))),
            ("ID", str(member.id)),
        )
    )
    if cutoff_summary:
        details.append(("Сезонная отсечка", cutoff_summary))
    if status:
        details.append(("Статус", status))
    return notice(
        code="FAMILY.APPLICATION",
        title="Заявка на вступление в семью Guinness",
        details=tuple(details),
        next_step=(
            "Решение в Discord не делает человека членом организации. "
            f"После принятия заведите подтверждение в игре через {cmd('members verify')}."
        ),
        accent=Accent.MEMBER,
        reference=number,
    )


class ApplicationExtrasView(SafeView):
    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__(timeout=300)
        self.fields = fields
        self.specialization = "combat"
        self.prime: list[str] = []
        spec = discord.ui.Select(
            placeholder="Специализация",
            options=[
                discord.SelectOption(label=label, value=key)
                for key, label in SPECIALIZATIONS.items()
            ],
            min_values=1,
            max_values=1,
        )
        spec.callback = self._on_spec  # type: ignore[method-assign]
        prime = discord.ui.Select(
            placeholder="Прайм (Пн/Ср/Пт/Сб)",
            options=[
                discord.SelectOption(label=label, value=key)
                for key, label in PRIME_DAYS.items()
            ],
            min_values=1,
            max_values=4,
        )
        prime.callback = self._on_prime  # type: ignore[method-assign]
        self.add_item(spec)
        self.add_item(prime)

    async def _on_spec(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values") if interaction.data else None
        if values:
            self.specialization = str(values[0])
        await respond_v2(interaction, "Специализация записана.", ephemeral=True)

    async def _on_prime(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values") if interaction.data else None
        self.prime = [str(item) for item in values or ()]
        await respond_v2(interaction, "Прайм записан.", ephemeral=True)

    @discord.ui.button(label="Отправить заявку", style=discord.ButtonStyle.primary)
    async def submit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        if guild_id is None or interaction.guild is None:
            await followup_spec(
                interaction,
                system_error(
                    code="APP.GUILD_ONLY",
                    summary="Заявку можно подать только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            draft = await build_application_draft(
                guild_id,
                ooc_name=self.fields["ooc_name"],
                ic_name=self.fields["ic_name"],
                character_id=self.fields["character_id"],
                specialization=self.specialization,
                prime_time=",".join(self.prime),
                source=self.fields["source"],
                change_surname=self.fields.get("change_surname"),
            )
            await publish_application(interaction, draft)
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
        except Exception:
            logger.exception("Failed to publish family application")
            await followup_spec(
                interaction,
                system_error(
                    code="APP.PUBLISH",
                    summary="Не удалось отправить заявку. Данные не потеряны в форме; повторите.",
                    retryable=True,
                    reference=current_reference(),
                ),
            )


class FamilyInviteModal(SafeModal, title="Заявка на вступление в семью"):
    ooc_name = discord.ui.TextInput(
        label="OOC имя и возраст",
        placeholder="Например: Алексей, 20",
        max_length=60,
        required=True,
    )
    ic_name = discord.ui.TextInput(
        label="IC-имя",
        placeholder="Имя персонажа Memphis",
        max_length=50,
        required=True,
    )
    character_id = discord.ui.TextInput(
        label="Идентификатор персонажа Memphis",
        placeholder="Только число. Паспорт другого проекта не подставляется",
        max_length=20,
        required=True,
    )
    source = discord.ui.TextInput(
        label="Кто пригласил",
        placeholder="Имя или ID пригласившего",
        max_length=80,
        required=True,
    )
    change_surname = discord.ui.TextInput(
        label="Сменить фамилию Guinness? (да/нет)",
        placeholder="Политика семьи, не правило Majestic",
        max_length=10,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        policy = "ask"
        if interaction.guild_id is not None:
            policy = await surname_policy(interaction.guild_id)
        if policy == "required" and not str(self.change_surname.value or "").strip():
            await respond_spec(
                interaction,
                system_error(
                    code="APP.SURNAME",
                    summary=(
                        "По политике Guinness ответьте, готовы ли вы сменить фамилию. "
                        "Это не правило Majestic. Заявка не отправлена."
                    ),
                    retryable=True,
                    reference=current_reference(),
                ),
            )
            return
        await respond_spec(
            interaction,
            notice(
                code="APP.DETAILS",
                title="Ещё два поля",
                summary="Выберите специализацию и прайм (Пн/Ср/Пт/Сб), затем отправьте заявку.",
                next_step="После отправки кадровик увидит сезонную отсечку на карточке.",
            ),
            view=ApplicationExtrasView(
                {
                    "ooc_name": str(self.ooc_name.value),
                    "ic_name": str(self.ic_name.value),
                    "character_id": str(self.character_id.value),
                    "source": str(self.source.value),
                    "change_surname": str(self.change_surname.value or ""),
                }
            ),
            ephemeral=True,
        )


async def publish_application(
    interaction: discord.Interaction, draft: ApplicationDraft
) -> None:
    guild = interaction.guild
    if guild is None:
        raise OnboardingError(
            "Заявку можно подать только на сервере.",
            code="APP.GUILD_ONLY",
            retryable=False,
        )
    number = await get_next_number("FI")
    payload = draft_payload(draft)
    cutoff = await application_cutoff(guild.id)
    embed = family_application_embed(
        interaction.user, number, payload, cutoff_summary=cutoff.summary
    )
    await log_request(
        number,
        "family_invite",
        interaction.user.id,
        draft.ic_name,
        guild_id=guild.id,
        payload=payload,
    )
    family_channel = get_text_channel(guild, "family_invite_logs")
    if family_channel is None:
        await fail_request_publication(number, "Family review channel is missing")
        raise RuntimeError(f"Канал {CHANNELS['family_invite_logs']} не найден")
    view = FamilyApprovalView(
        interaction.user, number, embed, draft.ic_name, draft.ooc_name
    )
    try:
        message = await send_v2(
            family_channel,
            content=request_ping_content(guild),
            embed=embed,
            view=view,
            component_marker=f"request:{number}",
            allowed_mentions=request_ping_mentions(guild),
        )
    except Exception as exc:
        await fail_request_publication(number, f"{type(exc).__name__}: {exc}")
        raise
    await set_request_publication(number, guild.id, family_channel.id, message.id)
    from cogs.recruiting import record_family_application

    try:
        await record_family_application(
            interaction.client, guild, interaction.user, number, draft.ic_name
        )
    except Exception:
        logger.exception("Failed to register family application in recruiting")
    await followup_spec(
        interaction,
        notice(
            code="APP.SENT",
            title="Заявка отправлена",
            summary=f"Заявка {number} ушла кадровику. Это ещё не членство в организации.",
            next_step="Дождитесь решения. При принятии понадобится подтверждение в игре.",
        ),
    )


class FamilyApprovalView(SafeView):
    def __init__(
        self,
        target_user: discord.Member,
        number: str,
        original_embed: object | None,
        ic_name: str,
        ooc_name: str,
        *,
        nickname_status: str = "idle",
    ):
        super().__init__(timeout=None)
        self.target_user = target_user
        self.number = number
        self.original_embed = original_embed
        self.ic_name = ic_name
        self.ooc_name = ooc_name
        if nickname_status == "requested":
            self.request_nickname.label = "Запрос ника отправлен"
            self.request_nickname.disabled = True
        elif nickname_status == "confirmed":
            self.request_nickname.label = "Ник изменён"
            self.request_nickname.style = discord.ButtonStyle.success
            self.request_nickname.disabled = True

    @discord.ui.button(
        label="Принять в кандидаты",
        style=discord.ButtonStyle.success,
        custom_id="approve_young",
    )
    async def approve_young(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self._accept(interaction)

    @discord.ui.button(
        label="Пригласить на собеседование",
        style=discord.ButtonStyle.secondary,
        custom_id="approve_main",
    )
    async def approve_main(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        await self._interview(interaction)

    @discord.ui.button(
        label="Запросить смену ника",
        style=discord.ButtonStyle.secondary,
        custom_id="family:request_nickname",
    )
    async def request_nickname(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        from cogs.forms import (
            NicknameChangedView,
            _family_request_status,
            _has_staff_access,
            _log_request_action,
        )
        from repositories.requests import (
            claim_nickname_change_request,
            complete_nickname_change_delivery,
            release_nickname_change_request,
        )
        from ui.messages import edit_status_message_v2

        del button
        if not _has_staff_access(interaction):
            await respond_spec(
                interaction,
                system_error(
                    code="APP.FORBIDDEN",
                    summary="Нет права запрашивать смену ника. Запрос не отправлен.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        await interaction.response.defer(ephemeral=True)
        state = await claim_nickname_change_request(self.number, interaction.user.id)
        if state != "claimed":
            messages = {
                "requested": "Запрос смены ника уже отправлен пользователю.",
                "sending": "Запрос уже отправляется другим сотрудником.",
                "confirmed": "Пользователь уже подтвердил смену ника.",
                "finalized": "Заявка уже обработана.",
                "missing": "Данные заявки не найдены.",
            }
            await followup_v2(
                interaction,
                messages.get(state, "Не удалось создать запрос смены ника."),
                ephemeral=True,
            )
            return
        dm_embed = notice(
            code="FAMILY.NICKNAME_REQUEST",
            title="Требуется изменить ник на сервере",
            summary=(
                "Старший состав просит установить ник по форме:\n"
                "**«Имя в игре | Имя в реале»**\n\n"
                "После изменения нажмите кнопку **«Поменял»**."
            ),
            next_step="После изменения нажмите кнопку «Поменял».",
            accent=Accent.MEMBER,
            reference=self.number,
        )
        try:
            dm_message = await send_v2(
                self.target_user,
                embed=dm_embed,
                view=NicknameChangedView(),
                component_marker=f"nickname-change:{self.number}",
            )
            if not await complete_nickname_change_delivery(self.number, dm_message.id):
                raise RuntimeError("Nickname request state changed during delivery")
        except Exception:
            await release_nickname_change_request(self.number)
            logger.exception("Failed to deliver nickname request %s", self.number)
            await followup_v2(
                interaction,
                "Не удалось отправить пользователю личное сообщение.",
                ephemeral=True,
            )
            return
        decision = await get_request_decision(self.number)
        if decision is not None:
            self.request_nickname.label = "Запрос ника отправлен"
            self.request_nickname.disabled = True
            await edit_status_message_v2(
                interaction.message,
                status=_family_request_status(decision),
                accent_color=int(Accent.MEMBER),
                view=self,
            )
        if interaction.guild is not None:
            await _log_request_action(
                interaction.guild,
                f"{interaction.user.mention} запросил смену ника у "
                f"{self.target_user.mention} по заявке **{self.number}**",
            )
        await followup_v2(
            interaction,
            "Пользователю отправлен запрос сменить ник.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Отклонить",
        style=discord.ButtonStyle.danger,
        custom_id="deny_family",
    )
    async def deny(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        del button
        from cogs.forms import RejectionReasonView, _claim_review, _finish_review
        from cogs.forms import _edit_decision_message, _family_request_status
        from cogs.forms import _has_staff_access

        if not _has_staff_access(interaction):
            await respond_spec(
                interaction,
                system_error(
                    code="APP.FORBIDDEN",
                    summary="Нет права разбирать заявки. Решение не записано.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        source_message = interaction.message

        async def finish(selector_interaction: discord.Interaction, reason: str):
            if not await _claim_review(selector_interaction, self.number, "rejected"):
                return
            try:
                await reject_application(
                    selector_interaction.user,
                    selector_interaction.guild_id or 0,
                    number=self.number,
                    reason=reason,
                )
            except OnboardingError as exc:
                await followup_spec(selector_interaction, _error_spec(exc))
                return
            if not await _finish_review(
                selector_interaction, self.number, "rejected", reason=reason
            ):
                return
            from cogs.recruiting import record_family_decision

            if selector_interaction.guild is not None:
                await record_family_decision(
                    selector_interaction.client,
                    selector_interaction.guild,
                    self.target_user,
                    self.number,
                    False,
                    selector_interaction.user,
                    reason,
                )
            decision = await get_request_decision(self.number)
            await _edit_decision_message(
                source_message,
                (
                    _family_request_status(decision)
                    if decision is not None
                    else f"Отклонено • {selector_interaction.user.mention}\nПричина: {reason}"
                ),
                Accent.ERROR,
            )
            self.stop()
            await followup_spec(
                selector_interaction,
                notice(
                    code="APP.REJECTED",
                    title="Заявка отклонена",
                    summary=f"Причина: {reason}. Кандидат не заведён в составе.",
                    next_step="Дополнительных действий не требуется.",
                ),
            )

        await respond_v2(
            interaction,
            "Укажите причину:",
            view=RejectionReasonView(finish),
            ephemeral=True,
        )

    async def _accept(self, interaction: discord.Interaction) -> None:
        from cogs.forms import (
            _claim_review,
            _edit_decision_message,
            _family_request_status,
            _finish_review,
            _has_staff_access,
        )

        if not _has_staff_access(interaction):
            await respond_spec(
                interaction,
                system_error(
                    code="APP.FORBIDDEN",
                    summary="Нет права разбирать заявки. Решение не записано.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        await interaction.response.defer(ephemeral=True)
        if not await _claim_review(interaction, self.number, "approved"):
            return
        decision = await get_request_decision(self.number)
        payload = decision.payload if decision is not None else {}
        draft = draft_from_payload(payload)
        if draft is None:
            from repositories.requests import expire_request_review_attempt

            await expire_request_review_attempt(
                self.number, interaction.user.id, str(interaction.id)
            )
            await followup_spec(
                interaction,
                system_error(
                    code="APP.STALE_PAYLOAD",
                    summary=(
                        "В заявке нет идентификатора персонажа Memphis. "
                        "Идентификатор персонажа Memphis обязателен. "
                        "Паспорт другого проекта не подставляется. Примите новую заявку."
                    ),
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            member, _track, limit = await accept_application(
                interaction.user,
                interaction.guild_id or 0,
                discord_id=self.target_user.id,
                number=self.number,
                draft=draft,
            )
        except OnboardingError as exc:
            from repositories.requests import expire_request_review_attempt

            await expire_request_review_attempt(
                self.number, interaction.user.id, str(interaction.id)
            )
            await followup_spec(interaction, _error_spec(exc))
            return
        if not await _finish_review(
            interaction, self.number, "approved", reason="candidate"
        ):
            return
        from cogs.recruiting import record_family_decision

        if interaction.guild is not None:
            await record_family_decision(
                interaction.client,
                interaction.guild,
                self.target_user,
                self.number,
                True,
                interaction.user,
                "кандидат, не член организации",
            )
        stored = await get_request_decision(self.number)
        await _edit_decision_message(
            interaction.message,
            (
                _family_request_status(stored)
                if stored is not None
                else f"Принят как кандидат • {interaction.user.mention}"
            ),
            Accent.SUCCESS,
        )
        self.stop()
        await followup_spec(
            interaction,
            notice(
                code="APP.ACCEPTED",
                title="Кандидат заведён",
                summary=(
                    f"{self.target_user.mention} в статусе кандидат. "
                    f"Это не членство в игре. {limit.message}"
                ),
                next_step=(
                    f"Подтвердите вступление в игре командой {cmd('members verify')}."
                ),
            ),
        )
        del member

    async def _interview(self, interaction: discord.Interaction) -> None:
        from cogs.forms import _has_staff_access

        if not _has_staff_access(interaction):
            await respond_spec(
                interaction,
                system_error(
                    code="APP.FORBIDDEN",
                    summary="Нет права разбирать заявки. Приглашение не отправлено.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await invite_to_interview(
                interaction.user,
                interaction.guild_id or 0,
                number=self.number,
                reason="собеседование",
            )
        except OnboardingError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        try:
            await send_v2(
                self.target_user,
                embed=notice(
                    code="APP.INTERVIEW",
                    title="Приглашение на собеседование",
                    summary=(
                        f"По заявке {self.number} кадровик приглашает вас на собеседование. "
                        "Решение о членстве ещё не принято."
                    ),
                    next_step="Дождитесь сообщения старшего состава со временем встречи.",
                ),
            )
        except discord.HTTPException:
            logger.exception("Failed to DM interview invite %s", self.number)
        await followup_spec(
            interaction,
            notice(
                code="APP.INTERVIEW_SENT",
                title="Приглашение записано",
                summary="Заявка остаётся открытой. Кандидат не заведён в составе.",
                next_step="После встречи примите или отклоните заявку.",
            ),
        )


class ApplicationsCog(commands.Cog):
    applications = app_commands.Group(
        name="applications",
        description="Заявки на вступление",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @applications.command(name="cutoff", description="Сезонная отсечка приёма")
    @requires(Permission.APPLICATIONS_REVIEW)
    async def cutoff_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="APP.GUILD_ONLY",
                    summary="Отсечка считается только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        cutoff = await application_cutoff(interaction.guild_id)
        await followup_spec(
            interaction,
            notice(
                code="APP.CUTOFF",
                title="Сезонная отсечка",
                summary=cutoff.summary,
                next_step="Часы игры появятся, когда подключится источник 9.1.",
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ApplicationsCog(bot))
