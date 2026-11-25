from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infrastructure.database import DATABASE_INTEGRITY_ERRORS, connection
from infrastructure.migrations import MIGRATIONS
from infrastructure.postgresql import translate_sql
from repositories import family as family_repo
from repositories import members as members_repo
from repositories.static_messages import set_guild_setting
from utils import database

GUILD_ID = 4_100
OTHER_GUILD = 4_200
DISCORD_ID = 100_001
ACTOR_ID = 9
ACTOR_NAME = "registrar"


class FamilySchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "family.db",
        )
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def _family(self, *, guild_id: int = GUILD_ID) -> family_repo.FamilyContext:
        return await family_repo.create_family_context(
            guild_id=guild_id,
            project="majestic",
            server_key="memphis",
            family_name="Guinness",
            family_tag="GNS",
            direction="unset",
        )

    async def test_migration_creates_empty_family_tables(self) -> None:
        async with connection() as db:
            for table in (
                "family_context",
                "members",
                "ranks",
                "clearances",
                "member_events",
                "onboarding_tracks",
                "training_assignments",
                "rules_versions",
                "promotion_criteria",
                "promotion_proposals",
            ):
                rows = await db.execute_fetchall(f"SELECT COUNT(*) FROM {table}")
                self.assertEqual(int(rows[0][0]), 0)

    async def test_one_active_family_per_guild_archived_rows_allowed(self) -> None:
        first = await self._family()
        with self.assertRaises(DATABASE_INTEGRITY_ERRORS):
            await self._family()

        archived = await family_repo.archive_family_context(first.id)
        self.assertIsNotNone(archived)
        assert archived is not None
        self.assertIsNotNone(archived.archived_at)
        self.assertIsNone(await family_repo.get_active_family(GUILD_ID))

        second = await self._family()
        self.assertNotEqual(second.id, first.id)
        self.assertEqual((await family_repo.get_active_family(GUILD_ID)).id, second.id)

    async def test_cannot_have_two_selected_current_memberships(self) -> None:
        family = await self._family()
        await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=DISCORD_ID,
            ic_name="First",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
        )
        with self.assertRaises(DATABASE_INTEGRITY_ERRORS):
            await members_repo.add_member(
                guild_id=GUILD_ID,
                family_id=family.id,
                discord_id=DISCORD_ID,
                ic_name="Second",
                actor_id=ACTOR_ID,
                actor_name=ACTOR_NAME,
                game_character_id=77,
            )

    async def test_second_character_is_allowed_when_not_selected(self) -> None:
        family = await self._family()
        first = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=DISCORD_ID,
            ic_name="Main",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            game_character_id=10,
        )
        second = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=DISCORD_ID,
            ic_name="Alt",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            game_character_id=11,
            is_selected=False,
        )
        current = await members_repo.get_current_member(family.id, DISCORD_ID)
        self.assertEqual(current.id, first.id)
        selected = await members_repo.select_character(second.id)
        self.assertTrue(selected.is_selected)
        refreshed = await members_repo.get_member(first.id)
        assert refreshed is not None
        self.assertFalse(refreshed.is_selected)
        self.assertEqual(await members_repo.count_current_roster(family.id), 1)

    async def test_same_character_id_on_another_family_is_a_different_person(self) -> None:
        family = await self._family()
        other = await self._family(guild_id=OTHER_GUILD)
        first = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=DISCORD_ID,
            ic_name="Here",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            game_character_id=42,
        )
        other_member = await members_repo.add_member(
            guild_id=OTHER_GUILD,
            family_id=other.id,
            discord_id=DISCORD_ID,
            ic_name="There",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            game_character_id=42,
        )
        self.assertNotEqual(first.id, other_member.id)
        self.assertEqual(first.game_character_id, other_member.game_character_id)

    async def test_lookup_uses_discord_id_not_display_name(self) -> None:
        family = await self._family()
        created = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=DISCORD_ID,
            ic_name="OldNick",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
        )
        found = await members_repo.get_current_member(family.id, DISCORD_ID)
        self.assertEqual(found.id, created.id)
        self.assertEqual(found.discord_id, DISCORD_ID)

    async def test_history_survives_exit_and_rejoin(self) -> None:
        family = await self._family()
        first = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=DISCORD_ID,
            ic_name="Once",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            reason="first join",
        )
        departed = await members_repo.mark_exit(
            first.id,
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            reason="left the family",
        )
        self.assertEqual(departed.status, "left")
        self.assertFalse(departed.is_selected)
        self.assertIsNotNone(departed.left_at)
        self.assertEqual(await members_repo.get_member(first.id), departed)

        second = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=DISCORD_ID,
            ic_name="Again",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            reason="rejoin",
        )
        self.assertNotEqual(second.id, first.id)
        history = await members_repo.list_person_history(family.id, DISCORD_ID)
        self.assertEqual([event.event_type for event in history], ["join", "exit", "join"])
        self.assertEqual(history[0].member_id, first.id)
        self.assertEqual(history[0].reason, "first join")
        self.assertEqual(history[1].reason, "left the family")
        self.assertEqual(history[2].member_id, second.id)
        still_there = await members_repo.get_member(first.id)
        assert still_there is not None
        self.assertEqual(still_there.status, "left")

    async def test_cannot_delete_family_or_member_that_has_history(self) -> None:
        family = await self._family()
        member = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=DISCORD_ID,
            ic_name="Keep",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
        )
        async with connection() as db:
            with self.assertRaises(DATABASE_INTEGRITY_ERRORS):
                await db.execute("DELETE FROM family_context WHERE id = ?", (family.id,))
            await db.rollback()
        async with connection() as db:
            with self.assertRaises(DATABASE_INTEGRITY_ERRORS):
                await db.execute("DELETE FROM members WHERE id = ?", (member.id,))
            await db.rollback()

    async def test_deleting_rank_nulls_member_rank_and_mentor_delete_nulls_link(
        self,
    ) -> None:
        family = await self._family()
        rank = await family_repo.create_rank(
            guild_id=GUILD_ID,
            family_id=family.id,
            name="Runner",
            sort_order=1,
            discord_role_id=555,
            is_staff=False,
        )
        mentor = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=200_001,
            ic_name="Mentor",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
        )
        mentee = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=200_002,
            ic_name="Mentee",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            rank_id=rank.id,
            mentor_id=mentor.id,
        )
        async with connection() as db:
            await db.execute("DELETE FROM ranks WHERE id = ?", (rank.id,))
            await db.commit()
        refreshed = await members_repo.get_member(mentee.id)
        assert refreshed is not None
        self.assertIsNone(refreshed.rank_id)

        async with connection() as db:
            await db.execute(
                "DELETE FROM member_events WHERE member_id = ?", (mentor.id,)
            )
            await db.execute("DELETE FROM members WHERE id = ?", (mentor.id,))
            await db.commit()
        after_mentor = await members_repo.get_member(mentee.id)
        assert after_mentor is not None
        self.assertIsNone(after_mentor.mentor_id)

    async def test_roster_profile_and_limit_come_from_profile_not_invented_number(
        self,
    ) -> None:
        family = await self._family()
        rank = await family_repo.create_rank(
            guild_id=GUILD_ID,
            family_id=family.id,
            name="Coordinator",
            sort_order=2,
            is_staff=True,
        )
        mentor = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=300_001,
            ic_name="Lead",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            status="active",
        )
        member = await members_repo.add_member(
            guild_id=GUILD_ID,
            family_id=family.id,
            discord_id=DISCORD_ID,
            ic_name="Member",
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            status="probation",
            rank_id=rank.id,
            mentor_id=mentor.id,
            direction="logistics",
            prime_time="20-23",
        )
        await members_repo.grant_clearance(
            member_id=member.id,
            activity="warehouse",
            granted_by=ACTOR_ID,
            rules_version="v1",
        )
        await members_repo.mark_leave(
            member.id,
            actor_id=ACTOR_ID,
            actor_name=ACTOR_NAME,
            reason="vacation",
        )

        self.assertEqual(len(await members_repo.list_current_roster(family.id)), 2)
        self.assertEqual(len(await members_repo.list_active_roster(family.id)), 1)
        self.assertEqual(
            len(await members_repo.list_roster_by_status(family.id, "leave")),
            1,
        )

        unset = await members_repo.check_roster_limit(GUILD_ID, family.id)
        self.assertEqual(unset.current, 2)
        self.assertIsNone(unset.limit)
        self.assertFalse(unset.has_limit)
        self.assertFalse(unset.at_or_over_limit)
        self.assertNotEqual(unset.limit, 150)

        await set_guild_setting(GUILD_ID, "profile.member_limit", "2")
        at_limit = await members_repo.check_roster_limit(GUILD_ID, family.id)
        self.assertEqual(at_limit.limit, 2)
        self.assertTrue(at_limit.at_or_over_limit)

        profile = await members_repo.get_member_profile(member.id)
        assert profile is not None
        self.assertEqual(profile.family.id, family.id)
        self.assertEqual(profile.rank.name, "Coordinator")
        self.assertTrue(profile.rank.is_staff)
        self.assertEqual(profile.mentor.id, mentor.id)
        self.assertEqual(len(profile.clearances), 1)
        self.assertEqual(profile.clearances[0].activity, "warehouse")
        self.assertEqual([event.event_type for event in profile.events], ["join", "leave"])

    def test_family_schema_translates_to_postgresql(self) -> None:
        migration = next(item for item in MIGRATIONS if item.version == 36)
        translated = [
            translate_sql(statement, include_returning=False)
            for statement in migration.statements
        ]
        joined = "\n".join(translated)
        self.assertNotIn("AUTOINCREMENT", joined)
        self.assertNotIn("?", joined)
        self.assertIn("BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", joined)
        self.assertIn("discord_id BIGINT NOT NULL", joined)
        self.assertIn("game_character_id BIGINT", joined)
        self.assertIn("idx_members_selected_current", joined)

        insert = translate_sql(
            """INSERT INTO members (
                   guild_id, family_id, discord_id, ic_name, game_character_id,
                   joined_at, left_at, status, rank_id, mentor_id, direction,
                   prime_time, verified_at, verified_by, is_selected
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?)"""
        )
        self.assertIn("%s", insert)
        self.assertNotIn("?", insert)
        self.assertIn("RETURNING id", insert)


if __name__ == "__main__":
    unittest.main()
