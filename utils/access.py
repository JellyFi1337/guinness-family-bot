from __future__ import annotations

import logging

import discord
from discord import app_commands

from config import BOT_OWNER_ID
from core.permissions import (
    AccessDecision,
    ActionScope,
    Permission,
    evaluate_access,
    evaluate_action,
)
from utils.discord_lookup import member_has_high_tools_access

logger = logging.getLogger(__name__)


def is_bot_owner(user: discord.abc.User | int | None) -> bool:
    user_id = user if isinstance(user, int) else getattr(user, "id", None)
    return BOT_OWNER_ID is not None and user_id == BOT_OWNER_ID


def has_administrator_access(user: discord.abc.User | None) -> bool:
    permissions = getattr(user, "guild_permissions", None)
    return is_bot_owner(user) or bool(
        isinstance(user, discord.Member)
        and getattr(permissions, "administrator", False)
    )


def has_staff_access(user: discord.abc.User | None) -> bool:
    return has_administrator_access(user) or (
        isinstance(user, discord.Member) and member_has_high_tools_access(user)
    )


def administrator_or_owner():
    """Operational commands only (``/admin``, ``/dev``). Domain commands use requires()."""

    async def predicate(interaction: discord.Interaction) -> bool:
        return has_administrator_access(interaction.user)

    return app_commands.check(predicate)


def bot_owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_bot_owner(interaction.user)

    return app_commands.check(predicate)


def staff_or_owner():
    """Operational commands only (``/admin``, ``/dev``). Domain commands use requires()."""

    async def predicate(interaction: discord.Interaction) -> bool:
        return has_staff_access(interaction.user)

    return app_commands.check(predicate)


class MissingPermission(app_commands.CheckFailure):
    """Named domain refusal. Never a generic «недостаточно прав»."""

    def __init__(self, decision: AccessDecision) -> None:
        super().__init__(decision.summary)
        self.decision = decision

    def to_spec(self, reference: str):
        from ui.templates import missing_permission

        return missing_permission(self.decision, reference=reference)


async def has_audit_view(user: discord.abc.User | None) -> bool:
    from core.permissions import has_permission

    return isinstance(user, discord.Member) and await has_permission(
        user, Permission.AUDIT_VIEW
    )


def requires(permission: Permission, *, scope=None):
    """Domain check: membership, named permission, optional scope, clearance."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if scope is None:
            decision = await evaluate_access(member, permission)
        else:
            resolved = scope(interaction) if callable(scope) else scope
            if not isinstance(resolved, ActionScope):
                resolved = ActionScope(kind=str(resolved))
            decision = await evaluate_action(member, permission, resolved)
        if decision.allowed:
            return True
        raise MissingPermission(decision)

    return app_commands.check(predicate)


def audit_view():
    return requires(Permission.AUDIT_VIEW)


async def _record_owner_bypass(interaction: discord.Interaction) -> None:
    try:
        from services.audit import record_owner_bypass

        await record_owner_bypass(interaction)
    except Exception:
        logger.exception("Could not record owner command bypass")


def install_bot_owner_command_bypass(tree: app_commands.CommandTree) -> int:
    """Wrap every application-command check with one global owner bypass.

    This also covers checks supplied by discord.py itself, including command
    cooldowns. Business rules inside a command (positive amounts, ownership of
    an object, sufficient balance, voice presence) intentionally remain active.
    """

    wrapped_count = 0
    for command in tree.walk_commands():
        checks = getattr(command, "checks", None)
        if not checks:
            continue
        for index, predicate in enumerate(tuple(checks)):
            if getattr(predicate, "__guinness_owner_bypass__", False):
                continue

            async def owner_aware_check(
                interaction: discord.Interaction,
                _predicate=predicate,
            ) -> bool:
                if is_bot_owner(interaction.user):
                    allowed = False
                    try:
                        allowed = bool(
                            await discord.utils.maybe_coroutine(
                                _predicate, interaction
                            )
                        )
                    except Exception:
                        allowed = False
                    if not allowed:
                        await _record_owner_bypass(interaction)
                    return True
                return bool(
                    await discord.utils.maybe_coroutine(_predicate, interaction)
                )

            # Marker on a function object; not something the type system models.
            owner_aware_check.__guinness_owner_bypass__ = True  # type: ignore[attr-defined]
            checks[index] = owner_aware_check
            wrapped_count += 1
    return wrapped_count
