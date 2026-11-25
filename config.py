import logging
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from configuration import (
    apply_file_secrets,
    load_public_settings,
    load_secret_env,
    resolve_bot_profile,
)

BASE_DIR = Path(__file__).resolve().parent
load_secret_env(BASE_DIR / ".env")
apply_file_secrets()
BOT_PROFILE = resolve_bot_profile()

_CONFIG_PATH_VALUE = os.getenv("BOT_CONFIG_PATH", "").strip()
CONFIG_PATH = (
    Path(_CONFIG_PATH_VALUE).expanduser()
    if _CONFIG_PATH_VALUE
    else BASE_DIR / "bot_config.toml"
)
if not CONFIG_PATH.is_absolute():
    CONFIG_PATH = BASE_DIR / CONFIG_PATH
_SETTINGS = load_public_settings(CONFIG_PATH)
_USED_SETTINGS: set[str] = set()
logger = logging.getLogger(__name__)


def _setting(name: str, default: Any = None) -> Any:
    _USED_SETTINGS.add(name)
    return _SETTINGS.get(name, default)


def _setting_text(name: str, default: str = "") -> str:
    value = _setting(name, default)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise RuntimeError(f"{name} in {CONFIG_PATH.name} must be a scalar value")


def _parse_snowflake(name: str, raw: str) -> int:
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive Discord snowflake")
    return parsed


def _optional_int(name: str) -> int | None:
    """Read a snowflake from bot_config.toml (channel/role/category IDs)."""

    value = _setting_text(name)
    if not value:
        return None
    return _parse_snowflake(name, value)


def _required_env_snowflake(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(
            f"{name} is required in .env "
            f"(BOT_PROFILE={BOT_PROFILE})"
        )
    return _parse_snowflake(name, raw)


def _process_env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    normalized = str(raw).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")

def _csv_names(name: str, default: str = "") -> tuple[str, ...]:
    value = _setting(name, default)
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise RuntimeError(f"{name} must contain only strings")
        values = value
    else:
        values = _setting_text(name, default).split(",")
    result = tuple(item.strip() for item in values if item.strip())
    if len(result) != len(set(result)):
        raise RuntimeError(f"{name} must not contain duplicates")
    return result


def _csv_ids(name: str) -> tuple[int, ...]:
    raw_value = _setting(name, "")
    values = raw_value if isinstance(raw_value, list) else _setting_text(name).split(",")
    result: list[int] = []
    for value in values:
        value = str(value).strip()
        if not value:
            continue
        if not value.isdigit() or int(value) <= 0:
            raise RuntimeError(
                f"{name} must contain only positive Discord IDs separated by commas"
            )
        result.append(int(value))
    if len(result) != len(set(result)):
        raise RuntimeError(f"{name} must not contain duplicates")
    return tuple(result)


# ==================== КОНФИГУРАЦИЯ БОТА ====================

TOKEN = os.getenv("TOKEN", "").strip() or None
GUILD_ID = _required_env_snowflake("GUILD_ID")
BOT_OWNER_ID = _required_env_snowflake("BOT_OWNER_ID")
# Backwards-compatible alias for integrations which still import OWNER_ID.
OWNER_ID = BOT_OWNER_ID
# Single operating guild for this process (resolved from .env BOT_PROFILE).
ALLOWED_GUILD_IDS: frozenset[int] = frozenset({GUILD_ID})
logger.info(
    "Discord profile=%s guild_id=%s (identity from .env; other guilds will be left)",
    BOT_PROFILE,
    GUILD_ID,
)


def is_authorized_guild(guild_id: int | None) -> bool:
    """True only for the guild selected via BOT_PROFILE + GUILD_ID_* in .env."""

    return guild_id is not None and guild_id in ALLOWED_GUILD_IDS

def _optional_positive_int(name: str) -> int | None:
    value = _setting_text(name)
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def _env_bool(name: str, default: bool = False) -> bool:
    value = _setting(name, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    value = str(value)
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


# Контекст проекта и сервера. Значения читает core/profile.py; поверх них
# накладываются записи guild_settings с префиксом profile.
PROFILE_SERVER_KEY = _setting_text("server_key", "memphis").strip()
PROFILE_FAMILY_NAME = _setting_text("family_name", "Guinness").strip()
PROFILE_FAMILY_TAG = _setting_text("family_tag").strip()
PROFILE_DIRECTION = _setting_text("direction", "unset").strip()
PROFILE_TIMEZONE = _setting_text("timezone", "Europe/Moscow").strip()
PROFILE_SEASON_KEY = _setting_text("season_key", "season-2").strip()
PROFILE_SEASON_START = _setting_text("season_start", "2026-07-28").strip()
PROFILE_SEASON_END = _setting_text("season_end", "2026-10-27").strip()
PROFILE_ONLINE_SOURCE = _setting_text("online_source", "none").strip()
PROFILE_ENABLED_UPGRADES = _csv_names("enabled_upgrades")
PROFILE_MEMBER_LIMIT = _optional_positive_int("member_limit")
PROFILE_ALLOW_ARCHIVED = _env_bool("allow_archived", False)
PROFILE_EXTERNAL_SERVER_ID = _setting_text("external_server_id").strip()

# Расписание Memphis: значения читает services/calendar.py. Пустой список
# или пустая строка — «требует настройки», код время не подставляет.
SCHEDULE_ORG_DAYS = _csv_names("schedule_org_days")
SCHEDULE_REST_DAYS = _csv_names("schedule_rest_days")
SCHEDULE_AIR_CARGO = _csv_names("schedule_air_cargo")
SCHEDULE_DEALERS = _csv_names("schedule_dealers")
SCHEDULE_WORKSHOPS = _csv_names("schedule_workshops")
SCHEDULE_INFLUENCE_WINDOW = _setting_text("schedule_influence_window").strip()
SCHEDULE_HOLD_INFLUENCE = _setting_text("schedule_hold_influence").strip()
SCHEDULE_LOTTERY_DAYS = _csv_names("schedule_lottery_days")
SCHEDULE_LOTTERY_TIME = _setting_text("schedule_lottery_time").strip()
SCHEDULE_AUCTION_DAYS = _csv_names("schedule_auction_days")
SCHEDULE_AUCTION_TIME = _setting_text("schedule_auction_time").strip()
SCHEDULE_WAREHOUSE_TAG_HOURS = _optional_positive_int("schedule_warehouse_tag_hours")


def public_settings_map() -> Mapping[str, Any]:
    """Flattened ``bot_config.toml`` values for the live settings registry."""

    return _SETTINGS


def profile_toml_values() -> dict[str, object]:
    """Leaf values of ``[profile]`` after the usual TOML flattening."""

    return {
        "server_key": PROFILE_SERVER_KEY,
        "family_name": PROFILE_FAMILY_NAME,
        "family_tag": PROFILE_FAMILY_TAG,
        "direction": PROFILE_DIRECTION,
        "timezone": PROFILE_TIMEZONE,
        "season_key": PROFILE_SEASON_KEY,
        "season_start": PROFILE_SEASON_START,
        "season_end": PROFILE_SEASON_END,
        "online_source": PROFILE_ONLINE_SOURCE,
        "enabled_upgrades": PROFILE_ENABLED_UPGRADES,
        "member_limit": PROFILE_MEMBER_LIMIT,
        "allow_archived": PROFILE_ALLOW_ARCHIVED,
        "external_server_id": PROFILE_EXTERNAL_SERVER_ID,
    }


def schedule_toml_values() -> dict[str, object]:
    """Leaf values of ``[profile.schedule]`` after TOML flattening."""

    return {
        "org_days": SCHEDULE_ORG_DAYS,
        "rest_days": SCHEDULE_REST_DAYS,
        "air_cargo_times": SCHEDULE_AIR_CARGO,
        "dealers_times": SCHEDULE_DEALERS,
        "workshops_times": SCHEDULE_WORKSHOPS,
        "influence_window": SCHEDULE_INFLUENCE_WINDOW,
        "hold_influence_time": SCHEDULE_HOLD_INFLUENCE,
        "lottery_days": SCHEDULE_LOTTERY_DAYS,
        "lottery_time": SCHEDULE_LOTTERY_TIME,
        "auction_days": SCHEDULE_AUCTION_DAYS,
        "auction_time": SCHEDULE_AUCTION_TIME,
        "warehouse_tag_hours": SCHEDULE_WAREHOUSE_TAG_HOURS,
        "timezone": PROFILE_TIMEZONE,
    }


def _presence_interval() -> float:
    value = _setting_text("BOT_ACTIVITY_INTERVAL", "60")
    try:
        return max(15.0, float(value))
    except ValueError as exc:
        raise RuntimeError("BOT_ACTIVITY_INTERVAL must be a number") from exc


def _bounded_float(name: str, default: str, minimum: float) -> float:
    try:
        return max(minimum, float(_setting_text(name, default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


def _bounded_int(name: str, default: str, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(_setting_text(name, default))))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _coins_to_milli(name: str, default: str) -> int:
    try:
        value = Decimal(_setting_text(name, default).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise RuntimeError(f"{name} must be a non-negative number") from exc
    if value < 0 or value > Decimal("1000000"):
        raise RuntimeError(f"{name} must be between 0 and 1000000")
    return int(value * 1000)


BOT_STATUS = _setting_text("BOT_STATUS", "online").strip().lower()
BOT_ACTIVITY_TYPE = _setting_text("BOT_ACTIVITY_TYPE", "playing").strip().lower()
BOT_ACTIVITY_TEXT = _setting_text("BOT_ACTIVITY_TEXT", "Guinness Family").strip()
BOT_ACTIVITY_TEXTS = tuple(
    text.strip()
    for text in _setting_text("BOT_ACTIVITY_TEXTS", "").split("|")
    if text.strip()
)
BOT_ACTIVITY_INTERVAL = _presence_interval()
BOT_STREAM_URL = _setting_text(
    "BOT_STREAM_URL", "https://www.twitch.tv/discord"
).strip()


def _legacy_key_present(old_key: str) -> bool:
    if old_key not in _SETTINGS:
        return False
    _USED_SETTINGS.add(old_key)
    return True


def _warn_legacy_key(old_key: str, new_key: str) -> None:
    logger.warning(
        "Обнаружен устаревший ключ %s; используйте %s в секции [profile]",
        old_key,
        new_key,
    )


def _legacy_text(new_key: str, old_key: str, default: str) -> str:
    old_present = _legacy_key_present(old_key)
    if old_present:
        _warn_legacy_key(old_key, new_key)
    if new_key in _SETTINGS:
        return _setting_text(new_key, default).strip()
    if old_present:
        return _setting_text(old_key, default).strip()
    return default


def _legacy_bool(new_key: str, old_key: str, default: bool) -> bool:
    old_present = _legacy_key_present(old_key)
    if old_present:
        _warn_legacy_key(old_key, new_key)
    if new_key in _SETTINGS:
        return _env_bool(new_key, default)
    if old_present:
        return _env_bool(old_key, default)
    return default


def _legacy_int(new_key: str, old_key: str, default: int, minimum: int) -> int:
    old_present = _legacy_key_present(old_key)
    if old_present:
        _warn_legacy_key(old_key, new_key)
    if new_key in _SETTINGS:
        raw = _setting(new_key, default)
    elif old_present:
        raw = _setting(old_key, default)
    else:
        return default
    if isinstance(raw, bool) or raw is None:
        raise RuntimeError(f"{new_key} must be an integer")
    if isinstance(raw, int):
        parsed = raw
    else:
        try:
            parsed = int(Decimal(str(raw).strip()))
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError(f"{new_key} must be an integer") from exc
    return max(minimum, parsed)


def _consume_legacy_choice_maps() -> tuple[str, ...]:
    found: list[str] = []
    pattern = re.compile(r"^CHOICE_MAP_(\d+)$")
    for key in _SETTINGS:
        match = pattern.fullmatch(key)
        if match is None:
            continue
        _USED_SETTINGS.add(key)
        found.append(key)
        logger.warning(
            "Обнаружен устаревший ключ %s; каталог карт заполняется "
            "старшим составом, а не файлами в репозитории",
            key,
        )

    def _index(key: str) -> int:
        matched = pattern.fullmatch(key)
        if matched is None:
            raise RuntimeError(key)
        return int(matched.group(1))

    return tuple(sorted(found, key=_index))


_consume_legacy_choice_maps()
RAGE_MASTER_ENABLED = _legacy_bool(
    "rage_master_enabled", "GTA5RP_STATUS_ENABLED", True
)
RAGE_MASTER_URL = _legacy_text(
    "rage_master_url",
    "GTA5RP_MASTER_URL",
    "https://cdn.rage.mp/master/",
)
RAGE_SERVER_QUERY = _legacy_text(
    "rage_server_query", "GTA5RP_SERVER_QUERY", ""
)
RAGE_SERVER_NAME = _legacy_text(
    "rage_server_name", "GTA5RP_SERVER_NAME", ""
)
PRESENCE_STATUS_TEXT = _legacy_text(
    "presence_status_text",
    "GTA5RP_STATUS_TEXT",
    "{server}: {players} {players_word}",
)
PRESENCE_GAME_INTERVAL = _legacy_int(
    "presence_game_interval", "GTA5RP_STATUS_INTERVAL", 60, 30
)
RAGE_REQUEST_TIMEOUT = _legacy_int(
    "rage_request_timeout", "GTA5RP_REQUEST_TIMEOUT", 10, 3
)
COMMAND_SYNC_FORCE = _process_env_bool("COMMAND_SYNC_FORCE", False)
COMMAND_CLEAR_GLOBAL = _process_env_bool("COMMAND_CLEAR_GLOBAL", False)

# Каналы (названия)
CHANNELS = {
    "help": _setting_text("CHANNEL_HELP", "помощь"),
    "help_target": _setting_text("CHANNEL_HELP_TARGET", "🛟・help"),
    "request_ss": _setting_text("CHANNEL_REQUEST_SS", "очередь-сс"),
    "vzp": _setting_text("CHANNEL_VZP", "🛠️・семейные-фичи"),
    "vzp_invite": _setting_text("CHANNEL_VZP_INVITE", "📝・сс-запросы"),
    "family_invite": _setting_text("CHANNEL_FAMILY_INVITE", "🐳・заявки-в-семью"),
    "family_invite_logs": _setting_text(
        "CHANNEL_FAMILY_INVITE_LOGS", "📋・заявки-в-семью"
    ),
    "add_birthday": _setting_text("CHANNEL_ADD_BIRTHDAY", "🛠️・семейные-фичи"),
    "birthday_request": _setting_text(
        "CHANNEL_BIRTHDAY_REQUEST", "📝・сс-запросы"
    ),
    "add_roles": _setting_text("CHANNEL_ADD_ROLES", "🔑・роли"),
    "happy_birthday": _setting_text(
        "CHANNEL_HAPPY_BIRTHDAY", "🎉・дни-рождения"
    ),
    "role_request": _setting_text("CHANNEL_ROLE_REQUEST", "📝・сс-запросы"),
    "request_actions": _setting_text(
        "CHANNEL_REQUEST_ACTIONS", "аудит"
    ),
    "anonymous_logs": _setting_text("CHANNEL_ANONYMOUS_LOGS", "🕵・анон-логи"),
    "anonymous_request": _setting_text(
        "CHANNEL_ANONYMOUS_REQUEST", "🎭・анон-обращения"
    ),
    "tickets_category": _setting_text(
        "CHANNEL_TICKETS_CATEGORY", "🏷️・TICKETS・🏷️"
    ),
    "treasury": _setting_text("CHANNEL_TREASURY", "выплаты"),
    "transactions": _setting_text("CHANNEL_TRANSACTIONS", "💱・транзакции"),
    "car_list": _setting_text("CHANNEL_CAR_LIST", "снабжение"),
    "car_bookings": _setting_text("CHANNEL_CAR_BOOKINGS", "🚗・авто-логи"),
    "weekly_stats": _setting_text("CHANNEL_WEEKLY_STATS", "📢・новости"),
    "shop": _setting_text("CHANNEL_SHOP", "🛠️・семейные-фичи"),
    "family_features": _setting_text(
        "CHANNEL_FAMILY_FEATURES", "старт-и-правила"
    ),
    "role_approval": _setting_text("CHANNEL_ROLE_APPROVAL", "📝・сс-запросы"),
    "summit_vzp": _setting_text("CHANNEL_SUMMIT_VZP", "архив-сборов"),
    "summit_list": _setting_text("CHANNEL_SUMMIT_LIST", "события"),
    "summit_contract": _setting_text(
        "CHANNEL_SUMMIT_CONTRACT", "отчёты"
    ),
    "giveaways": _setting_text("CHANNEL_GIVEAWAYS", "🎁・розыгрыши"),
    "recruiting": _setting_text("CHANNEL_RECRUITING", "💼・рекрутинг"),
    "recruiting_logs": _setting_text(
        "CHANNEL_RECRUITING_LOGS", "📎・рекрутинг-логи"
    ),
    "recruiting_invite": _setting_text(
        "CHANNEL_RECRUITING_INVITE", "🐳・заявки-в-семью"
    ),
    "roster_watch": _setting_text(
        "CHANNEL_ROSTER_WATCH", "📋・выход-состава"
    ),
    "voice_create_category": _setting_text(
        "CATEGORY_CREATE_VOICE", "✨・VOICES・✨"
    ),
    "high_tools_category": _setting_text(
        "CATEGORY_HIGH_TOOLS", "🔰・HIGH TOOLS ・🔰"
    ),
    "public_category": _setting_text("CATEGORY_PUBLIC", "🌐・PUBLIC・🌐"),
    "family_category": _setting_text("CATEGORY_FAMILY", "🏠・FAMILY・🏠"),
    "interactive_category": _setting_text(
        "CATEGORY_INTERACTIVE", "🕹️・INTERACTIVE・🕹️"
    ),
    "voice_create_lobby": _setting_text(
        "CHANNEL_CREATE_VOICE", "🌠・Create Voice +"
    ),
    "insights_rating": _setting_text("CHANNEL_INSIGHTS_RATING", "📈・рейтинг"),
    "insights_battles": _setting_text("CHANNEL_INSIGHTS_BATTLES", "📋・разборы"),
    "insights_market": _setting_text("CHANNEL_INSIGHTS_MARKET", "🛒・рынок"),
    "insights_property": _setting_text("CHANNEL_INSIGHTS_PROPERTY", "🏠・псн"),
    "birthday_fundraiser_category": _setting_text(
        "CATEGORY_BIRTHDAY_FUNDRAISER", "🎂・BIRTHDAY SECRET・🎂"
    ),
    "groups_category": _setting_text("CATEGORY_GROUPS", "🌸・GROUPS・🌸"),
    "developer_category": _setting_text(
        "CATEGORY_DEVELOPER", "⚙️・DEVELOPER・⚙️"
    ),
}

CHANNEL_ID_ENV = {
    "help": "CHANNEL_HELP_ID",
    "help_target": "CHANNEL_HELP_TARGET_ID",
    "request_ss": "CHANNEL_REQUEST_SS_ID",
    "vzp": "CHANNEL_VZP_ID",
    "vzp_invite": "CHANNEL_VZP_INVITE_ID",
    "family_invite": "CHANNEL_FAMILY_INVITE_ID",
    "family_invite_logs": "CHANNEL_FAMILY_INVITE_LOGS_ID",
    "add_birthday": "CHANNEL_ADD_BIRTHDAY_ID",
    "birthday_request": "CHANNEL_BIRTHDAY_REQUEST_ID",
    "add_roles": "CHANNEL_ADD_ROLES_ID",
    "happy_birthday": "CHANNEL_HAPPY_BIRTHDAY_ID",
    "role_request": "CHANNEL_ROLE_REQUEST_ID",
    "request_actions": "CHANNEL_REQUEST_ACTIONS_ID",
    "anonymous_logs": "CHANNEL_ANONYMOUS_LOGS_ID",
    "anonymous_request": "CHANNEL_ANONYMOUS_REQUEST_ID",
    "treasury": "CHANNEL_TREASURY_ID",
    "transactions": "CHANNEL_TRANSACTIONS_ID",
    "car_list": "CHANNEL_CAR_LIST_ID",
    "car_bookings": "CHANNEL_CAR_BOOKINGS_ID",
    "weekly_stats": "CHANNEL_WEEKLY_STATS_ID",
    "shop": "CHANNEL_SHOP_ID",
    "family_features": "CHANNEL_FAMILY_FEATURES_ID",
    "role_approval": "CHANNEL_ROLE_APPROVAL_ID",
    "summit_vzp": "CHANNEL_SUMMIT_VZP_ID",
    "summit_list": "CHANNEL_SUMMIT_LIST_ID",
    "summit_contract": "CHANNEL_SUMMIT_CONTRACT_ID",
    "giveaways": "CHANNEL_GIVEAWAYS_ID",
    "recruiting": "CHANNEL_RECRUITING_ID",
    "recruiting_logs": "CHANNEL_RECRUITING_LOGS_ID",
    "recruiting_invite": "CHANNEL_RECRUITING_INVITE_ID",
    "roster_watch": "CHANNEL_ROSTER_WATCH_ID",
    "voice_create_lobby": "CHANNEL_CREATE_VOICE_ID",
    "insights_rating": "CHANNEL_INSIGHTS_RATING_ID",
    "insights_battles": "CHANNEL_INSIGHTS_BATTLES_ID",
    "insights_market": "CHANNEL_INSIGHTS_MARKET_ID",
    "insights_property": "CHANNEL_INSIGHTS_PROPERTY_ID",
}
# Deprecated for runtime reads: use channel_id(name). Import-time snapshot.
CHANNEL_IDS = {key: _optional_int(env_name) for key, env_name in CHANNEL_ID_ENV.items()}
# Deprecated for runtime reads: use category id via the settings registry.
CATEGORY_ID_ENV = {
    "tickets_category": "CATEGORY_TICKETS_ID",
    "birthday_fundraiser_category": "CATEGORY_BIRTHDAY_FUNDRAISER_ID",
    "voice_create_category": "CATEGORY_VOICE_CREATE_ID",
    "high_tools_category": "CATEGORY_HIGH_TOOLS_ID",
    "public_category": "CATEGORY_PUBLIC_ID",
    "family_category": "CATEGORY_FAMILY_ID",
    "interactive_category": "CATEGORY_INTERACTIVE_ID",
    "groups_category": "CATEGORY_GROUPS_ID",
    "developer_category": "CATEGORY_DEVELOPER_ID",
}
CATEGORY_IDS = {
    key: _optional_int(env_name) for key, env_name in CATEGORY_ID_ENV.items()
}

AFK_VOICE_CHANNEL = _setting_text("AFK_VOICE_CHANNEL", "💤・Спальня")
AFK_VOICE_CHANNEL_ID = _optional_int("AFK_VOICE_CHANNEL_ID")
REQUEST_PING_ROLE_NAMES = _csv_names("REQUEST_PING_ROLES", "Owner,Dep. Owner,High")
REQUEST_PING_ROLE_IDS = _csv_ids("REQUEST_PING_ROLE_IDS")
ROSTER_WATCH_VISITOR_POLICY = _setting_text("ROSTER_WATCH_VISITOR_POLICY", "skip")
ROSTER_WATCH_BOT_POLICY = _setting_text("ROSTER_WATCH_BOT_POLICY", "skip")
ROSTER_WATCH_PING_ROLE_NAMES = _csv_names("ROSTER_WATCH_PING_ROLES")
ROSTER_WATCH_PING_ROLE_IDS = _csv_ids("ROSTER_WATCH_PING_ROLE_IDS")
REPORT_DETAIL_THRESHOLD = _bounded_int("REPORT_DETAIL_THRESHOLD", "0", 0, 1_000_000_000)
REPORT_EVIDENCE_MAX_BYTES = _bounded_int(
    "REPORT_EVIDENCE_MAX_BYTES", "0", 0, 100_000_000
)
REPORT_EVIDENCE_RETENTION_DAYS = _bounded_int(
    "REPORT_EVIDENCE_RETENTION_DAYS", "0", 0, 3650
)
PROFIT_PURCHASE_LOOKBACK = _bounded_int("PROFIT_PURCHASE_LOOKBACK", "0", 0, 10_000)
QUEUE_SLA_MINUTES = _bounded_int("QUEUE_SLA_MINUTES", "0", 0, 525_600)
QUEUE_DEPUTY_ROLE_IDS = _csv_ids("QUEUE_DEPUTY_ROLE_IDS")
QUEUE_KIND_SPECIALIZATION = _setting_text(
    "QUEUE_KIND_SPECIALIZATION", "report:contracts"
)
HIGH_TOOLS_ALLOWED_ROLE_NAMES = _csv_names(
    "HIGH_TOOLS_ALLOWED_ROLES", "Owner,Dep. Owner,High"
)
HIGH_TOOLS_ALLOWED_ROLE_IDS = _csv_ids("HIGH_TOOLS_ALLOWED_ROLE_IDS")
TREASURY_OWNER_ROLE_NAME = _setting_text("TREASURY_OWNER_ROLE", "Owner").strip()
TREASURY_OWNER_ROLE_ID = _optional_int("TREASURY_OWNER_ROLE_ID")
ROLE_REJECTION_REASONS = tuple(
    value.strip()
    for value in _setting_text(
        "ROLE_REJECTION_REASONS",
        "Провокационное название|Имитация старшего состава|Нецензурное название|Нарушение правил|Другое",
    ).split("|")
    if value.strip()
)
if not ROLE_REJECTION_REASONS:
    raise RuntimeError("ROLE_REJECTION_REASONS must not be empty")
if len(ROLE_REJECTION_REASONS) > 25:
    raise RuntimeError("Discord supports no more than 25 role rejection reasons")
if len(ROLE_REJECTION_REASONS) != len(set(ROLE_REJECTION_REASONS)):
    raise RuntimeError("ROLE_REJECTION_REASONS must not contain duplicates")
if any(len(reason) > 100 for reason in ROLE_REJECTION_REASONS):
    raise RuntimeError("Each role rejection reason must be at most 100 characters")
POLL_ALLOWED_ROLE_NAMES = _csv_names(
    "POLL_ALLOWED_ROLES", "Owner,Dep. Owner,High"
)
POLL_ALLOWED_ROLE_IDS = _csv_ids("POLL_ALLOWED_ROLE_IDS")
RECRUITING_ROLE_NAMES = _csv_names(
    "RECRUITING_ROLES", "Owner,Dep. Owner,High"
)
RECRUITING_ROLE_IDS = _csv_ids("RECRUITING_ROLE_IDS")
# Deprecated for runtime reads: use setting_int("COINS_DAILY") and siblings.
SHOP_PRICES = {
    "daily": _bounded_int("COINS_DAILY", "100", 1, 2_147_483_647),
    "color_role": _bounded_int(
        "COINS_COLOR_ROLE", "700", 1, 2_147_483_647
    ),
    "custom_role": _bounded_int(
        "COINS_CUSTOM_ROLE", "1400", 1, 2_147_483_647
    ),
    "vip_week": _bounded_int(
        "COINS_VIP_WEEK", "700", 1, 2_147_483_647
    ),
    "vip_month": _bounded_int(
        "COINS_VIP_MONTH", "2400", 1, 2_147_483_647
    ),
    "summit_emoji": _bounded_int(
        "COINS_SUMMIT_EMOJI", "1000", 1, 2_147_483_647
    ),
}
COINS_MESSAGE_REWARD_MILLI = _coins_to_milli("COINS_MESSAGE_REWARD", "5")
COINS_VOICE_REWARD_MILLI = _coins_to_milli(
    "COINS_VOICE_REWARD_PER_MINUTE", "0.5"
)
COINS_MESSAGE_COOLDOWN_SECONDS = _bounded_int(
    "COINS_MESSAGE_COOLDOWN_SECONDS", "45", 0, 3600
)
COINS_MESSAGE_MIN_LENGTH = _bounded_int(
    "COINS_MESSAGE_MIN_LENGTH", "4", 1, 500
)
COINS_MESSAGE_DAILY_CAP = _bounded_int(
    "COINS_MESSAGE_DAILY_CAP", "500", 0, 2_147_483_647
)
COINS_VOICE_DAILY_CAP = _bounded_int(
    "COINS_VOICE_DAILY_CAP", "300", 0, 2_147_483_647
)
COINS_VOICE_MIN_ACTIVE_MEMBERS = _bounded_int(
    "COINS_VOICE_MIN_ACTIVE_MEMBERS", "2", 1, 25
)
COINS_MESSAGE_EXCLUDED_CHANNEL_IDS = _csv_ids(
    "COINS_MESSAGE_EXCLUDED_CHANNEL_IDS"
)
TREASURY_BATCH_THRESHOLD = _bounded_int(
    "TREASURY_BATCH_THRESHOLD", "0", 0, 2_147_483_647
)
CONTRIBUTION_CONTRACT = _bounded_int("CONTRIBUTION_CONTRACT", "0", 0, 2_147_483_647)
CONTRIBUTION_ATTENDANCE = _bounded_int(
    "CONTRIBUTION_ATTENDANCE", "0", 0, 2_147_483_647
)
CONTRIBUTION_MENTORING = _bounded_int(
    "CONTRIBUTION_MENTORING", "0", 0, 2_147_483_647
)
CONTRIBUTION_WAREHOUSE = _bounded_int(
    "CONTRIBUTION_WAREHOUSE", "0", 0, 2_147_483_647
)
CONTRIBUTION_LOGISTICS = _bounded_int(
    "CONTRIBUTION_LOGISTICS", "0", 0, 2_147_483_647
)
CONTRIBUTION_DUTY = _bounded_int("CONTRIBUTION_DUTY", "0", 0, 2_147_483_647)
BONUS_PERIOD_KIND = _setting_text("BONUS_PERIOD_KIND", "week").strip().lower()
BONUS_MIN_POINTS = _bounded_int("BONUS_MIN_POINTS", "0", 0, 2_147_483_647)
BONUS_MAX_SHARE_PERCENT = _bounded_int("BONUS_MAX_SHARE_PERCENT", "0", 0, 100)
BONUS_ORG_INFLUENCE = _bounded_int("BONUS_ORG_INFLUENCE", "0", 0, 2_147_483_647)
BONUS_RANK_THRESHOLDS = _setting_text(
    "BONUS_RANK_THRESHOLDS", "500,1300,2600,4200,6300,8700,11500"
)
BONUS_RANK_NAMES = _setting_text(
    "BONUS_RANK_NAMES", "Чужаки,Местные,Авангард,Синдикат,Картель,Империя,Орден"
)
BONUS_RANK_MULTIPLIER_MILLI = _setting_text(
    "BONUS_RANK_MULTIPLIER_MILLI", "1100,1200,1350,1500,1650,1800,2000"
)
BONUS_RANK_DC = _setting_text("BONUS_RANK_DC", "100,250,450,750,1100,1500,2000")
INVENTORY_WAREHOUSE_SLOTS = _bounded_int(
    "INVENTORY_WAREHOUSE_SLOTS", "20", 0, 2_147_483_647
)
INVENTORY_SAFE_MONEY = _bounded_int(
    "INVENTORY_SAFE_MONEY", "10000000", 0, 2_147_483_647
)
INVENTORY_GUN_CABINET_KG = _bounded_int(
    "INVENTORY_GUN_CABINET_KG", "1000", 0, 2_147_483_647
)
INVENTORY_PANTRY_KG = _bounded_int("INVENTORY_PANTRY_KG", "100", 0, 2_147_483_647)
INVENTORY_GARAGE_EXTRA_MAX = _bounded_int(
    "INVENTORY_GARAGE_EXTRA_MAX", "30", 0, 2_147_483_647
)
INVENTORY_MAT_L1_KG = _bounded_int("INVENTORY_MAT_L1_KG", "1000", 0, 2_147_483_647)
INVENTORY_MAT_L2_KG = _bounded_int("INVENTORY_MAT_L2_KG", "2000", 0, 2_147_483_647)
INVENTORY_MAT_L3_KG = _bounded_int("INVENTORY_MAT_L3_KG", "4000", 0, 2_147_483_647)
INVENTORY_MAT_LEVEL = _bounded_int("INVENTORY_MAT_LEVEL", "0", 0, 3)
INVENTORY_LOW_STOCK = _bounded_int("INVENTORY_LOW_STOCK", "0", 0, 2_147_483_647)
INVENTORY_RECONCILE_STALE_HOURS = _bounded_int(
    "INVENTORY_RECONCILE_STALE_HOURS", "0", 0, 2_147_483_647
)
INVENTORY_SALE_PERCENT = _bounded_int("INVENTORY_SALE_PERCENT", "75", 0, 100)
PROGRESSION_SEASON_MEMBERSHIP_DAYS = _bounded_int(
    "PROGRESSION_SEASON_MEMBERSHIP_DAYS", "28", 0, 2_147_483_647
)
PROGRESSION_SEASON_PLAY_HOURS = _bounded_int(
    "PROGRESSION_SEASON_PLAY_HOURS", "30", 0, 2_147_483_647
)
PROGRESSION_DIRECTION_COOLDOWN_DAYS = _bounded_int(
    "PROGRESSION_DIRECTION_COOLDOWN_DAYS", "7", 0, 2_147_483_647
)
PROGRESSION_REP_LOSS_PER_MILLE = _bounded_int(
    "PROGRESSION_REP_LOSS_PER_MILLE", "50", 0, 1000
)
PROGRESSION_MAX_LEVEL = _bounded_int("PROGRESSION_MAX_LEVEL", "9", 1, 9)
EXTERNAL_API_BASE_URL = _setting_text("EXTERNAL_API_BASE_URL", "").strip().rstrip("/")
EXTERNAL_API_VERSION = _setting_text("EXTERNAL_API_VERSION", "").strip()
EXTERNAL_API_SERVER_ID = _setting_text("EXTERNAL_API_SERVER_ID", "").strip()
EXTERNAL_API_RATE_LIMIT = _bounded_int(
    "EXTERNAL_API_RATE_LIMIT", "0", 0, 2_147_483_647
)
EXTERNAL_API_RATE_WINDOW_SECONDS = _bounded_int(
    "EXTERNAL_API_RATE_WINDOW_SECONDS", "0", 0, 2_147_483_647
)
EXTERNAL_API_ATTRIBUTION = _setting_text("EXTERNAL_API_ATTRIBUTION", "").strip()
EXTERNAL_KEY_OWNER = _setting_text("EXTERNAL_KEY_OWNER", "").strip()
EXTERNAL_API_TIMEOUT_SECONDS = _bounded_int("EXTERNAL_API_TIMEOUT_SECONDS", "10", 1, 120)

# Платная музыкальная очередь. Списание выполняется только после успешного
# определения трека; незавершённые при перезапуске записи возвращаются.
MUSIC_PRICE_SHORT = _bounded_int(
    "MUSIC_PRICE_SHORT", "500", 1, 2_147_483_647
)
MUSIC_PRICE_LONG = _bounded_int(
    "MUSIC_PRICE_LONG", "1000", 1, 2_147_483_647
)
MUSIC_LONG_TRACK_SECONDS = _bounded_int(
    "MUSIC_LONG_TRACK_SECONDS", "600", 60, 86_400
)
MUSIC_MAX_TRACK_SECONDS = _bounded_int(
    "MUSIC_MAX_TRACK_SECONDS", "7200", 60, 86_400
)
MUSIC_MAX_QUEUE_SIZE = _bounded_int("MUSIC_MAX_QUEUE_SIZE", "25", 1, 100)
MUSIC_MAX_USER_QUEUE = _bounded_int("MUSIC_MAX_USER_QUEUE", "3", 1, 20)
MUSIC_PLAY_COOLDOWN_SECONDS = _bounded_int(
    "MUSIC_PLAY_COOLDOWN_SECONDS", "10", 1, 300
)
MUSIC_MAX_CONCURRENT_EXTRACTS = _bounded_int(
    "MUSIC_MAX_CONCURRENT_EXTRACTS", "4", 1, 16
)
MUSIC_CANCEL_WINDOW_SECONDS = _bounded_int(
    "MUSIC_CANCEL_WINDOW_SECONDS", "30", 5, 300
)
MUSIC_PUBLIC_SKIP_REFUND_SECONDS = _bounded_int(
    "MUSIC_PUBLIC_SKIP_REFUND_SECONDS", "180", 0, 86_400
)
MUSIC_SKIP_MIN_VOTES = _bounded_int("MUSIC_SKIP_MIN_VOTES", "2", 1, 25)
MUSIC_SKIP_VOTE_RATIO = min(
    1.0,
    _bounded_float("MUSIC_SKIP_VOTE_RATIO", "0.5", 0.1),
)
MUSIC_IDLE_TIMEOUT_SECONDS = _bounded_int(
    "MUSIC_IDLE_TIMEOUT_SECONDS", "120", 15, 3600
)
MUSIC_VOLUME_PERCENT = _bounded_int("MUSIC_VOLUME_PERCENT", "50", 1, 100)
MUSIC_VOLUME_STEP_PERCENT = _bounded_int(
    "MUSIC_VOLUME_STEP_PERCENT", "10", 1, 50
)
MUSIC_EXTRACT_TIMEOUT_SECONDS = _bounded_int(
    "MUSIC_EXTRACT_TIMEOUT_SECONDS", "20", 5, 120
)
MUSIC_ALLOWED_HOSTS = tuple(
    value.strip().casefold().lstrip(".")
    for value in _setting_text(
        "MUSIC_ALLOWED_HOSTS",
        "youtube.com,youtu.be,music.youtube.com,soundcloud.com",
    ).split(",")
    if value.strip()
)
FFMPEG_EXECUTABLE = (
    _setting_text("FFMPEG_EXECUTABLE", "ffmpeg").strip() or "ffmpeg"
)

# Пользовательские кулдауны. Снимок на момент импорта; актуальные значения —
# setting_int(...) или запись в runtime_settings. /help cooldown по-прежнему
# пишет в guild_settings и читается отдельно.
COMMAND_COOLDOWN_DEFAULTS = {
    "help_request": _bounded_int(
        "HELP_REQUEST_COOLDOWN_SECONDS", "300", 0, 86_400
    ),
    "help": _bounded_int("HELP_COMMAND_COOLDOWN_SECONDS", "5", 0, 86_400),
    "summit_list": _bounded_int(
        "SUMMIT_LIST_COOLDOWN_SECONDS", "30", 0, 86_400
    ),
    "event_create": _bounded_int(
        "SUMMIT_VZP_COOLDOWN_SECONDS", "30", 0, 86_400
    ),
    "contract_start": _bounded_int(
        "SUMMIT_CONTRACT_COOLDOWN_SECONDS", "30", 0, 86_400
    ),
    "summit_vzp": _bounded_int(
        "SUMMIT_VZP_COOLDOWN_SECONDS", "30", 0, 86_400
    ),
    "summit_contract": _bounded_int(
        "SUMMIT_CONTRACT_COOLDOWN_SECONDS", "30", 0, 86_400
    ),
}

# Роли
ROLES = {
    "staff": _setting_text("STAFF_ROLE", "Senior Staff"),
    "high": _setting_text("HIGH_ROLE", "High"),
    "owner": _setting_text("OWNER_ROLE", "Owner"),
    "dep_owner": _setting_text("DEP_OWNER_ROLE", "Dep. Owner"),
    "vzp_team": _setting_text("VZP_TEAM_ROLE", "VZP Team"),
    "guest": _setting_text("GUEST_ROLE", "Гость"),
    "young": _setting_text("YOUNG_ROLE", "Young"),
    "main": _setting_text("MAIN_ROLE", "Main"),
    "friend": _setting_text("FRIEND_ROLE", "Friend"),
    "super_friend": _setting_text("SUPER_FRIEND_ROLE", "Super Friend"),
    "help": _setting_text("HELP_ROLE", "Help"),
    "contract": _setting_text("CONTRACT_ROLE", "Contract"),
    "lead_vzp": _setting_text("LEAD_VZP_ROLE", "Lead VZP"),
    "vip": _setting_text("VIP_ROLE", "🪙 VIP"),
    "vzp_1": _setting_text("VZP_1_ROLE", "[1] VZP"),
    "vzp_2": _setting_text("VZP_2_ROLE", "[2] VZP"),
    "vzp_3": _setting_text("VZP_3_ROLE", "[3] VZP"),
}

# Каждой канонической роли соответствует необязательный Discord ID. Заполненный
# ID авторитетен: он переживает переименование роли и снимает неоднозначность
# при совпадающих именах. Имя остаётся запасным вариантом и используется только
# пока ID пуст. Собрать реальные значения помогает `/admin config_ids`.
ROLE_ID_ENV = {key: f"{key.upper()}_ROLE_ID" for key in ROLES}
# Deprecated for runtime reads: use role_ids(name) / the settings registry.
ROLE_IDS = {key: _optional_int(env_name) for key, env_name in ROLE_ID_ENV.items()}

# Роль Main определяет, считается ли канал видимым для семьи в еженедельной
# статистике. Без её надёжного разрешения приватные каналы могут попасть в
# публичный рейтинг, поэтому наличие ID проверяется отдельно в preflight.
MAIN_ROLE_ID = ROLE_IDS["main"]

# Иерархия назначения тикетов задаётся от самой высокой роли к самой низкой.
# Если ID заполнены, они являются авторитетными и имена не используются.
TICKET_ASSIGNMENT_ROLE_HIERARCHY = _csv_names(
    "TICKET_ASSIGNMENT_ROLE_HIERARCHY",
    "Owner,Dep. Owner,High",
)
TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS = _csv_ids(
    "TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS"
)

# Пути
def _data_path(env_name: str, default_name: str) -> Path:
    # systemd may relocate mutable state without putting ordinary settings in .env.
    configured_path = _setting_text(env_name, default_name)
    raw_path = os.getenv(env_name) or configured_path
    if not raw_path.strip():
        raise RuntimeError(f"{env_name} must not be empty")
    path = Path(raw_path).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _started_by_a_test_runner() -> bool:
    """True when this interpreter was launched by unittest or pytest.

    Detected from the entry point rather than from an environment variable
    precisely because an environment variable can be forgotten — and forgetting
    it once means the suite writes to whatever ``.env`` happens to point at.
    """

    entry = getattr(sys.modules.get("__main__"), "__file__", "") or ""
    name = os.path.basename(entry)
    parent = os.path.basename(os.path.dirname(entry))
    return name in {"pytest", "py.test"} or (
        name == "__main__.py" and parent in {"unittest", "pytest"}
    )


DB_PATH = _data_path("DB_PATH", "guinness_bot.db")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL and _started_by_a_test_runner():
    # The tests each build a throwaway SQLite schema and never expect a server.
    # A .env next to the checkout — which is exactly what the deploy directory
    # has — otherwise redirects the whole suite at the production database,
    # where it creates tickets, moves Coins and then fails on its own leftovers.
    # PostgreSQL has its own deliberate entry point:
    # scripts/postgresql_integration_check.py with POSTGRES_TEST_URL.
    print(
        "config: DATABASE_URL игнорируется под тестами — прогон идёт на "
        "временной SQLite-базе.",
        file=sys.stderr,
    )
    DATABASE_URL = ""
if DATABASE_URL and not DATABASE_URL.startswith(
    ("postgresql://", "postgres://", "postgresql+psycopg://")
):
    raise RuntimeError(
        "DATABASE_URL must use postgresql:// or postgresql+psycopg://"
    )
def _environment_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} environment variable must be true or false")


DATABASE_REQUIRE_POSTGRES = _environment_bool(
    "DATABASE_REQUIRE_POSTGRES", False
)
if DATABASE_REQUIRE_POSTGRES and not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is required when DATABASE_REQUIRE_POSTGRES=true"
    )
SHOWDOWN_STATE_PATH = _data_path("SHOWDOWN_STATE_PATH", "showdown_state.json")

# Database / background workload. PostgreSQL is used whenever DATABASE_URL is
# configured; DB_PATH remains a lightweight fallback for isolated local tests.
DB_POOL_SIZE = _bounded_int("DB_POOL_SIZE", "4", 1, 32)
STATS_QUEUE_SIZE = _bounded_int("STATS_QUEUE_SIZE", "50000", 5000, 250000)
STATS_FLUSH_BATCH_SIZE = _bounded_int(
    "STATS_FLUSH_BATCH_SIZE", "2000", 100, 10000
)
STATS_RAW_RETENTION_DAYS = _bounded_int(
    "STATS_RAW_RETENTION_DAYS", "90", 14, 365
)
STATS_AGGREGATE_RETENTION_DAYS = _bounded_int(
    "STATS_AGGREGATE_RETENTION_DAYS", "730", 90, 3650
)
TASK_HEALTH_HEARTBEAT_SECONDS = _bounded_int(
    "TASK_HEALTH_HEARTBEAT_SECONDS", "60", 15, 600
)
OPERATION_RETENTION_DAYS = _bounded_int(
    "OPERATION_RETENTION_DAYS", "30", 7, 365
)
POLL_RETENTION_DAYS = _bounded_int(
    "POLL_RETENTION_DAYS", "180", 30, 1825
)
AUDIT_RETENTION_DAYS = _bounded_int(
    "AUDIT_RETENTION_DAYS", "365", 0, 3650
)

# Logging is applied at process start (restart_required). Empty LOG_DIR means
# stdout/journald only. Do not default this to ./logs — the systemd unit cannot
# write inside the project tree.
LOG_DIR = _setting_text("LOG_DIR").strip()
LOG_FORMAT = _setting_text("LOG_FORMAT", "text").strip().casefold()
if LOG_FORMAT not in {"text", "json"}:
    raise RuntimeError("LOG_FORMAT must be 'text' or 'json'")
LOG_LEVEL = _setting_text("LOG_LEVEL", "INFO").strip().upper()
if LOG_LEVEL not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
    raise RuntimeError("LOG_LEVEL must be a standard logging level")
LOG_LOGGER_LEVELS = _setting_text(
    "LOG_LOGGER_LEVELS",
    "discord.gateway=WARNING,discord.client=WARNING",
)


def _validate_public_settings() -> None:
    unknown = sorted(set(_SETTINGS) - _USED_SETTINGS)
    if unknown:
        raise RuntimeError(
            f"Unknown settings in {CONFIG_PATH.name}: {', '.join(unknown)}"
        )

    empty_roles = sorted(key for key, value in ROLES.items() if not value.strip())
    if empty_roles:
        raise RuntimeError(
            f"Role names must not be empty: {', '.join(empty_roles)}"
        )
    padded_names = sorted(
        f"{kind}.{key}"
        for kind, values in (("role", ROLES), ("channel", CHANNELS))
        for key, value in values.items()
        if value != value.strip()
    )
    if padded_names:
        raise RuntimeError(
            "Role/channel names must not start or end with whitespace: "
            + ", ".join(padded_names)
        )

    empty_channels = sorted(
        key
        for key, value in CHANNELS.items()
        if not value.strip()
        and not CHANNEL_IDS.get(key)
        and not CATEGORY_IDS.get(key)
    )
    if empty_channels:
        raise RuntimeError(
            "Channel/category name or ID is required for: "
            + ", ".join(empty_channels)
        )


_validate_public_settings()


def channel_id(name: str) -> int | None:
    """Актуальный Discord ID канала. Предпочтительнее ``CHANNEL_IDS[name]``."""

    from core.settings import runtime_channel_id

    return runtime_channel_id(name)


def role_ids(name: str) -> tuple[int, ...]:
    """Актуальный список Discord ID ролей. Предпочтительнее констант ``*_ROLE_IDS``."""

    from core.settings import runtime_role_ids

    return runtime_role_ids(name)


def setting_int(name: str) -> int:
    """Актуальное целое значение настройки. Предпочтительнее импорта константы."""

    from core.settings import runtime_setting_int

    return runtime_setting_int(name)


# Цвета (опционально)
COLORS = {
    "primary": 0x2C3E50,
    "gold": 0xD4AF37,
    "success": 0x27AE60,
    "error": 0xE74C3C,
    "info": 0x3498DB,
}
