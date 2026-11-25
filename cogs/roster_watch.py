"""Discord leave and game-role loss cards for the roster officers."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import discord
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission, evaluate_access
from core.settings import get_registry
from services.membership import MembershipError, leave_member
from services.roster_watch import (
    AUDIT_LOOKBACK_SECONDS,
    KIND_DISCORD_LEFT,
    KIND_ROLE_LOST,
    ORG_GAME_ROLE_KEYS,
    VISITOR_ROLE_KEYS,
    AuditHint,
    DepartureCard,
    build_departure,
    dispatch_leftover_action,
    ensure_default_sources,
    load_policies,
    record_watch_event,
    removed_org_role_names,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.messages import followup_spec, followup_v2, respond_spec, respond_v2, send_v2
from ui.navigation import ConfirmDialog
from ui.templates import missing_permission, notice, system_error
from utils.discord_lookup import get_role, get_text_channel, request_ping_roles
from utils.locks import KeyedLockPool
from utils.views import SafeView

logger = logging.getLogger(__name__)
_WATCH_LOCKS: KeyedLockPool[tuple[int, int, str]] = KeyedLockPool()
_BUTTON_TEMPLATE = (
    r"rw:(?P<action>leave|reserve):(?P<uid>\d+)(?::(?P<extra>[A-Za-z0-9_-]+))?"
)


def _error_spec(exc: MembershipError) -> MessageSpec:
    if exc.decision is not None:
        return missing_permission(exc.decision, reference=current_reference())
    return system_error(
        code=exc.code,
        summary=str(exc),
        retryable=exc.retryable,
        reference=current_reference(),
    )


def _held_keys(member: discord.Member) -> set[str]:
    held: set[str] = set()
    owned = {role.id for role in member.roles}
    for key in ORG_GAME_ROLE_KEYS | VISITOR_ROLE_KEYS:
        role = get_role(member.guild, key)
        if role is not None and role.id in owned:
            held.add(key)
    return held


def _role_names(member: discord.Member) -> tuple[str, ...]:
    return tuple(
        role.name
        for role in member.roles
        if role != member.guild.default_role
    )


def _org_roles(guild: discord.Guild) -> dict[int, str]:
    found: dict[int, str] = {}
    for key in ORG_GAME_ROLE_KEYS:
        role = get_role(guild, key)
        if role is not None:
            found[role.id] = role.name
    return found


async def _ping_roles(guild: discord.Guild) -> list[discord.Role]:
    registry = get_registry()
    ids = await registry.get(guild.id, "ROSTER_WATCH_PING_ROLE_IDS")
    names = str(await registry.get(guild.id, "ROSTER_WATCH_PING_ROLES") or "")
    roles: list[discord.Role] = []
    if ids:
        for raw in ids:
            role = guild.get_role(int(raw))
            if role is not None:
                roles.append(role)
        if roles:
            return roles
    for name in (part.strip() for part in names.split(",")):
        if not name:
            continue
        role = discord.utils.get(guild.roles, name=name)
        if role is not None:
            roles.append(role)
    return roles or request_ping_roles(guild)


async def _audit_hint(
    guild: discord.Guild, member: discord.Member
) -> tuple[bool, AuditHint | None]:
    me = guild.me
    if me is None or not me.guild_permissions.view_audit_log:
        return False, None
    now = datetime.now(UTC)
    try:
        async for entry in guild.audit_logs(limit=6):
            if entry.action not in {
                discord.AuditLogAction.kick,
                discord.AuditLogAction.ban,
            }:
                continue
            target = entry.target
            if getattr(target, "id", None) != member.id:
                continue
            created = entry.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if (now - created).total_seconds() > AUDIT_LOOKBACK_SECONDS:
                continue
            action = (
                "kick" if entry.action == discord.AuditLogAction.kick else "ban"
            )
            actor = entry.user
            return True, AuditHint(
                action=action,
                moderator_id=getattr(actor, "id", None),
                reason=entry.reason,
            )
        return True, None
    except discord.Forbidden:
        return False, None


def _departure_view(card: DepartureCard) -> RosterDepartureView:
    return RosterDepartureView(card.discord_id, card.reserve_subjects)


class RosterDepartureView(SafeView):
    def __init__(self, discord_id: int, reserves: tuple[str, ...]) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Оформить выход",
                style=discord.ButtonStyle.danger,
                custom_id=f"rw:leave:{discord_id}",
            )
        )
        for subject in reserves[:3]:
            self.add_item(
                discord.ui.Button(
                    label="Поднять резерв",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"rw:reserve:{discord_id}:{subject}",
                )
            )


class RosterWatchButton(
    discord.ui.DynamicItem,
    template=_BUTTON_TEMPLATE,
):
    def __init__(
        self,
        item: discord.ui.Button,
        action: str,
        discord_id: int,
        extra: str,
    ) -> None:
        super().__init__(item)
        self.action = action
        self.discord_id = discord_id
        self.extra = extra

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> RosterWatchButton:
        del interaction
        return cls(
            item,
            match.group("action"),
            int(match.group("uid")),
            match.group("extra") or "",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action == "leave":
            await handle_leave_button(interaction, self.discord_id)
            return
        await handle_reserve_button(interaction, self.discord_id, self.extra)


async def _reply_spec(interaction: discord.Interaction, spec: MessageSpec) -> None:
    if interaction.response.is_done():
        await followup_spec(interaction, spec)
        return
    await respond_spec(interaction, spec)


async def handle_leave_button(
    interaction: discord.Interaction, discord_id: int
) -> None:
    decision = await evaluate_access(interaction.user, Permission.MEMBERS_MANAGE)
    if not decision.allowed:
        await _reply_spec(
            interaction,
            missing_permission(decision, reference=current_reference()),
        )
        return
    guild_id = interaction.guild_id
    if guild_id is None:
        await _reply_spec(
            interaction,
            system_error(
                code="ROSTER.GUILD_ONLY",
                summary="Выход оформляется только на сервере. Запись не изменена.",
                retryable=False,
                reference=current_reference(),
            ),
        )
        return

    async def _confirm(follow_up: discord.Interaction) -> None:
        try:
            result = await leave_member(
                follow_up.user,
                guild_id,
                discord_id,
                reason="оформление по карточке выхода из Discord",
            )
        except MembershipError as exc:
            await followup_spec(follow_up, _error_spec(exc))
            return
        leftovers = (
            ", ".join(item.summary for item in result.obligations)
            if result.obligations
            else "открытых обязательств нет"
        )
        await followup_spec(
            follow_up,
            notice(
                code="ROSTER.LEFT",
                title="Выход оформлен",
                summary=(
                    f"{result.member.ic_name} отмечен как вышедший. "
                    f"{leftovers}."
                ),
                next_step="Запись в реестре сохранена. Обязательства не удалены.",
                reference=str(result.member.id),
            ),
        )

    dialog = ConfirmDialog(
        domain="roster",
        object_id=str(discord_id),
        summary=(
            f"Оформить выход <@{discord_id}> через {cmd('members leave')}. "
            "Статус в игре сам не изменится."
        ),
        actor=str(interaction.user),
        on_confirm=_confirm,
        owner_id=interaction.user.id,
    )
    if interaction.response.is_done():
        await followup_v2(interaction, None, view=dialog, ephemeral=True)
        return
    await respond_v2(interaction, None, view=dialog, ephemeral=True)


async def handle_reserve_button(
    interaction: discord.Interaction, discord_id: int, subject_id: str
) -> None:
    del discord_id
    decision = await evaluate_access(interaction.user, Permission.MEMBERS_MANAGE)
    if not decision.allowed:
        await _reply_spec(
            interaction,
            missing_permission(decision, reference=current_reference()),
        )
        return
    guild_id = interaction.guild_id or 0
    summary = await dispatch_leftover_action(
        interaction.user,
        guild_id,
        action="promote_reserve",
        subject_id=subject_id,
    )
    await _reply_spec(
        interaction,
        notice(
            code="ROSTER.RESERVE",
            title="Резерв",
            summary=summary,
            next_step="Карточка состава не закрывает членство автоматически.",
        ),
    )


class RosterWatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        ensure_default_sources()
        self.bot.add_dynamic_items(RosterWatchButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RosterWatchButton)

    async def _publish(
        self, member: discord.Member, card: DepartureCard
    ) -> None:
        channel = get_text_channel(member.guild, "roster_watch")
        if channel is None:
            logger.warning(
                "Roster watch channel is missing; departure %s not posted",
                member.id,
            )
            return
        ping_roles = await _ping_roles(member.guild) if card.ping else []
        content = " ".join(role.mention for role in ping_roles) or None
        mentions = discord.AllowedMentions(
            roles=ping_roles, users=False, everyone=False
        )
        await send_v2(
            channel,
            content=content,
            embed=card.spec,
            view=_departure_view(card),
            allowed_mentions=mentions,
        )
        await record_watch_event(
            member.guild.id,
            kind=card.kind,
            discord_id=member.id,
            reason=card.reason,
        )

    async def _notify(
        self,
        member: discord.Member,
        *,
        kind: str,
        role_names: tuple[str, ...],
        held_keys: set[str],
        removed_role_names: tuple[str, ...] = (),
        can_read_audit: bool = False,
        audit: AuditHint | None = None,
    ) -> None:
        visitor_policy, bot_policy = await load_policies(member.guild.id)
        joined = member.joined_at.isoformat() if member.joined_at else None
        async with _WATCH_LOCKS.lock((member.guild.id, member.id, kind)):
            card = await build_departure(
                member.guild.id,
                discord_id=member.id,
                display_name=member.display_name,
                mention=member.mention,
                role_names=role_names,
                discord_joined_at=joined,
                is_bot=member.bot,
                held_keys=held_keys,
                kind=kind,
                can_read_audit=can_read_audit,
                audit=audit,
                visitor_policy=visitor_policy,
                bot_policy=bot_policy,
                removed_role_names=removed_role_names,
            )
            if card is None:
                return
            await self._publish(member, card)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        can_read, hint = await _audit_hint(member.guild, member)
        await self._notify(
            member,
            kind=KIND_DISCORD_LEFT,
            role_names=_role_names(member),
            held_keys=_held_keys(member),
            can_read_audit=can_read,
            audit=hint,
        )

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        removed = removed_org_role_names(
            before_ids={role.id for role in before.roles},
            after_ids={role.id for role in after.roles},
            org_roles=_org_roles(after.guild),
        )
        if not removed:
            return
        await self._notify(
            after,
            kind=KIND_ROLE_LOST,
            role_names=_role_names(before),
            held_keys=_held_keys(before),
            removed_role_names=removed,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RosterWatchCog(bot))
