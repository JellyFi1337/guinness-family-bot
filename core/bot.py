from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    AUDIT_RETENTION_DAYS,
    BOT_ACTIVITY_INTERVAL as PRESENCE_INTERVAL,
    BOT_PROFILE,
    COMMAND_CLEAR_GLOBAL,
    COMMAND_SYNC_FORCE,
    GUILD_ID,
    is_authorized_guild,
    OPERATION_RETENTION_DAYS,
    POLL_RETENTION_DAYS,
    TASK_HEALTH_HEARTBEAT_SECONDS,
)
from .log_context import (
    current_reference,
    new_correlation_id,
    notify_critical_error,
    sanitize_text,
    use_context,
)
from .profile import active_profile, format_profile_log
from .permissions import ensure_default_grants
from .settings import get_registry, warmup_runtime_settings
from infrastructure.database import database_status
from repositories.operations import get_operational_health, prune_operational_history
from services.audit import prune_old_decisions
from services.operation_worker import OperationWorker
from repositories.operations import reap_expired_operations, wake_operation_worker
from repositories.metrics_store import (
    load_counters,
    persist_snapshot,
    prune_snapshots,
)
from services.alerts import OwnerAlerts
from services.metrics import metrics
from services.task_supervisor import TaskSupervisor
from utils.database import close_db, get_setting, init_db, set_setting
from ui.messages import install_ack_instrumentation
from ui.navigation import register_navigation
from utils.presence import PresenceManager
from utils.access import install_bot_owner_command_bypass

from .command_tree import GuinnessCommandTree


logger = logging.getLogger(__name__)
_COMMAND_CACHE_VERSION = 1

EXTENSIONS = (
    "cogs.interactions",
    "cogs.anonymous_relay",
    "cogs.help",
    "cogs.admin",
    "cogs.setup_wizard",
    "cogs.config_panel",
    "cogs.panels",
    "cogs.audit",
    "cogs.members",
    "cogs.applications",
    "cogs.training",
    "cogs.contracts",
    "cogs.reports",
    "cogs.staff_queue",
    "cogs.staff_panel",
    "cogs.events",
    "cogs.treasury_v2",
    "cogs.bonus",
    "cogs.logistics",
    "cogs.progression",
    "cogs.insights",
    "cogs.member_panel",
    "cogs.roster_watch",
    "cogs.fundraiser",
    "cogs.summits",
    "cogs.voice_rooms",
    "cogs.car_park",
    "cogs.recruiting",
    "cogs.weekly_stats",
    "cogs.shop",
    "cogs.music",
    "cogs.polls",
    "cogs.giveaways",
    "cogs.groups",
    "cogs.showdown",
    "cogs.dev",
    "cogs.technical_status",
)


def _normalise_command_payload(value: Any) -> Any:
    """Remove Discord-generated fields before comparing command schemas."""

    if isinstance(value, dict):
        ignored = {
            "id",
            "application_id",
            "guild_id",
            "version",
            "name_localizations",
            "description_localizations",
        }
        return {
            key: _normalise_command_payload(item)
            for key, item in sorted(value.items())
            if key not in ignored and item is not None
        }
    if isinstance(value, list):
        return [_normalise_command_payload(item) for item in value]
    return value


def _remote_command_payload(command: app_commands.AppCommand) -> dict[str, Any]:
    payload = dict(command.to_dict())
    payload.update(
        default_member_permissions=(
            command.default_member_permissions.value
            if command.default_member_permissions is not None
            else None
        ),
        dm_permission=command.dm_permission,
        nsfw=command.nsfw,
        contexts=(
            command.allowed_contexts.to_array()
            if command.allowed_contexts is not None
            else None
        ),
        integration_types=(
            command.allowed_installs.to_array()
            if command.allowed_installs is not None
            else None
        ),
    )
    return payload


def _command_fingerprint(payload: list[dict[str, Any]]) -> str:
    normalised = sorted(
        (_normalise_command_payload(item) for item in payload),
        key=lambda item: (int(item.get("type", 1)), str(item.get("name", ""))),
    )
    encoded = json.dumps(
        normalised,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class GuinnessBot(commands.Bot):
    """Composition root for Discord, persistence and background services."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Privileged. ``members`` resolves role/permission checks without a
        # fetch on every interaction. ``message_content`` is required by the
        # Coins reward path: cogs.weekly_stats measures message length to
        # filter spam, and without this intent ``Message.content`` is empty for
        # every server message, so no message reward is ever granted.
        # Both must also be enabled in the Discord Developer Portal;
        # ``/admin preflight`` verifies that at runtime.
        intents.members = True
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            tree_cls=GuinnessCommandTree,
            # The bot manages server state and must never be user-installed or
            # invoked from DMs/private app contexts.  Declaring this at the
            # application-command root also keeps future commands fail-closed.
            allowed_contexts=app_commands.AppCommandContext(
                guild=True,
                dm_channel=False,
                private_channel=False,
            ),
            allowed_installs=app_commands.AppInstallationType(
                guild=True,
                user=False,
            ),
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
                replied_user=False,
            ),
        )
        self.task_supervisor = TaskSupervisor(
            self,
            health_interval=TASK_HEALTH_HEARTBEAT_SECONDS,
        )
        self.operation_worker = OperationWorker()
        self.presence_manager = PresenceManager(self)
        self.owner_alerts = OwnerAlerts(self)
        self._command_sync_lock = asyncio.Lock()
        self._commands_synced = False

    async def setup_hook(self) -> None:
        install_ack_instrumentation()
        await init_db(persistent=True)
        if GUILD_ID is not None:
            await warmup_runtime_settings(GUILD_ID)
        await self._log_active_profile()
        try:
            metrics.restore_counters(await load_counters())
        except Exception:
            # Diagnostics must never keep the bot from starting.
            logger.warning("Could not restore persisted metrics", exc_info=True)
        register_navigation(self)
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            logger.info("Loaded extension %s", extension)
        wrapped = install_bot_owner_command_bypass(self.tree)
        logger.info(
            "Installed bot-owner bypass for %d application-command check(s)",
            wrapped,
        )

        self.task_supervisor.start_periodic(
            "operation-outbox",
            self.operation_worker.run_batch,
            interval=0.25,
            timeout=120,
            jitter=0,
        )
        self.task_supervisor.start_periodic(
            "operation-outbox-reaper",
            reap_expired_operations,
            interval=60,
            timeout=30,
            jitter=0.1,
        )
        wake_operation_worker()
        self.task_supervisor.start_periodic(
            "persistence-retention",
            self._prune_persistence,
            interval=21_600,
            timeout=120,
        )
        # Presence used to run as a bare create_task: no health record, no
        # backoff, invisible to maintenance mode. It is a background job like
        # any other and now gets the same guarantees.
        self.task_supervisor.start_periodic(
            "presence-refresh",
            self.presence_manager.tick,
            interval=PRESENCE_INTERVAL,
            timeout=60,
        )
        self.task_supervisor.start_periodic(
            "owner-alerts",
            self._check_alerts,
            interval=120,
            timeout=60,
        )
        self.task_supervisor.start_periodic(
            "metrics-persist",
            self._persist_metrics,
            interval=300,
            timeout=60,
        )
        await self._sync_commands()

    async def _persist_metrics(self) -> None:
        """Checkpoint metrics so a restart does not erase the evidence."""

        await persist_snapshot(metrics.snapshot())
        await prune_snapshots()

    async def _log_active_profile(self) -> None:
        if GUILD_ID is None:
            return
        profile = await active_profile(GUILD_ID)
        logger.info(format_profile_log(profile))

    async def _check_alerts(self) -> None:
        """Turn the health snapshot into pushed alerts."""

        persisted = await get_operational_health()
        runtime = metrics.snapshot()
        outbox = dict(persisted["outbox"])
        stats_cog = self.get_cog("WeeklyStatsCog")
        ingestion = (
            stats_cog.ingestion_status()
            if stats_cog is not None and hasattr(stats_cog, "ingestion_status")
            else {}
        )
        backend = str(database_status().get("backend", "sqlite"))
        acquire = runtime["histograms"].get(f"database.{backend}.acquire_ms", {})
        await self.owner_alerts.run_once(
            {
                "tasks": [
                    (str(row[0]), str(row[1]), int(row[2] or 0))
                    for row in persisted["tasks"]
                ],
                "outbox_dead": int(outbox.get("dead", 0)),
                "stats_dropped": int(ingestion.get("dropped", 0)),
                "db_acquire_p95_ms": float(acquire.get("p95", 0.0)),
                "gateway_reconnects": int(
                    runtime["counters"].get("gateway.reconnects", 0)
                ),
                "music_extract_errors": int(
                    runtime["counters"].get("music.extract_error", 0)
                )
                + int(runtime["counters"].get("music.extract_timeout", 0)),
            }
        )

    async def _prune_persistence(self) -> None:
        await prune_operational_history(
            operation_retention_days=OPERATION_RETENTION_DAYS,
            poll_retention_days=POLL_RETENTION_DAYS,
        )
        try:
            peeked = get_registry().peek("AUDIT_RETENTION_DAYS")
            retention_days = peeked if isinstance(peeked, int) else AUDIT_RETENTION_DAYS
        except Exception:
            logger.exception("Could not read live AUDIT_RETENTION_DAYS")
            retention_days = AUDIT_RETENTION_DAYS
        await prune_old_decisions(retention_days)

    async def _sync_commands(self) -> None:
        async with self._command_sync_lock:
            if self._commands_synced:
                return
            if GUILD_ID is None:
                await self._sync_command_scope(None, "global")
                self._commands_synced = True
                return

            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            if COMMAND_CLEAR_GLOBAL:
                await self._clear_global_commands_once()
            await self._sync_command_scope(guild, f"guild:{GUILD_ID}")
            self._commands_synced = True

    def _local_command_payload(
        self,
        guild: discord.abc.Snowflake | None,
    ) -> list[dict[str, Any]]:
        return [
            command.to_dict(self.tree)
            for command in self.tree.get_commands(guild=guild)
        ]

    async def _sync_command_scope(
        self,
        guild: discord.abc.Snowflake | None,
        scope: str,
    ) -> None:
        local_payload = self._local_command_payload(guild)
        fingerprint = _command_fingerprint(local_payload)
        cache_key = f"command-schema:v{_COMMAND_CACHE_VERSION}:{scope}"
        cached = await get_setting(cache_key)
        if not COMMAND_SYNC_FORCE and cached == fingerprint:
            logger.info(
                "Command sync skipped for %s: schema unchanged (%d commands)",
                scope,
                len(local_payload),
            )
            return

        # Adopt an already matching Discord schema through a GET on the first
        # launch instead of issuing another rate-limited bulk PUT.
        if not COMMAND_SYNC_FORCE and cached is None:
            remote = await self.tree.fetch_commands(guild=guild)
            remote_payload = [
                _remote_command_payload(command) for command in remote
            ]
            if _command_fingerprint(remote_payload) == fingerprint:
                await set_setting(cache_key, fingerprint)
                logger.info(
                    "Command schema already current for %s (%d commands)",
                    scope,
                    len(local_payload),
                )
                return

        synced = await self.tree.sync(guild=guild)
        await set_setting(cache_key, fingerprint)
        logger.info("Synced %d commands to %s", len(synced), scope)

    async def _clear_global_commands_once(self) -> None:
        cache_key = f"command-schema:v{_COMMAND_CACHE_VERSION}:global-cleared"
        marker = "empty"
        self.tree.clear_commands(guild=None)
        if not COMMAND_SYNC_FORCE and await get_setting(cache_key) == marker:
            logger.info("Global command cleanup already completed")
            return
        removed_global = await self.tree.sync()
        await set_setting(cache_key, marker)
        logger.info(
            "Global command set cleared (%d command(s) remain)",
            len(removed_global),
        )

    async def _leave_unauthorized_guild(
        self, guild: discord.Guild, *, reason: str
    ) -> None:
        """Leave any Discord server that is not the .env-selected GUILD_ID."""

        if is_authorized_guild(guild.id):
            return
        logger.warning(
            "Leaving unauthorised guild id=%s name=%r reason=%s "
            "BOT_PROFILE=%s allowed_guild=%s",
            guild.id,
            guild.name,
            reason,
            BOT_PROFILE,
            GUILD_ID,
        )
        try:
            await guild.leave()
        except discord.HTTPException:
            logger.exception("Could not leave unauthorised guild %s", guild.id)

    async def _enforce_single_configured_guild(self) -> None:
        """On ready: drop every guild not identified for the active .env profile."""

        for guild in tuple(self.guilds):
            await self._leave_unauthorized_guild(
                guild, reason="present_at_ready_not_in_env"
            )

    async def on_ready(self) -> None:
        await self._enforce_single_configured_guild()
        if is_authorized_guild(GUILD_ID):
            configured = self.get_guild(GUILD_ID)
            if configured is not None:
                try:
                    await ensure_default_grants(configured)
                except Exception:
                    logger.exception(
                        "Could not seed default permission grants for guild %s",
                        GUILD_ID,
                    )
        remaining = [guild.id for guild in self.guilds]
        logger.info(
            "Bot %s is ready in %d guild(s) profile=%s allowed=%s remaining=%s",
            self.user,
            len(self.guilds),
            BOT_PROFILE,
            GUILD_ID,
            remaining,
        )

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        exc_type, exc, exc_tb = sys.exc_info()
        original = exc if exc is not None else RuntimeError("unknown event error")
        with use_context(
            correlation_id=new_correlation_id(),
            command=event_method,
            source="event",
        ):
            reference = current_reference()
            logger.error(
                "Unhandled event error event=%s type=%s error=%s reference=%s",
                event_method,
                type(original).__name__,
                sanitize_text(original),
                reference,
                exc_info=(
                    (exc_type, exc, exc_tb)
                    if exc_type is not None and exc is not None
                    else False
                ),
            )
            await notify_critical_error(
                self,
                key="event:unhandled",
                title="Необработанная ошибка события",
                summary=f"Событие `{event_method}` завершилось ошибкой.",
                detail=(
                    f"type={type(original).__name__} event={event_method} "
                    f"reference={reference}"
                ),
                next_step="Проверьте журнал и назовите reference.",
            )

    async def on_resumed(self) -> None:
        # A resume means the gateway connection dropped. Repeated resumes are
        # how lost events look from inside the process.
        metrics.increment("gateway.reconnects")
        # Re-check membership after resume: invites during an outage must not stick.
        await self._enforce_single_configured_guild()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._leave_unauthorized_guild(
            guild, reason="join_not_identified_in_env"
        )

    async def close(self) -> None:
        await self.presence_manager.stop()
        await self.task_supervisor.close()
        try:
            await super().close()
        finally:
            await close_db()
