import discord
from discord import app_commands
from discord.ext import commands
from ui.layouts import marker_component_id
from ui.messages import edit_message_v2, followup_v2, respond_v2, send_v2
from config import CHANNELS, ROLES
from utils.discord_lookup import get_category
from utils.database import (
    add_contribution,
    create_fundraiser,
    delete_contribution,
    delete_fundraiser_by_channel,
    get_contributions,
    get_fundraiser_by_channel,
    get_fundraiser_panels,
    set_fundraiser_message,
)
from utils.validation import normalize_positive_amount
from utils.locks import KeyedLockPool
from ui.design import Accent
from ui.templates import notice
from utils.embeds import apply_accent
from utils.views import SafeModal, SafeView, stop_matching_persistent_views
from utils.access import administrator_or_owner


_FUNDRAISER_LOCKS = KeyedLockPool[int]()


def _family_roles(guild: discord.Guild) -> list[discord.Role]:
    role_names = (
        ROLES["owner"],
        ROLES["dep_owner"],
        ROLES["high"],
        ROLES["main"],
        ROLES["young"],
    )
    roles: list[discord.Role] = []
    seen: set[int] = set()
    for role_name in role_names:
        role = discord.utils.get(guild.roles, name=role_name)
        if role is not None and role.id not in seen:
            roles.append(role)
            seen.add(role.id)
    return roles


def _fundraiser_embed(
    fundraiser_id: int,
    target_name: str,
    total: int,
    lines: list[str],
):
    description = f"**Подарок для {target_name}** {total:,}$"
    if lines:
        description += "\n\n" + "\n".join(lines)
    return notice(
        code="FUNDRAISER.PANEL",
        title="🎁 Сбор на подарок",
        summary=description,
        next_step="Нажмите «Внести запись», чтобы добавить сумму.",
        accent=Accent.MEMBER,
        reference=f"birthday-{fundraiser_id:04d}",
    )

class FundraiserButton(SafeView):
    def __init__(self, fundraiser_id: int):
        super().__init__(timeout=None)
        self.fundraiser_id = fundraiser_id
        button = discord.ui.Button(
            label="Внести запись",
            style=discord.ButtonStyle.primary,
            emoji="💰",
            custom_id=f"fundraiser:contribute:{fundraiser_id}",
        )
        button.callback = self.contribute
        self.add_item(button)

    async def contribute(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ContributionModal(self.fundraiser_id))


class ContributionModal(SafeModal, title="Внести взнос"):
    def __init__(self, fundraiser_id: int):
        super().__init__()
        self.fundraiser_id = fundraiser_id

    amount = discord.ui.TextInput(label="Сумма взноса (в кармитах)", placeholder="150000", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            amount = normalize_positive_amount(self.amount.value)
            await add_contribution(self.fundraiser_id, interaction.user.id, interaction.user.name, amount)
            await update_fundraiser_message(interaction.channel, self.fundraiser_id)
            await followup_v2(interaction, "✅ Взнос добавлен!", ephemeral=True)
        except ValueError as exc:
            await followup_v2(interaction, str(exc), ephemeral=True)


async def update_fundraiser_message(channel, fundraiser_id):
    async with _FUNDRAISER_LOCKS.lock(fundraiser_id):
        fundraiser = await get_fundraiser_by_channel(channel.id)
        if fundraiser is None:
            return
        contributions = await get_contributions(fundraiser_id)
        total = fundraiser[3]

        prefix = f"**Подарок для {fundraiser[2]}** {total:,}$\n\n"
        lines = []
        current_length = len(prefix)
        for uid, _uname, amt in contributions:
            line = f"<@{uid}> — {amt:,}$"
            if current_length + len(line) + 1 > 3_850:
                lines.append(f"…и ещё {len(contributions) - len(lines)} участник(ов).")
                break
            lines.append(line)
            current_length += len(line) + 1
        embed = _fundraiser_embed(
            fundraiser_id,
            fundraiser[2],
            total,
            lines,
        )
        msg = None
        if fundraiser[4]:
            try:
                msg = await channel.fetch_message(fundraiser[4])
            except discord.NotFound:
                msg = None
        if msg is None:
            expected = marker_component_id(f"fundraiser:{fundraiser_id}")

            def has_marker(message: discord.Message) -> bool:
                def visit(component) -> bool:
                    return getattr(component, "id", None) == expected or any(
                        visit(child) for child in getattr(component, "children", ())
                    )

                return any(visit(component) for component in message.components)

            async for candidate in channel.history(limit=100):
                legacy = bool(
                    candidate.embeds
                    and (
                        candidate.embeds[0].footer.text
                        == f"Guinness Family • birthday-{fundraiser_id:04d}"
                    )
                )
                if (
                    candidate.author == channel.guild.me
                    and (has_marker(candidate) or legacy)
                ):
                    msg = candidate
                    await set_fundraiser_message(fundraiser_id, candidate.id)
                    break
        if msg is not None:
            await edit_message_v2(
                msg,
                embed=apply_accent(embed, Accent.MEMBER),
                view=FundraiserButton(fundraiser_id),
            )
            return
        message = await send_v2(channel,
            embed=apply_accent(embed, Accent.MEMBER),
            view=FundraiserButton(fundraiser_id),
            component_marker=f"fundraiser:{fundraiser_id}",
        )
        await set_fundraiser_message(fundraiser_id, message.id)


class FundraiserCog(
    commands.GroupCog,
    group_name="fundraiser",
    group_description="Сборы на подарки",
):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        for fundraiser_id, _, _, message_id in await get_fundraiser_panels():
            if message_id:
                self.bot.add_view(
                    FundraiserButton(fundraiser_id), message_id=message_id
                )
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                "fundraiser-reconciliation",
                self.reconcile_panels,
                interval=60,
                timeout=90,
            )

    async def cog_unload(self) -> None:
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("fundraiser-reconciliation")

    async def reconcile_panels(self) -> None:
        for fundraiser_id, guild_id, channel_id, _message_id in await get_fundraiser_panels():
            guild = self.bot.get_guild(guild_id) if guild_id else None
            channel = guild.get_channel(channel_id) if guild else None
            if guild is not None and channel is None:
                await delete_fundraiser_by_channel(channel_id)
                continue
            if isinstance(channel, discord.TextChannel):
                try:
                    # Re-render even when the panel still exists. A contribution
                    # is committed before Discord is edited, so a failed edit
                    # must be repaired without waiting for another contribution.
                    # update_fundraiser_message serializes the full
                    # fetch/adopt/edit/recreate cycle with _FUNDRAISER_LOCKS.
                    await update_fundraiser_message(channel, fundraiser_id)
                except discord.HTTPException:
                    # Supervisor records health and retries the entire pass.
                    raise

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        fundraiser = await get_fundraiser_by_channel(channel.id)
        if fundraiser is None:
            return
        await delete_fundraiser_by_channel(channel.id)
        if fundraiser[4]:
            stop_matching_persistent_views(
                self.bot,
                lambda view: getattr(view, "fundraiser_id", None)
                == fundraiser[0],
            )

    @app_commands.command(name="create", description="Создать сбор на подарок")
    @administrator_or_owner()
    async def birthday_fundraiser(self, interaction: discord.Interaction, user: discord.Member):
        category = get_category(interaction.guild, "birthday_fundraiser_category")
        if not category:
            await respond_v2(interaction,
                f"Категория '{CHANNELS['birthday_fundraiser_category']}' не найдена.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        channel_name = f"сбор-{user.name.lower().replace(' ', '-')}"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=False),
        }
        family_roles = _family_roles(interaction.guild)
        for role in family_roles:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
            )

        channel = await category.create_text_channel(name=channel_name, overwrites=overwrites)
        fid = await create_fundraiser(
            user.id,
            user.display_name,
            channel.id,
            guild_id=interaction.guild.id,
        )

        embed = apply_accent(
            _fundraiser_embed(fid, user.display_name, 0, []),
            Accent.MEMBER,
        )
        mentions = " ".join(role.mention for role in family_roles)
        message = await send_v2(channel,
            content=mentions or None,
            embed=embed,
            view=FundraiserButton(fid),
            component_marker=f"fundraiser:{fid}",
            allowed_mentions=discord.AllowedMentions(
                roles=family_roles,
                users=False,
                everyone=False,
            ),
        )
        await set_fundraiser_message(fid, message.id)
        await followup_v2(interaction, f"✅ Канал создан: {channel.mention}", ephemeral=True)

    @app_commands.command(name="contribution_remove", description="Удалить взнос")
    @administrator_or_owner()
    async def remove_contribution(self, interaction: discord.Interaction, channel: discord.TextChannel, username: str):
        await interaction.response.defer(ephemeral=True)
        fr = await get_fundraiser_by_channel(channel.id)
        if not fr:
            await followup_v2(interaction, "Это не канал сбора.", ephemeral=True)
            return
        if await delete_contribution(fr[0], username):
            await update_fundraiser_message(channel, fr[0])
            await followup_v2(interaction, f"✅ Взнос от `{username}` удалён.", ephemeral=True)
        else:
            await followup_v2(interaction, "Взнос не найден.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(FundraiserCog(bot))
