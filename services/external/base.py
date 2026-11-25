"""Shared contract for game-world data sources.

Missing numbers stay missing. A failed fetch never becomes zero, and a stale
snapshot is labelled with the time it was last trusted.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

MSK = timezone(timedelta(hours=3))

SOURCE_NONE = "none"
SOURCE_RAGE_MASTER = "rage_master"
SOURCE_MAJESTIC_API = "majestic_api"

RESOURCE_ONLINE = "online"
RESOURCE_RATING = "rating-organizations"
RESOURCE_FAMILY_WARS = "family-wars"
RESOURCE_CAPTURES = "captures"
RESOURCE_MANSIONS = "mansions"
RESOURCE_MULTIPURPOSE = "multipurpose"
RESOURCE_ARENA = "arena"
RESOURCE_USERS = "users"
RESOURCE_MASTER_USERS = "master-users"

MARKETPLACE_KINDS = frozenset(
    {
        "vehicles",
        "items",
        "houses",
        "apartments",
        "warehouses",
        "offices",
        "clothes",
    }
)
RATING_PERIODS = frozenset({"week", "period", "season"})


def rating_snapshot_resource(period: str) -> str:
    """Storage key so week / period / season snapshots do not overwrite each other."""

    cleaned = period.strip()
    if cleaned not in RATING_PERIODS:
        return RESOURCE_RATING
    return f"{RESOURCE_RATING}/{cleaned}"


PERSONAL_RESOURCES = frozenset({RESOURCE_USERS, RESOURCE_MASTER_USERS})
CATALOG_RESOURCES = (
    RESOURCE_RATING,
    RESOURCE_FAMILY_WARS,
    RESOURCE_CAPTURES,
    RESOURCE_MANSIONS,
    RESOURCE_MULTIPURPOSE,
    RESOURCE_ARENA,
    "marketplace/vehicles",
    "marketplace/items",
    "marketplace/houses",
    "marketplace/apartments",
    "marketplace/warehouses",
    "marketplace/offices",
    "marketplace/clothes",
    RESOURCE_USERS,
    RESOURCE_MASTER_USERS,
)

_PLAYER_KEYS = ("players", "online", "playerCount", "count")


class ExternalSourceError(Exception):
    """Machine-readable refusal from an external source."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    resource: str
    source_key: str
    source_label: str
    payload_text: str | None
    fetched_at: str | None
    stale: bool
    attribution: str
    unavailable_reason: str | None = None

    def payload(self) -> Any:
        if self.payload_text is None:
            return None
        return json.loads(self.payload_text)

    def players(self) -> int | None:
        if self.unavailable_reason and self.payload_text is None:
            return None
        return extract_online_players(self.payload())


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def format_fetched_clock(fetched_at: str | None) -> str | None:
    if not fetched_at:
        return None
    try:
        parsed = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(MSK)
    return f"{local.hour:02d}:{local.minute:02d}"


def dump_payload(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def extract_online_players(payload: object) -> int | None:
    """Return a count only when a players-like field is present. Missing is not 0."""

    if isinstance(payload, bool):
        return None
    if isinstance(payload, int):
        return payload if payload >= 0 else None
    if not isinstance(payload, Mapping):
        return None
    for key in _PLAYER_KEYS:
        if key not in payload:
            continue
        raw = payload.get(key)
        if isinstance(raw, bool) or raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    nested = payload.get("data")
    if nested is not None and nested is not payload:
        return extract_online_players(nested)
    return None


def unavailable_snapshot(
    *,
    resource: str,
    source_key: str,
    source_label: str,
    reason: str,
    attribution: str = "",
) -> ResourceSnapshot:
    return ResourceSnapshot(
        resource=resource,
        source_key=source_key,
        source_label=source_label,
        payload_text=None,
        fetched_at=None,
        stale=False,
        attribution=attribution,
        unavailable_reason=reason,
    )


def snapshot_from_row(
    *,
    resource: str,
    source_key: str,
    source_label: str,
    payload_text: str,
    fetched_at: str,
    attribution: str,
    stale: bool,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        resource=resource,
        source_key=source_key,
        source_label=source_label,
        payload_text=payload_text,
        fetched_at=fetched_at,
        stale=stale,
        attribution=attribution,
        unavailable_reason=None,
    )


class ExternalSource(ABC):
    source_key: str
    source_label: str

    @abstractmethod
    async def fetch_online(self, guild_id: int) -> ResourceSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def fetch_org_rating(self, guild_id: int, period: str) -> ResourceSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def fetch_captures(self, guild_id: int) -> ResourceSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def fetch_family_wars(self, guild_id: int) -> ResourceSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def fetch_marketplace(self, guild_id: int, kind: str) -> ResourceSnapshot:
        raise NotImplementedError

    async def close(self) -> None:
        return None
