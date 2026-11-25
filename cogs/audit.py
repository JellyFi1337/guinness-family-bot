"""Read-only Discord surface for the decision journal."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from services.audit import (
    LIST_LIMIT,
    AuditError,
    Decision,
    get_decision,
    list_decisions,
)
from ui.command_registry import code
from ui.message_specs import MessageSpec, MessageTone
from ui.messages import (
    edit_interaction_message_v2,
    followup_spec,
    followup_v2,
    respond_v2,
)
from ui.pagination import paginate_lines
from ui.templates import system_error
from core.permissions import Permission
from utils.access import requires
from utils.views import SafeView


def _format_line(row: Decision) -> str:
    created = row.created_at[:19].replace("T", " ")
    return (
        f"`#{row.id}` {created} `{row.action}` "
        f"{row.actor_name} {row.subject_type}:{row.subject_id}"
    )


def _show_spec(row: Decision) -> MessageSpec:
    details = (
        ("Когда", row.created_at),
        ("Кто", f"{row.actor_name} ({row.actor_id})"),
        ("Действие", row.action),
        ("Объект", f"{row.subject_type}:{row.subject_id}"),
        ("Источник", row.source),
        ("Было", row.before or "—"),
        ("Стало", row.after or "—"),
        ("Основание", row.reason or "—"),
    )
    return MessageSpec(
        code="AUDIT.SHOW",
        title=f"Решение #{row.id}",
        summary=row.action,
        details=details,
        tone=MessageTone.NEUTRAL,
        reference=row.correlation_id or current_reference(),
    )


class AuditListView(SafeView):
    def __init__(self, pages: list[str], owner_id: int) -> None:
        super().__init__(timeout=180.0)
        self.pages = pages
        self.owner_id = owner_id
        self.index = 0
        self._sync()

    def _sync(self) -> None:
        self.previous_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.owner_id:
            await respond_v2(
                interaction,
                "Этот список открыт другим участником.",
                ephemeral=True,
            )
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await edit_interaction_message_v2(
            interaction, self.pages[self.index], view=self
        )

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)


class AuditCog(commands.Cog):
    audit = app_commands.Group(
        name="audit",
        description="Журнал административных решений",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @audit.command(name="list", description="Показать журнал решений")
    @app_commands.describe(
        actor="Кто принял решение",
        action="Код действия, например SETTINGS.SET",
        subject_type="Тип объекта, например setting",
        subject_id="Идентификатор объекта",
        since="Начало периода, ГГГГ-ММ-ДД",
        until="Конец периода, ГГГГ-ММ-ДД",
    )
    @requires(Permission.AUDIT_VIEW)
    async def list_command(
        self,
        interaction: discord.Interaction,
        actor: discord.User | None = None,
        action: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="AUDIT.GUILD_ONLY",
                    summary=(
                        "Журнал решений доступен только на сервере. "
                        "Записи не прочитаны."
                    ),
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        try:
            rows = await list_decisions(
                guild_id,
                actor_id=actor.id if actor is not None else None,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                since=since,
                until=until,
            )
        except AuditError as exc:
            await followup_spec(
                interaction,
                system_error(
                    code=exc.code,
                    summary=str(exc),
                    retryable=exc.retryable,
                    reference=current_reference(),
                ),
            )
            return
        if not rows:
            await followup_spec(
                interaction,
                MessageSpec(
                    code="AUDIT.EMPTY",
                    title="Журнал решений",
                    summary="По заданным фильтрам записей нет.",
                    next_step="Измените фильтры или период и повторите.",
                    tone=MessageTone.NEUTRAL,
                    reference=current_reference(),
                ),
            )
            return
        lines = [_format_line(row) for row in rows]
        if len(rows) >= LIST_LIMIT:
            lines.append(f"Показаны последние {LIST_LIMIT} записей.")
        pages = paginate_lines(lines)
        headed: list[str] = []
        total = len(pages)
        for index, page in enumerate(pages, start=1):
            headed.append(
                f"Журнал решений · страница {index} из {total}\n\n{page}\n\n"
                f"Подробности: {code('audit show')}."
            )
        view = (
            AuditListView(headed, interaction.user.id) if len(headed) > 1 else None
        )
        await followup_v2(interaction, headed[0], view=view, ephemeral=True)

    @audit.command(name="show", description="Показать одну запись журнала")
    @app_commands.describe(decision_id="Номер записи из журнала")
    @requires(Permission.AUDIT_VIEW)
    async def show_command(
        self,
        interaction: discord.Interaction,
        decision_id: app_commands.Range[int, 1, None],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        if guild_id is None:
            await followup_spec(
                interaction,
                system_error(
                    code="AUDIT.GUILD_ONLY",
                    summary=(
                        "Журнал решений доступен только на сервере. "
                        "Запись не прочитана."
                    ),
                    retryable=False,
                    reference=current_reference(),
                ),
            )
            return
        row = await get_decision(guild_id, int(decision_id))
        if row is None:
            await followup_spec(
                interaction,
                system_error(
                    code="AUDIT.NOT_FOUND",
                    summary=(
                        "Запись журнала с таким номером не найдена. "
                        "Данные не изменились; проверьте номер в "
                        f"{code('audit list')} и повторите."
                    ),
                    retryable=True,
                    reference=current_reference(),
                ),
            )
            return
        await followup_spec(interaction, _show_spec(row))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AuditCog(bot))
