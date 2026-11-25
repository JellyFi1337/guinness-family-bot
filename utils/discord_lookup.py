import logging

import discord

from config import (
    CATEGORY_IDS,
    CHANNEL_IDS,
    CHANNELS,
    REQUEST_PING_ROLE_IDS,
    REQUEST_PING_ROLE_NAMES,
    HIGH_TOOLS_ALLOWED_ROLE_IDS,
    HIGH_TOOLS_ALLOWED_ROLE_NAMES,
    ROLES,
    ROLE_IDS,
)

logger = logging.getLogger(__name__)


def _unique_named(items, name: str, *, kind: str, guild_id: int):
    matches = [item for item in items if item.name == name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidates = ", ".join(
            f"id={item.id} name={item.name!r}" for item in matches
        )
        logger.error(
            "Ambiguous %s name %r in guild %s; candidates: %s",
            kind,
            name,
            guild_id,
            candidates,
        )
    return None


def _configured_id(runtime_value: int | None, snapshot: int | None) -> int | None:
    if runtime_value is not None:
        return runtime_value
    return snapshot


def _resolve_id_then_name(
    *,
    configured_id: int | None,
    name: str | None,
    found_by_id,
    items,
    kind: str,
    guild_id: int,
):
    if found_by_id is not None:
        return found_by_id
    if not name:
        return None
    match = _unique_named(items, name, kind=kind, guild_id=guild_id)
    if match is not None and configured_id:
        logger.warning(
            "Configured %s id=%s was not found in guild %s; "
            "using unique name %r id=%s",
            kind,
            configured_id,
            guild_id,
            name,
            getattr(match, "id", None),
        )
    return match


def get_channel(guild: discord.Guild, key: str):
    try:
        from config import channel_id as runtime_channel_id

        runtime_id = runtime_channel_id(key)
    except Exception:
        runtime_id = None
    channel_id = _configured_id(runtime_id, CHANNEL_IDS.get(key))
    found = guild.get_channel(channel_id) if channel_id else None
    return _resolve_id_then_name(
        configured_id=channel_id,
        name=CHANNELS.get(key),
        found_by_id=found,
        items=getattr(guild, "channels", ()),
        kind=f"channel:{key}",
        guild_id=getattr(guild, "id", 0),
    )


def get_text_channel(guild: discord.Guild, key: str) -> discord.TextChannel | None:
    channel = get_channel(guild, key)
    return channel if isinstance(channel, discord.TextChannel) else None


def get_voice_channel(guild: discord.Guild, key: str) -> discord.VoiceChannel | None:
    channel = get_channel(guild, key)
    return channel if isinstance(channel, discord.VoiceChannel) else None


def get_category(guild: discord.Guild, key: str) -> discord.CategoryChannel | None:
    try:
        from core.settings import runtime_category_id

        runtime_id = runtime_category_id(key)
    except Exception:
        runtime_id = None
    category_id = _configured_id(runtime_id, CATEGORY_IDS.get(key))
    found = guild.get_channel(category_id) if category_id else None
    if found is not None and isinstance(found, discord.abc.GuildChannel):
        if not isinstance(found, discord.CategoryChannel):
            found = None
    resolved = _resolve_id_then_name(
        configured_id=category_id,
        name=CHANNELS.get(key),
        found_by_id=found,
        items=getattr(guild, "categories", ()),
        kind=f"category:{key}",
        guild_id=getattr(guild, "id", 0),
    )
    if resolved is None or isinstance(resolved, discord.CategoryChannel):
        return resolved
    if isinstance(resolved, discord.abc.GuildChannel):
        return None
    return resolved


def get_role(guild: discord.Guild, key: str) -> discord.Role | None:
    """Resolve a canonical role by configured ID, falling back to its name.

    A configured ID is authoritative for the same reason it is for channels:
    it survives a rename and cannot be shadowed by a second role carrying the
    same name. The name lookup stays available so an unconfigured deployment
    keeps working, but it refuses to guess when the name is ambiguous.
    """

    try:
        from core.settings import runtime_role_id

        runtime_id = runtime_role_id(key)
    except Exception:
        runtime_id = None
    role_id = _configured_id(runtime_id, ROLE_IDS.get(key))
    found = guild.get_role(role_id) if role_id else None
    return _resolve_id_then_name(
        configured_id=role_id,
        name=ROLES.get(key),
        found_by_id=found,
        items=getattr(guild, "roles", ()),
        kind=f"role:{key}",
        guild_id=getattr(guild, "id", 0),
    )


def role_label(key: str) -> str:
    """Human-readable name of a canonical role for user-facing messages."""

    return ROLES.get(key, key)


def request_ping_roles(guild: discord.Guild) -> list[discord.Role]:
    roles = []
    if REQUEST_PING_ROLE_IDS:
        for role_id in REQUEST_PING_ROLE_IDS:
            role = guild.get_role(role_id)
            if role is not None:
                roles.append(role)
        return roles
    for name in REQUEST_PING_ROLE_NAMES:
        role = _unique_named(
            guild.roles,
            name,
            kind="request-ping-role",
            guild_id=getattr(guild, "id", 0),
        )
        if role is not None:
            roles.append(role)
    return roles


def request_ping_content(guild: discord.Guild) -> str | None:
    roles = request_ping_roles(guild)
    return " ".join(role.mention for role in roles) or None


def request_ping_mentions(guild: discord.Guild) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        roles=request_ping_roles(guild), users=False, everyone=False
    )


def _member_has_configured_role(
    member: discord.Member, role_ids: tuple[int, ...], role_names: tuple[str, ...]
) -> bool:
    if role_ids:
        allowed_ids = set(role_ids)
        return any(role.id in allowed_ids for role in member.roles)
    allowed_names = set(role_names)
    return any(role.name in allowed_names for role in member.roles)


GAME_MEMBERSHIP_ROLE_KEYS = frozenset(
    {
        "guest",
        "young",
        "main",
        "friend",
        "super_friend",
        "staff",
        "high",
        "owner",
        "dep_owner",
        "lead_vzp",
        "vip",
    }
)
SUBSCRIPTION_ROLE_KEYS = frozenset(
    {
        "contract",
        "vzp_team",
        "vzp_1",
        "vzp_2",
        "vzp_3",
    }
)


def resolve_configured_roles(
    guild: discord.Guild,
    role_ids: tuple[int, ...],
    role_names: tuple[str, ...],
) -> list[discord.Role]:
    roles: list[discord.Role] = []
    seen: set[int] = set()
    if role_ids:
        for role_id in role_ids:
            role = guild.get_role(int(role_id))
            if role is None or role.id in seen:
                continue
            roles.append(role)
            seen.add(role.id)
        return roles
    for name in role_names:
        if not name:
            continue
        role = _unique_named(
            getattr(guild, "roles", ()),
            name,
            kind="access-role",
            guild_id=getattr(guild, "id", 0),
        )
        if role is None or role.id in seen:
            continue
        roles.append(role)
        seen.add(role.id)
    return roles


def _roles_for_keys(guild: discord.Guild, keys: frozenset[str]) -> list[discord.Role]:
    roles: list[discord.Role] = []
    seen: set[int] = set()
    for key in keys:
        role = get_role(guild, key)
        if role is None or role.id in seen:
            continue
        roles.append(role)
        seen.add(role.id)
    return roles


def subscription_role_ids(guild: discord.Guild) -> set[int]:
    return {role.id for role in _roles_for_keys(guild, SUBSCRIPTION_ROLE_KEYS)}


def is_subscription_role(guild: discord.Guild, role: discord.Role) -> bool:
    return role.id in subscription_role_ids(guild)


def member_grant_role_ids(member: discord.Member) -> set[int]:
    blocked = subscription_role_ids(member.guild)
    return {role.id for role in member.roles if role.id not in blocked}


def member_has_game_membership(member: discord.Member) -> bool:
    """Stub for 4.2: a configured family role, never a notification subscription."""

    configured = _roles_for_keys(member.guild, GAME_MEMBERSHIP_ROLE_KEYS)
    if not configured:
        return True
    held = {role.id for role in member.roles}
    return any(role.id in held for role in configured)


def member_has_high_tools_access(member: discord.Member) -> bool:
    return _member_has_configured_role(
        member, HIGH_TOOLS_ALLOWED_ROLE_IDS, HIGH_TOOLS_ALLOWED_ROLE_NAMES
    )


def interaction_category(interaction: discord.Interaction) -> discord.CategoryChannel | None:
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        channel = channel.parent
    return getattr(channel, "category", None)


def is_high_tools_interaction(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    category = interaction_category(interaction)
    if category is None:
        return False
    configured_id = CATEGORY_IDS.get("high_tools_category")
    if configured_id:
        return category.id == configured_id
    return category.name == CHANNELS["high_tools_category"]


def has_high_tools_access(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and member_has_high_tools_access(member)
