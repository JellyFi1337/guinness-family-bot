from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SHOWDOWN_STATE_PATH
from ui.layouts import marker_component_id
from ui.messages import edit_message_v2, edit_original_response_v2, respond_v2, send_v2
from ui.templates import notice
from utils.embeds import WHITE_COLOR
from utils.access import is_bot_owner
from utils.discord_lookup import get_category


logger = logging.getLogger(__name__)

SHOWDOWN_CHANNEL = "private-showdown"
SHOWDOWN_ROLE = "Private Showdown"
STATE_SCHEMA_VERSION = 2
_STATE_LOCK = asyncio.Lock()


@dataclass(slots=True)
class ReconcileResult:
    processed_members: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failures

    def fail(self, value: str) -> None:
        if value not in self.failures:
            self.failures.append(value)


def _empty_state() -> dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "guilds": {}}


def _normalise_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("guilds", {}), dict):
        raise ValueError("Invalid showdown state structure")
    value.setdefault("schema_version", STATE_SCHEMA_VERSION)
    guilds = value.setdefault("guilds", {})
    for guild_state in guilds.values():
        if not isinstance(guild_state, dict):
            raise ValueError("Invalid showdown guild state")
        # State files created by older versions contained only created_at/members.
        # Their presence always meant that Showdown was active.
        guild_state.setdefault("active", True)
        guild_state.setdefault("phase", "active")
        guild_state.setdefault("members", {})
        guild_state.setdefault("member_overwrites", {})
    return value


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(SHOWDOWN_STATE_PATH.read_text(encoding="utf-8"))
        return _normalise_state(value)
    except FileNotFoundError:
        return _empty_state()
    except (OSError, json.JSONDecodeError, ValueError):
        logger.exception("Failed to read durable Showdown state")
        raise


def _write_state(state: dict[str, Any]) -> None:
    """Atomically replace the durable state and force its contents to disk."""
    SHOWDOWN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SHOWDOWN_STATE_PATH.with_name(f".{SHOWDOWN_STATE_PATH.name}.tmp")
    payload = json.dumps(
        state,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, SHOWDOWN_STATE_PATH)
        try:
            SHOWDOWN_STATE_PATH.chmod(0o600)
        except OSError:
            # ACL-based filesystems (notably Windows) may not implement chmod.
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove temporary Showdown state %s", temporary)


async def _state_transaction(
    callback: Callable[[dict[str, Any]], tuple[bool, Any]],
) -> Any:
    async with _STATE_LOCK:
        state = await asyncio.to_thread(_read_state)
        changed, result = callback(state)
        if changed:
            state["schema_version"] = STATE_SCHEMA_VERSION
            await asyncio.to_thread(_write_state, state)
        return result


async def _load_guild_state(guild_id: int) -> dict[str, Any] | None:
    def read(state: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        entry = state.get("guilds", {}).get(str(guild_id))
        # The returned structure is detached from the transaction so callers can
        # never mutate the durable snapshot without an atomic write.
        return False, json.loads(json.dumps(entry)) if entry is not None else None

    return await _state_transaction(read)


async def _create_guild_state(guild_id: int, entry: dict[str, Any]) -> bool:
    """Persist the first snapshot exactly once; an active snapshot is immutable."""

    def create(state: dict[str, Any]) -> tuple[bool, bool]:
        guilds = state.setdefault("guilds", {})
        if str(guild_id) in guilds:
            return False, False
        guilds[str(guild_id)] = entry
        return True, True

    return bool(await _state_transaction(create))


async def _patch_guild_state(guild_id: int, **values: Any) -> bool:
    """Patch operational metadata only; member snapshots are never replaced."""

    forbidden = {"members", "member_overwrites"}.intersection(values)
    if forbidden:
        raise ValueError(f"Snapshot fields cannot be patched: {sorted(forbidden)}")

    def patch(state: dict[str, Any]) -> tuple[bool, bool]:
        entry = state.get("guilds", {}).get(str(guild_id))
        if entry is None:
            return False, False
        changed = False
        for key, value in values.items():
            if entry.get(key) != value:
                entry[key] = value
                changed = True
        return changed, True

    return bool(await _state_transaction(patch))


async def _delete_guild_state(guild_id: int) -> None:
    def delete(state: dict[str, Any]) -> tuple[bool, None]:
        removed = state.get("guilds", {}).pop(str(guild_id), None)
        return removed is not None, None

    await _state_transaction(delete)


def _role_ids(member: discord.Member, bot_member: discord.Member) -> list[int]:
    return [
        role.id
        for role in member.roles
        if not role.is_default()
        and not role.managed
        and role < bot_member.top_role
        and role.name != SHOWDOWN_ROLE
    ]


def _serialise_overwrite(overwrite: discord.PermissionOverwrite) -> dict[str, int]:
    allow, deny = overwrite.pair()
    return {"allow": allow.value, "deny": deny.value}


def _deserialise_overwrite(value: dict[str, Any]) -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite.from_pair(
        discord.Permissions(int(value.get("allow", 0))),
        discord.Permissions(int(value.get("deny", 0))),
    )


def _visible_member_overwrites(guild: discord.Guild) -> dict[str, dict[str, dict[str, int]]]:
    result: dict[str, dict[str, dict[str, int]]] = {}
    for channel in guild.channels:
        channel_values: dict[str, dict[str, int]] = {}
        for target, overwrite in channel.overwrites.items():
            if (
                isinstance(target, discord.Member)
                and not target.bot
                and overwrite.view_channel is True
            ):
                channel_values[str(target.id)] = _serialise_overwrite(overwrite)
        if channel_values:
            result[str(channel.id)] = channel_values
    return result


def _new_snapshot(guild: discord.Guild, bot_member: discord.Member, actor_id: int) -> dict[str, Any]:
    return {
        "active": True,
        "phase": "enabling",
        "created_at": datetime.now(UTC).isoformat(),
        "requested_by": actor_id,
        "channel_id": None,
        "role_id": None,
        "notice_message_id": None,
        "members": {
            str(member.id): _role_ids(member, bot_member)
            for member in guild.members
            if not member.bot
        },
        "member_overwrites": _visible_member_overwrites(guild),
    }


async def _snapshot_member_if_missing(
    guild: discord.Guild,
    member: discord.Member,
    bot_member: discord.Member,
) -> bool:
    """Append a newly joined member before touching any of their Discord roles."""

    role_ids = _role_ids(member, bot_member)
    visible_overwrites: dict[str, dict[str, int]] = {}
    for channel in guild.channels:
        overwrite = channel.overwrites_for(member)
        if overwrite.view_channel is True:
            visible_overwrites[str(channel.id)] = _serialise_overwrite(overwrite)

    def append(state: dict[str, Any]) -> tuple[bool, bool]:
        entry = state.get("guilds", {}).get(str(guild.id))
        if entry is None or entry.get("phase") == "disabling":
            return False, False
        members = entry.setdefault("members", {})
        key = str(member.id)
        if key in members:
            return False, True
        members[key] = role_ids
        saved_overwrites = entry.setdefault("member_overwrites", {})
        for channel_id, overwrite in visible_overwrites.items():
            saved_overwrites.setdefault(channel_id, {})[key] = overwrite
        return True, True

    return bool(await _state_transaction(append))


async def _snapshot_overwrite_if_missing(
    guild_id: int,
    channel_id: int,
    member_id: int,
    overwrite: discord.PermissionOverwrite,
) -> None:
    serialised = _serialise_overwrite(overwrite)

    def append(state: dict[str, Any]) -> tuple[bool, None]:
        entry = state.get("guilds", {}).get(str(guild_id))
        if entry is None or entry.get("phase") == "disabling":
            return False, None
        members = entry.setdefault("member_overwrites", {}).setdefault(
            str(channel_id), {}
        )
        if str(member_id) in members:
            return False, None
        members[str(member_id)] = serialised
        return True, None

    await _state_transaction(append)


async def _can_toggle_showdown(interaction: discord.Interaction) -> bool:
    """Restrict Showdown to the configured bot owner, even after role isolation."""
    return interaction.guild is not None and is_bot_owner(interaction.user)


class ShowdownCog(
    commands.GroupCog,
    group_name="showdown",
    group_description="Режим закрытого тестирования",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._operation_locks: dict[int, asyncio.Lock] = {}
        self._periodic_reconcile.start()

    def cog_unload(self) -> None:
        self._periodic_reconcile.cancel()

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._operation_locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    async def _finish(interaction: discord.Interaction, content: str) -> None:
        try:
            await edit_original_response_v2(interaction, content=content)
        except discord.NotFound:
            # Deleting the command source channel invalidates its webhook.
            logger.warning("Deferred Showdown response no longer exists")

    @app_commands.command(
        name="toggle",
        description="Включить или выключить режим закрытого тестирования",
    )
    @app_commands.check(_can_toggle_showdown)
    @app_commands.checks.cooldown(1, 60.0, key=lambda interaction: interaction.guild_id)
    async def let_showdown(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await respond_v2(
                interaction,
                "Команда доступна только на сервере.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with self._lock(guild.id):
            try:
                state = await _load_guild_state(guild.id)
                # Durable state is authoritative. Channel/role presence is only
                # an enforcement detail and can never toggle the mode by itself.
                if state is not None:
                    await _patch_guild_state(guild.id, phase="disabling")
                    result = await self._reconcile_disabled(guild, interaction.user)
                    if result.complete:
                        await self._finish(
                            interaction, "Режим закрытого тестирования выключен."
                        )
                    else:
                        await self._finish(
                            interaction,
                            "Выключение ещё не завершено. Бот автоматически повторит операцию: "
                            + ", ".join(result.failures[:10]),
                        )
                    return

                await self._enable(interaction)
            except (OSError, json.JSONDecodeError, ValueError):
                logger.exception("Showdown command could not persist its state")
                await self._finish(
                    interaction,
                    "Не удалось записать состояние Showdown. Изменения не потеряны; "
                    "бот продолжит восстановление после устранения ошибки хранилища.",
                )

    async def _enable(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await self._finish(interaction, "Боту требуется право «Управлять ролями».")
            return
        if not bot_member.guild_permissions.manage_channels:
            await self._finish(interaction, "Боту требуется право «Управлять каналами».")
            return
        if not await self._ensure_member_cache(guild):
            await self._finish(
                interaction,
                "Не удалось получить полный список участников. Режим не включён, чтобы не потерять роли.",
            )
            return

        entry = _new_snapshot(guild, bot_member, interaction.user.id)
        try:
            created = await _create_guild_state(guild.id, entry)
        except (OSError, json.JSONDecodeError, ValueError):
            logger.exception("Failed to persist initial Showdown snapshot")
            await self._finish(
                interaction,
                "Не удалось сохранить снимок ролей. Режим не включён, чтобы не потерять данные.",
            )
            return
        if not created:
            await self._finish(
                interaction,
                "Showdown уже активен; состояние не было перезаписано.",
            )
            return

        result = await self._reconcile_enabled(guild, entry, interaction.user)
        if result.complete:
            await self._finish(
                interaction,
                f"Режим закрытого тестирования включён. Обработано пользователей: **{result.processed_members}**.",
            )
        else:
            await self._finish(
                interaction,
                "Showdown сохранён, но ограничения применены не полностью. "
                "Бот продолжит безопасные повторные попытки: "
                + ", ".join(result.failures[:10]),
            )

    async def _ensure_member_cache(self, guild: discord.Guild) -> bool:
        if getattr(guild, "chunked", True):
            return True
        if not getattr(self.bot.intents, "members", False):
            return False
        try:
            await guild.chunk(cache=True)
            return bool(guild.chunked)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to chunk guild %s before Showdown", guild.id)
            return False

    async def _resolve_role(
        self,
        guild: discord.Guild,
        state: dict[str, Any],
        actor: object,
    ) -> discord.Role:
        role_id = state.get("role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None and not role_id:
            role = discord.utils.get(guild.roles, name=SHOWDOWN_ROLE)
        if role is None:
            role = await guild.create_role(
                name=SHOWDOWN_ROLE,
                permissions=discord.Permissions.none(),
                reason=f"Private Showdown reconciliation ({actor})",
            )
        if role.id != role_id:
            await _patch_guild_state(guild.id, role_id=role.id)
        return role

    async def _resolve_channel(
        self,
        guild: discord.Guild,
        state: dict[str, Any],
        role: discord.Role,
        bot_member: discord.Member,
        actor: object,
    ) -> discord.TextChannel:
        channel_id = state.get("channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel) and not channel_id:
            channel = discord.utils.get(guild.text_channels, name=SHOWDOWN_CHANNEL)
        category = get_category(guild, "developer_category")
        if category is None:
            raise RuntimeError(
                "Developer category is not configured or does not exist"
            )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=False,
                use_application_commands=True,
                send_messages_in_threads=False,
                create_public_threads=False,
                create_private_threads=False,
                add_reactions=False,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=True,
                manage_messages=True,
            ),
        }
        if channel is None:
            channel = await guild.create_text_channel(
                SHOWDOWN_CHANNEL,
                category=category,
                overwrites=overwrites,
                reason=f"Private Showdown reconciliation ({actor})",
            )
        else:
            if channel.category_id != category.id:
                await channel.edit(
                    category=category,
                    reason=f"Private Showdown reconciliation ({actor})",
                )
            for target, overwrite in overwrites.items():
                current = channel.overwrites_for(target)
                if current != overwrite:
                    await channel.set_permissions(
                        target,
                        overwrite=overwrite,
                        reason=f"Private Showdown reconciliation ({actor})",
                    )
        if channel.id != channel_id:
            await _patch_guild_state(guild.id, channel_id=channel.id)
        return channel

    async def _enforce_member(
        self,
        member: discord.Member,
        role: discord.Role,
        bot_member: discord.Member,
        actor: object,
        result: ReconcileResult,
    ) -> None:
        unmanageable = [
            current
            for current in member.roles
            if not current.is_default()
            and not current.managed
            and current != role
            and current >= bot_member.top_role
        ]
        if unmanageable:
            result.fail(f"роли выше роли бота у {member.display_name}")
            return
        removable = [
            current
            for current in member.roles
            if not current.is_default()
            and not current.managed
            and current != role
            and current < bot_member.top_role
        ]
        try:
            if removable:
                await member.remove_roles(
                    *removable,
                    reason=f"Private Showdown reconciliation ({actor})",
                )
            if role not in member.roles:
                await member.add_roles(
                    role,
                    reason=f"Private Showdown reconciliation ({actor})",
                )
            result.processed_members += 1
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to enforce Showdown for member %s", member.id)
            result.fail(member.display_name)

    async def _ensure_notice(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        state: dict[str, Any],
    ) -> None:
        marker = f"showdown:notice:{guild.id}"
        expected_marker = marker_component_id(marker)

        def has_marker(candidate: discord.Message) -> bool:
            def visit(component: object) -> bool:
                return getattr(component, "id", None) == expected_marker or any(
                    visit(child) for child in getattr(component, "children", ())
                )

            return any(visit(component) for component in candidate.components)

        message = None
        message_id = state.get("notice_message_id")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            async for candidate in channel.history(limit=100):
                if candidate.author == guild.me and has_marker(candidate):
                    message = candidate
                    break
        embed = notice(
            code="SHOWDOWN.CLOSED",
            title="Сервер закрыт",
            summary="Сервер временно закрыт на закрытое тестирование.",
            next_step="Дождитесь открытия сервера.",
            accent=WHITE_COLOR,
        )
        if message is None:
            message = await send_v2(
                channel,
                embed=embed,
                component_marker=marker,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif not message.flags.components_v2 or not has_marker(message):
            message = await edit_message_v2(
                message,
                embed=embed,
                component_marker=marker,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if message.id != message_id:
            await _patch_guild_state(guild.id, notice_message_id=message.id)

    async def _reconcile_enabled(
        self,
        guild: discord.Guild,
        state: dict[str, Any],
        actor: object,
    ) -> ReconcileResult:
        result = ReconcileResult()
        bot_member = guild.me
        if bot_member is None:
            result.fail("бот не найден среди участников")
            return result
        if not bot_member.guild_permissions.manage_roles:
            result.fail("нет права «Управлять ролями»")
        if not bot_member.guild_permissions.manage_channels:
            result.fail("нет права «Управлять каналами»")
        if result.failures:
            return result

        try:
            role = await self._resolve_role(guild, state, actor)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to resolve Showdown role in guild %s", guild.id)
            result.fail("роль Private Showdown")
            return result
        if role >= bot_member.top_role:
            result.fail("роль Private Showdown находится выше роли бота")
            return result
        try:
            channel = await self._resolve_channel(guild, state, role, bot_member, actor)
        except (discord.Forbidden, discord.HTTPException, RuntimeError):
            logger.exception("Failed to resolve Showdown channel in guild %s", guild.id)
            result.fail("категория DEVELOPER или #private-showdown")
            return result

        for member in guild.members:
            if member.bot:
                continue
            await _snapshot_member_if_missing(guild, member, bot_member)
            await self._enforce_member(member, role, bot_member, actor, result)

        # The role deny covers normal access. Explicit member allows have higher
        # precedence in Discord, therefore they are preserved and temporarily
        # changed to a deny as well.
        for target in guild.channels:
            if target.id == channel.id:
                continue
            try:
                role_overwrite = target.overwrites_for(role)
                if role_overwrite.view_channel is not False:
                    await target.set_permissions(
                        role,
                        view_channel=False,
                        reason=f"Private Showdown reconciliation ({actor})",
                    )
                for overwrite_target, overwrite in list(target.overwrites.items()):
                    if (
                        isinstance(overwrite_target, discord.Member)
                        and not overwrite_target.bot
                        and overwrite.view_channel is True
                    ):
                        await _snapshot_overwrite_if_missing(
                            guild.id,
                            target.id,
                            overwrite_target.id,
                            overwrite,
                        )
                        overwrite.view_channel = False
                        await target.set_permissions(
                            overwrite_target,
                            overwrite=overwrite,
                            reason=f"Private Showdown reconciliation ({actor})",
                        )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Failed to restrict channel %s", target.id)
                result.fail(f"#{target.name}")

        try:
            await self._ensure_notice(guild, channel, state)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to publish Showdown notice in guild %s", guild.id)
            result.fail("сообщение #private-showdown")

        if result.complete:
            await _patch_guild_state(guild.id, phase="active", active=True)
        return result

    async def _restore_roles(
        self,
        guild: discord.Guild,
        state: dict[str, Any],
        bot_member: discord.Member,
        actor: object,
        result: ReconcileResult,
    ) -> None:
        for member_id, role_ids in state.get("members", {}).items():
            member = guild.get_member(int(member_id))
            if member is None or member.bot:
                continue
            roles: list[discord.Role] = []
            for role_id in role_ids:
                target_role = guild.get_role(int(role_id))
                if target_role is None or target_role.managed or target_role in member.roles:
                    continue
                if target_role >= bot_member.top_role:
                    result.fail(f"роль {target_role.name} для {member.display_name}")
                    continue
                roles.append(target_role)
            if roles:
                try:
                    await member.add_roles(
                        *roles,
                        reason=f"Private Showdown disabled ({actor})",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Failed to restore roles for member %s", member.id)
                    result.fail(f"роли {member.display_name}")

    async def _restore_member_overwrites(
        self,
        guild: discord.Guild,
        state: dict[str, Any],
        actor: object,
        result: ReconcileResult,
    ) -> None:
        for channel_id, members in state.get("member_overwrites", {}).items():
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                continue
            for member_id, overwrite_value in members.items():
                member = guild.get_member(int(member_id))
                if member is None:
                    continue
                try:
                    await channel.set_permissions(
                        member,
                        overwrite=_deserialise_overwrite(overwrite_value),
                        reason=f"Private Showdown disabled ({actor})",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Failed to restore member overwrite in channel %s", channel.id
                    )
                    result.fail(f"доступ {member.display_name} в #{channel.name}")

    async def _reconcile_disabled(
        self,
        guild: discord.Guild,
        actor: object,
    ) -> ReconcileResult:
        result = ReconcileResult()
        state = await _load_guild_state(guild.id)
        if state is None:
            return result
        bot_member = guild.me
        if bot_member is None:
            result.fail("бот не найден среди участников")
            return result

        await self._restore_roles(guild, state, bot_member, actor, result)
        await self._restore_member_overwrites(guild, state, actor, result)
        # Do not start removing the isolation layer until all restorations have
        # succeeded. This keeps every retry safe and deterministic.
        if not result.complete:
            return result

        role_id = state.get("role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None and not role_id:
            role = discord.utils.get(guild.roles, name=SHOWDOWN_ROLE)
        channel_id = state.get("channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel) and not channel_id:
            channel = discord.utils.get(guild.text_channels, name=SHOWDOWN_CHANNEL)

        if role is not None:
            for target in guild.channels:
                if channel is not None and target.id == channel.id:
                    continue
                try:
                    if role in target.overwrites:
                        await target.set_permissions(
                            role,
                            overwrite=None,
                            reason=f"Private Showdown disabled ({actor})",
                        )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Failed to clear Showdown overwrite in %s", target.id)
                    result.fail(f"#{target.name}")
            if not result.complete:
                return result

            for member in list(role.members):
                if member.bot:
                    continue
                try:
                    await member.remove_roles(
                        role,
                        reason=f"Private Showdown disabled ({actor})",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Failed to remove Showdown role from %s", member.id)
                    result.fail(member.display_name)
            if not result.complete:
                return result

            try:
                await role.delete(reason=f"Private Showdown disabled ({actor})")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Failed to delete Showdown role in guild %s", guild.id)
                result.fail("роль Private Showdown")
                return result

        # The channel and snapshot are deliberately last. If either operation
        # fails, phase=disabling remains durable and the periodic task retries it.
        if channel is not None:
            try:
                await channel.delete(reason=f"Private Showdown disabled ({actor})")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Failed to delete Showdown channel in guild %s", guild.id)
                result.fail("#private-showdown")
                return result
        try:
            await _delete_guild_state(guild.id)
        except (OSError, json.JSONDecodeError, ValueError):
            logger.exception("Failed to remove completed Showdown state")
            result.fail("файл состояния")
        return result

    async def _reconcile_guild(self, guild: discord.Guild, actor: object) -> None:
        async with self._lock(guild.id):
            try:
                state = await _load_guild_state(guild.id)
                if state is None:
                    return
                if state.get("phase") == "disabling":
                    result = await self._reconcile_disabled(guild, actor)
                else:
                    await self._ensure_member_cache(guild)
                    result = await self._reconcile_enabled(guild, state, actor)
                if not result.complete:
                    logger.warning(
                        "Showdown reconciliation incomplete for guild %s: %s",
                        guild.id,
                        ", ".join(result.failures[:10]),
                    )
            except (OSError, json.JSONDecodeError, ValueError):
                logger.exception("Showdown state reconciliation failed for guild %s", guild.id)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Showdown Discord reconciliation failed for guild %s", guild.id)
            except Exception:
                # A single malformed Discord object or unexpected API response
                # must not terminate the periodic recovery loop for all guilds.
                logger.exception("Unexpected Showdown reconciliation failure for guild %s", guild.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._reconcile_guild(guild, "startup reconciliation")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        async with self._lock(member.guild.id):
            state = await _load_guild_state(member.guild.id)
            if state is None or state.get("phase") == "disabling":
                return
            bot_member = member.guild.me
            if bot_member is None:
                return
            # Persistence must complete before the first role is removed.
            if not await _snapshot_member_if_missing(member.guild, member, bot_member):
                return
            result = await self._reconcile_enabled(
                member.guild,
                await _load_guild_state(member.guild.id) or state,
                "member joined",
            )
            if not result.complete:
                logger.warning("Failed to isolate new member %s: %s", member.id, result.failures)

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        if after.bot or before.roles == after.roles:
            return
        async with self._lock(after.guild.id):
            state = await _load_guild_state(after.guild.id)
            if state is None or state.get("phase") == "disabling":
                return
            role_id = state.get("role_id")
            role = after.guild.get_role(int(role_id)) if role_id else None
            bot_member = after.guild.me
            if role is None or bot_member is None:
                return
            await _snapshot_member_if_missing(after.guild, after, bot_member)
            result = ReconcileResult()
            await self._enforce_member(after, role, bot_member, "member update", result)

    @tasks.loop(seconds=60.0, reconnect=True)
    async def _periodic_reconcile(self) -> None:
        for guild in self.bot.guilds:
            await self._reconcile_guild(guild, "periodic reconciliation")

    @_periodic_reconcile.before_loop
    async def _before_periodic_reconcile(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ShowdownCog(bot))
