import logging
import random
import re
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ui.messages import edit_message_v2, followup_v2, respond_v2, send_v2
from ui.layouts import marker_component_id

from config import (
    HIGH_TOOLS_ALLOWED_ROLE_IDS,
    HIGH_TOOLS_ALLOWED_ROLE_NAMES,
    ROLES,
)
from utils.database import (
    complete_giveaway_draw,
    clear_giveaway_message,
    create_giveaway,
    delete_giveaway,
    get_active_giveaways,
    get_giveaway,
    get_giveaway_draw,
    get_giveaway_entries,
    prepare_giveaway_draw,
    set_giveaway_message,
    toggle_giveaway_entry,
)
from utils.discord_lookup import get_text_channel
from ui.design import Accent
from ui.templates import notice
from utils.embeds import WHITE_COLOR, apply_accent
from utils.locks import KeyedLockPool
from utils.views import SafeView, stop_registered_view
from utils.access import has_administrator_access


logger = logging.getLogger(__name__)
_GIVEAWAY_LOCKS = KeyedLockPool[int]()


def _giveaway_lock(giveaway_id: int):
    return _GIVEAWAY_LOCKS.lock(giveaway_id)


def _member_has_access(
    member: discord.Member,
    role_ids: tuple[int, ...],
    role_names: tuple[str, ...],
) -> bool:
    if has_administrator_access(member):
        return True
    if role_ids:
        ids = set(role_ids)
        return any(role.id in ids for role in member.roles)
    names = set(role_names)
    return any(role.name in names for role in member.roles)


def _giveaway_access(member: discord.Member) -> bool:
    return _member_has_access(
        member, HIGH_TOOLS_ALLOWED_ROLE_IDS, HIGH_TOOLS_ALLOWED_ROLE_NAMES
    )


def _can_finish_giveaway(user: discord.abc.User, creator_id: int) -> bool:
    """Only the organizer, Owner role, an administrator, or bot owner may finish."""

    return (
        user.id == creator_id
        or has_administrator_access(user)
        or any(
            getattr(role, "name", None) == ROLES["owner"]
            for role in getattr(user, "roles", ())
        )
    )


def _duration(raw: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*(м|min|ч|h|д|d)\s*", raw.casefold())
    if not match:
        raise ValueError("Укажите длительность как `30м`, `2ч` или `3д`.")
    amount = int(match.group(1))
    if amount < 1:
        raise ValueError("Длительность должна быть больше нуля.")
    unit = match.group(2)
    delta = (
        timedelta(minutes=amount)
        if unit in {"м", "min"}
        else timedelta(hours=amount)
        if unit in {"ч", "h"}
        else timedelta(days=amount)
    )
    if delta > timedelta(days=30):
        raise ValueError("Максимальная длительность розыгрыша — 30 дней.")
    return delta


async def giveaway_embed(giveaway_id: int):
    giveaway = await get_giveaway(giveaway_id)
    if giveaway is None:
        raise RuntimeError("Розыгрыш не найден")
    _, title, prize, winner_count, _, _, creator_id, ends_at, status = giveaway
    entries = await get_giveaway_entries(giveaway_id)
    timestamp = int(datetime.fromisoformat(ends_at).timestamp())
    active = status == "active"
    details: list[tuple[str, str]] = [
        ("Победителей", str(winner_count)),
        ("Участников", str(len(entries))),
        (
            "Завершение" if active else "Статус",
            f"<t:{timestamp}:R>" if active else "🔒 Завершён",
        ),
    ]
    if entries:
        visible = " ".join(f"<@{user_id}>" for user_id in entries[:20])
        if len(entries) > 20:
            visible += f"\n…и ещё {len(entries) - 20}"
        details.append(("Участники", visible))
    details.append(("Организатор", f"<@{creator_id}>"))
    return notice(
        code="GIVEAWAY.CARD",
        title=f"🎁 {title}",
        summary=f"**Приз:** {prize}",
        details=tuple(details),
        next_step="Нажмите кнопку участия, пока розыгрыш открыт." if active else "Розыгрыш завершён.",
        accent=Accent.EVENTS,
        reference=f"#{giveaway_id:04d}",
    )


async def _edit_giveaway_message(
    message: discord.Message,
    giveaway_id: int,
    view: discord.ui.View | None,
) -> None:
    embed = apply_accent(await giveaway_embed(giveaway_id), Accent.EVENTS)
    await edit_message_v2(message, embed=embed, view=view)


async def _finish_and_announce_unlocked(
    guild: discord.Guild,
    giveaway_id: int,
    *,
    force_refresh: bool = False,
    forced_by_id: int | None = None,
) -> bool:
    giveaway = await get_giveaway(giveaway_id)
    if giveaway is None:
        return False
    _, _, _, winner_count, _, _, _, _, status = giveaway
    if status == "finished" and not force_refresh:
        return False
    if status not in {"active", "announcing", "finished"}:
        return False
    entries = [
        user_id
        for user_id in await get_giveaway_entries(giveaway_id)
        if (member := guild.get_member(user_id)) is not None and not member.bot
    ]
    candidate_winners = random.SystemRandom().sample(
        entries, min(winner_count, len(entries))
    )
    prepared = await prepare_giveaway_draw(
        giveaway_id,
        candidate_winners,
        force_refresh=force_refresh,
        forced_by_id=forced_by_id,
    )
    if prepared is None:
        return False
    draw_version, winners = prepared
    await _announce_prepared_giveaway(
        guild,
        giveaway_id,
        draw_version,
        winners,
        forced_by_id=forced_by_id,
    )
    return True


def _message_component_text(message: discord.Message) -> str:
    values: list[str] = []

    def visit(component) -> None:
        content = getattr(component, "content", None)
        if content:
            values.append(str(content))
        for child in getattr(component, "children", ()):
            visit(child)

    for component in message.components:
        visit(component)
    return "\n".join(values)


def _message_has_marker(message: discord.Message, marker: str) -> bool:
    expected = marker_component_id(marker)

    def visit(component) -> bool:
        return getattr(component, "id", None) == expected or any(
            visit(child) for child in getattr(component, "children", ())
        )

    return marker in _message_component_text(message) or any(
        visit(component) for component in message.components
    )


def _giveaway_result_embed(
    title: str,
    winners: list[int],
    *,
    forced_by_id: int | None = None,
):
    details = ()
    if forced_by_id is not None:
        details = (("Принудительно завершил", f"<@{forced_by_id}>"),)
    return notice(
        code="GIVEAWAY.RESULT",
        title="🎉 Розыгрыш завершён",
        summary=(
            f"**{title}**\n\nПобедители: "
            + ", ".join(f"<@{user_id}>" for user_id in winners)
            if winners
            else f"**{title}**\n\nРозыгрыш завершён без участников."
        ),
        details=details,
        next_step="Дополнительных действий не требуется.",
        accent=WHITE_COLOR,
    )


async def _announce_prepared_giveaway(
    guild: discord.Guild,
    giveaway_id: int,
    draw_version: int,
    winners: list[int],
    *,
    forced_by_id: int | None = None,
) -> int:
    giveaway = await get_giveaway(giveaway_id)
    draw = await get_giveaway_draw(giveaway_id)
    if giveaway is None or draw is None or draw[0] != draw_version:
        raise RuntimeError("Prepared giveaway draw no longer exists")
    _, title, _, _, channel_id, message_id, _, _, _ = giveaway
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError("Giveaway channel is unavailable")
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            await _edit_giveaway_message(message, giveaway_id, None)
        except discord.NotFound:
            # A deleted source message has no active controls to reconcile.
            pass
        except discord.HTTPException:
            # Keep the outbox operation retryable. Publishing winners while
            # the source card still appears open creates contradictory UI.
            logger.exception("Failed to close giveaway %s message", giveaway_id)
            raise

    marker = f"giveaway-result:{giveaway_id}:draw:{draw_version}"
    result_message: discord.Message | None = None
    if draw[2]:
        try:
            result_message = await channel.fetch_message(int(draw[2]))
        except (discord.NotFound, discord.Forbidden):
            result_message = None
    if result_message is None:
        try:
            async for candidate in channel.history(limit=100):
                if (
                    guild.me is not None
                    and candidate.author.id == guild.me.id
                    and _message_has_marker(candidate, marker)
                ):
                    result_message = candidate
                    break
        except discord.Forbidden:
            logger.warning("Cannot reconcile giveaway result in channel %s", channel.id)

    winner_mentions = " ".join(f"<@{user_id}>" for user_id in winners)
    result_embed = _giveaway_result_embed(
        title,
        winners,
        forced_by_id=forced_by_id,
    )
    if result_message is None:
        result_message = await send_v2(
            channel,
            content=winner_mentions or None,
            embed=result_embed,
            component_marker=marker,
            allowed_mentions=discord.AllowedMentions(
                users=[
                    guild.get_member(user_id)
                    for user_id in winners
                    if guild.get_member(user_id)
                ],
                roles=False,
                everyone=False,
            ),
        )
    await complete_giveaway_draw(giveaway_id, draw_version, result_message.id)
    return result_message.id


async def _finish_and_announce(
    guild: discord.Guild,
    giveaway_id: int,
    *,
    force_refresh: bool = False,
    forced_by_id: int | None = None,
) -> bool:
    async with _giveaway_lock(giveaway_id):
        return await _finish_and_announce_unlocked(
            guild,
            giveaway_id,
            force_refresh=force_refresh,
            forced_by_id=forced_by_id,
        )


class GiveawayView(SafeView):
    def __init__(self, giveaway_id: int):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(
        label="Участвовать / Выйти",
        style=discord.ButtonStyle.success,
        emoji="🎟️",
        custom_id="giveaway:toggle",
    )
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            await respond_v2(interaction, "Боты не участвуют.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with _giveaway_lock(self.giveaway_id):
            result = await toggle_giveaway_entry(self.giveaway_id, interaction.user.id)
            if result == "closed":
                await followup_v2(interaction, "Розыгрыш уже закрыт.", ephemeral=True)
                return
            await _edit_giveaway_message(
                interaction.message,
                self.giveaway_id,
                self,
            )
        await followup_v2(interaction,
            "Вы участвуете в розыгрыше." if result == "joined" else "Вы вышли из розыгрыша.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Завершить",
        style=discord.ButtonStyle.primary,
        emoji="🏁",
        custom_id="giveaway:finish",
    )
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        giveaway = await get_giveaway(self.giveaway_id)
        if giveaway is None:
            await followup_v2(interaction, "Розыгрыш не найден.", ephemeral=True)
            return
        creator_id = int(giveaway[6])
        if not _can_finish_giveaway(interaction.user, creator_id):
            await followup_v2(interaction,
                "Завершить розыгрыш может только организатор, участник с ролью "
                "Owner, администратор или владелец бота.",
                ephemeral=True,
            )
            return
        try:
            closed = await _finish_and_announce(
                interaction.guild,
                self.giveaway_id,
                # Every button-triggered finish is manual. Scheduled completion
                # leaves this field empty.
                forced_by_id=interaction.user.id,
            )
        except discord.HTTPException:
            logger.exception("Failed to finish giveaway %s", self.giveaway_id)
            await followup_v2(interaction,
                "Не удалось завершить розыгрыш.", ephemeral=True
            )
            return
        if closed:
            self.stop()
        await followup_v2(interaction,
            "Розыгрыш завершён." if closed else "Розыгрыш уже завершён.", ephemeral=True
        )

    @discord.ui.button(
        label="Удалить",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="giveaway:delete",
    )
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        giveaway = await get_giveaway(self.giveaway_id)
        if giveaway is None:
            await followup_v2(interaction, "Розыгрыш не найден.", ephemeral=True)
            return
        if interaction.user.id != giveaway[6] and not _giveaway_access(interaction.user):
            await followup_v2(interaction,
                "Удалить розыгрыш может организатор или старший состав.", ephemeral=True
            )
            return
        await delete_giveaway(self.giveaway_id)
        self.stop()
        await interaction.message.delete()
        await followup_v2(interaction, "Розыгрыш удалён.", ephemeral=True)


class GiveawaysCog(
    commands.GroupCog,
    group_name="giveaway",
    group_description="Розыгрыши",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._registered_views: dict[int, tuple[int, GiveawayView]] = {}

    def _register_view(
        self, giveaway_id: int, message_id: int, view: GiveawayView
    ) -> None:
        previous = self._registered_views.get(giveaway_id)
        if previous is not None:
            old_message_id, old_view = previous
            if old_message_id == message_id:
                return
            stop_registered_view(old_view)
        self.bot.add_view(view, message_id=message_id)
        self._registered_views[giveaway_id] = (message_id, view)

    async def cog_load(self):
        for giveaway_id, _, _, message_id, _ in await get_active_giveaways():
            if message_id:
                view = GiveawayView(giveaway_id)
                self._register_view(giveaway_id, message_id, view)
        supervisor = getattr(self.bot, "task_supervisor", None)
        operation_worker = getattr(self.bot, "operation_worker", None)
        if operation_worker is not None:
            operation_worker.register("giveaway.announce", self._announce_outbox)
        if supervisor is not None:
            supervisor.start_periodic(
                "giveaway-expiration",
                self.expiration,
                interval=30,
                timeout=60,
            )
            supervisor.start_periodic(
                "giveaway-panel-reconcile",
                self.reconcile_panels,
                interval=120,
                timeout=90,
            )

    async def cog_unload(self):
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("giveaway-expiration")
            await supervisor.stop_task("giveaway-panel-reconcile")
        operation_worker = getattr(self.bot, "operation_worker", None)
        if operation_worker is not None:
            operation_worker.unregister("giveaway.announce")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.reconcile_panels()

    async def reconcile_panels(self) -> None:
        failures = 0
        for giveaway_id, guild_id, channel_id, message_id, _ in await get_active_giveaways():
            async with _giveaway_lock(giveaway_id):
                guild = self.bot.get_guild(guild_id) if guild_id else None
                if guild is None:
                    guild = next(
                        (item for item in self.bot.guilds if item.get_channel(channel_id)),
                        None,
                    )
                if guild is None:
                    continue
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    continue
                marker = f"giveaway:{giveaway_id}"
                message = None
                try:
                    if message_id:
                        try:
                            message = await channel.fetch_message(message_id)
                        except discord.NotFound:
                            await clear_giveaway_message(
                                giveaway_id, expected_message_id=message_id
                            )
                    if message is None:
                        async for candidate in channel.history(limit=250):
                            if (
                                guild.me is not None
                                and candidate.author.id == guild.me.id
                                and _message_has_marker(candidate, marker)
                            ):
                                message = candidate
                                break
                    view = GiveawayView(giveaway_id)
                    if message is None:
                        embed = apply_accent(
                            await giveaway_embed(giveaway_id), Accent.EVENTS
                        )
                        message = await send_v2(
                            channel,
                            embed=embed,
                            view=view,
                            component_marker=marker,
                        )
                    else:
                        await _edit_giveaway_message(message, giveaway_id, view)
                    await set_giveaway_message(giveaway_id, message.id)
                    self._register_view(giveaway_id, message.id, view)
                except discord.HTTPException:
                    failures += 1
                    logger.exception(
                        "Failed to reconcile giveaway panel %s", giveaway_id
                    )
        if failures:
            raise RuntimeError(f"Could not reconcile {failures} giveaway panel(s)")

    async def _announce_outbox(self, operation) -> int:
        giveaway_id = int(operation.payload["giveaway_id"])
        draw_version = int(operation.payload["draw_version"])
        draw = await get_giveaway_draw(giveaway_id)
        if draw is None:
            raise RuntimeError("Giveaway was removed before announcement")
        if draw[3] == "announced" and draw[2]:
            return int(draw[2])
        giveaway = await get_giveaway(giveaway_id)
        if giveaway is None:
            raise RuntimeError("Giveaway was removed before announcement")
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            guild = next(
                (
                    item
                    for item in self.bot.guilds
                    if item.get_channel(giveaway[4]) is not None
                ),
                None,
            )
        if guild is None:
            raise RuntimeError("Giveaway guild is unavailable")
        return await _announce_prepared_giveaway(
            guild,
            giveaway_id,
            draw_version,
            draw[1],
            forced_by_id=(
                int(operation.payload["forced_by_id"])
                if operation.payload.get("forced_by_id") is not None
                else None
            ),
        )

    async def expiration(self):
        now = datetime.now(UTC).isoformat()
        for giveaway_id, _, channel_id, _, ends_at in await get_active_giveaways():
            if ends_at > now:
                continue
            for guild in self.bot.guilds:
                if guild.get_channel(channel_id) is not None:
                    try:
                        await _finish_and_announce(guild, giveaway_id)
                    except discord.HTTPException:
                        logger.exception(
                            "Failed to announce expired giveaway %s", giveaway_id
                        )
                    break

    @app_commands.command(name="create", description="Создать розыгрыш")
    @app_commands.checks.cooldown(1, 15.0, key=lambda interaction: interaction.user.id)
    async def giveaway(
        self,
        interaction: discord.Interaction,
        название: app_commands.Range[str, 1, 100],
        приз: app_commands.Range[str, 1, 500],
        победителей: app_commands.Range[int, 1, 20],
        длительность: app_commands.Range[str, 2, 20],
    ):
        if not isinstance(interaction.user, discord.Member) or not _giveaway_access(interaction.user):
            await respond_v2(interaction,
                "Создавать розыгрыши может только настроенный старший состав.", ephemeral=True
            )
            return
        try:
            ends_at = datetime.now(UTC) + _duration(длительность)
        except ValueError as exc:
            await respond_v2(interaction, str(exc), ephemeral=True)
            return
        channel = get_text_channel(interaction.guild, "giveaways")
        if channel is None and isinstance(interaction.channel, discord.TextChannel):
            channel = interaction.channel
        if channel is None:
            await respond_v2(interaction, "Текстовый канал не найден.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        giveaway_id = await create_giveaway(
            название,
            приз,
            победителей,
            channel.id,
            interaction.user.id,
            ends_at.isoformat(),
            guild_id=interaction.guild.id,
        )
        view = GiveawayView(giveaway_id)
        try:
            embed = apply_accent(await giveaway_embed(giveaway_id), Accent.EVENTS)
            message = await send_v2(channel,
                embed=embed,
                view=view,
                component_marker=f"giveaway:{giveaway_id}",
            )
        except discord.HTTPException:
            logger.exception("Failed to publish giveaway %s", giveaway_id)
            await delete_giveaway(giveaway_id)
            await followup_v2(interaction,
                "Не удалось отправить розыгрыш.", ephemeral=True
            )
            return
        await set_giveaway_message(giveaway_id, message.id)
        try:
            await message.create_thread(
                name=f"Обсуждение: {название}"[:100],
                auto_archive_duration=1440,
            )
        except discord.HTTPException:
            logger.exception("Failed to create giveaway thread %s", giveaway_id)
        await followup_v2(interaction,
            f"Розыгрыш создан: {message.jump_url}", ephemeral=True
        )

    @app_commands.command(name="reroll", description="Повторно выбрать победителей")
    @app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
    async def giveaway_reroll(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        giveaway = await get_giveaway(id)
        if giveaway is None or giveaway[8] != "finished":
            await followup_v2(interaction,
                "Завершённый розыгрыш с таким ID не найден.", ephemeral=True
            )
            return
        if interaction.user.id != giveaway[6] and not _giveaway_access(interaction.user):
            await followup_v2(interaction, "Нет прав.", ephemeral=True)
            return
        try:
            await _finish_and_announce(interaction.guild, id, force_refresh=True)
        except discord.HTTPException:
            logger.exception("Failed to reroll giveaway %s", id)
            await followup_v2(interaction,
                "Не удалось повторить выбор.", ephemeral=True
            )
            return
        await followup_v2(interaction, "Победители выбраны повторно.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawaysCog(bot))
