"""Persistent Memphis API panels. Discord transport only; logic is in services."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from config import CHANNELS, GUILD_ID
from services.insights import (
    battles_panel_spec,
    collect_new_battle_cards,
    market_panel_spec,
    property_panel_spec,
    rating_panel_spec,
)
from services.static_messages import StaticPayload, upsert_static_message
from ui.messages import send_v2
from utils.discord_lookup import get_text_channel

logger = logging.getLogger(__name__)
_JOB = "insights-refresh"
_INTERVAL = 300


async def _upsert_insight_panel(
    guild: discord.Guild,
    *,
    channel_key: str,
    panel_key: str,
    spec,
) -> discord.Message | None:
    channel = get_text_channel(guild, channel_key)
    if channel is None:
        raise RuntimeError(f"Канал #{CHANNELS[channel_key]} не найден")

    def build() -> StaticPayload:
        return StaticPayload(embed=spec, revision=1)

    return await upsert_static_message(
        guild=guild,
        channel=channel,
        panel_key=panel_key,
        build=build,
    )


async def update_rating_panel(guild: discord.Guild) -> discord.Message | None:
    spec = await rating_panel_spec(guild.id)
    return await _upsert_insight_panel(
        guild,
        channel_key="insights_rating",
        panel_key="insights-rating",
        spec=spec,
    )


async def update_battles_panel(guild: discord.Guild) -> discord.Message | None:
    spec = await battles_panel_spec(guild.id)
    return await _upsert_insight_panel(
        guild,
        channel_key="insights_battles",
        panel_key="insights-battles",
        spec=spec,
    )


async def update_market_panel(guild: discord.Guild) -> discord.Message | None:
    spec = await market_panel_spec(guild.id)
    return await _upsert_insight_panel(
        guild,
        channel_key="insights_market",
        panel_key="insights-market",
        spec=spec,
    )


async def update_property_panel(guild: discord.Guild) -> discord.Message | None:
    spec = await property_panel_spec(guild.id)
    return await _upsert_insight_panel(
        guild,
        channel_key="insights_property",
        panel_key="insights-property",
        spec=spec,
    )


async def publish_new_battles(guild: discord.Guild) -> None:
    channel = get_text_channel(guild, "insights_battles")
    if channel is None:
        return
    cards = await collect_new_battle_cards(guild.id)
    for spec in cards:
        await send_v2(channel, spec)


async def refresh_insight_panels(guild: discord.Guild) -> None:
    from services.external.majestic_api import majestic_integration_ready

    if not await majestic_integration_ready(guild.id):
        return
    for updater in (
        update_rating_panel,
        update_battles_panel,
        update_market_panel,
        update_property_panel,
    ):
        try:
            await updater(guild)
        except RuntimeError as exc:
            logger.warning("Insights panel skipped: %s", exc)
    try:
        await publish_new_battles(guild)
    except discord.HTTPException:
        logger.exception("Insights battle publish failed")


class InsightsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                _JOB,
                self._refresh,
                interval=_INTERVAL,
                timeout=60,
            )

    async def cog_unload(self) -> None:
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task(_JOB)

    async def _refresh(self) -> None:
        if GUILD_ID is None:
            return
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        await refresh_insight_panels(guild)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InsightsCog(bot))
