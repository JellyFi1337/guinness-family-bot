import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands

from config import (
    BOT_ACTIVITY_INTERVAL,
    BOT_ACTIVITY_TEXT,
    BOT_ACTIVITY_TEXTS,
    BOT_ACTIVITY_TYPE,
    BOT_STATUS,
    BOT_STREAM_URL,
    GUILD_ID,
    PRESENCE_GAME_INTERVAL,
    PRESENCE_STATUS_TEXT,
    RAGE_SERVER_NAME,
)
from repositories.game_online import record_game_online_sample
from services.external import close_external_clients, resolve_online_binding
from services.external.base import ResourceSnapshot, format_fetched_clock
from services.external.rage_master import extract_players
from services.metrics import metrics


logger = logging.getLogger(__name__)

STATUS_MAP = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "do_not_disturb": discord.Status.dnd,
    "invisible": discord.Status.invisible,
    "offline": discord.Status.invisible,
}


class PresenceManager:
    """Apply a static or rotating Discord presence configured through .env."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.task: asyncio.Task | None = None
        self.session: aiohttp.ClientSession | None = None
        self.last_players: int | None = None
        # What Discord is already displaying, so an unchanged render is a no-op.
        self._last_text: str | None = None
        self._index = 0

    def start(self) -> None:
        """Legacy entry point for deployments without a task supervisor.

        Prefer :meth:`tick` under TaskSupervisor: a bare ``create_task`` has no
        health record, no backoff and is invisible to maintenance mode, which
        is exactly the set of guarantees every other background job has.
        """

        if self.task is None or self.task.done():
            self.task = asyncio.create_task(
                self._run(), name="guinness-presence-rotation"
            )

    async def tick(self) -> None:
        """One presence refresh, driven by TaskSupervisor."""

        if self.bot.is_closed():
            return
        status = STATUS_MAP.get(BOT_STATUS)
        if status is None:
            logger.warning("Unknown BOT_STATUS=%r; using online", BOT_STATUS)
            status = discord.Status.online
        texts = BOT_ACTIVITY_TEXTS or (BOT_ACTIVITY_TEXT,)
        await self._refresh_once(status, texts)
        self._index += 1

    async def _refresh_once(self, status, texts) -> None:
        text, _uses_game = await self._presence_text(texts)
        if text == self._last_text:
            metrics.increment("presence.skipped")
            return
        try:
            await self.bot.change_presence(status=status, activity=self._activity(text))
        except discord.HTTPException:
            logger.exception("Failed to update bot presence")
            return
        self._last_text = text
        metrics.increment("presence.updated")
        logger.info(
            "Presence updated: status=%s type=%s text=%r",
            BOT_STATUS,
            BOT_ACTIVITY_TYPE,
            text,
        )

    async def _presence_text(self, texts: tuple[str, ...]) -> tuple[str, bool]:
        family_text = self._render(texts[self._index % len(texts)])
        if GUILD_ID is None:
            return family_text, False
        try:
            binding = await resolve_online_binding(GUILD_ID)
        except Exception:
            logger.warning("Could not resolve online source", exc_info=True)
            return family_text, False
        if binding.source is None:
            return family_text, False
        snapshot = await binding.source.fetch_online(GUILD_ID)
        text = self._render_online(binding.server_name, snapshot)
        players = snapshot.players()
        self.last_players = players
        if (
            not snapshot.stale
            and players is not None
            and snapshot.unavailable_reason is None
        ):
            try:
                await record_game_online_sample(
                    binding.server_query,
                    binding.server_name or RAGE_SERVER_NAME,
                    players,
                )
            except Exception:
                logger.exception(
                    "Could not store %s online statistics",
                    binding.server_name or RAGE_SERVER_NAME,
                )
        return text, True

    def _render_online(self, server: str, snapshot: ResourceSnapshot) -> str:
        players = snapshot.players()
        label = server or RAGE_SERVER_NAME
        if players is None:
            return f"{label}: онлайн недоступен"[:128]
        live = self._render(PRESENCE_STATUS_TEXT, players=players, server=label)
        if snapshot.stale:
            clock = format_fetched_clock(snapshot.fetched_at)
            if clock:
                stamped = f"{live} (данные от {clock})"
                return stamped[:128]
        return live[:128]

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        if self.session is not None:
            await self.session.close()
            self.session = None
        await close_external_clients()

    @staticmethod
    def _players_word(value: int) -> str:
        if value % 10 == 1 and value % 100 != 11:
            return "игрок"
        if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
            return "игрока"
        return "игроков"

    @staticmethod
    def _extract_players(payload: object, query: str) -> int:
        return extract_players(payload, query)

    def _render(
        self,
        text: str,
        *,
        players: int | None = None,
        server: str | None = None,
    ) -> str:
        users = sum(
            1 for guild in self.bot.guilds for member in guild.members if not member.bot
        )
        values = {
            "guilds": len(self.bot.guilds),
            "users": users,
            "latency": round(self.bot.latency * 1000),
            "players": players if players is not None else "—",
            "players_word": self._players_word(players) if players is not None else "игроков",
            "server": server or RAGE_SERVER_NAME,
        }
        try:
            return text.format_map(values)[:128]
        except (KeyError, ValueError):
            logger.warning("Invalid placeholder in BOT activity text: %r", text)
            return text[:128]

    def _activity(self, text: str) -> discord.BaseActivity | None:
        activity_type = BOT_ACTIVITY_TYPE
        if activity_type in {"none", "off", "disabled"}:
            return None
        if activity_type == "playing":
            return discord.Game(name=text)
        if activity_type == "streaming":
            return discord.Streaming(name=text, url=BOT_STREAM_URL)
        if activity_type == "listening":
            return discord.Activity(type=discord.ActivityType.listening, name=text)
        if activity_type == "watching":
            return discord.Activity(type=discord.ActivityType.watching, name=text)
        if activity_type == "competing":
            return discord.Activity(type=discord.ActivityType.competing, name=text)
        if activity_type == "custom":
            return discord.CustomActivity(name=text)
        logger.warning("Unknown BOT_ACTIVITY_TYPE=%r; using playing", activity_type)
        return discord.Game(name=text)

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        status = STATUS_MAP.get(BOT_STATUS)
        if status is None:
            logger.warning("Unknown BOT_STATUS=%r; using online", BOT_STATUS)
            status = discord.Status.online

        texts = BOT_ACTIVITY_TEXTS or (BOT_ACTIVITY_TEXT,)
        while not self.bot.is_closed():
            text, uses_game = await self._presence_text(texts)
            if text == self._last_text:
                # The rendered presence is identical to what Discord already
                # shows. Sending it again is a gateway round trip per minute
                # that changes nothing, and it hides real presence updates in
                # the log.
                metrics.increment("presence.skipped")
            else:
                try:
                    await self.bot.change_presence(
                        status=status,
                        activity=self._activity(text),
                    )
                except discord.HTTPException:
                    logger.exception("Failed to update bot presence")
                else:
                    self._last_text = text
                    metrics.increment("presence.updated")
                    logger.info(
                        "Presence updated: status=%s type=%s text=%r",
                        BOT_STATUS,
                        BOT_ACTIVITY_TYPE,
                        text,
                    )

            if not uses_game and len(texts) == 1:
                return
            self._index += 1
            await asyncio.sleep(
                PRESENCE_GAME_INTERVAL if uses_game else BOT_ACTIVITY_INTERVAL
            )
