"""Shared fixtures for Memphis workflow and idempotency tests.

Imported as a loose module from ``scripts/postgresql_integration_check.py``
(``tests/`` on ``sys.path``) so ``tests/__init__.py`` does not clear
``DATABASE_URL``. Unittest discovery loads the same file via the package.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import Permission, grant_role_permission
from core.settings import get_registry
from infrastructure.database import database_backend
from repositories.static_messages import set_guild_setting
from services.event_planning import ensure_event_integrations
from services.membership import register_member, reset_obligation_sources, verify_in_game
from services.queue import reset_queue_sources
from services.report_review import ensure_report_integrations
from services.treasury import ensure_treasury_integrations
from utils import database

_ALL_PERMISSIONS = tuple(Permission)


def role(role_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, name=name)


def guild(roles: list[SimpleNamespace], guild_id: int) -> SimpleNamespace:
    by_id = {item.id: item for item in roles}

    def get_role(role_id: int):
        return by_id.get(int(role_id))

    return SimpleNamespace(id=guild_id, roles=roles, get_role=get_role, members=[])


def actor(
    *,
    user_id: int,
    roles: list[SimpleNamespace],
    guild: SimpleNamespace,
    display_name: str = "officer",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        name=display_name,
        display_name=display_name,
        roles=roles,
        guild=guild,
        guild_id=guild.id,
    )


class MemphisBackendCase:
    """SQLite temp file for unittest; reuse the live backend for PostgreSQL."""

    guild_id: int
    family_role_id: int
    perm_role_id: int
    staff_id: int
    db_name: str

    async def open_backend(self) -> None:
        self._owns_sqlite = database_backend() != "postgresql"
        if self._owns_sqlite:
            await database.close_db()
            self.temp_dir = tempfile.TemporaryDirectory()
            self.path_patch = patch.object(
                database,
                "DB_PATH",
                Path(self.temp_dir.name) / self.db_name,
            )
            self.path_patch.start()
            await database.init_db()
        else:
            await database.reset_database_data()
        reset_obligation_sources()
        reset_queue_sources()
        ensure_report_integrations()
        ensure_event_integrations()
        ensure_treasury_integrations()
        get_registry().clear_cache()

    async def close_backend(self) -> None:
        reset_obligation_sources()
        reset_queue_sources()
        get_registry().clear_cache()
        if self._owns_sqlite:
            await database.close_db()
            self.path_patch.stop()
            self.temp_dir.cleanup()
        else:
            await database.reset_database_data()

    async def grant_staff(self) -> None:
        for permission in _ALL_PERMISSIONS:
            await grant_role_permission(self.guild_id, permission, self.perm_role_id)

    async def seed_upgrades(self, value: str = "air_cargo,territory_control") -> None:
        await set_guild_setting(self.guild_id, "profile.enabled_upgrades", value)

    def build_guild(self) -> None:
        family = role(self.family_role_id, "High")
        perms = role(self.perm_role_id, "Кадровик")
        self.family_role = family
        self.perm_role = perms
        self.guild = guild([family, perms], self.guild_id)
        self.staff = actor(
            user_id=self.staff_id, roles=[family, perms], guild=self.guild
        )

    async def register_verified(self, user_id: int, ic_name: str) -> None:
        await register_member(
            self.staff, self.guild_id, discord_id=user_id, ic_name=ic_name
        )
        await verify_in_game(self.staff, self.guild_id, user_id)

    def member_actor(self, user_id: int, display_name: str) -> SimpleNamespace:
        return actor(
            user_id=user_id,
            roles=[self.family_role],
            guild=self.guild,
            display_name=display_name,
        )
