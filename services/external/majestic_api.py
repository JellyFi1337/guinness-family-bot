"""Official Majestic public API client.

Four staff-filled values stay empty on purpose: base URL, API version, Memphis
serverId, rate limit N/M, and the ToS attribution format. Until they are set
and MAJESTIC_API_KEY is present in .env, this client must not start.

Wiki HTML is never fetched or parsed. Personal endpoints ``users`` and
``master-users`` are catalogued and refused: no dossiers on foreign-family
players and no personal summaries by nick.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from core.settings import get_registry
from repositories.game_online import get_external_snapshot, upsert_external_snapshot
from services.external.base import (
    CATALOG_RESOURCES,
    MARKETPLACE_KINDS,
    PERSONAL_RESOURCES,
    RATING_PERIODS,
    RESOURCE_ARENA,
    RESOURCE_CAPTURES,
    RESOURCE_FAMILY_WARS,
    RESOURCE_MANSIONS,
    RESOURCE_MULTIPURPOSE,
    RESOURCE_ONLINE,
    RESOURCE_RATING,
    SOURCE_MAJESTIC_API,
    ExternalSource,
    ExternalSourceError,
    ResourceSnapshot,
    dump_payload,
    rating_snapshot_resource,
    snapshot_from_row,
    unavailable_snapshot,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

API_KEY_HEADER = "x-api-key"
API_KEY_ENV = "MAJESTIC_API_KEY"
_SOURCE_LABEL = "Majestic API"
_MAX_RETRIES = 4
_BACKOFF_CAP_SECONDS = 30
SleepFn = Callable[[int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MajesticSettings:
    base_url: str
    version: str
    server_id: str
    rate_limit: int
    window_seconds: int
    attribution: str
    key_owner: str
    timeout_seconds: int

    def missing_parts(self, *, key_present: bool) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.base_url:
            missing.append("базовый URL")
        if not self.version:
            missing.append("версия API")
        if not self.server_id:
            missing.append("serverId")
        if self.rate_limit <= 0 or self.window_seconds <= 0:
            missing.append("лимит запросов")
        if not self.attribution:
            missing.append("формат подписи источника")
        if not key_present:
            missing.append("ключ в .env")
        return tuple(missing)

    def ready(self, *, key_present: bool) -> bool:
        return not self.missing_parts(key_present=key_present)


def read_api_key() -> str:
    return os.getenv(API_KEY_ENV, "").strip()


async def load_majestic_settings(guild_id: int) -> MajesticSettings:
    registry = get_registry()
    base = str(await registry.get(guild_id, "EXTERNAL_API_BASE_URL") or "").strip()
    version = str(await registry.get(guild_id, "EXTERNAL_API_VERSION") or "").strip()
    server_id = str(await registry.get(guild_id, "EXTERNAL_API_SERVER_ID") or "").strip()
    rate_limit = int(await registry.get(guild_id, "EXTERNAL_API_RATE_LIMIT") or 0)
    window = int(await registry.get(guild_id, "EXTERNAL_API_RATE_WINDOW_SECONDS") or 0)
    attribution = str(await registry.get(guild_id, "EXTERNAL_API_ATTRIBUTION") or "").strip()
    owner = str(await registry.get(guild_id, "EXTERNAL_KEY_OWNER") or "").strip()
    timeout = int(await registry.get(guild_id, "EXTERNAL_API_TIMEOUT_SECONDS") or 10)
    return MajesticSettings(
        base_url=base.rstrip("/"),
        version=version.strip("/"),
        server_id=server_id,
        rate_limit=rate_limit,
        window_seconds=window,
        attribution=attribution,
        key_owner=owner,
        timeout_seconds=timeout,
    )


async def majestic_integration_ready(guild_id: int) -> bool:
    settings = await load_majestic_settings(guild_id)
    return settings.ready(key_present=bool(read_api_key()))


async def majestic_missing_parts(guild_id: int) -> tuple[str, ...]:
    settings = await load_majestic_settings(guild_id)
    return settings.missing_parts(key_present=bool(read_api_key()))


class _RateLimiter:
    def __init__(self, limit: int, window_seconds: int, sleeper: SleepFn) -> None:
        self._limit = max(1, limit)
        self._window_ms = max(1, window_seconds) * 1000
        self._times: list[int] = []
        self._sleeper = sleeper

    async def acquire(self) -> None:
        while True:
            now_ms = int(time.monotonic() * 1000)
            self._times = [stamp for stamp in self._times if now_ms - stamp < self._window_ms]
            if len(self._times) < self._limit:
                self._times.append(now_ms)
                return
            wait_ms = self._window_ms - (now_ms - self._times[0])
            wait_s = max(1, (wait_ms + 999) // 1000)
            await self._sleeper(wait_s)


async def _sleep_seconds(seconds: int) -> None:
    await asyncio.sleep(seconds)


class MajesticApiClient(ExternalSource):
    source_key = SOURCE_MAJESTIC_API
    source_label = _SOURCE_LABEL
    catalog = CATALOG_RESOURCES

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        sleeper: SleepFn | None = None,
        api_key: str | None = None,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._sleeper = sleeper or _sleep_seconds
        self._api_key = api_key
        self._memory: dict[str, tuple[int, ResourceSnapshot]] = {}
        self._limiter: _RateLimiter | None = None
        self._limiter_key: tuple[int, int] | None = None

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _key(self) -> str:
        if self._api_key is not None:
            return self._api_key.strip()
        return read_api_key()

    def _headers(self) -> dict[str, str]:
        key = self._key()
        if not key:
            return {}
        return {API_KEY_HEADER: key}

    async def _session_obj(self, timeout_seconds: int) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=max(1, timeout_seconds))
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    def _limiter_for(self, settings: MajesticSettings) -> _RateLimiter:
        key = (settings.rate_limit, settings.window_seconds)
        if self._limiter is None or self._limiter_key != key:
            self._limiter = _RateLimiter(
                settings.rate_limit,
                settings.window_seconds,
                self._sleeper,
            )
            self._limiter_key = key
        return self._limiter

    def _ttl_ms(self, settings: MajesticSettings) -> int:
        window = settings.window_seconds if settings.window_seconds > 0 else 60
        return window * 1000

    def _cached(self, resource: str, settings: MajesticSettings) -> ResourceSnapshot | None:
        item = self._memory.get(resource)
        if item is None:
            return None
        stored_ms, snapshot = item
        now_ms = int(time.monotonic() * 1000)
        if now_ms - stored_ms >= self._ttl_ms(settings):
            return None
        return snapshot

    def _remember(self, snapshot: ResourceSnapshot) -> None:
        if snapshot.stale or snapshot.payload_text is None:
            return
        self._memory[snapshot.resource] = (int(time.monotonic() * 1000), snapshot)

    def _url(self, settings: MajesticSettings, resource: str) -> str:
        return f"{settings.base_url}/{settings.version}/{resource.strip().strip('/')}"

    async def _last_trusted(self, guild_id: int, resource: str) -> ResourceSnapshot | None:
        row = await get_external_snapshot(guild_id, self.source_key, resource)
        if row is None:
            return None
        return snapshot_from_row(
            resource=row.resource,
            source_key=row.source_key,
            source_label=self.source_label,
            payload_text=row.payload_text,
            fetched_at=row.fetched_at,
            attribution=row.attribution,
            stale=True,
        )

    async def _fallback(
        self,
        guild_id: int,
        resource: str,
        settings: MajesticSettings,
        reason: str,
    ) -> ResourceSnapshot:
        trusted = await self._last_trusted(guild_id, resource)
        if trusted is not None:
            return trusted
        return unavailable_snapshot(
            resource=resource,
            source_key=self.source_key,
            source_label=self.source_label,
            reason=reason,
            attribution=settings.attribution,
        )

    def _retry_after_seconds(self, headers: Mapping[str, str], backoff: int) -> int:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is None:
            return backoff
        try:
            parsed = int(str(raw).strip())
        except (TypeError, ValueError):
            return backoff
        if parsed < 1:
            return backoff
        return parsed

    async def _request_json(
        self,
        settings: MajesticSettings,
        resource: str,
        params: Mapping[str, str] | None,
    ) -> Any:
        if resource in PERSONAL_RESOURCES:
            raise ExternalSourceError(
                "не строить досье на игроков чужих семей",
                code="EXT.NO_DOSSIER",
            )
        session = await self._session_obj(settings.timeout_seconds)
        limiter = self._limiter_for(settings)
        url = self._url(settings, resource)
        backoff = 1
        last_error = "запрос к API не удался"
        for _attempt in range(_MAX_RETRIES):
            await limiter.acquire()
            try:
                async with session.get(
                    url,
                    headers=self._headers(),
                    params=dict(params) if params else None,
                ) as response:
                    if response.status == 429:
                        wait_s = self._retry_after_seconds(response.headers, backoff)
                        logger.warning(
                            "Majestic API rate-limited for %s, waiting %s s",
                            resource,
                            wait_s,
                        )
                        await self._sleeper(wait_s)
                        backoff = min(backoff * 2, _BACKOFF_CAP_SECONDS)
                        last_error = "лимит запросов, ожидание"
                        continue
                    if response.status in {401, 403}:
                        logger.warning("Majestic API key rejected for %s", resource)
                        raise _HttpFailure("ключ API отклонён")
                    if response.status >= 400:
                        logger.warning(
                            "Majestic API HTTP %s for %s",
                            response.status,
                            resource,
                        )
                        raise _HttpFailure("ответ API недоступен")
                    return await response.json(content_type=None)
            except asyncio.TimeoutError:
                last_error = "таймаут запроса"
                logger.warning("Majestic API timeout for %s", resource)
                break
            except aiohttp.ClientError:
                last_error = "сеть API недоступна"
                logger.warning("Majestic API request failed for %s", resource)
                break
            except _HttpFailure as exc:
                last_error = str(exc)
                break
        raise _HttpFailure(last_error)

    async def _fetch_resource(
        self,
        guild_id: int,
        resource: str,
        settings: MajesticSettings,
        params: Mapping[str, str] | None = None,
        *,
        storage_key: str | None = None,
    ) -> ResourceSnapshot:
        stored = (storage_key or resource).strip() or resource
        cached = self._cached(stored, settings)
        if cached is not None:
            return cached
        try:
            payload = await self._request_json(settings, resource, params)
        except ExternalSourceError:
            raise
        except _HttpFailure as exc:
            return await self._fallback(guild_id, stored, settings, str(exc))
        fetched_at = utc_now_iso()
        body = dump_payload(payload)
        await upsert_external_snapshot(
            guild_id,
            self.source_key,
            stored,
            body,
            fetched_at,
            settings.attribution,
        )
        snapshot = snapshot_from_row(
            resource=stored,
            source_key=self.source_key,
            source_label=self.source_label,
            payload_text=body,
            fetched_at=fetched_at,
            attribution=settings.attribution,
            stale=False,
        )
        self._remember(snapshot)
        return snapshot

    async def fetch_online(self, guild_id: int) -> ResourceSnapshot:
        settings = await load_majestic_settings(guild_id)
        if not settings.ready(key_present=bool(self._key())):
            return unavailable_snapshot(
                resource=RESOURCE_ONLINE,
                source_key=self.source_key,
                source_label=self.source_label,
                reason="интеграция выключена: настройки API не заполнены",
                attribution=settings.attribution,
            )
        return await self._fetch_resource(
            guild_id,
            RESOURCE_ONLINE,
            settings,
            {"serverId": settings.server_id} if settings.server_id else None,
        )

    async def fetch_org_rating(self, guild_id: int, period: str) -> ResourceSnapshot:
        cleaned = period.strip()
        if cleaned not in RATING_PERIODS:
            raise ExternalSourceError(
                "period должен быть week, period или season",
                code="EXT.BAD_PERIOD",
            )
        settings = await load_majestic_settings(guild_id)
        stored = rating_snapshot_resource(cleaned)
        if not settings.ready(key_present=bool(self._key())):
            return unavailable_snapshot(
                resource=stored,
                source_key=self.source_key,
                source_label=self.source_label,
                reason="интеграция выключена: настройки API не заполнены",
                attribution=settings.attribution,
            )
        params = {"period": cleaned}
        if settings.server_id:
            params["serverId"] = settings.server_id
        return await self._fetch_resource(
            guild_id,
            RESOURCE_RATING,
            settings,
            params,
            storage_key=stored,
        )

    async def fetch_captures(self, guild_id: int) -> ResourceSnapshot:
        return await self._named(guild_id, RESOURCE_CAPTURES)

    async def fetch_family_wars(self, guild_id: int) -> ResourceSnapshot:
        return await self._named(guild_id, RESOURCE_FAMILY_WARS)

    async def fetch_mansions(self, guild_id: int) -> ResourceSnapshot:
        return await self._named(guild_id, RESOURCE_MANSIONS)

    async def fetch_multipurpose(self, guild_id: int) -> ResourceSnapshot:
        return await self._named(guild_id, RESOURCE_MULTIPURPOSE)

    async def fetch_arena(self, guild_id: int) -> ResourceSnapshot:
        return await self._named(guild_id, RESOURCE_ARENA)

    async def fetch_marketplace(self, guild_id: int, kind: str) -> ResourceSnapshot:
        cleaned = kind.strip()
        if cleaned not in MARKETPLACE_KINDS:
            raise ExternalSourceError(
                "неизвестный раздел маркетплейса",
                code="EXT.BAD_MARKETPLACE",
            )
        return await self._named(guild_id, f"marketplace/{cleaned}")

    async def fetch_personal(self, guild_id: int, resource: str) -> ResourceSnapshot:
        del guild_id, resource
        raise ExternalSourceError(
            "не строить досье на игроков чужих семей",
            code="EXT.NO_DOSSIER",
        )

    async def _named(self, guild_id: int, resource: str) -> ResourceSnapshot:
        settings = await load_majestic_settings(guild_id)
        if not settings.ready(key_present=bool(self._key())):
            return unavailable_snapshot(
                resource=resource,
                source_key=self.source_key,
                source_label=self.source_label,
                reason="интеграция выключена: настройки API не заполнены",
                attribution=settings.attribution,
            )
        params = {"serverId": settings.server_id} if settings.server_id else None
        return await self._fetch_resource(guild_id, resource, settings, params)


class _HttpFailure(Exception):
    """Internal: HTTP/network failure without leaking headers or the key."""
