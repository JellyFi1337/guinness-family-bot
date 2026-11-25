import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import COMMAND_COOLDOWN_DEFAULTS
from repositories.operations import OutboxOperation
from repositories.static_messages import set_static_panels_enabled
from cogs.config_panel import deliver_settings_export
from services.config_panel import export_settings
from services.preflight import (
    PreflightReport,
    Severity,
    run_preflight,
)
from ui.command_registry import cmd, code
from ui.templates import admin_receipt, notice
from repositories.treasury import (
    add_treasury_transaction,
    set_treasury_balance_with_transaction,
)
from ui.messages import followup_v2, respond_v2
from services.cooldowns import command_cooldowns

from cogs.forms import (
    send_treasury_transaction_message,
    update_happy_birthday_message,
    update_treasury_message,
)
from cogs.interactions import SetupReport, setup_all_persistent_messages
from config import TREASURY_OWNER_ROLE_ID, TREASURY_OWNER_ROLE_NAME
from utils.database import (
    add_birthday,
)
from utils.validation import (
    normalize_birthday,
    normalize_positive_amount,
    normalize_treasury_action,
)
from utils.embeds import (
    ACCENT_GOLD,
    ERROR_COLOR,
    SUCCESS_COLOR,
    error_embed,
    success_embed,
)
from utils.access import administrator_or_owner, is_bot_owner


logger = logging.getLogger(__name__)

_PANEL_STATUS_LABELS = {
    "updated": "обновлена",
    "merged": "объединена",
    "missing": "канал не найден",
    "failed": "ошибка",
}
_SEVERITY_ICONS = {
    Severity.CRITICAL: "🔴",
    Severity.WARNING: "🟡",
    Severity.OK: "🟢",
}
_PREFLIGHT_AREA_TITLES = {
    "intents": "Privileged intents",
    "channels": "Каналы",
    "categories": "Категории",
    "roles": "Роли",
    "access": "Списки доступа",
    "permissions": "Права бота",
    "runtime": "Окружение",
    "database": "База данных",
    "profile": "Профиль семьи",
    "grants": "Полномочия ролей",
    "panels": "Каналы панелей",
    "online": "Источник онлайна",
    "outbox": "Очередь outbox",
    "tasks": "Фоновые задачи",
}
_PREFLIGHT_VERDICT = {
    "ready": ("Preflight: готов к работе", "Можно выпускать."),
    "limited": (
        "Preflight: работает с ограничениями",
        "Блокеров нет, но есть ограничения. Их можно устранить без остановки.",
    ),
    "not_ready": (
        "Preflight: не готов",
        "Исправьте блокеры и повторите проверку.",
    ),
}


def _setup_report_embed(report: SetupReport):
    """Render the per-panel outcome instead of one unconditional success line."""

    problems = report.missing + report.failed
    details: list[tuple[str, str]] = []
    if problems:
        details.append(
            (
                "Требуют внимания",
                "\n".join(
                    f"• **{item.title}** — {_PANEL_STATUS_LABELS[item.status]}: {item.detail}"
                    for item in problems
                )[:1024],
            )
        )
    if report.updated:
        details.append(
            ("Обновлены", ", ".join(item.title for item in report.updated)[:1024])
        )
    if report.merged:
        details.append(
            (
                "Объединены в общую панель",
                ", ".join(item.title for item in report.merged)[:1024],
            )
        )
    return notice(
        code="ADMIN.SETUP",
        title="Настройка завершена" if report.ok else "Настройка завершена частично",
        summary=(
            f"Обновлено панелей: **{len(report.updated)}**"
            + (f" · объединено: **{len(report.merged)}**" if report.merged else "")
            + (f" · с проблемами: **{len(problems)}**" if problems else "")
        ),
        details=tuple(details),
        next_step=(
            f"Каналы, роли и профиль настраиваются через {cmd('setup start')}."
            if report.ok
            else f"Исправьте причины и повторите {cmd('admin setup')}."
        ),
        accent=SUCCESS_COLOR if report.ok else ERROR_COLOR,
    )


def _preflight_embeds(report: PreflightReport):
    critical = report.critical()
    warnings = report.warnings()
    details: list[tuple[str, str]] = []
    for area, title in _PREFLIGHT_AREA_TITLES.items():
        problems = report.by_area(area)
        if not problems:
            details.append((title, "🟢 без замечаний"))
            continue
        lines = [
            f"{_SEVERITY_ICONS[item.severity]} **{item.target}** — {item.message}"
            for item in problems
        ]
        details.append((title, _clip_lines(lines)))
    verdict = report.verdict()
    title, fallback_next = _PREFLIGHT_VERDICT[verdict]
    next_step = (
        fallback_next
        if verdict == "ready"
        else f"{fallback_next} Повторите {cmd('admin preflight')}."
    )
    cards = [
        notice(
            code="ADMIN.PREFLIGHT",
            title=title,
            summary=(
                f"🔴 критично: **{len(critical)}** · "
                f"🟡 предупреждений: **{len(warnings)}** · "
                f"по имени вместо ID: **{len(report.name_only_routes)}**"
            ),
            details=tuple(details),
            next_step=next_step,
            accent=SUCCESS_COLOR if verdict == "ready" else ERROR_COLOR,
        )
    ]
    if report.name_only_routes:
        cards.append(
            notice(
                code="ADMIN.PREFLIGHT_ROUTES",
                title="Маршруты, работающие только по имени",
                summary=(
                    "Переименование любого из этих объектов выключит функцию. "
                    f"Готовый блок с ID выдаёт {code('config export')}."
                ),
                details=(
                    (
                        f"Всего: {len(report.name_only_routes)}",
                        _clip_lines([f"• {item}" for item in report.name_only_routes]),
                    ),
                ),
                next_step=f"Заполните ID через {code('config export')}.",
                accent=ERROR_COLOR,
            )
        )
    return cards


def _clip_lines(lines: list[str], limit: int = 1024) -> str:
    """Join lines without ever cutting a line in half mid-report."""

    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit - 24:
            out.append(f"… и ещё {len(lines) - len(out)}")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "—"


_COOLDOWN_COMMAND_CHOICES = [
    app_commands.Choice(name="Help-запрос", value="help_request"),
    app_commands.Choice(name="Справка /help", value="help"),
    app_commands.Choice(name="Обычный сбор", value="summit_list"),
    app_commands.Choice(name="Создание события", value="event_create"),
    app_commands.Choice(name="Запуск контракта", value="contract_start"),
]


class AdminCog(
    commands.GroupCog,
    group_name="admin",
    group_description="Администрирование Guinness Bot",
):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.register("treasury.publish", self._publish_treasury_transaction)

    async def cog_unload(self) -> None:
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.unregister("treasury.publish")

    @staticmethod
    def _treasury_transaction_embed(payload: dict):
        """The financial journal entry, built from the AdminReceipt template.

        An audit line is only auditable if it says what the value used to be.
        The template makes that structural rather than something each call site
        remembers to include.
        """

        from utils.embeds import apply_accent

        number = int(payload["transaction_number"])
        user_mention = f"<@{int(payload['user_id'])}>"
        reference = f"№{number:04d}"
        if payload.get("kind") == "set":
            spec = admin_receipt(
                code="TREASURY.SET",
                title=f"Казна • Транзакция {reference}",
                actor=user_mention,
                changed="Баланс казны установлен вручную",
                before=f"{int(payload['old_amount']):,}$",
                after=f"{int(payload['new_amount']):,}$",
                reason=f"Изменение: **{int(payload['difference']):+,}$**",
                reference=reference,
            )
            return apply_accent(spec, ACCENT_GOLD)
        action = str(payload["action"])
        amount = int(payload["amount"])
        is_removal = action == "remove"
        spec = admin_receipt(
            code="TREASURY.REMOVE" if is_removal else "TREASURY.ADD",
            title=f"Казна • Транзакция {reference}",
            actor=user_mention,
            changed="Списание из казны" if is_removal else "Пополнение казны",
            after=f"{'−' if is_removal else '+'}{amount:,}$",
            reason=str(payload["reason"]),
            reference=reference,
        )
        return apply_accent(spec, ERROR_COLOR if is_removal else SUCCESS_COLOR)

    async def _publish_treasury_transaction(
        self, operation: OutboxOperation
    ) -> int:
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(
                f"Guild {operation.guild_id} is unavailable for treasury publication"
            )
        number = int(operation.payload["transaction_number"])
        message = await send_treasury_transaction_message(
            guild,
            self._treasury_transaction_embed(operation.payload),
            transaction_number=number,
        )
        # The canonical panel is itself reconciled and deduplicated by the
        # shared static-message service.
        await update_treasury_message(guild)
        return message.id

    # ==================== SLASH КОМАНДЫ ====================
    @app_commands.command(
        name="preflight",
        description="Проверить готовность сервера, прав и окружения перед релизом",
    )
    @administrator_or_owner()
    @app_commands.checks.cooldown(1, 30.0, key=lambda interaction: interaction.guild_id)
    async def preflight(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        report = await run_preflight(interaction.client, interaction.guild)
        embeds = _preflight_embeds(report)
        await followup_v2(interaction, embed=embeds[0], ephemeral=True)
        for extra in embeds[1:]:
            await followup_v2(interaction, embed=extra, ephemeral=True)

    @app_commands.command(
        name="config_ids",
        description="Устаревшая выгрузка актуальных настроек",
    )
    @administrator_or_owner()
    @app_commands.checks.cooldown(1, 30.0, key=lambda interaction: interaction.guild_id)
    async def config_ids(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_v2(
                interaction,
                notice(
                    code="ADMIN.CONFIG_IDS_GUILD_ONLY",
                    title="Выгрузка настроек",
                    summary="Выгрузка доступна только на сервере.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        text = await export_settings(guild.id)
        await deliver_settings_export(
            interaction,
            text,
            summary=(
                f"Команда устарела. Дальше используйте {cmd('config export')}. "
                "Секреты в файл не попали."
            ),
        )

    @app_commands.command(
        name="setup",
        description="Разместить постоянные панели (администратор/владелец бота)",
    )
    @administrator_or_owner()
    @app_commands.checks.cooldown(1, 60.0, key=lambda interaction: interaction.guild_id)
    async def setup_guinness_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await set_static_panels_enabled(interaction.guild.id, True)
            report = await setup_all_persistent_messages(interaction.guild)
            recruiting = interaction.client.get_cog("RecruitingCog")
            if recruiting is not None:
                await recruiting.refresh_stats(interaction.guild)
            await followup_v2(
                interaction,
                embed=_setup_report_embed(report),
                ephemeral=True,
            )
        except Exception:
            logger.exception("Failed to set up persistent Guinness messages")
            await followup_v2(
                interaction,
                embed=error_embed(
                    "Настройка не завершена",
                    "Проверьте права бота и журнал ошибок, затем повторите "
                    f"команду. Диагностика: {code('admin preflight')}.",
                ),
                ephemeral=True,
            )

    @app_commands.command(name="birthday_add", description="Добавить день рождения пользователю")
    @administrator_or_owner()
    async def add_birthday_admin(self, interaction: discord.Interaction, user: discord.Member, date: str):
        try:
            date = normalize_birthday(date)
        except ValueError as exc:
            await respond_v2(interaction, str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await add_birthday(user.id, date, user.display_name)
        await update_happy_birthday_message(interaction.guild)
        await followup_v2(interaction, f"✅ День рождения {user.mention} ({date}) добавлен.", ephemeral=True)

    @app_commands.command(name="birthday_remove", description="Удалить день рождения пользователя")
    @administrator_or_owner()
    async def remove_birthday_admin(self, interaction: discord.Interaction, user: discord.Member):
        from utils.database import remove_birthday
        await interaction.response.defer(ephemeral=True)
        await remove_birthday(user.id)
        from cogs.forms import update_happy_birthday_message
        await update_happy_birthday_message(interaction.guild)
        await followup_v2(interaction, f"✅ День рождения {user.mention} удалён.", ephemeral=True)

    @app_commands.command(name="treasury_set", description="Установить баланс казны")
    async def set_treasury(self, interaction: discord.Interaction, amount: int):
        member = interaction.user
        allowed = is_bot_owner(member) or (isinstance(member, discord.Member) and any(
            role.id == TREASURY_OWNER_ROLE_ID
            if TREASURY_OWNER_ROLE_ID is not None
            else role.name == TREASURY_OWNER_ROLE_NAME
            for role in member.roles
        ))
        if not allowed:
            await respond_v2(interaction,
                f"Команда доступна только роли **{TREASURY_OWNER_ROLE_NAME}**.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if amount < 0:
                raise ValueError("Баланс казны не может быть отрицательным")
            tx_number, old_amount, difference = (
                await set_treasury_balance_with_transaction(
                    interaction.user.id,
                    interaction.user.name,
                    amount,
                    guild_id=interaction.guild.id,
                    interaction_id=interaction.id,
                )
            )
            result_text = (
                f"✅ Значение казны установлено: **{amount:,}$**. "
                "Журнал и панель обновляются автоматически."
                if difference
                else f"ℹ️ Значение казны уже составляет **{amount:,}$**."
            )
            await followup_v2(interaction,
                result_text,
                ephemeral=True,
            )
        except Exception:
            logger.exception("Failed to set treasury balance")
            await followup_v2(interaction,
                "❌ Не удалось установить значение казны.", ephemeral=True
            )

    @app_commands.command(name="transaction_add", description="Добавить транзакцию")
    @administrator_or_owner()
    async def add_transaction(
        self,
        interaction: discord.Interaction,
        action: str,
        amount: int,
        reason: app_commands.Range[str, 1, 200],
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            action = normalize_treasury_action(action)
            amount = normalize_positive_amount(amount)
        except ValueError as exc:
            await followup_v2(interaction, str(exc), ephemeral=True)
            return

        try:
            tx_number = await add_treasury_transaction(
                interaction.user.id,
                interaction.user.name,
                amount,
                action,
                reason,
                guild_id=interaction.guild.id,
                interaction_id=interaction.id,
            )
            await followup_v2(interaction,
                f"✅ Транзакция-{tx_number:04d} сохранена. "
                "Журнал и панель обновляются автоматически.",
                ephemeral=True,
            )
        except Exception:
            logger.exception("Failed to add treasury transaction")
            await followup_v2(interaction,
                "❌ Не удалось добавить транзакцию.", ephemeral=True
            )

    @app_commands.command(
        name="cooldown_access",
        description="Временно снять или вернуть cooldown пользователю",
    )
    @app_commands.describe(
        участник="Пользователь, для которого меняется cooldown",
        команда="Команда или действие",
        режим="Одно использование, период или возврат cooldown",
        минуты="Длительность временного снятия (только для режима «На время»)",
    )
    @app_commands.choices(
        команда=_COOLDOWN_COMMAND_CHOICES,
        режим=[
            app_commands.Choice(name="Разрешить один раз", value="once"),
            app_commands.Choice(name="Снять на время", value="timed"),
            app_commands.Choice(name="Вернуть cooldown", value="restore"),
        ],
    )
    @administrator_or_owner()
    async def cooldown_access(
        self,
        interaction: discord.Interaction,
        участник: discord.Member,
        команда: app_commands.Choice[str],
        режим: app_commands.Choice[str],
        минуты: app_commands.Range[int, 1, 1440] | None = None,
    ):
        if команда.value not in COMMAND_COOLDOWN_DEFAULTS:
            await respond_v2(
                interaction,
                "Для выбранной команды cooldown не поддерживается.",
                ephemeral=True,
            )
            return
        duration_minutes = int(минуты) if минуты is not None else None
        if режим.value == "timed" and duration_minutes is None:
            await respond_v2(
                interaction,
                "Для временного снятия укажите длительность в минутах.",
                ephemeral=True,
            )
            return
        if режим.value == "once":
            command_cooldowns.grant_once(
                interaction.guild_id,
                команда.value,
                участник.id,
            )
            result = "следующий запуск пройдёт без ожидания"
        elif режим.value == "timed":
            assert duration_minutes is not None
            command_cooldowns.grant_for(
                interaction.guild_id,
                команда.value,
                участник.id,
                duration_minutes * 60,
            )
            result = f"ограничение снято на **{duration_minutes} мин.**"
        else:
            removed = command_cooldowns.restore(
                interaction.guild_id,
                команда.value,
                участник.id,
            )
            result = (
                "стандартный cooldown восстановлен"
                if removed
                else "индивидуального исключения не было; действует стандартный cooldown"
            )
        await respond_v2(
            interaction,
            embed=success_embed(
                "Cooldown пользователя обновлён",
                f"Участник: {участник.mention}\n"
                f"Команда: **{команда.name}**\n"
                f"Результат: {result}.",
            ),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
