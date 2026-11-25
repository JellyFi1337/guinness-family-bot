from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import Permission
from core.settings import get_registry
from infrastructure.database import connection
from repositories.settings import get_runtime_setting
from services.provisioning import (
    ACTION_BIND,
    ACTION_UNSET,
    SCAN_AMBIGUOUS,
    SetupError,
    apply_confirmed_plan,
    assert_config_manage,
    build_plan,
    choose_object,
    load_wizard_state,
    mark_confirmed,
    role_inventory,
    save_wizard_state,
    set_step,
    start_or_resume,
    structure_inventory,
    GuildObject,
    GuildSnapshot,
)
from utils import database
from utils.access import MissingPermission, requires


def _role(role_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, name=name)


def _guild(roles: list[SimpleNamespace], guild_id: int = 77) -> SimpleNamespace:
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


def _empty_snapshot(guild_id: int = 77) -> GuildSnapshot:
    return GuildSnapshot(guild_id=guild_id)


async def _outbox_kinds(guild_id: int) -> list[str]:
    async with connection() as db:
        rows = await db.execute_fetchall(
            "SELECT kind FROM operation_outbox WHERE guild_id = ?",
            (guild_id,),
        )
    return [str(row[0]) for row in rows]


class ProvisioningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "provisioning.db",
        )
        self.path_patch.start()
        await database.init_db()
        get_registry().clear_cache()

    async def asyncTearDown(self) -> None:
        get_registry().clear_cache()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_plan_does_not_apply(self) -> None:
        snapshot = _empty_snapshot()
        state, resumed = await start_or_resume(
            77, snapshot, actor_id=5, actor_name="Admin"
        )
        self.assertFalse(resumed)
        plan = build_plan(state, snapshot)
        self.assertGreater(plan.create_channels, 0)
        self.assertIn("каналов", plan.preview_text())
        self.assertIn("старт-и-правила", plan.preview_text())
        self.assertIn("очередь-сс", plan.preview_text())
        self.assertEqual(await _outbox_kinds(77), [])
        self.assertIsNone(await get_runtime_setting(77, "CHANNEL_HELP_ID"))

    async def test_resume_interrupted_wizard(self) -> None:
        snapshot = _empty_snapshot()
        state, _ = await start_or_resume(77, snapshot, actor_id=5, actor_name="Admin")
        correlation = state.correlation_id
        state = set_step(state, "structure")
        await save_wizard_state(77, state, actor_id=5, actor_name="Admin")

        resumed_state, resumed = await start_or_resume(
            77, snapshot, actor_id=5, actor_name="Admin"
        )
        self.assertTrue(resumed)
        self.assertEqual(resumed_state.step, "structure")
        self.assertEqual(resumed_state.correlation_id, correlation)
        loaded = await load_wizard_state(77)
        assert loaded is not None
        self.assertEqual(loaded.step, "structure")

    async def test_rerun_does_not_duplicate_bound_objects(self) -> None:
        next_id = 2000
        channels: list[GuildObject] = []
        categories: list[GuildObject] = []
        roles: list[GuildObject] = []
        registry = get_registry()
        for item in structure_inventory():
            next_id += 1
            obj = GuildObject(
                next_id,
                item.expected_name,
                "category" if item.kind == "category" else item.channel_type,
            )
            if item.kind == "category":
                categories.append(obj)
            else:
                channels.append(obj)
            await registry.set(77, item.setting_key, obj.id, updated_by="5")
        for item in role_inventory():
            next_id += 1
            obj = GuildObject(next_id, item.expected_name, "role")
            roles.append(obj)
            await registry.set(77, item.setting_key, obj.id, updated_by="5")
        snapshot = GuildSnapshot(
            guild_id=77,
            channels=tuple(channels),
            categories=tuple(categories),
            roles=tuple(roles),
        )
        state, _ = await start_or_resume(77, snapshot, actor_id=5, actor_name="Admin")
        plan = build_plan(state, snapshot)
        self.assertEqual(plan.create_channels, 0)
        result = await apply_confirmed_plan(
            77,
            mark_confirmed(state),
            snapshot,
            actor_id=5,
            actor_name="Admin",
        )
        self.assertEqual(result.created_enqueued, 0)
        self.assertEqual(await _outbox_kinds(77), [])

        again, _ = await start_or_resume(77, snapshot, actor_id=5, actor_name="Admin")
        self.assertEqual(build_plan(again, snapshot).create_channels, 0)
        second = await apply_confirmed_plan(
            77,
            mark_confirmed(again),
            snapshot,
            actor_id=5,
            actor_name="Admin",
        )
        self.assertEqual(second.created_enqueued, 0)
        self.assertEqual(await _outbox_kinds(77), [])

    async def test_ambiguous_name_requires_explicit_choice(self) -> None:
        help_item = next(item for item in structure_inventory() if item.key == "help")
        snapshot = GuildSnapshot(
            guild_id=77,
            channels=(
                GuildObject(201, help_item.expected_name, "text"),
                GuildObject(202, help_item.expected_name, "text"),
            ),
        )
        state, _ = await start_or_resume(77, snapshot, actor_id=5, actor_name="Admin")
        help_choice = state.structure["help"]
        self.assertEqual(help_choice.resolved, SCAN_AMBIGUOUS)
        self.assertEqual(help_choice.action, ACTION_UNSET)
        with self.assertRaises(SetupError) as raised:
            choose_object(state, "help", ACTION_BIND, snapshot=snapshot)
        self.assertEqual(raised.exception.code, "SETUP.AMBIGUOUS")
        plan = build_plan(state, snapshot)
        self.assertTrue(any("неоднознач" in item for item in plan.attention))
        help_line = next(line for line in plan.items if line.startswith("help:"))
        self.assertIn("ambiguous", help_line)
        self.assertIn("unset", help_line)
        confirmed = mark_confirmed(state)
        with self.assertRaises(SetupError) as apply_raised:
            await apply_confirmed_plan(
                77, confirmed, snapshot, actor_id=5, actor_name="Admin"
            )
        self.assertEqual(apply_raised.exception.code, "SETUP.AMBIGUOUS")
        self.assertIsNone(await get_runtime_setting(77, "CHANNEL_HELP_ID"))

        bound = choose_object(
            state, "help", ACTION_BIND, target_id=202, snapshot=snapshot
        )
        self.assertEqual(bound.structure["help"].action, ACTION_BIND)
        self.assertEqual(bound.structure["help"].target_id, 202)

    async def test_denied_without_permission(self) -> None:
        main = _role(10, "Main")
        guild = _guild([main])
        stranger = _member(user_id=88, roles=[main], guild=guild)
        with self.assertRaises(SetupError) as raised:
            await assert_config_manage(stranger)
        self.assertEqual(raised.exception.code, "ACCESS.MISSING_PERMISSION")

        async def dummy(_interaction: object) -> bool:
            return True

        requires(Permission.CONFIG_MANAGE)(dummy)
        predicate = dummy.__discord_app_commands_checks__[0]
        command = SimpleNamespace(checks=[predicate], qualified_name="setup start")
        interaction = SimpleNamespace(
            user=stranger,
            guild_id=77,
            command=command,
            id=9003,
        )
        with self.assertRaises(MissingPermission):
            await predicate(interaction)

    async def test_apply_without_confirm_is_rejected(self) -> None:
        snapshot = _empty_snapshot()
        state, _ = await start_or_resume(77, snapshot, actor_id=5, actor_name="Admin")
        with self.assertRaises(SetupError) as raised:
            await apply_confirmed_plan(
                77, state, snapshot, actor_id=5, actor_name="Admin"
            )
        self.assertEqual(raised.exception.code, "SETUP.NOT_CONFIRMED")
        self.assertEqual(await _outbox_kinds(77), [])


if __name__ == "__main__":
    unittest.main()
