"""Live settings registry: DB → TOML → code default, without a restart.

Shard fan-out is not implemented in this process: a write invalidates the
local cache immediately. A database write and its decision-log row share one
transaction; ``set_decision_recorder`` is only for extra listeners.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from configuration import is_secret_env_name

logger = logging.getLogger(__name__)

SETTING_GROUPS: Final[frozenset[str]] = frozenset(
    {
        "discord.channels",
        "discord.roles",
        "discord.categories",
        "access",
        "economy",
        "cooldowns",
        "profile",
        "texts",
        "limits",
        "onboarding",
        "roster",
        "reports",
        "queue",
        "calendar",
        "treasury",
        "contribution",
        "bonus",
        "inventory",
        "progression",
        "external",
    }
)
SETTING_TYPES: Final[frozenset[str]] = frozenset(
    {
        "channel_id",
        "role_id",
        "category_id",
        "int",
        "decimal",
        "bool",
        "string",
        "csv_ids",
    }
)
_SNOWFLAKE_TYPES = frozenset({"channel_id", "role_id", "category_id"})
_ROLE_ID_LIST_ALIASES: Final[dict[str, str]] = {
    "request_ping": "REQUEST_PING_ROLE_IDS",
    "high_tools": "HIGH_TOOLS_ALLOWED_ROLE_IDS",
    "poll": "POLL_ALLOWED_ROLE_IDS",
    "recruiting": "RECRUITING_ROLE_IDS",
    "ticket_assignment": "TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS",
}


class SettingsError(ValueError):
    """Invalid settings operation with a stable machine-readable code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SettingDescriptor:
    key: str
    group: str
    value_type: str
    default: Any
    title: str
    description: str
    restart_required: bool = False
    validator: Callable[[Any], Any] | None = None

    def __post_init__(self) -> None:
        if self.group not in SETTING_GROUPS:
            raise ValueError(f"Unknown settings group: {self.group}")
        if self.value_type not in SETTING_TYPES:
            raise ValueError(f"Unknown settings type: {self.value_type}")


@dataclass(frozen=True, slots=True)
class SettingChange:
    guild_id: int
    key: str
    before: str | None
    after: str | None
    updated_by: str
    source: str = "command"


OnChange = Callable[[SettingChange], Awaitable[None]]


class SettingsStore:
    async def get(self, guild_id: int, key: str) -> str | None:
        raise NotImplementedError

    async def list(self, guild_id: int) -> dict[str, str]:
        raise NotImplementedError

    async def set(
        self,
        guild_id: int,
        key: str,
        value: str,
        *,
        updated_by: str | None,
        db: Any | None = None,
    ) -> None:
        raise NotImplementedError

    async def delete(self, guild_id: int, key: str, db: Any | None = None) -> None:
        raise NotImplementedError


class DatabaseSettingsStore(SettingsStore):
    async def get(self, guild_id: int, key: str) -> str | None:
        from repositories.settings import get_runtime_setting

        return await get_runtime_setting(guild_id, key)

    async def list(self, guild_id: int) -> dict[str, str]:
        from repositories.settings import list_runtime_settings

        return await list_runtime_settings(guild_id)

    async def set(
        self,
        guild_id: int,
        key: str,
        value: str,
        *,
        updated_by: str | None,
        db: Any | None = None,
    ) -> None:
        from repositories.settings import set_runtime_setting, upsert_runtime_setting

        if db is not None:
            await upsert_runtime_setting(
                db, guild_id, key, value, updated_by=updated_by
            )
            return
        await set_runtime_setting(guild_id, key, value, updated_by=updated_by)

    async def delete(self, guild_id: int, key: str, db: Any | None = None) -> None:
        from repositories.settings import (
            delete_runtime_setting,
            delete_runtime_setting_on,
        )

        if db is not None:
            await delete_runtime_setting_on(db, guild_id, key)
            return
        await delete_runtime_setting(guild_id, key)


class SettingsRegistry:
    def __init__(
        self,
        descriptors: Sequence[SettingDescriptor],
        *,
        store: SettingsStore | None = None,
        toml_values: Mapping[str, Any] | None = None,
        on_change: OnChange | None = None,
    ) -> None:
        self._descriptors = {item.key: item for item in descriptors}
        if len(self._descriptors) != len(descriptors):
            raise ValueError("Setting keys must be unique")
        self._store = store if store is not None else DatabaseSettingsStore()
        self._toml = dict(toml_values) if toml_values is not None else None
        self._on_change = on_change
        self._cache: dict[tuple[int, str], str] = {}

    def descriptor(self, key: str) -> SettingDescriptor:
        return self._require(key)

    def descriptors(self, *, group: str | None = None) -> tuple[SettingDescriptor, ...]:
        items = tuple(self._descriptors.values())
        if group is None:
            return items
        return tuple(item for item in items if item.group == group)

    def format_value(self, key: str, value: Any) -> str:
        return _serialize(self._require(key), value)

    def parse(self, key: str, value: Any) -> Any:
        descriptor = self._require(key)
        return self._coerce(descriptor, value)

    async def value_source(self, guild_id: int, key: str) -> str:
        descriptor = self._require(key)
        if descriptor.restart_required:
            return "file" if descriptor.key in self._toml_map() else "default"
        cached = self._cache.get((guild_id, descriptor.key))
        stored = cached if cached is not None else await self._store.get(guild_id, descriptor.key)
        if stored is not None:
            return "database"
        return "file" if descriptor.key in self._toml_map() else "default"

    def clear_cache(self, *, guild_id: int | None = None, key: str | None = None) -> None:
        if guild_id is None and key is None:
            self._cache.clear()
            return
        for cache_key in list(self._cache):
            cached_guild, cached_key = cache_key
            if guild_id is not None and cached_guild != guild_id:
                continue
            if key is not None and cached_key != key:
                continue
            self._cache.pop(cache_key, None)

    async def warmup(self, guild_id: int) -> None:
        rows = await self._store.list(guild_id)
        for key, value in rows.items():
            if key in self._descriptors:
                self._cache[(guild_id, key)] = value

    async def get(self, guild_id: int, key: str) -> Any:
        descriptor = self._require(key)
        raw = await self._raw(guild_id, descriptor)
        return self._coerce(descriptor, raw)

    def peek(self, key: str, *, guild_id: int | None = None) -> Any:
        """Cached or TOML/default value. Used by sync accessors after warmup."""

        descriptor = self._require(key)
        resolved_guild = _guild_id(guild_id)
        cached = self._cache.get((resolved_guild, key))
        if cached is not None:
            return self._coerce(descriptor, cached)
        return self._coerce(descriptor, self._toml_or_default(descriptor))

    async def set(
        self,
        guild_id: int,
        key: str,
        value: Any,
        *,
        updated_by: str,
        source: str = "command",
    ) -> Any:
        descriptor = self._require(key)
        if is_secret_env_name(key):
            raise SettingsError(
                f"Настройка {key} секретная и не хранится в runtime_settings. "
                "Значение не записано, повторять запрос бессмысленно. "
                "Храните секреты в .env. Код: SETTINGS_SECRET.",
                code="SETTINGS_SECRET",
            )
        if descriptor.restart_required:
            raise SettingsError(
                f"Настройка {key} применяется только после перезапуска и не может "
                "быть изменена на работающем боте. Значение не записано. "
                "Измените bot_config.toml или .env и перезапустите процесс. "
                "Код: SETTINGS_RESTART_REQUIRED.",
                code="SETTINGS_RESTART_REQUIRED",
            )
        parsed = self._coerce(descriptor, value)
        serialized = _serialize(descriptor, parsed)
        before = await self._raw(guild_id, descriptor)
        before_text = (
            _serialize(descriptor, self._coerce(descriptor, before))
            if before is not None and before != ""
            else (None if before is None else str(before))
        )
        if isinstance(self._store, DatabaseSettingsStore):
            await self._persist_with_audit(
                guild_id,
                key,
                serialized,
                updated_by=updated_by,
                source=source,
                before=before_text,
            )
        else:
            await self._store.set(guild_id, key, serialized, updated_by=updated_by)
        self._cache[(guild_id, key)] = serialized
        await self._emit(
            SettingChange(
                guild_id=guild_id,
                key=key,
                before=before_text,
                after=serialized,
                updated_by=updated_by,
                source=source,
            )
        )
        return parsed

    async def reset(
        self,
        guild_id: int,
        key: str,
        *,
        updated_by: str,
        source: str = "command",
    ) -> Any:
        descriptor = self._require(key)
        if is_secret_env_name(key):
            raise SettingsError(
                f"Настройка {key} секретная и не хранится в runtime_settings. "
                "Сброс не выполнен. Код: SETTINGS_SECRET.",
                code="SETTINGS_SECRET",
            )
        if descriptor.restart_required:
            raise SettingsError(
                f"Настройка {key} изменяется только в файле и требует перезапуска. "
                "Сброс на работающем боте не выполнен. Код: SETTINGS_RESTART_REQUIRED.",
                code="SETTINGS_RESTART_REQUIRED",
            )
        stored = self._cache.get((guild_id, key))
        if stored is None:
            stored = await self._store.get(guild_id, key)
        fallback = self._toml_or_default(descriptor)
        after = self._coerce(descriptor, fallback)
        after_text = _serialize(descriptor, after)
        if stored is None:
            return after
        if isinstance(self._store, DatabaseSettingsStore):
            await self._persist_reset_with_audit(
                guild_id,
                key,
                updated_by=updated_by,
                source=source,
                before=stored,
                after=after_text,
            )
        else:
            await self._store.delete(guild_id, key)
        self.clear_cache(guild_id=guild_id, key=key)
        await self._emit(
            SettingChange(
                guild_id=guild_id,
                key=key,
                before=stored,
                after=after_text,
                updated_by=updated_by,
                source=source,
            )
        )
        return after

    async def _persist_with_audit(
        self,
        guild_id: int,
        key: str,
        serialized: str,
        *,
        updated_by: str,
        source: str,
        before: str | None,
    ) -> None:
        from infrastructure.database import connection as db_connection
        from infrastructure.transactions import begin_write
        from services.audit import AUDIT_SOURCES, record_decision

        actor_id = int(updated_by) if updated_by.isdigit() else 0
        async with db_connection() as db:
            await begin_write(db, "runtime-setting", f"{guild_id}:{key}")
            await self._store.set(
                guild_id, key, serialized, updated_by=updated_by, db=db
            )
            await record_decision(
                guild_id=guild_id,
                action="SETTINGS.SET",
                subject_type="setting",
                subject_id=key,
                actor_id=actor_id,
                actor_name=updated_by,
                before=before,
                after=serialized,
                source=source if source in AUDIT_SOURCES else "system",
                db=db,
            )
            await db.commit()

    async def _persist_reset_with_audit(
        self,
        guild_id: int,
        key: str,
        *,
        updated_by: str,
        source: str,
        before: str | None,
        after: str | None,
    ) -> None:
        from infrastructure.database import connection as db_connection
        from infrastructure.transactions import begin_write
        from services.audit import AUDIT_SOURCES, record_decision

        actor_id = int(updated_by) if updated_by.isdigit() else 0
        async with db_connection() as db:
            await begin_write(db, "runtime-setting", f"{guild_id}:{key}")
            await self._store.delete(guild_id, key, db=db)
            await record_decision(
                guild_id=guild_id,
                action="SETTINGS.RESET",
                subject_type="setting",
                subject_id=key,
                actor_id=actor_id,
                actor_name=updated_by,
                before=before,
                after=after,
                source=source if source in AUDIT_SOURCES else "system",
                db=db,
            )
            await db.commit()

    async def _raw(self, guild_id: int, descriptor: SettingDescriptor) -> Any:
        if descriptor.restart_required:
            return self._toml_or_default(descriptor)
        cached = self._cache.get((guild_id, descriptor.key))
        if cached is not None:
            return cached
        stored = await self._store.get(guild_id, descriptor.key)
        if stored is not None:
            self._cache[(guild_id, descriptor.key)] = stored
            return stored
        return self._toml_or_default(descriptor)

    def _toml_or_default(self, descriptor: SettingDescriptor) -> Any:
        toml = self._toml_map()
        if descriptor.key in toml:
            return toml[descriptor.key]
        return descriptor.default

    def _toml_map(self) -> Mapping[str, Any]:
        if self._toml is not None:
            return self._toml
        from config import public_settings_map

        return public_settings_map()

    def _require(self, key: str) -> SettingDescriptor:
        descriptor = self._descriptors.get(key)
        if descriptor is None:
            raise SettingsError(
                f"Неизвестная настройка {key}. Значение не прочитано и не записано; "
                "повторять запрос с тем же ключом бессмысленно. "
                "Выберите ключ из реестра настроек. Код: SETTINGS_UNKNOWN_KEY.",
                code="SETTINGS_UNKNOWN_KEY",
            )
        return descriptor

    def _coerce(self, descriptor: SettingDescriptor, value: Any) -> Any:
        try:
            parsed = _parse_value(descriptor.value_type, value, default=descriptor.default)
            if descriptor.validator is not None:
                parsed = descriptor.validator(parsed)
            return parsed
        except SettingsError:
            raise
        except (TypeError, ValueError) as exc:
            raise SettingsError(
                f"Значение настройки {descriptor.key} имеет неверный тип "
                f"({descriptor.value_type}). Значение не применено; "
                "исправьте формат и повторите. "
                f"Код: SETTINGS_INVALID_TYPE. {exc}",
                code="SETTINGS_INVALID_TYPE",
            ) from exc

    async def _emit(self, change: SettingChange) -> None:
        logger.info(
            "Setting changed guild=%s key=%s by=%s before=%s after=%s",
            change.guild_id,
            change.key,
            change.updated_by,
            change.before,
            change.after,
        )
        if self._on_change is None:
            return
        try:
            await self._on_change(change)
        except Exception:
            logger.exception(
                "Settings change recorder failed for key=%s guild=%s",
                change.key,
                change.guild_id,
            )


def _guild_id(guild_id: int | None) -> int:
    if guild_id is not None:
        return guild_id
    from config import GUILD_ID

    if GUILD_ID is None:
        raise SettingsError(
            "GUILD_ID не задан, настройку сервера прочитать нельзя. "
            "Укажите GUILD_ID_TEST или GUILD_ID_MAIN в .env "
            "(и BOT_PROFILE) и перезапустите бота. "
            "Код: SETTINGS_NO_GUILD.",
            code="SETTINGS_NO_GUILD",
        )
    return int(GUILD_ID)


def _parse_value(value_type: str, value: Any, *, default: Any) -> Any:
    if value_type in _SNOWFLAKE_TYPES:
        return _parse_optional_snowflake(value)
    if value_type == "int":
        return _parse_int(value, default=default)
    if value_type == "decimal":
        return _parse_decimal(value, default=default)
    if value_type == "bool":
        return _parse_bool(value, default=default)
    if value_type == "csv_ids":
        return _parse_csv_ids(value)
    if value_type == "string":
        if value is None:
            return "" if default is None else str(default)
        if isinstance(value, (list, tuple)):
            return ",".join(str(item).strip() for item in value if str(item).strip())
        return str(value)
    raise SettingsError(
        f"Тип {value_type} не поддерживается. Код: SETTINGS_INVALID_TYPE.",
        code="SETTINGS_INVALID_TYPE",
    )


def _parse_optional_snowflake(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("snowflake must be a positive integer")
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        parsed = int(text)
    if parsed <= 0:
        raise ValueError("snowflake must be a positive integer")
    return parsed


def _parse_int(value: Any, *, default: Any) -> int | None:
    if value is None or value == "":
        if default is None:
            return None
        if isinstance(default, int) and not isinstance(default, bool):
            return default
        raise ValueError("integer is required")
    if isinstance(value, bool):
        raise ValueError("integer is required")
    if isinstance(value, int):
        return value
    return int(str(value).strip())


def _parse_decimal(value: Any, *, default: Any) -> Decimal:
    if value is None or value == "":
        if isinstance(default, Decimal):
            return default
        if default is None or default == "":
            raise ValueError("decimal is required")
        return Decimal(str(default))
    if isinstance(value, bool):
        raise ValueError("decimal is required")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("decimal is required") from exc


def _parse_bool(value: Any, *, default: Any) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean must be true or false")


def _parse_csv_ids(value: Any) -> tuple[int, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item).strip() for item in value]
    else:
        items = [part.strip() for part in str(value).split(",")]
    result: list[int] = []
    for item in items:
        if not item:
            continue
        parsed = int(item)
        if parsed <= 0:
            raise ValueError("csv_ids must contain only positive integers")
        result.append(parsed)
    if len(result) != len(set(result)):
        raise ValueError("csv_ids must not contain duplicates")
    return tuple(result)


def _serialize(descriptor: SettingDescriptor, value: Any) -> str:
    if value is None:
        return ""
    if descriptor.value_type == "csv_ids":
        return ",".join(str(item) for item in value)
    if descriptor.value_type == "bool":
        return "true" if value else "false"
    if descriptor.value_type == "decimal":
        return format(value, "f")
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(str(item) for item in value)
    return str(value)


def _validate_profile_upgrades(value: Any) -> str:
    from core.profile import ProfileConfigError, _parse_upgrades

    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple, set, frozenset)):
        text = ",".join(str(item).strip() for item in value if str(item).strip())
    else:
        text = "" if value is None else str(value)
    try:
        upgrades = _parse_upgrades(text)
    except ProfileConfigError as exc:
        raise SettingsError(str(exc), code="SETTINGS_INVALID_TYPE") from exc
    return ",".join(sorted(upgrades))


def _validate_member_limit(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SettingsError(
            "Лимит состава должен быть положительным целым числом или пустым. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    parsed = int(value)
    if parsed <= 0:
        raise SettingsError(
            "Лимит состава должен быть положительным целым числом или пустым. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _validate_iso_date(value: Any) -> str:
    text = str(value).strip()
    date.fromisoformat(text)
    return text


def _validate_schedule_days(value: Any) -> str:
    from services.calendar import parse_day_keys

    text = str(value or "").strip()
    try:
        parse_day_keys(text)
    except ValueError as exc:
        raise SettingsError(
            f"{exc} Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        ) from exc
    return text


def _validate_schedule_times(value: Any) -> str:
    from services.calendar import parse_clock_times

    text = str(value or "").strip()
    try:
        parse_clock_times(text)
    except ValueError as exc:
        raise SettingsError(
            f"{exc} Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        ) from exc
    return text


def _validate_schedule_window(value: Any) -> str:
    from services.calendar import parse_clock_window

    text = str(value or "").strip()
    try:
        parse_clock_window(text)
    except ValueError as exc:
        raise SettingsError(
            f"{exc} Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        ) from exc
    return text


def _validate_warehouse_tag_hours(value: Any) -> int:
    if value is None or value == "":
        raise SettingsError(
            "Срок метки склада должен быть положительным целым числом часов. "
            "Пустое значение не записывается. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(
            "Срок метки склада должен быть положительным целым числом часов. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        ) from exc
    if parsed <= 0:
        raise SettingsError(
            "Срок метки склада должен быть положительным целым числом часов. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _validate_onboarding_days(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise SettingsError(
            "Срок адаптации должен быть целым числом дней от 0 до 3650. "
            "0 значит «требует настройки». Значение не записано. "
            "Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    parsed = int(value)
    if parsed < 0 or parsed > 3650:
        raise SettingsError(
            "Срок адаптации должен быть от 0 до 3650 дней. "
            "0 значит «требует настройки». Значение не записано. "
            "Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _validate_report_limit(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise SettingsError(
            "Порог отчёта должен быть целым числом от 0. "
            "0 значит «не задан». Значение не записано. "
            "Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    parsed = int(value)
    if parsed < 0:
        raise SettingsError(
            "Порог отчёта не может быть отрицательным. "
            "0 значит «не задан». Значение не записано. "
            "Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _validate_percent(value: Any) -> int:
    parsed = _validate_report_limit(value)
    if parsed > 100:
        raise SettingsError(
            "Потолок доли — целое от 0 до 100. 0 значит «потолка нет». "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _validate_mat_level(value: Any) -> int:
    parsed = _validate_report_limit(value)
    if parsed > 3:
        raise SettingsError(
            "Уровень матовозки — целое от 0 до 3. 0 значит «нет машины». "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _validate_per_mille(value: Any) -> int:
    parsed = _validate_report_limit(value)
    if parsed > 1000:
        raise SettingsError(
            "Доля в тысячных — целое от 0 до 1000. 50 значит 5%. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _validate_upgrade_max(value: Any) -> int:
    parsed = _validate_report_limit(value)
    if parsed < 1 or parsed > 9:
        raise SettingsError(
            "Максимум уровней улучшения — целое от 1 до 9. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _validate_optional_http_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    lowered = text.casefold()
    if not lowered.startswith("http://") and not lowered.startswith("https://"):
        raise SettingsError(
            "Базовый URL API должен быть пустым или начинаться с http:// или https://. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return text


def _validate_api_timeout_seconds(value: Any) -> int:
    parsed = _validate_report_limit(value)
    if parsed < 1 or parsed > 120:
        raise SettingsError(
            "Таймаут запроса API — целое от 1 до 120 секунд. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _validate_period_kind(value: Any) -> str:
    text = str(value or "week").strip().lower()
    if text in {"week", "rating"}:
        return text
    raise SettingsError(
        "Вид периода — week или rating. Значение не записано. "
        "Код: SETTINGS_INVALID_TYPE.",
        code="SETTINGS_INVALID_TYPE",
    )


def _validate_rank_csv(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = [chunk.strip() for chunk in text.split(",") if chunk.strip()]
    if not parts:
        return ""
    for part in parts:
        try:
            parsed = int(part)
        except ValueError as exc:
            raise SettingsError(
                "Справочник рангов — целые через запятую. Значение не записано. "
                "Код: SETTINGS_INVALID_TYPE.",
                code="SETTINGS_INVALID_TYPE",
            ) from exc
        if parsed < 0:
            raise SettingsError(
                "Пороги и начисления ранга не могут быть отрицательными. "
                "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
                code="SETTINGS_INVALID_TYPE",
            )
    return ",".join(parts)


_QUEUE_KIND_KEYS = frozenset(
    {
        "report",
        "application",
        "appeal",
        "help",
        "promotion",
        "resource",
        "payout",
    }
)
_QUEUE_SPECIALIZATIONS = frozenset(
    {"combat", "logistics", "contracts", "mentoring"}
)


def _validate_queue_kind_map(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts: list[str] = []
    for chunk in text.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if ":" not in piece:
            raise SettingsError(
                "Соответствие типа очереди пишется как тип:специализация. "
                "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
                code="SETTINGS_INVALID_TYPE",
            )
        kind, spec = piece.split(":", 1)
        kind = kind.strip()
        spec = spec.strip()
        if kind not in _QUEUE_KIND_KEYS:
            raise SettingsError(
                f"Неизвестный тип очереди {kind!r}. "
                "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
                code="SETTINGS_INVALID_TYPE",
            )
        if spec and spec not in _QUEUE_SPECIALIZATIONS:
            raise SettingsError(
                f"Неизвестная специализация {spec!r}. "
                "Допустимы combat, logistics, contracts, mentoring. "
                "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
                code="SETTINGS_INVALID_TYPE",
            )
        parts.append(f"{kind}:{spec}" if spec else kind)
    return ",".join(parts)


def _validate_watch_policy(value: Any) -> str:
    text = str(value).strip().casefold()
    if text not in {"skip", "silent"}:
        raise SettingsError(
            "Политика уведомления о выходе: skip или silent. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return text


def _validate_surname_policy(value: Any) -> str:
    text = str(value).strip().casefold()
    if text not in {"off", "ask", "required"}:
        raise SettingsError(
            "Политика фамилии: off, ask или required. "
            "Это правило Guinness, не Majestic. Значение не записано. "
            "Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return text


def _validate_audit_retention_days(value: Any) -> int:
    if value is None or value == "":
        return 365
    if isinstance(value, bool):
        raise SettingsError(
            "Срок хранения журнала должен быть целым числом дней от 0 до 3650. "
            "Значение не записано. Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    parsed = int(value)
    if parsed < 0 or parsed > 3650:
        raise SettingsError(
            "Срок хранения журнала должен быть от 0 до 3650 дней. "
            "0 отключает очистку. Значение не записано. "
            "Код: SETTINGS_INVALID_TYPE.",
            code="SETTINGS_INVALID_TYPE",
        )
    return parsed


def _d(
    key: str,
    group: str,
    value_type: str,
    default: Any,
    title: str,
    description: str,
    *,
    restart_required: bool = False,
    validator: Callable[[Any], Any] | None = None,
) -> SettingDescriptor:
    return SettingDescriptor(
        key=key,
        group=group,
        value_type=value_type,
        default=default,
        title=title,
        description=description,
        restart_required=restart_required,
        validator=validator,
    )


def build_descriptors() -> tuple[SettingDescriptor, ...]:
    from config import CATEGORY_ID_ENV, CHANNEL_ID_ENV, ROLE_ID_ENV

    items: list[SettingDescriptor] = [
        _d(
            "TOKEN",
            "access",
            "string",
            "",
            "Токен бота",
            "Секрет Discord. Только .env, требуется перезапуск.",
            restart_required=True,
        ),
        _d(
            "DATABASE_URL",
            "access",
            "string",
            "",
            "URL базы данных",
            "Секрет подключения. Только .env, требуется перезапуск.",
            restart_required=True,
        ),
        _d(
            "GUILD_ID",
            "access",
            "string",
            "",
            "ID Discord-сервера",
            "Единственная разрешённая гильдия. Только .env "
            "(GUILD_ID_TEST / GUILD_ID_MAIN + BOT_PROFILE). Требуется перезапуск.",
            restart_required=True,
        ),
        _d(
            "BOT_OWNER_ID",
            "access",
            "string",
            "",
            "ID владельца бота",
            "Аварийный доступ владельца. Только .env, требуется перезапуск.",
            restart_required=True,
        ),
            _d(
                "DB_POOL_SIZE",
                "limits",
                "int",
                4,
                "Размер пула PostgreSQL",
                "Число соединений. Требуется перезапуск.",
                restart_required=True,
            ),
            _d(
                "AUDIT_RETENTION_DAYS",
                "limits",
                "int",
                365,
                "Срок хранения журнала решений",
                "Дни. 0 отключает автоматическую очистку.",
                validator=_validate_audit_retention_days,
            ),
            _d(
                "ONBOARDING_PROBATION_DAYS",
                "onboarding",
                "int",
                0,
                "Длительность испытательного срока",
                "Дни. 0 — срок не задан и не выдумывается.",
                validator=_validate_onboarding_days,
            ),
            _d(
                "ONBOARDING_RETENTION_DAYS",
                "onboarding",
                "int",
                0,
                "Период удержания после принятия",
                "Дни наблюдения воронки рекрутинга. 0 — не задан.",
                validator=_validate_onboarding_days,
            ),
            _d(
                "FAMILY_SURNAME_POLICY",
                "onboarding",
                "string",
                "ask",
                "Политика смены фамилии",
                "Правило Guinness: off, ask или required. Не правило Majestic.",
                validator=_validate_surname_policy,
            ),
            _d(
                "ROSTER_WATCH_VISITOR_POLICY",
                "roster",
                "string",
                "skip",
                "Выход гостя или друга",
                "skip — не создавать карточку; silent — карточка без пинга.",
                validator=_validate_watch_policy,
            ),
            _d(
                "ROSTER_WATCH_BOT_POLICY",
                "roster",
                "string",
                "skip",
                "Выход бота",
                "skip — не создавать карточку; silent — карточка без пинга.",
                validator=_validate_watch_policy,
            ),
            _d(
                "ROSTER_WATCH_PING_ROLE_IDS",
                "roster",
                "csv_ids",
                (),
                "Роли пинга при выходе из состава",
                "Пусто — взять роли пинга заявок. Пинг только при игровой роли.",
            ),
            _d(
                "ROSTER_WATCH_PING_ROLES",
                "roster",
                "string",
                "",
                "Имена ролей пинга при выходе",
                "Запасные имена, если ID не заданы.",
            ),
            _d(
                "REPORT_DETAIL_THRESHOLD",
                "reports",
                "int",
                0,
                "Порог подробного подтверждения",
                "Если результат не меньше порога — нужны ссылки или вложения. "
                "0 — порог не задан, подробности нужны при выплате.",
                validator=_validate_report_limit,
            ),
            _d(
                "REPORT_EVIDENCE_MAX_BYTES",
                "reports",
                "int",
                0,
                "Максимальный размер вложения отчёта",
                "Байты. 0 — лимит не задан старшим составом.",
                validator=_validate_report_limit,
            ),
            _d(
                "REPORT_EVIDENCE_RETENTION_DAYS",
                "reports",
                "int",
                0,
                "Срок хранения вложений отчёта",
                "Дни. 0 — срок не задан, автоудаления нет.",
                validator=_validate_onboarding_days,
            ),
            _d(
                "PROFIT_PURCHASE_LOOKBACK",
                "reports",
                "int",
                0,
                "Сколько принятых отчётов брать для средней закупки",
                "0 — все принятые отчёты этого контракта, окно не выдумывается.",
                validator=_validate_report_limit,
            ),
            _d(
                "QUEUE_SLA_MINUTES",
                "queue",
                "int",
                0,
                "Срок ответа по очереди, минуты",
                "0 — срок не задан, автоэскалации нет. Число не выдумывается.",
                validator=_validate_report_limit,
            ),
            _d(
                "QUEUE_DEPUTY_ROLE_IDS",
                "queue",
                "csv_ids",
                (),
                "Роли заместителей для эскалации",
                "Пусто — заместители не заданы, человека бот не выдумывает.",
            ),
            _d(
                "QUEUE_KIND_SPECIALIZATION",
                "queue",
                "string",
                "report:contracts",
                "Тип очереди → специализация",
                "CSV вид:тип. Пустая специализация — любой. "
                "Ключи специализации только из пункта 4.4.",
                validator=_validate_queue_kind_map,
            ),
            _d(
                "TREASURY_BATCH_THRESHOLD",
                "treasury",
                "int",
                0,
                "Порог пакетного согласования выплат",
                "Игровые деньги. 0 — пакет выключен, второе решение не требуется. "
                "Число не выдумывается.",
                validator=_validate_report_limit,
            ),
            _d(
                "CONTRIBUTION_CONTRACT",
                "contribution",
                "int",
                0,
                "Заслуги за принятый отчёт",
                "0 — правило не задано, начисления нет. Не Dark Coins.",
                validator=_validate_report_limit,
            ),
            _d(
                "CONTRIBUTION_ATTENDANCE",
                "contribution",
                "int",
                0,
                "Заслуги за явку, подтверждённую командиром",
                "0 — правило не задано. Голос и сообщения не считаются.",
                validator=_validate_report_limit,
            ),
            _d(
                "CONTRIBUTION_MENTORING",
                "contribution",
                "int",
                0,
                "Заслуги наставника за закрытое обучение новичка",
                "0 — правило не задано. Категория отдельно от контракта и явки.",
                validator=_validate_report_limit,
            ),
            _d(
                "CONTRIBUTION_WAREHOUSE",
                "contribution",
                "int",
                0,
                "Заслуги за сдачу на склад",
                "0 — правило не задано. Подтверждение — поступление на склад организации.",
                validator=_validate_report_limit,
            ),
            _d(
                "CONTRIBUTION_LOGISTICS",
                "contribution",
                "int",
                0,
                "Заслуги за выполненную логистику",
                "0 — правило не задано. Подтверждение — выдача комплекта.",
                validator=_validate_report_limit,
            ),
            _d(
                "CONTRIBUTION_DUTY",
                "contribution",
                "int",
                0,
                "Заслуги за дежурство",
                "0 — правило не задано. Подтверждение дежурства появится позже.",
                validator=_validate_report_limit,
            ),
            _d(
                "BONUS_PERIOD_KIND",
                "bonus",
                "string",
                "week",
                "Вид премиального периода",
                "week — ISO-неделя; rating — период рейтинга с границами, "
                "которые задаёт лидер.",
                validator=_validate_period_kind,
            ),
            _d(
                "BONUS_MIN_POINTS",
                "bonus",
                "int",
                0,
                "Минимальные заслуги для доли фонда",
                "0 — порога нет. Число не выдумывается.",
                validator=_validate_report_limit,
            ),
            _d(
                "BONUS_MAX_SHARE_PERCENT",
                "bonus",
                "int",
                0,
                "Потолок доли одного человека",
                "Проценты от строки фонда. 0 — потолка нет.",
                validator=_validate_percent,
            ),
            _d(
                "BONUS_ORG_INFLUENCE",
                "bonus",
                "int",
                0,
                "Подтверждённое влияние организации",
                "0 — не подтверждено, ранг не считается. Не выдумывается.",
                validator=_validate_report_limit,
            ),
            _d(
                "BONUS_RANK_THRESHOLDS",
                "bonus",
                "string",
                "500,1300,2600,4200,6300,8700,11500",
                "Пороги ранга по влиянию",
                "Целые через запятую. Пусто — таблица сезона из плана. "
                "Пороги менялись между сезонами.",
                validator=_validate_rank_csv,
            ),
            _d(
                "BONUS_RANK_NAMES",
                "bonus",
                "string",
                "Чужаки,Местные,Авангард,Синдикат,Картель,Империя,Орден",
                "Названия рангов по влиянию",
                "Через запятую, в том же порядке, что пороги.",
            ),
            _d(
                "BONUS_RANK_MULTIPLIER_MILLI",
                "bonus",
                "string",
                "1100,1200,1350,1500,1650,1800,2000",
                "Множители Dark Coins в тысячных",
                "1100 значит ×1,1. Целые через запятую, без float.",
                validator=_validate_rank_csv,
            ),
            _d(
                "BONUS_RANK_DC",
                "bonus",
                "string",
                "100,250,450,750,1100,1500,2000",
                "Справочник разового DC за ранг",
                "Сервер считает итог сам. Семья число не обещает.",
                validator=_validate_rank_csv,
            ),
            _d(
                "INVENTORY_WAREHOUSE_SLOTS",
                "inventory",
                "int",
                20,
                "Складские места организации",
                "Подтверждено: 20 мест, один склад на семью.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_SAFE_MONEY",
                "inventory",
                "int",
                10_000_000,
                "Сейф офиса",
                "Подтверждено: до 10 000 000.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_GUN_CABINET_KG",
                "inventory",
                "int",
                1000,
                "Оружейный шкаф, кг",
                "Подтверждено: 1000 кг.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_PANTRY_KG",
                "inventory",
                "int",
                100,
                "Кладовая, кг",
                "Подтверждено: 100 кг.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_GARAGE_EXTRA_MAX",
                "inventory",
                "int",
                30,
                "Максимум дополнительных мест гаража",
                "Подтверждено: до 30. Учёт машин — отдельный пункт.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_MAT_L1_KG",
                "inventory",
                "int",
                1000,
                "Bobcat XL, кг",
                "Подтверждено: 1000 кг.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_MAT_L2_KG",
                "inventory",
                "int",
                2000,
                "Speedo, кг",
                "Подтверждено: 2000 кг.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_MAT_L3_KG",
                "inventory",
                "int",
                4000,
                "Guardian, кг",
                "Подтверждено: 4000 кг.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_MAT_LEVEL",
                "inventory",
                "int",
                0,
                "Текущий уровень матовозки",
                "0 — машины нет / не подтверждена. 1–3 — Bobcat XL, Speedo, Guardian.",
                validator=_validate_mat_level,
            ),
            _d(
                "INVENTORY_LOW_STOCK",
                "inventory",
                "int",
                0,
                "Порог запаса предмета по умолчанию",
                "0 — оповещение по предмету не задано. Ёмкости мест — отдельные ключи.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_RECONCILE_STALE_HOURS",
                "inventory",
                "int",
                0,
                "Срок устаревания сверки, часы",
                "0 — оповещение о сверке выключено. Интервал не выдуман.",
                validator=_validate_report_limit,
            ),
            _d(
                "INVENTORY_SALE_PERCENT",
                "inventory",
                "int",
                75,
                "Доля при продаже склада государству",
                "Подтверждено: 75%. Только текст предупреждения, бот цену не считает.",
                validator=_validate_percent,
            ),
            _d(
                "PROGRESSION_SEASON_MEMBERSHIP_DAYS",
                "progression",
                "int",
                28,
                "Дней в организации до конца сезона",
                "Условие сезонной награды. Между сезонами задаётся настройкой, не константой в логике.",
                validator=_validate_report_limit,
            ),
            _d(
                "PROGRESSION_SEASON_PLAY_HOURS",
                "progression",
                "int",
                30,
                "Часов игры за сезон",
                "Порог сезонной награды. Часы вводит человек; голос не считается.",
                validator=_validate_report_limit,
            ),
            _d(
                "PROGRESSION_DIRECTION_COOLDOWN_DAYS",
                "progression",
                "int",
                7,
                "Перерыв после смены направления, дни",
                "Подтверждено планом: 7 дней.",
                validator=_validate_report_limit,
            ),
            _d(
                "PROGRESSION_REP_LOSS_PER_MILLE",
                "progression",
                "int",
                50,
                "Потеря репутации при смене направления, тысячные",
                "50 = 5%. Хранится без float.",
                validator=_validate_per_mille,
            ),
            _d(
                "PROGRESSION_MAX_LEVEL",
                "progression",
                "int",
                9,
                "Максимум уровней улучшения",
                "Подтверждено: 9 уровней.",
                validator=_validate_upgrade_max,
            ),
            _d(
                "EXTERNAL_API_BASE_URL",
                "external",
                "string",
                "",
                "Базовый URL API Majestic",
                "Пусто — интеграция выключена. Не угадывается в коде.",
                validator=_validate_optional_http_url,
            ),
            _d(
                "EXTERNAL_API_VERSION",
                "external",
                "string",
                "",
                "Версия API Majestic",
                "Пусто — интеграция выключена. Не угадывается в коде.",
            ),
            _d(
                "EXTERNAL_API_SERVER_ID",
                "external",
                "string",
                "",
                "serverId Memphis в API",
                "Wiki адресует сервер как RU19 — совпадение проверяет первый запрос. "
                "Пустое значение не подменяется профилем.",
            ),
            _d(
                "EXTERNAL_API_RATE_LIMIT",
                "external",
                "int",
                0,
                "Лимит запросов N",
                "Документация печатает N/M как неизвестные. 0 — лимит не подтверждён, "
                "интеграция выключена.",
                validator=_validate_report_limit,
            ),
            _d(
                "EXTERNAL_API_RATE_WINDOW_SECONDS",
                "external",
                "int",
                0,
                "Окно лимита запросов, секунды",
                "M из «N запросов / M секунд». 0 — окно не подтверждено, интеграция выключена.",
                validator=_validate_report_limit,
            ),
            _d(
                "EXTERNAL_API_ATTRIBUTION",
                "external",
                "string",
                "",
                "Формат подписи источника",
                "Требуемый текст ToS. Пусто — формат не подтверждён, интеграция выключена.",
            ),
            _d(
                "EXTERNAL_KEY_OWNER",
                "external",
                "string",
                "",
                "Ответственный за ключ API",
                "Ключ выпускается на человека, не на организацию. Кто держит ключ.",
            ),
            _d(
                "EXTERNAL_API_TIMEOUT_SECONDS",
                "external",
                "int",
                10,
                "Таймаут запроса API, секунды",
                "Инфраструктурный предел ожидания ответа, не игровое значение.",
                validator=_validate_api_timeout_seconds,
            ),
            _d(
                "MAJESTIC_API_KEY",
                "external",
                "string",
                "",
                "Ключ API Majestic",
                "Секрет. Только .env, заголовок x-api-key. В runtime_settings не хранится.",
                restart_required=True,
            ),
        _d(
            "DB_PATH",
            "access",
            "string",
            "guinness_bot.db",
            "Путь к SQLite",
            "Только для изолированных тестов. Требуется перезапуск.",
            restart_required=True,
        ),
        _d(
            "SHOWDOWN_STATE_PATH",
            "access",
            "string",
            "showdown_state.json",
            "Путь к состоянию Showdown",
            "Файл состояния закрытого тестирования. Требуется перезапуск.",
            restart_required=True,
        ),
        _d(
            "FFMPEG_EXECUTABLE",
            "access",
            "string",
            "ffmpeg",
            "Исполняемый файл ffmpeg",
            "Путь к ffmpeg для музыки. Требуется перезапуск.",
            restart_required=True,
        ),
        _d(
            "LOG_DIR",
            "access",
            "string",
            "",
            "Каталог файловых логов",
            "Пусто — только stdout. На сервере /var/lib/guinnes-bot/logs. "
            "Требуется перезапуск.",
            restart_required=True,
        ),
        _d(
            "LOG_FORMAT",
            "access",
            "string",
            "text",
            "Формат лога",
            "text или json. Требуется перезапуск.",
            restart_required=True,
        ),
        _d(
            "LOG_LEVEL",
            "access",
            "string",
            "INFO",
            "Уровень корневого логгера",
            "Стандартный уровень logging. Требуется перезапуск.",
            restart_required=True,
        ),
        _d(
            "LOG_LOGGER_LEVELS",
            "access",
            "string",
            "discord.gateway=WARNING,discord.client=WARNING",
            "Уровни отдельных логгеров",
            "name=LEVEL через запятую. gateway и client не ниже WARNING.",
            restart_required=True,
        ),
    ]

    channel_titles = {
        "help": "Канал справки",
        "help_target": "Целевой канал справки",
        "request_ss": "Канал обращений",
        "vzp": "Канал боевых заявок",
        "vzp_invite": "Канал заявок боевого состава",
        "family_invite": "Канал заявок в семью",
        "family_invite_logs": "Журнал заявок в семью",
        "add_birthday": "Канал добавления дня рождения",
        "birthday_request": "Канал заявок на день рождения",
        "add_roles": "Канал ролей",
        "happy_birthday": "Канал поздравлений",
        "role_request": "Канал заявок на роли",
        "request_actions": "Журнал решений по заявкам",
        "anonymous_logs": "Журнал анонимных обращений",
        "anonymous_request": "Канал анонимных обращений",
        "treasury": "Канал казны",
        "transactions": "Канал транзакций",
        "car_list": "Канал автопарка",
        "car_bookings": "Журнал автопарка",
        "weekly_stats": "Канал статистики",
        "shop": "Канал магазина",
        "family_features": "Канал семейных панелей",
        "role_approval": "Канал согласования ролей",
        "summit_vzp": "Канал боевых сборов",
        "summit_list": "Канал общих сборов",
        "summit_contract": "Канал контрактов",
        "giveaways": "Канал розыгрышей",
        "recruiting": "Канал рекрутинга",
        "recruiting_logs": "Журнал рекрутинга",
        "recruiting_invite": "Канал приглашений рекрутинга",
        "roster_watch": "Канал уведомлений о составе",
        "voice_create_lobby": "Канал создания голосовых комнат",
        "insights_rating": "Канал рейтинга Memphis",
        "insights_battles": "Канал разборов каптов и войн семей",
        "insights_market": "Канал рынка Memphis",
        "insights_property": "Канал ПСН и особняков",
    }
    for logical, setting_key in CHANNEL_ID_ENV.items():
        title = channel_titles.get(logical, logical)
        items.append(
            _d(
                setting_key,
                "discord.channels",
                "channel_id",
                None,
                f"ID: {title}",
                "Discord ID канала. Пусто — искать по имени.",
            )
        )
        name_key = setting_key.removesuffix("_ID")
        if name_key != setting_key:
            items.append(
                _d(
                    name_key,
                    "discord.channels",
                    "string",
                    "",
                    f"Имя: {title}",
                    "Запасное имя канала, если ID не задан или объект по ID не найден.",
                )
            )

    items.append(
        _d(
            "AFK_VOICE_CHANNEL_ID",
            "discord.channels",
            "channel_id",
            None,
            "ID AFK-канала",
            "Discord ID голосового канала ожидания.",
        )
    )
    items.append(
        _d(
            "AFK_VOICE_CHANNEL",
            "discord.channels",
            "string",
            "",
            "Имя AFK-канала",
            "Запасное имя голосового канала ожидания.",
        )
    )

    category_titles = {
        "tickets_category": "Категория тикетов",
        "birthday_fundraiser_category": "Категория сборов на день рождения",
        "voice_create_category": "Категория голосовых комнат",
        "high_tools_category": "Категория инструментов СС",
        "public_category": "Публичная категория",
        "family_category": "Семейная категория",
        "interactive_category": "Интерактивная категория",
        "groups_category": "Категория рабочих групп",
        "developer_category": "Категория разработчика",
    }
    category_name_keys = {
        "tickets_category": "CHANNEL_TICKETS_CATEGORY",
        "birthday_fundraiser_category": "CATEGORY_BIRTHDAY_FUNDRAISER",
        "voice_create_category": "CATEGORY_CREATE_VOICE",
        "high_tools_category": "CATEGORY_HIGH_TOOLS",
        "public_category": "CATEGORY_PUBLIC",
        "family_category": "CATEGORY_FAMILY",
        "interactive_category": "CATEGORY_INTERACTIVE",
        "groups_category": "CATEGORY_GROUPS",
        "developer_category": "CATEGORY_DEVELOPER",
    }
    for logical, setting_key in CATEGORY_ID_ENV.items():
        title = category_titles.get(logical, logical)
        items.append(
            _d(
                setting_key,
                "discord.categories",
                "category_id",
                None,
                f"ID: {title}",
                "Discord ID категории. Пусто — искать по имени.",
            )
        )
        category_name = category_name_keys.get(logical)
        if category_name is not None:
            items.append(
                _d(
                    category_name,
                    "discord.categories",
                    "string",
                    "",
                    f"Имя: {title}",
                    "Запасное имя категории.",
                )
            )

    role_titles = {
        "staff": "Роль старшего состава",
        "high": "Роль High",
        "owner": "Роль Owner",
        "dep_owner": "Роль Dep. Owner",
        "vzp_team": "Роль боевой команды",
        "guest": "Роль гостя",
        "young": "Роль Young",
        "main": "Роль Main",
        "friend": "Роль Friend",
        "super_friend": "Роль Super Friend",
        "help": "Роль Help",
        "contract": "Роль Contract",
        "lead_vzp": "Роль командира боевого направления",
        "vip": "Роль VIP",
        "vzp_1": "Боевой приоритет 1",
        "vzp_2": "Боевой приоритет 2",
        "vzp_3": "Боевой приоритет 3",
    }
    for logical, setting_key in ROLE_ID_ENV.items():
        title = role_titles.get(logical, logical)
        items.append(
            _d(
                setting_key,
                "discord.roles",
                "role_id",
                None,
                f"ID: {title}",
                "Discord ID роли. Пусто — искать по имени.",
            )
        )
        name_key = setting_key.removesuffix("_ID")
        if name_key.endswith("_ROLE"):
            items.append(
                _d(
                    name_key,
                    "discord.roles",
                    "string",
                    "",
                    f"Имя: {title}",
                    "Запасное имя роли.",
                )
            )

    access_strings = {
        "REQUEST_PING_ROLES": ("Роли пинга по заявке", "Имена ролей через запятую."),
        "HIGH_TOOLS_ALLOWED_ROLES": (
            "Роли доступа к инструментам СС",
            "Имена ролей через запятую.",
        ),
        "TREASURY_OWNER_ROLE": ("Роль владельца казны", "Имя роли."),
        "POLL_ALLOWED_ROLES": ("Роли создания опросов", "Имена ролей через запятую."),
        "RECRUITING_ROLES": ("Роли рекрутеров", "Имена ролей через запятую."),
        "TICKET_ASSIGNMENT_ROLE_HIERARCHY": (
            "Иерархия назначения тикетов",
            "Имена ролей от высшей к низшей.",
        ),
        "ROLE_REJECTION_REASONS": (
            "Причины отказа в роли",
            "Варианты через вертикальную черту.",
        ),
    }
    access_ids = {
        "REQUEST_PING_ROLE_IDS": "ID ролей пинга по заявке",
        "HIGH_TOOLS_ALLOWED_ROLE_IDS": "ID ролей доступа к инструментам СС",
        "POLL_ALLOWED_ROLE_IDS": "ID ролей создания опросов",
        "RECRUITING_ROLE_IDS": "ID ролей рекрутеров",
        "TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS": "ID иерархии назначения тикетов",
    }
    for key, (title, description) in access_strings.items():
        items.append(_d(key, "access", "string", "", title, description))
    for key, title in access_ids.items():
        items.append(
            _d(key, "access", "csv_ids", (), title, "Discord ID через запятую.")
        )
    items.append(
        _d(
            "TREASURY_OWNER_ROLE_ID",
            "access",
            "role_id",
            None,
            "ID роли владельца казны",
            "Discord ID роли владельца казны.",
        )
    )

    economy_ints = {
        "COINS_DAILY": ("Ежедневные Coins", "Награда /coins daily."),
        "COINS_COLOR_ROLE": ("Цена цветной роли", "Стоимость в Coins."),
        "COINS_CUSTOM_ROLE": ("Цена своей роли", "Стоимость в Coins."),
        "COINS_VIP_WEEK": ("Цена VIP на неделю", "Стоимость в Coins."),
        "COINS_VIP_MONTH": ("Цена VIP на месяц", "Стоимость в Coins."),
        "COINS_SUMMIT_EMOJI": ("Цена эмодзи сбора", "Стоимость в Coins."),
        "COINS_MESSAGE_COOLDOWN_SECONDS": (
            "Пауза награды за сообщение",
            "Секунды между начислениями.",
        ),
        "COINS_MESSAGE_MIN_LENGTH": (
            "Минимальная длина сообщения",
            "Короче — без награды.",
        ),
        "COINS_MESSAGE_DAILY_CAP": ("Дневной потолок за сообщения", "Coins в сутки."),
        "COINS_VOICE_DAILY_CAP": ("Дневной потолок за голос", "Coins в сутки."),
        "COINS_VOICE_MIN_ACTIVE_MEMBERS": (
            "Минимум участников в голосе",
            "Ниже порога награда не идёт.",
        ),
    }
    for key, (title, description) in economy_ints.items():
        items.append(_d(key, "economy", "int", 0, title, description))
    items.append(
        _d(
            "COINS_MESSAGE_REWARD",
            "economy",
            "decimal",
            Decimal("5"),
            "Награда за сообщение",
            "Coins за подходящее сообщение.",
        )
    )
    items.append(
        _d(
            "COINS_VOICE_REWARD_PER_MINUTE",
            "economy",
            "decimal",
            Decimal("0.5"),
            "Награда за минуту голоса",
            "Coins за минуту в голосовом канале.",
        )
    )
    items.append(
        _d(
            "COINS_MESSAGE_EXCLUDED_CHANNEL_IDS",
            "economy",
            "csv_ids",
            (),
            "Каналы без награды за сообщения",
            "Discord ID через запятую.",
        )
    )

    cooldown_keys = {
        "HELP_REQUEST_COOLDOWN_SECONDS": "Пауза обращения за помощью",
        "HELP_COMMAND_COOLDOWN_SECONDS": "Пауза команды справки",
        "SUMMIT_LIST_COOLDOWN_SECONDS": "Пауза общего сбора",
        "SUMMIT_VZP_COOLDOWN_SECONDS": "Пауза создания события",
        "SUMMIT_CONTRACT_COOLDOWN_SECONDS": "Пауза запуска контракта",
    }
    for key, title in cooldown_keys.items():
        items.append(
            _d(key, "cooldowns", "int", 0, title, "Секунды. 0 отключает паузу.")
        )

    items.extend(
        (
            _d(
                "server_key",
                "profile",
                "string",
                "memphis",
                "Ключ сервера",
                "memphis — активный профиль, milton — архивный.",
            ),
            _d(
                "family_name",
                "profile",
                "string",
                "Guinness",
                "Название семьи",
                "Отображаемое имя организации.",
            ),
            _d(
                "family_tag",
                "profile",
                "string",
                "",
                "Тег семьи",
                "Игровой тег. Пусто — ещё не задан.",
            ),
            _d(
                "direction",
                "profile",
                "string",
                "unset",
                "Направление семьи",
                "legal, criminal или unset.",
            ),
            _d(
                "timezone",
                "profile",
                "string",
                "Europe/Moscow",
                "Часовой пояс",
                "Идентификатор IANA.",
            ),
            _d(
                "season_key",
                "profile",
                "string",
                "season-2",
                "Ключ сезона",
                "Настраиваемый идентификатор сезона рейтинга.",
            ),
            _d(
                "season_start",
                "profile",
                "string",
                "",
                "Начало сезона",
                "Дата ГГГГ-ММ-ДД. Не зашивается в код.",
                validator=_validate_iso_date,
            ),
            _d(
                "season_end",
                "profile",
                "string",
                "",
                "Конец сезона",
                "Дата ГГГГ-ММ-ДД. Управляет сезонным правом.",
                validator=_validate_iso_date,
            ),
            _d(
                "online_source",
                "profile",
                "string",
                "none",
                "Источник онлайна",
                "none или majestic_api.",
            ),
            _d(
                "enabled_upgrades",
                "profile",
                "string",
                "",
                "Открытые улучшения",
                "Идентификаторы через запятую. Неизвестный — ошибка.",
                validator=_validate_profile_upgrades,
            ),
            _d(
                "member_limit",
                "profile",
                "int",
                None,
                "Лимит состава",
                "Игровой предел. Пусто — требует настройки, 150 не подставлять.",
                validator=_validate_member_limit,
            ),
            _d(
                "allow_archived",
                "profile",
                "bool",
                False,
                "Разрешить архивный профиль",
                "Нужно, чтобы сделать milton активным.",
            ),
            _d(
                "external_server_id",
                "profile",
                "string",
                "",
                "Внешний ID сервера",
                "Для Memphis в рейтинге — RU19. Пусто — значение реестра.",
            ),
            _d(
                "schedule_org_days",
                "calendar",
                "string",
                "mon,wed,fri,sat",
                "Игровые дни организаций",
                "Дни каптов. Прайм предлагается только из этого списка.",
                validator=_validate_schedule_days,
            ),
            _d(
                "schedule_rest_days",
                "calendar",
                "string",
                "tue,thu,sun",
                "Выходные дни организаций",
                "Дни фракций. Для обучения, наставничества, инвентаризации и разборов.",
                validator=_validate_schedule_days,
            ),
            _d(
                "schedule_air_cargo",
                "calendar",
                "string",
                "00:00,04:00,08:00,12:00,16:00,20:00",
                "Слоты ВЗА",
                "Местное время профиля, каждые 4 часа.",
                validator=_validate_schedule_times,
            ),
            _d(
                "schedule_dealers",
                "calendar",
                "string",
                "10:30,18:30",
                "Вербовка дилеров",
                "Местное время. Вечерний слот конфликтует с контейнерами.",
                validator=_validate_schedule_times,
            ),
            _d(
                "schedule_workshops",
                "calendar",
                "string",
                "14:30,22:45",
                "Захват цехов",
                "Местное время профиля.",
                validator=_validate_schedule_times,
            ),
            _d(
                "schedule_influence_window",
                "calendar",
                "string",
                "12:00-23:00",
                "Окно влияния за криминальные действия",
                "Интервал ЧЧ:ММ–ЧЧ:ММ в поясе профиля.",
                validator=_validate_schedule_window,
            ),
            _d(
                "schedule_hold_influence",
                "calendar",
                "string",
                "12:00",
                "Влияние за удержание квадратов",
                "Одно местное время ЧЧ:ММ.",
                validator=_validate_schedule_times,
            ),
            _d(
                "schedule_lottery_days",
                "calendar",
                "string",
                "mon,wed,fri,sun",
                "Дни лотереи контейнеров",
                "В эти дни в schedule_lottery_time идёт лотерея.",
                validator=_validate_schedule_days,
            ),
            _d(
                "schedule_lottery_time",
                "calendar",
                "string",
                "18:00",
                "Время лотереи контейнеров",
                "Местное время. Конфликт с вечерней вербовкой дилеров.",
                validator=_validate_schedule_times,
            ),
            _d(
                "schedule_auction_days",
                "calendar",
                "string",
                "tue,thu,sat",
                "Дни аукциона контейнеров",
                "В эти дни в schedule_auction_time идёт аукцион.",
                validator=_validate_schedule_days,
            ),
            _d(
                "schedule_auction_time",
                "calendar",
                "string",
                "18:00",
                "Время аукциона контейнеров",
                "Местное время. Конфликт с вечерней вербовкой дилеров.",
                validator=_validate_schedule_times,
            ),
            _d(
                "schedule_warehouse_tag_hours",
                "calendar",
                "int",
                6,
                "Часы метки склада после дилеров",
                "Подтверждённый срок wiki. Не дублирует напоминания F2 о старте.",
                validator=_validate_warehouse_tag_hours,
            ),
        )
    )
    return tuple(items)


_REGISTRY: SettingsRegistry | None = None


def get_registry() -> SettingsRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SettingsRegistry(build_descriptors())
    return _REGISTRY


def set_decision_recorder(callback: OnChange | None) -> None:
    """Extra listener after a settings write. Persistence uses record_decision."""

    get_registry()._on_change = callback


async def warmup_runtime_settings(guild_id: int) -> None:
    await get_registry().warmup(guild_id)


def runtime_channel_id(name: str, *, guild_id: int | None = None) -> int | None:
    from config import CHANNEL_ID_ENV

    key = CHANNEL_ID_ENV.get(name, name)
    value = get_registry().peek(key, guild_id=guild_id)
    return value if value is None or isinstance(value, int) else None


def runtime_category_id(name: str, *, guild_id: int | None = None) -> int | None:
    from config import CATEGORY_ID_ENV

    key = CATEGORY_ID_ENV.get(name, name)
    value = get_registry().peek(key, guild_id=guild_id)
    return value if value is None or isinstance(value, int) else None


def runtime_role_id(name: str, *, guild_id: int | None = None) -> int | None:
    from config import ROLE_ID_ENV

    key = ROLE_ID_ENV.get(name, name)
    value = get_registry().peek(key, guild_id=guild_id)
    return value if value is None or isinstance(value, int) else None


def runtime_role_ids(name: str, *, guild_id: int | None = None) -> tuple[int, ...]:
    key = _ROLE_ID_LIST_ALIASES.get(name, name)
    value = get_registry().peek(key, guild_id=guild_id)
    if isinstance(value, tuple):
        return value
    return ()


def runtime_setting_int(name: str, *, guild_id: int | None = None) -> int:
    value = get_registry().peek(name, guild_id=guild_id)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is None:
        descriptor = get_registry().descriptor(name)
        if isinstance(descriptor.default, int) and not isinstance(
            descriptor.default, bool
        ):
            return descriptor.default
    raise SettingsError(
        f"Настройка {name} должна быть целым числом. "
        "Исправьте значение и повторите. Код: SETTINGS_INVALID_TYPE.",
        code="SETTINGS_INVALID_TYPE",
    )
