"""Active project/server/family/season context for Memphis and archived Milton.

Catalogs, calendar, copy, online source and activity templates all read this
profile. Game numbers that are not confirmed stay empty and are never invented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KNOWN_UPGRADES = frozenset(
    {
        "territory_control",
        "air_cargo",
        "workshops",
        "dealers",
        "mat_truck_l1",
        "mat_truck_l2",
        "mat_truck_l3",
        "psn",
        "fort_zancudo",
        "cayo_perico",
        "bank_robbery",
        "business_robbery",
        "player_robbery",
        "truck_intercept",
    }
)
KNOWN_DIRECTIONS = frozenset({"legal", "criminal", "unset"})
KNOWN_ONLINE_SOURCES = frozenset({"none", "majestic_api", "rage_master"})
DEFAULT_TIMEZONE = "Europe/Moscow"
SETTING_PREFIX = "profile."
_OVERLAY_FIELDS = frozenset(
    {
        "server_key",
        "family_name",
        "family_tag",
        "direction",
        "timezone",
        "season_key",
        "season_start",
        "season_end",
        "online_source",
        "enabled_upgrades",
        "member_limit",
        "allow_archived",
        "external_server_id",
    }
)


class ProfileConfigError(ValueError):
    """Invalid project/server profile configuration."""


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """Immutable identity of a known RP server."""

    project: str
    server_key: str
    server_label: str
    external_server_id: str
    archived: bool


PROFILE_REGISTRY: Mapping[str, ServerIdentity] = MappingProxyType(
    {
        "memphis": ServerIdentity(
            project="majestic",
            server_key="memphis",
            server_label="Majestic RP Memphis",
            external_server_id="RU19",
            archived=False,
        ),
        "milton": ServerIdentity(
            project="gta5rp",
            server_key="milton",
            server_label="GTA 5 RP Milton",
            external_server_id="",
            archived=True,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ServerProfile:
    """Resolved context of the family on a specific project and server."""

    project: str
    server_key: str
    server_label: str
    external_server_id: str
    family_name: str
    family_tag: str
    direction: str
    timezone: str
    season_key: str
    season_start: date
    season_end: date
    online_source: str
    enabled_upgrades: frozenset[str]
    member_limit: int | None
    archived: bool

    def requires(self, upgrade: str) -> bool:
        return profile_requires(self, upgrade)


def profile_requires(profile: ServerProfile, upgrade: str) -> bool:
    """Return whether an activity template may run for this profile."""

    if upgrade not in KNOWN_UPGRADES:
        raise ProfileConfigError(_unknown_upgrade_message((upgrade,)))
    return upgrade in profile.enabled_upgrades


def format_profile_log(profile: ServerProfile) -> str:
    """One startup line: project, server, family, direction, season, upgrades."""

    return (
        "Active profile "
        f"project={profile.project} "
        f"server={profile.server_key} "
        f"family={profile.family_name} "
        f"direction={profile.direction} "
        f"season={profile.season_key} "
        f"upgrades={len(profile.enabled_upgrades)}"
    )


def build_profile(values: Mapping[str, Any]) -> ServerProfile:
    """Build a profile from flattened TOML and/or guild_settings overlay."""

    server_key = _required_text(values.get("server_key"), "server_key").casefold()
    identity = PROFILE_REGISTRY.get(server_key)
    if identity is None:
        known = ", ".join(sorted(PROFILE_REGISTRY))
        raise ProfileConfigError(
            f"Неизвестный ключ сервера {server_key!r}. "
            f"Допустимы только {known}."
        )

    allow_archived = _parse_bool(values.get("allow_archived"), default=False)
    if identity.archived and not allow_archived:
        raise ProfileConfigError(
            f"Профиль {identity.server_key} архивный и доступен только на чтение. "
            "Сделать его активным можно только явной настройкой allow_archived."
        )

    family_name = _required_text(values.get("family_name"), "family_name")
    family_tag = _optional_text(values.get("family_tag"))
    direction = _parse_choice(
        values.get("direction"),
        field="direction",
        allowed=KNOWN_DIRECTIONS,
        default="unset",
    )
    timezone = _validate_timezone(
        _optional_text(values.get("timezone")) or DEFAULT_TIMEZONE
    )
    season_key = _required_text(values.get("season_key"), "season_key")
    season_start = _parse_date(values.get("season_start"), "season_start")
    season_end = _parse_date(values.get("season_end"), "season_end")
    if season_end < season_start:
        raise ProfileConfigError(
            "Дата окончания сезона не может быть раньше даты начала."
        )
    online_source = _parse_choice(
        values.get("online_source"),
        field="online_source",
        allowed=KNOWN_ONLINE_SOURCES,
        default="none",
    )
    enabled_upgrades = _parse_upgrades(values.get("enabled_upgrades"))
    member_limit = _parse_member_limit(values.get("member_limit"))
    external_server_id = _optional_text(values.get("external_server_id"))
    if not external_server_id:
        external_server_id = identity.external_server_id

    return ServerProfile(
        project=identity.project,
        server_key=identity.server_key,
        server_label=identity.server_label,
        external_server_id=external_server_id,
        family_name=family_name,
        family_tag=family_tag,
        direction=direction,
        timezone=timezone,
        season_key=season_key,
        season_start=season_start,
        season_end=season_end,
        online_source=online_source,
        enabled_upgrades=enabled_upgrades,
        member_limit=member_limit,
        archived=identity.archived,
    )


async def active_profile(guild_id: int) -> ServerProfile:
    """TOML ``[profile]``, then guild_settings, then runtime_settings."""

    from config import profile_toml_values

    merged: dict[str, Any] = dict(profile_toml_values())
    merged.update(await _overlay_from_guild(guild_id))
    merged.update(await _overlay_from_runtime(guild_id))
    return build_profile(merged)


async def _overlay_from_guild(guild_id: int) -> dict[str, str]:
    from repositories.static_messages import list_guild_settings

    raw = await list_guild_settings(guild_id, prefix=SETTING_PREFIX)
    overlay: dict[str, str] = {}
    for key, value in raw.items():
        field = key.removeprefix(SETTING_PREFIX)
        if field in _OVERLAY_FIELDS:
            overlay[field] = value
    return overlay


async def _overlay_from_runtime(guild_id: int) -> dict[str, str]:
    from repositories.settings import list_runtime_settings

    raw = await list_runtime_settings(guild_id)
    return {
        key: value
        for key, value in raw.items()
        if key in _OVERLAY_FIELDS
    }


def _unknown_upgrade_message(unknown: tuple[str, ...]) -> str:
    allowed = ", ".join(sorted(KNOWN_UPGRADES))
    names = ", ".join(unknown)
    return (
        f"Неизвестный идентификатор улучшения организации: {names}. "
        f"Допустимы: {allowed}."
    )


def _parse_upgrades(value: Any) -> frozenset[str]:
    if value is None or value == "":
        return frozenset()
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item).strip() for item in value]
    else:
        items = [part.strip() for part in str(value).split(",")]
    upgrades = [item for item in items if item]
    unknown = tuple(sorted({item for item in upgrades if item not in KNOWN_UPGRADES}))
    if unknown:
        raise ProfileConfigError(_unknown_upgrade_message(unknown))
    return frozenset(upgrades)


def _parse_member_limit(value: Any) -> int | None:
    """Positive integer, or None when the game limit still requires configuration."""

    if value is None or isinstance(value, bool):
        if value is None:
            return None
        raise ProfileConfigError(
            "Лимит состава должен быть положительным целым числом или пустым, "
            "если игровой предел ещё не задан."
        )
    if isinstance(value, int):
        if value <= 0:
            raise ProfileConfigError(
                "Лимит состава должен быть положительным целым числом или пустым, "
                "если игровой предел ещё не задан."
            )
        return value
    text = str(value).strip()
    if not text or text.casefold() in {"none", "unset", "requires_configuration"}:
        return None
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ProfileConfigError(
            "Лимит состава должен быть положительным целым числом или пустым, "
            "если игровой предел ещё не задан."
        ) from exc
    if parsed <= 0:
        raise ProfileConfigError(
            "Лимит состава должен быть положительным целым числом или пустым, "
            "если игровой предел ещё не задан."
        )
    return parsed


def _parse_date(value: Any, field: str) -> date:
    text = _required_text(value, field)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ProfileConfigError(
            f"{field} должно быть датой в формате ГГГГ-ММ-ДД."
        ) from exc


def _parse_choice(
    value: Any,
    *,
    field: str,
    allowed: frozenset[str],
    default: str,
) -> str:
    text = _optional_text(value).casefold()
    if not text:
        return default
    if text not in allowed:
        names = ", ".join(sorted(allowed))
        raise ProfileConfigError(
            f"{field} должно быть одним из: {names}. Получено: {text!r}."
        )
    return text


def _parse_bool(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ProfileConfigError(
        "allow_archived должно быть true или false."
    )


def _validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        if name not in {DEFAULT_TIMEZONE, "UTC"}:
            raise ProfileConfigError(
                f"Неизвестный часовой пояс {name!r}. "
                "Укажите идентификатор IANA, например Europe/Moscow."
            ) from exc
    return name


def _required_text(value: Any, field: str) -> str:
    text = _optional_text(value)
    if not text:
        raise ProfileConfigError(f"{field} не может быть пустым.")
    return text


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
