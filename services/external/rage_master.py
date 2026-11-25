"""Archived RAGE master-list source. Behaviour matches the former Milton poller."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any

import aiohttp

from config import (
    PRESENCE_GAME_INTERVAL,
    RAGE_MASTER_URL,
    RAGE_REQUEST_TIMEOUT,
    RAGE_SERVER_QUERY,
)
from repositories.game_online import get_external_snapshot, upsert_external_snapshot
from services.external.base import (
    RESOURCE_ARENA,
    RESOURCE_CAPTURES,
    RESOURCE_FAMILY_WARS,
    RESOURCE_MANSIONS,
    RESOURCE_MULTIPURPOSE,
    RESOURCE_ONLINE,
    RESOURCE_RATING,
    SOURCE_RAGE_MASTER,
    ExternalSource,
    ResourceSnapshot,
    dump_payload,
    snapshot_from_row,
    unavailable_snapshot,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

_ARCHIVE_REASON = "архивный источник master-list этого не отдаёт"
_SOURCE_LABEL = "RAGE master-list"


def extract_players(payload: object, query: str) -> int:
    if isinstance(payload, Mapping):
        records: Iterable[object] = payload.values()
    elif isinstance(payload, list):
        records = payload
    else:
        raise ValueError("Master-list response is not an object or list")

    needle = query.casefold()
    matches: list[int] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        name = str(record.get("name", ""))
        if needle not in name.casefold():
            continue
        try:
            players = int(record.get("players", 0))
        except (TypeError, ValueError):
            continue
        if players >= 0:
            matches.append(players)
    if not matches:
        raise LookupError(f"Server matching {query!r} was not found")
    return max(matches)


class RageMasterSource(ExternalSource):
    source_key = SOURCE_RAGE_MASTER
    source_label = _SOURCE_LABEL

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        query: str | None = None,
        master_url: str | None = None,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._query = (query or RAGE_SERVER_QUERY).strip()
        self._master_url = (master_url or RAGE_MASTER_URL).strip()
        self._memory: dict[str, tuple[int, ResourceSnapshot]] = {}
        window = PRESENCE_GAME_INTERVAL if PRESENCE_GAME_INTERVAL else 60
        self._ttl_ms = max(1, window) * 1000

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _session_obj(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout_s = RAGE_REQUEST_TIMEOUT if RAGE_REQUEST_TIMEOUT else 10
            timeout = aiohttp.ClientTimeout(total=max(1, timeout_s))
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    def _cached(self, resource: str) -> ResourceSnapshot | None:
        item = self._memory.get(resource)
        if item is None:
            return None
        stored_ms, snapshot = item
        now_ms = int(time.monotonic() * 1000)
        if now_ms - stored_ms >= self._ttl_ms:
            return None
        return snapshot

    def _remember(self, snapshot: ResourceSnapshot) -> None:
        if snapshot.stale or snapshot.payload_text is None:
            return
        self._memory[snapshot.resource] = (int(time.monotonic() * 1000), snapshot)

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

    async def fetch_online(self, guild_id: int) -> ResourceSnapshot:
        cached = self._cached(RESOURCE_ONLINE)
        if cached is not None:
            return cached
        try:
            session = await self._session_obj()
            async with session.get(self._master_url) as response:
                response.raise_for_status()
                payload: Any = await response.json(content_type=None)
            players = extract_players(payload, self._query)
            fetched_at = utc_now_iso()
            body = dump_payload({"players": players})
            await upsert_external_snapshot(
                guild_id,
                self.source_key,
                RESOURCE_ONLINE,
                body,
                fetched_at,
                "",
            )
            snapshot = snapshot_from_row(
                resource=RESOURCE_ONLINE,
                source_key=self.source_key,
                source_label=self.source_label,
                payload_text=body,
                fetched_at=fetched_at,
                attribution="",
                stale=False,
            )
            self._remember(snapshot)
            return snapshot
        except (aiohttp.ClientError, asyncio.TimeoutError, LookupError, ValueError):
            logger.warning(
                "Could not refresh %s online count",
                self._query,
                exc_info=True,
            )
            trusted = await self._last_trusted(guild_id, RESOURCE_ONLINE)
            if trusted is not None:
                return trusted
            return unavailable_snapshot(
                resource=RESOURCE_ONLINE,
                source_key=self.source_key,
                source_label=self.source_label,
                reason="онлайн недоступен",
            )

    async def fetch_org_rating(self, guild_id: int, period: str) -> ResourceSnapshot:
        return self._unsupported(RESOURCE_RATING)

    async def fetch_captures(self, guild_id: int) -> ResourceSnapshot:
        return self._unsupported(RESOURCE_CAPTURES)

    async def fetch_family_wars(self, guild_id: int) -> ResourceSnapshot:
        return self._unsupported(RESOURCE_FAMILY_WARS)

    async def fetch_marketplace(self, guild_id: int, kind: str) -> ResourceSnapshot:
        return self._unsupported(f"marketplace/{kind}")

    def _unsupported(self, resource: str) -> ResourceSnapshot:
        return unavailable_snapshot(
            resource=resource,
            source_key=self.source_key,
            source_label=self.source_label,
            reason=_ARCHIVE_REASON,
        )

    async def fetch_mansions(self, guild_id: int) -> ResourceSnapshot:
        return self._unsupported(RESOURCE_MANSIONS)

    async def fetch_multipurpose(self, guild_id: int) -> ResourceSnapshot:
        return self._unsupported(RESOURCE_MULTIPURPOSE)

    async def fetch_arena(self, guild_id: int) -> ResourceSnapshot:
        return self._unsupported(RESOURCE_ARENA)
