from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import Permission, grant_role_permission
from services.membership import register_member
from services.staff_panel import (
    SECTION_EVENTS,
    SECTION_LABELS,
    SECTION_ORDER,
    SECTION_PAYOUTS,
    SECTION_PROGRESS,
    SECTION_QUEUE,
    SECTION_ROSTER,
    SECTION_SUPPLY,
    build_staff_panel,
    staff_section_lines,
    visible_staff_keys,
)
from ui.command_registry import cmd
from utils import database

GUILD_ID = 9_103
FAMILY_ROLE_ID = 41
PERM_ROLE_ID = 42
STAFF_ID = 800
MEMBER_ID = 801


def _role(role_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, name=name)


def _guild(roles: list[SimpleNamespace], guild_id: int = GUILD_ID) -> SimpleNamespace:
    by_id = {role.id: role for role in roles}

    def get_role(role_id: int):
        return by_id.get(int(role_id))

    return SimpleNamespace(id=guild_id, roles=roles, get_role=get_role)


def _actor(
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


class StaffPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "staff-panel.db",
        )
        self.path_patch.start()
        await database.init_db()
        family = _role(FAMILY_ROLE_ID, "High")
        perms = _role(PERM_ROLE_ID, "Кадровик")
        self.guild = _guild([family, perms])
        self.staff = _actor(user_id=STAFF_ID, roles=[family, perms], guild=self.guild)
        self.member = _actor(
            user_id=MEMBER_ID,
            roles=[family],
            guild=self.guild,
            display_name="member",
        )
        for permission in (
            Permission.MEMBERS_MANAGE,
            Permission.MEMBERS_VIEW,
            Permission.QUEUE_ASSIGN,
            Permission.EVENTS_CREATE,
            Permission.TREASURY_VIEW,
            Permission.INVENTORY_MANAGE,
            Permission.PROGRESSION_MANAGE,
        ):
            await grant_role_permission(GUILD_ID, permission, PERM_ROLE_ID)
        await register_member(self.staff, GUILD_ID, discord_id=STAFF_ID, ic_name="Staff")
        await register_member(
            self.staff, GUILD_ID, discord_id=MEMBER_ID, ic_name="Member"
        )

    async def asyncTearDown(self) -> None:
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_section_labels_match_memphis_panel(self) -> None:
        self.assertEqual(
            tuple(SECTION_LABELS[key] for key in SECTION_ORDER),
            (
                "Требует решения",
                "Состав",
                "План событий",
                "Выплаты",
                "Снабжение",
                "Развитие",
            ),
        )

    async def test_member_without_staff_grants_sees_no_tabs(self) -> None:
        keys = await visible_staff_keys(self.member)
        self.assertEqual(keys, ())

    async def test_staff_panel_reads_live_queue_and_roster(self) -> None:
        panel = await build_staff_panel(self.staff, GUILD_ID)
        self.assertEqual(
            panel.visible_keys,
            (
                SECTION_QUEUE,
                SECTION_ROSTER,
                SECTION_EVENTS,
                SECTION_PAYOUTS,
                SECTION_SUPPLY,
                SECTION_PROGRESS,
            ),
        )
        queue = panel.section(SECTION_QUEUE)
        self.assertIn("Нет задач", " ".join(queue.lines))
        self.assertIn(cmd("queue list"), " ".join(queue.lines))
        roster = panel.section(SECTION_ROSTER)
        self.assertIn("2", " ".join(roster.lines))
        denied = await staff_section_lines(self.member, GUILD_ID, SECTION_QUEUE)
        self.assertTrue(any("полномоч" in line.lower() for line in denied))
