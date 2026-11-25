import asyncio
import logging
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Literal, TypeAlias, overload

import discord
from discord import app_commands
from discord.ext import commands

from ui.messages import followup_v2

from config import (
    AFK_VOICE_CHANNEL,
    AFK_VOICE_CHANNEL_ID,
    CHANNELS,
    COINS_MESSAGE_EXCLUDED_CHANNEL_IDS,
    COINS_MESSAGE_MIN_LENGTH,
    COINS_VOICE_MIN_ACTIVE_MEMBERS,
    STATS_AGGREGATE_RETENTION_DAYS,
    STATS_FLUSH_BATCH_SIZE,
    STATS_QUEUE_SIZE,
    STATS_RAW_RETENTION_DAYS,
)
from repositories.game_online import GameOnlineDay, get_game_online_days
from core.profile import active_profile
from services.external import resolve_online_binding
from repositories.stats import (
    aggregate_and_prune_stats,
    drain_message_spillover,
    spill_message_events,
)
from repositories.economy import (
    EconomySettings,
    award_message_activity,
    get_economy_settings,
    get_economy_statistics,
    prune_economy_activity,
    set_economy_settings,
    sync_voice_activity,
)
from services.static_messages import StaticPayload, upsert_static_message
from services.metrics import metrics
from utils.discord_lookup import get_role, get_text_channel
from utils.access import administrator_or_owner
from ui.design import Accent
from ui.templates import notice
from utils.embeds import MessageSpec, apply_accent
from utils.locks import KeyedLockPool
from utils.database import (
    checkpoint_db,
    close_all_stats_voice_sessions,
    get_setting,
    get_weekly_stats_data,
    init_db,
    record_stats_messages_batch,
    record_stats_channel_event,
    record_stats_member_event,
    reset_stats_voice_sessions,
    set_setting,
    transition_stats_voice_session,
)


logger = logging.getLogger(__name__)
_STATS_MESSAGE_LOCKS: dict[int, asyncio.Lock] = {}
MSK = timezone(timedelta(hours=3), name="MSK")
STATS_MESSAGE_KEY = "weekly_stats_message_id"
STATS_WEEK_KEY = "weekly_stats_current_week"
_RU_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_StatsCreateRecord: TypeAlias = tuple[int, int, int, str, int, str, bool, bool]
_StatsEvent: TypeAlias = (
    tuple[Literal["create"], _StatsCreateRecord]
    | tuple[Literal["delete"], int]
)


def _is_afk_channel(channel: discord.VoiceChannel) -> bool:
    return bool(
        (AFK_VOICE_CHANNEL_ID and channel.id == AFK_VOICE_CHANNEL_ID)
        or channel.name.casefold() == AFK_VOICE_CHANNEL.casefold()
    )


def _guild_channel(guild: discord.Guild, channel_id: int):
    get_channel_or_thread = getattr(guild, "get_channel_or_thread", None)
    if callable(get_channel_or_thread):
        return get_channel_or_thread(channel_id)
    return guild.get_channel(channel_id)


_MAIN_ROLE_WARNED: set[int] = set()


def _warn_missing_main_role(guild: discord.Guild) -> None:
    guild_id = getattr(guild, "id", 0)
    if guild_id in _MAIN_ROLE_WARNED:
        return
    _MAIN_ROLE_WARNED.add(guild_id)
    logger.error(
        "Main role is unresolved in guild %s: set MAIN_ROLE_ID in bot_config.toml. "
        "Every channel is treated as staff-only until it resolves.",
        guild_id,
    )


def _is_family_visible_channel(
    guild: discord.Guild,
    channel,
) -> bool:
    """Return whether the configured Main role can view a channel."""

    if channel is None:
        return False
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        # Lightweight test doubles have no permission resolver. Real guild
        # channels always do; keeping them visible preserves test compatibility.
        return True
    main_role = get_role(guild, "main")
    if main_role is None:
        # Fail closed. The previous fallback to @everyone silently promoted
        # staff-only channels into the public weekly ranking whenever the Main
        # role was renamed or ambiguous. Marking them non-family-visible keeps
        # private activity private; ``/admin preflight`` reports the missing
        # MAIN_ROLE_ID so the cause is visible instead of leaking data.
        _warn_missing_main_role(guild)
        return False
    try:
        return bool(permissions_for(main_role).view_channel)
    except (AttributeError, TypeError):
        return False


def _channel_is_reportable(
    guild: discord.Guild,
    channel_id: int,
    stored_family_visible: bool | int | None,
) -> bool:
    channel = _guild_channel(guild, channel_id)
    if channel is not None:
        return _is_family_visible_channel(guild, channel)
    return stored_family_visible is True or stored_family_visible == 1


def _week_period(now: datetime | None = None):
    local_now = (now or datetime.now(UTC)).astimezone(MSK)
    start = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(days=7)
    return start, end, start.astimezone(UTC), end.astimezone(UTC)


def _period_for_week(week_start: date):
    start = datetime.combine(week_start, time.min, tzinfo=MSK)
    end = start + timedelta(days=7)
    return start, end, start.astimezone(UTC), end.astimezone(UTC)


def _duration(minutes: int) -> str:
    hours, mins = divmod(max(0, minutes), 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def _duration_seconds(seconds: float) -> str:
    if 0 < seconds < 60:
        return "<1 мин"
    return _duration(int(max(0, seconds) // 60))


def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    tail = abs(value) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _channel_label(channel_id: int, channel_name: str, *, deleted: bool) -> str:
    if not deleted:
        return f"<#{channel_id}>"
    name = discord.utils.escape_markdown(
        " ".join(str(channel_name or "без названия").split())
    )[:60]
    return f"`#{name}` · удалён"


@dataclass(slots=True)
class _VoiceChannelUsage:
    channel_id: int
    channel_name: str
    seconds: float = 0
    user_ids: set[int] = field(default_factory=set)
    room_creator_id: int | None = None
    family_visible: bool | None = None


@dataclass(slots=True)
class _VoiceSummary:
    by_user: dict[int, float]
    by_channel: dict[int, _VoiceChannelUsage]
    peak_concurrent: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _recovery_heartbeat(value: str | None, *, now: datetime) -> str | None:
    """Validate a persisted heartbeat and clamp clock drift to ``now``."""

    if not value:
        return None
    try:
        recovered_at = _as_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return now.isoformat()
    return min(recovered_at, now).isoformat()


def _summarize_voice_sessions(
    rows,
    *,
    start: datetime,
    end: datetime,
    now: datetime,
) -> _VoiceSummary:
    """Clip raw sessions to the report period in a single linear pass."""

    by_user: dict[int, float] = defaultdict(float)
    by_channel: dict[int, _VoiceChannelUsage] = {}
    voice_points: list[tuple[datetime, int]] = []
    for row in rows:
        user_id, channel_id, channel_name, started_at, ended_at, *extra = row
        room_creator_id = extra[0] if extra else None
        family_visible = extra[1] if len(extra) > 1 else None
        try:
            session_start = max(_as_utc(datetime.fromisoformat(started_at)), start)
            session_end = min(
                _as_utc(datetime.fromisoformat(ended_at)) if ended_at else now,
                end,
            )
        except (TypeError, ValueError):
            logger.warning(
                "Skipped malformed voice statistics row for guild channel %s",
                channel_id,
            )
            continue
        seconds = max(0.0, (session_end - session_start).total_seconds())
        if not seconds:
            continue
        user_id = int(user_id)
        channel_id = int(channel_id)
        by_user[user_id] += seconds
        usage = by_channel.get(channel_id)
        if usage is None:
            usage = _VoiceChannelUsage(channel_id, str(channel_name))
            by_channel[channel_id] = usage
        elif channel_name:
            # Prefer the latest observed name while keeping the immutable ID.
            usage.channel_name = str(channel_name)
        usage.seconds += seconds
        usage.user_ids.add(user_id)
        if usage.room_creator_id is None and room_creator_id is not None:
            usage.room_creator_id = int(room_creator_id)
        if family_visible is not None:
            usage.family_visible = bool(family_visible) or bool(
                usage.family_visible
            )
        voice_points.extend(((session_start, 1), (session_end, -1)))

    concurrent = peak_concurrent = 0
    # End points precede start points at the same instant: sessions are
    # half-open ranges and must not inflate the concurrency peak.
    for _, delta in sorted(voice_points, key=lambda item: (item[0], item[1])):
        concurrent += delta
        peak_concurrent = max(peak_concurrent, concurrent)
    return _VoiceSummary(dict(by_user), by_channel, peak_concurrent)


def _voice_channel_sections(
    guild: discord.Guild,
    usages: dict[int, _VoiceChannelUsage],
    *,
    limit: int = 5,
) -> tuple[str, str | None, str | None]:
    """Build live, temporary archive, and deleted-channel leaderboards."""

    live = sorted(
        (
            usage
            for usage in usages.values()
            if _guild_channel(guild, usage.channel_id) is not None
            and _channel_is_reportable(
                guild,
                usage.channel_id,
                usage.family_visible,
            )
        ),
        key=lambda item: item.seconds,
        reverse=True,
    )
    live_lines = "\n".join(
        (
            f"**{index}.** <#{usage.channel_id}> — "
            f"**{_duration_seconds(usage.seconds)}** · "
            f"{len(usage.user_ids)} "
            f"{_plural_ru(len(usage.user_ids), 'участник', 'участника', 'участников')}"
        )
        for index, usage in enumerate(live[:limit], 1)
    ) or "За период нет активности в доступных каналах."

    creator_seconds: dict[int, float] = defaultdict(float)
    creator_channels: dict[int, set[int]] = defaultdict(set)
    creator_participants: dict[int, set[int]] = defaultdict(set)
    deleted_regular: list[_VoiceChannelUsage] = []
    for usage in usages.values():
        if _guild_channel(guild, usage.channel_id) is not None:
            continue
        if usage.room_creator_id is None:
            if usage.family_visible:
                deleted_regular.append(usage)
            continue
        creator_seconds[usage.room_creator_id] += usage.seconds
        creator_channels[usage.room_creator_id].add(usage.channel_id)
        creator_participants[usage.room_creator_id].update(usage.user_ids)

    archived_temporary = sorted(
        creator_seconds.items(), key=lambda item: item[1], reverse=True
    )
    temporary_lines = (
        "\n".join(
            (
                f"**{index}.** <@{creator_id}> — "
                f"**{_duration_seconds(seconds)}** · "
                f"{len(creator_channels[creator_id])} "
                f"{_plural_ru(len(creator_channels[creator_id]), 'комната', 'комнаты', 'комнат')} · "
                f"{len(creator_participants[creator_id])} "
                f"{_plural_ru(len(creator_participants[creator_id]), 'участник', 'участника', 'участников')}"
            )
            for index, (creator_id, seconds) in enumerate(
                archived_temporary[:limit], 1
            )
        )
        or None
    )

    deleted_regular.sort(key=lambda item: item.seconds, reverse=True)
    deleted_lines = (
        "\n".join(
            (
                f"**{index}.** "
                f"{_channel_label(usage.channel_id, usage.channel_name, deleted=True)}"
                f" — **{_duration_seconds(usage.seconds)}**"
            )
            for index, usage in enumerate(deleted_regular[:limit], 1)
        )
        or None
    )
    return live_lines, temporary_lines, deleted_lines


def _top_lines(rows, value_formatter=str, limit=5):
    return "\n".join(
        f"**{index}.** <@{user_id}> — {value_formatter(value)}"
        for index, (user_id, value) in enumerate(rows[:limit], 1)
    ) or "Пока нет данных."


def build_game_online_embed(
    rows: list[GameOnlineDay],
    *,
    weeks: int,
    server_name: str,
    server_query: str,
    now: datetime | None = None,
) -> MessageSpec:
    local_today = (now or datetime.now(UTC)).astimezone(MSK).date()
    current_week = local_today - timedelta(days=local_today.weekday())
    by_day = {datetime.fromisoformat(row.day).date(): row for row in rows}
    latest = max(rows, key=lambda row: row.last_sample_at) if rows else None
    details: list[tuple[str, str]] = []
    for offset in range(weeks):
        week_start = current_week - timedelta(weeks=offset)
        lines: list[str] = []
        week_rows: list[GameOnlineDay] = []
        for day_offset, weekday in enumerate(_RU_WEEKDAYS):
            day = week_start + timedelta(days=day_offset)
            row = by_day.get(day)
            if row is None:
                lines.append(f"**{weekday} {day:%d.%m}:** —")
                continue
            week_rows.append(row)
            lines.append(
                f"**{weekday} {day:%d.%m}:** пик **{row.peak_players:,}** "
                f"• средний {row.average_players:,.0f}"
            )
        if week_rows:
            week_peak = max(row.peak_players for row in week_rows)
            samples = sum(row.sample_count for row in week_rows)
            average = sum(
                row.average_players * row.sample_count for row in week_rows
            ) / max(1, samples)
            lines.append(f"\nПик недели: **{week_peak:,}** • средний: **{average:,.0f}**")
        details.append(
            (
                f"Неделя {week_start:%d.%m}–{week_start + timedelta(days=6):%d.%m}",
                "\n".join(lines),
            )
        )
    return apply_accent(
        notice(
            code="STATS.ONLINE",
            title=f"🎮 Онлайн · {server_name}",
            summary=(
                f"Последнее значение: **{latest.last_players:,}**"
                if latest is not None
                else "Данные ещё не собраны."
            ),
            details=tuple(details),
            next_step=f"Поиск: {server_query} • время МСК",
        ),
        Accent.SERVICE,
    )


async def build_weekly_stats_embed(
    guild: discord.Guild,
    *,
    week_start: date | None = None,
    now: datetime | None = None,
) -> MessageSpec:
    report_now = _as_utc(now or datetime.now(UTC))
    # Round to the refresh interval. The raw timestamp changed on every build,
    # so the rendered payload was always "different" and the static-message
    # service re-edited the message every five minutes even on a dead server.
    report_now = report_now.replace(
        minute=(report_now.minute // _STATS_TIMESTAMP_QUANTUM_MINUTES)
        * _STATS_TIMESTAMP_QUANTUM_MINUTES,
        second=0,
        microsecond=0,
    )
    if week_start is None:
        local_start, local_end, start, end = _week_period(report_now)
    else:
        local_start, local_end, start, end = _period_for_week(week_start)
    current_week_start = _week_period(report_now)[0].date()
    is_current_week = local_start.date() == current_week_start
    data = await get_weekly_stats_data(guild.id, start.isoformat(), end.isoformat())

    joined = left = 0
    for event_type, count in data["member_events"]:
        if event_type == "join":
            joined = count
        elif event_type == "leave":
            left = count
    humans = sum(not member.bot for member in guild.members)
    message_rows = data["message_activity"]
    messages = sum(row[2] for row in message_rows)
    unique_authors = len({row[1] for row in message_rows})
    deleted_messages = sum(row[3] or 0 for row in message_rows)

    voice = _summarize_voice_sessions(
        data["voice_sessions"],
        start=start,
        end=end,
        now=min(report_now, end),
    )
    total_voice_seconds = sum(voice.by_user.values())
    live_voice_lines, temporary_voice_lines, deleted_voice_lines = (
        _voice_channel_sections(guild, voice.by_channel)
    )

    message_by_user: Counter[int] = Counter()
    for _, user_id, count, _ in message_rows:
        message_by_user[user_id] += count
    all_users = set(message_by_user) | set(voice.by_user)
    activity = sorted(
        (
            (
                user_id,
                message_by_user[user_id],
                voice.by_user.get(user_id, 0),
                message_by_user[user_id] + voice.by_user.get(user_id, 0) / 300,
            )
            for user_id in all_users
        ),
        key=lambda row: row[3],
        reverse=True,
    )[:5]
    activity_lines = "\n".join(
        (
            f"**{index}.** <@{user_id}> — **{msg_count:,}** сообщ. · "
            f"**{_duration_seconds(voice_seconds)}** в голосовых"
        )
        for index, (user_id, msg_count, voice_seconds, _) in enumerate(activity, 1)
    ) or "Активность за период ещё не зафиксирована."

    reportable_text_channels = [
        (channel_id, channel_name, count)
        for channel_id, channel_name, count, family_visible in data["text_channels"]
        if _channel_is_reportable(guild, channel_id, family_visible)
    ][:5]
    text_lines = "\n".join(
        (
            f"**{index}.** "
            f"{_channel_label(channel_id, channel_name, deleted=_guild_channel(guild, channel_id) is None)}"
            f" — **{count:,}** сообщ."
        )
        for index, (channel_id, channel_name, count) in enumerate(
            reportable_text_channels, 1
        )
    ) or "Активность за период ещё не зафиксирована."

    created_channels = sum(
        count
        for event_type, channel_id, _, family_visible, count in data["channel_events"]
        if event_type == "create"
        and _channel_is_reportable(guild, channel_id, family_visible)
    )
    deleted_channels = sum(
        count
        for event_type, channel_id, _, family_visible, count in data["channel_events"]
        if event_type == "delete"
        and _channel_is_reportable(guild, channel_id, family_visible)
    )

    summit_groups: dict[str, list[tuple[int, int]]] = {
        "contract": [],
        "list": [],
        "vzp_gathering": [],
    }
    for kind, creator_id, count in data["summit_leaders"]:
        if kind in summit_groups:
            summit_groups[kind].append((creator_id, count))
    for rows in summit_groups.values():
        rows.sort(key=lambda item: item[1], reverse=True)

    processed = {
        (action_kind, outcome): count
        for action_kind, outcome, count in data["processed_actions"]
    }
    tickets_closed = processed.get(("ticket", "closed"), 0)
    applications_approved = processed.get(("application", "approved"), 0)
    applications_rejected = processed.get(("application", "rejected"), 0)
    active_days = max(
        1,
        min(7, (report_now.astimezone(MSK).date() - local_start.date()).days + 1),
    )
    daily_average = messages / active_days
    event_count = sum(count for _, _, count in data["summit_leaders"])

    details: list[tuple[str, str]] = [
        (
            "👥 Состав",
            (
                f"Участников: **{humans:,}**\n"
                f"Пришло / ушло: **+{joined} / −{left}**\n"
                f"Баланс недели: **{joined - left:+d}**"
            ),
        ),
        (
            "💬 Общение",
            (
                f"Сообщений: **{messages:,}**\n"
                f"Активных авторов: **{unique_authors:,}**\n"
                f"Среднее в день: **{daily_average:,.1f}**"
            ),
        ),
        (
            "🔊 Голос",
            (
                f"Время участников: **{_duration_seconds(total_voice_seconds)}**\n"
                f"Участников: **{len(voice.by_user):,}**\n"
                f"Пик одновременно: **{voice.peak_concurrent:,}**"
            ),
        ),
        ("🏆 Лидеры общей активности", activity_lines),
        ("#️⃣ Текстовые каналы", text_lines),
        ("🔉 Голосовые каналы", live_voice_lines),
    ]
    if temporary_voice_lines:
        details.append(
            (
                "🕓 Архив временных комнат",
                (
                    f"{temporary_voice_lines}\n"
                    "-# Суммируется время всех участников в удалённых комнатах создателя."
                ),
            )
        )
    if deleted_voice_lines:
        details.append(("🗑️ Удалённые голосовые каналы", deleted_voice_lines))
    details.extend(
        (
            (
                "🛠️ Работа команды",
                (
                    f"Тикетов закрыто: **{tickets_closed:,}**\n"
                    f"Заявок принято / отклонено: "
                    f"**{applications_approved:,} / {applications_rejected:,}**\n"
                    f"Всего решений: "
                    f"**{tickets_closed + applications_approved + applications_rejected:,}**"
                ),
            ),
            (
                "🗂️ События сервера",
                (
                    f"Каналы созданы / удалены: "
                    f"**{created_channels:,} / {deleted_channels:,}**\n"
                    f"Запущено мероприятий: **{event_count:,}**\n"
                    f"Удалено сообщений: **{deleted_messages:,}**"
                ),
            ),
            (
                "📜 Контракты",
                _top_lines(
                    summit_groups["contract"], lambda value: f"{value} запуск.", 3
                ),
            ),
            (
                "📋 Сборы",
                _top_lines(summit_groups["list"], lambda value: f"{value} запуск.", 3),
            ),
            (
                "🗺️ Архивные сборы",
                _top_lines(
                    summit_groups["vzp_gathering"], lambda value: f"{value} запуск.", 3
                ),
            ),
        )
    )
    return apply_accent(
        notice(
            code="STATS.WEEKLY",
            title="📊 Еженедельный отчёт сервера",
            summary=(
                f"**{local_start:%d.%m.%Y} — "
                f"{(local_end - timedelta(seconds=1)):%d.%m.%Y} · МСК**\n"
                + (
                    "Живая сводка текущей недели: состав, сообщения, голосовая "
                    "активность и работа команды."
                    if is_current_week
                    else "Итоговая сводка завершённой недели. Этот отчёт больше "
                    "не обновляется."
                )
            ),
            details=tuple(details),
            next_step=(
                "Рейтинг: 1 сообщение = 1 балл, 5 минут в голосовом = 1 балл "
                + (
                    "• обновляется автоматически"
                    if is_current_week
                    else "• неделя завершена"
                )
            ),
        ),
        Accent.SERVICE,
    )


def _stats_week_setting_key(guild_id: int) -> str:
    return f"{STATS_WEEK_KEY}:{guild_id}"


def _stats_message_setting_key(guild_id: int) -> str:
    """Guild-scoped id of the live weekly message.

    The week itself was already stored per guild while the message id was
    written to one global key, so the two could describe different servers.
    """

    return f"{STATS_MESSAGE_KEY}:{guild_id}"


# How many missed weeks a restart will backfill. A bot that was offline longer
# than this had no events to report for the oldest weeks anyway.
_MAX_BACKFILL_WEEKS = 12

# The report is rebuilt every five minutes; quantising its timestamp to the same
# interval keeps an unchanged week byte-identical between passes.
_STATS_TIMESTAMP_QUANTUM_MINUTES = 5


async def _upsert_weekly_stats_message(
    guild: discord.Guild,
    week_start: date,
    *,
    allow_legacy_adoption: bool,
    now: datetime | None = None,
) -> discord.Message | None:
    channel = get_text_channel(guild, "weekly_stats")
    if channel is None:
        raise RuntimeError(f"Канал #{CHANNELS['weekly_stats']} не найден")

    async def build() -> StaticPayload:
        embed = await build_weekly_stats_embed(
            guild,
            week_start=week_start,
            now=now,
        )
        return StaticPayload(
            embed=apply_accent(embed, Accent.SERVICE),
            revision=4,
        )

    message = await upsert_static_message(
        guild=guild,
        channel=channel,
        panel_key=f"weekly-statistics:{week_start.isoformat()}",
        build=build,
        legacy_setting_key=(
            STATS_MESSAGE_KEY if allow_legacy_adoption else None
        ),
        legacy_match=(
            lambda item: bool(
                item.embeds
                and item.embeds[0].title
                in {
                    "📊 Еженедельная статистика сервера",
                    "📊 Еженедельный отчёт сервера",
                }
            )
            if allow_legacy_adoption
            else None
        ),
    )
    return message


async def update_weekly_stats_message(
    guild: discord.Guild,
) -> discord.Message | None:
    rotation_now = datetime.now(UTC)
    local_start, _, _, _ = _week_period(rotation_now)
    current_week = local_start.date()
    week_setting_key = _stats_week_setting_key(guild.id)
    stored_value = await get_setting(week_setting_key)
    stored_week: date | None = None
    if stored_value:
        try:
            stored_week = date.fromisoformat(stored_value)
        except ValueError:
            logger.warning(
                "Invalid weekly statistics period guild=%s value=%r",
                guild.id,
                stored_value,
            )

    if stored_week is not None and stored_week < current_week:
        # Finalise every week that elapsed while the bot was down, not just the
        # one it happened to remember. Publishing only the stored week silently
        # dropped the report for each intermediate week.
        missed: list[date] = []
        cursor = stored_week
        while cursor < current_week and len(missed) < _MAX_BACKFILL_WEEKS:
            missed.append(cursor)
            cursor = cursor + timedelta(days=7)
        skipped = (current_week - cursor).days // 7
        if skipped > 0:
            logger.warning(
                "Weekly statistics backfill capped: %d week(s) before %s skipped",
                skipped,
                cursor.isoformat(),
            )
        for week in missed:
            _, _, _, week_end = _period_for_week(week)
            previous = await _upsert_weekly_stats_message(
                guild,
                week,
                allow_legacy_adoption=False,
                now=week_end,
            )
            if previous is None:
                return None
    elif stored_week is not None and stored_week > current_week:
        logger.warning(
            "Weekly statistics period is ahead of local clock guild=%s value=%s",
            guild.id,
            stored_week,
        )
        stored_week = None

    message = await _upsert_weekly_stats_message(
        guild,
        current_week,
        allow_legacy_adoption=stored_week is None,
        now=rotation_now,
    )
    if message is None:
        return None
    await set_setting(_stats_message_setting_key(guild.id), str(message.id))
    # Kept in step for one release so a rolling restart can still read the old
    # global key; the guild-scoped key above is authoritative.
    await set_setting(STATS_MESSAGE_KEY, str(message.id))
    await set_setting(week_setting_key, current_week.isoformat())
    return message


class WeeklyStatsCog(
    commands.GroupCog,
    group_name="stats",
    group_description="Статистика сервера и экономики",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._stats_events: asyncio.Queue[_StatsEvent] = asyncio.Queue(
            maxsize=STATS_QUEUE_SIZE
        )
        self._retry_events: deque[_StatsEvent] = deque()
        self._flush_lock = asyncio.Lock()
        self._sessions_restored = False
        self._coin_voice_initialized = False
        self._voice_session_locks = KeyedLockPool[tuple[int, int]]()
        self._economy_settings_cache: dict[int, tuple[float, EconomySettings]] = {}
        self._dropped_events = 0
        self._spilled_events = 0
        self._last_overflow_warning = 0.0

    def _update_ingestion_metrics(self) -> None:
        metrics.gauge("stats.queue", self._stats_events.qsize())
        metrics.gauge("stats.retry_queue", len(self._retry_events))
        metrics.gauge("stats.dropped", self._dropped_events)
        metrics.gauge("stats.spilled", self._spilled_events)

    async def cog_load(self):
        await init_db()
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                "stats-ingest",
                self._flush_message_events,
                interval=0.25,
                timeout=30,
                jitter=0,
            )
            supervisor.start_periodic(
                "stats-maintenance",
                self.stats_maintenance,
                interval=300,
                timeout=180,
            )
            supervisor.start_periodic(
                "stats-retention",
                self.stats_retention,
                interval=3_600,
                timeout=600,
            )
            supervisor.start_periodic(
                "stats-voice-heartbeat",
                self._record_voice_heartbeats,
                interval=60,
                timeout=20,
                jitter=0,
            )
            supervisor.start_periodic(
                "coin-voice-accrual",
                self._accrue_voice_coins,
                interval=30,
                timeout=25,
                jitter=0,
            )

    async def cog_unload(self):
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("stats-ingest")
            await supervisor.stop_task("stats-maintenance")
            await supervisor.stop_task("stats-retention")
            await supervisor.stop_task("stats-voice-heartbeat")
            await supervisor.stop_task("coin-voice-accrual")
        if self._sessions_restored:
            now = datetime.now(UTC).isoformat()
            for guild in self.bot.guilds:
                await close_all_stats_voice_sessions(guild.id, now)
                await set_setting(f"stats_heartbeat_{guild.id}", now)
        while self._retry_events or not self._stats_events.empty():
            await self._flush_message_events()

    async def _flush_message_events(self) -> None:
        async with self._flush_lock:
            events: list[_StatsEvent] = []
            while self._retry_events and len(events) < STATS_FLUSH_BATCH_SIZE:
                events.append(self._retry_events.popleft())
            while len(events) < STATS_FLUSH_BATCH_SIZE:
                try:
                    events.append(self._stats_events.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if len(events) < STATS_FLUSH_BATCH_SIZE:
                # Replay anything that overflowed earlier. Idempotency lives in
                # coin_activity_ledger, so a replayed message cannot pay twice.
                for record in await drain_message_spillover(
                    STATS_FLUSH_BATCH_SIZE - len(events)
                ):
                    events.append(("create", record))
            if not events:
                self._update_ingestion_metrics()
                return
            records: list[_StatsCreateRecord] = []
            deleted_ids: list[int] = []
            for event in events:
                if event[0] == "create":
                    records.append(event[1])
                else:
                    deleted_ids.append(event[1])
            try:
                await record_stats_messages_batch(
                    [(*record[:6], record[7]) for record in records],
                    deleted_ids,
                )
                if records:
                    guild_ids = {int(record[1]) for record in records}
                    settings = {
                        guild_id: await self._economy_settings(guild_id)
                        for guild_id in guild_ids
                    }
                    await award_message_activity(records, settings)
            except Exception:
                self._retry_events.extendleft(reversed(events))
                self._update_ingestion_metrics()
                raise
            self._update_ingestion_metrics()

    @overload
    async def _enqueue_stat(
        self,
        kind: Literal["create"],
        payload: _StatsCreateRecord,
    ) -> None: ...

    @overload
    async def _enqueue_stat(
        self,
        kind: Literal["delete"],
        payload: int,
    ) -> None: ...

    async def _enqueue_stat(
        self,
        kind: Literal["create", "delete"],
        payload: _StatsCreateRecord | int,
    ) -> None:
        # Gateway dispatch creates one task per listener. Blocking producers
        # when SQLite is unavailable would merely move the unbounded queue into
        # asyncio tasks and eventually exhaust memory. Keep telemetry best-
        # effort after the configured in-memory buffer is exhausted.
        try:
            if kind == "create":
                assert isinstance(payload, tuple)
                event: _StatsEvent = ("create", payload)
            else:
                assert isinstance(payload, int)
                event = ("delete", payload)
            self._stats_events.put_nowait(event)
        except asyncio.QueueFull:
            pass
        else:
            self._update_ingestion_metrics()
            return

        # The buffer is exhausted. A "create" event carries the eligibility flag
        # that pays Coins, so it is spilled to durable storage rather than
        # dropped; a "delete" event is pure statistics and may be lost with a
        # metric. Writing here is intentional backpressure and only happens
        # once STATS_QUEUE_SIZE events are already waiting.
        if kind == "create":
            try:
                await spill_message_events([payload])
                self._spilled_events += 1
                metrics.increment("stats.events_spilled")
            except Exception:
                self._dropped_events += 1
                metrics.increment("stats.events_dropped")
                logger.exception("Could not spill a message event carrying Coins")
        else:
            self._dropped_events += 1
            metrics.increment("stats.events_dropped")

        now = asyncio.get_running_loop().time()
        if now - self._last_overflow_warning >= 60:
            logger.warning(
                "Statistics queue is full; spilled=%d dropped=%d queued=%d retry=%d",
                self._spilled_events,
                self._dropped_events,
                self._stats_events.qsize(),
                len(self._retry_events),
            )
            self._last_overflow_warning = now
        self._update_ingestion_metrics()

    def _maintenance_active(self) -> bool:
        supervisor = getattr(self.bot, "task_supervisor", None)
        return bool(supervisor is not None and supervisor.paused)

    def ingestion_status(self) -> dict[str, int]:
        self._update_ingestion_metrics()
        return {
            "spilled": self._spilled_events,
            "queued": self._stats_events.qsize(),
            "retry": len(self._retry_events),
            "dropped": self._dropped_events,
        }

    async def discard_pending_events(self) -> int:
        """Discard pre-reset telemetry while the supervisor maintenance gate is held."""

        async with self._flush_lock:
            discarded = len(self._retry_events)
            self._retry_events.clear()
            while True:
                try:
                    self._stats_events.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    discarded += 1
            self._update_ingestion_metrics()
            return discarded

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if self._maintenance_active() or message.guild is None or message.author.bot:
            return
        await self._enqueue_stat(
            "create",
            (
                message.id,
                message.guild.id,
                message.channel.id,
                getattr(message.channel, "name", "неизвестный-канал"),
                message.author.id,
                message.created_at.isoformat(),
                (
                    len(message.content.strip()) >= COINS_MESSAGE_MIN_LENGTH
                    and message.channel.id
                    not in COINS_MESSAGE_EXCLUDED_CHANNEL_IDS
                ),
                _is_family_visible_channel(message.guild, message.channel),
            ),
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not self._maintenance_active() and payload.guild_id is not None:
            await self._enqueue_stat("delete", payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if not self._maintenance_active() and payload.guild_id is not None:
            for message_id in payload.message_ids:
                await self._enqueue_stat("delete", message_id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not self._maintenance_active() and not member.bot:
            await record_stats_member_event(member.guild.id, member.id, "join")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not self._maintenance_active() and not member.bot:
            await record_stats_member_event(member.guild.id, member.id, "leave")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not self._maintenance_active():
            await record_stats_channel_event(
                channel.guild.id,
                channel.id,
                channel.name,
                "create",
                _is_family_visible_channel(channel.guild, channel),
            )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if not self._maintenance_active():
            await record_stats_channel_event(
                channel.guild.id,
                channel.id,
                channel.name,
                "delete",
                _is_family_visible_channel(channel.guild, channel),
            )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if self._maintenance_active() or member.bot:
            return
        channel_changed = before.channel != after.channel
        eligibility_changed = (
            before.deaf != after.deaf or before.self_deaf != after.self_deaf
        )
        if not channel_changed and not eligibility_changed:
            return
        if channel_changed:
            target = (
                after.channel
                if after.channel is not None and not _is_afk_channel(after.channel)
                else None
            )
            async with self._voice_session_locks.lock(
                (member.guild.id, member.id)
            ):
                await transition_stats_voice_session(
                    member.guild.id,
                    member.id,
                    target.id if target else None,
                    target.name if target else None,
                    (
                        _is_family_visible_channel(member.guild, target)
                        if target
                        else None
                    ),
                )
        await self._sync_affected_voice_coins(member, before, after)

    async def _economy_settings(self, guild_id: int) -> EconomySettings:
        now = asyncio.get_running_loop().time()
        cached = self._economy_settings_cache.get(guild_id)
        if cached is not None and now - cached[0] < 60:
            return cached[1]
        settings = await get_economy_settings(guild_id)
        self._economy_settings_cache[guild_id] = (now, settings)
        return settings

    @staticmethod
    def _active_voice_members(guild: discord.Guild) -> list[tuple[int, int]]:
        active: list[tuple[int, int]] = []
        for channel in guild.voice_channels:
            if _is_afk_channel(channel):
                continue
            members = [
                member
                for member in channel.members
                if not member.bot
                and member.voice is not None
                and not member.voice.deaf
                and not member.voice.self_deaf
            ]
            if len(members) < COINS_VOICE_MIN_ACTIVE_MEMBERS:
                continue
            active.extend((member.id, channel.id) for member in members)
        return active

    async def _sync_affected_voice_coins(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        affected: set[int] = {member.id}
        current: dict[int, int] = {}
        channels = {
            channel.id: channel
            for channel in (before.channel, after.channel)
            if channel is not None
        }
        for channel in channels.values():
            eligible = [
                candidate
                for candidate in channel.members
                if not candidate.bot
                and candidate.voice is not None
                and not candidate.voice.deaf
                and not candidate.voice.self_deaf
            ]
            affected.update(
                candidate.id for candidate in channel.members if not candidate.bot
            )
            if (
                not _is_afk_channel(channel)
                and len(eligible) >= COINS_VOICE_MIN_ACTIVE_MEMBERS
            ):
                current.update(
                    {candidate.id: channel.id for candidate in eligible}
                )
        await sync_voice_activity(
            member.guild.id,
            current.items(),
            await self._economy_settings(member.guild.id),
            observed_user_ids=affected,
        )

    async def _sync_guild_voice_coins(
        self, guild: discord.Guild, *, reset: bool = False
    ) -> None:
        await sync_voice_activity(
            guild.id,
            self._active_voice_members(guild),
            await self._economy_settings(guild.id),
            reset=reset,
        )

    async def _accrue_voice_coins(self) -> None:
        reset = not self._coin_voice_initialized
        for guild in self.bot.guilds:
            await self._sync_guild_voice_coins(guild, reset=reset)
        self._coin_voice_initialized = True

    async def stats_maintenance(self):
        """Five-minute tick: keep live state and the weekly panel current.

        Retention no longer runs here. Draining 90 days of raw events every
        five minutes — and forcing a full ANALYZE with it — was pure background
        load on a database whose autovacuum already tracks real churn.
        """

        if not self._sessions_restored:
            await self._restore_voice_sessions()
            self._sessions_restored = True
        await self._record_voice_heartbeats()
        await self._flush_message_events()
        for guild in self.bot.guilds:
            if await get_setting(
                _stats_message_setting_key(guild.id)
            ) or await get_setting(STATS_MESSAGE_KEY):
                try:
                    await update_weekly_stats_message(guild)
                except (RuntimeError, discord.HTTPException):
                    logger.exception("Failed to update weekly stats for guild %s", guild.id)

    async def stats_retention(self):
        """Daily sweep: aggregate raw events and drop what is past retention.

        ``aggregate_and_prune_stats`` carries its own once-per-day guard, so a
        restart cannot make this run more often than intended; economy pruning
        is bounded per pass and drains across days if a backlog exists.
        """

        result = await aggregate_and_prune_stats(
            raw_retention_days=STATS_RAW_RETENTION_DAYS,
            aggregate_retention_days=STATS_AGGREGATE_RETENTION_DAYS,
        )
        if result.get("skipped"):
            return
        removed = await prune_economy_activity(
            STATS_RAW_RETENTION_DAYS, STATS_AGGREGATE_RETENTION_DAYS
        )
        # Cheap probe on PostgreSQL, WAL checkpoint on the SQLite fallback.
        await checkpoint_db()
        logger.info(
            "Statistics retention pass complete: %s; coins ledger=%d daily=%d",
            {key: value for key, value in result.items() if value},
            removed.get("ledger", 0),
            removed.get("daily", 0),
        )

    async def _record_voice_heartbeats(self) -> None:
        # Never overwrite the last known-good heartbeat before startup
        # recovery has used it to close sessions left open by a crash.
        if not self._sessions_restored:
            return
        now = datetime.now(UTC).isoformat()
        for guild in self.bot.guilds:
            await set_setting(f"stats_heartbeat_{guild.id}", now)

    async def restore_after_reset(self) -> None:
        """Recreate live voice/economy sessions after an administrative reset."""

        self._sessions_restored = False
        self._coin_voice_initialized = False
        self._economy_settings_cache.clear()
        await self._restore_voice_sessions()
        self._sessions_restored = True
        await self._record_voice_heartbeats()

    async def _restore_voice_sessions(self):
        now = datetime.now(UTC)
        for guild in self.bot.guilds:
            heartbeat = _recovery_heartbeat(
                await get_setting(f"stats_heartbeat_{guild.id}"),
                now=now,
            )
            active_sessions = []
            for channel in guild.voice_channels:
                if _is_afk_channel(channel):
                    continue
                for member in channel.members:
                    if not member.bot:
                        active_sessions.append(
                            (
                                member.id,
                                channel.id,
                                channel.name,
                                _is_family_visible_channel(guild, channel),
                            )
                        )
            await reset_stats_voice_sessions(guild.id, heartbeat, active_sessions)
            if not self._coin_voice_initialized:
                await self._sync_guild_voice_coins(guild, reset=True)
        self._coin_voice_initialized = True

    @app_commands.command(name="weekly", description="Получить недельную статистику")
    @app_commands.checks.cooldown(1, 20.0, key=lambda interaction: interaction.user.id)
    async def stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._flush_message_events()
        embed = apply_accent(
            await build_weekly_stats_embed(interaction.guild),
            Accent.SERVICE,
        )
        await followup_v2(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="update", description="Обновить недельную статистику")
    @administrator_or_owner()
    async def stats_update(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._flush_message_events()
        message = await update_weekly_stats_message(interaction.guild)
        if message is None:
            await followup_v2(
                interaction,
                "Статичные сообщения отключены. Сначала выполните `/admin setup`.",
                ephemeral=True,
            )
            return
        await followup_v2(interaction,
            f"Статистика обновлена: {message.jump_url}", ephemeral=True
        )

    @app_commands.command(
        name="server_online",
        description="Показать статистику онлайна игрового сервера",
    )
    @app_commands.describe(weeks="Количество недель: от 1 до 12")
    @app_commands.checks.cooldown(
        1,
        15.0,
        key=lambda interaction: interaction.user.id,
    )
    async def server_online_stats(
        self,
        interaction: discord.Interaction,
        weeks: app_commands.Range[int, 1, 12] = 4,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await followup_v2(
                interaction,
                embed=notice(
                    code="STATS.GUILD_ONLY",
                    title="Онлайн сервера",
                    summary="Статистика онлайна доступна только на сервере.",
                    next_step="Повторите команду на сервере семьи.",
                ),
                ephemeral=True,
            )
            return
        binding = await resolve_online_binding(guild.id)
        profile = await active_profile(guild.id)
        server_name = binding.server_name or profile.server_label
        server_query = binding.server_query
        if not server_query:
            await followup_v2(
                interaction,
                embed=notice(
                    code="STATS.ONLINE_SOURCE",
                    title="Онлайн сервера",
                    summary=(
                        "Источник онлайна не настроен. Поиск в master-list "
                        "не выполняется."
                    ),
                    next_step=(
                        "Для архивного RAGE задайте строку поиска в профиле. "
                        "Активный Memphis этот источник не использует."
                    ),
                ),
                ephemeral=True,
            )
            return
        local_today = datetime.now(UTC).astimezone(MSK).date()
        current_week = local_today - timedelta(days=local_today.weekday())
        since = current_week - timedelta(weeks=int(weeks) - 1)
        rows = await get_game_online_days(
            server_query,
            since_day=since.isoformat(),
        )
        await followup_v2(
            interaction,
            embed=build_game_online_embed(
                rows,
                weeks=int(weeks),
                server_name=server_name,
                server_query=server_query,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="economy",
        description="Статистика начисления Guinn Coins",
    )
    @app_commands.describe(days="Период статистики: от 1 до 365 дней")
    @administrator_or_owner()
    async def coins_economy_stats(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365] = 7,
    ):
        await interaction.response.defer(ephemeral=True)
        await self._flush_message_events()
        start = (datetime.now(UTC) - timedelta(days=int(days) - 1)).date().isoformat()
        data = await get_economy_statistics(interaction.guild_id, start)
        settings = await self._economy_settings(interaction.guild_id)
        sources = {str(row[0]): row[1:] for row in data["sources"]}

        def source_line(source: str, label: str) -> str:
            units, coins, events, eligible = sources.get(source, (0, 0, 0, 0))
            return (
                f"**{label}:** {int(coins or 0):,} Coins • "
                f"{int(eligible or 0):,}/{int(events or 0):,} событий • "
                f"начислено {int(units or 0) / 1000:g} до округления"
            )

        top = "\n".join(
            f"**{index}.** <@{user_id}> — {int(total):,} Coins"
            for index, (user_id, total) in enumerate(data["top"], 1)
        ) or "Пока нет данных."
        embed = apply_accent(
            notice(
                code="STATS.ECONOMY",
                title="🪙 Экономика активности",
                summary=f"Период: последние **{days} дн.**",
                details=(
                    (
                        "Текущие ставки",
                        (
                            f"Сообщение: **{settings.message_reward_milli / 1000:g}**\n"
                            f"Минута в голосовом: **{settings.voice_reward_milli / 1000:g}**\n"
                            f"Антиспам-интервал: **{settings.message_cooldown_seconds} сек.**"
                        ),
                    ),
                    (
                        "Начисления",
                        "\n".join(
                            (
                                source_line("message", "Сообщения"),
                                source_line("voice", "Голос"),
                            )
                        ),
                    ),
                    ("Топ получателей", top),
                ),
                next_step=(
                    f"Дробный остаток всех участников: "
                    f"{int(data['pending_milli']) / 1000:g} Coins"
                ),
            ),
            Accent.TREASURY,
        )
        await followup_v2(interaction, embed=embed, ephemeral=True)

    @app_commands.command(
        name="economy_config",
        description="Настроить начисление Coins за активность",
    )
    @app_commands.describe(
        message_reward="Coins за одно сообщение",
        voice_reward="Coins за минуту в голосовом",
        message_cooldown="Минимальный интервал между оплачиваемыми сообщениями",
    )
    @administrator_or_owner()
    async def coins_economy_config(
        self,
        interaction: discord.Interaction,
        message_reward: app_commands.Range[float, 0, 1000] | None = None,
        voice_reward: app_commands.Range[float, 0, 1000] | None = None,
        message_cooldown: app_commands.Range[int, 0, 3600] | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        current = await self._economy_settings(interaction.guild_id)
        updated = EconomySettings(
            message_reward_milli=(
                current.message_reward_milli
                if message_reward is None
                else round(float(message_reward) * 1000)
            ),
            voice_reward_milli=(
                current.voice_reward_milli
                if voice_reward is None
                else round(float(voice_reward) * 1000)
            ),
            message_cooldown_seconds=(
                current.message_cooldown_seconds
                if message_cooldown is None
                else int(message_cooldown)
            ),
        )
        if updated != current:
            await set_economy_settings(interaction.guild_id, updated)
            self._economy_settings_cache.pop(interaction.guild_id, None)
        embed = apply_accent(
            notice(
                code="STATS.ECONOMY_CONFIG",
                title=(
                    "🪙 Настройки экономики обновлены"
                    if updated != current
                    else "🪙 Настройки экономики"
                ),
                summary=(
                    f"За сообщение: **{updated.message_reward_milli / 1000:g} Coins**\n"
                    f"За минуту в голосовом: **{updated.voice_reward_milli / 1000:g} Coins**\n"
                    f"Интервал сообщений: **{updated.message_cooldown_seconds} сек.**"
                ),
                next_step=(
                    "Новые ставки применяются к следующей активности."
                    if updated != current
                    else "Ставки не изменились."
                ),
            ),
            Accent.TREASURY,
        )
        await followup_v2(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WeeklyStatsCog(bot))
