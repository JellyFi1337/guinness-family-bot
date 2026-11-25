"""Slash surface for the family roster."""

from __future__ import annotations

from collections.abc import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission, has_permission
from services.membership import (
    LeaveResult,
    MemberCard,
    MembershipError,
    STATUS_LABELS,
    apply_promote_newcomers,
    build_card,
    build_roster,
    extra_from_filters,
    filter_bar_options,
    preview_promote_newcomers,
    register_member,
    resolve_mentor_id,
    resolve_rank_id,
    roster_query_from_state,
    start_probation,
    verify_in_game,
    leave_member,
)
from ui.command_registry import cmd, code
from ui.custom_ids import CustomIdError
from ui.message_specs import MessageSpec, MessageTone
from ui.messages import followup_spec, followup_v2
from ui.navigation import (
    ConfirmDialog,
    FilterBar,
    FilterSpec,
    PageState,
    Paginator,
    register_page_source,
    state_from_id,
    unregister_sources,
)
from ui.templates import missing_permission, notice, system_error
from utils.access import requires

_DOMAIN = "members"


def _error_spec(exc: MembershipError) -> MessageSpec:
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


class MembersPageSource:
    async def get_lines(self, interaction: discord.Interaction, parsed) -> Sequence[str]:
        guild_id = interaction.guild_id
        if guild_id is None:
            return ("Реестр доступен только на сервере.",)
        state = state_from_id(parsed)
        query = roster_query_from_state(state.filter, state.extra)
        view = await build_roster(
            interaction.user,
            guild_id,
            sort=state.sort or "name",
            **query,
        )
        return view.lines

    async def allowed(self, interaction: discord.Interaction, parsed) -> bool:
        del parsed
        return await has_permission(interaction.user, Permission.MEMBERS_VIEW)

    def title(self, parsed) -> str:
        del parsed
        return "Состав семьи"


def _card_spec(card: MemberCard) -> MessageSpec:
    member = card.profile.member
    rank = card.profile.rank.name if card.profile.rank else "—"
    mentor = (
        card.profile.mentor.ic_name if card.profile.mentor is not None else "—"
    )
    character = (
        str(member.game_character_id)
        if member.game_character_id is not None
        else "не указан"
    )
    verified = (
        f"подтверждён {member.verified_at}"
        if card.verified
        else "не подтверждён в игре"
    )
    if card.obligations:
        obligations = "\n".join(
            (
                f"{item.summary}"
                + (" · требует сверки" if item.needs_reconciliation else "")
                + (" · не закрыто" if item.outstanding else "")
            )
            for item in card.obligations
        )
    else:
        obligations = "Открытых обязательств нет."
    decisions = (
        "\n".join(card.decisions) if card.decisions else "Решений в журнале нет."
    )
    clearances = (
        "\n".join(
            (
                f"{item.activity}: до {item.expires_at or 'бессрочно'}"
                if not item.revoked_at
                else f"{item.activity}: отозван"
            )
            for item in card.profile.clearances
        )
        or "Допусков нет."
    )
    return notice(
        code="MEMBERS.CARD",
        title=f"Карточка · {member.ic_name}",
        summary=(
            f"{card.discord_name} в Discord, персонаж {character}. "
            f"Смена ника Discord эту карточку не создаёт заново."
        ),
        details=(
            ("Статус", STATUS_LABELS.get(member.status, member.status)),
            ("Должность", rank),
            ("Специализация", member.direction or "—"),
            ("Наставник", mentor),
            ("Вступил", member.joined_at),
            ("Срок членства", f"{card.tenure_days} дн."),
            ("Проверка в игре", verified),
            ("Допуски", clearances),
            ("Обязательства", obligations),
            ("Журнал", decisions),
        ),
        next_step=(
            f"Подтверждение в игре — {code('members verify')}. "
            f"Выход — {code('members leave')}."
        ),
        tone=MessageTone.NEUTRAL,
        reference=current_reference(),
    )


def _leave_spec(result: LeaveResult) -> MessageSpec:
    leftover = (
        "\n".join(item.summary for item in result.obligations)
        if result.obligations
        else "Открытых денежных и складских обязательств в карточке нет."
    )
    return notice(
        code="MEMBERS.LEFT",
        title="Выход оформлен",
        summary=(
            f"{result.member.ic_name} больше не в текущем составе. "
            "Новые внутренние задачи закрыты, резервы освобождены, "
            "брони помечены как требующие сверки. "
            "Невыплаченные обязательства остались в истории."
        ),
        details=(("Обязательства", leftover),),
        next_step="Казначей закрывает оставшиеся обязательства отдельно.",
        tone=MessageTone.WARNING,
        reference=current_reference(),
    )


async def _roster_view(
    interaction: discord.Interaction,
    guild_id: int,
    *,
    status: str | None,
    rank_id: int | None,
    direction: str | None,
    mentor_id: int | None,
    prime_time: str | None,
    sort: str,
) -> None:
    view = await build_roster(
        interaction.user,
        guild_id,
        status=status,
        rank_id=rank_id,
        direction=direction,
        mentor_id=mentor_id,
        prime_time=prime_time,
        sort=sort,
    )
    specs = filter_bar_options(view.ranks, view.members)
    filters = tuple(
        FilterSpec(key=key, placeholder=placeholder, options=options)
        for key, placeholder, options in specs
        if len(options) > 1
    )
    state = PageState(
        page=1,
        filter=status or "all",
        sort=sort or "name",
        owner_id=interaction.user.id,
        extra=extra_from_filters(
            rank_id=rank_id,
            direction=direction,
            mentor_id=mentor_id,
            prime_time=prime_time,
        ),
    )
    try:
        pager = FilterBar(filters).attach(
            Paginator(
                domain=_DOMAIN,
                object_id=str(view.family.id),
                lines=view.lines,
                state=state,
                title="Состав семьи",
            )
        )
    except CustomIdError:
        pager = Paginator(
            domain=_DOMAIN,
            object_id=str(view.family.id),
            lines=view.lines,
            state=PageState(
                page=1,
                filter=status or "all",
                sort=sort or "name",
                owner_id=interaction.user.id,
            ),
            title="Состав семьи",
            filters=(
                FilterSpec(
                    key="f",
                    placeholder="Статус",
                    options=(
                        ("all", "Все текущие"),
                        ("candidate", "Кандидат"),
                        ("probation", "Испытательный срок"),
                        ("active", "В составе"),
                        ("leave", "Отпуск"),
                        ("left", "Вышел"),
                    ),
                ),
                FilterSpec(
                    key="s",
                    placeholder="Сортировка",
                    options=(
                        ("name", "Имя"),
                        ("joined", "Дата вступления"),
                        ("status", "Статус"),
                        ("rank", "Должность"),
                    ),
                ),
            ),
        )
    await followup_v2(interaction, None, view=pager, ephemeral=True)


class MembersCog(commands.Cog):
    members = app_commands.Group(
        name="members",
        description="Реестр состава семьи",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        register_page_source(_DOMAIN, MembersPageSource())

    async def cog_unload(self) -> None:
        unregister_sources(_DOMAIN)

    @members.command(name="list", description="Показать реестр состава")
    @app_commands.describe(
        status="Статус членства",
        rank="Должность",
        specialization="Специализация",
        mentor="Наставник",
        prime="Прайм-время",
        sort="Сортировка",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Все текущие", value="all"),
            app_commands.Choice(name="Кандидат", value="candidate"),
            app_commands.Choice(name="Испытательный срок", value="probation"),
            app_commands.Choice(name="В составе", value="active"),
            app_commands.Choice(name="Отпуск", value="leave"),
            app_commands.Choice(name="Вышел", value="left"),
        ],
        sort=[
            app_commands.Choice(name="Имя", value="name"),
            app_commands.Choice(name="Дата вступления", value="joined"),
            app_commands.Choice(name="Статус", value="status"),
            app_commands.Choice(name="Должность", value="rank"),
        ],
    )
    @requires(Permission.MEMBERS_VIEW)
    async def list_command(
        self,
        interaction: discord.Interaction,
        status: str | None = None,
        rank: str | None = None,
        specialization: str | None = None,
        mentor: discord.Member | None = None,
        prime: str | None = None,
        sort: str = "name",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MEMBERS.GUILD_ONLY",
                    summary="Реестр доступен только на сервере. Данные не прочитаны.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            await _roster_view(
                interaction,
                guild_id,
                status=status,
                rank_id=await resolve_rank_id(guild_id, rank),
                direction=specialization,
                mentor_id=await resolve_mentor_id(
                    guild_id, mentor.id if mentor is not None else None
                ),
                prime_time=prime,
                sort=sort,
            )
        except MembershipError as exc:
            await followup_spec(interaction, _error_spec(exc))

    @members.command(name="card", description="Карточка участника")
    @app_commands.describe(member="Участник Discord")
    @requires(Permission.MEMBERS_VIEW)
    async def card_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MEMBERS.GUILD_ONLY",
                    summary="Карточка доступна только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            card = await build_card(
                interaction.user,
                guild_id,
                member.id,
                discord_name=member.display_name,
            )
        except MembershipError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, _card_spec(card))

    @members.command(name="register", description="Завести участника в реестре")
    @app_commands.describe(
        member="Аккаунт Discord",
        ic_name="IC-имя",
        character_id="Идентификатор персонажа",
        rank="Должность",
        specialization="Специализация",
        mentor="Наставник",
    )
    @requires(Permission.MEMBERS_MANAGE)
    async def register_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        ic_name: str,
        character_id: app_commands.Range[int, 1, None] | None = None,
        rank: str | None = None,
        specialization: str | None = None,
        mentor: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MEMBERS.GUILD_ONLY",
                    summary="Заведение доступно только на сервере. Запись не создана.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            created, limit = await register_member(
                interaction.user,
                guild_id,
                discord_id=member.id,
                ic_name=ic_name,
                game_character_id=int(character_id) if character_id else None,
                rank_name=rank,
                direction=specialization,
                mentor_discord_id=mentor.id if mentor else None,
            )
        except MembershipError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="MEMBERS.REGISTERED",
                title="Участник заведён",
                summary=(
                    f"{created.ic_name} добавлен как кандидат. "
                    "Это ещё не членство в игровой организации."
                ),
                details=(
                    ("Лимит", limit.message),
                    ("Дальше", f"{code('members probation')} или {code('members verify')}"),
                ),
                next_step=(
                    "Подтвердите вступление в игре отдельно. "
                    "Выдача Discord-роли ранг в игре не меняет."
                ),
                tone=MessageTone.SUCCESS,
                reference=current_reference(),
            ),
        )

    @members.command(name="probation", description="Перевести на испытательный срок")
    @app_commands.describe(member="Участник", reason="Основание")
    @requires(Permission.MEMBERS_MANAGE)
    async def probation_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MEMBERS.GUILD_ONLY",
                    summary="Действие доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            updated = await start_probation(
                interaction.user, guild_id, member.id, reason=reason
            )
        except MembershipError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="MEMBERS.PROBATION",
                title="Испытательный срок",
                summary=f"{updated.ic_name} на испытательном сроке. В игре ещё не подтверждён.",
                next_step=f"Когда вступит в организацию — {code('members verify')}.",
                tone=MessageTone.SUCCESS,
                reference=current_reference(),
            ),
        )

    @members.command(name="verify", description="Подтвердить вступление в игре")
    @app_commands.describe(member="Участник", reason="Основание")
    @requires(Permission.MEMBERS_MANAGE)
    async def verify_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MEMBERS.GUILD_ONLY",
                    summary="Подтверждение доступно только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            updated = await verify_in_game(
                interaction.user, guild_id, member.id, reason=reason
            )
        except MembershipError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(
            interaction,
            notice(
                code="MEMBERS.VERIFIED",
                title="Вступление в игре подтверждено",
                summary=(
                    f"{updated.ic_name} теперь в составе. "
                    "Одобрение в Discord само по себе этим не было."
                ),
                next_step="Discord-роль отдельно не означает смену должности в игре.",
                tone=MessageTone.SUCCESS,
                reference=current_reference(),
            ),
        )

    @members.command(name="leave", description="Оформить выход из состава")
    @app_commands.describe(member="Участник", reason="Основание")
    @requires(Permission.MEMBERS_MANAGE)
    async def leave_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MEMBERS.GUILD_ONLY",
                    summary="Выход оформляется только на сервере. Запись не изменена.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            result = await leave_member(
                interaction.user, guild_id, member.id, reason=reason
            )
        except MembershipError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        await followup_spec(interaction, _leave_spec(result))

    @members.command(
        name="promote",
        description="Перевести группу кандидатов на испытательный срок",
    )
    @app_commands.describe(reason="Основание")
    @requires(Permission.MEMBERS_MANAGE)
    async def promote_command(
        self,
        interaction: discord.Interaction,
        reason: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="MEMBERS.GUILD_ONLY",
                    summary="Массовый перевод доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            preview = await preview_promote_newcomers(interaction.user, guild_id)
        except MembershipError as exc:
            await followup_spec(interaction, _error_spec(exc))
            return
        if not preview.members:
            await followup_spec(
                interaction,
                notice(
                    code="MEMBERS.PROMOTE_EMPTY",
                    title="Некого переводить",
                    summary="Кандидатов в текущем составе нет.",
                    next_step=f"Заведите людей через {code('members register')}.",
                    reference=current_reference(),
                ),
            )
            return
        member_ids = tuple(item.id for item in preview.members)
        names = "\n".join(preview.lines)

        async def _confirm(follow_up: discord.Interaction) -> None:
            try:
                updated = await apply_promote_newcomers(
                    follow_up.user, guild_id, member_ids, reason=reason
                )
            except MembershipError as exc:
                await followup_spec(follow_up, _error_spec(exc))
                return
            await followup_spec(
                follow_up,
                notice(
                    code="MEMBERS.PROMOTED",
                    title="Кандидаты на испытании",
                    summary=(
                        f"Переведены: {len(updated)}. "
                        "В основной состав они попадут только после "
                        f"{cmd('members verify')}."
                    ),
                    next_step="Подтверждайте вступление в игре по одному или повторно проверьте список.",
                    tone=MessageTone.SUCCESS,
                    reference=current_reference(),
                ),
            )

        dialog = ConfirmDialog(
            domain=_DOMAIN,
            object_id=f"p{guild_id}",
            summary=(
                "Эти кандидаты перейдут на испытательный срок. "
                f"В основной состав это их не переводит.\n{names}"
            ),
            actor=str(interaction.user),
            on_confirm=_confirm,
            owner_id=interaction.user.id,
        )
        await followup_v2(interaction, None, view=dialog, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MembersCog(bot))
