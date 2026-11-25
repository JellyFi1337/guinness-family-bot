"""Pre-release readiness checks for a live guild.

Every dependency the bot silently assumes — that a channel exists, that it is
the right type, that the bot may post in it, that the Main role resolves, that
FFmpeg is installed, that the privileged intents were actually granted — used
to surface only as a failed interaction in front of a user. This module turns
all of them into one explicit report so the failure happens before release.

The checks are read-only: nothing here creates, edits or deletes anything.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass, field
from enum import Enum

import discord

from config import (
    AFK_VOICE_CHANNEL,
    AFK_VOICE_CHANNEL_ID,
    CATEGORY_IDS,
    CHANNEL_ID_ENV,
    CHANNEL_IDS,
    CHANNELS,
    FFMPEG_EXECUTABLE,
    HIGH_TOOLS_ALLOWED_ROLE_IDS,
    HIGH_TOOLS_ALLOWED_ROLE_NAMES,
    POLL_ALLOWED_ROLE_IDS,
    POLL_ALLOWED_ROLE_NAMES,
    RECRUITING_ROLE_IDS,
    RECRUITING_ROLE_NAMES,
    REQUEST_PING_ROLE_IDS,
    REQUEST_PING_ROLE_NAMES,
    ROLE_ID_ENV,
    ROLE_IDS,
    ROLES,
    TICKET_ASSIGNMENT_ROLE_HIERARCHY,
    TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS,
    TREASURY_OWNER_ROLE_ID,
    TREASURY_OWNER_ROLE_NAME,
)
from infrastructure.migrations import MIGRATIONS
from utils.database import get_database_pool_status
from utils.discord_lookup import get_category, get_channel, get_role


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    OK = "ok"


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    area: str
    target: str
    message: str


@dataclass(slots=True)
class PreflightReport:
    findings: list[Finding] = field(default_factory=list)
    name_only_routes: list[str] = field(default_factory=list)

    def add(self, severity: Severity, area: str, target: str, message: str) -> None:
        self.findings.append(Finding(severity, area, target, message))

    def critical(self) -> list[Finding]:
        return [item for item in self.findings if item.severity is Severity.CRITICAL]

    def warnings(self) -> list[Finding]:
        return [item for item in self.findings if item.severity is Severity.WARNING]

    def ready(self) -> bool:
        return not self.critical() and not self.name_only_routes

    def verdict(self) -> str:
        if not self.ready():
            return "not_ready"
        if self.warnings():
            return "limited"
        return "ready"

    def by_area(self, area: str) -> list[Finding]:
        return [
            item
            for item in self.findings
            if item.area == area and item.severity is not Severity.OK
        ]


# Guild-wide permissions the bot cannot operate without.
REQUIRED_GUILD_PERMISSIONS = (
    ("view_channel", "View Channel"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
    ("attach_files", "Attach Files"),
    ("read_message_history", "Read Message History"),
    ("manage_messages", "Manage Messages"),
    ("manage_channels", "Manage Channels"),
    ("manage_roles", "Manage Roles"),
    ("manage_threads", "Manage Threads"),
    ("create_public_threads", "Create Public Threads"),
    ("send_messages_in_threads", "Send Messages in Threads"),
    ("move_members", "Move Members"),
    ("connect", "Connect"),
    ("speak", "Speak"),
)

# Channel keys that must resolve to a voice channel rather than a text channel.
VOICE_CHANNEL_KEYS = frozenset({"voice_create_lobby"})

# Roles the bot has to be able to assign or reorder. Its own top role must sit
# above every one of them or the operation fails with a Discord 403 at runtime.
MANAGED_ROLE_KEYS = (
    "vip",
    "guest",
    "help",
    "contract",
    "young",
    "main",
    "friend",
    "super_friend",
)
OBSOLETE_ROLE_KEYS = (
    "vzp_team",
    "vzp_1",
    "vzp_2",
    "vzp_3",
    "lead_vzp",
)


def _check_intents(bot, report: PreflightReport) -> None:
    intents = getattr(bot, "intents", None)
    if intents is None:
        report.add(Severity.WARNING, "intents", "intents", "Не удалось прочитать intents клиента")
        return
    for attribute, label, why in (
        ("members", "Server Members Intent", "разрешение ролей и прав без лишних запросов"),
        (
            "message_content",
            "Message Content Intent",
            "без него Coins за сообщения не начисляются никогда",
        ),
        ("guilds", "Guilds Intent", "базовая работа с каналами"),
        ("voice_states", "Voice States Intent", "временные комнаты и голосовые Coins"),
    ):
        if getattr(intents, attribute, False):
            report.add(Severity.OK, "intents", label, "включён")
        else:
            report.add(Severity.CRITICAL, "intents", label, f"выключен — {why}")


def _check_channels(guild: discord.Guild, report: PreflightReport) -> None:
    for key in sorted(CHANNEL_ID_ENV):
        setting = CHANNEL_ID_ENV[key]
        configured_id = CHANNEL_IDS.get(key)
        channel = get_channel(guild, key)
        label = f"{key} ({CHANNELS.get(key, '?')})"
        if channel is None:
            reason = (
                f"ID {configured_id} не найден на сервере"
                if configured_id
                else f"канал с именем {CHANNELS.get(key)!r} не найден или имя неоднозначно"
            )
            report.add(Severity.CRITICAL, "channels", label, reason)
            continue
        expected_voice = key in VOICE_CHANNEL_KEYS
        is_voice = isinstance(channel, discord.VoiceChannel)
        if expected_voice and not is_voice:
            report.add(
                Severity.CRITICAL, "channels", label,
                f"ожидался голосовой канал, найден {type(channel).__name__}",
            )
            continue
        if not expected_voice and not isinstance(channel, discord.TextChannel):
            report.add(
                Severity.CRITICAL, "channels", label,
                f"ожидался текстовый канал, найден {type(channel).__name__}",
            )
            continue
        if not configured_id:
            report.name_only_routes.append(f"{setting} → #{channel.name} ({channel.id})")
        report.add(Severity.OK, "channels", label, f"#{channel.name}")

    if AFK_VOICE_CHANNEL_ID or AFK_VOICE_CHANNEL:
        afk = (
            guild.get_channel(AFK_VOICE_CHANNEL_ID)
            if AFK_VOICE_CHANNEL_ID
            else discord.utils.get(guild.voice_channels, name=AFK_VOICE_CHANNEL)
        )
        if afk is None:
            report.add(
                Severity.WARNING, "channels", f"afk ({AFK_VOICE_CHANNEL})",
                "AFK-канал не найден: голосовые Coins не будут его исключать",
            )
        else:
            if not AFK_VOICE_CHANNEL_ID:
                report.name_only_routes.append(
                    f"AFK_VOICE_CHANNEL_ID → #{afk.name} ({afk.id})"
                )
            report.add(Severity.OK, "channels", "afk", f"#{afk.name}")


def _check_categories(guild: discord.Guild, report: PreflightReport) -> None:
    setting_names = {
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
    for key in sorted(setting_names):
        configured_id = CATEGORY_IDS.get(key)
        category = get_category(guild, key)
        label = f"{key} ({CHANNELS.get(key, '?')})"
        if category is None:
            reason = (
                f"ID {configured_id} не найден или не является категорией"
                if configured_id
                else f"категория {CHANNELS.get(key)!r} не найдена или имя неоднозначно"
            )
            report.add(Severity.CRITICAL, "categories", label, reason)
            continue
        if not configured_id:
            report.name_only_routes.append(
                f"{setting_names[key]} → {category.name} ({category.id})"
            )
        report.add(Severity.OK, "categories", label, category.name)


def _check_roles(guild: discord.Guild, report: PreflightReport) -> None:
    for key in sorted(ROLES):
        configured_id = ROLE_IDS.get(key)
        role = get_role(guild, key)
        label = f"{key} ({ROLES[key]})"
        if key in OBSOLETE_ROLE_KEYS:
            if role is None:
                report.add(
                    Severity.OK,
                    "roles",
                    label,
                    "устаревший ключ: роль не требуется и на допуск не влияет",
                )
            else:
                report.add(
                    Severity.WARNING,
                    "roles",
                    label,
                    "ключ устарел: на приоритет и допуск не влияет, удалите вручную",
                )
            continue
        if role is None:
            reason = (
                f"ID {configured_id} не найден на сервере"
                if configured_id
                else f"роль {ROLES[key]!r} не найдена или имя неоднозначно"
            )
            severity = Severity.CRITICAL if key == "main" else Severity.WARNING
            report.add(severity, "roles", label, reason)
            continue
        if not configured_id:
            report.name_only_routes.append(f"{ROLE_ID_ENV[key]} → @{role.name} ({role.id})")
        report.add(Severity.OK, "roles", label, f"@{role.name}")

    if not ROLE_IDS.get("main"):
        report.add(
            Severity.CRITICAL, "roles", "MAIN_ROLE_ID",
            "не задан: без него видимость каналов в еженедельной статистике "
            "определяется по имени и любой канал может уйти в публичный рейтинг",
        )

    _check_role_list(guild, report, "REQUEST_PING_ROLE_IDS", "REQUEST_PING_ROLES",
                     REQUEST_PING_ROLE_IDS, REQUEST_PING_ROLE_NAMES)
    _check_role_list(guild, report, "HIGH_TOOLS_ALLOWED_ROLE_IDS", "HIGH_TOOLS_ALLOWED_ROLES",
                     HIGH_TOOLS_ALLOWED_ROLE_IDS, HIGH_TOOLS_ALLOWED_ROLE_NAMES)
    _check_role_list(guild, report, "POLL_ALLOWED_ROLE_IDS", "POLL_ALLOWED_ROLES",
                     POLL_ALLOWED_ROLE_IDS, POLL_ALLOWED_ROLE_NAMES)
    _check_role_list(guild, report, "RECRUITING_ROLE_IDS", "RECRUITING_ROLES",
                     RECRUITING_ROLE_IDS, RECRUITING_ROLE_NAMES)
    _check_role_list(
        guild, report,
        "TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS", "TICKET_ASSIGNMENT_ROLE_HIERARCHY",
        TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS, TICKET_ASSIGNMENT_ROLE_HIERARCHY,
    )

    treasury = (
        guild.get_role(TREASURY_OWNER_ROLE_ID)
        if TREASURY_OWNER_ROLE_ID
        else discord.utils.get(guild.roles, name=TREASURY_OWNER_ROLE_NAME)
    )
    if treasury is None:
        report.add(
            Severity.CRITICAL, "access", "TREASURY_OWNER_ROLE",
            f"роль {TREASURY_OWNER_ROLE_NAME!r} не найдена: /admin treasury_set недоступна никому",
        )
    else:
        if not TREASURY_OWNER_ROLE_ID:
            report.name_only_routes.append(
                f"TREASURY_OWNER_ROLE_ID → @{treasury.name} ({treasury.id})"
            )
        report.add(Severity.OK, "access", "TREASURY_OWNER_ROLE", f"@{treasury.name}")


def _check_role_list(
    guild: discord.Guild,
    report: PreflightReport,
    id_setting: str,
    name_setting: str,
    ids: tuple[int, ...],
    names: tuple[str, ...],
) -> None:
    if ids:
        missing = [str(role_id) for role_id in ids if guild.get_role(role_id) is None]
        if missing:
            report.add(
                Severity.CRITICAL, "access", id_setting,
                "ID не найдены на сервере: " + ", ".join(missing),
            )
        else:
            report.add(Severity.OK, "access", id_setting, f"{len(ids)} ролей")
        return
    resolved: list[str] = []
    missing_names: list[str] = []
    for name in names:
        matches = [role for role in guild.roles if role.name == name]
        if len(matches) == 1:
            resolved.append(f"@{matches[0].name} ({matches[0].id})")
        else:
            missing_names.append(name if not matches else f"{name} (дубликат имени)")
    if missing_names:
        report.add(
            Severity.CRITICAL, "access", name_setting,
            "не разрешаются по имени: " + ", ".join(missing_names),
        )
    if resolved:
        report.name_only_routes.append(f"{id_setting} → " + ", ".join(resolved))


def _check_permissions(guild: discord.Guild, report: PreflightReport) -> None:
    me = guild.me
    if me is None:
        report.add(
            Severity.CRITICAL, "permissions", "bot",
            "Бот не найден среди участников сервера",
        )
        return
    guild_permissions = me.guild_permissions
    if guild_permissions.administrator:
        report.add(Severity.OK, "permissions", "guild", "Administrator")
    else:
        missing = [
            label
            for attribute, label in REQUIRED_GUILD_PERMISSIONS
            if not getattr(guild_permissions, attribute, False)
        ]
        if missing:
            report.add(
                Severity.CRITICAL, "permissions", "guild",
                "не выданы: " + ", ".join(missing),
            )
        else:
            report.add(Severity.OK, "permissions", "guild", "все требуемые права выданы")

    for key in sorted(CHANNEL_ID_ENV):
        channel = get_channel(guild, key)
        if channel is None:
            continue
        permissions = channel.permissions_for(me)
        required: tuple[tuple[str, str], ...]
        if isinstance(channel, discord.VoiceChannel):
            required = (("view_channel", "View Channel"), ("connect", "Connect"),
                        ("speak", "Speak"), ("move_members", "Move Members"))
        else:
            required = (("view_channel", "View Channel"), ("send_messages", "Send Messages"),
                        ("embed_links", "Embed Links"), ("attach_files", "Attach Files"),
                        ("read_message_history", "Read Message History"))
        missing = [
            label for attribute, label in required
            if not getattr(permissions, attribute, False)
        ]
        if missing:
            report.add(
                Severity.CRITICAL, "permissions", f"#{channel.name}",
                "нет прав: " + ", ".join(missing),
            )

    for key in ("tickets_category", "voice_create_category", "groups_category",
                "birthday_fundraiser_category"):
        category = get_category(guild, key)
        if category is None:
            continue
        permissions = category.permissions_for(me)
        if not permissions.manage_channels:
            report.add(
                Severity.CRITICAL, "permissions", f"категория {category.name}",
                "нет права Manage Channels: бот не сможет создавать каналы в ней",
            )

    top_position = me.top_role.position
    blocked = [
        f"@{role.name}"
        for key in MANAGED_ROLE_KEYS
        if (role := get_role(guild, key)) is not None and role.position >= top_position
    ]
    if blocked:
        report.add(
            Severity.CRITICAL, "permissions", "иерархия ролей",
            f"роль бота (@{me.top_role.name}) не выше: " + ", ".join(blocked),
        )
    else:
        report.add(Severity.OK, "permissions", "иерархия ролей", f"@{me.top_role.name}")


def _check_runtime(report: PreflightReport) -> None:
    if shutil.which(FFMPEG_EXECUTABLE):
        report.add(Severity.OK, "runtime", "FFmpeg", FFMPEG_EXECUTABLE)
    else:
        report.add(
            Severity.CRITICAL, "runtime", "FFmpeg",
            f"{FFMPEG_EXECUTABLE!r} не найден в PATH: музыка работать не будет",
        )
    for module, label, severity in (
        ("yt_dlp", "yt-dlp", Severity.CRITICAL),
        ("nacl", "PyNaCl", Severity.CRITICAL),
        ("psycopg", "psycopg", Severity.WARNING),
    ):
        if importlib.util.find_spec(module) is not None:
            report.add(Severity.OK, "runtime", label, "установлен")
        else:
            report.add(severity, "runtime", label, "не установлен")


async def _check_database(report: PreflightReport) -> None:
    status = get_database_pool_status()
    backend = str(status.get("backend", "unknown"))
    if backend == "postgresql":
        report.add(
            Severity.OK, "database", "backend",
            f"PostgreSQL, пул {status.get('opened')}/{status.get('size')}",
        )
    else:
        report.add(
            Severity.CRITICAL, "database", "backend",
            f"{backend}: продакшен требует DATABASE_URL с PostgreSQL",
        )
    try:
        applied = await _applied_migration_version()
    except Exception as exc:  # pragma: no cover - diagnostics only
        report.add(Severity.WARNING, "database", "migrations", f"не удалось прочитать: {exc}")
        return
    expected = MIGRATIONS[-1].version
    if applied == expected:
        report.add(Severity.OK, "database", "migrations", f"версия {applied}")
    else:
        report.add(
            Severity.CRITICAL, "database", "migrations",
            f"применена версия {applied}, ожидается {expected}",
        )


async def _check_profile(guild: discord.Guild, report: PreflightReport) -> None:
    from core.profile import active_profile

    try:
        profile = await active_profile(guild.id)
    except Exception as exc:
        report.add(
            Severity.CRITICAL,
            "profile",
            "profile",
            f"профиль не прочитан: {exc}",
        )
        return
    if not profile.family_name.strip():
        report.add(
            Severity.CRITICAL,
            "profile",
            "family_name",
            "название семьи пустое",
        )
    else:
        report.add(Severity.OK, "profile", "family_name", profile.family_name)
    if profile.direction == "unset":
        report.add(
            Severity.WARNING,
            "profile",
            "direction",
            "направление семьи не задано",
        )
    if profile.member_limit is None:
        report.add(
            Severity.WARNING,
            "profile",
            "member_limit",
            "лимит состава требует настройки",
        )


async def _check_permission_grants(guild: discord.Guild, report: PreflightReport) -> None:
    from core.permissions import Permission
    from repositories.permissions import list_permissions_for_roles

    role_ids = [int(role.id) for role in getattr(guild, "roles", ())]
    held = await list_permissions_for_roles(guild.id, role_ids)
    if Permission.CONFIG_MANAGE.value in held:
        report.add(
            Severity.OK,
            "grants",
            Permission.CONFIG_MANAGE.value,
            "есть хотя бы у одной роли",
        )
        return
    report.add(
        Severity.CRITICAL,
        "grants",
        Permission.CONFIG_MANAGE.value,
        "ни у одной роли нет полномочия «Настройка бота»",
    )


async def _check_panel_channels(guild: discord.Guild, report: PreflightReport) -> None:
    from services.panels import known_panels, unavailable_reason
    from utils.discord_lookup import get_text_channel

    seen: set[str] = set()
    for definition in known_panels():
        if definition.channel_key in seen:
            continue
        seen.add(definition.channel_key)
        if await unavailable_reason(guild.id, definition.key):
            continue
        channel = get_text_channel(guild, definition.channel_key)
        if channel is None:
            report.add(
                Severity.WARNING,
                "panels",
                definition.key,
                f"канал {definition.channel_key} не найден",
            )


async def _check_online_source(guild: discord.Guild, report: PreflightReport) -> None:
    from core.profile import active_profile
    from services.external import majestic_integration_ready, majestic_missing_parts

    try:
        profile = await active_profile(guild.id)
    except Exception as exc:
        report.add(Severity.WARNING, "online", "online_source", str(exc))
        return
    if profile.online_source == "none":
        report.add(
            Severity.OK,
            "online",
            "online_source",
            "онлайн не запрашивается",
        )
        return
    if profile.online_source == "rage_master":
        report.add(
            Severity.OK,
            "online",
            "online_source",
            "источник — архивный master-list RAGE",
        )
        return
    if profile.online_source == "majestic_api":
        if await majestic_integration_ready(guild.id):
            report.add(
                Severity.OK,
                "online",
                "online_source",
                "интеграция Majestic настроена",
            )
            return
        missing = ", ".join(await majestic_missing_parts(guild.id))
        report.add(
            Severity.WARNING,
            "online",
            "online_source",
            f"интеграция выключена: не заполнены {missing}"
            if missing
            else "интеграция выключена: настройки API не заполнены",
        )
        return
    report.add(
        Severity.WARNING,
        "online",
        "online_source",
        f"источник {profile.online_source} недоступен",
    )


async def _check_outbox(report: PreflightReport) -> None:
    from repositories.operations import get_operational_health

    health = await get_operational_health()
    counts = {str(state): int(count) for state, count in health.get("outbox", [])}
    unfinished = counts.get("pending", 0) + counts.get("retry", 0) + counts.get(
        "processing", 0
    )
    dead = counts.get("dead", 0)
    if dead:
        report.add(
            Severity.WARNING,
            "outbox",
            "dead",
            f"мёртвых операций: {dead}",
        )
    if unfinished:
        report.add(
            Severity.WARNING,
            "outbox",
            "queue",
            f"незавершённых операций: {unfinished}",
        )
        return
    report.add(Severity.OK, "outbox", "queue", "очередь пуста")


async def _check_background_tasks(bot, report: PreflightReport) -> None:
    supervisor = getattr(bot, "task_supervisor", None)
    if supervisor is None:
        report.add(
            Severity.WARNING,
            "tasks",
            "supervisor",
            "диспетчер фоновых задач недоступен",
        )
        return
    snapshot = supervisor.snapshot()
    if snapshot.get("paused"):
        report.add(Severity.WARNING, "tasks", "supervisor", "фоновые задачи на паузе")
    jobs = snapshot.get("jobs")
    if isinstance(jobs, dict):
        stopped = [name for name, state in jobs.items() if state != "running"]
        if stopped:
            report.add(
                Severity.WARNING,
                "tasks",
                "jobs",
                "остановлены: " + ", ".join(stopped),
            )
            return
    report.add(Severity.OK, "tasks", "jobs", "фоновые задачи работают")


async def _applied_migration_version() -> int:
    from infrastructure.database import connection

    async with connection() as db:
        rows = await db.execute_fetchall(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        )
    return int(rows[0][0]) if rows else 0


async def run_preflight(bot, guild: discord.Guild) -> PreflightReport:
    """Run every readiness check and return the collected report."""

    report = PreflightReport()
    _check_intents(bot, report)
    _check_channels(guild, report)
    _check_categories(guild, report)
    _check_roles(guild, report)
    _check_permissions(guild, report)
    _check_runtime(report)
    await _check_database(report)
    await _check_profile(guild, report)
    await _check_permission_grants(guild, report)
    await _check_panel_channels(guild, report)
    await _check_online_source(guild, report)
    await _check_outbox(report)
    _check_background_tasks(bot, report)
    return report


def collect_config_ids(guild: discord.Guild) -> str:
    """Render a ready-to-paste TOML block with the guild's current object IDs.

    Filling `bot_config.toml` by hand for 33 channels, 9 categories and 17 roles
    is where transcription mistakes come from. This reads the live guild and
    prints exactly what belongs in the file.
    """

    lines: list[str] = [
        "# Сгенерировано /admin config_ids — сверьте и вставьте в bot_config.toml.",
        "# Пустое значение означает, что объект не найден по текущему имени.",
        "",
        "[channels]",
    ]
    for key in CHANNEL_ID_ENV:
        channel = get_channel(guild, key)
        value = str(channel.id) if channel is not None else ""
        comment = f"  # {CHANNELS.get(key, '')}" if channel is None else f"  # #{channel.name}"
        lines.append(f'{CHANNEL_ID_ENV[key]} = "{value}"{comment}')

    afk = (
        guild.get_channel(AFK_VOICE_CHANNEL_ID)
        if AFK_VOICE_CHANNEL_ID
        else discord.utils.get(guild.voice_channels, name=AFK_VOICE_CHANNEL)
    )
    lines.append(f'AFK_VOICE_CHANNEL_ID = "{afk.id if afk else ""}"')

    lines += ["", "[categories]"]
    for key, setting in (
        ("high_tools_category", "CATEGORY_HIGH_TOOLS_ID"),
        ("public_category", "CATEGORY_PUBLIC_ID"),
        ("family_category", "CATEGORY_FAMILY_ID"),
        ("interactive_category", "CATEGORY_INTERACTIVE_ID"),
        ("tickets_category", "CATEGORY_TICKETS_ID"),
        ("birthday_fundraiser_category", "CATEGORY_BIRTHDAY_FUNDRAISER_ID"),
        ("voice_create_category", "CATEGORY_VOICE_CREATE_ID"),
        ("groups_category", "CATEGORY_GROUPS_ID"),
        ("developer_category", "CATEGORY_DEVELOPER_ID"),
    ):
        category = get_category(guild, key)
        suffix = f"  # {category.name}" if category is not None else f"  # {CHANNELS.get(key, '')}"
        lines.append(f'{setting} = "{category.id if category else ""}"{suffix}')

    lines += ["", "[roles]"]
    for key, setting in ROLE_ID_ENV.items():
        role = get_role(guild, key)
        suffix = f"  # @{role.name}" if role is not None else f"  # {ROLES[key]}"
        lines.append(f'{setting} = "{role.id if role else ""}"{suffix}')

    lines += ["", "[access]"]
    for setting, names in (
        ("REQUEST_PING_ROLE_IDS", REQUEST_PING_ROLE_NAMES),
        ("HIGH_TOOLS_ALLOWED_ROLE_IDS", HIGH_TOOLS_ALLOWED_ROLE_NAMES),
        ("POLL_ALLOWED_ROLE_IDS", POLL_ALLOWED_ROLE_NAMES),
        ("RECRUITING_ROLE_IDS", RECRUITING_ROLE_NAMES),
        ("TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS", TICKET_ASSIGNMENT_ROLE_HIERARCHY),
    ):
        ids: list[str] = []
        for name in names:
            matches = [role for role in guild.roles if role.name == name]
            if len(matches) == 1:
                ids.append(str(matches[0].id))
        lines.append(f'{setting} = "{",".join(ids)}"  # {", ".join(names)}')
    treasury = discord.utils.get(guild.roles, name=TREASURY_OWNER_ROLE_NAME)
    lines.append(f'TREASURY_OWNER_ROLE_ID = "{treasury.id if treasury else ""}"')
    return "\n".join(lines)
