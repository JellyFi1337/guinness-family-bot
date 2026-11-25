from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    HIGH_TOOLS_ALLOWED_ROLE_IDS,
    HIGH_TOOLS_ALLOWED_ROLE_NAMES,
    POLL_ALLOWED_ROLE_IDS,
    POLL_ALLOWED_ROLE_NAMES,
)
from ui.layouts import marker_component_id
from repositories.polls import (
    PollRecord,
    acknowledge_poll_revision,
    cast_poll_vote,
    close_poll,
    create_poll,
    delete_poll,
    get_active_polls,
    get_poll,
    get_poll_id_by_message,
    get_poll_results,
    get_polls_needing_sync,
    mark_poll_message_missing,
    mark_poll_messages_missing,
    mark_open_polls_dirty,
    mark_expired_polls,
    mark_poll_synced,
    set_poll_message,
)
from ui.messages import edit_message_v2, followup_v2, respond_v2, send_v2
from ui.templates import notice
from utils.discord_lookup import get_text_channel
from utils.embeds import WHITE_COLOR
from utils.locks import KeyedLockPool
from utils.views import SafeView
from utils.access import has_administrator_access


logger = logging.getLogger(__name__)
_POLL_LOCKS = KeyedLockPool[int]()


def _member_has_access(
    member: discord.Member,
    role_ids: tuple[int, ...],
    role_names: tuple[str, ...],
) -> bool:
    if has_administrator_access(member):
        return True
    if role_ids:
        expected = set(role_ids)
        return any(role.id in expected for role in member.roles)
    expected_names = set(role_names)
    return any(role.name in expected_names for role in member.roles)


def _poll_create_access(member: discord.Member) -> bool:
    return _member_has_access(member, POLL_ALLOWED_ROLE_IDS, POLL_ALLOWED_ROLE_NAMES)


def _poll_manage_access(member: discord.Member) -> bool:
    return _member_has_access(
        member, HIGH_TOOLS_ALLOWED_ROLE_IDS, HIGH_TOOLS_ALLOWED_ROLE_NAMES
    )


def _anonymous_poll_log_embed(
    poll: PollRecord,
    message: discord.Message,
):
    return notice(
        code="POLL.ANONYMOUS_LOG",
        title="Анонимный опрос создан",
        summary=poll.question,
        details=(
            ("Инициатор", f"<@{poll.creator_id}>"),
            ("Канал", f"<#{poll.channel_id}>"),
            ("Опрос", message.jump_url),
        ),
        next_step="Опрос опубликован в выбранном канале.",
        accent=WHITE_COLOR,
        reference=f"#{poll.id:04d}",
    )


async def poll_embed(poll: PollRecord):
    totals, voters = await get_poll_results(poll.id)
    denominator = sum(totals) if poll.multiple else voters
    status_open = poll.status == "open" and datetime.fromisoformat(poll.closes_at) > datetime.now(UTC)
    blocks: list[str] = []
    for index, (option, total) in enumerate(
        zip(poll.options, totals, strict=True), start=1
    ):
        percent = (total / denominator * 100) if denominator else 0
        filled = min(10, round(percent / 10))
        bar = "▰" * filled + "▱" * (10 - filled)
        blocks.append(
            f"**{index}. {option}**\n{bar}  **{total}** · {percent:.0f}%"
        )

    closes_timestamp = int(datetime.fromisoformat(poll.closes_at).timestamp())
    owner = "**Аноним**" if poll.anonymous else f"<@{poll.creator_id}>"
    mode = "несколько вариантов" if poll.multiple else "один вариант"
    return notice(
        code="POLL.CARD",
        title=f"📊 {poll.question}",
        summary="\n\n".join(blocks),
        details=(
            ("Проголосовали", str(voters)),
            ("Режим", mode),
            (
                "Статус",
                f"🟢 Открыт до <t:{closes_timestamp}:R>" if status_open else "🔒 Завершён",
            ),
            ("Инициатор", owner),
        ),
        next_step="Выберите вариант в меню под карточкой." if status_open else "Опрос завершён.",
        accent=WHITE_COLOR,
        reference=f"#{poll.id:04d}",
    )


class PollView(SafeView):
    def __init__(self, poll: PollRecord):
        super().__init__(timeout=None)
        self.poll_id = poll.id

        select = discord.ui.Select(
            custom_id="poll:vote",
            placeholder=(
                "Выберите один или несколько вариантов"
                if poll.multiple
                else "Выберите один вариант"
            ),
            min_values=1,
            max_values=len(poll.options) if poll.multiple else 1,
            options=[
                discord.SelectOption(label=option, value=str(index))
                for index, option in enumerate(poll.options)
            ],
            row=0,
        )
        select.callback = self._vote
        self.add_item(select)

        close_button = discord.ui.Button(
            label="Завершить опрос",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id="poll:close",
            row=1,
        )
        close_button.callback = self._close
        self.add_item(close_button)

    async def _vote(self, interaction: discord.Interaction) -> None:
        if interaction.user.bot:
            await respond_v2(interaction, "Боты не участвуют в опросах.", ephemeral=True)
            return
        values = tuple(int(value) for value in interaction.data.get("values", ()))
        await interaction.response.defer(ephemeral=True)
        async with _POLL_LOCKS.hold(self.poll_id):
            result = await cast_poll_vote(self.poll_id, interaction.user.id, values)
        messages = {
            "recorded": "Ваш голос сохранён.",
            "closed": "Опрос уже завершён.",
            "not_found": "Опрос больше не существует.",
            "invalid": "Не удалось распознать выбранный вариант.",
        }
        await followup_v2(interaction, messages[result], ephemeral=True)

    async def _close(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        poll = await get_poll(self.poll_id)
        if poll is None:
            await respond_v2(interaction, "Опрос больше не существует.", ephemeral=True)
            return
        member = interaction.user
        if (
            interaction.user.id != poll.creator_id
            and not isinstance(member, discord.Member)
        ) or (
            interaction.user.id != poll.creator_id
            and isinstance(member, discord.Member)
            and not _poll_manage_access(member)
        ):
            await respond_v2(
                interaction,
                "Завершить опрос может инициатор или старший состав.",
                ephemeral=True,
            )
            return
        async with _POLL_LOCKS.hold(self.poll_id):
            closed = await close_poll(self.poll_id)
            poll = await get_poll(self.poll_id)
            if poll is not None and interaction.message is not None:
                await edit_message_v2(
                    interaction.message,
                    embed=await poll_embed(poll),
                    view=None,
                )
                await mark_poll_synced(
                    poll.id,
                    poll.ui_revision,
                    interaction.message.id,
                )
        await followup_v2(
            interaction,
            "Опрос завершён." if closed else "Опрос уже был завершён.",
            ephemeral=True,
        )


class PollsCog(
    commands.GroupCog,
    group_name="poll",
    group_description="Интерактивные опросы",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        for poll in await get_active_polls():
            self.bot.add_view(PollView(poll), message_id=poll.message_id)
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                "poll-ui-sync",
                self.sync_polls,
                interval=2,
                timeout=60,
                jitter=0,
            )

    async def cog_unload(self) -> None:
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("poll-ui-sync")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Raw delete events are not replayed after downtime. Marking open polls
        # dirty forces a bounded fetch/edit pass and recreates missing panels.
        await mark_open_polls_dirty()

    async def sync_polls(self) -> None:
        await mark_expired_polls()
        failures = 0
        for record in await get_polls_needing_sync():
            async with _POLL_LOCKS.hold(record.id):
                poll = await get_poll(record.id)
                if poll is None:
                    continue
                guild = self.bot.get_guild(record.guild_id)
                if guild is None:
                    await acknowledge_poll_revision(record.id, record.ui_revision)
                    continue
                channel = guild.get_channel_or_thread(record.channel_id)
                if channel is None:
                    try:
                        channel = await guild.fetch_channel(record.channel_id)
                    except discord.NotFound:
                        await acknowledge_poll_revision(record.id, record.ui_revision)
                        continue
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    await acknowledge_poll_revision(record.id, record.ui_revision)
                    continue
                message = None
                try:
                    if record.message_id is not None:
                        try:
                            message = await channel.fetch_message(record.message_id)
                        except discord.NotFound:
                            message = None
                    if message is None:
                        marker = f"poll:{record.id}"
                        expected = marker_component_id(marker)

                        def has_marker(
                            candidate: discord.Message, expected: int = expected
                        ) -> bool:
                            # Bound explicitly: the closure is only used inside
                            # this iteration today, but a late-binding capture
                            # silently starts matching the wrong poll the moment
                            # the search is moved or deferred.
                            def visit(component) -> bool:
                                return getattr(component, "id", None) == expected or any(
                                    visit(child)
                                    for child in getattr(component, "children", ())
                                )

                            return any(visit(component) for component in candidate.components)

                        async for candidate in channel.history(limit=100):
                            if candidate.author == guild.me and has_marker(candidate):
                                message = candidate
                                break
                    view = PollView(poll) if poll.status == "open" else None
                    if message is None:
                        message = await send_v2(
                            channel,
                            embed=await poll_embed(poll),
                            view=view,
                            component_marker=f"poll:{poll.id}",
                        )
                    else:
                        await edit_message_v2(
                            message,
                            embed=await poll_embed(poll),
                            view=view,
                        )
                    await mark_poll_synced(poll.id, poll.ui_revision, message.id)
                except discord.HTTPException:
                    logger.exception("Could not synchronize poll %s message", record.id)
                    failures += 1
        if failures:
            raise RuntimeError(
                f"Could not synchronize {failures} poll message(s)"
            )

    async def _mark_deleted_panel(self, guild_id: int | None, message_id: int) -> None:
        if guild_id is None:
            return
        poll_id = await get_poll_id_by_message(guild_id, message_id)
        if poll_id is None:
            return
        async with _POLL_LOCKS.hold(poll_id):
            await mark_poll_message_missing(
                poll_id,
                guild_id=guild_id,
                message_id=message_id,
            )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self._mark_deleted_panel(payload.guild_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self,
        payload: discord.RawBulkMessageDeleteEvent,
    ) -> None:
        if payload.guild_id is None:
            return
        await mark_poll_messages_missing(payload.guild_id, payload.message_ids)

    @app_commands.command(name="create", description="Создать интерактивный опрос")
    @app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
    async def poll(
        self,
        interaction: discord.Interaction,
        вопрос: app_commands.Range[str, 1, 300],
        варианты: app_commands.Range[str, 3, 1000],
        часы: app_commands.Range[int, 1, 168] = 24,
        несколько_ответов: bool = False,
        анонимный: bool = False,
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not _poll_create_access(member):
            await respond_v2(interaction, "У вас нет права создавать опросы.", ephemeral=True)
            return
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await respond_v2(
                interaction,
                "Опрос можно создать только в текстовом канале или ветке.",
                ephemeral=True,
            )
            return
        options = list(
            dict.fromkeys(part.strip() for part in варианты.split("|") if part.strip())
        )
        if not 2 <= len(options) <= 10 or any(len(option) > 55 for option in options):
            await respond_v2(
                interaction,
                "Укажите от 2 до 10 вариантов через `|`; каждый — не более 55 символов.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        poll_id = await create_poll(
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            creator_id=interaction.user.id,
            question=вопрос,
            options=options,
            multiple=несколько_ответов,
            anonymous=анонимный,
            closes_at=(datetime.now(UTC) + timedelta(hours=часы)).isoformat(),
        )
        poll = await get_poll(poll_id)
        if poll is None:
            raise RuntimeError("Created poll could not be loaded")
        try:
            message = await send_v2(
                channel,
                embed=await poll_embed(poll),
                view=PollView(poll),
                component_marker=f"poll:{poll.id}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            await delete_poll(poll_id)
            logger.exception("Failed to publish poll %s", poll_id)
            await followup_v2(interaction, "Не удалось создать опрос.", ephemeral=True)
            return
        await set_poll_message(
            poll_id,
            message.id,
            synced_revision=poll.ui_revision,
        )
        if poll.anonymous and interaction.guild is not None:
            logs_channel = get_text_channel(interaction.guild, "anonymous_logs")
            if logs_channel is None:
                logger.warning(
                    "Anonymous poll %s created without an anonymous log channel",
                    poll.id,
                )
            else:
                try:
                    await send_v2(
                        logs_channel,
                        embed=_anonymous_poll_log_embed(poll, message),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    # The poll is already published. Do not report its creation
                    # as failed just because the private audit channel is
                    # temporarily unavailable.
                    logger.exception(
                        "Failed to write anonymous poll %s audit log",
                        poll.id,
                    )
        await followup_v2(interaction, f"Опрос создан: {message.jump_url}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PollsCog(bot))
