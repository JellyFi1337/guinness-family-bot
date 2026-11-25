from __future__ import annotations

import logging
import time

import discord
from discord import app_commands

from config import is_authorized_guild
from core.log_context import (
    bind_interaction,
    collect_interaction_options,
    current_reference,
    notify_critical_error,
    sanitize_text,
)
from infrastructure.database import DATABASE_OPERATIONAL_ERRORS
from ui.message_specs import MessageSpec, MessageTone
from ui.templates import system_error
from ui.messages import followup_spec, respond_spec, respond_v2
from utils.access import MissingPermission, is_bot_owner
from services.metrics import metrics


logger = logging.getLogger(__name__)


class GuinnessCommandTree(app_commands.CommandTree):
    """Uniform, Components v2-aware application command error boundary."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bind_interaction(interaction)
        interaction.extras["metrics.command_started_at"] = time.perf_counter()
        if not is_authorized_guild(interaction.guild_id):
            await respond_v2(
                interaction,
                "Эта команда недоступна на данном сервере.",
                ephemeral=True,
            )
            logger.warning(
                "Rejected interaction outside configured guild command=%s guild=%s user=%s",
                getattr(interaction.command, "qualified_name", "unknown"),
                interaction.guild_id,
                interaction.user.id,
            )
            return False
        supervisor = getattr(self.client, "task_supervisor", None)
        if supervisor is not None and supervisor.paused and not is_bot_owner(interaction.user):
            await respond_v2(
                interaction,
                "Бот выполняет техническое обслуживание. Повторите команду позже.",
                ephemeral=True,
            )
            return False
        return True

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        bind_interaction(interaction, source="command")
        reference = current_reference()
        spec: MessageSpec
        if isinstance(error, app_commands.CommandOnCooldown):
            spec = MessageSpec(
                code="COMMAND.COOLDOWN",
                title="Команда не выполнена",
                summary=(
                    "Команда используется слишком часто. "
                    f"Повторите через {error.retry_after:.1f} сек."
                ),
                next_step="Дождитесь окончания задержки и повторите.",
                tone=MessageTone.WARNING,
                reference=reference,
            )
        elif isinstance(error, app_commands.MissingPermissions):
            spec = MessageSpec(
                code="COMMAND.USER_PERMISSIONS",
                title="Команда недоступна",
                summary="У вас недостаточно прав для использования этой команды.",
                next_step="Обратитесь к старшему составу, если доступ нужен.",
                tone=MessageTone.WARNING,
                reference=reference,
            )
        elif isinstance(error, app_commands.BotMissingPermissions):
            spec = MessageSpec(
                code="COMMAND.BOT_PERMISSIONS",
                title="Команда не выполнена",
                summary="Боту недостаточно Discord-прав для выполнения команды.",
                next_step="Сообщите администрации: проверьте /admin preflight.",
                tone=MessageTone.ERROR,
                reference=reference,
            )
        elif isinstance(error, MissingPermission):
            spec = error.to_spec(reference)
        elif isinstance(error, app_commands.CheckFailure):
            spec = MessageSpec(
                code="COMMAND.UNAVAILABLE",
                title="Команда недоступна",
                summary="Команда недоступна для вашей роли или в этом канале.",
                next_step="Используйте её в предназначенном канале.",
                tone=MessageTone.WARNING,
                reference=reference,
            )
        else:
            original = getattr(error, "original", error)
            if isinstance(original, discord.HTTPException):
                metrics.increment("discord.http_errors")
            command_name = getattr(interaction.command, "qualified_name", "unknown")
            user = interaction.user
            channel = interaction.channel
            logger.error(
                "Unhandled application command error type=%s error=%s "
                "command=%s user_id=%s user_name=%s guild_id=%s "
                "channel_id=%s channel_name=%s interaction_id=%s "
                "options=%s reference=%s",
                type(original).__name__,
                sanitize_text(original),
                command_name,
                user.id,
                getattr(user, "name", ""),
                interaction.guild_id,
                interaction.channel_id,
                getattr(channel, "name", ""),
                interaction.id,
                collect_interaction_options(interaction),
                reference,
                exc_info=(type(original), original, original.__traceback__),
            )
            if isinstance(original, DATABASE_OPERATIONAL_ERRORS):
                await notify_critical_error(
                    self.client,
                    key="db:failure",
                    title="Отказ базы данных",
                    summary="Команда не выполнена из-за ошибки базы данных.",
                    detail=(
                        f"type={type(original).__name__} command={command_name} "
                        f"reference={reference}"
                    ),
                    next_step="Проверьте доступность PostgreSQL и назовите reference.",
                )
            else:
                await notify_critical_error(
                    self.client,
                    key="command:unhandled",
                    title="Необработанная ошибка команды",
                    summary=f"Команда `{command_name}` завершилась ошибкой.",
                    detail=(
                        f"type={type(original).__name__} command={command_name} "
                        f"reference={reference}"
                    ),
                    next_step="Проверьте журнал и назовите reference.",
                )
            # A Discord-side failure usually means the command never reached its
            # durable step, so retrying is safe. Anything else may have written
            # and then failed on a side effect, and a blind retry can duplicate
            # it — say so instead of telling everyone "действие не завершено".
            spec = system_error(
                code="COMMAND.INTERNAL",
                summary=(
                    "Discord отклонил запрос, изменения не сохранены."
                    if isinstance(original, discord.HTTPException)
                    else "Произошла внутренняя ошибка."
                ),
                retryable=isinstance(original, discord.HTTPException),
                reference=reference,
            )
        try:
            if interaction.response.is_done():
                await followup_spec(interaction, spec, ephemeral=True)
            else:
                await respond_spec(interaction, spec, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            logger.warning("Could not deliver application command error response")
