from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from ui.templates import notice
from ui.messages import (
    edit_interaction_message_v2,
    edit_original_response_v2,
    followup_v2,
    respond_v2,
    send_v2,
)
from utils.access import bot_owner_only, is_bot_owner
from utils.embeds import WHITE_COLOR
from utils.views import SafeView


@dataclass(frozen=True, slots=True)
class TechnicalNotice:
    label: str
    description: str
    emoji: str
    color: int


TECHNICAL_NOTICES: dict[str, TechnicalNotice] = {
    "maintenance": TechnicalNotice(
        label="Технические работы",
        description="Ведутся технические работы. Некоторые функции бота могут быть временно недоступны.",
        emoji="🛠️",
        color=WHITE_COLOR,
    ),
    "restored": TechnicalNotice(
        label="Работа восстановлена",
        description="Работа бота восстановлена. Все основные функции снова доступны.",
        emoji="✅",
        color=discord.Color.green().value,
    ),
    "problems": TechnicalNotice(
        label="Технические проблемы",
        description="Наблюдаются технические проблемы. Некоторые команды и кнопки могут работать нестабильно.",
        emoji="⚠️",
        color=discord.Color.orange().value,
    ),
    "investigating": TechnicalNotice(
        label="Проблема уже в работе",
        description="Команда разработки уже знает о проблеме и работает над её устранением.",
        emoji="🔧",
        color=WHITE_COLOR,
    ),
}

VABORITY_PAGE_ITEM_LIMIT = 25
VABORITY_PAGE_TEXT_LIMIT = 3_500


@dataclass(frozen=True, slots=True)
class OccupiedVoiceChannel:
    channel_id: int
    name: str
    member_ids: tuple[int, ...]


def occupied_voice_channels(guild: discord.Guild) -> list[OccupiedVoiceChannel]:
    """Return occupied voice channels, including channels hidden from the caller."""

    result: list[OccupiedVoiceChannel] = []
    for channel in guild.voice_channels:
        member_ids = tuple(member.id for member in channel.members if not member.bot)
        if not member_ids:
            continue
        result.append(
            OccupiedVoiceChannel(
                channel_id=channel.id,
                name=channel.name,
                member_ids=member_ids,
            )
        )
    return result


def build_vabority_pages(
    channels: list[OccupiedVoiceChannel],
) -> list[tuple[OccupiedVoiceChannel, ...]]:
    """Split selector options and their member lists within Discord limits."""

    pages: list[tuple[OccupiedVoiceChannel, ...]] = []
    current: list[OccupiedVoiceChannel] = []
    current_size = 0
    for channel in channels:
        line_size = len(_vabority_channel_text(channel)) + 2
        if current and (
            len(current) >= VABORITY_PAGE_ITEM_LIMIT
            or current_size + line_size > VABORITY_PAGE_TEXT_LIMIT
        ):
            pages.append(tuple(current))
            current = []
            current_size = 0
        current.append(channel)
        current_size += line_size
    if current:
        pages.append(tuple(current))
    return pages


def _vabority_channel_text(channel: OccupiedVoiceChannel) -> str:
    name = discord.utils.escape_markdown(discord.utils.escape_mentions(channel.name))
    members = " ".join(f"<@{member_id}>" for member_id in channel.member_ids)
    return f"**{name} · {len(channel.member_ids)} чел.**\n{members}"


def _vabority_page_text(
    page: tuple[OccupiedVoiceChannel, ...],
    *,
    page_index: int,
    page_count: int,
) -> str:
    text = "\n\n".join(_vabority_channel_text(channel) for channel in page)
    if page_count > 1:
        text += f"\n\nСтраница {page_index + 1}/{page_count}"
    return text


def technical_status_embed(item: TechnicalNotice):
    what_next = (
        "Повторите действие позже; отправленные данные не дублируйте."
        if item is not TECHNICAL_NOTICES["restored"]
        else "Можно снова пользоваться командами, формами и кнопками."
    )
    return notice(
        code="TECH.STATUS",
        title=f"{item.emoji} Технический статус бота • {item.label}",
        summary=item.description,
        details=(("Что делать", what_next),),
        next_step=what_next,
        accent=item.color,
        reference=item.label,
    )


class TechnicalStatusSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        owner_id: int,
        destination: discord.TextChannel | discord.Thread,
    ) -> None:
        self.owner_id = owner_id
        self.destination = destination
        super().__init__(
            placeholder="Выберите техническое уведомление",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=notice.label,
                    value=key,
                    emoji=notice.emoji,
                    description=notice.description[:100],
                )
                for key, notice in TECHNICAL_NOTICES.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id or not is_bot_owner(interaction.user):
            await respond_v2(
                interaction,
                "Этот селектор доступен только владельцу бота.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        notice = TECHNICAL_NOTICES[self.values[0]]
        try:
            message = await send_v2(
                self.destination,
                embed=technical_status_embed(notice),
            )
        except discord.HTTPException:
            await followup_v2(
                interaction,
                "Не удалось отправить уведомление. Проверьте права бота в канале.",
                ephemeral=True,
            )
            return
        await followup_v2(
            interaction,
            f"Техническое уведомление отправлено: {message.jump_url}",
            ephemeral=True,
        )


class TechnicalStatusView(SafeView):
    def __init__(
        self,
        *,
        owner_id: int,
        destination: discord.TextChannel | discord.Thread,
    ) -> None:
        super().__init__(timeout=300)
        self.add_item(
            TechnicalStatusSelect(owner_id=owner_id, destination=destination)
        )


class VaborityChannelSelect(discord.ui.Select):
    def __init__(self, controller: "VaborityView") -> None:
        self.controller = controller
        page = controller.pages[controller.page_index]
        options = []
        for channel in page:
            suffix = f" · {len(channel.member_ids)} чел."
            options.append(
                discord.SelectOption(
                    label=f"{channel.name[:100 - len(suffix)]}{suffix}",
                    value=str(channel.channel_id),
                )
            )
        super().__init__(
            placeholder="Выберите голосовой канал",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await respond_v2(interaction, "Сервер недоступен.", ephemeral=True)
            return
        if member.voice is None or member.voice.channel is None:
            await respond_v2(
                interaction,
                "Сначала подключитесь к голосовому каналу.",
                ephemeral=True,
            )
            return

        target = guild.get_channel(int(self.values[0]))
        if not isinstance(target, discord.VoiceChannel):
            await respond_v2(
                interaction,
                "Выбранный канал больше не существует.",
                ephemeral=True,
            )
            return
        if member.voice.channel.id == target.id:
            await respond_v2(
                interaction,
                "Вы уже находитесь в выбранном канале.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await member.move_to(
                target,
                reason="Owner-only voice maintenance",
            )
        except discord.Forbidden:
            await edit_original_response_v2(
                interaction,
                "Боту не хватает права «Перемещать участников».",
                view=None,
            )
            return
        except discord.HTTPException:
            await edit_original_response_v2(
                interaction,
                "Не удалось подключиться к выбранному каналу.",
                view=None,
            )
            return
        await edit_original_response_v2(
            interaction,
            f"Подключение к **{discord.utils.escape_markdown(target.name)}** выполнено.",
            view=None,
        )


class VaborityPageButton(discord.ui.Button):
    def __init__(
        self,
        controller: "VaborityView",
        *,
        delta: int,
        disabled: bool,
    ) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Назад" if delta < 0 else "Далее",
            custom_id=f"vabority:page:{delta}",
            disabled=disabled,
        )
        self.controller = controller
        self.delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.controller
        view.page_index += self.delta
        view.rebuild()
        await edit_interaction_message_v2(
            interaction,
            view.page_text(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class VaborityView(SafeView):
    def __init__(
        self,
        *,
        owner_id: int,
        pages: list[tuple[OccupiedVoiceChannel, ...]],
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.pages = pages
        self.page_index = 0
        self.rebuild()

    def page_text(self) -> str:
        return _vabority_page_text(
            self.pages[self.page_index],
            page_index=self.page_index,
            page_count=len(self.pages),
        )

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(VaborityChannelSelect(self))
        if len(self.pages) > 1:
            self.add_item(
                VaborityPageButton(
                    self,
                    delta=-1,
                    disabled=self.page_index == 0,
                )
            )
            self.add_item(
                VaborityPageButton(
                    self,
                    delta=1,
                    disabled=self.page_index == len(self.pages) - 1,
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not is_bot_owner(interaction.user):
            await respond_v2(
                interaction,
                "Команда доступна только владельцу бота.",
                ephemeral=True,
            )
            return False
        return await super().interaction_check(interaction)


class TechnicalStatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="vabority", description=".")
    @app_commands.default_permissions()
    @app_commands.guild_only()
    @bot_owner_only()
    async def vabority(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await respond_v2(
                interaction,
                "Команда доступна только на сервере.",
                ephemeral=True,
            )
            return
        if member.voice is None or member.voice.channel is None:
            await respond_v2(
                interaction,
                "Сначала подключитесь к голосовому каналу.",
                ephemeral=True,
            )
            return

        pages = build_vabority_pages(occupied_voice_channels(guild))
        if not pages:
            await respond_v2(
                interaction,
                "Сейчас в голосовых каналах нет участников.",
                ephemeral=True,
            )
            return

        view = VaborityView(owner_id=member.id, pages=pages)
        await respond_v2(
            interaction,
            view.page_text(),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TechnicalStatusCog(bot))
