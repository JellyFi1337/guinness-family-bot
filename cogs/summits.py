import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from repositories.operations import OutboxOperation
from repositories.summits import (
    SummitPanel,
    clear_summit_message,
    clear_vzp_position_message,
    complete_summit_launch,
    get_summit_panels,
    is_summit_list_archived,
    is_vzp_list_archived,
    request_summit_launch,
    set_summit_thread,
)
from services.cooldowns import cooldown_claim, cooldown_retry_text
from services.summit_archive import (
    archive_channel_spec,
    archive_open_legacy_gatherings,
    archived_interaction_spec,
)
from ui.layouts import marker_component_id
from ui.messages import (
    edit_message_v2,
    edit_original_response_v2,
    followup_spec,
    followup_v2,
    send_v2,
)

from config import BASE_DIR, CHANNELS
from repositories.maps import FamilyMap
from services.maps import (
    catalog_ref,
    list_catalog,
    map_select_page,
    parse_catalog_ref,
)
from utils.discord_lookup import get_role, get_text_channel
from ui.command_registry import cmd
from ui.design import Accent
from ui.message_specs import MessageSpec
from ui.templates import notice
from utils.embeds import apply_accent
from utils.locks import KeyedLockPool
from utils.views import SafeView, stop_registered_view
from utils.access import has_administrator_access
from utils.database import (
    approve_vzp_gathering,
    create_summit_list,
    create_vzp_summit,
    delete_vzp_summit,
    delete_summit_list,
    get_all_summit_views,
    get_active_summit_emojis,
    init_db,
    get_summit_list,
    get_summit_list_members,
    get_summit_list_reserves,
    get_vzp_positions,
    get_vzp_summit,
    join_contract_list,
    join_summit_list,
    leave_summit_list,
    move_summit_member_to_reserve,
    reserve_summit_list,
    set_summit_list_message,
    set_vzp_gathering_map,
    set_vzp_gathering_position_list,
    set_vzp_gathering_thread,
    set_vzp_summit_message,
    toggle_summit_list,
    swap_summit_member_with_reserve,
    vzp_take_position,
)


_REFRESH_LOCKS = KeyedLockPool[tuple[str, int]]()


def _refresh_lock(kind: str, item_id: int):
    return _REFRESH_LOCKS.lock((kind, item_id))


logger = logging.getLogger(__name__)


async def _reply_if_archived(
    interaction: discord.Interaction,
    *,
    list_id: int | None = None,
    vzp_list_id: int | None = None,
) -> bool:
    spec = await archived_interaction_spec(list_id=list_id, vzp_list_id=vzp_list_id)
    if spec is None:
        return False
    await followup_spec(interaction, spec)
    return True


def _staff(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    role = get_role(interaction.guild, "staff")
    return has_administrator_access(user) or (role is not None and role in user.roles)


def _vzp_manager(interaction: discord.Interaction, summit) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member) or summit is None:
        return False
    return user.id == summit[5] or has_administrator_access(user)


def _map_image_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def _map_attachment(raw_path: str | None) -> discord.File | None:
    if not raw_path or parse_catalog_ref(raw_path) is not None:
        return None
    image_path = _map_image_path(raw_path)
    if not image_path.is_file():
        return None
    return discord.File(image_path, filename=image_path.name)


async def _send_map_card(
    destination: discord.abc.Messageable,
    name: str,
    position_id: int,
    raw_path: str | None,
) -> None:
    extra: dict[str, object] = {}
    attachment = _map_attachment(raw_path)
    if attachment is not None:
        extra["file"] = attachment
    await send_v2(
        destination,
        embed=map_embed_for(name, position_id),
        component_marker=f"summit:vzp_map:{position_id}",
        allowed_mentions=discord.AllowedMentions.none(),
        **extra,
    )


async def _channel(guild: discord.Guild, key: str) -> discord.TextChannel | None:
    return get_text_channel(guild, key)


class VzpSummitView(SafeView):
    def __init__(self, list_id: int, positions):
        super().__init__(timeout=None)
        self.list_id = list_id
        for position, user_id, _ in positions:
            button = discord.ui.Button(
                label=str(position),
                custom_id=f"summit_vzp:{list_id}:{position}",
                style=discord.ButtonStyle.danger if user_id else discord.ButtonStyle.success,
            )

            async def callback(interaction: discord.Interaction, number=position):
                await interaction.response.defer(ephemeral=True)
                if await _reply_if_archived(interaction, vzp_list_id=self.list_id):
                    return
                result = await vzp_take_position(
                    self.list_id, number, interaction.user.id, interaction.user.display_name
                )
                messages = {
                    "left": "Вы покинули позицию.",
                    "taken_ok": f"Вы заняли позицию {number}.",
                    "taken": "Эта позиция уже занята.",
                    "already": "Вы уже занимаете другую позицию.",
                    "invalid": "Позиция не найдена.",
                }
                await refresh_vzp_message(interaction.guild, self.list_id)
                await followup_v2(interaction, messages[result], ephemeral=True)

            button.callback = callback
            self.add_item(button)


class SummitListView(SafeView):
    def __init__(
        self,
        list_id: int,
        *,
        participating: bool = False,
        reserve_member: bool = False,
        is_open: bool = True,
        reserves_enabled: bool = False,
        launched: bool = False,
    ):
        super().__init__(timeout=None)
        self.list_id = list_id
        self._add(
            "Участвовать / выйти",
            discord.ButtonStyle.success,
            "participation",
            self.participation,
        )
        self.reserves_enabled = reserves_enabled
        if reserves_enabled:
            self._add(
                "Замена / покинуть замену",
                discord.ButtonStyle.primary,
                "call_reserve",
                self.call_reserve,
            )
            self._add("Играю на замене", discord.ButtonStyle.secondary, "reserve", self.reserve)
            self._add("В замену", discord.ButtonStyle.secondary, "reserve_alt", self.move_to_reserve)
        self._add("Вызвать список", discord.ButtonStyle.primary, "call_list", self.call_list)
        self._add(
            "Закрыть вход" if is_open else "Открыть вход",
            discord.ButtonStyle.danger if is_open else discord.ButtonStyle.success,
            "toggle",
            self.toggle,
        )
        self._add("Удалить список", discord.ButtonStyle.danger, "delete", self.delete)
        if launched:
            for child in self.children:
                child.disabled = True

    def _add(self, label, style, suffix, callback):
        button = discord.ui.Button(label=label, style=style, custom_id=f"summit_list:{self.list_id}:{suffix}")
        button.callback = callback
        self.add_item(button)

    async def _refresh(
        self, interaction: discord.Interaction, *, focus_user_id: int | None = None
    ) -> None:
        await refresh_summit_list_message(
            interaction.guild, self.list_id, focus_user_id=focus_user_id
        )

    async def participation(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        members = await get_summit_list_members(self.list_id)
        if any(member[0] == interaction.user.id for member in members):
            result = await leave_summit_list(self.list_id, interaction.user.id)
            if result == "locked":
                await followup_v2(
                    interaction,
                    "Список уже зафиксирован и не может быть изменён.",
                    ephemeral=True,
                )
                return
            await self._refresh(interaction, focus_user_id=interaction.user.id)
            await followup_v2(interaction, "Вы покинули список.", ephemeral=True)
            return

        if self.reserves_enabled:
            result, bumped_id, bumped_name = await join_summit_list(
                self.list_id,
                interaction.user.id,
                interaction.user.display_name,
                0,
            )
        else:
            result = await join_contract_list(
                self.list_id,
                interaction.user.id,
                interaction.user.display_name,
                0,
            )
            bumped_id = bumped_name = None
        if result in {"joined", "replaced"}:
            await self._refresh(interaction, focus_user_id=interaction.user.id)
        if result == "replaced" and bumped_id:
            member = interaction.guild.get_member(bumped_id)
            if member:
                try:
                    await send_v2(member, "Вы переведены в очередь замены в списке сбора.")
                except discord.HTTPException:
                    pass
        text = {
            "joined": "Вы добавлены в список.",
            "replaced": f"Вы добавлены в список. {bumped_name} переведён в замену.",
            "full": "Список заполнен.",
            "already": "Вы уже в основном списке.",
            "closed": "Вход в этот список закрыт.",
        }[result]
        await followup_v2(interaction, text, ephemeral=True)

    async def reserve(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        result = await reserve_summit_list(self.list_id, interaction.user.id, interaction.user.display_name)
        if result == "reserved":
            await self._refresh(interaction, focus_user_id=interaction.user.id)
        await followup_v2(interaction,
            {"reserved": "Вы добавлены в очередь замены.", "already": "Вы уже в очереди замены.", "member": "Вы уже в основном списке.", "closed": "Вход закрыт."}[result],
            ephemeral=True,
        )

    async def move_to_reserve(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        result = await move_summit_member_to_reserve(self.list_id, interaction.user.id)
        if result == "moved":
            await self._refresh(interaction, focus_user_id=interaction.user.id)
        await followup_v2(interaction,
            "Вы перемещены в очередь замены."
            if result == "moved"
            else "Вы должны быть в основном списке, чтобы перейти в замену.",
            ephemeral=True,
        )

    async def call_reserve(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        members = await get_summit_list_members(self.list_id)
        reserves = await get_summit_list_reserves(self.list_id)
        member_ids = {member[0] for member in members}
        reserve_ids = {reserve[0] for reserve in reserves}

        if interaction.user.id in reserve_ids:
            result = await leave_summit_list(self.list_id, interaction.user.id)
            if result == "locked":
                await followup_v2(
                    interaction,
                    "Список уже зафиксирован и не может быть изменён.",
                    ephemeral=True,
                )
                return
            await self._refresh(interaction, focus_user_id=interaction.user.id)
            await followup_v2(interaction, "Вы покинули список замены.", ephemeral=True)
            return

        if interaction.user.id not in member_ids:
            await followup_v2(interaction,
                "Сначала присоединитесь к основному списку или очереди замены.",
                ephemeral=True,
            )
            return
        if not reserves:
            await followup_v2(interaction, "Очередь замены пуста.", ephemeral=True)
            return

        selected = reserves[0]
        result, _ = await swap_summit_member_with_reserve(
            self.list_id,
            interaction.user.id,
            selected[0],
            0,
        )
        if result == "swapped":
            await self._refresh(interaction, focus_user_id=interaction.user.id)
            await followup_v2(interaction,
                f"Вы переведены в замену. В основной список добавлен <@{selected[0]}>.",
                ephemeral=True,
            )
        else:
            await followup_v2(interaction,
                "Список изменился, повторите действие.", ephemeral=True
            )

    async def call_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        async with _refresh_lock("launch", self.list_id):
            await self._call_list(interaction)

    async def _call_list(self, interaction: discord.Interaction):
        summit = await get_summit_list(self.list_id)
        if not summit or (interaction.user.id != summit[5] and not _staff(interaction)):
            await followup_v2(interaction, "Только инициатор или администрация может вызвать список.", ephemeral=True)
            return
        members = await get_summit_list_members(self.list_id)
        if not members:
            await followup_v2(interaction,
                "Список пуст — вызывать некого.", ephemeral=True
            )
            return
        if summit[13]:
            await followup_v2(interaction, "Список уже находится на исполнении.", ephemeral=True)
            return
        state = await request_summit_launch(self.list_id, interaction.guild.id)
        if state in {"queued", "pending"}:
            await refresh_summit_list_message(interaction.guild, self.list_id)
            await followup_v2(
                interaction,
                "Запуск списка сохранён. Участники и ветка появятся автоматически.",
                ephemeral=True,
            )
            return
        await followup_v2(
            interaction,
            "Список уже запущен."
            if state == "launched"
            else "Список пуст — вызывать некого."
            if state == "empty"
            else "Сбор архивирован."
            if state == "archived"
            else "Список больше не существует.",
            ephemeral=True,
        )

    async def toggle(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        summit = await get_summit_list(self.list_id)
        if not summit or (interaction.user.id != summit[5] and not _staff(interaction)):
            await followup_v2(interaction, "Только инициатор или администрация может менять статус.", ephemeral=True)
            return
        is_open = await toggle_summit_list(self.list_id)
        await self._refresh(interaction)
        await followup_v2(interaction, "Вход открыт." if is_open else "Вход закрыт.", ephemeral=True)

    async def delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        async with _refresh_lock("launch", self.list_id):
            summit = await get_summit_list(self.list_id)
            if not summit or (interaction.user.id != summit[5] and not _staff(interaction)):
                await followup_v2(interaction, "Только инициатор или администрация может удалить список.", ephemeral=True)
                return
            if summit[11]:
                thread = interaction.guild.get_thread(summit[11])
                if thread is not None:
                    try:
                        await thread.delete(reason=f"Summit deleted by {interaction.user}")
                    except discord.NotFound:
                        pass
            await delete_summit_list(self.list_id)
            embed = apply_accent(
                notice(
                    code="SUMMIT.LIST_DELETED",
                    title="🗑️ Список удалён",
                    summary=(
                        f"Список **«{summit[1]}»** удалён.\n"
                        f"Создатель: <@{summit[5]}>\nУдалил: {interaction.user.mention}"
                    ),
                    reference=f"#{self.list_id}",
                ),
                Accent.EVENTS,
            )
            await edit_message_v2(interaction.message, content=None, embed=embed, view=None)
        await followup_v2(interaction, "Список удалён.", ephemeral=True)


class CatalogMapSelect(discord.ui.Select):
    def __init__(self, gathering_id: int, maps: tuple[FamilyMap, ...]):
        self.gathering_id = gathering_id
        self.by_id = {item.id: item for item in maps}
        super().__init__(
            placeholder="Выберите карту",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=item.name[:100],
                    value=str(item.id),
                    description=f"{item.event_type} · {item.version}"[:100],
                )
                for item in maps
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.gathering_id):
            return
        summit = await get_summit_list(self.gathering_id)
        if not summit or not _vzp_manager(interaction, summit):
            await edit_original_response_v2(interaction,
                content="Нет прав на выбор карты.", view=None
            )
            return

        try:
            chosen = self.by_id[int(self.values[0])]
        except (KeyError, TypeError, ValueError):
            await edit_original_response_v2(interaction,
                content="Карта не найдена в каталоге.", view=None
            )
            return
        name = chosen.name
        raw_path = catalog_ref(chosen.id)

        operation_lock = _refresh_lock("map", self.gathering_id)
        await operation_lock.acquire()
        try:
            summit = await get_summit_list(self.gathering_id)
            if not summit:
                await edit_original_response_v2(interaction,
                    content="Сбор больше не существует.", view=None
                )
                return
            await set_vzp_gathering_map(self.gathering_id, name, raw_path)

            # An approved gathering keeps one thread, but every map gets a new
            # independent position list below its image.
            if summit[10] and summit[11]:
                thread = await _get_thread(interaction, summit[11])
                if thread is not None:
                    if isinstance(interaction.user, discord.Member):
                        try:
                            await thread.add_user(interaction.user)
                        except discord.HTTPException:
                            logger.exception(
                                "Failed to add VZP manager %s to thread %s",
                                interaction.user.id,
                                thread.id,
                            )
                    old_position_id = summit[12]
                    # Persist the new position list before publishing either
                    # message.  The periodic reconciler can then finish a map
                    # change after a process crash without duplicating cards.
                    new_position_id = await create_vzp_summit(
                        name,
                        summit[2],
                        thread.id,
                        summit[5],
                        guild_id=interaction.guild.id,
                    )
                    await set_vzp_gathering_position_list(
                        self.gathering_id, new_position_id
                    )
                    await _send_map_card(thread, name, new_position_id, raw_path)
                    if old_position_id:
                        old_summit = await get_vzp_summit(old_position_id)
                        if old_summit and old_summit[4]:
                            try:
                                old_message = await thread.fetch_message(old_summit[4])
                                await edit_message_v2(old_message, view=None)
                            except discord.HTTPException:
                                logger.exception(
                                    "Failed to disable old VZP position list %s",
                                    old_position_id,
                                )
                    positions = await get_vzp_positions(new_position_id)
                    position_message = await send_v2(thread,
                        embed=vzp_embed(
                            (
                                new_position_id,
                                name,
                                summit[2],
                                thread.id,
                                None,
                                thread.id,
                                summit[5],
                            ),
                            positions,
                        ),
                        view=VzpSummitView(new_position_id, positions),
                        component_marker=f"summit:vzp_positions:{new_position_id}",
                    )
                    await set_vzp_summit_message(
                        new_position_id, position_message.id, thread.id
                    )
                    if old_position_id:
                        await delete_vzp_summit(old_position_id)

            await refresh_vzp_gathering_message(interaction.guild, self.gathering_id)
            await edit_original_response_v2(interaction,
                content=f"Выбрана карта **{name}**.", view=None
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to change VZP map for %s", self.gathering_id)
            await edit_original_response_v2(interaction,
                content="Не удалось сменить карту.", view=None
            )
        finally:
            operation_lock.release()


class _MapPageButton(discord.ui.Button):
    def __init__(
        self,
        *,
        label: str,
        gathering_id: int,
        maps: tuple[FamilyMap, ...],
        page: int,
        disabled: bool,
    ) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            disabled=disabled,
        )
        self.gathering_id = gathering_id
        self.maps = maps
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await edit_message_v2(
            interaction.message,
            content="Выберите карту:",
            view=VzpMapSelectView(self.gathering_id, self.maps, page=self.page),
        )


class VzpMapSelectView(SafeView):
    def __init__(
        self,
        gathering_id: int,
        maps: tuple[FamilyMap, ...],
        page: int = 0,
    ):
        super().__init__(timeout=180)
        page_maps, current, total = map_select_page(maps, page)
        self.add_item(CatalogMapSelect(gathering_id, page_maps))
        if total > 1:
            self.add_item(
                _MapPageButton(
                    label="Назад",
                    gathering_id=gathering_id,
                    maps=maps,
                    page=current - 1,
                    disabled=current <= 0,
                )
            )
            self.add_item(
                _MapPageButton(
                    label="Вперёд",
                    gathering_id=gathering_id,
                    maps=maps,
                    page=current + 1,
                    disabled=current >= total - 1,
                )
            )


class VzpGatheringView(SummitListView):
    def __init__(
        self,
        list_id: int,
        *,
        participating: bool = False,
        reserve_member: bool = False,
        is_open: bool = True,
        selected_map: str | None = None,
        approved: bool = False,
    ):
        super().__init__(
            list_id,
            participating=participating,
            reserve_member=reserve_member,
            is_open=is_open,
            reserves_enabled=True,
        )
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.custom_id == f"summit_list:{list_id}:call_list":
                child.label = "Сменить карту" if selected_map else "Выбрать карту"
                child.style = discord.ButtonStyle.primary
            if approved and child.custom_id in {
                f"summit_list:{list_id}:participation",
                f"summit_list:{list_id}:call_reserve",
                f"summit_list:{list_id}:reserve",
                f"summit_list:{list_id}:reserve_alt",
                f"summit_list:{list_id}:toggle",
            }:
                child.disabled = True
                if child.custom_id.endswith(":toggle"):
                    child.label = "Список утверждён"

        if selected_map and not approved:
            self._add(
                "Одобрить список",
                discord.ButtonStyle.success,
                "approve_vzp_gathering",
                self.approve_list,
            )

    async def _refresh(
        self, interaction: discord.Interaction, *, focus_user_id: int | None = None
    ) -> None:
        await refresh_vzp_gathering_message(
            interaction.guild, self.list_id, focus_user_id=focus_user_id
        )

    async def call_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        summit = await get_summit_list(self.list_id)
        if not summit or not _vzp_manager(interaction, summit):
            await followup_v2(interaction,
                "Выбирать карту может инициатор, администратор или старший состав.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await followup_v2(interaction, "Выбор карты доступен только на сервере.", ephemeral=True)
            return
        maps = await list_catalog(interaction.guild.id)
        if not maps:
            await followup_v2(
                interaction,
                (
                    "Каталог карт пуст. Добавьте запись через "
                    f"{cmd('event map_add')} с версией, типом события и источником."
                ),
                ephemeral=True,
            )
            return
        await followup_v2(
            interaction,
            "Выберите карту:",
            view=VzpMapSelectView(self.list_id, maps),
            ephemeral=True,
        )

    async def toggle(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        summit = await get_summit_list(self.list_id)
        if not summit or not _vzp_manager(interaction, summit):
            await followup_v2(interaction,
                "Управлять входом может инициатор, администратор или старший состав.",
                ephemeral=True,
            )
            return
        if summit[10]:
            await followup_v2(interaction,
                "Утверждённый список уже зафиксирован.", ephemeral=True
            )
            return
        is_open = await toggle_summit_list(self.list_id)
        await self._refresh(interaction)
        await followup_v2(interaction,
            "Вход открыт." if is_open else "Вход закрыт.", ephemeral=True
        )

    async def approve_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        summit = await get_summit_list(self.list_id)
        if not summit or not _vzp_manager(interaction, summit):
            await followup_v2(interaction,
                "Утверждать список может инициатор, администратор или старший состав.",
                ephemeral=True,
            )
            return
        async with _refresh_lock("approve", self.list_id):
            await self._approve_list(interaction)

    async def _approve_list(self, interaction: discord.Interaction):
        summit = await get_summit_list(self.list_id)
        if not summit:
            await followup_v2(interaction, "Сбор больше не существует.", ephemeral=True)
            return
        if summit[10]:
            await followup_v2(interaction, "Список уже утверждён.", ephemeral=True)
            return
        if not summit[8]:
            await followup_v2(interaction, "Сначала выберите карту.", ephemeral=True)
            return
        members = await get_summit_list_members(self.list_id)
        if not members:
            await followup_v2(interaction,
                "Нельзя утвердить пустой список.", ephemeral=True
            )
            return

        try:
            thread = await _ensure_vzp_thread(interaction, summit)
            await _grant_vzp_thread_access(interaction.guild, thread, summit, members)

            position_list_id = summit[12]
            if not position_list_id:
                position_list_id = await create_vzp_summit(
                    summit[8],
                    summit[2],
                    thread.id,
                    summit[5],
                    guild_id=interaction.guild.id,
                )
                await set_vzp_gathering_position_list(
                    self.list_id, position_list_id
                )

            position_summit = await get_vzp_summit(position_list_id)
            if position_summit and not position_summit[4]:
                await _send_map_card(
                    thread, summit[8], position_list_id, summit[9]
                )
                positions = await get_vzp_positions(position_list_id)
                position_message = await send_v2(thread,
                    embed=vzp_embed(
                        (
                            position_list_id,
                            summit[8],
                            summit[2],
                            thread.id,
                            None,
                            thread.id,
                            summit[5],
                        ),
                        positions,
                    ),
                    view=VzpSummitView(position_list_id, positions),
                    component_marker=f"summit:vzp_positions:{position_list_id}",
                )
                await set_vzp_summit_message(
                    position_list_id, position_message.id, thread.id
                )

            await approve_vzp_gathering(self.list_id, position_list_id)
            await refresh_vzp_gathering_message(interaction.guild, self.list_id)
            await followup_v2(interaction,
                f"Список утверждён. Приватная ветка: {thread.mention}", ephemeral=True
            )
        except (RuntimeError, discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to approve VZP gathering %s", self.list_id)
            await followup_v2(interaction,
                "Не удалось создать ветку архивного сбора.", ephemeral=True
            )

    async def delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.list_id):
            return
        summit = await get_summit_list(self.list_id)
        can_delete = (
            summit is not None
            and isinstance(interaction.user, discord.Member)
            and (
                interaction.user.id == summit[5]
                or has_administrator_access(interaction.user)
            )
        )
        if not can_delete:
            await followup_v2(interaction,
                "Удалить сбор может только инициатор или администратор.", ephemeral=True
            )
            return
        if summit[11]:
            thread = await _get_thread(interaction, summit[11])
            if thread is not None:
                try:
                    await thread.delete(reason=f"Archived gathering deleted by {interaction.user}")
                except discord.HTTPException:
                    logger.exception("Failed to delete VZP gathering thread %s", thread.id)
        await delete_summit_list(self.list_id)
        await edit_message_v2(interaction.message, content="Архивный сбор удалён.", embed=None, view=None)
        await followup_v2(interaction, "Архивный сбор удалён.", ephemeral=True)


class ContractView(SafeView):
    def __init__(
        self,
        contract_id: int,
        *,
        participating: bool = False,
        is_open: bool = True,
        launched: bool = False,
    ):
        super().__init__(timeout=None)
        self.contract_id = contract_id
        self._add(
            "Участвовать / выйти",
            discord.ButtonStyle.success,
            "participation",
            self.participation,
        )
        self._add("Запустить контракт", discord.ButtonStyle.primary, "launch", self.launch)
        self._add(
            "Закрыть вход" if is_open else "Открыть вход",
            discord.ButtonStyle.danger if is_open else discord.ButtonStyle.success,
            "toggle",
            self.toggle,
        )
        self._add("Удалить контракт", discord.ButtonStyle.danger, "delete", self.delete)
        if launched:
            for child in self.children:
                child.disabled = True

    def _add(self, label, style, suffix, callback):
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"summit_contract:{self.contract_id}:{suffix}",
        )
        button.callback = callback
        self.add_item(button)

    async def participation(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.contract_id):
            return
        members = await get_summit_list_members(self.contract_id)
        if any(member[0] == interaction.user.id for member in members):
            result = await leave_summit_list(self.contract_id, interaction.user.id)
            if result == "locked":
                await followup_v2(
                    interaction,
                    "Контракт уже запущен и не может быть изменён.",
                    ephemeral=True,
                )
                return
            await refresh_contract_message(
                interaction.guild, self.contract_id, focus_user_id=interaction.user.id
            )
            await followup_v2(interaction, "Вы покинули контракт.", ephemeral=True)
            return

        result = await join_contract_list(
            self.contract_id,
            interaction.user.id,
            interaction.user.display_name,
            0,
        )
        if result == "joined":
            await refresh_contract_message(
                interaction.guild, self.contract_id, focus_user_id=interaction.user.id
            )
        await followup_v2(interaction,
            {
                "joined": "Вы добавлены в контракт.",
                "full": "Контракт уже заполнен.",
                "closed": "Вход в контракт закрыт.",
                "already": "Вы уже участвуете в контракте.",
            }[result],
            ephemeral=True,
        )

    async def launch(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.contract_id):
            return
        async with _refresh_lock("launch", self.contract_id):
            await self._launch(interaction)

    async def _launch(self, interaction: discord.Interaction):
        contract = await get_summit_list(self.contract_id)
        if not contract or (interaction.user.id != contract[5] and not _staff(interaction)):
            await followup_v2(interaction,
                "Только инициатор или администрация может запустить контракт.",
                ephemeral=True,
            )
            return
        members = await get_summit_list_members(self.contract_id)
        if not members:
            await followup_v2(interaction,
                "Контракт пуст — запускать некого.", ephemeral=True
            )
            return
        if contract[13]:
            await followup_v2(interaction,
                "Контракт уже запущен.", ephemeral=True
            )
            return
        state = await request_summit_launch(
            self.contract_id, interaction.guild.id
        )
        if state in {"queued", "pending"}:
            await refresh_contract_message(interaction.guild, self.contract_id)
            await followup_v2(
                interaction,
                "Запуск контракта сохранён. Участники и ветка появятся автоматически.",
                ephemeral=True,
            )
            return
        await followup_v2(
            interaction,
            "Контракт уже запущен."
            if state == "launched"
            else "Контракт пуст — запускать некого."
            if state == "empty"
            else "Сбор архивирован."
            if state == "archived"
            else "Контракт больше не существует.",
            ephemeral=True,
        )

    async def toggle(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.contract_id):
            return
        contract = await get_summit_list(self.contract_id)
        if not contract or (interaction.user.id != contract[5] and not _staff(interaction)):
            await followup_v2(interaction, "Нет прав на изменение статуса.", ephemeral=True)
            return
        is_open = await toggle_summit_list(self.contract_id)
        await refresh_contract_message(interaction.guild, self.contract_id)
        await followup_v2(interaction,
            "Вход открыт." if is_open else "Вход закрыт.", ephemeral=True
        )

    async def delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if await _reply_if_archived(interaction, list_id=self.contract_id):
            return
        async with _refresh_lock("launch", self.contract_id):
            contract = await get_summit_list(self.contract_id)
            if not contract or (interaction.user.id != contract[5] and not _staff(interaction)):
                await followup_v2(interaction, "Нет прав на удаление контракта.", ephemeral=True)
                return
            if contract[11]:
                thread = interaction.guild.get_thread(contract[11])
                if thread is not None:
                    try:
                        await thread.delete(reason=f"Contract deleted by {interaction.user}")
                    except discord.NotFound:
                        pass
            result = apply_accent(
                notice(
                    code="SUMMIT.CONTRACT_DELETED",
                    title="🗑️ Контракт удалён",
                    summary=(
                        f"Контракт **«{contract[1]}»** удалён.\n"
                        f"Создатель: <@{contract[5]}>\nУдалил: {interaction.user.mention}"
                    ),
                    reference=f"#{self.contract_id}",
                ),
                Accent.EVENTS,
            )
            await delete_summit_list(self.contract_id)
            await edit_message_v2(interaction.message, content=None, embed=result, view=None)
        await followup_v2(interaction, "Контракт удалён.", ephemeral=True)


def map_embed_for(name: str, position_list_id: int) -> MessageSpec:
    return apply_accent(
        notice(
            code="SUMMIT.MAP",
            title="🗺️ Карта выбрана",
            summary=f"**{name}**",
            next_step="Займите позицию в списке под этой картой.",
            reference=f"#{position_list_id}",
        ),
        Accent.EVENTS,
    )


def vzp_embed(summit, positions) -> MessageSpec:
    occupied = sum(1 for _, user_id, _ in positions if user_id)
    lines = [
        f"{position}. {'🔴' if user_id else '🟢'}"
        + (f" <@{user_id}>" if user_id else "")
        for position, user_id, _ in positions
    ]
    return apply_accent(
        notice(
            code="SUMMIT.POSITIONS",
            title=summit[1],
            summary=f"Список позиций ({occupied}/{summit[2]}):\n" + "\n".join(lines),
            next_step="Займите свободную позицию кнопкой.",
            reference=f"#{summit[0]}",
        ),
        Accent.EVENTS,
    )


def summit_embed(
    guild: discord.Guild,
    summit,
    members,
    reserves=(),
    *,
    show_reserves: bool = False,
    vip_emojis=None,
) -> MessageSpec:
    vip_emojis = vip_emojis or {}
    lines = [
        f"{(vip_emojis.get(user_id) + ' ') if vip_emojis.get(user_id) else ''}"
        f"<@{user_id}>"
        for user_id, _, _ in members
    ]
    reserve_lines = [f"<@{user_id}>" for user_id, _ in reserves]
    description = (
        f"**{summit[1]}**\n"
        f"Список участников ({len(members)}/{summit[2]}):\n"
        + ("\n".join(lines) if lines else "Пока никого.")
        + (
            f"\n\nНа замене ({len(reserves)})\n"
            + ("\n".join(reserve_lines) if reserves else "Пока никого.")
            if show_reserves
            else ""
        )
        + f"\n\nСтатус: {'✅ На исполнении' if summit[13] else ('🟢 Открыт' if summit[6] else '🔴 Закрыт')}"
        + f"\nИнициатор: <@{summit[5]}>"
    )
    return apply_accent(
        notice(
            code="SUMMIT.LIST",
            title="Список",
            summary=description,
            next_step="Запишитесь кнопкой под карточкой, пока вход открыт.",
            reference=f"#{summit[0]}",
        ),
        Accent.EVENTS,
    )


def vzp_gathering_embed(guild: discord.Guild, summit, members, reserves) -> MessageSpec:
    spec = summit_embed(
        guild,
        summit,
        members,
        reserves,
        show_reserves=True,
    )
    details: list[tuple[str, str]] = list(spec.details)
    if summit[8]:
        details.append(("Карта", summit[8]))
    details.append(
        (
            "Утверждение",
            "✅ Одобренный список" if summit[10] else "🟡 Ожидает утверждения",
        )
    )
    return apply_accent(
        notice(
            code="SUMMIT.ARCHIVED_GATHERING",
            title="Архивный сбор",
            summary=spec.summary,
            details=tuple(details),
            next_step="Запишитесь кнопкой, пока сбор открыт.",
            reference=f"#{summit[0]}",
        ),
        Accent.EVENTS,
    )


def contract_embed(summit, members, vip_emojis=None) -> MessageSpec:
    vip_emojis = vip_emojis or {}
    lines = [
        f"{(vip_emojis.get(user_id) + ' ') if vip_emojis.get(user_id) else ''}"
        f"<@{user_id}>"
        for user_id, _, priority in members
    ]
    description = (
        f"**{summit[1]}**\n"
        f"Список участников ({len(members)}/{summit[2]}):\n"
        + ("\n".join(lines) if lines else "Пока никого.")
        + f"\n\nСтатус: {'✅ Контракт запущен' if summit[13] else ('🟢 Открыт' if summit[6] else '🔴 Закрыт')}"
        + f"\nИнициатор: <@{summit[5]}>"
    )
    return apply_accent(
        notice(
            code="SUMMIT.CONTRACT",
            title="Контракт",
            summary=description,
            next_step="Запишитесь кнопкой под карточкой, пока вход открыт.",
            reference=f"#{summit[0]}",
        ),
        Accent.EVENTS,
    )


async def _get_thread(
    interaction: discord.Interaction, thread_id: int
) -> discord.Thread | None:
    thread = interaction.guild.get_thread(thread_id)
    if thread is None:
        try:
            fetched = await interaction.client.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        thread = fetched if isinstance(fetched, discord.Thread) else None
    if thread is not None and thread.archived:
        try:
            await thread.edit(archived=False, reason="Archived gathering resumed")
        except discord.HTTPException:
            logger.exception("Failed to reopen VZP thread %s", thread.id)
    return thread


async def _ensure_vzp_thread(
    interaction: discord.Interaction, summit
) -> discord.Thread:
    if summit[11]:
        existing = await _get_thread(interaction, summit[11])
        if existing is not None:
            return existing

    channel = interaction.guild.get_channel(summit[3])
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError("Канал архивных сборов не найден")
    thread = await channel.create_thread(
        name=f"archive-{summit[1]}"[:100],
        type=discord.ChannelType.private_thread,
        auto_archive_duration=10080,
        invitable=False,
        reason=f"Archived gathering approved by {interaction.user}",
    )
    await set_vzp_gathering_thread(summit[0], thread.id)
    return thread


async def _grant_vzp_thread_access(
    guild: discord.Guild, thread: discord.Thread, summit, members
) -> None:
    allowed_ids = {member[0] for member in members}
    allowed_ids.add(summit[5])
    lead_role = get_role(guild, "lead_vzp")
    if lead_role is not None:
        allowed_ids.update(member.id for member in lead_role.members if not member.bot)

    for user_id in allowed_ids:
        member = guild.get_member(user_id)
        if member is None or member.bot:
            continue
        try:
            await thread.add_user(member)
        except discord.HTTPException:
            logger.exception("Failed to add member %s to VZP thread %s", user_id, thread.id)


async def _refresh_vzp_message(guild: discord.Guild, list_id: int) -> None:
    summit = await get_vzp_summit(list_id)
    if not summit or not summit[4]:
        return
    channel = guild.get_channel(summit[3]) or guild.get_thread(summit[3])
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    try:
        message = await channel.fetch_message(summit[4])
        positions = await get_vzp_positions(list_id)
        await edit_message_v2(message, embed=vzp_embed(summit, positions), view=VzpSummitView(list_id, positions))
    except discord.HTTPException:
        logger.exception("Failed to update VZP summit %s", list_id)


async def refresh_vzp_message(guild: discord.Guild, list_id: int) -> None:
    async with _refresh_lock("positions", list_id):
        await _refresh_vzp_message(guild, list_id)


async def _refresh_summit_list_message(
    guild: discord.Guild, list_id: int, *, focus_user_id: int | None = None
) -> None:
    summit = await get_summit_list(list_id)
    if not summit or not summit[4]:
        return
    channel = guild.get_channel(summit[3])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(summit[4])
        members = await get_summit_list_members(list_id)
        vip_emojis = await get_active_summit_emojis(member[0] for member in members)
        embed = apply_accent(
            summit_embed(guild, summit, members, vip_emojis=vip_emojis),
            Accent.EVENTS,
        )
        await edit_message_v2(
            message,
            embed=embed,
            view=SummitListView(
                list_id,
                participating=focus_user_id is not None and any(
                    member[0] == focus_user_id for member in members
                ),
                is_open=bool(summit[6]),
                launched=bool(summit[13]),
            ),
        )
    except discord.HTTPException:
        logger.exception("Failed to update summit list %s", list_id)


async def refresh_summit_list_message(
    guild: discord.Guild, list_id: int, *, focus_user_id: int | None = None
) -> None:
    async with _refresh_lock("summit", list_id):
        await _refresh_summit_list_message(
            guild, list_id, focus_user_id=focus_user_id
        )


async def _refresh_vzp_gathering_message(
    guild: discord.Guild, list_id: int, *, focus_user_id: int | None = None
) -> None:
    summit = await get_summit_list(list_id)
    if not summit or summit[7] != "vzp_gathering" or not summit[4]:
        return
    channel = guild.get_channel(summit[3])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(summit[4])
        members = await get_summit_list_members(list_id)
        reserves = await get_summit_list_reserves(list_id)
        embed = apply_accent(
            vzp_gathering_embed(guild, summit, members, reserves),
            Accent.EVENTS,
        )
        await edit_message_v2(
            message,
            embed=embed,
            view=VzpGatheringView(
                list_id,
                participating=focus_user_id is not None
                and any(member[0] == focus_user_id for member in members),
                reserve_member=focus_user_id is not None
                and any(reserve[0] == focus_user_id for reserve in reserves),
                is_open=bool(summit[6]),
                selected_map=summit[8],
                approved=bool(summit[10]),
            ),
        )
    except discord.HTTPException:
        logger.exception("Failed to update VZP gathering %s", list_id)


async def refresh_vzp_gathering_message(
    guild: discord.Guild, list_id: int, *, focus_user_id: int | None = None
) -> None:
    async with _refresh_lock("vzp_gathering", list_id):
        await _refresh_vzp_gathering_message(
            guild, list_id, focus_user_id=focus_user_id
        )


async def _refresh_contract_message(
    guild: discord.Guild, contract_id: int, *, focus_user_id: int | None = None
) -> None:
    contract = await get_summit_list(contract_id)
    if not contract or contract[7] != "contract" or not contract[4]:
        return
    channel = guild.get_channel(contract[3])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(contract[4])
        members = await get_summit_list_members(contract_id)
        vip_emojis = await get_active_summit_emojis(member[0] for member in members)
        embed = apply_accent(
            contract_embed(contract, members, vip_emojis),
            Accent.EVENTS,
        )
        await edit_message_v2(
            message,
            embed=embed,
            view=ContractView(
                contract_id,
                participating=focus_user_id is not None
                and any(member[0] == focus_user_id for member in members),
                is_open=bool(contract[6]),
                launched=bool(contract[13]),
            ),
        )
    except discord.HTTPException:
        logger.exception("Failed to update contract %s", contract_id)


async def refresh_contract_message(
    guild: discord.Guild, contract_id: int, *, focus_user_id: int | None = None
) -> None:
    async with _refresh_lock("contract", contract_id):
        await _refresh_contract_message(
            guild, contract_id, focus_user_id=focus_user_id
        )


class SummitsCog(
    commands.GroupCog,
    group_name="summit",
    group_description="Сборы и контракты",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_views: dict[tuple[str, int], tuple[int, discord.ui.View]] = {}

    def _register_panel_view(
        self,
        list_id: int,
        message_id: int,
        view: discord.ui.View,
        *,
        kind: str = "list",
    ) -> None:
        key = (kind, list_id)
        previous = self._panel_views.get(key)
        if previous is not None:
            old_message_id, old_view = previous
            if old_message_id == message_id:
                return
            stop_registered_view(old_view)
        self.bot.add_view(view, message_id=message_id)
        self._panel_views[key] = (message_id, view)

    @staticmethod
    def _view_for_summit(list_id: int, summit) -> discord.ui.View:
        if summit and summit[7] == "contract":
            return ContractView(
                list_id,
                is_open=bool(summit[6]),
                launched=bool(summit[13]),
            )
        if summit and summit[7] == "vzp_gathering":
            return VzpGatheringView(
                list_id,
                is_open=bool(summit[6]),
                selected_map=summit[8],
                approved=bool(summit[10]),
            )
        return SummitListView(
            list_id,
            is_open=bool(summit and summit[6]),
            launched=bool(summit and summit[13]),
        )

    @staticmethod
    def _has_marker(message: discord.Message, marker: str) -> bool:
        expected = marker_component_id(marker)

        def visit(component) -> bool:
            return getattr(component, "id", None) == expected or any(
                visit(child) for child in getattr(component, "children", ())
            )

        return any(visit(component) for component in message.components)

    async def _find_marker_message(self, channel, marker: str):
        async for candidate in channel.history(limit=250):
            if (
                self.bot.user is not None
                and candidate.author.id == self.bot.user.id
                and self._has_marker(candidate, marker)
            ):
                return candidate
        return None

    async def _reconcile_panel(self, panel: SummitPanel) -> None:
        if panel.kind == "vzp_positions":
            if await is_vzp_list_archived(panel.id):
                if panel.message_id:
                    positions = await get_vzp_positions(panel.id)
                    self._register_panel_view(
                        panel.id,
                        panel.message_id,
                        VzpSummitView(panel.id, positions),
                        kind="vzp_positions",
                    )
                return
            await self._reconcile_position_panel(panel)
            return
        if await is_summit_list_archived(panel.id):
            if panel.message_id:
                summit = await get_summit_list(panel.id)
                view = self._view_for_summit(panel.id, summit)
                self._register_panel_view(panel.id, panel.message_id, view)
            return
        summit = await get_summit_list(panel.id)
        if summit is None:
            return
        guild = self.bot.get_guild(panel.guild_id) if panel.guild_id else None
        if guild is None:
            guild = next(
                (
                    item
                    for item in self.bot.guilds
                    if item.get_channel(panel.channel_id) is not None
                ),
                None,
            )
        if guild is None:
            return
        channel = guild.get_channel(panel.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        marker = f"summit:{panel.kind}:{panel.id}"
        message = None
        if panel.message_id:
            try:
                message = await channel.fetch_message(panel.message_id)
            except discord.NotFound:
                await clear_summit_message(
                    panel.id, expected_message_id=panel.message_id
                )
        if message is None:
            message = await self._find_marker_message(channel, marker)

        members = await get_summit_list_members(panel.id)
        content = None
        allowed_mentions = discord.AllowedMentions.none()
        if panel.kind == "contract":
            vip_emojis = await get_active_summit_emojis(
                member[0] for member in members
            )
            embed = contract_embed(summit, members, vip_emojis)
            view = ContractView(
                panel.id,
                is_open=bool(summit[6]),
                launched=bool(summit[13]),
            )
            role = get_role(guild, "contract")
        elif panel.kind == "vzp_gathering":
            reserves = await get_summit_list_reserves(panel.id)
            embed = vzp_gathering_embed(guild, summit, members, reserves)
            view = VzpGatheringView(
                panel.id,
                is_open=bool(summit[6]),
                selected_map=summit[8],
                approved=bool(summit[10]),
            )
            role = get_role(guild, "vzp_team")
        else:
            vip_emojis = await get_active_summit_emojis(
                member[0] for member in members
            )
            embed = summit_embed(guild, summit, members, vip_emojis=vip_emojis)
            view = SummitListView(
                panel.id,
                is_open=bool(summit[6]),
                launched=bool(summit[13]),
            )
            role = None
        if role is not None:
            content = role.mention
            allowed_mentions = discord.AllowedMentions(
                roles=[role], users=False, everyone=False
            )

        embed = apply_accent(embed, Accent.EVENTS)
        if message is None:
            message = await send_v2(
                channel,
                content=content,
                embed=embed,
                view=view,
                component_marker=marker,
                allowed_mentions=allowed_mentions,
            )
        else:
            message = await edit_message_v2(
                message,
                content=content,
                embed=embed,
                view=view,
                component_marker=marker,
                allowed_mentions=allowed_mentions,
            )
        await set_summit_list_message(panel.id, message.id)
        self._register_panel_view(panel.id, message.id, view)

    async def _reconcile_position_panel(self, panel: SummitPanel) -> None:
        summit = await get_vzp_summit(panel.id)
        if summit is None:
            return
        guild = self.bot.get_guild(panel.guild_id) if panel.guild_id else None
        if guild is None:
            guild = next(
                (
                    item
                    for item in self.bot.guilds
                    if item.get_channel(panel.channel_id) is not None
                    or item.get_thread(panel.channel_id) is not None
                ),
                None,
            )
        if guild is None:
            return
        channel = guild.get_channel(panel.channel_id) or guild.get_thread(panel.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        # A map image and its position card form one durable pair.  The map
        # title is stored on the position list; the configured path remains the
        # source of truth for the image after a restart.
        map_marker = f"summit:vzp_map:{panel.id}"
        map_message = await self._find_marker_message(channel, map_marker)
        if map_message is None and summit[1]:
            await _send_map_card(channel, summit[1], panel.id, None)
        marker = f"summit:vzp_positions:{panel.id}"
        message = None
        if panel.message_id:
            try:
                message = await channel.fetch_message(panel.message_id)
            except discord.NotFound:
                await clear_vzp_position_message(
                    panel.id, expected_message_id=panel.message_id
                )
        if message is None:
            message = await self._find_marker_message(channel, marker)
        positions = await get_vzp_positions(panel.id)
        view = VzpSummitView(panel.id, positions)
        embed = vzp_embed(summit, positions)
        if message is None:
            message = await send_v2(
                channel,
                embed=embed,
                view=view,
                component_marker=marker,
            )
        else:
            message = await edit_message_v2(
                message,
                embed=embed,
                view=view,
                component_marker=marker,
            )
        await set_vzp_summit_message(panel.id, message.id, channel.id)
        self._register_panel_view(
            panel.id, message.id, view, kind="vzp_positions"
        )

    async def reconcile_panels(self) -> None:
        failures = 0
        for panel in await get_summit_panels():
            async with _refresh_lock("panel", panel.id):
                try:
                    await self._reconcile_panel(panel)
                except discord.HTTPException:
                    failures += 1
                    logger.exception("Failed to reconcile summit panel %s", panel.id)
        if failures:
            raise RuntimeError(f"Could not reconcile {failures} summit panel(s)")

    async def _launch_outbox(self, operation: OutboxOperation) -> int | None:
        list_id = int(operation.payload["list_id"])
        async with _refresh_lock("launch", list_id):
            return await self._launch_outbox_unlocked(operation, list_id)

    async def _launch_outbox_unlocked(
        self, operation: OutboxOperation, list_id: int
    ) -> int | None:
        if await is_summit_list_archived(list_id):
            return None
        summit = await get_summit_list(list_id)
        if summit is None:
            # Deletion is a valid terminal state, not a dead-letter failure.
            return None
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError("Summit guild is unavailable")
        channel = guild.get_channel(summit[3])
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Summit channel is unavailable")
        members = await get_summit_list_members(list_id)
        if not members:
            raise RuntimeError("Summit became empty before launch")

        marker = f"summit-execution:{list_id}"
        announcement = await self._find_marker_message(channel, marker)
        mentions = " ".join(f"<@{member[0]}>" for member in members)
        mentioned_members = [
            member
            for user_id, *_ in members
            if (member := guild.get_member(user_id)) is not None
        ]
        is_contract = summit[7] == "contract"
        embed = apply_accent(
            notice(
                code="SUMMIT.RESULT",
                title=(
                    f"💸 Контракт «{summit[1]}» запущен"
                    if is_contract
                    else f"📋 Список «{summit[1]}» на исполнении"
                ),
                summary=f"Инициатор: <@{summit[5]}>",
                next_step="Сбор проходит в ветке этого сообщения.",
                reference=f"#{list_id}",
            ),
            Accent.EVENTS,
        )
        allowed_mentions = discord.AllowedMentions(
            users=mentioned_members, roles=False, everyone=False
        )
        if announcement is None:
            announcement = await send_v2(
                channel,
                content=mentions,
                embed=embed,
                component_marker=marker,
                allowed_mentions=allowed_mentions,
            )
        else:
            announcement = await edit_message_v2(
                announcement,
                content=mentions,
                embed=embed,
                component_marker=marker,
                allowed_mentions=allowed_mentions,
            )

        thread = announcement.thread
        if thread is None and summit[11]:
            thread = guild.get_thread(summit[11])
            if thread is None:
                try:
                    fetched = await self.bot.fetch_channel(summit[11])
                except discord.NotFound:
                    fetched = None
                thread = fetched if isinstance(fetched, discord.Thread) else None
        if thread is None:
            thread = await announcement.create_thread(
                name=(
                    f"контракт-{summit[1]}"
                    if is_contract
                    else f"список-{summit[1]}"
                )[:100],
                auto_archive_duration=10080,
                reason="Durable summit launch",
            )
        elif thread.archived:
            await thread.edit(
                archived=False,
                reason="Summit launched",
            )
        for member in mentioned_members:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                logger.exception("Failed to add %s to summit thread %s", member.id, thread.id)
        if not await complete_summit_launch(list_id, thread.id):
            raise RuntimeError("Summit launch state could not be completed")
        latest = next(
            (
                item
                for item in await get_summit_panels()
                if item.id == list_id and item.kind != "vzp_positions"
            ),
            None,
        )
        if latest is not None:
            await self._reconcile_panel(latest)
        return announcement.id

    async def _archive_outbox(self, operation: OutboxOperation) -> int | None:
        payload = operation.payload
        spec = archive_channel_spec(
            str(payload.get("title") or "сбор"),
            member_count=int(payload.get("member_count") or 0),
            list_id=int(payload["list_id"]),
            kind=str(payload.get("kind") or ""),
        )
        if operation.guild_id == 0:
            return None
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError("Archive notify guild is unavailable")
        channel = guild.get_channel(int(payload["channel_id"]))
        if isinstance(channel, discord.TextChannel):
            await send_v2(
                channel,
                embed=spec,
                component_marker=f"summit:archived:{payload['list_id']}",
            )
        creator = guild.get_member(int(payload["creator_id"]))
        if creator is not None:
            try:
                await send_v2(creator, embed=spec)
            except discord.HTTPException:
                logger.warning(
                    "Could not notify creator %s about archived gathering %s",
                    payload.get("creator_id"),
                    payload.get("list_id"),
                )
        return None

    async def cog_load(self):
        await init_db()
        await archive_open_legacy_gatherings()
        vzp, regular = await get_all_summit_views()
        for list_id, message_id in vzp:
            positions = await get_vzp_positions(list_id)
            self._register_panel_view(
                list_id,
                message_id,
                VzpSummitView(list_id, positions),
                kind="vzp_positions",
            )
        for list_id, message_id in regular:
            summit = await get_summit_list(list_id)
            self._register_panel_view(
                list_id, message_id, self._view_for_summit(list_id, summit)
            )
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.register("summit.launch", self._launch_outbox)
            worker.register("summit.archive", self._archive_outbox)
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                "summit-panel-reconcile",
                self.reconcile_panels,
                interval=120,
                timeout=120,
            )

    async def cog_unload(self) -> None:
        worker = getattr(self.bot, "operation_worker", None)
        if worker is not None:
            worker.unregister("summit.launch")
            worker.unregister("summit.archive")
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("summit-panel-reconcile")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.reconcile_panels()

    @app_commands.command(name="list_create", description="Создать общий список участников")
    async def summit_list(
        self,
        interaction: discord.Interaction,
        название: app_commands.Range[str, 1, 100],
        количество: app_commands.Range[int, 1, 50],
    ):
        await interaction.response.defer(ephemeral=True)
        async with cooldown_claim(
            interaction.guild_id,
            "summit_list",
            interaction.user.id,
            bypass=has_administrator_access(interaction.user),
        ) as claim:
            if not claim.allowed:
                await followup_v2(
                    interaction,
                    cooldown_retry_text(claim.retry_after),
                    ephemeral=True,
                )
                return
            channel = await _channel(interaction.guild, "summit_list")
            if not channel:
                await followup_v2(
                    interaction,
                    f"Канал #{CHANNELS['summit_list']} не найден.",
                    ephemeral=True,
                )
                return
            claim.keep()
        list_id = await create_summit_list(
            название,
            количество,
            channel.id,
            interaction.user.id,
            guild_id=interaction.guild.id,
        )
        summit = await get_summit_list(list_id)
        embed = apply_accent(
            summit_embed(interaction.guild, summit, [], []),
            Accent.EVENTS,
        )
        message = None
        thread = None
        try:
            message = await send_v2(channel,
                embed=embed,
                view=SummitListView(list_id, is_open=bool(summit[6])),
                component_marker=f"summit:list:{list_id}",
            )
            await set_summit_list_message(list_id, message.id)
            thread = await message.create_thread(
                name=f"список-{название}"[:100],
                auto_archive_duration=10080,
                reason=f"Summit list {list_id} created",
            )
            if not await set_summit_thread(list_id, thread.id):
                raise RuntimeError("Summit thread could not be persisted")
            if isinstance(interaction.user, discord.Member):
                try:
                    await thread.add_user(interaction.user)
                except discord.HTTPException:
                    logger.warning(
                        "Could not add creator %s to summit thread %s",
                        interaction.user.id,
                        thread.id,
                    )
        except Exception:
            await delete_summit_list(list_id)
            if thread is not None:
                try:
                    await thread.delete(reason="Summit creation rolled back")
                except discord.HTTPException:
                    logger.exception("Could not roll back summit thread %s", thread.id)
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.exception("Could not roll back summit message %s", message.id)
            raise
        await followup_v2(
            interaction,
            f"Список создан: {message.jump_url}\nВетка для общения: {thread.mention}",
            ephemeral=True,
        )

    @app_commands.command(name="contract_create", description="Создать контракт участников")
    async def summit_contract(
        self,
        interaction: discord.Interaction,
        название: app_commands.Range[str, 1, 100],
        количество: app_commands.Range[int, 1, 50],
    ):
        await interaction.response.defer(ephemeral=True)
        async with cooldown_claim(
            interaction.guild_id,
            "summit_contract",
            interaction.user.id,
            bypass=has_administrator_access(interaction.user),
        ) as claim:
            if not claim.allowed:
                await followup_v2(
                    interaction,
                    cooldown_retry_text(claim.retry_after),
                    ephemeral=True,
                )
                return
            channel = await _channel(interaction.guild, "summit_contract")
            if not channel:
                await followup_v2(
                    interaction,
                    f"Канал #{CHANNELS['summit_contract']} не найден.",
                    ephemeral=True,
                )
                return
            claim.keep()
        contract_id = await create_summit_list(
            название,
            количество,
            channel.id,
            interaction.user.id,
            kind="contract",
            guild_id=interaction.guild.id,
        )
        contract = await get_summit_list(contract_id)
        contract_role = get_role(interaction.guild, "contract")
        embed = apply_accent(contract_embed(contract, []), Accent.EVENTS)
        message = None
        thread = None
        try:
            message = await send_v2(channel,
                content=contract_role.mention if contract_role else None,
                embed=embed,
                view=ContractView(contract_id, is_open=bool(contract[6])),
                component_marker=f"summit:contract:{contract_id}",
                allowed_mentions=discord.AllowedMentions(
                    roles=[contract_role] if contract_role else False,
                    users=False,
                    everyone=False,
                ),
            )
            await set_summit_list_message(contract_id, message.id)
            thread = await message.create_thread(
                name=f"контракт-{название}"[:100],
                auto_archive_duration=10080,
                reason=f"Summit contract {contract_id} created",
            )
            if not await set_summit_thread(contract_id, thread.id):
                raise RuntimeError("Contract thread could not be persisted")
            if isinstance(interaction.user, discord.Member):
                try:
                    await thread.add_user(interaction.user)
                except discord.HTTPException:
                    logger.warning(
                        "Could not add creator %s to contract thread %s",
                        interaction.user.id,
                        thread.id,
                    )
        except Exception:
            await delete_summit_list(contract_id)
            if thread is not None:
                try:
                    await thread.delete(reason="Contract creation rolled back")
                except discord.HTTPException:
                    logger.exception(
                        "Could not roll back contract thread %s", thread.id
                    )
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.exception(
                        "Could not roll back contract message %s", message.id
                    )
            raise
        await followup_v2(interaction,
            f"Контракт создан: {message.jump_url}\n"
            f"Ветка для общения: {thread.mention}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SummitsCog(bot))
