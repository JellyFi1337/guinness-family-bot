import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.technical_status import TechnicalStatusView
from ui.messages import edit_interaction_message_v2, followup_v2, respond_v2
from ui.templates import notice
from repositories.operations import get_operational_health
from repositories.static_messages import set_static_panels_enabled
from services.metrics import metrics

from config import (
    AFK_VOICE_CHANNEL,
    AFK_VOICE_CHANNEL_ID,
    CATEGORY_IDS,
    CHANNELS,
)
from utils.database import get_database_pool_status, reset_database_data
from utils.discord_lookup import get_category, get_voice_channel
from utils.views import SafeView
from utils.access import bot_owner_only, is_bot_owner


logger = logging.getLogger(__name__)
def _is_protected(channel: discord.abc.GuildChannel) -> bool:
    if (
        channel.name.casefold() == AFK_VOICE_CHANNEL.casefold()
        or (AFK_VOICE_CHANNEL_ID and channel.id == AFK_VOICE_CHANNEL_ID)
    ):
        return True

    category = getattr(channel, "category", None)
    if category is None:
        return False
    developer_category_id = CATEGORY_IDS.get("developer_category")
    if developer_category_id:
        return category.id == developer_category_id
    return category.name == CHANNELS["developer_category"]


class DevCog(commands.Cog):
    dev = app_commands.Group(
        name="dev",
        description="Диагностика и технические уведомления",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @dev.command(
        name="health",
        description="Проверить состояние БД и фоновых сервисов",
    )
    @bot_owner_only()
    async def health(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        persisted = await get_operational_health()
        pool = get_database_pool_status()
        supervisor = self.bot.task_supervisor.snapshot()
        runtime_metrics = metrics.snapshot()
        stats_cog = self.bot.get_cog("WeeklyStatsCog")
        stats_ingestion = (
            stats_cog.ingestion_status()
            if stats_cog is not None and hasattr(stats_cog, "ingestion_status")
            else {"queued": 0, "retry": 0, "dropped": 0}
        )

        outbox = dict(persisted["outbox"])
        degraded = [
            row for row in persisted["tasks"] if row[1] != "healthy" or row[2]
        ]
        jobs = supervisor["jobs"]
        unfinished_outbox = sum(
            outbox.get(state, 0) for state in ("pending", "retry", "processing")
        )
        stopped_jobs = [name for name, state in jobs.items() if state != "running"]
        unhealthy = bool(
            degraded
            or outbox.get("dead")
            or unfinished_outbox
            or stopped_jobs
            or stats_ingestion["retry"]
            or stats_ingestion["dropped"]
        )
        details: list[tuple[str, str]] = [
            (
                str(pool.get("backend", "database")).replace(
                    "postgresql", "PostgreSQL"
                ).replace("sqlite", "SQLite"),
                (
                    f"Пул: **{pool['opened']}/{pool['size']}** соединений\n"
                    f"Используется: **{pool['in_use']}**\n"
                    f"Persistent: **{'да' if pool['persistent'] else 'нет'}**"
                ),
            ),
            (
                "Outbox",
                (
                    f"Ожидают: **{outbox.get('pending', 0) + outbox.get('retry', 0)}**\n"
                    f"В работе: **{outbox.get('processing', 0)}**\n"
                    f"Dead-letter: **{outbox.get('dead', 0)}**"
                ),
            ),
            (
                "Фоновые задачи",
                (
                    f"Запущено: **{sum(state == 'running' for state in jobs.values())}**\n"
                    f"Активно сейчас: **{supervisor['active']}**\n"
                    f"Maintenance: **{'да' if supervisor['paused'] else 'нет'}**"
                ),
            ),
            (
                "Статистика",
                (
                    f"В очереди: **{stats_ingestion['queued']}**\n"
                    f"На повторе: **{stats_ingestion['retry']}**\n"
                    f"Потеряно при перегрузке: **{stats_ingestion['dropped']}**"
                ),
            ),
        ]
        acquire = runtime_metrics["histograms"].get(
            f"database.{pool.get('backend', 'sqlite')}.acquire_ms",
            {},
        )
        command_latency = runtime_metrics["histograms"].get(
            "command.response_ms",
            {},
        )
        outbox_age = runtime_metrics["histograms"].get("outbox.age_ms", {})
        details.append(
            (
                "Задержки",
                (
                    f"DB acquire p95: **{float(acquire.get('p95', 0)):.1f} ms**\n"
                    f"Команды p95: **{float(command_latency.get('p95', 0)):.1f} ms**\n"
                    f"Возраст outbox p95: **{float(outbox_age.get('p95', 0)):.1f} ms**\n"
                    f"Outbox обработано: **"
                    f"{runtime_metrics['counters'].get('outbox.processed', 0)}**\n"
                    f"Discord HTTP ошибок: **"
                    f"{runtime_metrics['counters'].get('discord.http_errors', 0)}**"
                ),
            )
        )
        if degraded:
            details.append(
                (
                    "Последние сбои",
                    "\n".join(
                        f"`{name}` — {state}, ошибок подряд: {failures}"
                        for name, state, failures, _, _ in degraded[:10]
                    ),
                )
            )
        spec = notice(
            code="DEV.HEALTH",
            title="Состояние Guinness Bot",
            summary=(
                "Все основные сервисы работают в штатном режиме."
                if not unhealthy
                else "Обнаружены сервисы, требующие внимания владельца."
            ),
            details=tuple(details),
            next_step=f"Gateway latency: {round(self.bot.latency * 1000)} ms",
            accent=0xE67E22 if unhealthy else 0x2ECC71,
        )
        await followup_v2(interaction, embed=spec, ephemeral=True)

    @dev.command(
        name="status",
        description="Опубликовать уведомление о техническом состоянии бота",
    )
    @bot_owner_only()
    async def publish_technical_status(
        self, interaction: discord.Interaction
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await respond_v2(
                interaction,
                "Команду можно использовать только в текстовом канале сервера.",
                ephemeral=True,
            )
            return
        await respond_v2(
            interaction,
            "Выберите уведомление — оно будет опубликовано в этом канале.",
            view=TechnicalStatusView(
                owner_id=interaction.user.id,
                destination=channel,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="dev_reset_server",
        description=".",
    )
    @app_commands.default_permissions()
    @bot_owner_only()
    @app_commands.checks.cooldown(1, 300.0, key=lambda interaction: interaction.guild_id)
    async def dev_reset_server(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await respond_v2(interaction,
                "Команда доступна только на сервере.", ephemeral=True
            )
            return
        if not is_bot_owner(interaction.user):
            await respond_v2(interaction,
                "Команда доступна только владельцу бота.", ephemeral=True
            )
            return

        tickets = CHANNELS["tickets_category"]
        fundraisers = CHANNELS["birthday_fundraiser_category"]
        await respond_v2(interaction,
            "⚠️ **НЕОБРАТИМОЕ ДЕЙСТВИЕ**\n\n"
            "Будут удалены:\n"
            "• все сообщения во всех текстовых каналах;\n"
            "• все активные и архивные ветки во всех каналах;\n"
            f"• все каналы в категориях `{tickets}` и `{fundraisers}`;\n"
            f"• все пользовательские комнаты в категории `{CHANNELS['voice_create_category']}`;\n"
            f"• все каналы в категории `{CHANNELS['groups_category']}`;\n"
            "• все данные бота из базы данных.\n\n"
            f"Все каналы категории `{CHANNELS['developer_category']}` и "
            f"`#{AFK_VOICE_CHANNEL}` не затрагиваются.\n\n"
            "Схема БД и сами категории останутся. Для запуска нажмите подтверждение.",
            ephemeral=True,
            view=ConfirmServerResetView(interaction.user.id),
        )


class ConfirmServerResetView(SafeView):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id

    @discord.ui.button(
        label="ПОДТВЕРДИТЬ ПОЛНУЮ ОЧИСТКУ",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.author_id:
            await respond_v2(interaction,
                "Только инициатор может подтвердить.", ephemeral=True
            )
            return
        if not is_bot_owner(interaction.user):
            await respond_v2(interaction,
                "Команда доступна только владельцу бота.", ephemeral=True
            )
            return
        if interaction.guild is None:
            await edit_interaction_message_v2(interaction,
                content="Сервер больше недоступен.", view=None
            )
            return

        await edit_interaction_message_v2(interaction,
            content="Очистка запущена. Это может занять продолжительное время…",
            view=None,
        )

        supervisor = getattr(interaction.client, "task_supervisor", None)
        if supervisor is None:
            result = await self._perform_reset(interaction)
        else:
            # Static panels, statistics, expirations and the outbox must not
            # race a destructive reset. The supervisor resumes them even if
            # one of the Discord cleanup operations raises unexpectedly.
            async with supervisor.maintenance():
                result = await self._perform_reset(interaction)
        await followup_v2(interaction, result, ephemeral=True)

    async def _perform_reset(self, interaction: discord.Interaction) -> str:
        """Execute a reset while the caller owns the maintenance gate."""

        deleted_messages, deleted_channels, deleted_threads = 0, 0, 0
        failures: list[str] = []

        music = interaction.client.get_cog("MusicCog")
        if music is not None and hasattr(music, "shutdown_guild"):
            try:
                await music.shutdown_guild(
                    interaction.guild,
                    reason=f"server_reset:{interaction.user.id}",
                )
            except Exception:
                logger.exception("Failed to stop music before server reset")
                failures.append("музыкальный плеер")

        recruiting = interaction.client.get_cog("RecruitingCog")
        if recruiting is not None:
            try:
                await recruiting.revoke_all_invites(interaction.guild)
            except Exception:
                logger.exception("Failed to revoke personal recruiting invites")
                failures.append("персональные ссылки рекрутинга")

        threads = {
            thread.id: thread
            for thread in interaction.guild.threads
            if thread.parent is None
            or not _is_protected(thread.parent)
        }
        thread_parents = [
            *(
                channel
                for channel in interaction.guild.text_channels
                if not _is_protected(channel)
            ),
            *(
                channel
                for channel in getattr(interaction.guild, "forums", [])
                if not _is_protected(channel)
            ),
        ]
        for parent in thread_parents:
            try:
                async for thread in parent.archived_threads(limit=None):
                    threads[thread.id] = thread
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Failed to list archived threads in %s: %s", parent.id, exc)
                failures.append(f"архивные ветки в #{parent.name}")

        for thread in threads.values():
            try:
                await thread.delete(
                    reason=f"Full server reset requested by {interaction.user}"
                )
                deleted_threads += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Failed to delete thread %s: %s", thread.id, exc)
                failures.append(f"ветка #{thread.name}")

        # Work on a snapshot because channels are deleted later in this operation.
        for channel in list(interaction.guild.text_channels):
            if _is_protected(channel):
                continue
            try:
                deleted = await channel.purge(
                    limit=None,
                    bulk=True,
                    reason=f"Full server reset requested by {interaction.user}",
                )
                deleted_messages += len(deleted)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Failed to purge channel %s: %s", channel.id, exc)
                failures.append(f"сообщения в #{channel.name}")

        voice_lobby = get_voice_channel(interaction.guild, "voice_create_lobby")
        categories = {
            category
            for category in (
                get_category(interaction.guild, "tickets_category"),
                get_category(interaction.guild, "birthday_fundraiser_category"),
                get_category(interaction.guild, "voice_create_category"),
                get_category(interaction.guild, "groups_category"),
                voice_lobby.category if voice_lobby else None,
            )
            if category is not None
        }
        for category in categories:
            for channel in list(category.channels):
                if _is_protected(channel):
                    continue
                if voice_lobby and channel.id == voice_lobby.id:
                    continue
                try:
                    await channel.delete(
                        reason=f"Full server reset requested by {interaction.user}"
                    )
                    deleted_channels += 1
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning("Failed to delete channel %s: %s", channel.id, exc)
                    failures.append(f"канал #{channel.name}")

        database_reset = False
        stats_cog = interaction.client.get_cog("WeeklyStatsCog")
        try:
            if stats_cog is not None and hasattr(stats_cog, "discard_pending_events"):
                discarded = await stats_cog.discard_pending_events()
                if discarded:
                    logger.info(
                        "Discarded %d queued statistics event(s) before reset",
                        discarded,
                    )
            await reset_database_data()
            # This durable flag is written after the reset because guild_settings
            # is part of the cleared data. Periodic workers may continue updating
            # statistics and business state, but cannot recreate panels until
            # /admin setup explicitly enables publication again.
            await set_static_panels_enabled(interaction.guild.id, False)
            database_reset = True
        except Exception:
            logger.exception("Failed to reset the bot database")
            failures.append("база данных")

        if (
            database_reset
            and stats_cog is not None
            and hasattr(stats_cog, "restore_after_reset")
        ):
            try:
                await stats_cog.restore_after_reset()
            except Exception:
                logger.exception("Failed to restore voice statistics after reset")
                failures.append("восстановление голосовой статистики")

        if database_reset and recruiting is not None:
            try:
                await recruiting.ensure_guild(interaction.guild)
                await recruiting.refresh_stats(interaction.guild)
            except Exception:
                logger.exception("Failed to restore recruiting after database reset")
                failures.append("восстановление рекрутинга")

        result = (
            "Очистка завершена.\n"
            f"Удалено сообщений: **{deleted_messages}**\n"
            f"Удалено веток: **{deleted_threads}**\n"
            f"Удалено каналов: **{deleted_channels}**\n"
            f"База данных: **{'очищена' if database_reset else 'ошибка'}**"
        )
        if failures:
            result += "\nНе удалось обработать: " + ", ".join(failures[:15])
            if len(failures) > 15:
                result += f" и ещё {len(failures) - 15}"

        return result

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.author_id:
            await respond_v2(interaction,
                "Только инициатор может отменить операцию.", ephemeral=True
            )
            return
        await edit_interaction_message_v2(interaction, content="Операция отменена.", view=None)


async def setup(bot: commands.Bot):
    await bot.add_cog(DevCog(bot))
    logger.warning("DevCog loaded; destructive command dev_reset_server is enabled")
