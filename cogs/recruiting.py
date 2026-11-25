import asyncio
import logging
from datetime import UTC, datetime, timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from repositories.recruiting import (
    count_unresolved_joins,
    decide_attribution,
    get_recruiting_summary,
    get_referral_decision,
    forget_invite,
    get_invite_baseline,
    get_unresolved_joins,
    record_unresolved_join,
    resolve_join,
    save_invite_baseline,
)
from services.static_messages import StaticPayload, upsert_static_message
from ui.layouts import marker_component_id
from ui.messages import (
    edit_message_v2,
    followup_spec,
    followup_v2,
    respond_v2,
    send_v2,
)
from ui.command_registry import cmd, code
from ui.results import completed, unchanged

from config import RECRUITING_ROLE_IDS, RECRUITING_ROLE_NAMES
from utils.database import (
    deactivate_recruiter_invite,
    deactivate_recruiter_invite_by_code,
    decide_recruiting_referral,
    get_active_recruiter_invites,
    get_recruiter_invite,
    mark_recruiting_application,
    record_recruiting_join,
    set_setting,
    upsert_recruiter_invite,
)
from utils.discord_lookup import get_text_channel
from ui.design import Accent
from ui.message_specs import MessageSpec, MessageTone
from ui.templates import notice, public_panel
from utils.embeds import apply_accent
from utils.access import has_administrator_access, is_bot_owner


logger = logging.getLogger(__name__)


def is_recruiter(member: discord.Member) -> bool:
    if is_bot_owner(member):
        return True
    if RECRUITING_ROLE_IDS:
        allowed = set(RECRUITING_ROLE_IDS)
        return any(role.id in allowed for role in member.roles)
    allowed_names = {name.casefold() for name in RECRUITING_ROLE_NAMES}
    return any(role.name.casefold() in allowed_names for role in member.roles)


def can_manage_recruiting(member: discord.Member) -> bool:
    return has_administrator_access(member) or is_recruiter(member)


def _top_recruiters(ranked, limit: int = 10) -> str:
    """Render an already-ranked ``(recruiter_id, count)`` list from SQL."""

    return "\n".join(
        f"**{place}.** <@{recruiter_id}> · **{count}**"
        for place, (recruiter_id, count) in enumerate(ranked[:limit], 1)
    ) or "Нет принятых участников за период."


def _recruiter_funnel_fields(
    guild: discord.Guild, per_recruiter
) -> list[tuple[str, str]]:
    totals: dict[int, list[int]] = {
        item.recruiter_id: [item.joined, item.applied, item.accepted]
        for item in per_recruiter
    }
    for member in guild.members:
        if not member.bot and is_recruiter(member):
            totals.setdefault(member.id, [0, 0, 0])
    ordered = sorted(
        totals.items(),
        key=lambda item: (-item[1][2], -item[1][1], -item[1][0], item[0]),
    )
    lines = [
        (
            f"**{place}.** <@{recruiter_id}>\n"
            f"↳ **{accepted}** принято · {applied} заявок · {joined} входов"
        )
        for place, (recruiter_id, (joined, applied, accepted)) in enumerate(
            ordered,
            1,
        )
    ] or ["Нет настроенных рекрутеров."]
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > 1000:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [
        (
            "Результаты рекрутеров"
            if index == 0
            else "Результаты рекрутеров · продолжение",
            chunk,
        )
        for index, chunk in enumerate(chunks[:4])
    ]


async def build_recruiting_embed(
    guild: discord.Guild, summary=None
) -> MessageSpec:
    if summary is None:
        summary = await get_recruiting_summary(
            guild.id,
            recent_since=(datetime.now(UTC) - timedelta(days=7)).isoformat(),
        )
    joined = summary.joined
    applied = summary.applied
    accepted = summary.accepted
    rejected = summary.rejected
    pending = summary.pending
    conversion = summary.conversion

    details: list[tuple[str, str]] = [
        (
            "Воронка",
            (
                f"**{joined}** вошли  →  **{applied}** подали заявку  →  "
                f"**{accepted}** приняты"
            ),
        ),
        ("Ожидают решения", f"**{pending}**"),
        ("Отклонены", f"**{rejected}**"),
        ("Общая конверсия", f"**{conversion:.1f}%**"),
    ]
    details.extend(_recruiter_funnel_fields(guild, summary.per_recruiter))
    details.append(("Приняты за 7 дней", _top_recruiters(summary.accepted_recent, 5)))
    details.append(("Всего принято", _top_recruiters(summary.accepted_total, 5)))
    return apply_accent(
        public_panel(
            code="RECRUIT.STATS",
            title="Рекрутинг · сводка",
            purpose=(
                "Путь кандидата: персональная ссылка → семейная заявка → решение.\n"
                "В отчёт входят только приглашения, закреплённые за рекрутерами."
            ),
            rules=tuple(details),
            action=f"Автообновление каждые 10 минут · ручное: {cmd('recruiting update')}",
        ),
        Accent.MEMBER,
    )


def _has_marker(message: discord.Message, marker: str) -> bool:
    expected = marker_component_id(marker)

    def visit(component) -> bool:
        return getattr(component, "id", None) == expected or any(
            visit(child) for child in getattr(component, "children", ())
        )

    return any(visit(component) for component in message.components)


async def _upsert_recruiting_log(
    guild: discord.Guild,
    embed: MessageSpec,
    *,
    marker: str | None = None,
    required: bool = False,
    find_existing: bool = True,
    edit_existing: bool = True,
) -> discord.Message | None:
    channel = get_text_channel(guild, "recruiting_logs")
    if channel is None:
        message = f"Recruiting log channel is missing in guild {guild.id}"
        if required:
            raise RuntimeError(message)
        logger.warning(message)
        return None
    existing = None
    if marker is not None and find_existing:
        async for candidate in channel.history(limit=250):
            if candidate.author == guild.me and _has_marker(candidate, marker):
                existing = candidate
                break
    try:
        if existing is not None:
            if not edit_existing:
                return existing
            return await edit_message_v2(
                existing,
                embed=embed,
                component_marker=marker,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return await send_v2(
            channel,
            embed=embed,
            component_marker=marker,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to send recruiting log in guild %s", guild.id)
        if required:
            raise
    return None


async def record_family_application(
    bot: commands.Bot,
    guild: discord.Guild,
    member: discord.Member,
    number: str,
    ic_name: str,
) -> None:
    recruiter_id = await mark_recruiting_application(
        guild.id, member.id, number, ic_name
    )
    if recruiter_id is None:
        return
    embed = apply_accent(
        notice(
            code="RECRUIT.APPLICATION",
            title=f"Заявка {number} · на рассмотрении",
            summary=(
                "Кандидат, пришедший по персональной ссылке, подал заявку в семью."
            ),
            details=(
                ("Кандидат", member.mention),
                ("Рекрутер", f"<@{recruiter_id}>"),
                ("Статус", "Ожидает решения"),
                ("Имя персонажа", ic_name),
            ),
            next_step="Дождитесь решения по заявке в той же карточке журнала.",
            reference=number,
        ),
        Accent.MEMBER,
    )
    await _upsert_recruiting_log(
        guild,
        embed,
        marker=f"recruiting-application:{guild.id}:{number}",
        find_existing=False,
    )
    cog = bot.get_cog("RecruitingCog")
    if cog is not None:
        await cog.refresh_stats(guild)


async def record_family_decision(
    bot: commands.Bot,
    guild: discord.Guild,
    member: discord.Member,
    number: str,
    accepted: bool,
    reviewer: discord.Member,
    detail: str,
) -> None:
    result = await decide_recruiting_referral(
        guild.id, member.id, number, accepted, reviewer.id
    )
    if result is None:
        return
    recruiter_id, changed = result
    requested_accepted = accepted
    decision_reviewer_id = reviewer.id
    stale_conflict = False
    if not changed:
        persisted = await get_referral_decision(guild.id, member.id, number)
        if persisted is not None and persisted[0] in {"accepted", "rejected"}:
            accepted = persisted[0] == "accepted"
            if persisted[1] is not None:
                decision_reviewer_id = int(persisted[1])
        if accepted != requested_accepted:
            stale_conflict = True
            detail = (
                "Ранее зафиксированное решение сохранено. Повторное действие "
                "не изменило итог заявки."
            )
    status = "принята" if accepted else "отклонена"
    details: list[tuple[str, str]] = [
        ("Кандидат", f"<@{member.id}>"),
        ("Рекрутер", f"<@{recruiter_id}>"),
        ("Решение принял", f"<@{decision_reviewer_id}>"),
    ]
    if not stale_conflict:
        details.append(
            ("Выданная роль" if accepted else "Причина отказа", detail)
        )
    embed = apply_accent(
        notice(
            code="RECRUIT.DECISION",
            title=f"Заявка {number} · {status}",
            summary=(
                detail
                if stale_conflict
                else "Решение сохранено в той же карточке рекрутингового журнала."
            ),
            details=tuple(details),
            next_step=(
                "Кандидат принят в семью" if accepted else "Заявка закрыта отказом"
            ),
            tone=MessageTone.SUCCESS if accepted else MessageTone.ERROR,
            reference=number,
        ),
        Accent.SUCCESS if accepted else Accent.ERROR,
    )
    await _upsert_recruiting_log(
        guild,
        embed,
        marker=f"recruiting-application:{guild.id}:{number}",
        required=True,
        edit_existing=changed,
    )
    cog = bot.get_cog("RecruitingCog")
    if cog is not None:
        await cog.refresh_stats(guild)


class RecruitingCog(
    commands.GroupCog,
    group_name="recruiting",
    group_description="Рекрутинг семьи",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invite_uses: dict[int, dict[str, int]] = {}
        # Guilds whose invite counts have been observed at least once in this
        # process. Until a guild is in here, attribution stays off.
        self._baseline_ready: set[int] = set()
        self._locks: dict[int, asyncio.Lock] = {}
        self._stats_locks: dict[int, asyncio.Lock] = {}
        # Last published report per guild. Identical content skips the edit.
        self._stats_digests: dict[int, tuple] = {}

    async def cog_load(self) -> None:
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                "recruiting-stats",
                self.stats_updater,
                interval=600,
                timeout=120,
            )

    async def cog_unload(self) -> None:
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("recruiting-stats")

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def _fetch_invites(
        self, guild: discord.Guild
    ) -> list[discord.Invite] | None:
        try:
            return await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Cannot read invites in guild %s; Manage Server permission is required",
                guild.id,
            )
            return None

    async def _create_invite(
        self, guild: discord.Guild, member: discord.Member, *, notify: bool
    ) -> str | None:
        channel = get_text_channel(guild, "recruiting_invite")
        if channel is None:
            logger.error("Recruiting invite channel is missing in guild %s", guild.id)
            return None
        try:
            invite = await channel.create_invite(
                max_age=0,
                max_uses=0,
                unique=True,
                reason=f"Personal recruiting link for {member} ({member.id})",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to create recruiting invite for %s", member.id)
            return None
        try:
            await upsert_recruiter_invite(
                guild.id, member.id, invite.code, invite.url, channel.id
            )
        except Exception:
            # Compensate: an invite Discord knows about but the database does
            # not would be counted against nobody and never revoked.
            logger.exception(
                "Failed to persist recruiting invite %s; revoking it", invite.code
            )
            try:
                await invite.delete(reason="Compensating unsaved recruiting invite")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not revoke unsaved invite %s", invite.code)
            return None
        self.invite_uses.setdefault(guild.id, {})[invite.code] = invite.uses or 0
        await save_invite_baseline(guild.id, self.invite_uses[guild.id])
        if notify:
            try:
                invite_embed = apply_accent(
                    notice(
                        code="RECRUIT.INVITE",
                        title="Персональная ссылка рекрутера",
                        summary=(
                            f"**Ваша ссылка:** {invite.url}\n\n"
                            "Передавайте её кандидатам, которых приглашаете лично. "
                            "Бот свяжет вход, семейную заявку и итоговое решение в "
                            "единую воронку рекрутинга."
                        ),
                        details=(
                            (
                                "Важно",
                                (
                                    "• не используйте ссылку другого рекрутера;\n"
                                    "• не публикуйте её от чужого имени;\n"
                                    "• при перевыпуске старая ссылка перестанет учитываться."
                                ),
                            ),
                        ),
                        next_step="Управление ссылкой: /recruiting link",
                    ),
                    Accent.MEMBER,
                )
                await send_v2(
                    member,
                    embed=invite_embed,
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.info("Cannot DM recruiting invite to member %s", member.id)
        return invite.url

    async def ensure_guild(self, guild: discord.Guild) -> None:
        async with self._lock(guild.id):
            invites = await self._fetch_invites(guild)
            if invites is None:
                return
            live = {invite.code: invite for invite in invites}
            observed = {invite.code: invite.uses or 0 for invite in invites}
            self.invite_uses[guild.id] = observed
            await save_invite_baseline(guild.id, observed)
            self._baseline_ready.add(guild.id)
            records = {
                row[0]: row for row in await get_active_recruiter_invites(guild.id)
            }
            eligible = {
                member.id: member
                for member in guild.members
                if not member.bot and is_recruiter(member)
            }
            for recruiter_id, record in records.items():
                if recruiter_id not in eligible or record[1] not in live:
                    await deactivate_recruiter_invite(guild.id, recruiter_id)
                    invite = live.get(record[1])
                    if recruiter_id not in eligible and invite is not None:
                        try:
                            await invite.delete(reason="Recruiter role removed")
                        except (discord.Forbidden, discord.HTTPException):
                            logger.exception("Failed to revoke invite %s", invite.code)
            for recruiter_id, member in eligible.items():
                record = records.get(recruiter_id)
                if record is None or record[1] not in live:
                    await self._create_invite(guild, member, notify=True)

    async def _refresh_stats(self, guild: discord.Guild) -> discord.Message | None:
        channel = get_text_channel(guild, "recruiting")
        if channel is None:
            logger.warning("Recruiting stats channel is missing in guild %s", guild.id)
            return None
        key = f"recruiting_stats_message_id:{guild.id}"

        now = datetime.now(UTC)
        summary = await get_recruiting_summary(
            guild.id, recent_since=(now - timedelta(days=7)).isoformat()
        )
        # The funnel also lists recruiters with zero referrals, so the roster
        # itself is part of what is rendered and must be part of the digest.
        roster = tuple(
            sorted(
                member.id
                for member in guild.members
                if not member.bot and is_recruiter(member)
            )
        )
        digest = (summary.digest(), roster)
        if self._stats_digests.get(guild.id) == digest:
            return None

        async def build() -> StaticPayload:
            embed = apply_accent(
                await build_recruiting_embed(guild, summary),
                Accent.MEMBER,
            )
            return StaticPayload(
                embed=embed,
                revision=3,
            )

        message = await upsert_static_message(
            guild=guild,
            channel=channel,
            panel_key="recruiting-statistics",
            build=build,
            legacy_setting_key=key,
            legacy_match=lambda item: bool(
                item.embeds and item.embeds[0].title == "💼 Рекрутинг Guinness Family"
            ),
        )
        if message is None:
            return None
        await set_setting(key, str(message.id))
        self._stats_digests[guild.id] = digest
        return message

    async def refresh_stats(self, guild: discord.Guild) -> discord.Message | None:
        lock = self._stats_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            return await self._refresh_stats(guild)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # The invite baseline must be loaded before the first join is judged.
        # Returning early here left attribution running blind for up to the ten
        # minutes until ``stats_updater`` first fired. The full refresh is still
        # left to the supervisor; only the baseline is primed eagerly.
        for guild in self.bot.guilds:
            try:
                await self._prime_baseline(guild)
            except aiohttp.ClientConnectionError as exc:
                logger.warning(
                    "Recruiting baseline deferred in guild %s: %s", guild.id, exc
                )
            except Exception:
                logger.exception("Failed to prime recruiting baseline in %s", guild.id)
        if getattr(self.bot, "task_supervisor", None) is not None:
            # GuinnessBot already starts ``stats_updater`` through TaskSupervisor.
            # Running the same refresh here would duplicate Discord requests.
            return
        for guild in self.bot.guilds:
            try:
                await self.ensure_guild(guild)
                await self.refresh_stats(guild)
            except aiohttp.ClientConnectionError as exc:
                logger.warning(
                    "Recruiting initialization deferred in guild %s: %s",
                    guild.id,
                    exc,
                )
            except Exception:
                logger.exception("Failed to initialize recruiting in guild %s", guild.id)

    async def _prime_baseline(self, guild: discord.Guild) -> None:
        """Load persisted invite counts and refresh them from Discord once."""

        async with self._lock(guild.id):
            self.invite_uses[guild.id] = await get_invite_baseline(guild.id)
            invites = await self._fetch_invites(guild)
            if invites is None:
                return
            observed = {invite.code: invite.uses or 0 for invite in invites}
            self.invite_uses[guild.id] = observed
            await save_invite_baseline(guild.id, observed)
            self._baseline_ready.add(guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        attributed = False
        async with self._lock(guild.id):
            invites = await self._fetch_invites(guild)
            if invites is None:
                return
            observed = {invite.code: invite.uses or 0 for invite in invites}
            baseline = self.invite_uses.get(guild.id) or await get_invite_baseline(
                guild.id
            )
            managed = {
                row[1]: row[0] for row in await get_active_recruiter_invites(guild.id)
            }

            # Persist what we just saw regardless of the decision below, so the
            # next join is judged against fresh counts.
            self.invite_uses[guild.id] = observed
            await save_invite_baseline(guild.id, observed)

            decision = decide_attribution(
                baseline=baseline,
                observed=observed,
                managed_codes=frozenset(managed),
                baseline_ready=guild.id in self._baseline_ready,
            )
            self._baseline_ready.add(guild.id)
            if decision.needs_review:
                await record_unresolved_join(
                    guild.id,
                    member.id,
                    reason=decision.reason,
                    candidates=", ".join(decision.candidates),
                )
                return
            if not decision.resolved:
                return
            await record_recruiting_join(
                guild.id, member.id, managed[decision.invite_code], decision.invite_code
            )
            attributed = True
        if attributed:
            await self.refresh_stats(guild)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        self.invite_uses.setdefault(invite.guild.id, {}).pop(invite.code, None)
        await forget_invite(invite.guild.id, invite.code)
        await deactivate_recruiter_invite_by_code(invite.guild.id, invite.code)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if before.bot or is_recruiter(before) == is_recruiter(after):
            return
        await self.ensure_guild(after.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot or not is_recruiter(member):
            return
        record = await get_recruiter_invite(member.guild.id, member.id)
        if record is None or not record[4]:
            return
        invites = await self._fetch_invites(member.guild)
        if invites is not None:
            invite = discord.utils.get(invites, code=record[1])
            if invite is not None:
                try:
                    await invite.delete(reason="Recruiter left the server")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Failed to revoke departed recruiter's invite")
        await deactivate_recruiter_invite(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.ensure_guild(guild)
        await self.refresh_stats(guild)

    async def stats_updater(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self.ensure_guild(guild)
                await self.refresh_stats(guild)
            except aiohttp.ClientConnectionError as exc:
                if self.bot.is_closed():
                    logger.info(
                        "Recruiting refresh stopped during shutdown in guild %s",
                        guild.id,
                    )
                else:
                    logger.warning(
                        "Recruiting refresh deferred in guild %s: %s",
                        guild.id,
                        exc,
                    )
            except Exception:
                logger.exception("Failed to refresh recruiting stats in %s", guild.id)

    @app_commands.command(
        name="link",
        description="Получить или перевыпустить свою персональную ссылку рекрутера",
    )
    @app_commands.describe(сбросить="Удалить старую ссылку и создать новую")
    async def recruiting_link(
        self, interaction: discord.Interaction, сбросить: bool = False
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await respond_v2(interaction,
                "Команда доступна только на сервере.", ephemeral=True
            )
            return
        if not is_recruiter(interaction.user):
            await respond_v2(interaction,
                "Персональные ссылки доступны только настроенному старшему составу.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        record = await get_recruiter_invite(interaction.guild.id, interaction.user.id)
        if record and record[4] and not сбросить:
            await followup_v2(interaction, f"Ваша ссылка: {record[2]}", ephemeral=True)
            return
        previous_code = record[1] if record and сбросить else None
        # Order matters. Revoking first meant a failed creation left the
        # recruiter with no link at all; now the old one is only dropped once a
        # replacement exists and is saved.
        url = await self._create_invite(interaction.guild, interaction.user, notify=False)
        if url is None:
            await followup_spec(
                interaction,
                unchanged(
                    code="RECRUITING.LINK_FAILED",
                    title="Ссылка не создана",
                    summary=(
                        "Не удалось создать персональную ссылку, старая осталась "
                        "действующей."
                        if previous_code
                        else "Не удалось создать персональную ссылку."
                    ),
                    next_step="Проверьте права бота на канал заявок и повторите.",
                    reference=str(interaction.id),
                ),
                ephemeral=True,
            )
            return
        if previous_code:
            for invite in await self._fetch_invites(interaction.guild) or []:
                if invite.code == previous_code:
                    try:
                        await invite.delete(
                            reason=f"Reset by recruiter {interaction.user}"
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        logger.exception("Failed to delete old recruiting invite")
                    break
            await forget_invite(interaction.guild.id, previous_code)
        await followup_spec(
            interaction,
            completed(
                code="RECRUITING.LINK_ISSUED",
                title="Ссылка выдана",
                summary=f"Ваша персональная ссылка: {url}",
                next_step=(
                    "Старая ссылка отозвана и больше не учитывается."
                    if previous_code
                    else "Передавайте её только кандидатам, которых приглашаете лично."
                ),
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="update",
        description="Обновить отчёт рекрутинга и проверить персональные ссылки",
    )
    async def recruiting_update(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await respond_v2(interaction, "Команда доступна на сервере.", ephemeral=True)
            return
        if not can_manage_recruiting(interaction.user):
            await respond_v2(interaction, "Недостаточно прав.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.ensure_guild(interaction.guild)
        await self.refresh_stats(interaction.guild)
        await followup_v2(interaction, "Отчёт и персональные ссылки обновлены.", ephemeral=True)

    @app_commands.command(
        name="stats",
        description="Показать текущую статистику рекрутинга",
    )
    async def recruiting_stats(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await respond_v2(interaction, "Команда доступна на сервере.", ephemeral=True)
            return
        if not can_manage_recruiting(interaction.user):
            await respond_v2(interaction, "Недостаточно прав.", ephemeral=True)
            return
        await respond_v2(interaction,
            embed=await build_recruiting_embed(interaction.guild), ephemeral=True
        )

    @app_commands.command(
        name="assign",
        description="Вручную назначить рекрутера, если Discord не определил приглашение",
    )
    @app_commands.describe(
        участник="Приведённый участник",
        рекрутер="Член старшего состава",
        номер_заявки="Например FI-0001, если заявка уже подана",
    )
    async def recruiting_assign(
        self,
        interaction: discord.Interaction,
        участник: discord.Member,
        рекрутер: discord.Member,
        номер_заявки: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await respond_v2(interaction, "Команда доступна на сервере.", ephemeral=True)
            return
        if (
            not isinstance(interaction.user, discord.Member)
            or not has_administrator_access(interaction.user)
        ):
            await respond_v2(interaction,
                "Команда доступна только администраторам.", ephemeral=True
            )
            return
        if not is_recruiter(рекрутер):
            await respond_v2(interaction,
                "У выбранного рекрутера нет настроенной роли старшего состава.", ephemeral=True
            )
            return
        if участник.bot or участник.id == рекрутер.id:
            await respond_v2(interaction, "Некорректный участник.", ephemeral=True)
            return
        assigned = await record_recruiting_join(
            interaction.guild.id, участник.id, рекрутер.id, None
        )
        if not assigned:
            await respond_v2(interaction,
                "Этот участник уже принят и учтён за рекрутером; привязка не изменена.",
                ephemeral=True,
            )
            return
        if номер_заявки:
            await mark_recruiting_application(
                interaction.guild.id,
                участник.id,
                номер_заявки.strip().upper(),
                участник.display_name,
            )
        await self.refresh_stats(interaction.guild)
        await respond_v2(interaction,
            f"{участник.mention} закреплён за рекрутером {рекрутер.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="unresolved",
        description="Показать входы, которые бот не смог привязать к рекрутеру",
    )
    async def recruiting_unresolved(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await followup_v2(interaction, "Команда доступна на сервере.", ephemeral=True)
            return
        if not can_manage_recruiting(interaction.user):
            await followup_v2(
                interaction,
                "Команда доступна настроенному старшему составу.",
                ephemeral=True,
            )
            return
        pending = await get_unresolved_joins(interaction.guild.id)
        total = await count_unresolved_joins(interaction.guild.id)
        if not pending:
            await followup_spec(
                interaction,
                completed(
                    code="RECRUITING.UNRESOLVED_EMPTY",
                    title="Непривязанных входов нет",
                    summary="Все входы за период привязаны к рекрутерам.",
                ),
                ephemeral=True,
            )
            return
        lines = [
            f"• <@{item.member_id}> — {item.reason_label}"
            + (f" ({item.candidates})" if item.candidates else "")
            for item in pending
        ]
        await followup_spec(
            interaction,
            unchanged(
                code="RECRUITING.UNRESOLVED",
                title="Входы без рекрутера",
                summary=(
                    f"Бот не стал угадывать рекрутера для **{total}** "
                    "входов — привязка сделала бы неверный учёт постоянным."
                ),
                details=(("Ожидают решения", "\n".join(lines)[:1024]),),
                next_step=f"Привяжите каждого через {code('recruiting resolve')}.",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="resolve",
        description="Вручную привязать вход участника к рекрутеру",
    )
    @app_commands.describe(
        участник="Участник из списка непривязанных входов",
        рекрутер="Рекрутер, за которым закрепить вход (пусто — вход без рекрутера)",
    )
    async def recruiting_resolve(
        self,
        interaction: discord.Interaction,
        участник: discord.Member,
        рекрутер: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await followup_v2(interaction, "Команда доступна на сервере.", ephemeral=True)
            return
        if not can_manage_recruiting(interaction.user):
            await followup_v2(
                interaction,
                "Команда доступна настроенному старшему составу.",
                ephemeral=True,
            )
            return
        if рекрутер is not None and not is_recruiter(рекрутер):
            await followup_spec(
                interaction,
                unchanged(
                    code="RECRUITING.NOT_A_RECRUITER",
                    title="Привязка не выполнена",
                    summary="У выбранного участника нет роли рекрутера.",
                ),
                ephemeral=True,
            )
            return
        if рекрутер is not None:
            await record_recruiting_join(
                interaction.guild.id, участник.id, рекрутер.id, None
            )
        closed = await resolve_join(
            interaction.guild.id,
            участник.id,
            recruiter_id=рекрутер.id if рекрутер is not None else None,
            resolved_by=interaction.user.id,
        )
        if not closed and рекрутер is None:
            await followup_spec(
                interaction,
                unchanged(
                    code="RECRUITING.NOTHING_TO_RESOLVE",
                    title="Ничего не изменилось",
                    summary="Для этого участника нет ожидающих записей.",
                ),
                ephemeral=True,
            )
            return
        await self.refresh_stats(interaction.guild)
        await followup_spec(
            interaction,
            completed(
                code="RECRUITING.RESOLVED",
                title="Вход привязан",
                summary=(
                    f"{участник.mention} закреплён за {рекрутер.mention}."
                    if рекрутер is not None
                    else f"{участник.mention} отмечен как вход без рекрутера."
                ),
            ),
            ephemeral=True,
        )

    async def revoke_all_invites(self, guild: discord.Guild) -> None:
        records = await get_active_recruiter_invites(guild.id)
        codes = {row[1] for row in records}
        invites = await self._fetch_invites(guild)
        if invites is None:
            raise RuntimeError("Discord invite list is unavailable")
        for invite in invites:
            if invite.code not in codes:
                continue
            try:
                await invite.delete(reason="Bot data reset")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Failed to revoke recruiting invite %s", invite.code)
        for recruiter_id, *_ in records:
            await deactivate_recruiter_invite(guild.id, recruiter_id)
        self.invite_uses.pop(guild.id, None)
        self._baseline_ready.discard(guild.id)
        await save_invite_baseline(guild.id, {})


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecruitingCog(bot))
