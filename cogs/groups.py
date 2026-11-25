from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands

from config import CATEGORY_IDS, CHANNELS
from repositories.groups import save_private_group
from ui.messages import followup_v2, send_v2
from ui.templates import notice
from utils.access import staff_or_owner
from utils.discord_lookup import get_category
from utils.embeds import WHITE_COLOR


_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_LIMIT = 90  # Discord allows at most 100 permission overwrites.


def _channel_slug(value: str) -> str:
    value = value.strip().casefold().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9_-]+", "-", value)
    return re.sub(r"-{2,}", "-", value).strip("-_")[:80] or "private-group"


class GroupsCog(
    commands.GroupCog,
    group_name="group",
    group_description="Приватные группы",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="create",
        description="Создать приватный канал для выбранных участников и ролей",
    )
    @app_commands.describe(
        название="Название приватного канала",
        доступ="Упоминания участников и ролей через пробел (можно больше 10)",
        роль="Дополнительная роль с доступом",
    )
    @staff_or_owner()
    async def group_create(
        self,
        interaction: discord.Interaction,
        название: app_commands.Range[str, 1, 80],
        доступ: app_commands.Range[str, 1, 2000] | None = None,
        роль: discord.Role | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return
        await interaction.response.defer(ephemeral=True)

        member_ids = set(map(int, _USER_MENTION.findall(доступ or "")))
        role_ids = set(map(int, _ROLE_MENTION.findall(доступ or "")))
        if роль is not None:
            role_ids.add(роль.id)
        member_ids.discard(interaction.user.id)
        role_ids.discard(guild.default_role.id)

        members = [member for member_id in member_ids if (member := guild.get_member(member_id))]
        roles = [role for role_id in role_ids if (role := guild.get_role(role_id)) and not role.managed]
        overwrite_count = 2 + len(members) + len(roles)
        if overwrite_count > _CHANNEL_LIMIT:
            await followup_v2(
                interaction,
                f"Слишком много отдельных доступов: **{overwrite_count - 2}**. "
                f"Максимум — **{_CHANNEL_LIMIT - 2}** участников и ролей суммарно.",
                ephemeral=True,
            )
            return

        category = get_category(guild, "groups_category")
        if category is None:
            if CATEGORY_IDS.get("groups_category"):
                await followup_v2(
                    interaction,
                    "Категория GROUPS из `CATEGORY_GROUPS_ID` не найдена.",
                    ephemeral=True,
                )
                return
            bot_member = guild.me
            category_overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True),
            }
            if bot_member is not None:
                category_overwrites[bot_member] = discord.PermissionOverwrite(
                    view_channel=True, manage_channels=True
                )
            category = await guild.create_category(
                CHANNELS["groups_category"],
                overwrites=category_overwrites,
                reason=f"Private group requested by {interaction.user} ({interaction.user.id})",
            )

        access = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            add_reactions=True,
            use_application_commands=True,
        )
        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: access,
        }
        for member in members:
            overwrites[member] = access
        for access_role in roles:
            overwrites[access_role] = access
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            )

        base_name = _channel_slug(str(название))
        existing = {channel.name for channel in category.channels}
        channel_name = base_name
        suffix = 2
        while channel_name in existing:
            ending = f"-{suffix}"
            channel_name = f"{base_name[:100 - len(ending)]}{ending}"
            suffix += 1

        channel = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Приватная группа • создатель {interaction.user.id}",
            reason=f"Private group requested by {interaction.user} ({interaction.user.id})",
        )
        await save_private_group(
            channel_id=channel.id,
            guild_id=guild.id,
            creator_id=interaction.user.id,
            member_ids=sorted(member.id for member in members),
            role_ids=sorted(item.id for item in roles),
        )

        access_lines = [interaction.user.mention]
        access_lines.extend(member.mention for member in members)
        access_lines.extend(item.mention for item in roles)
        spec = notice(
            code="GROUP.CREATED",
            title=f"🌸 {название}",
            summary="Приватный рабочий канал группы.",
            details=(
                ("Инициатор", interaction.user.mention),
                ("Доступ", "\n".join(access_lines)[:1024]),
            ),
            next_step="Канал виден только указанным участникам и ролям.",
            accent=WHITE_COLOR,
        )
        await send_v2(
            channel,
            embed=spec,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await followup_v2(
            interaction,
            f"Приватная группа создана: {channel.mention}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GroupsCog(bot))
