"""Resolve the active external data source from the server profile."""

from __future__ import annotations

from dataclasses import dataclass

from config import RAGE_MASTER_ENABLED, RAGE_SERVER_NAME, RAGE_SERVER_QUERY
from core.profile import active_profile
from services.external.base import (
    SOURCE_MAJESTIC_API,
    SOURCE_NONE,
    SOURCE_RAGE_MASTER,
    ExternalSource,
)
from services.external.majestic_api import (
    MajesticApiClient,
    majestic_integration_ready,
    majestic_missing_parts,
)
from services.external.rage_master import RageMasterSource

__all__ = [
    "OnlineBinding",
    "close_external_clients",
    "majestic_client",
    "majestic_integration_ready",
    "majestic_missing_parts",
    "reset_external_clients",
    "resolve_online_binding",
    "resolve_source",
]

_rage: RageMasterSource | None = None
_majestic: MajesticApiClient | None = None


@dataclass(frozen=True, slots=True)
class OnlineBinding:
    source: ExternalSource | None
    server_name: str
    server_query: str
    source_key: str


def _rage_source() -> RageMasterSource:
    global _rage
    if _rage is None:
        _rage = RageMasterSource()
    return _rage


def _majestic_source() -> MajesticApiClient:
    global _majestic
    if _majestic is None:
        _majestic = MajesticApiClient()
    return _majestic


def majestic_client() -> MajesticApiClient:
    return _majestic_source()


def reset_external_clients() -> None:
    global _rage, _majestic
    _rage = None
    _majestic = None


async def close_external_clients() -> None:
    global _rage, _majestic
    if _rage is not None:
        await _rage.close()
        _rage = None
    if _majestic is not None:
        await _majestic.close()
        _majestic = None


async def resolve_source(guild_id: int) -> ExternalSource | None:
    binding = await resolve_online_binding(guild_id)
    return binding.source


async def resolve_online_binding(guild_id: int) -> OnlineBinding:
    profile = await active_profile(guild_id)
    key = profile.online_source
    if key == SOURCE_NONE:
        return OnlineBinding(None, "", "", SOURCE_NONE)
    if key == SOURCE_RAGE_MASTER:
        name = RAGE_SERVER_NAME or profile.server_label
        query = RAGE_SERVER_QUERY
        if not RAGE_MASTER_ENABLED or not query:
            return OnlineBinding(None, name, query, key)
        return OnlineBinding(_rage_source(), name, query, key)
    if key == SOURCE_MAJESTIC_API:
        if not await majestic_integration_ready(guild_id):
            return OnlineBinding(None, profile.server_label, profile.server_key, key)
        return OnlineBinding(
            _majestic_source(),
            profile.server_label,
            profile.server_key,
            key,
        )
    return OnlineBinding(None, "", "", key)
