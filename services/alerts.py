"""Push the conditions the owner must not learn about from a player.

Every health signal existed already — `/dev health` shows the outbox, the task
supervisor, the ingest queue and the latency histograms. All of it was pull-only:
someone had to think to look. A dead-letter operation, a background task failing
in a loop or dropped statistics could sit unnoticed for days.

This service turns those signals into a message. Each alert is rate-limited per
key so a persistent fault produces a reminder, not a flood, and resolution is
announced once so silence keeps meaning "healthy".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import discord

from config import BOT_OWNER_ID
from services.metrics import metrics
from ui.message_specs import MessageSpec, MessageTone
from utils.discord_lookup import get_text_channel


logger = logging.getLogger(__name__)

# A firing condition repeats at most this often. Long enough not to spam a
# phone overnight, short enough that a real outage is not forgotten.
REPEAT_INTERVAL_SECONDS = 3_600.0

# Fail this many consecutive times before a background task is worth waking
# someone for. One transient Discord 503 is not an incident.
TASK_FAILURE_THRESHOLD = 3

# Thresholds the audit set as release criteria.
DB_ACQUIRE_P95_LIMIT_MS = 50.0
GATEWAY_RECONNECT_LIMIT = 5
MUSIC_EXTRACT_ERROR_LIMIT = 10


@dataclass(frozen=True, slots=True)
class Alert:
    key: str
    title: str
    summary: str
    detail: str
    next_step: str

    def to_spec(self, *, resolved: bool = False) -> MessageSpec:
        return MessageSpec(
            code=f"ALERT.{self.key.split(':')[0].upper()}",
            title=f"Восстановлено: {self.title}" if resolved else self.title,
            summary="Условие больше не выполняется." if resolved else self.summary,
            details=() if resolved else (("Подробности", self.detail),),
            next_step="Дополнительных действий не требуется." if resolved else self.next_step,
            tone=MessageTone.SUCCESS if resolved else MessageTone.ERROR,
            reference=self.key,
        )


@dataclass(slots=True)
class AlertState:
    last_sent: dict[str, float] = field(default_factory=dict)
    active: set[str] = field(default_factory=set)


class OwnerAlerts:
    """Delivers alerts to the technical channel, falling back to a DM."""

    def __init__(self, bot, *, channel_key: str = "request_actions") -> None:
        self.bot = bot
        self.channel_key = channel_key
        self.state = AlertState()

    def _now(self) -> float:
        return time.monotonic()

    def _should_send(self, key: str) -> bool:
        last = self.state.last_sent.get(key)
        return last is None or self._now() - last >= REPEAT_INTERVAL_SECONDS

    async def _deliver(self, spec: MessageSpec) -> bool:
        from ui.messages import send_v2  # local import keeps the module cheap

        for guild in self.bot.guilds:
            channel = get_text_channel(guild, self.channel_key)
            if channel is None:
                continue
            try:
                await send_v2(channel, embed=spec)
                return True
            except discord.HTTPException:
                logger.exception("Could not publish an alert to #%s", channel.name)
        if BOT_OWNER_ID is None:
            return False
        owner = self.bot.get_user(BOT_OWNER_ID)
        if owner is None:
            try:
                owner = await self.bot.fetch_user(BOT_OWNER_ID)
            except discord.HTTPException:
                return False
        try:
            await send_v2(owner, embed=spec)
            return True
        except discord.HTTPException:
            logger.exception("Could not DM the owner an alert")
            return False

    async def raise_alert(self, alert: Alert) -> None:
        """Report a condition, at most once per repeat interval."""

        self.state.active.add(alert.key)
        if not self._should_send(alert.key):
            return
        if await self._deliver(alert.to_spec()):
            self.state.last_sent[alert.key] = self._now()
            metrics.increment("alerts.sent")
            logger.warning("Alert raised: %s — %s", alert.key, alert.detail)

    async def clear_alert(self, key: str) -> None:
        """Announce recovery once, so continued silence means healthy."""

        if key not in self.state.active:
            return
        self.state.active.discard(key)
        self.state.last_sent.pop(key, None)
        resolved = Alert(key, key, "", "", "")
        if await self._deliver(resolved.to_spec(resolved=True)):
            metrics.increment("alerts.resolved")

    async def evaluate(self, snapshot: dict) -> list[Alert]:
        """Turn one health snapshot into the alerts it justifies.

        Kept separate from delivery so the rules can be tested without a
        gateway, and so a new signal is a new entry here rather than a new
        code path.
        """

        alerts: list[Alert] = []

        for name, state, failures in snapshot.get("tasks", ()):
            if state != "healthy" and failures >= TASK_FAILURE_THRESHOLD:
                alerts.append(
                    Alert(
                        key=f"task:{name}",
                        title="Фоновая задача не работает",
                        summary=f"Задача `{name}` падает подряд {failures} раз(а).",
                        detail=f"state={state}, consecutive_failures={failures}",
                        next_step="Проверьте журнал и /dev health.",
                    )
                )
            else:
                await self.clear_alert(f"task:{name}")

        dead = int(snapshot.get("outbox_dead", 0))
        if dead:
            alerts.append(
                Alert(
                    key="outbox:dead",
                    title="Операции в dead-letter",
                    summary=f"Не выполнено окончательно: **{dead}**.",
                    detail="Эти Discord-действия не будут повторены автоматически.",
                    next_step="Разберите причины и переотправьте вручную.",
                )
            )
        else:
            await self.clear_alert("outbox:dead")

        dropped = int(snapshot.get("stats_dropped", 0))
        if dropped:
            alerts.append(
                Alert(
                    key="stats:dropped",
                    title="Потеряны события статистики",
                    summary=f"Отброшено событий: **{dropped}**.",
                    detail="Очередь переполнялась; денежные события ушли в spillover.",
                    next_step="Проверьте STATS_QUEUE_SIZE и скорость записи в БД.",
                )
            )
        else:
            await self.clear_alert("stats:dropped")

        acquire_p95 = float(snapshot.get("db_acquire_p95_ms", 0.0))
        if acquire_p95 > DB_ACQUIRE_P95_LIMIT_MS:
            alerts.append(
                Alert(
                    key="db:acquire",
                    title="База данных отвечает медленно",
                    summary=f"DB acquire p95: **{acquire_p95:.0f} мс**.",
                    detail=f"Порог — {DB_ACQUIRE_P95_LIMIT_MS:.0f} мс.",
                    next_step="Проверьте пул соединений и конкурирующие записи.",
                )
            )
        else:
            await self.clear_alert("db:acquire")

        reconnects = int(snapshot.get("gateway_reconnects", 0))
        if reconnects >= GATEWAY_RECONNECT_LIMIT:
            alerts.append(
                Alert(
                    key="gateway:reconnects",
                    title="Повторные переподключения к Discord",
                    summary=f"Переподключений: **{reconnects}**.",
                    detail="Возможны потери событий gateway.",
                    next_step="Проверьте сеть хоста и статус Discord.",
                )
            )
        else:
            await self.clear_alert("gateway:reconnects")

        extract_errors = int(snapshot.get("music_extract_errors", 0))
        if extract_errors >= MUSIC_EXTRACT_ERROR_LIMIT:
            alerts.append(
                Alert(
                    key="music:extract",
                    title="Музыкальный источник нестабилен",
                    summary=f"Ошибок извлечения: **{extract_errors}**.",
                    detail="Circuit breaker мог отключить источник.",
                    next_step="Проверьте версию yt-dlp и доступность источника.",
                )
            )
        else:
            await self.clear_alert("music:extract")

        return alerts

    async def run_once(self, snapshot: dict) -> int:
        alerts = await self.evaluate(snapshot)
        for alert in alerts:
            await self.raise_alert(alert)
        return len(alerts)

