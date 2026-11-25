from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import (
    ActionScope,
    Permission,
    can_act_on,
    ensure_default_grants,
    grant_role_permission,
    has_permission,
    revoke_role_permission,
)
from repositories.permissions import list_permissions_for_roles, list_role_permissions
from services.audit import list_decisions
from utils import access, database
from utils.access import MissingPermission, requires


def _role(role_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, name=name)


def _guild(roles: list[SimpleNamespace], guild_id: int = 1) -> SimpleNamespace:
    by_id = {role.id: role for role in roles}

    def get_role(role_id: int):
        return by_id.get(int(role_id))

    return SimpleNamespace(id=guild_id, roles=roles, get_role=get_role)


def _member(
    *,
    user_id: int,
    roles: list[SimpleNamespace],
    guild: SimpleNamespace,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        name="tester",
        display_name="tester",
        roles=roles,
        guild=guild,
        guild_id=guild.id,
    )


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "permissions.db",
        )
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_grant_and_revoke_role_permission(self) -> None:
        main = _role(10, "Main")
        high = _role(11, "High")
        guild = _guild([main, high])
        member = _member(user_id=20, roles=[main, high], guild=guild)

        self.assertFalse(await has_permission(member, Permission.AUDIT_VIEW))
        self.assertTrue(
            await grant_role_permission(1, Permission.AUDIT_VIEW, high.id)
        )
        self.assertEqual(
            await list_role_permissions(1, high.id),
            frozenset({Permission.AUDIT_VIEW.value}),
        )
        self.assertTrue(await has_permission(member, Permission.AUDIT_VIEW))
        self.assertTrue(
            await revoke_role_permission(1, Permission.AUDIT_VIEW, high.id)
        )
        self.assertFalse(await has_permission(member, Permission.AUDIT_VIEW))
        rows = await list_decisions(1, action="PERMISSIONS.GRANT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].after, "AUDIT_VIEW")

    async def test_denies_without_permission_and_names_it(self) -> None:
        main = _role(10, "Main")
        guild = _guild([main])
        member = _member(user_id=21, roles=[main], guild=guild)
        self.assertFalse(await has_permission(member, Permission.TREASURY_PAY))

        async def dummy(_interaction: object) -> bool:
            return True

        requires(Permission.TREASURY_PAY)(dummy)
        predicate = dummy.__discord_app_commands_checks__[0]
        interaction = SimpleNamespace(user=member, guild_id=1)
        with self.assertRaises(MissingPermission) as raised:
            await predicate(interaction)
        self.assertEqual(raised.exception.decision.code, "ACCESS.MISSING_PERMISSION")
        self.assertIn("Выплата из казны", str(raised.exception))
        self.assertNotIn("недостаточно прав", str(raised.exception).casefold())
        spec = raised.exception.to_spec("ref-1")
        self.assertEqual(spec.code, "ACCESS.MISSING_PERMISSION")
        self.assertIn("TREASURY_PAY", spec.summary)

    async def test_commander_cannot_manage_someone_elses_event(self) -> None:
        main = _role(10, "Main")
        guild = _guild([main])
        commander = _member(user_id=30, roles=[main], guild=guild)
        await grant_role_permission(1, Permission.EVENTS_MANAGE_OWN, main.id)
        own = ActionScope(kind="event", owner_id=30, resource_id="e1")
        foreign = ActionScope(kind="event", owner_id=99, resource_id="e2")
        self.assertTrue(await can_act_on(commander, Permission.EVENTS_MANAGE_OWN, own))
        self.assertFalse(
            await can_act_on(commander, Permission.EVENTS_MANAGE_OWN, foreign)
        )
        self.assertFalse(
            await can_act_on(commander, Permission.EVENTS_MANAGE_ALL, foreign)
        )

    async def test_notification_subscription_does_not_grant_permissions(self) -> None:
        main = _role(10, "Main")
        contract = _role(12, "Contract")
        vzp = _role(13, "VZP Team")
        guild = _guild([main, contract, vzp])
        await grant_role_permission(1, Permission.AUDIT_VIEW, contract.id)
        await grant_role_permission(1, Permission.EVENTS_CREATE, vzp.id)
        subscriber = _member(
            user_id=40, roles=[main, contract, vzp], guild=guild
        )
        self.assertEqual(
            await list_permissions_for_roles(1, {contract.id, vzp.id}),
            frozenset({Permission.AUDIT_VIEW.value, Permission.EVENTS_CREATE.value}),
        )
        self.assertFalse(await has_permission(subscriber, Permission.AUDIT_VIEW))
        self.assertFalse(await has_permission(subscriber, Permission.EVENTS_CREATE))

    async def test_owner_bypass_is_recorded_for_missing_permission(self) -> None:
        main = _role(10, "Main")
        guild = _guild([main])
        owner = _member(user_id=777, roles=[main], guild=guild)
        stranger = _member(user_id=888, roles=[main], guild=guild)

        async def dummy(_interaction: object) -> bool:
            return True

        requires(Permission.CONFIG_MANAGE)(dummy)
        predicate = dummy.__discord_app_commands_checks__[0]
        command = SimpleNamespace(checks=[predicate], qualified_name="audit list")
        tree = SimpleNamespace(walk_commands=lambda: [command])
        owner_interaction = SimpleNamespace(
            user=owner,
            guild_id=1,
            command=command,
            id=9001,
        )
        stranger_interaction = SimpleNamespace(
            user=stranger,
            guild_id=1,
            command=command,
            id=9002,
        )

        with patch.object(access, "BOT_OWNER_ID", 777):
            self.assertFalse(await has_permission(owner, Permission.CONFIG_MANAGE))
            self.assertEqual(access.install_bot_owner_command_bypass(tree), 1)
            self.assertTrue(await command.checks[0](owner_interaction))
            with self.assertRaises(MissingPermission):
                await command.checks[0](stranger_interaction)

        rows = await list_decisions(1, action="ACCESS.OWNER_BYPASS")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, "owner_bypass")
        self.assertEqual(rows[0].subject_id, "audit list")

    async def test_discord_administrator_does_not_receive_domain_permission(self) -> None:
        main = _role(10, "Main")
        guild = _guild([main])
        admin = _member(user_id=50, roles=[main], guild=guild)
        admin.guild_permissions = SimpleNamespace(administrator=True)
        self.assertFalse(await has_permission(admin, Permission.MEMBERS_MANAGE))

    async def test_default_grants_come_from_access_roles(self) -> None:
        high = _role(11, "High")
        owner_role = _role(14, "Owner")
        contract = _role(12, "Contract")
        guild = _guild([high, owner_role, contract])
        added = await ensure_default_grants(guild)
        self.assertGreater(added, 0)
        high_perms = await list_role_permissions(1, high.id)
        self.assertIn(Permission.AUDIT_VIEW.value, high_perms)
        self.assertIn(Permission.MEMBERS_MANAGE.value, high_perms)
        self.assertNotIn(Permission.TREASURY_PAY.value, high_perms)
        self.assertNotIn(Permission.MAINTENANCE.value, high_perms)
        treasury = await list_role_permissions(1, owner_role.id)
        self.assertIn(Permission.TREASURY_PAY.value, treasury)
        self.assertEqual(await list_role_permissions(1, contract.id), frozenset())
        self.assertEqual(await ensure_default_grants(guild), 0)


if __name__ == "__main__":
    unittest.main()
