import logging
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from ui.layouts import marker_component_id
from ui.messages import edit_message_v2, followup_v2, respond_v2, send_v2
from ui.templates import notice

from utils.discord_lookup import get_voice_channel
from utils.embeds import WHITE_COLOR
from utils.locks import KeyedLockPool
from utils.views import SafeView, stop_registered_view
from utils.access import is_bot_owner
from utils.database import (
    get_voice_rooms_snapshot,
    add_voice_owner_ban,
    create_voice_room,
    complete_voice_room_transfer,
    delete_voice_room,
    get_all_voice_rooms,
    get_voice_room,
    get_voice_room_by_owner,
    get_voice_owner_bans,
    get_pending_voice_room_transfers,
    has_active_vip,
    init_db,
    remove_voice_owner_ban,
    set_voice_room_message,
    set_voice_room_owner,
    transfer_voice_room_owner,
)


logger = logging.getLogger(__name__)
_VOICE_CONTROL_LOCKS = KeyedLockPool[int]()
# Bounded fan-out for the periodic reconciler: one slow channel must not hold
# up the pass, and the whole pass must stay inside Discord's rate limits.
_RECONCILE_CONCURRENCY = 5


def _voice_control_lock(channel_id: int):
    return _VOICE_CONTROL_LOCKS.lock(channel_id)


def room_embed(owner_id: int, banned_ids=(), *, vip_access: bool = False):
    mentions = [f"<@{user_id}>" for user_id in banned_ids] if vip_access else []
    if len(mentions) > 30:
        mentions = mentions[:30] + [f"…и ещё {len(banned_ids) - 30}"]
    ban_value = (
        ("\n".join(mentions) if mentions else "Пока никого.")
        if vip_access
        else "Доступен владельцам активного VIP."
    )
    return notice(
        code="VOICE.PANEL",
        title="🎙️ Управление голосовым каналом",
        summary=(
            f"Владелец: <@{owner_id}>\n\n"
            "Кнопки ниже открывают/закрывают вход и скрывают/показывают канал.\n"
            "Команды владельца: `/voice limit`, `/voice kick`, `/voice ban`, "
            "`/voice unban`, `/voice access`, `/voice name`, `/voice transfer`, "
            "`/voice delete`."
        ),
        details=(
            (
                f"VIP ban-лист ({len(banned_ids) if vip_access else 0})",
                ban_value,
            ),
        ),
        next_step="Используйте кнопки ниже или команды владельца.",
        accent=WHITE_COLOR,
        reference=str(owner_id),
    )


class VoiceRoomView(SafeView):
    def __init__(self, channel_id: int, *, closed: bool = False, hidden: bool = False):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.connect_button = self._button(
            "Открыть" if closed else "Закрыть",
            discord.ButtonStyle.success if closed else discord.ButtonStyle.danger,
            "toggle_connect",
            self.toggle_connect,
        )
        self.visibility_button = self._button(
            "Показать" if hidden else "Скрыть",
            discord.ButtonStyle.primary if hidden else discord.ButtonStyle.secondary,
            "toggle_visibility",
            self.toggle_visibility,
        )

    def _button(self, label, style, suffix, callback):
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"voice_room:{self.channel_id}:{suffix}",
        )
        button.callback = callback
        self.add_item(button)
        return button

    async def _room_and_owner(self, interaction: discord.Interaction):
        """Resolve the room, acknowledging the interaction before the lookup.

        Callers may or may not have deferred already, so the reply path adapts
        instead of assuming: responding twice raises, and responding late after
        a slow query shows the user "Приложение не отвечает".
        """

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        room = await get_voice_room(self.channel_id)
        channel = interaction.guild.get_channel(self.channel_id) if interaction.guild else None
        if not room or not isinstance(channel, discord.VoiceChannel):
            await followup_v2(
                interaction, "Голосовой канал больше не существует.", ephemeral=True
            )
            return None, None
        if not _can_manage(interaction, room):
            await followup_v2(
                interaction,
                "Только владелец комнаты может управлять каналом.",
                ephemeral=True,
            )
            return None, None
        return room, channel

    async def toggle_connect(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        room, channel = await self._room_and_owner(interaction)
        if channel is None:
            return
        async with _voice_control_lock(channel.id):
            default = interaction.guild.default_role
            overwrite = channel.overwrites_for(default)
            is_closed = overwrite.connect is False
            overwrite.connect = True if is_closed else False
            await channel.set_permissions(
                default, overwrite=overwrite, reason="Voice room access toggle"
            )
            self.connect_button.label = "Закрыть" if is_closed else "Открыть"
            self.connect_button.style = (
                discord.ButtonStyle.danger if is_closed else discord.ButtonStyle.success
            )
            await edit_message_v2(interaction.message, view=self)
        await followup_v2(interaction,
            "Канал открыт." if is_closed else "Канал закрыт.", ephemeral=True
        )

    async def toggle_visibility(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        room, channel = await self._room_and_owner(interaction)
        if channel is None:
            return
        async with _voice_control_lock(channel.id):
            default = interaction.guild.default_role
            overwrite = channel.overwrites_for(default)
            is_hidden = overwrite.view_channel is False
            overwrite.view_channel = True if is_hidden else False
            await channel.set_permissions(
                default, overwrite=overwrite, reason="Voice room visibility toggle"
            )
            self.visibility_button.label = "Скрыть" if is_hidden else "Показать"
            self.visibility_button.style = (
                discord.ButtonStyle.secondary if is_hidden else discord.ButtonStyle.primary
            )
            await edit_message_v2(interaction.message, view=self)
        await followup_v2(interaction,
            "Канал показан." if is_hidden else "Канал скрыт.", ephemeral=True
        )


def _can_manage(interaction: discord.Interaction, room) -> bool:
    user = interaction.user
    return isinstance(user, discord.Member) and (
        user.id == room[1] or is_bot_owner(user)
    )


async def _apply_voice_owner_permissions(
    channel: discord.VoiceChannel,
    old_owner: discord.Member,
    new_owner: discord.Member,
    old_bans: set[int],
    new_bans: set[int],
) -> None:
    await channel.set_permissions(
        old_owner, overwrite=None, reason="Voice room ownership transferred"
    )
    await channel.set_permissions(
        new_owner,
        view_channel=True,
        connect=True,
        speak=True,
        manage_channels=True,
        move_members=True,
        manage_messages=True,
        reason="Voice room ownership transferred",
    )
    for user_id in old_bans - new_bans:
        member = channel.guild.get_member(user_id)
        if member is not None and member.id != new_owner.id:
            await channel.set_permissions(member, overwrite=None)
    for user_id in new_bans:
        member = channel.guild.get_member(user_id)
        if member is not None and member.id != new_owner.id:
            await channel.set_permissions(member, view_channel=False, connect=False)
            if member.voice and member.voice.channel == channel:
                await member.move_to(None, reason="New voice owner ban list applied")


class VoiceRoomsCog(
    commands.GroupCog,
    group_name="voice",
    group_description="Управление временной голосовой комнатой",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._creation_locks = KeyedLockPool[int]()
        self._reconcile_lock = asyncio.Lock()
        self._registered_views: dict[int, tuple[int, VoiceRoomView]] = {}
        # Last rendered state per panel. An unchanged digest skips the edit.
        self._panel_digests: dict[int, tuple] = {}

    async def cog_load(self):
        await init_db()
        # Message-bound views can be registered before the gateway is ready,
        # but channel existence must only be reconciled after the cache is ready.
        for channel_id, _owner_id, message_id in await get_all_voice_rooms():
            if message_id:
                self._register_control_view(channel_id, message_id, VoiceRoomView(channel_id))
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                "voice-room-reconcile",
                self._reconcile_rooms,
                interval=180,
                timeout=120,
            )

    async def cog_unload(self) -> None:
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("voice-room-reconcile")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._reconcile_rooms()

    def _register_control_view(
        self, channel_id: int, message_id: int, view: VoiceRoomView
    ) -> None:
        previous = self._registered_views.get(channel_id)
        if previous is not None:
            previous_message_id, previous_view = previous
            if previous_message_id == message_id:
                return
            stop_registered_view(previous_view)
        self.bot.add_view(view, message_id=message_id)
        self._registered_views[channel_id] = (message_id, view)

    def _remove_control_view(self, channel_id: int) -> None:
        previous = self._registered_views.pop(channel_id, None)
        if previous is not None:
            _message_id, view = previous
            stop_registered_view(view)

    @staticmethod
    def _has_control_marker(message: discord.Message, channel_id: int) -> bool:
        expected = marker_component_id(f"voice-room:{channel_id}")

        def visit(component) -> bool:
            return getattr(component, "id", None) == expected or any(
                visit(child) for child in getattr(component, "children", ())
            )

        return any(visit(component) for component in message.components)

    async def _ensure_control_message(
        self,
        channel: discord.VoiceChannel,
        owner_id: int,
        message_id: int | None,
    ) -> discord.Message:
        banned_ids = await get_voice_owner_bans(owner_id)
        vip_access = await has_active_vip(owner_id)
        overwrite = channel.overwrites_for(channel.guild.default_role)
        view = VoiceRoomView(
            channel.id,
            closed=overwrite.connect is False,
            hidden=overwrite.view_channel is False,
        )
        message = None
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                message = None

        if message is None:
            async for candidate in channel.history(limit=50):
                if (
                    candidate.author == channel.guild.me
                    and self._has_control_marker(candidate, channel.id)
                ):
                    message = candidate
                    break

        created = False
        if message is None:
            message = await send_v2(
                channel,
                embed=room_embed(owner_id, banned_ids, vip_access=vip_access),
                view=view,
                component_marker=f"voice-room:{channel.id}",
            )
            created = True
        else:
            await edit_message_v2(
                message,
                embed=room_embed(owner_id, banned_ids, vip_access=vip_access),
                view=view,
            )

        if message.id != message_id:
            try:
                await set_voice_room_message(channel.id, message.id)
            except Exception:
                # Avoid leaving an untracked duplicate when persistence fails.
                if created:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        logger.exception(
                            "Failed to remove orphan voice control message %s",
                            message.id,
                        )
                raise
        self._register_control_view(channel.id, message.id, view)
        return message

    @staticmethod
    def _panel_digest(channel, owner_id: int, message_id: int | None) -> tuple:
        """What the control panel actually depends on.

        Only a change in one of these makes the rendered panel different, so an
        unchanged digest means the edit would be a no-op on Discord's side.
        """

        overwrite = channel.overwrites_for(channel.guild.default_role)
        return (
            channel.name,
            channel.user_limit,
            owner_id,
            message_id,
            overwrite.connect,
            overwrite.view_channel,
        )

    async def _reconcile_rooms(self) -> None:
        if not self.bot.is_ready():
            return
        async with self._reconcile_lock:
            for channel_id, previous_owner_id, owner_id in (
                await get_pending_voice_room_transfers()
            ):
                channel = self.bot.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    continue
                old_owner = (
                    channel.guild.get_member(previous_owner_id)
                    if previous_owner_id is not None
                    else None
                )
                new_owner = channel.guild.get_member(owner_id)
                if new_owner is None:
                    try:
                        new_owner = await channel.guild.fetch_member(owner_id)
                    except discord.NotFound:
                        new_owner = None
                if new_owner is None:
                    if old_owner is not None:
                        await channel.set_permissions(
                            old_owner,
                            view_channel=True,
                            connect=True,
                            speak=True,
                            manage_channels=True,
                            move_members=True,
                            manage_messages=True,
                            reason="Voice ownership transfer rollback",
                        )
                        await set_voice_room_owner(channel_id, old_owner.id)
                    continue
                if old_owner is not None:
                    await _apply_voice_owner_permissions(
                        channel,
                        old_owner,
                        new_owner,
                        set(await get_voice_owner_bans(old_owner.id)),
                        set(await get_voice_owner_bans(new_owner.id))
                        if await has_active_vip(new_owner.id)
                        else set(),
                    )
                else:
                    await channel.set_permissions(
                        new_owner,
                        view_channel=True,
                        connect=True,
                        speak=True,
                        manage_channels=True,
                        move_members=True,
                        manage_messages=True,
                        reason="Voice ownership transfer reconciliation",
                    )
                await complete_voice_room_transfer(channel_id, new_owner.id)
            # One batched read for every room instead of two queries and a
            # write per room.
            snapshot = await get_voice_rooms_snapshot()
            live_ids: set[int] = set()
            pending: list[tuple] = []
            for channel_id, owner_id, message_id, owner_has_vip, banned_ids in snapshot:
                channel = self.bot.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    await delete_voice_room(channel_id)
                    self._remove_control_view(channel_id)
                    continue
                live_ids.add(channel_id)
                pending.append(
                    (channel, owner_id, message_id, owner_has_vip, banned_ids)
                )

            # Bounded concurrency: enough to stop one slow channel from holding
            # up the pass, low enough to stay well inside Discord's rate limits.
            semaphore = asyncio.Semaphore(_RECONCILE_CONCURRENCY)

            async def reconcile_one(
                channel, owner_id, message_id, owner_has_vip, banned_ids
            ) -> None:
                async with semaphore:
                    if owner_has_vip:
                        for banned_id in banned_ids:
                            member = channel.guild.get_member(banned_id)
                            if member is None or member.id == owner_id:
                                continue
                            try:
                                await channel.set_permissions(
                                    member, view_channel=False, connect=False
                                )
                            except discord.HTTPException:
                                logger.exception(
                                    "Failed to restore voice ban %s in %s",
                                    banned_id,
                                    channel.id,
                                )
                    digest = self._panel_digest(channel, owner_id, message_id)
                    if self._panel_digests.get(channel.id) == digest:
                        # Nothing a user can see has changed since the last
                        # pass; editing the panel anyway was a Discord request
                        # per room per three minutes for no visible effect.
                        return
                    try:
                        await self._ensure_control_message(
                            channel, owner_id, message_id
                        )
                    except discord.HTTPException:
                        logger.exception(
                            "Failed to reconcile voice control panel in %s", channel.id
                        )
                        self._panel_digests.pop(channel.id, None)
                        return
                    self._panel_digests[channel.id] = digest

            await asyncio.gather(*(reconcile_one(*item) for item in pending))
            for stale_id in set(self._registered_views) - live_ids:
                self._remove_control_view(stale_id)
                self._panel_digests.pop(stale_id, None)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        supervisor = getattr(self.bot, "task_supervisor", None)
        if member.bot or (supervisor is not None and supervisor.paused):
            return
        create_lobby = get_voice_channel(member.guild, "voice_create_lobby")
        if (
            after.channel
            and create_lobby is not None
            and after.channel.id == create_lobby.id
            and (before.channel is None or before.channel.id != after.channel.id)
        ):
            category = after.channel.category
            if category is None:
                logger.warning(
                    "Voice create lobby %s has no category", after.channel.id
                )
                return
            async with self._creation_locks.lock(member.id):
                await self._handle_lobby_join(member, category)

        if before.channel and isinstance(before.channel, discord.VoiceChannel):
            room = await get_voice_room(before.channel.id)
            if room:
                await self._cleanup_room(before.channel, room)

    async def _handle_lobby_join(
        self, member: discord.Member, category: discord.CategoryChannel
    ) -> None:
        existing = await get_voice_room_by_owner(member.id)
        if existing:
            existing_channel = member.guild.get_channel(existing[0])
            if isinstance(existing_channel, discord.VoiceChannel):
                try:
                    await member.move_to(
                        existing_channel,
                        reason="Member already owns a temporary voice room",
                    )
                    await send_v2(member,
                        f"У вас уже есть голосовая комната **{existing_channel.name}**. "
                        "Вы были перемещены в неё."
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Failed to return %s to existing voice room %s",
                        member.id,
                        existing_channel.id,
                    )
                return
            await delete_voice_room(existing[0])
        try:
            await self._create_room(member, category)
        except Exception:
            logger.exception(
                "Failed to create temporary voice room for %s in category %s",
                member.id,
                category.id,
            )

    async def _create_room(self, member: discord.Member, category: discord.CategoryChannel):
        banned_ids = await get_voice_owner_bans(member.id)
        vip_access = await has_active_vip(member.id)
        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(
                view_channel=True, connect=True
            ),
            member: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                manage_channels=True,
                move_members=True,
                manage_messages=True,
            ),
        }
        for banned_id in banned_ids if vip_access else ():
            banned_member = member.guild.get_member(banned_id)
            if banned_member is not None:
                overwrites[banned_member] = discord.PermissionOverwrite(
                    view_channel=False, connect=False
                )
        channel = await member.guild.create_voice_channel(
            name=f"Комната {member.display_name}"[:100],
            category=category,
            overwrites=overwrites,
            reason=f"Temporary room for {member}",
        )
        try:
            registered = await create_voice_room(
                channel.id,
                member.id,
                creator_id=member.id,
            )
        except Exception:
            try:
                await channel.delete(reason="Voice room persistence rollback")
            except discord.HTTPException:
                logger.exception(
                    "Failed to delete untracked voice room %s", channel.id
                )
            raise
        if not registered:
            await channel.delete(reason="Duplicate temporary voice room prevented")
            return
        try:
            await member.move_to(channel, reason="Move to temporary voice room")
        except discord.HTTPException:
            try:
                await channel.delete(reason="Voice room creation rollback")
            except discord.HTTPException:
                logger.exception(
                    "Failed to delete voice room %s after move failure", channel.id
                )
            else:
                await delete_voice_room(channel.id)
            raise
        try:
            await self._ensure_control_message(channel, member.id, None)
        except Exception:
            # The room remains usable and is repaired by the periodic reconciler.
            logger.exception("Failed to post control message for voice room %s", channel.id)

    async def _cleanup_room(self, channel: discord.VoiceChannel, room):
        if not channel.members:
            try:
                await channel.delete(reason="Temporary voice room is empty")
            except discord.HTTPException:
                logger.exception("Failed to delete empty voice room %s", channel.id)
                return
            await delete_voice_room(channel.id)
            return

    async def _managed_room(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            await respond_v2(interaction, "Команда доступна только участникам сервера.", ephemeral=True)
            return None, None
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            channel = interaction.user.voice.channel if interaction.user.voice else None
        if not isinstance(channel, discord.VoiceChannel):
            await respond_v2(interaction,
                "Используйте команду в чате своей комнаты или находясь в ней.",
                ephemeral=True,
            )
            return None, None
        room = await get_voice_room(channel.id)
        if not room:
            await respond_v2(interaction, "Это не временная голосовая комната.", ephemeral=True)
            return None, None
        if not _can_manage(interaction, room):
            await respond_v2(interaction, "Нет прав на управление этой комнатой.", ephemeral=True)
            return None, None
        return room, channel

    async def _refresh_control_message(self, channel: discord.VoiceChannel, room):
        try:
            await self._ensure_control_message(channel, room[1], room[2])
        except (discord.HTTPException, RuntimeError):
            logger.exception("Failed to refresh voice control message %s", channel.id)

    @app_commands.command(name="limit", description="Установить лимит участников")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def set_voice_limit(self, interaction: discord.Interaction, лимит: app_commands.Range[int, 0, 99]):
        await interaction.response.defer(ephemeral=True)
        room, channel = await self._managed_room(interaction)
        if channel is None:
            return
        try:
            updated = await channel.edit(
                user_limit=лимит, reason=f"Voice room limit changed by {interaction.user}"
            )
            if updated.user_limit != лимит:
                fetched = await interaction.guild.fetch_channel(channel.id)
                if not isinstance(fetched, discord.VoiceChannel):
                    raise RuntimeError("Голосовой канал не найден")
                if fetched.user_limit != лимит:
                    updated = await fetched.edit(
                        user_limit=лимит,
                        reason=f"Voice room limit retry by {interaction.user}",
                    )
            await followup_v2(interaction,
                "Лимит снят." if лимит == 0 else f"Лимит сразу установлен: {лимит}.",
                ephemeral=True,
            )
        except (RuntimeError, discord.HTTPException):
            logger.exception("Failed to set voice limit for channel %s", channel.id)
            await followup_v2(interaction,
                "Не удалось установить лимит.", ephemeral=True
            )

    @app_commands.command(name="kick", description="Отключить участника")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def voice_kick(self, interaction: discord.Interaction, участник: discord.Member):
        await interaction.response.defer(ephemeral=True)
        _, channel = await self._managed_room(interaction)
        if channel is None:
            return
        if участник.id == interaction.user.id or участник.voice is None or участник.voice.channel != channel:
            await respond_v2(interaction, "Участник должен находиться в вашей комнате.", ephemeral=True)
            return
        await участник.move_to(None, reason=f"Kicked from voice room by {interaction.user}")
        await followup_v2(interaction, f"{участник.mention} отключён от комнаты.", ephemeral=True)

    @app_commands.command(name="ban", description="Запретить участнику вход")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def voice_ban(self, interaction: discord.Interaction, участник: discord.Member):
        await interaction.response.defer(ephemeral=True)
        room, channel = await self._managed_room(interaction)
        if channel is None:
            return
        if not await has_active_vip(room[1]):
            await respond_v2(interaction,
                "Постоянный ban-лист доступен владельцам активного VIP.", ephemeral=True
            )
            return
        if участник.id == room[1] or участник.bot:
            await respond_v2(interaction,
                "Нельзя заблокировать владельца комнаты или бота.", ephemeral=True
            )
            return
        await add_voice_owner_ban(room[1], участник.id)
        await channel.set_permissions(участник, view_channel=False, connect=False, reason=f"Banned by {interaction.user}")
        if участник.voice and участник.voice.channel == channel:
            await участник.move_to(None, reason="Banned from voice room")
        await self._refresh_control_message(channel, room)
        await followup_v2(interaction,
            f"{участник.mention} добавлен в ваш постоянный список блокировок.",
            ephemeral=True,
        )

    @app_commands.command(name="unban", description="Снять блокировку с участника")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def voice_unban(self, interaction: discord.Interaction, участник: discord.Member):
        await interaction.response.defer(ephemeral=True)
        room, channel = await self._managed_room(interaction)
        if channel is None:
            return
        if not await has_active_vip(room[1]):
            await respond_v2(interaction,
                "Постоянный ban-лист доступен владельцам активного VIP.", ephemeral=True
            )
            return
        removed = await remove_voice_owner_ban(room[1], участник.id)
        await channel.set_permissions(участник, overwrite=None, reason=f"Unbanned by {interaction.user}")
        await self._refresh_control_message(channel, room)
        await followup_v2(interaction,
            f"Блокировка {участник.mention} снята."
            if removed
            else f"{участник.mention} отсутствовал в вашем списке блокировок.",
            ephemeral=True,
        )

    @app_commands.command(
        name="access",
        description="Выдать роли или участнику доступ к скрытой голосовой комнате",
    )
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def give_access(
        self,
        interaction: discord.Interaction,
        цель: discord.Member | discord.Role,
    ):
        await interaction.response.defer(ephemeral=True)
        _, channel = await self._managed_room(interaction)
        if channel is None:
            return
        await channel.set_permissions(
            цель,
            view_channel=True,
            connect=True,
            speak=True,
            reason=f"Access granted by {interaction.user}",
        )
        result = (
            f"Роль {цель.mention} получила доступ."
            if isinstance(цель, discord.Role)
            else f"Участник {цель.mention} получил доступ."
        )
        await followup_v2(
            interaction,
            result,
            ephemeral=True,
        )

    @app_commands.command(name="name", description="Переименовать временную комнату")
    @app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
    async def set_voice_name(self, interaction: discord.Interaction, название: app_commands.Range[str, 1, 100]):
        await interaction.response.defer(ephemeral=True)
        _, channel = await self._managed_room(interaction)
        if channel is None:
            return
        await channel.edit(name=название, reason="Voice room renamed")
        await followup_v2(interaction, f"Название изменено на «{название}».", ephemeral=True)

    @app_commands.command(
        name="transfer",
        description="Передать права владельца голосовой комнаты",
    )
    @app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
    async def voice_transfer(
        self, interaction: discord.Interaction, новый_владелец: discord.Member
    ):
        await interaction.response.defer(ephemeral=True)
        room, channel = await self._managed_room(interaction)
        if channel is None:
            return
        if новый_владелец.bot or новый_владелец.id == room[1]:
            await respond_v2(interaction,
                "Выберите другого участника, который не является ботом.", ephemeral=True
            )
            return

        transferred = await transfer_voice_room_owner(
            channel.id, room[1], новый_владелец.id
        )
        if not transferred:
            await followup_v2(interaction,
                "У выбранного участника уже есть своя голосовая комната.",
                ephemeral=True,
            )
            return
        old_bans = set(await get_voice_owner_bans(room[1]))
        new_bans = (
            set(await get_voice_owner_bans(новый_владелец.id))
            if await has_active_vip(новый_владелец.id)
            else set()
        )
        try:
            await _apply_voice_owner_permissions(
                channel,
                interaction.user,
                новый_владелец,
                old_bans,
                new_bans,
            )
        except discord.HTTPException:
            logger.exception("Failed to apply voice ownership transfer in %s", channel.id)
            await transfer_voice_room_owner(
                channel.id, новый_владелец.id, room[1]
            )
            try:
                await channel.set_permissions(
                    interaction.user,
                    view_channel=True,
                    connect=True,
                    speak=True,
                    manage_channels=True,
                    move_members=True,
                    manage_messages=True,
                    reason="Voice room ownership transfer rollback",
                )
                await channel.set_permissions(
                    новый_владелец,
                    overwrite=None,
                    reason="Voice room ownership transfer rollback",
                )
            except discord.HTTPException:
                logger.exception("Failed to restore voice permissions after rollback")
            else:
                await complete_voice_room_transfer(channel.id, room[1])
            await followup_v2(interaction,
                "Не удалось передать права; владелец комнаты не изменён.",
                ephemeral=True,
            )
            return
        await complete_voice_room_transfer(channel.id, новый_владелец.id)
        updated_room = await get_voice_room(channel.id)
        await self._refresh_control_message(channel, updated_room)
        await followup_v2(interaction,
            f"Права владельца переданы {новый_владелец.mention}.", ephemeral=True
        )

    @app_commands.command(name="delete", description="Удалить свою временную комнату")
    @app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
    async def voice_delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        _, channel = await self._managed_room(interaction)
        if channel is None:
            return
        await respond_v2(interaction, "Комната удаляется.", ephemeral=True)
        await channel.delete(reason=f"Deleted by owner {interaction.user}")
        await delete_voice_room(channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceRoomsCog(bot))
