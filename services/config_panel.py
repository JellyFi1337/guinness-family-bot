"""Inspect, change, reset and export live settings without a restart."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from configuration import is_secret_env_name
from core.permissions import Permission, evaluate_access
from core.settings import SETTING_GROUPS, SettingsError, SettingsRegistry, get_registry

SOURCE_LABELS: Mapping[str, str] = {
    "database": "БД",
    "file": "файл",
    "default": "по умолчанию",
}
GROUP_LABELS: Mapping[str, str] = {
    "discord.channels": "Каналы",
    "discord.roles": "Роли",
    "discord.categories": "Категории",
    "access": "Доступ",
    "economy": "Экономика",
    "cooldowns": "Паузы",
    "profile": "Профиль",
    "texts": "Тексты",
    "limits": "Лимиты",
    "bonus": "Премия",
    "inventory": "Склад",
}
EXPORT_TABLES: Mapping[str, str] = {
    "discord.channels": "channels",
    "discord.roles": "roles",
    "discord.categories": "categories",
}
_SNOWFLAKE_TYPES = frozenset({"channel_id", "role_id", "category_id"})


class ConfigError(Exception):
    """User-facing config failure with a stable machine-readable code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SettingView:
    key: str
    title: str
    group: str
    value_text: str
    source: str
    restart_required: bool
    editable: bool


@dataclass(frozen=True, slots=True)
class SettingDiff:
    key: str
    title: str
    file_text: str
    database_text: str


@dataclass(frozen=True, slots=True)
class SettingChangeResult:
    key: str
    before: str
    after: str
    changed: bool


def group_label(group: str) -> str:
    return GROUP_LABELS.get(group, group)


def known_groups() -> tuple[str, ...]:
    return tuple(sorted(SETTING_GROUPS))


def is_hidden_setting(key: str) -> bool:
    return is_secret_env_name(key)


async def assert_config_manage(member: object) -> None:
    decision = await evaluate_access(member, Permission.CONFIG_MANAGE)
    if decision.allowed:
        return
    raise ConfigError(
        (
            f"{decision.summary} Повторять то же действие без полномочия "
            "бессмысленно. Попросите старший состав выдать «Настройка бота»."
        ),
        code=decision.code or "CONFIG.DENIED",
    )


async def list_settings(
    guild_id: int,
    *,
    group: str | None = None,
    registry: SettingsRegistry | None = None,
) -> tuple[SettingView, ...]:
    active = registry or get_registry()
    if group is not None and group not in SETTING_GROUPS:
        raise ConfigError(
            (
                f"Неизвестная группа {group}. Список не показан. "
                "Выберите группу из реестра настроек. Код: CONFIG.UNKNOWN_GROUP."
            ),
            code="CONFIG.UNKNOWN_GROUP",
        )
    views: list[SettingView] = []
    for descriptor in active.descriptors(group=group):
        if is_hidden_setting(descriptor.key):
            continue
        value = await active.get(guild_id, descriptor.key)
        source = await active.value_source(guild_id, descriptor.key)
        views.append(
            SettingView(
                key=descriptor.key,
                title=descriptor.title,
                group=descriptor.group,
                value_text=active.format_value(descriptor.key, value),
                source=source,
                restart_required=descriptor.restart_required,
                editable=not descriptor.restart_required,
            )
        )
    return tuple(views)


def format_setting_lines(views: tuple[SettingView, ...]) -> list[str]:
    lines: list[str] = []
    for item in views:
        source = SOURCE_LABELS.get(item.source, item.source)
        note = ""
        if item.restart_required:
            note = " · изменяется только в файле, требует перезапуска"
        lines.append(
            f"`{item.key}` = `{item.value_text or '—'}` · {source}{note}\n"
            f"{item.title}"
        )
    return lines


async def set_setting(
    guild_id: int,
    key: str,
    value: str,
    *,
    actor_id: int,
    actor_name: str,
    guild: object | None = None,
    registry: SettingsRegistry | None = None,
    source: str = "command",
) -> SettingChangeResult:
    active = registry or get_registry()
    if is_hidden_setting(key):
        raise ConfigError(
            (
                f"Настройка {key} секретная и не показывается и не принимается. "
                "Значение не записано. Храните секреты в .env. Код: CONFIG.SECRET."
            ),
            code="CONFIG.SECRET",
        )
    try:
        descriptor = active.descriptor(key)
    except SettingsError as exc:
        raise ConfigError(str(exc), code=exc.code) from exc
    try:
        parsed = active.parse(key, value)
        _ensure_discord_object(guild, descriptor, parsed)
        before_value = await active.get(guild_id, key)
        before = active.format_value(key, before_value)
        parsed = await active.set(
            guild_id,
            key,
            value,
            updated_by=str(actor_id),
            source=source,
        )
    except SettingsError as exc:
        raise ConfigError(_explain_validation(exc, key), code=exc.code) from exc
    after = active.format_value(key, parsed)
    return SettingChangeResult(key=key, before=before, after=after, changed=before != after)


async def reset_setting(
    guild_id: int,
    key: str,
    *,
    actor_id: int,
    actor_name: str,
    registry: SettingsRegistry | None = None,
    source: str = "command",
) -> SettingChangeResult:
    del actor_name
    active = registry or get_registry()
    if is_hidden_setting(key):
        raise ConfigError(
            (
                f"Настройка {key} секретная. Сброс не выполнен. "
                "Код: CONFIG.SECRET."
            ),
            code="CONFIG.SECRET",
        )
    try:
        before_value = await active.get(guild_id, key)
        before = active.format_value(key, before_value)
        after_value = await active.reset(
            guild_id,
            key,
            updated_by=str(actor_id),
            source=source,
        )
    except SettingsError as exc:
        raise ConfigError(str(exc), code=exc.code) from exc
    after = active.format_value(key, after_value)
    return SettingChangeResult(key=key, before=before, after=after, changed=before != after)


async def diff_settings(
    guild_id: int,
    *,
    registry: SettingsRegistry | None = None,
) -> tuple[SettingDiff, ...]:
    active = registry or get_registry()
    diffs: list[SettingDiff] = []
    for descriptor in active.descriptors():
        if is_hidden_setting(descriptor.key) or descriptor.restart_required:
            continue
        source = await active.value_source(guild_id, descriptor.key)
        if source != "database":
            continue
        current = await active.get(guild_id, descriptor.key)
        file_value = descriptor.default
        toml = _toml_map(active)
        if descriptor.key in toml:
            file_value = toml[descriptor.key]
        file_parsed = active.parse(descriptor.key, file_value)
        diffs.append(
            SettingDiff(
                key=descriptor.key,
                title=descriptor.title,
                file_text=active.format_value(descriptor.key, file_parsed),
                database_text=active.format_value(descriptor.key, current),
            )
        )
    return tuple(diffs)


async def export_settings(
    guild_id: int,
    *,
    registry: SettingsRegistry | None = None,
) -> str:
    active = registry or get_registry()
    grouped: dict[str, list[str]] = {}
    for descriptor in active.descriptors():
        if is_hidden_setting(descriptor.key):
            continue
        value = await active.get(guild_id, descriptor.key)
        source = await active.value_source(guild_id, descriptor.key)
        table = EXPORT_TABLES.get(descriptor.group, descriptor.group)
        grouped.setdefault(table, []).append(
            f"{descriptor.key} = {_toml_literal(descriptor, value)}  "
            f"# {SOURCE_LABELS.get(source, source)}"
        )
    lines = [
        "# Выгрузка актуальных настроек. Секреты не включены.",
        "# Источник каждого значения: БД, файл или по умолчанию.",
        "# Перенесите нужные строки в bot_config.toml.",
        "",
    ]
    for table in sorted(grouped):
        lines.append(f"[{table}]")
        lines.extend(sorted(grouped[table]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def autocomplete_keys(
    query: str,
    *,
    writable: bool,
    registry: SettingsRegistry | None = None,
) -> list[str]:
    active = registry or get_registry()
    needle = query.strip().casefold()
    keys: list[str] = []
    for descriptor in active.descriptors():
        if is_hidden_setting(descriptor.key):
            continue
        if writable and descriptor.restart_required:
            continue
        if needle and needle not in descriptor.key.casefold():
            continue
        keys.append(descriptor.key)
        if len(keys) >= 25:
            break
    return keys


def _explain_validation(exc: SettingsError, key: str) -> str:
    if exc.code == "SETTINGS_INVALID_TYPE":
        return (
            f"{exc} Допустимый формат зависит от типа настройки {key}: "
            "целое число, true/false, Discord ID, список ID через запятую "
            "или обычная строка. Значение не записано."
        )
    return str(exc)


def _ensure_discord_object(guild: object | None, descriptor: Any, parsed: object) -> None:
    if guild is None or parsed is None or descriptor.value_type not in _SNOWFLAKE_TYPES:
        return
    object_id = int(parsed) if isinstance(parsed, int) else None
    if object_id is None:
        return
    if descriptor.value_type == "role_id":
        getter = getattr(guild, "get_role", None)
        found = getter(object_id) if callable(getter) else None
        if found is None:
            raise ConfigError(
                (
                    f"Роль с ID {object_id} на сервере не найдена. "
                    "Значение не записано. Укажите существующую роль. "
                    "Код: CONFIG.UNKNOWN_ROLE."
                ),
                code="CONFIG.UNKNOWN_ROLE",
            )
        return
    getter = getattr(guild, "get_channel", None)
    found = getter(object_id) if callable(getter) else None
    if found is None:
        kind = "категория" if descriptor.value_type == "category_id" else "канал"
        raise ConfigError(
            (
                f"{kind.capitalize()} с ID {object_id} на сервере не найден. "
                "Значение не записано. Укажите существующий объект. "
                "Код: CONFIG.UNKNOWN_OBJECT."
            ),
            code="CONFIG.UNKNOWN_OBJECT",
        )


def _toml_map(registry: SettingsRegistry) -> Mapping[str, Any]:
    return registry._toml_map()


def _toml_literal(descriptor: Any, value: object) -> str:
    if descriptor.value_type == "bool":
        return "true" if value else "false"
    if (
        descriptor.value_type == "int"
        and isinstance(value, int)
        and not isinstance(value, bool)
    ):
        return str(value)
    if value is None:
        return '""'
    if descriptor.value_type in _SNOWFLAKE_TYPES and isinstance(value, int):
        return f'"{value}"'
    serialized = (
        ",".join(str(item) for item in value)
        if isinstance(value, (list, tuple, set, frozenset))
        else str(value)
    )
    escaped = serialized.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
