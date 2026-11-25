"""Slash surface for the primary-setup wizard."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core.log_context import current_reference
from core.permissions import Permission
from core.profile import KNOWN_DIRECTIONS, KNOWN_UPGRADES
from repositories.operations import OutboxOperation
from services.provisioning import (
    ACTION_BIND,
    ACTION_CREATE,
    ACTION_SKIP,
    KIND_CATEGORY,
    KIND_CHANNEL,
    KIND_ROLE,
    KIND_VOICE,
    PROVISION_CATEGORY,
    PROVISION_CHANNEL,
    PROVISION_ROLE,
    GuildObject,
    GuildSnapshot,
    SetupError,
    WizardState,
    apply_confirmed_plan,
    assert_config_manage,
    build_plan,
    choose_object,
    known_panels,
    load_wizard_state,
    mark_confirmed,
    record_created_object,
    refresh_scan,
    role_inventory,
    save_wizard_state,
    set_panels,
    set_profile_fields,
    set_role_permissions,
    set_step,
    setup_status,
    start_or_resume,
    structure_inventory,
    upgrade_label,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec, MessageTone
from ui.messages import edit_interaction_message_v2, respond_spec, respond_v2
from ui.navigation import ConfirmDialog
from ui.templates import admin_receipt, notice, system_error
from utils.access import requires
from utils.views import SafeView

_DIRECTION_LABELS = {
    "legal": "Легальное",
    "criminal": "Криминальное",
    "unset": "Не задано",
}
_STEP_TITLES = {
    "profile": "Профиль",
    "structure": "Структура",
    "roles": "Роли",
    "permissions": "Полномочия",
    "panels": "Панели",
    "summary": "Итог",
    "done": "Готово",
}


def snapshot_guild(guild: discord.Guild) -> GuildSnapshot:
    channels: list[GuildObject] = []
    categories: list[GuildObject] = []
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            categories.append(GuildObject(int(channel.id), channel.name, "category"))
            continue
        kind = "voice" if isinstance(channel, discord.abc.GuildChannel) and getattr(
            channel, "type", None
        ) in {discord.ChannelType.voice, discord.ChannelType.stage_voice} else "text"
        channels.append(GuildObject(int(channel.id), channel.name, kind))
    roles = tuple(
        GuildObject(int(role.id), role.name, "role")
        for role in guild.roles
        if not role.is_default()
    )
    return GuildSnapshot(
        guild_id=int(guild.id),
        channels=tuple(channels),
        categories=tuple(categories),
        roles=roles,
    )


def _error_spec(exc: SetupError, *, retryable: bool) -> MessageSpec:
    return system_error(
        code=exc.code,
        summary=str(exc),
        retryable=retryable,
        reference=current_reference(),
    )


def _wizard_spec(state: WizardState, snapshot: GuildSnapshot) -> MessageSpec:
    plan = build_plan(state, snapshot)
    details = [
        ("Шаг", _STEP_TITLES.get(state.step, state.step)),
        ("Серия", state.correlation_id or "—"),
        ("План", plan.preview_text()),
    ]
    if state.step == "profile":
        upgrades = state.profile.get("enabled_upgrades") or []
        details.extend(
            (
                ("Сервер", str(state.profile.get("server_key") or "memphis")),
                ("Семья", str(state.profile.get("family_name") or "—")),
                ("Тег", str(state.profile.get("family_tag") or "—")),
                (
                    "Направление",
                    _DIRECTION_LABELS.get(str(state.profile.get("direction") or "unset"), "—"),
                ),
                ("Пояс", str(state.profile.get("timezone") or "—")),
                (
                    "Сезон",
                    f"{state.profile.get('season_start') or '—'} — "
                    f"{state.profile.get('season_end') or '—'}",
                ),
                (
                    "Улучшения",
                    ", ".join(upgrade_label(str(item)) for item in upgrades) or "не заданы",
                ),
            )
        )
    elif state.step in {"structure", "roles"}:
        attention = ", ".join(plan.attention) if plan.attention else "нет"
        details.append(("Требует внимания", attention))
    elif state.step == "permissions":
        filled = sum(1 for values in state.permissions.values() if values)
        details.append(("Ролей с полномочиями", str(filled)))
    elif state.step == "panels":
        enabled = [
            spec.title
            for spec in known_panels()
            if bool((state.panels.get(spec.key) or {}).get("enabled"))
        ]
        details.append(("К развёртыванию", ", ".join(enabled) or "нет"))
    elif state.step in {"summary", "done"}:
        applied = state.applied
        details.extend(
            (
                ("Создано / в очереди", ", ".join(applied.get("created") or []) or "нет"),
                ("Привязано", ", ".join(applied.get("bound") or []) or "нет"),
                ("Пропущено", ", ".join(applied.get("skipped") or []) or "нет"),
            )
        )
    next_step = {
        "profile": "Заполните карточку семьи и нажмите «Далее».",
        "structure": "Для каждого канала выберите привязать, создать или пропустить.",
        "roles": "Привяжите игровые и служебные роли или пропустите лишние.",
        "permissions": "Проверьте предзаполненные полномочия и нажмите «Далее».",
        "panels": "Отметьте панели, которые нужно разместить.",
        "summary": "Проверьте план и подтвердите применение.",
        "done": f"Структура записана. Панели можно обновить через {cmd('admin setup')}.",
    }.get(state.step, "Продолжите мастер.")
    return notice(
        code="SETUP.WIZARD",
        title="Мастер настройки",
        summary=plan.preview_text() if state.step == "summary" else "Первичная настройка сервера.",
        details=tuple(details),
        next_step=next_step,
        tone=MessageTone.NEUTRAL,
        reference=state.correlation_id or current_reference(),
    )


class ProfileModal(discord.ui.Modal, title="Профиль семьи"):
    family_name = discord.ui.TextInput(label="Название семьи", max_length=80)
    family_tag = discord.ui.TextInput(
        label="Тег семьи", required=False, max_length=16
    )
    timezone = discord.ui.TextInput(label="Часовой пояс", max_length=40)
    season_start = discord.ui.TextInput(label="Начало сезона ГГГГ-ММ-ДД", max_length=10)
    season_end = discord.ui.TextInput(label="Конец сезона ГГГГ-ММ-ДД", max_length=10)

    def __init__(self, view: WizardView) -> None:
        super().__init__()
        self.wizard = view
        profile = view.state.profile
        self.family_name.default = str(profile.get("family_name") or "")[:80]
        self.family_tag.default = str(profile.get("family_tag") or "")[:16]
        self.timezone.default = str(profile.get("timezone") or "Europe/Moscow")[:40]
        self.season_start.default = str(profile.get("season_start") or "")[:10]
        self.season_end.default = str(profile.get("season_end") or "")[:10]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self.wizard.state = set_profile_fields(
                self.wizard.state,
                {
                    "family_name": str(self.family_name.value),
                    "family_tag": str(self.family_tag.value),
                    "timezone": str(self.timezone.value),
                    "season_start": str(self.season_start.value),
                    "season_end": str(self.season_end.value),
                },
            )
            await self.wizard.persist(interaction)
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
            return
        await self.wizard.refresh(interaction)


class WizardView(SafeView):
    def __init__(
        self,
        cog: SetupWizardCog,
        *,
        owner_id: int,
        guild_id: int,
        state: WizardState,
        snapshot: GuildSnapshot,
    ) -> None:
        super().__init__(timeout=600.0)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.state = state
        self.snapshot = snapshot
        self.selected_key = ""
        self.page = 0
        self._mount()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.owner_id:
            await respond_spec(
                interaction,
                system_error(
                    code="SETUP.FOREIGN",
                    summary=(
                        "Этот мастер открыт другим участником. "
                        "Запустите свой экземпляр командой "
                        f"{cmd('setup start')}."
                    ),
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return False
        try:
            await assert_config_manage(interaction.user)
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=False), ephemeral=True)
            return False
        return True

    async def persist(self, interaction: discord.Interaction) -> None:
        self.state = await save_wizard_state(
            self.guild_id,
            self.state,
            actor_id=interaction.user.id,
            actor_name=interaction.user.display_name,
        )

    async def refresh(self, interaction: discord.Interaction, *, snapshot: GuildSnapshot | None = None) -> None:
        if snapshot is not None:
            self.snapshot = snapshot
        elif interaction.guild is not None:
            self.snapshot = snapshot_guild(interaction.guild)
        self.state = refresh_scan(self.state, self.snapshot)
        self.clear_items()
        self._mount()
        spec = _wizard_spec(self.state, self.snapshot)
        if interaction.response.is_done():
            await edit_interaction_message_v2(interaction, embed=spec, view=self)
            return
        await respond_spec(interaction, spec, view=self, ephemeral=True)

    def _mount(self) -> None:
        step = self.state.step
        if step == "profile":
            self._mount_profile()
        elif step == "structure":
            self._mount_objects(structure_inventory(), pick_bind=True)
        elif step == "roles":
            self._mount_objects(role_inventory(), pick_bind=True, roles=True)
        elif step == "permissions":
            self._mount_permissions()
        elif step == "panels":
            self._mount_panels()
        elif step == "summary":
            self._mount_summary()
        else:
            self._add_nav(done=True)

    def _mount_profile(self) -> None:
        edit = discord.ui.Button(label="Заполнить профиль", style=discord.ButtonStyle.primary)
        edit.callback = self._open_profile
        self.add_item(edit)
        directions = discord.ui.Select(
            placeholder="Направление семьи",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=_DIRECTION_LABELS[key],
                    value=key,
                    default=str(self.state.profile.get("direction") or "unset") == key,
                )
                for key in sorted(KNOWN_DIRECTIONS)
            ],
        )
        directions.callback = self._set_direction
        self.add_item(directions)
        selected = {
            str(item) for item in (self.state.profile.get("enabled_upgrades") or [])
        }
        upgrades = discord.ui.Select(
            placeholder="Открытые улучшения",
            min_values=0,
            max_values=min(25, len(KNOWN_UPGRADES)),
            options=[
                discord.SelectOption(
                    label=upgrade_label(key)[:100],
                    value=key,
                    default=key in selected,
                )
                for key in sorted(KNOWN_UPGRADES)
            ],
        )
        upgrades.callback = self._set_upgrades
        self.add_item(upgrades)
        self._add_nav()

    def _mount_objects(self, inventory: tuple[Any, ...], *, pick_bind: bool, roles: bool = False) -> None:
        page_size = 20
        pages = max(1, (len(inventory) + page_size - 1) // page_size)
        self.page = min(self.page, pages - 1)
        chunk = inventory[self.page * page_size : (self.page + 1) * page_size]
        bucket = self.state.roles if roles else self.state.structure
        options: list[discord.SelectOption] = []
        for item in chunk:
            choice = bucket.get(item.key)
            action = choice.action if choice is not None else "unset"
            resolved = choice.resolved if choice is not None else ""
            options.append(
                discord.SelectOption(
                    label=item.title[:100],
                    value=item.key,
                    description=f"{resolved or '—'} / {action}"[:100],
                    default=item.key == self.selected_key,
                )
            )
        picker = discord.ui.Select(
            placeholder="Объект настройки",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="Нет объектов", value="-")],
        )
        picker.callback = self._pick_object
        self.add_item(picker)
        if pick_bind:
            for label, action, style in (
                ("Привязать", ACTION_BIND, discord.ButtonStyle.success),
                ("Создать", ACTION_CREATE, discord.ButtonStyle.primary),
                ("Пропустить", ACTION_SKIP, discord.ButtonStyle.secondary),
            ):
                button = discord.ui.Button(label=label, style=style)
                button.callback = self._make_action_callback(action, roles=roles)
                self.add_item(button)
            if self.selected_key:
                selected = next((item for item in inventory if item.key == self.selected_key), None)
                if selected is not None:
                    if roles:
                        role_select = discord.ui.RoleSelect(
                            placeholder="Выберите роль Discord",
                            min_values=1,
                            max_values=1,
                        )
                        role_select.callback = self._bind_selected_role
                        self.add_item(role_select)
                    else:
                        channel_types = [discord.ChannelType.category] if selected.kind == KIND_CATEGORY else (
                            [discord.ChannelType.voice]
                            if selected.channel_type == KIND_VOICE
                            else [discord.ChannelType.text]
                        )
                        channel_select = discord.ui.ChannelSelect(
                            placeholder="Выберите объект Discord",
                            min_values=1,
                            max_values=1,
                            channel_types=channel_types,
                        )
                        channel_select.callback = self._bind_selected_channel
                        self.add_item(channel_select)
        if pages > 1:
            prev_page = discord.ui.Button(label="Ранее", disabled=self.page <= 0)
            next_page = discord.ui.Button(label="Позже", disabled=self.page >= pages - 1)
            prev_page.callback = self._prev_page
            next_page.callback = self._next_page
            self.add_item(prev_page)
            self.add_item(next_page)
        self._add_nav()

    def _mount_permissions(self) -> None:
        options = []
        for item in role_inventory():
            choice = self.state.roles.get(item.key)
            if choice is None or choice.action == ACTION_SKIP:
                continue
            options.append(
                discord.SelectOption(
                    label=item.title[:100],
                    value=item.key,
                    default=item.key == self.selected_key,
                )
            )
        if options:
            picker = discord.ui.Select(
                placeholder="Роль для полномочий",
                min_values=1,
                max_values=1,
                options=options[:25],
            )
            picker.callback = self._pick_object
            self.add_item(picker)
        selected = set(self.state.permissions.get(self.selected_key) or [])
        perm_select = discord.ui.Select(
            placeholder="Полномочия роли",
            min_values=0,
            max_values=min(25, len(Permission)),
            options=[
                discord.SelectOption(
                    label=permission.label[:100],
                    value=permission.value,
                    default=permission.value in selected,
                )
                for permission in Permission
            ],
        )
        perm_select.callback = self._set_permissions
        self.add_item(perm_select)
        self._add_nav()

    def _mount_panels(self) -> None:
        selected = {
            spec.key
            for spec in known_panels()
            if bool((self.state.panels.get(spec.key) or {}).get("enabled"))
        }
        picker = discord.ui.Select(
            placeholder="Какие панели разместить",
            min_values=0,
            max_values=len(known_panels()),
            options=[
                discord.SelectOption(
                    label=spec.title[:100],
                    value=spec.key,
                    default=spec.key in selected,
                )
                for spec in known_panels()
            ],
        )
        picker.callback = self._set_panels
        self.add_item(picker)
        self._add_nav()

    def _mount_summary(self) -> None:
        preview = discord.ui.Button(label="Предпросмотр и применение", style=discord.ButtonStyle.danger)
        preview.callback = self._open_confirm
        self.add_item(preview)
        self._add_nav()

    def _add_nav(self, *, done: bool = False) -> None:
        if not done and self.state.step != "profile":
            back = discord.ui.Button(label="Назад", style=discord.ButtonStyle.secondary)
            back.callback = self._go_back
            self.add_item(back)
        if not done and self.state.step != "summary":
            nxt = discord.ui.Button(label="Далее", style=discord.ButtonStyle.primary)
            nxt.callback = self._go_next
            self.add_item(nxt)

    async def _open_profile(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ProfileModal(self))

    async def _set_direction(self, interaction: discord.Interaction) -> None:
        values = getattr(interaction, "data", {}).get("values") if hasattr(interaction, "data") else None
        selected = list(values or [])
        if not selected and getattr(interaction, "data", None):
            selected = list(interaction.data.get("values") or [])  # type: ignore[union-attr]
        if not selected:
            for child in self.children:
                if isinstance(child, discord.ui.Select) and child.values:
                    selected = list(child.values)
                    break
        if not selected:
            await interaction.response.defer()
            return
        try:
            self.state = set_profile_fields(self.state, {"direction": selected[0]})
            await self.persist(interaction)
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
            return
        await self.refresh(interaction)

    async def _set_upgrades(self, interaction: discord.Interaction) -> None:
        selected = _select_values(interaction, self)
        try:
            self.state = set_profile_fields(self.state, {"enabled_upgrades": selected})
            await self.persist(interaction)
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
            return
        await self.refresh(interaction)

    async def _pick_object(self, interaction: discord.Interaction) -> None:
        values = _select_values(interaction, self)
        self.selected_key = values[0] if values and values[0] != "-" else ""
        await self.refresh(interaction)

    def _make_action_callback(self, action: str, *, roles: bool):  # noqa: ARG002
        async def _callback(interaction: discord.Interaction) -> None:
            if not self.selected_key:
                await respond_spec(
                    interaction,
                    system_error(
                        code="SETUP.NO_OBJECT",
                        summary=(
                            "Сначала выберите объект в списке. "
                            "Ничего не изменено."
                        ),
                        retryable=True,
                        reference=current_reference(),
                    ),
                    ephemeral=True,
                )
                return
            try:
                self.state = choose_object(
                    self.state,
                    self.selected_key,
                    action,
                    snapshot=self.snapshot,
                    kind=KIND_ROLE if roles else KIND_CHANNEL,
                )
                await self.persist(interaction)
            except SetupError as exc:
                await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
                return
            await self.refresh(interaction)

        return _callback

    async def _bind_selected_channel(self, interaction: discord.Interaction) -> None:
        values = getattr(interaction, "data", {}) or {}
        resolved = []
        if isinstance(values, dict):
            resolved = list(values.get("values") or [])
        if not resolved:
            for child in self.children:
                if isinstance(child, discord.ui.ChannelSelect) and child.values:
                    resolved = [str(item.id) for item in child.values]
                    break
        if not self.selected_key or not resolved:
            await interaction.response.defer()
            return
        try:
            self.state = choose_object(
                self.state,
                self.selected_key,
                ACTION_BIND,
                target_id=int(resolved[0]),
                snapshot=self.snapshot,
                kind=KIND_CHANNEL,
            )
            await self.persist(interaction)
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
            return
        await self.refresh(interaction)

    async def _bind_selected_role(self, interaction: discord.Interaction) -> None:
        resolved: list[str] = []
        for child in self.children:
            if isinstance(child, discord.ui.RoleSelect) and child.values:
                resolved = [str(item.id) for item in child.values]
                break
        if not self.selected_key or not resolved:
            await interaction.response.defer()
            return
        try:
            self.state = choose_object(
                self.state,
                self.selected_key,
                ACTION_BIND,
                target_id=int(resolved[0]),
                snapshot=self.snapshot,
                kind=KIND_ROLE,
            )
            await self.persist(interaction)
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
            return
        await self.refresh(interaction)

    async def _set_permissions(self, interaction: discord.Interaction) -> None:
        if not self.selected_key:
            await respond_spec(
                interaction,
                system_error(
                    code="SETUP.NO_OBJECT",
                    summary="Сначала выберите роль. Полномочия не изменены.",
                    retryable=True,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        try:
            self.state = set_role_permissions(
                self.state, self.selected_key, _select_values(interaction, self)
            )
            await self.persist(interaction)
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
            return
        await self.refresh(interaction)

    async def _set_panels(self, interaction: discord.Interaction) -> None:
        selected = set(_select_values(interaction, self))
        try:
            self.state = set_panels(
                self.state,
                {spec.key: spec.key in selected for spec in known_panels()},
            )
            await self.persist(interaction)
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
            return
        await self.refresh(interaction)

    async def _prev_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.page += 1
        await self.refresh(interaction)

    async def _go_back(self, interaction: discord.Interaction) -> None:
        order = ["profile", "structure", "roles", "permissions", "panels", "summary"]
        index = order.index(self.state.step) if self.state.step in order else 0
        self.state = set_step(self.state, order[max(0, index - 1)])
        await self.persist(interaction)
        await self.refresh(interaction)

    async def _go_next(self, interaction: discord.Interaction) -> None:
        order = ["profile", "structure", "roles", "permissions", "panels", "summary"]
        index = order.index(self.state.step) if self.state.step in order else 0
        self.state = set_step(self.state, order[min(len(order) - 1, index + 1)])
        await self.persist(interaction)
        await self.refresh(interaction)

    async def _open_confirm(self, interaction: discord.Interaction) -> None:
        plan = build_plan(self.state, self.snapshot)
        if plan.attention and any("неоднознач" in item for item in plan.attention):
            await respond_spec(
                interaction,
                system_error(
                    code="SETUP.AMBIGUOUS",
                    summary=(
                        "Есть объекты с неоднозначным именем. "
                        "Ничего не создано. Выберите каждый такой объект явно."
                    ),
                    retryable=True,
                    reference=self.state.correlation_id or current_reference(),
                ),
                ephemeral=True,
            )
            return

        async def on_confirm(confirm_interaction: discord.Interaction) -> None:
            await self.cog.apply_wizard(
                confirm_interaction, self.guild_id, self.owner_id
            )

        dialog = ConfirmDialog(
            domain="setup",
            object_id="apply",
            summary=plan.preview_text(),
            actor=interaction.user.mention,
            on_confirm=on_confirm,
            owner_id=self.owner_id,
        )
        await respond_v2(interaction, view=dialog, ephemeral=True)


def _select_values(interaction: discord.Interaction, view: WizardView) -> list[str]:
    data = getattr(interaction, "data", None)
    if isinstance(data, dict) and data.get("values"):
        return [str(item) for item in data["values"]]
    for child in view.children:
        if isinstance(child, discord.ui.Select) and child.values:
            return [str(item) for item in child.values]
    return []


class SetupWizardCog(commands.Cog):
    setup = app_commands.Group(
        name="setup",
        description="Первичная настройка бота на сервере",
        default_permissions=discord.Permissions.none(),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        worker = getattr(self.bot, "operation_worker", None)
        if worker is None:
            return
        worker.register(PROVISION_CATEGORY, self._provision_object)
        worker.register(PROVISION_CHANNEL, self._provision_object)
        worker.register(PROVISION_ROLE, self._provision_object)

    async def cog_unload(self) -> None:
        worker = getattr(self.bot, "operation_worker", None)
        if worker is None:
            return
        worker.unregister(PROVISION_CATEGORY)
        worker.unregister(PROVISION_CHANNEL)
        worker.unregister(PROVISION_ROLE)

    @setup.command(name="start", description="Запустить или продолжить первичную настройку")
    @requires(Permission.CONFIG_MANAGE)
    async def start_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="SETUP.GUILD_ONLY",
                    summary="Мастер доступен только на сервере. Ничего не изменено.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        snapshot = snapshot_guild(guild)
        try:
            state, resumed = await start_or_resume(
                guild.id,
                snapshot,
                actor_id=interaction.user.id,
                actor_name=interaction.user.display_name,
            )
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
            return
        view = WizardView(
            self,
            owner_id=interaction.user.id,
            guild_id=guild.id,
            state=state,
            snapshot=snapshot,
        )
        spec = _wizard_spec(state, snapshot)
        if resumed:
            spec = notice(
                code="SETUP.RESUMED",
                title="Продолжение настройки",
                summary="Прерванный мастер восстановлен с последнего шага.",
                details=spec.details,
                next_step=spec.next_step,
                reference=state.correlation_id or current_reference(),
            )
        await respond_spec(interaction, spec, view=view, ephemeral=True)

    @setup.command(name="status", description="Показать состояние первичной настройки")
    @requires(Permission.CONFIG_MANAGE)
    async def status_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_spec(
                interaction,
                system_error(
                    code="SETUP.GUILD_ONLY",
                    summary="Статус настройки доступен только на сервере.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        status = await setup_status(guild.id, snapshot_guild(guild))
        details = tuple(
            (title, "заполнено" if filled else "не заполнено")
            for title, filled in status.blocks
        ) + (
            ("Шаг", _STEP_TITLES.get(status.step, status.step)),
            ("Кто менял", status.updated_by_name or "—"),
            ("Когда", status.updated_at or "—"),
            ("План", status.preview),
        )
        await respond_spec(
            interaction,
            notice(
                code="SETUP.STATUS",
                title="Состояние настройки",
                summary=status.preview,
                details=details,
                next_step=f"Продолжить можно командой {cmd('setup start')}.",
                reference=status.correlation_id or current_reference(),
            ),
            ephemeral=True,
        )

    async def apply_wizard(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        owner_id: int,
    ) -> None:
        guild = interaction.guild
        if guild is None or interaction.user.id != owner_id:
            await respond_spec(
                interaction,
                system_error(
                    code="SETUP.FOREIGN",
                    summary="Подтверждение недоступно. Применение не выполнено.",
                    retryable=False,
                    reference=current_reference(),
                ),
                ephemeral=True,
            )
            return
        try:
            await assert_config_manage(interaction.user)
            state = await load_wizard_state(guild_id)
            if state is None:
                raise SetupError(
                    "Состояние мастера не найдено. Ничего не применено. "
                    f"Запустите {cmd('setup start')} снова. Код: SETUP.NO_STATE.",
                    code="SETUP.NO_STATE",
                )
            snapshot = snapshot_guild(guild)
            state = refresh_scan(mark_confirmed(state), snapshot)
            result = await apply_confirmed_plan(
                guild_id,
                state,
                snapshot,
                actor_id=interaction.user.id,
                actor_name=interaction.user.display_name,
                source="component",
            )
        except SetupError as exc:
            await respond_spec(interaction, _error_spec(exc, retryable=True), ephemeral=True)
            return
        if not result.pending_creates:
            await self._deploy_panels(guild, result.panel_keys)
        await respond_spec(
            interaction,
            admin_receipt(
                code="SETUP.APPLIED",
                title="Настройка применена",
                actor=interaction.user.mention,
                changed=result.correlation_id,
                after=(
                    f"создано {result.created_enqueued}, привязано {result.bound}, "
                    f"пропущено {result.skipped}, полномочий {result.grants}"
                ),
                reason="; ".join(result.attention) if result.attention else "расхождений нет",
                reference=result.correlation_id,
            ),
            ephemeral=True,
        )

    async def _deploy_panels(self, guild: discord.Guild, panel_keys: tuple[str, ...]) -> None:
        if not panel_keys:
            return
        from cogs.interactions import setup_all_persistent_messages
        from repositories.static_messages import set_static_panels_enabled

        await set_static_panels_enabled(guild.id, True)
        await setup_all_persistent_messages(guild, panel_keys=frozenset(panel_keys))

    async def _provision_object(self, operation: OutboxOperation) -> str:
        payload = operation.payload
        key = str(payload.get("key") or "")
        name = str(payload.get("name") or key)
        channel_type = str(payload.get("channel_type") or "text")
        object_kind = str(payload.get("object_kind") or KIND_CATEGORY)
        correlation_id = str(payload.get("correlation_id") or "")
        actor_id = int(payload.get("actor_id") or 0)
        actor_name = str(payload.get("actor_name") or "system")
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(f"guild {operation.guild_id} is unavailable")
        created = await _create_discord_object(
            guild, kind=object_kind, channel_type=channel_type, name=name
        )
        await record_created_object(
            operation.guild_id,
            key=key,
            object_id=int(created.id),
            actor_id=actor_id,
            actor_name=actor_name,
            correlation_id=correlation_id,
            source="system",
            kind=object_kind,
        )
        state = await load_wizard_state(operation.guild_id)
        pending = tuple(state.applied.get("created") or []) if state is not None else ()
        if not pending and state is not None:
            await self._deploy_panels(
                guild,
                tuple(
                    panel_key
                    for panel_key, payload in state.panels.items()
                    if bool(payload.get("enabled"))
                ),
            )
        return str(created.id)


async def _create_discord_object(
    guild: discord.Guild,
    *,
    kind: str,
    channel_type: str,
    name: str,
) -> Any:
    reason = "Guinness setup"
    if kind == KIND_ROLE:
        return await guild.create_role(name=name, reason=reason)
    if kind == KIND_CATEGORY:
        return await guild.create_category(name, reason=reason)
    if channel_type == KIND_VOICE:
        return await guild.create_voice_channel(name, reason=reason)
    return await guild.create_text_channel(name, reason=reason)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupWizardCog(bot))
