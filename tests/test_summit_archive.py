from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cogs.summits as summits_mod
from config import COMMAND_COOLDOWN_DEFAULTS
from infrastructure.database import connection
from infrastructure.migrations import MIGRATIONS
from infrastructure.postgresql import translate_sql
from repositories.summits import is_summit_list_archived, is_vzp_list_archived
from services.preflight import MANAGED_ROLE_KEYS, OBSOLETE_ROLE_KEYS
from services.summit_archive import (
    archive_open_legacy_gatherings,
    archived_gathering_spec,
    archived_interaction_spec,
)
from ui.command_registry import COMMANDS, cmd
from utils import database

GUILD_ID = 10_200


class SummitArchiveSchemaTests(unittest.TestCase):
    def test_migration_52_translates_to_postgresql(self) -> None:
        migration = next(item for item in MIGRATIONS if item.version == 52)
        self.assertEqual(migration.columns["summit_lists"]["archived_at"], "TEXT")
        self.assertEqual(migration.columns["summit_vzp_lists"]["archived_at"], "TEXT")
        joined = "\n".join(
            translate_sql(statement, include_returning=False)
            for statement in migration.statements
        )
        self.assertNotIn("AUTOINCREMENT", joined)
        self.assertNotIn("?", joined)
        self.assertIn("idx_summit_lists_archived", joined)
        self.assertIn("idx_summit_vzp_lists_archived", joined)

    def test_vzp_commands_and_priority_are_gone(self) -> None:
        source = inspect.getsource(summits_mod)
        self.assertNotIn("def _priority(", source)
        for name in (
            "summit vzp_create",
            "summit vzp_take",
            "summit vzp_leave",
            "summit vzp_kick",
            "summit vzp_promote",
            "summit vzp_expand",
            "summit vzp_thread_add",
        ):
            self.assertNotIn(name, COMMANDS)
        self.assertIn("summit list_create", COMMANDS)
        self.assertIn("summit contract_create", COMMANDS)

    def test_cooldowns_moved_onto_new_commands(self) -> None:
        self.assertEqual(COMMAND_COOLDOWN_DEFAULTS["event_create"], 30)
        self.assertEqual(COMMAND_COOLDOWN_DEFAULTS["contract_start"], 30)
        self.assertEqual(
            COMMAND_COOLDOWN_DEFAULTS["event_create"],
            COMMAND_COOLDOWN_DEFAULTS["summit_vzp"],
        )
        self.assertEqual(
            COMMAND_COOLDOWN_DEFAULTS["contract_start"],
            COMMAND_COOLDOWN_DEFAULTS["summit_contract"],
        )

    def test_obsolete_vzp_roles_are_not_managed(self) -> None:
        for key in OBSOLETE_ROLE_KEYS:
            self.assertNotIn(key, MANAGED_ROLE_KEYS)

    def test_archived_spec_points_to_new_commands(self) -> None:
        spec = archived_gathering_spec(reference="#1")
        self.assertEqual(spec.code, "SUMMIT.ARCHIVED")
        self.assertIn(cmd("event create"), spec.next_step or "")
        self.assertIn(cmd("contract start"), spec.next_step or "")
        self.assertNotIn("VZP", spec.summary or "")
        self.assertNotIn("ВЗП", spec.summary or "")


class SummitArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "summit-archive.db",
        )
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_open_gathering_is_closed_with_roster_kept(self) -> None:
        gathering_id = await database.create_summit_list(
            "Ночной сбор",
            4,
            501,
            99,
            kind="vzp_gathering",
            guild_id=GUILD_ID,
        )
        await database.set_summit_list_message(gathering_id, 777)
        self.assertEqual(
            (await database.join_summit_list(gathering_id, 11, "one", 0))[0],
            "joined",
        )
        self.assertEqual(
            (await database.join_summit_list(gathering_id, 12, "two", 0))[0],
            "joined",
        )
        await database.reserve_summit_list(gathering_id, 13, "three")
        position_id = await database.create_vzp_summit(
            "Карта", 3, 501, 99, guild_id=GUILD_ID
        )
        await database.set_vzp_summit_message(position_id, 888, 501)
        await database.set_vzp_gathering_position_list(gathering_id, position_id)
        await database.vzp_take_position(position_id, 1, 11, "one")

        report = await archive_open_legacy_gatherings()
        self.assertEqual(report.gatherings, 1)
        self.assertEqual(report.position_lists, 0)

        self.assertTrue(await is_summit_list_archived(gathering_id))
        self.assertTrue(await is_vzp_list_archived(position_id))
        gathering = await database.get_summit_list(gathering_id)
        self.assertEqual(gathering[4], 777)
        self.assertEqual(gathering[6], 0)
        self.assertEqual(
            [member[0] for member in await database.get_summit_list_members(gathering_id)],
            [11, 12],
        )
        self.assertEqual(
            [reserve[0] for reserve in await database.get_summit_list_reserves(gathering_id)],
            [13],
        )
        spec = await archived_interaction_spec(list_id=gathering_id)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.code, "SUMMIT.ARCHIVED")
        button = await archived_interaction_spec(vzp_list_id=position_id)
        self.assertIsNotNone(button)

        second = await archive_open_legacy_gatherings()
        self.assertEqual(second.gatherings, 0)
        self.assertEqual(second.position_lists, 0)

        async with connection() as db:
            rows = await db.execute_fetchall(
                """SELECT idempotency_key FROM operation_outbox
                   WHERE kind = 'summit.archive' ORDER BY id"""
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], f"summit.archive:list:{gathering_id}")

        async with connection() as db:
            tables = {
                row[0]
                for row in await db.execute_fetchall(
                    """SELECT name FROM sqlite_master
                       WHERE type = 'table' AND name IN (
                           'summit_lists', 'summit_vzp_lists',
                           'summit_vzp_positions', 'summit_list_members',
                           'summit_list_reserves'
                       )"""
                )
            }
        self.assertEqual(
            tables,
            {
                "summit_lists",
                "summit_vzp_lists",
                "summit_vzp_positions",
                "summit_list_members",
                "summit_list_reserves",
            },
        )

    async def test_open_contract_is_archived_regular_list_stays(self) -> None:
        contract_id = await database.create_summit_list(
            "Старый контракт",
            2,
            601,
            50,
            kind="contract",
            guild_id=GUILD_ID,
        )
        await database.join_contract_list(contract_id, 21, "alpha", 0)
        list_id = await database.create_summit_list(
            "Обычный список",
            2,
            602,
            50,
            kind="list",
            guild_id=GUILD_ID,
        )
        await database.join_contract_list(list_id, 22, "beta", 0)

        report = await archive_open_legacy_gatherings()
        self.assertEqual(report.gatherings, 1)
        self.assertTrue(await is_summit_list_archived(contract_id))
        self.assertFalse(await is_summit_list_archived(list_id))
        self.assertIsNone(await archived_interaction_spec(list_id=list_id))
        self.assertEqual(
            [member[0] for member in await database.get_summit_list_members(contract_id)],
            [21],
        )
        self.assertEqual(
            [member[0] for member in await database.get_summit_list_members(list_id)],
            [22],
        )

    async def test_orphan_position_list_is_archived(self) -> None:
        position_id = await database.create_vzp_summit(
            "Позиции", 2, 701, 77, guild_id=GUILD_ID
        )
        await database.vzp_take_position(position_id, 1, 31, "gamma")
        report = await archive_open_legacy_gatherings()
        self.assertEqual(report.gatherings, 0)
        self.assertEqual(report.position_lists, 1)
        self.assertTrue(await is_vzp_list_archived(position_id))
        positions = await database.get_vzp_positions(position_id)
        self.assertEqual(positions[0][1], 31)
