from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import Permission, grant_role_permission
from core.settings import get_registry
from repositories.members import get_current_member, list_clearances
from repositories.recruiting import get_referral_funnel
from infrastructure.database import connection
from services.membership import register_member, verify_in_game
from services.onboarding import (
    SEASONAL_MEMBERSHIP_DAYS,
    SEASONAL_PLAY_HOURS,
    ApplicationDraft,
    OnboardingError,
    accept_application,
    advance_track,
    assign_mentor,
    assign_training,
    build_application_draft,
    clearance_needs_review,
    complete_training,
    decide_promotion,
    draft_from_payload,
    draft_payload,
    grant_activity_clearance,
    list_mentees,
    propose_promotion,
    publish_rules,
    seasonal_cutoff,
    set_promotion_criteria,
)
from utils import database
from utils.database import (
    decide_recruiting_referral,
    mark_recruiting_application,
    record_recruiting_join,
)

GUILD_ID = 4_400
FAMILY_ROLE_ID = 76
PERM_ROLE_ID = 77
STAFF_ID = 900
MENTOR_A_ID = 2_001
MENTOR_B_ID = 2_002
USER_ID = 1_001
USER_B_ID = 1_002


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


def _draft(*, character_id: int = 88001, specialization: str = "combat") -> ApplicationDraft:
    return ApplicationDraft(
        ooc_name="Алексей",
        ooc_age=20,
        ic_name="Alex Memphis",
        character_id=character_id,
        specialization=specialization,
        prime_time="mon,wed",
        source="наставник",
        change_surname="да",
        invited_by=None,
    )


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "onboarding.db",
        )
        self.path_patch.start()
        await database.init_db()
        family = _role(FAMILY_ROLE_ID, "High")
        perms = _role(PERM_ROLE_ID, "Кадровик")
        self.family_role = family
        self.guild = _guild([family, perms])
        self.staff = _actor(
            user_id=STAFF_ID, roles=[family, perms], guild=self.guild
        )
        self.mentor_a = _actor(
            user_id=MENTOR_A_ID,
            roles=[family],
            guild=self.guild,
            display_name="mentor-a",
        )
        self.mentor_b = _actor(
            user_id=MENTOR_B_ID,
            roles=[family],
            guild=self.guild,
            display_name="mentor-b",
        )
        await grant_role_permission(
            GUILD_ID, Permission.APPLICATIONS_REVIEW, PERM_ROLE_ID
        )
        await grant_role_permission(GUILD_ID, Permission.MEMBERS_MANAGE, PERM_ROLE_ID)
        await grant_role_permission(GUILD_ID, Permission.TRAINING_MANAGE, PERM_ROLE_ID)

    async def asyncTearDown(self) -> None:
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def _accept(self, discord_id: int, *, number: str, spec: str = "combat"):
        return await accept_application(
            self.staff,
            GUILD_ID,
            discord_id=discord_id,
            number=number,
            draft=_draft(specialization=spec),
        )

    async def test_full_newbie_route_stays_candidate_until_verify(self) -> None:
        member, track, _limit = await self._accept(USER_ID, number="FI-0001")
        self.assertEqual(member.status, "candidate")
        self.assertEqual(track.current_step, "rules")
        self.assertEqual(track.status, "active")
        self.assertIsNone(member.verified_at)

        await register_member(
            self.staff, GUILD_ID, discord_id=MENTOR_A_ID, ic_name="Mentor A"
        )
        await assign_mentor(
            self.staff,
            GUILD_ID,
            discord_id=USER_ID,
            mentor_discord_id=MENTOR_A_ID,
        )
        assignment = await assign_training(
            self.mentor_a,
            GUILD_ID,
            discord_id=USER_ID,
            title="Разбор правил боя",
        )
        self.assertEqual(assignment.specialization, "combat")
        self.assertEqual(assignment.step_key, "rules")
        done = await complete_training(
            self.mentor_a, GUILD_ID, assignment_id=assignment.id
        )
        self.assertEqual(done.status, "done")

        steps = []
        current = track
        for _ in range(5):
            current = await advance_track(
                self.mentor_a, GUILD_ID, discord_id=USER_ID
            )
            steps.append((current.current_step, current.status))
        self.assertEqual(
            [item[0] for item in steps],
            ["comms", "first_task", "skill_check", "main_review", "main_review"],
        )
        self.assertEqual(steps[-1][1], "completed")

        family_member = await get_current_member(member.family_id, USER_ID)
        assert family_member is not None
        self.assertEqual(family_member.status, "candidate")
        self.assertIsNone(family_member.verified_at)

        active = await verify_in_game(self.staff, GUILD_ID, USER_ID)
        self.assertEqual(active.status, "active")
        self.assertIsNotNone(active.verified_at)

    async def test_mentor_isolation(self) -> None:
        await self._accept(USER_ID, number="FI-0001")
        await self._accept(USER_B_ID, number="FI-0002", spec="logistics")
        await register_member(
            self.staff, GUILD_ID, discord_id=MENTOR_A_ID, ic_name="Mentor A"
        )
        await register_member(
            self.staff, GUILD_ID, discord_id=MENTOR_B_ID, ic_name="Mentor B"
        )
        await assign_mentor(
            self.staff,
            GUILD_ID,
            discord_id=USER_ID,
            mentor_discord_id=MENTOR_A_ID,
        )
        await assign_mentor(
            self.staff,
            GUILD_ID,
            discord_id=USER_B_ID,
            mentor_discord_id=MENTOR_B_ID,
        )

        seen_a = await list_mentees(self.mentor_a, GUILD_ID)
        seen_b = await list_mentees(self.mentor_b, GUILD_ID)
        self.assertEqual([card.member.discord_id for card in seen_a], [USER_ID])
        self.assertEqual([card.member.discord_id for card in seen_b], [USER_B_ID])

        with self.assertRaises(OnboardingError) as err:
            await assign_training(
                self.mentor_a,
                GUILD_ID,
                discord_id=USER_B_ID,
                title="Чужое задание",
            )
        self.assertEqual(err.exception.code, "ACCESS.MISSING_PERMISSION")

        foreign = await assign_training(
            self.mentor_b,
            GUILD_ID,
            discord_id=USER_B_ID,
            title="Складская смена",
        )
        with self.assertRaises(OnboardingError):
            await complete_training(
                self.mentor_a, GUILD_ID, assignment_id=foreign.id
            )

    async def test_rules_version_marks_only_previous_activity_clearance(self) -> None:
        member, _track, _limit = await self._accept(USER_ID, number="FI-0001")
        await publish_rules(
            self.staff, GUILD_ID, activity="contracts", version="1"
        )
        await publish_rules(
            self.staff, GUILD_ID, activity="logistics", version="1"
        )
        contracts = await grant_activity_clearance(
            self.staff,
            GUILD_ID,
            discord_id=USER_ID,
            activity="contracts",
            rules_version="1",
        )
        logistics = await grant_activity_clearance(
            self.staff,
            GUILD_ID,
            discord_id=USER_ID,
            activity="logistics",
            rules_version="1",
        )
        _published, marked = await publish_rules(
            self.staff, GUILD_ID, activity="contracts", version="2"
        )
        self.assertEqual(marked, 1)
        fresh = {item.id: item for item in await list_clearances(member.id)}
        self.assertTrue(clearance_needs_review(fresh[contracts.id]))
        self.assertFalse(clearance_needs_review(fresh[logistics.id]))

        newer = await grant_activity_clearance(
            self.staff,
            GUILD_ID,
            discord_id=USER_ID,
            activity="contracts",
            rules_version="2",
        )
        self.assertFalse(clearance_needs_review(newer))

    async def test_seasonal_cutoff_boundary(self) -> None:
        season_end = date(2026, 10, 10)
        cutoff_day = season_end - timedelta(days=SEASONAL_MEMBERSHIP_DAYS)
        on_cutoff = seasonal_cutoff(season_end, as_of=cutoff_day)
        after_cutoff = seasonal_cutoff(
            season_end, as_of=cutoff_day + timedelta(days=1)
        )
        self.assertEqual(SEASONAL_MEMBERSHIP_DAYS, 28)
        self.assertEqual(SEASONAL_PLAY_HOURS, 30)
        self.assertEqual(on_cutoff.cutoff_date, cutoff_day)
        self.assertTrue(on_cutoff.days_ok)
        self.assertFalse(on_cutoff.hours_confirmed)
        self.assertEqual(on_cutoff.hours_required, 30)
        self.assertFalse(after_cutoff.days_ok)
        self.assertFalse(after_cutoff.hours_confirmed)

    async def test_milton_passport_is_not_copied_to_character_id(self) -> None:
        stale = draft_from_payload(
            {
                "ooc_name": "Иван",
                "ic_name": "Ivan",
                "ic_passport": "77-123456",
                "reason": "хочу в семью",
            }
        )
        self.assertIsNone(stale)
        draft = await build_application_draft(
            GUILD_ID,
            ooc_name="Алексей, 20",
            ic_name="Alex Memphis",
            character_id="88001",
            specialization="combat",
            prime_time="пн,ср",
            source="друг",
            change_surname="да",
        )
        payload = draft_payload(draft)
        self.assertEqual(payload["character_id"], 88001)
        self.assertNotIn("ic_passport", payload)

    async def test_promotion_needs_criteria_and_does_not_activate(self) -> None:
        member, _track, _limit = await self._accept(USER_ID, number="FI-0001")
        with self.assertRaises(OnboardingError) as err:
            await propose_promotion(self.staff, GUILD_ID, discord_id=USER_ID)
        self.assertEqual(err.exception.code, "PROMOTION.NO_CRITERIA")

        await set_promotion_criteria(
            self.staff,
            GUILD_ID,
            title="Переход в основной состав",
            body="Решение кадровика по опубликованным критериям.",
        )
        proposal = await propose_promotion(
            self.staff, GUILD_ID, discord_id=USER_ID
        )
        decided = await decide_promotion(
            self.staff,
            GUILD_ID,
            proposal_id=proposal.id,
            accepted=True,
            reason="по критериям",
        )
        self.assertEqual(decided.status, "accepted")
        current = await get_current_member(member.family_id, USER_ID)
        assert current is not None
        self.assertEqual(current.status, "candidate")
        self.assertIsNone(current.verified_at)

    async def test_verify_marks_recruiting_funnel(self) -> None:
        await record_recruiting_join(GUILD_ID, USER_ID, STAFF_ID, "invite-1")
        await mark_recruiting_application(GUILD_ID, USER_ID, "FI-0001", "Alex")
        await decide_recruiting_referral(
            GUILD_ID, USER_ID, "FI-0001", True, STAFF_ID
        )
        await self._accept(USER_ID, number="FI-0001")
        before = await get_referral_funnel(GUILD_ID, USER_ID)
        assert before is not None
        self.assertEqual(before.status, "accepted")
        self.assertIsNone(before.verified_at)

        await get_registry().set(
            GUILD_ID, "ONBOARDING_RETENTION_DAYS", 1, updated_by="test"
        )
        past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        async with connection() as db:
            await db.execute(
                """UPDATE recruiting_referrals
                      SET accepted_at = ?
                    WHERE guild_id = ? AND member_id = ?""",
                (past, GUILD_ID, USER_ID),
            )
            await db.commit()

        await verify_in_game(self.staff, GUILD_ID, USER_ID)
        after = await get_referral_funnel(GUILD_ID, USER_ID)
        assert after is not None
        self.assertIsNotNone(after.verified_at)
        self.assertIsNotNone(after.retained_at)

    async def test_onboarding_tables_exist_after_migration_37(self) -> None:
        async with connection() as db:
            for table in (
                "onboarding_tracks",
                "training_assignments",
                "rules_versions",
                "promotion_criteria",
                "promotion_proposals",
            ):
                rows = await db.execute_fetchall(f"SELECT COUNT(*) FROM {table}")
                self.assertEqual(int(rows[0][0]), 0)


class OnboardingPureTests(unittest.TestCase):
    def test_draft_from_payload_requires_memphis_id(self) -> None:
        self.assertIsNone(
            draft_from_payload({"ic_passport": "77-1", "ic_name": "Ivan"})
        )

    def test_cutoff_numbers_come_from_the_plan(self) -> None:
        self.assertEqual(SEASONAL_MEMBERSHIP_DAYS, 28)
        self.assertEqual(SEASONAL_PLAY_HOURS, 30)
