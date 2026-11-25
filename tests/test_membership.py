from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import Permission, grant_role_permission
from repositories.members import get_current_member, get_member, list_person_history
from repositories.static_messages import set_guild_setting
from services.membership import (
    MembershipError,
    OpenObligation,
    apply_promote_newcomers,
    build_card,
    build_roster,
    describe_limit,
    leave_member,
    preview_promote_newcomers,
    register_member,
    register_obligation_source,
    reset_obligation_sources,
    start_probation,
    verify_in_game,
)
from utils import database

GUILD_ID = 4_200
FAMILY_ROLE_ID = 76
PERM_ROLE_ID = 77
STAFF_ID = 900
USER_ID = 1001


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


class _StickyObligation:
    def __init__(self) -> None:
        self.items = [
            OpenObligation(
                kind="report",
                subject_id="r-1",
                summary="Несданный отчёт по контракту",
                outstanding=True,
            )
        ]

    async def list_open(self, guild_id: int, discord_id: int) -> list[OpenObligation]:
        del guild_id, discord_id
        return list(self.items)


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "membership.db",
        )
        self.path_patch.start()
        await database.init_db()
        reset_obligation_sources()
        family = _role(FAMILY_ROLE_ID, "High")
        perms = _role(PERM_ROLE_ID, "Кадровик")
        self.guild = _guild([family, perms])
        self.staff = _actor(
            user_id=STAFF_ID, roles=[family, perms], guild=self.guild
        )
        self.stranger = _actor(
            user_id=800, roles=[family], guild=self.guild, display_name="member"
        )
        await grant_role_permission(GUILD_ID, Permission.MEMBERS_VIEW, PERM_ROLE_ID)
        await grant_role_permission(
            GUILD_ID, Permission.MEMBERS_MANAGE, PERM_ROLE_ID
        )

    async def asyncTearDown(self) -> None:
        reset_obligation_sources()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_lifecycle_candidate_probation_active_leave(self) -> None:
        member, _limit = await register_member(
            self.staff,
            GUILD_ID,
            discord_id=USER_ID,
            ic_name="Alex",
            rank_name="Runner",
            direction="logistics",
        )
        self.assertEqual(member.status, "candidate")
        self.assertIsNone(member.verified_at)

        trial = await start_probation(self.staff, GUILD_ID, USER_ID)
        self.assertEqual(trial.status, "probation")
        self.assertIsNone(trial.verified_at)

        active = await verify_in_game(self.staff, GUILD_ID, USER_ID)
        self.assertEqual(active.status, "active")
        self.assertIsNotNone(active.verified_at)
        self.assertEqual(active.id, member.id)

        left = await leave_member(self.staff, GUILD_ID, USER_ID, reason="ушёл")
        self.assertEqual(left.member.status, "left")
        self.assertEqual(await get_member(member.id), left.member)
        history = await list_person_history(member.family_id, USER_ID)
        self.assertEqual(
            [event.event_type for event in history],
            ["join", "promote", "promote", "exit"],
        )

    async def test_leave_keeps_outstanding_obligations(self) -> None:
        sticky = _StickyObligation()
        register_obligation_source(sticky)
        await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID, ic_name="Alex"
        )
        await verify_in_game(self.staff, GUILD_ID, USER_ID)
        result = await leave_member(self.staff, GUILD_ID, USER_ID)
        self.assertEqual(len(result.obligations), 1)
        self.assertEqual(result.obligations[0].subject_id, "r-1")
        self.assertTrue(result.obligations[0].outstanding)
        self.assertEqual(sticky.items[0].subject_id, "r-1")
        self.assertTrue(result.tasks_closed)

    async def test_roster_warns_when_limit_is_near_and_never_invents_150(self) -> None:
        unset = describe_limit(
            type("Limit", (), {"current": 3, "limit": None})()
        )
        self.assertEqual(unset.kind, "unset")
        self.assertNotIn("150", unset.message)

        await set_guild_setting(GUILD_ID, "profile.member_limit", "2")
        await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID, ic_name="One"
        )
        _member, notice = await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID + 1, ic_name="Two"
        )
        self.assertEqual(notice.kind, "reached")
        self.assertIn("2", notice.message)
        roster = await build_roster(self.staff, GUILD_ID)
        self.assertEqual(roster.limit.kind, "reached")
        self.assertTrue(any("предел" in line.casefold() for line in roster.lines))

    async def test_refuses_without_permission(self) -> None:
        with self.assertRaises(MembershipError) as raised:
            await build_roster(self.stranger, GUILD_ID)
        self.assertEqual(raised.exception.code, "ACCESS.MISSING_PERMISSION")
        self.assertIn("Просмотр состава", str(raised.exception))
        with self.assertRaises(MembershipError) as denied:
            await register_member(
                self.stranger, GUILD_ID, discord_id=USER_ID, ic_name="Alex"
            )
        self.assertEqual(denied.exception.code, "ACCESS.MISSING_PERMISSION")
        self.assertIn("Управление составом", str(denied.exception))

    async def test_nick_change_keeps_the_same_history(self) -> None:
        created, _limit = await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID, ic_name="OldIC"
        )
        card_before = await build_card(
            self.staff, GUILD_ID, USER_ID, discord_name="OldNick"
        )
        card_after = await build_card(
            self.staff, GUILD_ID, USER_ID, discord_name="NewNick"
        )
        self.assertEqual(card_before.profile.member.id, created.id)
        self.assertEqual(card_after.profile.member.id, created.id)
        self.assertEqual(card_after.profile.member.discord_id, USER_ID)
        self.assertEqual(card_after.discord_name, "NewNick")
        self.assertEqual(
            [event.id for event in card_before.profile.events],
            [event.id for event in card_after.profile.events],
        )
        current = await get_current_member(created.family_id, USER_ID)
        self.assertEqual(current.id, created.id)

    async def test_batch_promote_previews_and_does_not_verify(self) -> None:
        first, _ = await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID, ic_name="A"
        )
        second, _ = await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID + 1, ic_name="B"
        )
        preview = await preview_promote_newcomers(self.staff, GUILD_ID)
        self.assertEqual({item.id for item in preview.members}, {first.id, second.id})
        updated = await apply_promote_newcomers(
            self.staff, GUILD_ID, [first.id, second.id]
        )
        self.assertEqual({item.status for item in updated}, {"probation"})
        self.assertIsNone(updated[0].verified_at)
        verified = await verify_in_game(self.staff, GUILD_ID, USER_ID)
        self.assertEqual(verified.status, "active")

    async def test_register_is_not_in_game_membership(self) -> None:
        member, _ = await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID, ic_name="Alex"
        )
        self.assertEqual(member.status, "candidate")
        roster = await build_roster(self.staff, GUILD_ID, status="active")
        self.assertEqual(roster.members, ())


if __name__ == "__main__":
    unittest.main()
