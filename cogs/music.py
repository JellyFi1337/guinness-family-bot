from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
import time
from collections import deque
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, cast
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    FFMPEG_EXECUTABLE,
    MUSIC_ALLOWED_HOSTS,
    MUSIC_CANCEL_WINDOW_SECONDS,
    MUSIC_EXTRACT_TIMEOUT_SECONDS,
    MUSIC_IDLE_TIMEOUT_SECONDS,
    MUSIC_LONG_TRACK_SECONDS,
    MUSIC_MAX_QUEUE_SIZE,
    MUSIC_MAX_CONCURRENT_EXTRACTS,
    MUSIC_MAX_TRACK_SECONDS,
    MUSIC_MAX_USER_QUEUE,
    MUSIC_PLAY_COOLDOWN_SECONDS,
    MUSIC_PRICE_LONG,
    MUSIC_PRICE_SHORT,
    MUSIC_PUBLIC_SKIP_REFUND_SECONDS,
    MUSIC_SKIP_MIN_VOTES,
    MUSIC_SKIP_VOTE_RATIO,
    MUSIC_VOLUME_PERCENT,
    MUSIC_VOLUME_STEP_PERCENT,
)
from repositories.music import (
    MusicSession,
    MusicTrack,
    cancel_music_track,
    complete_music_track,
    create_paid_music_track,
    delete_music_session,
    get_music_statistics,
    get_music_session,
    list_music_sessions,
    mark_music_track_playing,
    refund_interrupted_music_tracks,
    refund_music_track,
    set_music_track_volume,
    upsert_music_session,
)
from ui.messages import (
    edit_message_v2,
    followup_v2,
    respond_v2,
    send_v2,
)
from services.metrics import metrics
from ui.command_registry import code
from ui.design import Accent
from ui.templates import notice as notice_card
from utils.access import has_staff_access
from utils.embeds import MessageSpec, apply_accent
from utils.views import SafeView


logger = logging.getLogger(__name__)
_EXTRACT_EXECUTOR = ThreadPoolExecutor(
    max_workers=MUSIC_MAX_CONCURRENT_EXTRACTS,
    thread_name_prefix="music-extract",
)
# Admission control. The executor has a fixed number of workers but accepted an
# unbounded number of submissions, so a burst queued dozens of jobs behind four
# threads and every user waited for all of them. Callers that cannot get a slot
# immediately are told the search is busy instead of queueing invisibly.
_EXTRACT_ADMISSION = asyncio.Semaphore(MUSIC_MAX_CONCURRENT_EXTRACTS * 2)


# Latched after the first failure to spawn yt-dlp out of process: without it
# every search would pay a doomed process spawn.
_SUBPROCESS_BACKEND_OK = True


class ExtractionBusy(RuntimeError):
    """No extraction slot was free; the caller should retry later."""


class ExtractionUnavailable(RuntimeError):
    """The source is failing repeatedly and is temporarily not attempted."""


class _CircuitBreaker:
    """Stop hammering a source that is failing for everyone.

    Without this, a YouTube outage turns every /music play into a full timeout,
    so four extraction workers stay saturated by requests that cannot succeed
    and healthy sources queue behind them.

    ``clock`` is injectable so tests can advance time without real sleep and
    without depending on process-global ``time`` patches from other suites.
    """

    __slots__ = ("_failures", "_opened_at", "_clock", "threshold", "cooldown")

    def __init__(
        self,
        *,
        threshold: int = 5,
        cooldown: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = time.monotonic if clock is None else clock
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def _now(self) -> float:
        return self._clock()

    def is_open(self, host: str) -> bool:
        opened = self._opened_at.get(host)
        if opened is None:
            return False
        if self._now() - opened >= self.cooldown:
            # Half-open: let one request through to probe the source.
            self._opened_at.pop(host, None)
            self._failures[host] = self.threshold - 1
            return False
        return True

    def record_success(self, host: str) -> None:
        self._failures.pop(host, None)
        self._opened_at.pop(host, None)

    def record_failure(self, host: str) -> None:
        count = self._failures.get(host, 0) + 1
        self._failures[host] = count
        if count >= self.threshold:
            self._opened_at[host] = self._now()
            logger.warning(
                "Music extraction circuit opened for %s after %d failures",
                host,
                count,
            )


_EXTRACT_BREAKER = _CircuitBreaker()


def shutdown_extract_executor() -> None:
    """Release extraction threads on unload instead of leaking them."""

    _EXTRACT_EXECUTOR.shutdown(wait=False, cancel_futures=True)
_FFMPEG_BEFORE_OPTIONS = (
    "-nostdin "
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_on_network_error 1 "
    "-reconnect_on_http_error 4xx,5xx "
    "-reconnect_delay_max 5 "
    "-rw_timeout 15000000"
)


@dataclass(frozen=True, slots=True)
class ExtractedTrack:
    title: str
    webpage_url: str
    duration_seconds: int
    stream_url: str | None = None


@dataclass(slots=True)
class GuildMusicState:
    queue: deque[MusicTrack] = field(default_factory=deque)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    panel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    current: MusicTrack | None = None
    current_is_replay: bool = False
    started_monotonic: float | None = None
    skip_votes: set[int] = field(default_factory=set)
    stop_mode: str | None = None
    current_source: discord.PCMVolumeTransformer | None = None
    current_volume_percent: int = MUSIC_VOLUME_PERCENT
    control_message: discord.Message | None = None
    control_channel_id: int | None = None
    persisted_session: tuple[int, int, str] | None = None
    repeat_mode: str = "off"
    paused: bool = False
    paused_since: float | None = None
    idle_disconnected: bool = False
    played_track_ids: set[int] = field(default_factory=set)
    track_volumes: dict[int, int] = field(default_factory=dict)
    notice: str | None = None
    shutdown_reason: str = "player_stopped"
    pending_reservations: dict[int, int] = field(default_factory=dict)


def _price(value: int) -> str:
    return f"{value:,} Guinn Coins".replace(",", " ")


def _duration(seconds: int) -> str:
    minutes, second = divmod(max(0, seconds), 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02d}:{second:02d}" if hours else f"{minute}:{second:02d}"


def _track_link(track: MusicTrack) -> str:
    title = discord.utils.escape_markdown(track.title)
    return f"[{title}]({track.webpage_url})"


def _allowed_url(raw: str) -> bool:
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in MUSIC_ALLOWED_HOSTS)


_YTDLP_FORMAT = "bestaudio/best"


def _extraction_target(query: str) -> str:
    """Validate the query and turn it into a yt-dlp target.

    Shared by both extraction backends so the allow-list and the scheme checks
    cannot drift apart between them.
    """

    normalized = query.strip()
    lowered = normalized.casefold()
    if lowered.startswith(("http://", "https://")):
        if not _allowed_url(query):
            raise ValueError("Этот домен не разрешён настройками музыкального бота.")
        return query
    if "://" in normalized or lowered.startswith(("file:", "data:")):
        raise ValueError("Поддерживаются только безопасные HTTP/HTTPS-ссылки.")
    return f"ytsearch1:{query}"


def _stream_url_from_info(info: dict) -> str | None:
    """Find the playable URL wherever this yt-dlp version put it.

    ``extract_info`` exposes it at the top level; ``--dump-single-json`` reports
    the selected format under ``requested_downloads``. Falling back to the
    format list keeps an unfamiliar extractor from becoming a hard failure.
    """

    direct = info.get("url")
    if direct:
        return str(direct)
    for download in info.get("requested_downloads") or ():
        if isinstance(download, dict) and download.get("url"):
            return str(download["url"])
    formats = info.get("formats") or ()
    audio = [
        item
        for item in formats
        if isinstance(item, dict)
        and item.get("url")
        and item.get("acodec") not in (None, "none")
    ]
    if audio:
        return str(audio[-1]["url"])
    return None


def _track_from_info(info: dict | None, *, stream: bool) -> ExtractedTrack:
    """Turn a yt-dlp info dict into a track, or explain why it is unusable."""

    if not info:
        raise ValueError("Трек не найден.")
    entries = info.get("entries")
    if entries is not None:
        info = next((entry for entry in entries if entry), None)
    if not info:
        raise ValueError("Трек не найден.")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise ValueError("Прямые трансляции нельзя добавлять в платную очередь.")
    duration = int(info.get("duration") or 0)
    if duration < 1:
        raise ValueError("Не удалось определить длительность трека.")
    webpage_url = str(info.get("webpage_url") or info.get("original_url") or "")
    if not webpage_url or not _allowed_url(webpage_url):
        raise ValueError("Источник трека не входит в список разрешённых доменов.")
    stream_url = _stream_url_from_info(info) if stream else None
    return ExtractedTrack(
        title=str(info.get("title") or "Без названия")[:200],
        webpage_url=webpage_url,
        duration_seconds=duration,
        stream_url=stream_url,
    )


def _extract_sync(query: str, *, stream: bool) -> ExtractedTrack:
    """In-process extraction. Correct, but not interruptible."""

    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise RuntimeError(
            "yt-dlp не установлен. Установите зависимости из requirements.lock."
        ) from exc

    target = _extraction_target(query)
    options = {
        "format": _YTDLP_FORMAT,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": MUSIC_EXTRACT_TIMEOUT_SECONDS,
        "retries": 2,
        "extract_flat": False,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(target, download=False)
    return _track_from_info(info, stream=stream)


class _SubprocessUnavailable(RuntimeError):
    """The out-of-process backend could not run; fall back in-process."""


async def _extract_subprocess(query: str, *, stream: bool) -> ExtractedTrack:
    """Extraction in a killable child process.

    ``asyncio.wait_for`` around a thread cancels the *wait*, not the work: a
    yt-dlp call stuck on a dead socket keeps one of four worker threads busy
    long after the user gave up, and enough of those starve the pool. A child
    process can actually be killed, so the timeout means what it says.

    Raises :class:`_SubprocessUnavailable` when the backend itself cannot be
    used, so the caller degrades to the in-process path instead of failing.
    """

    target = _extraction_target(query)
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--no-cache-dir",
        "--no-progress",
        "--format",
        _YTDLP_FORMAT,
        "--socket-timeout",
        str(MUSIC_EXTRACT_TIMEOUT_SECONDS),
        "--retries",
        "2",
        "--",
        target,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, NotImplementedError) as exc:
        raise _SubprocessUnavailable(str(exc)) from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=MUSIC_EXTRACT_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncio.TimeoutError):
        # This is the whole point of the child process.
        process.kill()
        with suppress(ProcessLookupError):
            await process.wait()
        raise
    except asyncio.CancelledError:
        process.kill()
        with suppress(ProcessLookupError):
            await process.wait()
        raise

    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[:300]
        if not stdout.strip():
            # yt-dlp exits non-zero for both "no such video" and "module not
            # found". Only the latter means the backend is unusable.
            if "No module named" in detail:
                raise _SubprocessUnavailable(detail)
            raise ValueError("Трек не найден.")
        logger.warning("yt-dlp exited with %s: %s", process.returncode, detail)

    try:
        info = json.loads(stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise _SubprocessUnavailable(f"unparseable yt-dlp output: {exc}") from exc
    if not isinstance(info, dict):
        raise _SubprocessUnavailable("yt-dlp returned a non-object payload")
    return _track_from_info(info, stream=stream)


async def _run_extraction(query: str, *, stream: bool) -> ExtractedTrack:
    """Prefer the killable backend; fall back only when it cannot run."""

    global _SUBPROCESS_BACKEND_OK
    if _SUBPROCESS_BACKEND_OK:
        try:
            return await _extract_subprocess(query, stream=stream)
        except _SubprocessUnavailable as exc:
            # Latch the decision: probing a missing backend on every request
            # would add a process spawn to every search.
            _SUBPROCESS_BACKEND_OK = False
            logger.warning(
                "Out-of-process yt-dlp unavailable, falling back in-process: %s", exc
            )
            metrics.increment("music.extract_subprocess_unavailable")
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _EXTRACT_EXECUTOR,
            lambda: _extract_sync(query, stream=stream),
        ),
        timeout=MUSIC_EXTRACT_TIMEOUT_SECONDS,
    )


def _query_host(query: str) -> str:
    normalized = query.strip().casefold()
    if not normalized.startswith(("http://", "https://")):
        return "search"
    try:
        return urlparse(normalized).hostname or "unknown"
    except ValueError:
        return "unknown"


async def _extract(query: str, *, stream: bool = False) -> ExtractedTrack:
    """Resolve a track, bounded by the configured timeout.

    The timeout used to be ``MUSIC_EXTRACT_TIMEOUT_SECONDS * 3 + 5`` — 65
    seconds where the operator had configured 20 — so no setting described the
    real wait. It is now exactly what the configuration says.

    Extraction runs in a killable child process, so a timeout actually stops
    the work rather than only abandoning the wait. Admission control bounds
    concurrency, and the circuit breaker stops feeding a failing source.
    """

    host = _query_host(query)
    if _EXTRACT_BREAKER.is_open(host):
        metrics.increment("music.extract_circuit_open")
        raise ExtractionUnavailable(host)
    if _EXTRACT_ADMISSION.locked():
        metrics.increment("music.extract_rejected")
        raise ExtractionBusy()
    async with _EXTRACT_ADMISSION:
        started = time.monotonic()
        try:
            result = await _run_extraction(query, stream=stream)
        except (TimeoutError, asyncio.TimeoutError):
            _EXTRACT_BREAKER.record_failure(host)
            metrics.increment("music.extract_timeout")
            raise
        except ValueError:
            # A rejected URL or a missing track is a user-input problem, not a
            # sick source: it must not count towards opening the circuit.
            raise
        except Exception:
            _EXTRACT_BREAKER.record_failure(host)
            metrics.increment("music.extract_error")
            raise
        _EXTRACT_BREAKER.record_success(host)
        metrics.observe("music.extract_ms", (time.monotonic() - started) * 1_000)
        return result


def _track_embed(
    title: str,
    track: MusicTrack,
    *,
    extra: str | None = None,
    volume_percent: int | None = None,
) -> MessageSpec:
    details: list[tuple[str, str]] = [
        ("Длительность", _duration(track.duration_seconds)),
        ("Стоимость", _price(track.price)),
        ("Громкость", f"{volume_percent or track.volume_percent}%"),
        ("Заказал", f"<@{track.requester_id}>"),
    ]
    if extra:
        details.append(("Состояние", extra))
    return apply_accent(
        notice_card(
            code="MUSIC.TRACK",
            title=title,
            summary=_track_link(track),
            details=tuple(details),
            next_step=f"Музыкальная очередь • трек #{track.id}",
            reference=f"#{track.id}",
        ),
        Accent.SERVICE,
    )


def _progress_bar(elapsed_seconds: int, duration_seconds: int) -> str:
    width = 14
    ratio = min(1.0, max(0.0, elapsed_seconds / max(1, duration_seconds)))
    filled = min(width, max(0, int(ratio * width)))
    return f"`{'━' * filled}{'─' * (width - filled)}`"


def _repeat_label(repeat_mode: str) -> str:
    return {
        "track": "Повтор текущего трека",
        "queue": "Цикл всей очереди",
    }.get(repeat_mode, "Без повтора")


def _schedule_repeat(
    state: GuildMusicState,
    track: MusicTrack,
) -> None:
    if state.repeat_mode == "track":
        state.played_track_ids.add(track.id)
        state.queue.appendleft(track)
    elif state.repeat_mode == "queue":
        state.played_track_ids.add(track.id)
        state.queue.append(track)


def _discard_scheduled_replays(state: GuildMusicState) -> None:
    replay_ids = set(state.played_track_ids)
    state.queue = deque(track for track in state.queue if track.id not in replay_ids)
    for track_id in replay_ids:
        state.track_volumes.pop(track_id, None)


def _has_pending_music_work(state: GuildMusicState) -> bool:
    return bool(state.queue) or state.wake.is_set()


def _player_embed(
    *,
    track: MusicTrack | None,
    queued: list[MusicTrack],
    elapsed_seconds: int,
    volume_percent: int,
    repeat_mode: str,
    paused: bool,
    voice_channel_id: int | None,
    notice: str | None = None,
    idle_disconnected: bool = False,
) -> MessageSpec:
    if track is None:
        details: list[tuple[str, str]] = [
            (
                "Сессия",
                (
                    f"Канал: <#{voice_channel_id}>\nРежим: **{_repeat_label(repeat_mode)}**"
                    if voice_channel_id is not None
                    else f"Режим: **{_repeat_label(repeat_mode)}**"
                ),
            ),
        ]
        if queued:
            details.append(
                (
                    f"В очереди · {len(queued)}",
                    "\n".join(
                        f"**{index}.** {_track_link(item)}"
                        for index, item in enumerate(queued[:3], 1)
                    ),
                )
            )
        if notice:
            details.append(("Последнее событие", notice))
        return apply_accent(
            notice_card(
                code="MUSIC.IDLE",
                title="Музыкальный плеер",
                summary=(
                    f"Подготавливается следующий трек: {_track_link(queued[0])}."
                    if queued
                    else (
                        (
                            "Очередь закончилась, бот отключился от голосового "
                            "канала и ждёт новый заказ через `/music play`."
                        )
                        if idle_disconnected
                        else (
                            "Очередь закончилась. Плеер ожидает новый заказ "
                            "через `/music play`."
                        )
                    )
                ),
                details=tuple(details),
                next_step=(
                    "Панель сохранена • удалить её можно кнопкой «Отключить» "
                    "или командой /music disconnect"
                ),
            ),
            Accent.SERVICE,
        )

    status = "Пауза" if paused else "Воспроизведение"
    session_lines = [
        f"Громкость: **{volume_percent}%**",
        f"Режим: **{_repeat_label(repeat_mode)}**",
    ]
    if voice_channel_id is not None:
        session_lines.insert(0, f"Канал: <#{voice_channel_id}>")
    upcoming = "\n".join(
        f"**{index}.** {_track_link(item)} · {_duration(item.duration_seconds)}"
        for index, item in enumerate(queued[:3], 1)
    )
    if len(queued) > 3:
        upcoming += f"\n*И ещё {len(queued) - 3} в очереди*"
    details = [
        (
            "Прогресс",
            (
                f"{_progress_bar(elapsed_seconds, track.duration_seconds)}\n"
                f"`{_duration(elapsed_seconds)} / {_duration(track.duration_seconds)}`"
            ),
        ),
        ("Сессия", "\n".join(session_lines)),
        (
            "Заказ",
            (
                f"Длительность: **{_duration(track.duration_seconds)}**\n"
                f"Стоимость: **{_price(track.price)}**\n"
                f"Номер: **#{track.id}**"
            ),
        ),
        (f"Далее · {len(queued)}", upcoming or "Очередь пуста."),
    ]
    if notice:
        details.append(("Последнее событие", notice))
    return apply_accent(
        notice_card(
            code="MUSIC.NOW",
            title="Музыкальный плеер",
            summary=(
                f"### {_track_link(track)}\n"
                f"{'⏸️' if paused else '▶️'} **{status}** • заказал <@{track.requester_id}>"
            ),
            details=tuple(details),
            next_step=(
                "Управление доступно кнопками ниже • режимы повтора не списывают "
                "монеты повторно"
            ),
            reference=f"#{track.id}",
        ),
        Accent.WARNING if paused else Accent.SERVICE,
    )


class MusicControlsView(SafeView):
    def __init__(
        self,
        cog: MusicCog,
        *,
        repeat_mode: str = "off",
        paused: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.repeat_track.style = (
            discord.ButtonStyle.success if repeat_mode == "track" else discord.ButtonStyle.secondary
        )
        self.loop_queue.style = (
            discord.ButtonStyle.success if repeat_mode == "queue" else discord.ButtonStyle.secondary
        )
        self.pause_resume.label = "Продолжить" if paused else "Пауза"
        self.pause_resume.emoji = "▶️" if paused else "⏸️"

    @discord.ui.button(
        label="Пропустить",
        style=discord.ButtonStyle.secondary,
        custom_id="music:skip",
        emoji="⏭️",
        row=0,
    )
    async def skip(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.handle_skip(interaction)

    @discord.ui.button(
        label="Отменить свою",
        style=discord.ButtonStyle.secondary,
        custom_id="music:cancel",
        emoji="✖️",
        row=0,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.handle_cancel(interaction)

    @discord.ui.button(
        label="Очередь",
        style=discord.ButtonStyle.primary,
        custom_id="music:queue",
        emoji="📋",
        row=0,
    )
    async def queue(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.show_queue(interaction)

    @discord.ui.button(
        label="Пауза",
        style=discord.ButtonStyle.secondary,
        custom_id="music:pause_resume",
        emoji="⏸️",
        row=0,
    )
    async def pause_resume(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.handle_pause_resume(interaction)

    @discord.ui.button(
        label="Повтор трека",
        style=discord.ButtonStyle.secondary,
        custom_id="music:repeat_track",
        emoji="🔂",
        row=1,
    )
    async def repeat_track(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.handle_repeat_mode(interaction, requested_mode="track")

    @discord.ui.button(
        label="Цикл очереди",
        style=discord.ButtonStyle.secondary,
        custom_id="music:loop_queue",
        emoji="🔁",
        row=1,
    )
    async def loop_queue(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.handle_repeat_mode(interaction, requested_mode="queue")

    @discord.ui.button(
        label="Тише",
        style=discord.ButtonStyle.secondary,
        custom_id="music:volume_down",
        emoji="🔉",
        row=1,
    )
    async def volume_down(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.handle_volume(
            interaction,
            delta=-MUSIC_VOLUME_STEP_PERCENT,
        )

    @discord.ui.button(
        label="Громче",
        style=discord.ButtonStyle.secondary,
        custom_id="music:volume_up",
        emoji="🔊",
        row=1,
    )
    async def volume_up(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.handle_volume(
            interaction,
            delta=MUSIC_VOLUME_STEP_PERCENT,
        )

    @discord.ui.button(
        label="Отключить",
        style=discord.ButtonStyle.danger,
        custom_id="music:disconnect",
        emoji="⏹️",
        row=1,
    )
    async def disconnect(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.handle_disconnect(interaction)


class MusicCog(
    commands.GroupCog,
    group_name="music",
    group_description="Плеер, очередь и управление музыкальной сессией",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._states: dict[int, GuildMusicState] = {}
        self._recovery_lock = asyncio.Lock()
        self._recovered = False
        self._persistent_controls: MusicControlsView | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and isinstance(
            interaction.channel, discord.VoiceChannel
        ):
            return True
        await respond_v2(
            interaction,
            "Команды `/music` доступны только в текстовом чате голосового канала.",
            ephemeral=True,
        )
        return False

    def _state(self, guild_id: int) -> GuildMusicState:
        return self._states.setdefault(guild_id, GuildMusicState())

    async def cog_load(self) -> None:
        self._persistent_controls = MusicControlsView(self)
        self.bot.add_view(self._persistent_controls)

    async def cog_unload(self) -> None:
        if self._persistent_controls is not None:
            self._persistent_controls.stop()
            self._persistent_controls = None
        tasks = [
            state.task
            for state in self._states.values()
            if state.task is not None and not state.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # The executor is a module-level singleton; without this a reload leaves
        # its worker threads (and any hung yt-dlp call) running forever.
        shutdown_extract_executor()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with self._recovery_lock:
            if self._recovered:
                return
            refunded = await refund_interrupted_music_tracks()
            # Paid orders deliberately use refund-on-restart semantics. A
            # persisted panel therefore represents a dead session, not a queue
            # that can be resumed safely with a potentially expired stream URL.
            stale_sessions = await list_music_sessions()
            removed_sessions = 0
            for session in stale_sessions:
                if await self._delete_stale_session(session):
                    removed_sessions += 1
            self._recovered = True
            if refunded:
                logger.warning(
                    "Refunded %d interrupted paid music track(s)",
                    refunded,
                )
            if removed_sessions:
                logger.warning(
                    "Removed %d stale music player session(s)",
                    removed_sessions,
                )

    async def _resolve_text_channel(
        self,
        guild: discord.Guild | None,
        channel_id: int,
    ) -> Any | None:
        channel: Any | None = None
        if guild is not None:
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return channel

    async def _message_from_session(
        self,
        guild: discord.Guild | None,
        session: MusicSession,
    ) -> discord.Message | None:
        if session.message_id is None:
            return None
        channel: Any | None = None
        if guild is not None:
            channel = guild.get_channel(session.text_channel_id) or guild.get_thread(
                session.text_channel_id
            )
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(session.text_channel_id)
            except discord.NotFound:
                return None
        if not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(session.message_id)
        except discord.NotFound:
            return None

    async def _delete_discord_message(self, message: discord.Message) -> bool:
        try:
            await message.delete()
            return True
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Could not delete music panel message=%s",
                message.id,
            )
            return False

    async def _delete_session_message(
        self,
        guild: discord.Guild | None,
        session: MusicSession,
    ) -> bool:
        if session.message_id is None:
            return True
        channel: Any | None = None
        if guild is not None:
            channel = guild.get_channel(session.text_channel_id) or guild.get_thread(
                session.text_channel_id
            )
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(session.text_channel_id)
            except discord.NotFound:
                return True
            except (discord.Forbidden, discord.HTTPException):
                return False
        if not hasattr(channel, "fetch_message"):
            return True
        try:
            message = await channel.fetch_message(session.message_id)
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False
        return await self._delete_discord_message(message)

    async def _delete_stale_session(self, session: MusicSession) -> bool:
        guild = self.bot.get_guild(session.guild_id)
        if not await self._delete_session_message(guild, session):
            return False
        await delete_music_session(session.guild_id)
        return True

    async def _refresh_player(
        self,
        guild: discord.Guild,
        state: GuildMusicState,
    ) -> discord.Message | None:
        async with state.panel_lock:
            voice_client = cast(discord.VoiceClient | None, guild.voice_client)
            async with state.lock:
                track = state.current
                queued = list(state.queue)
                elapsed = int(self._elapsed(state))
                volume_percent = state.current_volume_percent
                repeat_mode = state.repeat_mode
                paused = state.paused
                idle_disconnected = state.idle_disconnected
                notice = state.notice
                channel_id = state.control_channel_id or (
                    track.text_channel_id if track is not None else None
                )
                voice_channel_id = (
                    track.voice_channel_id
                    if track is not None
                    else (
                        getattr(voice_client.channel, "id", None)
                        if voice_client is not None and voice_client.channel is not None
                        else None
                    )
                )
                message = state.control_message
            if channel_id is None:
                return None

            if message is None:
                session = await get_music_session(guild.id)
                if session is not None:
                    try:
                        message = await self._message_from_session(guild, session)
                    except (discord.Forbidden, discord.HTTPException):
                        logger.warning(
                            "Could not fetch canonical music panel guild=%s message=%s",
                            guild.id,
                            session.message_id,
                        )
                        return None
                    if message is not None:
                        channel_id = session.text_channel_id

            embed = _player_embed(
                track=track,
                queued=queued,
                elapsed_seconds=elapsed,
                volume_percent=volume_percent,
                repeat_mode=repeat_mode,
                paused=paused,
                voice_channel_id=voice_channel_id,
                notice=notice,
                idle_disconnected=idle_disconnected,
            )
            view = MusicControlsView(
                self,
                repeat_mode=repeat_mode,
                paused=paused,
            )
            if message is not None:
                try:
                    message = await edit_message_v2(
                        message,
                        embed=embed,
                        view=view,
                    )
                except (discord.NotFound, discord.Forbidden):
                    message = None
                except discord.HTTPException:
                    logger.exception(
                        "Could not edit music player guild=%s message=%s",
                        guild.id,
                        message.id,
                    )

            if message is None:
                channel = await self._resolve_text_channel(guild, channel_id)
                if channel is None or not hasattr(channel, "send"):
                    return None
                try:
                    message = await send_v2(
                        channel,
                        embed=embed,
                        view=view,
                        component_marker=f"music-player:{guild.id}",
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Could not create music player guild=%s channel=%s",
                        guild.id,
                        channel_id,
                    )
                    return None

            async with state.lock:
                state.control_message = message
                state.control_channel_id = channel_id
            session_signature = (channel_id, message.id, repeat_mode)
            async with state.lock:
                needs_persist = state.persisted_session != session_signature
            if needs_persist:
                await upsert_music_session(
                    guild_id=guild.id,
                    text_channel_id=channel_id,
                    message_id=message.id,
                    repeat_mode=repeat_mode,
                )
            async with state.lock:
                state.control_message = message
                state.control_channel_id = channel_id
                state.persisted_session = session_signature
            return message

    async def _delete_player(
        self,
        guild: discord.Guild,
        state: GuildMusicState,
    ) -> bool:
        async with state.panel_lock:
            async with state.lock:
                message = state.control_message
            session = await get_music_session(guild.id)
            deleted = True
            if message is not None:
                deleted = await self._delete_discord_message(message)
            if (
                session is not None
                and session.message_id is not None
                and (message is None or message.id != session.message_id)
            ):
                deleted = await self._delete_session_message(guild, session) and deleted
            if not deleted:
                return False
            await delete_music_session(guild.id)
            async with state.lock:
                state.control_message = None
                state.control_channel_id = None
                state.persisted_session = None
            return True

    async def _bind_canonical_control(
        self,
        interaction: discord.Interaction,
        state: GuildMusicState,
    ) -> bool:
        message = interaction.message
        if message is None:
            return True
        guild_id = interaction.guild_id
        if guild_id is None:
            await respond_v2(
                interaction,
                "Эта панель недоступна вне сервера.",
                ephemeral=True,
            )
            return False

        async with state.lock:
            signature = state.persisted_session
            state_message_id = (
                state.control_message.id if state.control_message is not None else None
            )
            task_active = state.task is not None and not state.task.done()
        canonical_id = signature[1] if signature is not None else None
        if canonical_id is None and task_active:
            canonical_id = state_message_id
        if canonical_id is None:
            session = await get_music_session(guild_id)
            canonical_id = session.message_id if session is not None else None

        if canonical_id != message.id:
            await respond_v2(
                interaction,
                "Эта панель устарела. Используйте актуальное сообщение музыкального плеера.",
                ephemeral=True,
            )
            await self._delete_discord_message(message)
            return False

        async with state.lock:
            state.control_message = message
            state.control_channel_id = (
                getattr(interaction, "channel_id", None)
                or state.control_channel_id
                or getattr(getattr(message, "channel", None), "id", None)
            )
        return True

    async def _refund_failed_track(
        self,
        state: GuildMusicState,
        track: MusicTrack,
        reason: str,
        message: str,
        *,
        replay: bool,
    ) -> None:
        refunded = False if replay else await refund_music_track(track.id, reason)
        async with state.lock:
            state.notice = (
                f"{message} "
                + (
                    f"Заказчику возвращено **{_price(track.price)}**."
                    if refunded
                    else "Повтор остановлен без дополнительного списания."
                )
            )[:1024]

    async def _connect(
        self,
        guild: discord.Guild,
        channel_id: int,
    ) -> discord.VoiceClient:
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError("Голосовой канал заказа больше недоступен.")
        bot_member = guild.me
        if bot_member is None:
            raise RuntimeError("Не удалось определить участника бота.")
        permissions = channel.permissions_for(bot_member)
        if not permissions.connect or not permissions.speak:
            raise RuntimeError("Боту нужны права Connect и Speak в голосовом канале.")
        voice = guild.voice_client
        if voice is None:
            return cast(
                discord.VoiceClient,
                await channel.connect(timeout=20, reconnect=True),
            )
        voice = cast(discord.VoiceClient, voice)
        if getattr(voice.channel, "id", None) != channel.id:
            await voice.move_to(channel)
        return voice

    async def _next_track(
        self,
        guild: discord.Guild,
        state: GuildMusicState,
        track: MusicTrack,
        *,
        replay: bool,
    ) -> bool:
        try:
            extracted = await _extract(track.webpage_url, stream=True)
            if not extracted.stream_url:
                raise RuntimeError("Источник не вернул аудиопоток.")
            async with state.lock:
                if state.current is None or state.current.id != track.id:
                    return False
                if state.stop_mode is not None:
                    return False
                volume_percent = state.current_volume_percent
            voice = await self._connect(guild, track.voice_channel_id)
            if not replay and not await mark_music_track_playing(track.id):
                return False
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    extracted.stream_url,
                    executable=FFMPEG_EXECUTABLE,
                    before_options=_FFMPEG_BEFORE_OPTIONS,
                    options="-vn -loglevel warning",
                ),
                volume=volume_percent / 100,
            )
            loop = asyncio.get_running_loop()
            completed: asyncio.Future[Exception | None] = loop.create_future()

            def after(error: Exception | None) -> None:
                def finish() -> None:
                    if not completed.done():
                        completed.set_result(error)

                loop.call_soon_threadsafe(finish)

            async with state.lock:
                if state.stop_mode is not None:
                    source.cleanup()
                    return False
                state.started_monotonic = time.monotonic()
                state.current_source = source
                state.paused = False
                state.paused_since = None
            voice.play(source, after=after)
            await self._refresh_player(guild, state)
            while True:
                try:
                    playback_error = await asyncio.wait_for(
                        asyncio.shield(completed),
                        timeout=30,
                    )
                    break
                except TimeoutError:
                    async with state.lock:
                        paused = state.paused
                        timed_out = (
                            not paused and self._elapsed(state) > track.duration_seconds + 300
                        )
                    if timed_out:
                        voice.stop()
                        playback_error = RuntimeError("Превышено время воспроизведения")
                        break
                    if not paused:
                        await self._refresh_player(guild, state)

            async with state.lock:
                stop_mode = state.stop_mode
                state.current_source = None
                state.paused = False
                state.paused_since = None
            if stop_mode is None:
                if playback_error is None:
                    if not replay:
                        await complete_music_track(track.id)
                    return True
                else:
                    logger.warning(
                        "Playback failed for music track %s: %s",
                        track.id,
                        playback_error,
                    )
                    await self._refund_failed_track(
                        state,
                        track,
                        "playback_failed",
                        "Не удалось завершить воспроизведение.",
                        replay=replay,
                    )
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Music track %s failed", track.id)
            # The exception text may contain URLs, file paths or yt-dlp
            # internals. The player message is public, so it carries a stable
            # code and the detail stays in the log.
            del exc
            await self._refund_failed_track(
                state,
                track,
                "playback_setup_failed",
                "Не удалось начать воспроизведение. `MUSIC.PLAYBACK_FAILED`",
                replay=replay,
            )
            return False

    async def _player_loop(
        self,
        guild: discord.Guild,
        state: GuildMusicState,
    ) -> None:
        try:
            while not self.bot.is_closed():
                track = None
                replay = False
                refresh_idle = False
                async with state.lock:
                    if state.queue:
                        track = state.queue.popleft()
                        replay = track.id in state.played_track_ids
                        state.current = track
                        state.current_is_replay = replay
                        state.started_monotonic = None
                        state.skip_votes.clear()
                        state.stop_mode = None
                        state.current_source = None
                        state.paused = False
                        state.paused_since = None
                        state.idle_disconnected = False
                        state.current_volume_percent = state.track_volumes.setdefault(
                            track.id,
                            track.volume_percent,
                        )
                    else:
                        state.wake.clear()
                        refresh_idle = not state.idle_disconnected
                if track is None:
                    if refresh_idle:
                        await self._refresh_player(guild, state)
                    try:
                        await asyncio.wait_for(
                            state.wake.wait(),
                            timeout=MUSIC_IDLE_TIMEOUT_SECONDS,
                        )
                        continue
                    except TimeoutError:
                        async with state.lock:
                            if _has_pending_music_work(state):
                                continue
                            should_refresh = not state.idle_disconnected
                            state.idle_disconnected = True
                            state.notice = (
                                "Бот отключён от голосового канала из-за "
                                "бездействия. Панель и очередь сохранены."
                            )
                        if should_refresh:
                            voice = cast(
                                discord.VoiceClient | None,
                                guild.voice_client,
                            )
                            if voice is not None and voice.is_connected():
                                try:
                                    await voice.disconnect(force=True)
                                except discord.HTTPException:
                                    logger.warning("Could not disconnect idle music voice client")
                                    async with state.lock:
                                        state.idle_disconnected = False
                                        state.notice = (
                                            "Не удалось отключиться от голосового "
                                            "канала. Бот повторит попытку."
                                        )
                            await self._refresh_player(guild, state)
                        continue

                completed = await self._next_track(
                    guild,
                    state,
                    track,
                    replay=replay,
                )
                async with state.lock:
                    if completed:
                        _schedule_repeat(state, track)
                    if track.id not in state.played_track_ids:
                        state.track_volumes.pop(track.id, None)
                    if state.current is not None and state.current.id == track.id:
                        state.current = None
                    state.current_is_replay = False
                    state.started_monotonic = None
                    state.skip_votes.clear()
                    state.stop_mode = None
                    state.current_source = None
                    state.paused = False
                    state.paused_since = None
        finally:
            current = None
            queued: list[MusicTrack] = []
            async with state.lock:
                current = state.current
                queued = list(state.queue)
                reason = state.shutdown_reason
                state.queue.clear()
                state.current = None
                state.current_is_replay = False
                state.task = None
                state.current_source = None
                state.paused = False
                state.paused_since = None
                state.idle_disconnected = False
            if current is not None:
                await refund_music_track(current.id, reason)
            for track in queued:
                await refund_music_track(track.id, reason)
            voice = cast(discord.VoiceClient | None, guild.voice_client)
            if voice is not None and voice.is_connected():
                try:
                    await voice.disconnect(force=True)
                except discord.HTTPException:
                    logger.warning("Could not disconnect idle music voice client")
            await self._delete_player(guild, state)
            async with state.lock:
                state.repeat_mode = "off"
                state.played_track_ids.clear()
                state.track_volumes.clear()
                state.notice = None
                state.shutdown_reason = "player_stopped"

    def _ensure_player(
        self,
        guild: discord.Guild,
        state: GuildMusicState,
    ) -> None:
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(
                self._player_loop(guild, state),
                name=f"music-player:{guild.id}",
            )

    async def shutdown_guild(
        self,
        guild: discord.Guild,
        *,
        reason: str,
    ) -> tuple[int, bool]:
        """Stop one guild session before disconnects, resets, or maintenance."""

        state = self._state(guild.id)
        async with state.lock:
            refundable_ids = {
                track.id for track in state.queue if track.id not in state.played_track_ids
            }
            if state.current is not None and not state.current_is_replay:
                refundable_ids.add(state.current.id)
            state.shutdown_reason = reason[:100]
            task = state.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            async with state.lock:
                current = state.current
                queued = list(state.queue)
                state.current = None
                state.current_is_replay = False
                state.queue.clear()
            if current is not None:
                await refund_music_track(current.id, reason)
            for track in queued:
                await refund_music_track(track.id, reason)
            voice = cast(discord.VoiceClient | None, guild.voice_client)
            if voice is not None and voice.is_connected():
                try:
                    await voice.disconnect(force=True)
                except discord.HTTPException:
                    logger.warning(
                        "Could not disconnect music voice client guild=%s",
                        guild.id,
                    )
            await self._delete_player(guild, state)
            async with state.lock:
                state.repeat_mode = "off"
                state.played_track_ids.clear()
                state.track_volumes.clear()
                state.notice = None
                state.idle_disconnected = False
                state.shutdown_reason = "player_stopped"
        async with state.lock:
            panel_deleted = state.control_message is None
        return len(refundable_ids), panel_deleted

    async def show_queue(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond_v2(interaction, "Команда доступна на сервере.", ephemeral=True)
            return
        state = self._state(interaction.guild.id)
        if not await self._bind_canonical_control(interaction, state):
            return
        async with state.lock:
            current = state.current
            queued = list(state.queue)
            repeat_mode = state.repeat_mode
        lines = []
        if current is not None:
            lines.append(f"**Сейчас:** {_track_link(current)} • <@{current.requester_id}>")
        lines.extend(
            f"**{index}.** {_track_link(track)} "
            f"• {_duration(track.duration_seconds)} • <@{track.requester_id}>"
            for index, track in enumerate(queued, 1)
        )
        embed = apply_accent(
            notice_card(
                code="MUSIC.QUEUE",
                title="Музыкальная очередь",
                summary="\n".join(lines) or "Очередь пуста.",
                next_step=(
                    f"Режим: {_repeat_label(repeat_mode)} • в ожидании: {len(queued)}"
                ),
            ),
            Accent.SERVICE,
        )
        if interaction.response.is_done():
            await followup_v2(interaction, embed=embed, ephemeral=True)
        else:
            await respond_v2(interaction, embed=embed, ephemeral=True)

    def _elapsed(self, state: GuildMusicState) -> float:
        reference = (
            state.paused_since
            if state.paused and state.paused_since is not None
            else time.monotonic()
        )
        return (
            max(0.0, reference - state.started_monotonic)
            if state.started_monotonic is not None
            else 0.0
        )

    async def handle_volume(
        self,
        interaction: discord.Interaction,
        *,
        target: int | None = None,
        delta: int = 0,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await respond_v2(
                interaction,
                "Управление громкостью доступно на сервере.",
                ephemeral=True,
            )
            return
        state = self._state(interaction.guild.id)
        if not await self._bind_canonical_control(interaction, state):
            return
        await interaction.response.defer(ephemeral=True)
        async with state.lock:
            track = state.current
            source = state.current_source
            if track is None or source is None:
                await followup_v2(
                    interaction,
                    "Сейчас ничего не воспроизводится.",
                    ephemeral=True,
                )
                return
            if interaction.user.id != track.requester_id and not has_staff_access(interaction.user):
                await followup_v2(
                    interaction,
                    "Громкость может менять автор трека или старший состав.",
                    ephemeral=True,
                )
                return
            value = (
                max(1, min(100, int(target)))
                if target is not None
                else max(
                    1,
                    min(100, state.current_volume_percent + int(delta)),
                )
            )
            source.volume = value / 100
            state.current_volume_percent = value
            state.track_volumes[track.id] = value
            replay = state.current_is_replay

        if not replay:
            await set_music_track_volume(track.id, value)
        await self._refresh_player(interaction.guild, state)
        await followup_v2(
            interaction,
            f"Громкость установлена на **{value}%**.",
            ephemeral=True,
        )

    async def handle_repeat_mode(
        self,
        interaction: discord.Interaction,
        *,
        requested_mode: str,
    ) -> None:
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
            or requested_mode not in {"off", "track", "queue"}
        ):
            await respond_v2(
                interaction,
                "Режим воспроизведения доступен только на сервере.",
                ephemeral=True,
            )
            return
        state = self._state(interaction.guild.id)
        if not await self._bind_canonical_control(interaction, state):
            return
        await interaction.response.defer(ephemeral=True)
        async with state.lock:
            track = state.current
            is_staff = has_staff_access(interaction.user)
            if track is None:
                await followup_v2(
                    interaction,
                    "Сейчас нет трека, для которого можно изменить режим.",
                    ephemeral=True,
                )
                return
            if state.repeat_mode == "queue" and not is_staff:
                await followup_v2(
                    interaction,
                    "Режим цикла очереди может изменить только старший состав.",
                    ephemeral=True,
                )
                return
            if requested_mode == "queue" and not is_staff:
                await followup_v2(
                    interaction,
                    "Цикл всей очереди может включить только старший состав.",
                    ephemeral=True,
                )
                return
            if (
                requested_mode in {"track", "off"}
                and interaction.user.id != track.requester_id
                and not is_staff
            ):
                await followup_v2(
                    interaction,
                    "Повтор трека может менять его заказчик или старший состав.",
                    ephemeral=True,
                )
                return
            new_mode = (
                "off"
                if requested_mode != "off" and state.repeat_mode == requested_mode
                else requested_mode
            )
            if state.repeat_mode == "queue" and new_mode != "queue":
                _discard_scheduled_replays(state)
            if new_mode != state.repeat_mode:
                state.played_track_ids.clear()
            state.repeat_mode = new_mode
            state.notice = f"Режим изменён: **{_repeat_label(new_mode)}**."

        await self._refresh_player(interaction.guild, state)
        await followup_v2(
            interaction,
            f"Активный режим: **{_repeat_label(new_mode)}**.",
            ephemeral=True,
        )

    async def _set_pause(
        self,
        interaction: discord.Interaction,
        *,
        target: bool | None,
    ) -> None:
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
            or not has_staff_access(interaction.user)
        ):
            await respond_v2(
                interaction,
                "Пауза доступна старшему составу.",
                ephemeral=True,
            )
            return
        state = self._state(interaction.guild.id)
        if not await self._bind_canonical_control(interaction, state):
            return
        await interaction.response.defer(ephemeral=True)
        voice = cast(discord.VoiceClient | None, interaction.guild.voice_client)
        async with state.lock:
            if state.current is None or voice is None:
                await followup_v2(
                    interaction,
                    "Сейчас ничего не воспроизводится.",
                    ephemeral=True,
                )
                return
            should_pause = not state.paused if target is None else target
            if should_pause:
                if not voice.is_playing():
                    await followup_v2(
                        interaction,
                        "Трек уже находится на паузе.",
                        ephemeral=True,
                    )
                    return
                voice.pause()
                state.paused = True
                state.paused_since = time.monotonic()
                state.notice = "Воспроизведение приостановлено старшим составом."
            else:
                if not voice.is_paused():
                    await followup_v2(
                        interaction,
                        "Трек не находится на паузе.",
                        ephemeral=True,
                    )
                    return
                voice.resume()
                if state.started_monotonic is not None and state.paused_since is not None:
                    state.started_monotonic += time.monotonic() - state.paused_since
                state.paused_since = None
                state.paused = False
                state.notice = "Воспроизведение продолжено."

        await self._refresh_player(interaction.guild, state)
        await followup_v2(
            interaction,
            "Воспроизведение приостановлено." if should_pause else "Воспроизведение продолжено.",
            ephemeral=True,
        )

    async def handle_pause_resume(self, interaction: discord.Interaction) -> None:
        await self._set_pause(interaction, target=None)

    async def handle_disconnect(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
            or not has_staff_access(interaction.user)
        ):
            await respond_v2(
                interaction,
                "Отключение плеера доступно старшему составу.",
                ephemeral=True,
            )
            return
        state = self._state(interaction.guild.id)
        if not await self._bind_canonical_control(interaction, state):
            return
        await interaction.response.defer(ephemeral=True)
        count, panel_deleted = await self.shutdown_guild(
            interaction.guild,
            reason=f"staff_music_stop:{interaction.user.id}",
        )
        await followup_v2(
            interaction,
            (
                (
                    "Плеер отключён, его сообщение удалено. "
                    if panel_deleted
                    else (
                        "Плеер отключён, но Discord не позволил удалить панель. "
                        "Удаление будет повторено при следующем запуске. "
                    )
                )
                + f"Заказов передано на возврат: **{count}**."
            ),
            ephemeral=True,
        )

    async def _stop_current(
        self,
        interaction: discord.Interaction,
        state: GuildMusicState,
        track: MusicTrack,
        *,
        refund: bool,
        reason: str,
        message: str,
    ) -> None:
        replay = state.current_is_replay
        changed = True
        if not replay:
            changed = (
                await refund_music_track(track.id, reason)
                if refund
                else await cancel_music_track(track.id, reason)
            )
        if not changed:
            await followup_v2(
                interaction,
                "Состояние трека уже изменилось. Обновите очередь.",
                ephemeral=True,
            )
            return
        actual_refund = refund and not replay
        state.stop_mode = "refunded" if actual_refund else "cancelled"
        if state.repeat_mode == "track":
            state.repeat_mode = "off"
        state.notice = message
        voice = cast(
            discord.VoiceClient | None,
            interaction.guild.voice_client if interaction.guild is not None else None,
        )
        if voice is not None and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        suffix = (
            f" Автору возвращено **{_price(track.price)}**."
            if actual_refund
            else (" Повтор остановлен без дополнительного списания." if replay else "")
        )
        await followup_v2(interaction, message + suffix, ephemeral=True)

    async def handle_skip(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await respond_v2(interaction, "Команда доступна на сервере.", ephemeral=True)
            return
        state = self._state(interaction.guild.id)
        if not await self._bind_canonical_control(interaction, state):
            return
        await interaction.response.defer(ephemeral=True)
        async with state.lock:
            track = state.current
            if track is None:
                await followup_v2(interaction, "Сейчас ничего не играет.", ephemeral=True)
                return
            voice_channel = interaction.guild.get_channel(track.voice_channel_id)
            if not isinstance(voice_channel, discord.VoiceChannel):
                await followup_v2(interaction, "Голосовой канал недоступен.", ephemeral=True)
                return
            if (
                interaction.user.voice is None
                or interaction.user.voice.channel is None
                or interaction.user.voice.channel.id != voice_channel.id
            ):
                await followup_v2(
                    interaction,
                    "Для голосования нужно находиться в канале с ботом.",
                    ephemeral=True,
                )
                return

            elapsed = self._elapsed(state)
            if interaction.user.id == track.requester_id:
                free = elapsed <= MUSIC_CANCEL_WINDOW_SECONDS
                await self._stop_current(
                    interaction,
                    state,
                    track,
                    refund=free,
                    reason="requester_cancel" if free else "requester_skip",
                    message=(
                        "Ошибочный трек отменён бесплатно."
                        if free
                        else "Ваш трек пропущен без возврата."
                    ),
                )
                return
            if has_staff_access(interaction.user):
                await self._stop_current(
                    interaction,
                    state,
                    track,
                    refund=True,
                    reason=f"staff_skip:{interaction.user.id}",
                    message="Трек пропущен старшим составом.",
                )
                return

            eligible = [
                member
                for member in voice_channel.members
                if not member.bot and member.id != track.requester_id
            ]
            if interaction.user.id not in {member.id for member in eligible}:
                await followup_v2(interaction, "Вы не участвуете в голосовании.", ephemeral=True)
                return
            state.skip_votes.add(interaction.user.id)
            required = min(
                len(eligible),
                max(
                    MUSIC_SKIP_MIN_VOTES,
                    math.ceil(len(eligible) * MUSIC_SKIP_VOTE_RATIO),
                ),
            )
            required = max(1, required)
            if len(state.skip_votes) < required:
                await followup_v2(
                    interaction,
                    f"Голос за skip принят: **{len(state.skip_votes)}/{required}**.",
                    ephemeral=True,
                )
                return
            refundable = (
                MUSIC_PUBLIC_SKIP_REFUND_SECONDS > 0 and elapsed <= MUSIC_PUBLIC_SKIP_REFUND_SECONDS
            )
            await self._stop_current(
                interaction,
                state,
                track,
                refund=refundable,
                reason=f"listener_vote_skip:{interaction.user.id}",
                message=(
                    "Голосование завершено, трек пропущен."
                    if refundable
                    else "Голосование завершено. Поздний skip выполнен без возврата."
                ),
            )

    async def handle_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond_v2(interaction, "Команда доступна на сервере.", ephemeral=True)
            return
        state = self._state(interaction.guild.id)
        if not await self._bind_canonical_control(interaction, state):
            return
        await interaction.response.defer(ephemeral=True)
        async with state.lock:
            current = state.current
            if current is not None and current.requester_id == interaction.user.id:
                if self._elapsed(state) > MUSIC_CANCEL_WINDOW_SECONDS:
                    await followup_v2(
                        interaction,
                        "Бесплатное окно отмены закончилось. Используйте "
                        f"{code('music skip')}.",
                        ephemeral=True,
                    )
                    return
                await self._stop_current(
                    interaction,
                    state,
                    current,
                    refund=True,
                    reason="requester_cancel",
                    message="Ошибочный трек отменён бесплатно.",
                )
                return

            queued = next(
                (track for track in state.queue if track.requester_id == interaction.user.id),
                None,
            )
            if queued is None:
                await followup_v2(
                    interaction,
                    "У вас нет трека, который можно отменить.",
                    ephemeral=True,
                )
                return
            if queued.id in state.played_track_ids:
                state.queue.remove(queued)
                state.played_track_ids.discard(queued.id)
                state.track_volumes.pop(queued.id, None)
                await followup_v2(
                    interaction,
                    "Повтор вашего трека удалён из очереди. Дополнительных списаний не было.",
                    ephemeral=True,
                )
                return
            if not await refund_music_track(queued.id, "requester_queue_cancel"):
                await followup_v2(
                    interaction,
                    "Песня уже начала обрабатываться.",
                    ephemeral=True,
                )
                return
            state.queue.remove(queued)
            state.track_volumes.pop(queued.id, None)
            await followup_v2(
                interaction,
                f"Песня удалена из очереди. Возвращено **{_price(queued.price)}**.",
                ephemeral=True,
            )

    @app_commands.command(name="play", description="Найти и добавить трек за Guinn Coins")
    @app_commands.describe(
        запрос="Название трека или разрешённая ссылка",
        громкость="Громкость этого трека от 1 до 100% (необязательно)",
    )
    @app_commands.checks.cooldown(
        1,
        MUSIC_PLAY_COOLDOWN_SECONDS,
        key=lambda interaction: interaction.user.id,
    )
    async def play(
        self,
        interaction: discord.Interaction,
        запрос: app_commands.Range[str, 1, 200],
        громкость: app_commands.Range[int, 1, 100] | None = None,
    ) -> None:
        text_channel_id = interaction.channel_id
        if (
            interaction.guild is None
            or text_channel_id is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.voice is None
            or not isinstance(interaction.user.voice.channel, discord.VoiceChannel)
        ):
            await respond_v2(
                interaction,
                "Сначала зайдите в голосовой канал.",
                ephemeral=True,
            )
            return
        voice_channel = interaction.user.voice.channel
        if interaction.channel_id != voice_channel.id:
            await respond_v2(
                interaction,
                "Откройте чат голосового канала, в котором вы сейчас находитесь.",
                ephemeral=True,
            )
            return
        voice = cast(discord.VoiceClient | None, interaction.guild.voice_client)
        if voice is not None and getattr(voice.channel, "id", None) != voice_channel.id:
            await respond_v2(
                interaction,
                "Бот уже работает в другом голосовом канале.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        state = self._state(interaction.guild.id)
        async with state.lock:
            active = (
                len(state.queue) + (1 if state.current else 0) + len(state.pending_reservations)
            )
            user_active = (
                sum(track.requester_id == interaction.user.id for track in state.queue)
                + (
                    1
                    if state.current is not None
                    and state.current.requester_id == interaction.user.id
                    else 0
                )
                + sum(
                    user_id == interaction.user.id
                    for user_id in state.pending_reservations.values()
                )
            )
        if active >= MUSIC_MAX_QUEUE_SIZE:
            await followup_v2(interaction, "Музыкальная очередь заполнена.", ephemeral=True)
            return
        if user_active >= MUSIC_MAX_USER_QUEUE:
            await followup_v2(
                interaction,
                f"У одного пользователя может быть не более "
                f"**{MUSIC_MAX_USER_QUEUE}** активных песен.",
                ephemeral=True,
            )
            return

        # Reserve the slot *before* extraction. Reserving afterwards let an
        # unbounded number of concurrent searches all pass the capacity check,
        # spend up to a minute in yt-dlp, and then discover the queue was full —
        # so the wait was paid for nothing.
        async with state.lock:
            active = (
                len(state.queue) + (1 if state.current else 0) + len(state.pending_reservations)
            )
            user_active = (
                sum(track.requester_id == interaction.user.id for track in state.queue)
                + (
                    1
                    if state.current is not None
                    and state.current.requester_id == interaction.user.id
                    else 0
                )
                + sum(
                    user_id == interaction.user.id
                    for user_id in state.pending_reservations.values()
                )
            )
            if active >= MUSIC_MAX_QUEUE_SIZE or user_active >= MUSIC_MAX_USER_QUEUE:
                queue_available = False
            else:
                queue_available = True
                state.pending_reservations[interaction.id] = interaction.user.id
        if not queue_available:
            await followup_v2(
                interaction, "Музыкальная очередь заполнена.", ephemeral=True
            )
            return

        async def _release_reservation() -> None:
            async with state.lock:
                state.pending_reservations.pop(interaction.id, None)

        try:
            extracted = await _extract(str(запрос))
        except ExtractionBusy:
            await _release_reservation()
            await followup_v2(
                interaction,
                "Поиск перегружен, повторите через несколько секунд. "
                "`MUSIC.EXTRACT_BUSY`",
                ephemeral=True,
            )
            return
        except ExtractionUnavailable:
            await _release_reservation()
            await followup_v2(
                interaction,
                "Источник музыки временно недоступен, попробуйте другую ссылку. "
                "`MUSIC.SOURCE_DOWN`",
                ephemeral=True,
            )
            return
        except (TimeoutError, asyncio.TimeoutError):
            await _release_reservation()
            await followup_v2(
                interaction,
                f"Поиск занял больше {MUSIC_EXTRACT_TIMEOUT_SECONDS} сек. "
                "и был прерван. `MUSIC.EXTRACT_TIMEOUT`",
                ephemeral=True,
            )
            return
        except ValueError as exc:
            # Only ValueError carries a message meant for the user: a rejected
            # domain, a live stream, a track that does not exist.
            await _release_reservation()
            await followup_v2(
                interaction, f"Не удалось добавить трек: {exc}", ephemeral=True
            )
            return
        except Exception:
            logger.exception("Music extraction failed for user %s", interaction.user.id)
            await _release_reservation()
            await followup_v2(
                interaction,
                "Источник музыки временно недоступен. `MUSIC.EXTRACT_FAILED`",
                ephemeral=True,
            )
            return
        if extracted.duration_seconds > MUSIC_MAX_TRACK_SECONDS:
            await _release_reservation()
            await followup_v2(
                interaction,
                f"Максимальная длительность — {_duration(MUSIC_MAX_TRACK_SECONDS)}.",
                ephemeral=True,
            )
            return
        price = (
            MUSIC_PRICE_LONG
            if extracted.duration_seconds > MUSIC_LONG_TRACK_SECONDS
            else MUSIC_PRICE_SHORT
        )
        try:
            track = await create_paid_music_track(
                interaction_id=interaction.id,
                guild_id=interaction.guild.id,
                text_channel_id=text_channel_id,
                voice_channel_id=voice_channel.id,
                requester_id=interaction.user.id,
                query=str(запрос),
                webpage_url=extracted.webpage_url,
                title=extracted.title,
                duration_seconds=extracted.duration_seconds,
                price=price,
                volume_percent=int(громкость or MUSIC_VOLUME_PERCENT),
            )
        except BaseException:
            async with state.lock:
                state.pending_reservations.pop(interaction.id, None)
            raise
        async with state.lock:
            state.pending_reservations.pop(interaction.id, None)
        if track is None:
            await followup_v2(
                interaction,
                f"Недостаточно Guinn Coins. Нужно **{_price(price)}**.",
                ephemeral=True,
            )
            return
        if track.state != "queued":
            await followup_v2(
                interaction,
                "Этот музыкальный заказ уже был обработан.",
                ephemeral=True,
            )
            return
        async with state.lock:
            if all(item.id != track.id for item in state.queue) and (
                state.current is None or state.current.id != track.id
            ):
                state.queue.append(track)
            if state.control_channel_id is None:
                state.control_channel_id = track.text_channel_id
            state.track_volumes.setdefault(track.id, track.volume_percent)
            state.notice = (
                f"{_track_link(track)} добавлен в очередь пользователем <@{track.requester_id}>."
            )
            state.wake.set()
            self._ensure_player(interaction.guild, state)
            position = len(state.queue) + (1 if state.current else 0)
            refresh_existing = state.current is not None or state.control_message is not None
        if refresh_existing:
            await self._refresh_player(interaction.guild, state)
        await followup_v2(
            interaction,
            embed=_track_embed(
                "Песня добавлена в очередь",
                track,
                extra=f"Позиция: **{position}**",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="queue", description="Показать музыкальную очередь")
    async def queue(self, interaction: discord.Interaction) -> None:
        await self.show_queue(interaction)

    @app_commands.command(name="now", description="Показать текущий трек")
    async def now_playing(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond_v2(interaction, "Команда доступна на сервере.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        state = self._state(interaction.guild.id)
        async with state.lock:
            track = state.current
        if track is None:
            await followup_v2(interaction, "Сейчас ничего не играет.", ephemeral=True)
            return
        message = await self._refresh_player(interaction.guild, state)
        await followup_v2(
            interaction,
            (
                f"Плеер обновлён: [открыть сообщение]({message.jump_url})."
                if message is not None
                else "Трек играет, но сообщение плеера сейчас недоступно."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="volume",
        description="Изменить громкость текущего трека",
    )
    @app_commands.describe(громкость="Громкость от 1 до 100%")
    async def music_volume(
        self,
        interaction: discord.Interaction,
        громкость: app_commands.Range[int, 1, 100],
    ) -> None:
        await self.handle_volume(interaction, target=int(громкость))

    @app_commands.command(name="skip", description="Пропустить трек или проголосовать за пропуск")
    async def skip(self, interaction: discord.Interaction) -> None:
        await self.handle_skip(interaction)

    @app_commands.command(name="cancel", description="Бесплатно отменить свой ошибочный трек")
    async def music_cancel(self, interaction: discord.Interaction) -> None:
        await self.handle_cancel(interaction)

    @app_commands.command(
        name="mode",
        description="Выбрать обычный режим, повтор трека или цикл очереди",
    )
    @app_commands.describe(режим="Режим музыкальной сессии")
    @app_commands.choices(
        режим=[
            app_commands.Choice(name="Без повтора — перейти дальше", value="off"),
            app_commands.Choice(name="Повтор трека — повторять текущий", value="track"),
            app_commands.Choice(name="Цикл очереди — повторять всю очередь", value="queue"),
        ]
    )
    async def music_mode(
        self,
        interaction: discord.Interaction,
        режим: app_commands.Choice[str],
    ) -> None:
        await self.handle_repeat_mode(
            interaction,
            requested_mode=режим.value,
        )

    @app_commands.command(name="pause", description="Поставить музыку на паузу")
    async def music_pause(self, interaction: discord.Interaction) -> None:
        await self._set_pause(interaction, target=True)

    @app_commands.command(name="resume", description="Продолжить воспроизведение музыки")
    async def music_resume(self, interaction: discord.Interaction) -> None:
        await self._set_pause(interaction, target=False)

    @app_commands.command(
        name="disconnect",
        description="Отключить плеер, удалить панель и очистить очередь",
    )
    async def music_disconnect(self, interaction: discord.Interaction) -> None:
        await self.handle_disconnect(interaction)

    @app_commands.command(name="stats", description="Показать статистику музыкальной экономики")
    async def music_stats(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not has_staff_access(interaction.user):
            await respond_v2(interaction, "Команда доступна старшему составу.", ephemeral=True)
            return
        stats = await get_music_statistics(interaction.guild.id)
        top = "\n".join(
            f"**{index}.** <@{user_id}> — {count}"
            for index, (user_id, count) in enumerate(stats["top"], 1)
        )
        embed = apply_accent(
            notice_card(
                code="MUSIC.STATS",
                title="Статистика музыкальной экономики",
                details=(
                    ("Заказано", str(stats["requested"])),
                    ("Доиграно", str(stats["completed"])),
                    ("Возвратов", str(stats["refunded"])),
                    ("Списано всего", _price(int(stats["gross_spent"]))),
                    ("Возвращено", _price(int(stats["returned"]))),
                    ("Топ заказчиков", top or "Пока нет данных."),
                ),
                next_step="Показаны накопленные итоги музыкальной экономики.",
            ),
            Accent.SERVICE,
        )
        await respond_v2(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
