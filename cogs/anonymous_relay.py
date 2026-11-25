from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from repositories.anonymous import (
    AnonymousConversation,
    AnonymousMessageLink,
    close_anonymous_conversation_by_message,
    close_anonymous_conversation_by_staff_message,
    get_anonymous_conversation_by_message,
    get_anonymous_message_links,
    link_anonymous_message,
    set_anonymous_thread,
)
from ui.messages import (
    edit_status_message_v2,
    followup_v2,
    respond_v2,
    send_v2,
)
from utils.discord_lookup import request_ping_content, request_ping_mentions
from ui.templates import notice
from utils.embeds import WHITE_COLOR
from utils.views import SafeModal, SafeView


logger = logging.getLogger(__name__)
_THREAD_LOCKS: dict[int, asyncio.Lock] = {}


async def _fetch_thread(
    client: discord.Client,
    guild: discord.Guild,
    channel_id: int | None,
) -> discord.Thread | None:
    if channel_id is None:
        return None
    cached = guild.get_thread(channel_id)
    if isinstance(cached, discord.Thread):
        return cached
    try:
        fetched = await client.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return fetched if isinstance(fetched, discord.Thread) else None


async def _fetch_linked_message(
    interaction: discord.Interaction,
    link: AnonymousMessageLink,
) -> discord.Message | None:
    source = interaction.message
    if (
        source is not None
        and source.id == link.message_id
        and source.channel.id == link.channel_id
    ):
        return source

    channel = interaction.client.get_channel(link.channel_id)
    if channel is None:
        guild = interaction.client.get_guild(interaction.guild_id or 0)
        if guild is not None:
            channel = guild.get_channel_or_thread(link.channel_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(link.channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    fetch_message = getattr(channel, "fetch_message", None)
    if fetch_message is None:
        return None
    try:
        return await fetch_message(link.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _close_linked_messages(
    interaction: discord.Interaction,
    conversation: AnonymousConversation,
    *,
    status: str,
) -> None:
    links = await get_anonymous_message_links(conversation.id)
    semaphore = asyncio.Semaphore(4)

    async def close_link(link: AnonymousMessageLink) -> None:
        # Thread replies form the conversation history and have no interactive
        # controls. The thread itself is locked below, so rewriting every reply
        # would only add avoidable Discord API traffic.
        if link.direction == "thread":
            return
        async with semaphore:
            message = await _fetch_linked_message(interaction, link)
            if message is None:
                return
            try:
                await edit_status_message_v2(
                    message,
                    status=status,
                    accent_color=discord.Color.red(),
                    view=None,
                )
            except (discord.NotFound, discord.Forbidden):
                return
            except discord.HTTPException:
                logger.exception(
                    "Could not close anonymous linked message %s in channel %s",
                    link.message_id,
                    link.channel_id,
                )

    await asyncio.gather(*(close_link(link) for link in links))


async def _finalize_anonymous_close(
    interaction: discord.Interaction,
    conversation: AnonymousConversation,
    *,
    status: str,
    audit_reason: str,
) -> None:
    await _close_linked_messages(
        interaction,
        conversation,
        status=status,
    )
    guild = interaction.client.get_guild(conversation.guild_id)
    if guild is None:
        return
    thread = await _fetch_thread(
        interaction.client,
        guild,
        conversation.thread_id,
    )
    if thread is None:
        return
    try:
        await thread.edit(
            archived=True,
            locked=True,
            reason=audit_reason,
        )
    except discord.HTTPException:
        logger.exception(
            "Could not archive anonymous thread %s",
            conversation.number,
        )


async def _ensure_conversation_thread(
    interaction: discord.Interaction,
    conversation: AnonymousConversation,
) -> discord.Thread:
    guild = interaction.client.get_guild(conversation.guild_id)
    if guild is None:
        raise RuntimeError("Anonymous conversation guild is unavailable")

    existing = await _fetch_thread(
        interaction.client,
        guild,
        conversation.thread_id or conversation.public_message_id,
    )
    if existing is not None:
        if existing.archived:
            await existing.edit(
                archived=False,
                reason="Продолжение анонимного обращения",
            )
        return existing

    lock = _THREAD_LOCKS.setdefault(conversation.id, asyncio.Lock())
    try:
        async with lock:
            existing = await _fetch_thread(
                interaction.client,
                guild,
                conversation.thread_id or conversation.public_message_id,
            )
            if existing is not None:
                return existing

            channel = guild.get_channel(conversation.public_channel_id)
            if not isinstance(channel, discord.TextChannel):
                fetched = await interaction.client.fetch_channel(
                    conversation.public_channel_id
                )
                channel = fetched if isinstance(fetched, discord.TextChannel) else None
            if channel is None:
                raise RuntimeError("Anonymous request channel is unavailable")

            message = await channel.fetch_message(conversation.public_message_id)
            thread = await message.create_thread(
                name=f"обращение-{conversation.number.lower()}"[:100],
                auto_archive_duration=1440,
                reason="Обсуждение анонимного обращения",
            )
            intro = await send_v2(
                thread,
                f"Переписка по анонимному обращению **{conversation.number}**.",
                component_marker=f"anonymous-thread:{conversation.number}",
            )
            await set_anonymous_thread(
                conversation.id,
                thread_id=thread.id,
                thread_message_id=intro.id,
            )
            return thread
    finally:
        if not lock.locked():
            _THREAD_LOCKS.pop(conversation.id, None)


def _staff_reply_embed(
    conversation: AnonymousConversation,
    staff: discord.abc.User,
    text: str,
):
    return notice(
        code="ANONYMOUS.STAFF_REPLY",
        title="Ответ по анонимному обращению",
        summary=text,
        details=(
            ("Обращение", conversation.number),
            ("Ответил", staff.mention),
        ),
        next_step="Автор получит ответ в личные сообщения.",
        accent=WHITE_COLOR,
        reference=conversation.number,
    )


def _author_reply_embed(
    conversation: AnonymousConversation,
    text: str,
):
    return notice(
        code="ANONYMOUS.AUTHOR_REPLY",
        title="Ответ автора анонимного обращения",
        summary=text,
        details=(("Обращение", conversation.number),),
        next_step="Сообщение передано старшему составу.",
        accent=WHITE_COLOR,
        reference=conversation.number,
    )


class AnonymousStaffReplyModal(SafeModal, title="Ответить на обращение"):
    response_text = discord.ui.TextInput(
        label="Ответ пользователю",
        placeholder="Введите сообщение, которое бот отправит автору в ЛС",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=2_000,
    )

    def __init__(self, source_message_id: int) -> None:
        super().__init__()
        self.source_message_id = source_message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from cogs.forms import _has_staff_access

        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        conversation = await get_anonymous_conversation_by_message(
            self.source_message_id
        )
        if conversation is None or conversation.status != "open":
            await followup_v2(
                interaction,
                "Связь с этим обращением не найдена или уже закрыта.",
                ephemeral=True,
            )
            return
        guild = interaction.client.get_guild(conversation.guild_id)
        if guild is None:
            await followup_v2(interaction, "Сервер недоступен.", ephemeral=True)
            return
        try:
            thread = await _ensure_conversation_thread(
                interaction,
                conversation,
            )
        except (discord.HTTPException, RuntimeError):
            logger.exception(
                "Could not prepare thread for anonymous request %s",
                conversation.number,
            )
            await followup_v2(
                interaction,
                "Не удалось открыть ветку анонимного обращения.",
                ephemeral=True,
            )
            return

        recipient = guild.get_member(conversation.author_id)
        try:
            recipient = recipient or await interaction.client.fetch_user(
                conversation.author_id
            )
            dm_embed = notice(
                code="ANONYMOUS.DM_REPLY",
                title="Ответ на ваше анонимное обращение",
                summary=str(self.response_text.value),
                next_step="Ответьте кнопкой под этим сообщением, если нужно продолжить.",
                accent=WHITE_COLOR,
                reference=conversation.number,
            )
            dm_message = await send_v2(
                recipient,
                embed=dm_embed,
                view=AnonymousUserReplyView(),
                component_marker=f"anonymous-dm:{conversation.number}",
            )
        except discord.HTTPException:
            await followup_v2(
                interaction,
                "Не удалось отправить автору личное сообщение.",
                ephemeral=True,
            )
            return

        await link_anonymous_message(
            conversation.id,
            message_id=dm_message.id,
            channel_id=dm_message.channel.id,
            direction="staff_dm",
        )
        thread_message = await send_v2(
            thread,
            embed=_staff_reply_embed(
                conversation,
                interaction.user,
                str(self.response_text.value),
            ),
            component_marker=f"anonymous-relay:{conversation.number}",
        )
        await link_anonymous_message(
            conversation.id,
            message_id=thread_message.id,
            channel_id=thread.id,
            direction="thread",
        )
        await followup_v2(
            interaction,
            "Ответ отправлен автору анонимного обращения.",
            ephemeral=True,
        )


class AnonymousAuthorReplyModal(SafeModal, title="Ответить старшему составу"):
    response_text = discord.ui.TextInput(
        label="Ваш ответ",
        placeholder="Сообщение будет передано старшему составу",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=2_000,
    )

    def __init__(self, source_message_id: int) -> None:
        super().__init__()
        self.source_message_id = source_message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        conversation = await get_anonymous_conversation_by_message(
            self.source_message_id
        )
        if (
            conversation is None
            or conversation.status != "open"
            or interaction.user.id != conversation.author_id
        ):
            await followup_v2(
                interaction,
                "Эта переписка недоступна.",
                ephemeral=True,
            )
            return
        guild = interaction.client.get_guild(conversation.guild_id)
        if guild is None:
            await followup_v2(interaction, "Сервер недоступен.", ephemeral=True)
            return
        try:
            thread = await _ensure_conversation_thread(
                interaction,
                conversation,
            )
        except (discord.HTTPException, RuntimeError):
            logger.exception(
                "Could not prepare thread for anonymous request %s",
                conversation.number,
            )
            await followup_v2(
                interaction,
                "Ветка анонимного обращения недоступна.",
                ephemeral=True,
            )
            return
        thread_message = await send_v2(
            thread,
            content=request_ping_content(guild),
            embed=_author_reply_embed(
                conversation,
                str(self.response_text.value),
            ),
            component_marker=f"anonymous-relay:{conversation.number}",
            allowed_mentions=request_ping_mentions(guild),
        )
        await link_anonymous_message(
            conversation.id,
            message_id=thread_message.id,
            channel_id=thread.id,
            direction="thread",
        )
        await followup_v2(
            interaction,
            "Ответ передан старшему составу.",
            ephemeral=True,
        )


class AnonymousStaffReplyView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Ответить автору",
        style=discord.ButtonStyle.primary,
        custom_id="anonymous:staff_reply",
    )
    async def reply(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        from cogs.forms import _has_staff_access

        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        if interaction.message is None:
            await respond_v2(interaction, "Обращение не найдено.", ephemeral=True)
            return
        # A modal must be the initial interaction response. Resolve the
        # conversation on modal submit (a fresh interaction) so a busy SQLite
        # writer cannot consume Discord's three-second acknowledgement window.
        await interaction.response.send_modal(
            AnonymousStaffReplyModal(interaction.message.id)
        )

    @discord.ui.button(
        label="Закрыть обращение",
        style=discord.ButtonStyle.danger,
        custom_id="anonymous:staff_close",
    )
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        from cogs.forms import _has_staff_access

        if not _has_staff_access(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        if interaction.message is None:
            await respond_v2(interaction, "Обращение не найдено.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        conversation = await close_anonymous_conversation_by_staff_message(
            interaction.message.id
        )
        if conversation is None:
            await followup_v2(
                interaction,
                "Обращение уже закрыто или связь с ним недоступна.",
                ephemeral=True,
            )
            return

        await _finalize_anonymous_close(
            interaction,
            conversation,
            status=f"🔒 Закрыто старшим составом • {interaction.user.mention}",
            audit_reason=(
                f"Anonymous request {conversation.number} closed by staff "
                f"{interaction.user.id}"
            ),
        )
        await followup_v2(
            interaction,
            f"Обращение **{conversation.number}** закрыто.",
            ephemeral=True,
        )


class AnonymousUserReplyView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Ответить",
        style=discord.ButtonStyle.primary,
        custom_id="anonymous:user_reply",
    )
    async def reply(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.message is None:
            await respond_v2(interaction, "Переписка не найдена.", ephemeral=True)
            return
        await interaction.response.send_modal(
            AnonymousAuthorReplyModal(interaction.message.id)
        )

    @discord.ui.button(
        label="Закрыть обращение",
        style=discord.ButtonStyle.danger,
        custom_id="anonymous:user_close",
    )
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.message is None:
            await respond_v2(interaction, "Переписка не найдена.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        conversation = await close_anonymous_conversation_by_message(
            interaction.message.id,
            interaction.user.id,
        )
        if conversation is None:
            await followup_v2(
                interaction,
                "Обращение уже закрыто или эта переписка вам не принадлежит.",
                ephemeral=True,
            )
            return

        await _finalize_anonymous_close(
            interaction,
            conversation,
            status="🔒 Закрыто автором обращения",
            audit_reason=(
                f"Anonymous request {conversation.number} closed by author "
                f"{interaction.user.id}"
            ),
        )
        await followup_v2(
            interaction,
            f"Обращение **{conversation.number}** закрыто.",
            ephemeral=True,
        )


class AnonymousRelayCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(AnonymousStaffReplyView())
        self.bot.add_view(AnonymousUserReplyView())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AnonymousRelayCog(bot))
