"""Named domain permissions granted to Discord roles, not to people.

A check has four mandatory parts: active membership (stub until 4.2), the
named permission, action scope, and a current clearance (stub until 4.4).
Notification subscriptions never participate in either membership or grants.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Iterable

from infrastructure.database import connection as _connect
from infrastructure.transactions import begin_write
from repositories.permissions import (
    delete_grant,
    insert_grant,
    list_permissions_for_roles,
    list_role_permissions,
)

logger = logging.getLogger(__name__)


class Permission(StrEnum):
    MEMBERS_VIEW = "MEMBERS_VIEW"
    MEMBERS_MANAGE = "MEMBERS_MANAGE"
    APPLICATIONS_REVIEW = "APPLICATIONS_REVIEW"
    TRAINING_MANAGE = "TRAINING_MANAGE"
    CONTRACTS_MANAGE = "CONTRACTS_MANAGE"
    REPORTS_REVIEW = "REPORTS_REVIEW"
    EVENTS_CREATE = "EVENTS_CREATE"
    EVENTS_MANAGE_OWN = "EVENTS_MANAGE_OWN"
    EVENTS_MANAGE_ALL = "EVENTS_MANAGE_ALL"
    TREASURY_VIEW = "TREASURY_VIEW"
    TREASURY_APPROVE = "TREASURY_APPROVE"
    TREASURY_PAY = "TREASURY_PAY"
    INVENTORY_MANAGE = "INVENTORY_MANAGE"
    VEHICLES_MANAGE = "VEHICLES_MANAGE"
    PROGRESSION_MANAGE = "PROGRESSION_MANAGE"
    QUEUE_ASSIGN = "QUEUE_ASSIGN"
    AUDIT_VIEW = "AUDIT_VIEW"
    CONFIG_MANAGE = "CONFIG_MANAGE"
    MAINTENANCE = "MAINTENANCE"

    @property
    def label(self) -> str:
        return PERMISSION_TITLES[self]


PERMISSION_TITLES: Final[dict[Permission, str]] = {
    Permission.MEMBERS_VIEW: "Просмотр состава",
    Permission.MEMBERS_MANAGE: "Управление составом",
    Permission.APPLICATIONS_REVIEW: "Разбор заявок",
    Permission.TRAINING_MANAGE: "Учебные задания",
    Permission.CONTRACTS_MANAGE: "Контракты",
    Permission.REPORTS_REVIEW: "Проверка отчётов",
    Permission.EVENTS_CREATE: "Создание событий",
    Permission.EVENTS_MANAGE_OWN: "Свои события",
    Permission.EVENTS_MANAGE_ALL: "Все события",
    Permission.TREASURY_VIEW: "Просмотр казны",
    Permission.TREASURY_APPROVE: "Согласование выплат",
    Permission.TREASURY_PAY: "Выплата из казны",
    Permission.INVENTORY_MANAGE: "Склад",
    Permission.VEHICLES_MANAGE: "Транспорт",
    Permission.PROGRESSION_MANAGE: "Развитие семьи",
    Permission.QUEUE_ASSIGN: "Назначение очереди",
    Permission.AUDIT_VIEW: "Просмотр журнала решений",
    Permission.CONFIG_MANAGE: "Настройка бота",
    Permission.MAINTENANCE: "Техническое обслуживание",
}

STAFF_DEFAULT_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.MEMBERS_VIEW,
        Permission.MEMBERS_MANAGE,
        Permission.APPLICATIONS_REVIEW,
        Permission.TRAINING_MANAGE,
        Permission.CONTRACTS_MANAGE,
        Permission.REPORTS_REVIEW,
        Permission.EVENTS_CREATE,
        Permission.EVENTS_MANAGE_OWN,
        Permission.EVENTS_MANAGE_ALL,
        Permission.TREASURY_VIEW,
        Permission.INVENTORY_MANAGE,
        Permission.VEHICLES_MANAGE,
        Permission.PROGRESSION_MANAGE,
        Permission.QUEUE_ASSIGN,
        Permission.AUDIT_VIEW,
        Permission.CONFIG_MANAGE,
    }
)

TREASURY_DEFAULT_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.TREASURY_VIEW,
        Permission.TREASURY_APPROVE,
        Permission.TREASURY_PAY,
    }
)


@dataclass(frozen=True, slots=True)
class ActionScope:
    kind: str
    owner_id: int | None = None
    resource_id: str | None = None


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    code: str
    summary: str
    missing: Permission | None = None


def has_current_clearance(member: Any) -> bool:
    """Global access gate does not require an activity clearance.

    Per-activity clearances and re-review live in services/onboarding.py.
    Requiring a clearance here would lock /config and /audit.
    """

    return member is not None


def _permission_of(value: Permission | str) -> Permission:
    return value if isinstance(value, Permission) else Permission(value)


async def evaluate_access(member: Any, permission: Permission | str) -> AccessDecision:
    needed = _permission_of(permission)
    if member is None or getattr(member, "guild", None) is None:
        return AccessDecision(
            allowed=False,
            code="ACCESS.NOT_A_MEMBER",
            summary=(
                "Проверка полномочий доступна только участнику сервера. "
                "Действие не выполнено."
            ),
            missing=needed,
        )
    from utils.discord_lookup import (
        member_grant_role_ids,
        member_has_game_membership,
    )

    if not member_has_game_membership(member):
        return AccessDecision(
            allowed=False,
            code="ACCESS.NOT_A_MEMBER",
            summary=(
                "Нужно активное членство семьи. Действие не выполнено; "
                "подписка на уведомления членством не считается."
            ),
            missing=needed,
        )
    if not has_current_clearance(member):
        return AccessDecision(
            allowed=False,
            code="ACCESS.CLEARANCE_INACTIVE",
            summary=(
                "Допуск не действует. Действие не выполнено; "
                "обратитесь к старшему составу."
            ),
            missing=needed,
        )
    guild_id = int(getattr(member.guild, "id", 0) or getattr(member, "guild_id", 0))
    held = await list_permissions_for_roles(guild_id, member_grant_role_ids(member))
    if needed.value not in held:
        return AccessDecision(
            allowed=False,
            code="ACCESS.MISSING_PERMISSION",
            summary=(
                f"Не хватает полномочия «{needed.label}» ({needed.value}). "
                "Действие не выполнено."
            ),
            missing=needed,
        )
    return AccessDecision(
        allowed=True,
        code="ACCESS.OK",
        summary="",
        missing=None,
    )


def _scope_allows(permission: Permission, member: Any, scope: ActionScope) -> bool:
    member_id = int(getattr(member, "id", 0) or 0)
    if permission is Permission.EVENTS_MANAGE_OWN:
        return scope.kind == "event" and scope.owner_id == member_id
    if permission is Permission.EVENTS_MANAGE_ALL:
        return scope.kind == "event"
    return True


async def evaluate_action(
    member: Any,
    permission: Permission | str,
    scope: ActionScope,
) -> AccessDecision:
    needed = _permission_of(permission)
    decision = await evaluate_access(member, needed)
    if not decision.allowed:
        return decision
    if _scope_allows(needed, member, scope):
        return decision
    return AccessDecision(
        allowed=False,
        code="ACCESS.OUT_OF_SCOPE",
        summary=(
            f"Полномочие «{needed.label}» не покрывает этот объект. "
            "Действие не выполнено."
        ),
        missing=needed,
    )


async def has_permission(member: Any, permission: Permission | str) -> bool:
    return (await evaluate_access(member, permission)).allowed


async def can_act_on(
    member: Any, permission: Permission | str, scope: ActionScope
) -> bool:
    return (await evaluate_action(member, permission, scope)).allowed


async def grant_role_permission(
    guild_id: int,
    permission: Permission | str,
    role_id: int,
    *,
    actor_id: int = 0,
    actor_name: str = "system",
    source: str = "system",
) -> bool:
    needed = _permission_of(permission)
    from services.audit import record_decision

    async with _connect() as db:
        await begin_write(db, "permission-grant", f"{guild_id}:{role_id}:{needed.value}")
        inserted = await insert_grant(
            db, guild_id=guild_id, permission=needed.value, role_id=role_id
        )
        if inserted:
            await record_decision(
                guild_id=guild_id,
                action="PERMISSIONS.GRANT",
                subject_type="role",
                subject_id=str(role_id),
                actor_id=actor_id,
                actor_name=actor_name,
                before=None,
                after=needed.value,
                source=source,
                db=db,
            )
        await db.commit()
    return inserted


async def revoke_role_permission(
    guild_id: int,
    permission: Permission | str,
    role_id: int,
    *,
    actor_id: int = 0,
    actor_name: str = "system",
    source: str = "system",
) -> bool:
    needed = _permission_of(permission)
    from services.audit import record_decision

    async with _connect() as db:
        await begin_write(db, "permission-grant", f"{guild_id}:{role_id}:{needed.value}")
        removed = await delete_grant(
            db, guild_id=guild_id, permission=needed.value, role_id=role_id
        )
        if removed:
            await record_decision(
                guild_id=guild_id,
                action="PERMISSIONS.REVOKE",
                subject_type="role",
                subject_id=str(role_id),
                actor_id=actor_id,
                actor_name=actor_name,
                before=needed.value,
                after=None,
                source=source,
                db=db,
            )
        await db.commit()
    return removed


def _default_roles(guild: Any) -> tuple[list[Any], list[Any]]:
    from config import (
        HIGH_TOOLS_ALLOWED_ROLE_IDS,
        HIGH_TOOLS_ALLOWED_ROLE_NAMES,
        TREASURY_OWNER_ROLE_ID,
        TREASURY_OWNER_ROLE_NAME,
    )
    from utils.discord_lookup import (
        is_subscription_role,
        resolve_configured_roles,
    )

    try:
        from core.settings import get_registry

        staff_ids = get_registry().peek("HIGH_TOOLS_ALLOWED_ROLE_IDS")
        treasury_id = get_registry().peek("TREASURY_OWNER_ROLE_ID")
    except Exception:
        staff_ids = HIGH_TOOLS_ALLOWED_ROLE_IDS
        treasury_id = TREASURY_OWNER_ROLE_ID
    if not isinstance(staff_ids, (tuple, list)):
        staff_ids = HIGH_TOOLS_ALLOWED_ROLE_IDS
    treasury_ids = (int(treasury_id),) if isinstance(treasury_id, int) else ()
    if not treasury_ids and TREASURY_OWNER_ROLE_ID:
        treasury_ids = (TREASURY_OWNER_ROLE_ID,)
    staff = [
        role
        for role in resolve_configured_roles(
            guild, tuple(int(item) for item in staff_ids), HIGH_TOOLS_ALLOWED_ROLE_NAMES
        )
        if not is_subscription_role(guild, role)
    ]
    treasury = [
        role
        for role in resolve_configured_roles(
            guild, treasury_ids, (TREASURY_OWNER_ROLE_NAME,)
        )
        if not is_subscription_role(guild, role)
    ]
    return staff, treasury


async def _grant_bundle_if_empty(
    db: Any,
    guild_id: int,
    role_id: int,
    permissions: Iterable[Permission],
) -> list[str]:
    existing = await list_role_permissions(guild_id, role_id)
    added: list[str] = []
    if existing:
        return added
    for permission in permissions:
        inserted = await insert_grant(
            db, guild_id=guild_id, permission=permission.value, role_id=role_id
        )
        if inserted:
            added.append(permission.value)
    return added


async def ensure_default_grants(guild: Any) -> int:
    """Fill empty roles from [access] so an upgrade does not lock senior staff out."""

    guild_id = int(getattr(guild, "id", 0) or 0)
    if not guild_id:
        return 0
    staff_roles, treasury_roles = _default_roles(guild)
    bundles: dict[int, set[Permission]] = {}
    labels: dict[int, list[str]] = {}
    for role in staff_roles:
        bundles.setdefault(int(role.id), set()).update(STAFF_DEFAULT_PERMISSIONS)
        labels.setdefault(int(role.id), []).append("staff")
    for role in treasury_roles:
        bundles.setdefault(int(role.id), set()).update(TREASURY_DEFAULT_PERMISSIONS)
        labels.setdefault(int(role.id), []).append("treasury")
    added = 0
    from services.audit import record_decision

    async with _connect() as db:
        await begin_write(db, "permission-defaults", guild_id)
        seeded: list[str] = []
        for role_id, permissions in bundles.items():
            granted = await _grant_bundle_if_empty(db, guild_id, role_id, permissions)
            if granted:
                added += len(granted)
                seeded.append(f"{'+'.join(labels[role_id])}:{role_id}")
        if added:
            await record_decision(
                guild_id=guild_id,
                action="PERMISSIONS.SEED",
                subject_type="guild",
                subject_id=str(guild_id),
                actor_id=0,
                actor_name="system",
                after=",".join(seeded),
                source="system",
                db=db,
            )
        await db.commit()
    if added:
        logger.info(
            "Seeded %s default permission grant(s) for guild %s",
            added,
            guild_id,
        )
    elif not staff_roles and not treasury_roles:
        logger.warning(
            "Could not resolve HIGH_TOOLS or treasury roles to seed permissions "
            "in guild %s",
            guild_id,
        )
    return added
