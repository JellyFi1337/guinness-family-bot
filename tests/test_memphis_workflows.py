"""End-to-end Memphis processes: member journey, event of 50, supply kit."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from repositories.accounts import find_obligation
from repositories.contribution import list_entries_for_member
from repositories.events import get_event, list_roster
from repositories.inventory import get_balance, get_item_by_sku, get_location_by_kind
from repositories.members import add_member, get_current_member
from services.bonus import approve_period, declare_period
from services.calendar import list_event_reminders
from services.contracts import add_catalog_item, change_catalog_status, start_run
from services.contribution import member_breakdown, publish_rules
from services.event_planning import (
    confirm_attendance,
    confirm_ready,
    create_event,
    join_event,
    lock_roster,
    record_event_outcome,
    reschedule_event,
)
from services.inventory import (
    create_item,
    issue_kit,
    receive_stock,
    reconcile_location,
    request_kit,
    reserve_kit,
    return_kit_item,
)
from services.membership import ensure_family, verify_in_game
from services.onboarding import (
    ApplicationDraft,
    accept_application,
    advance_track,
    assign_mentor,
    assign_training,
    complete_training,
)
from services.report_review import review_report, submit_report
from services.treasury import approve_obligation, confirm_obligation, mark_paid

try:
    from memphis_harness import MemphisBackendCase
except ImportError:
    from tests.memphis_harness import MemphisBackendCase

GUILD_ID = 9_100
FAMILY_ROLE_ID = 91
PERM_ROLE_ID = 92
STAFF_ID = 9_010
REVIEWER_ID = 9_011
PAYER_ID = 9_012
MENTOR_ID = 9_013
USER_A = 9_101
ROSTER_BASE = 21_000
RANGE_START = "2000-01-01T00:00:00+00:00"
RANGE_END = "2100-01-01T00:00:00+00:00"


def _draft() -> ApplicationDraft:
    return ApplicationDraft(
        ooc_name="Алексей",
        ooc_age=20,
        ic_name="Alex Memphis",
        character_id=88001,
        specialization="combat",
        prime_time="mon,wed",
        source="наставник",
        change_surname="да",
        invited_by=None,
    )


def _window(*, hours: int = 4, duration: int = 2) -> tuple[str, str]:
    start = datetime.now(UTC) + timedelta(hours=hours)
    end = start + timedelta(hours=duration)
    return start.isoformat(), end.isoformat()


class MemphisWorkflowTests(MemphisBackendCase, unittest.IsolatedAsyncioTestCase):
    guild_id = GUILD_ID
    family_role_id = FAMILY_ROLE_ID
    perm_role_id = PERM_ROLE_ID
    staff_id = STAFF_ID
    db_name = "memphis-workflows.db"

    async def asyncSetUp(self) -> None:
        await self.open_backend()
        self.build_guild()
        self.reviewer = self.member_actor(REVIEWER_ID, "R")
        self.reviewer.roles = [self.family_role, self.perm_role]
        self.payer = self.member_actor(PAYER_ID, "P")
        self.payer.roles = [self.family_role, self.perm_role]
        self.mentor = self.member_actor(MENTOR_ID, "M")
        self.mentor.roles = [self.family_role, self.perm_role]
        self.alpha = self.member_actor(USER_A, "A")
        await self.grant_staff()
        await self.seed_upgrades()
        await self.register_verified(STAFF_ID, "Staff")
        await self.register_verified(REVIEWER_ID, "Reviewer")
        await self.register_verified(PAYER_ID, "Payer")
        await self.register_verified(MENTOR_ID, "Mentor")

    async def asyncTearDown(self) -> None:
        await self.close_backend()

    async def test_member_journey_from_application_to_confirmed_payout(self) -> None:
        member, track, _limit = await accept_application(
            self.staff,
            GUILD_ID,
            discord_id=USER_A,
            number="FI-9101",
            draft=_draft(),
        )
        self.assertEqual(member.status, "candidate")
        self.assertEqual(track.current_step, "rules")
        stored = await get_current_member(member.family_id, USER_A)
        assert stored is not None
        self.assertIsNone(stored.verified_at)

        active = await verify_in_game(self.staff, GUILD_ID, USER_A)
        self.assertEqual(active.status, "active")
        self.assertIsNotNone(active.verified_at)

        await assign_mentor(
            self.staff,
            GUILD_ID,
            discord_id=USER_A,
            mentor_discord_id=MENTOR_ID,
        )
        assignment = await assign_training(
            self.mentor, GUILD_ID, discord_id=USER_A, title="Первый разбор"
        )
        await complete_training(self.mentor, GUILD_ID, assignment_id=assignment.id)
        steps = []
        current = track
        for _ in range(5):
            current = await advance_track(
                self.mentor, GUILD_ID, discord_id=USER_A
            )
            steps.append(current.current_step)
        self.assertIn("first_task", steps)
        self.assertEqual(steps[-1], "main_review")

        await publish_rules(
            self.staff,
            GUILD_ID,
            {
                "contract": 7,
                "attendance": 0,
                "mentoring": 0,
                "warehouse": 0,
                "logistics": 0,
                "duty": 0,
            },
        )
        item = await add_catalog_item(
            self.staff,
            GUILD_ID,
            title="Первая задача",
            direction="criminal",
            team_max=1,
            upgrades="air_cargo",
        )
        published = await change_catalog_status(
            self.staff, GUILD_ID, item.id, "available"
        )
        run = await start_run(self.alpha, GUILD_ID, published.id)
        report = await submit_report(
            self.alpha,
            GUILD_ID,
            run.id,
            result_amount=40,
            shares=f"{USER_A}:40",
            duration_minutes=15,
            links="https://example.com/first.png",
            cash=40,
        )
        accepted = await review_report(
            self.staff, GUILD_ID, report.id, "accept", reason="принято"
        )
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual((await member_breakdown(GUILD_ID, USER_A))["contract"], 7)
        entries = await list_entries_for_member(member.family_id, USER_A)
        self.assertTrue(any(row.basis_kind == "report" for row in entries))

        obligation = await find_obligation(
            family_id=member.family_id,
            basis_kind="report",
            basis_id=str(accepted.id),
        )
        assert obligation is not None
        approved = await approve_obligation(self.reviewer, GUILD_ID, obligation.id)
        paid = await mark_paid(
            self.payer, GUILD_ID, approved.id, proof_ref="screen-first"
        )
        confirmed = await confirm_obligation(self.alpha, GUILD_ID, paid.id)
        self.assertEqual(confirmed.status, "paid")
        self.assertEqual(confirmed.confirmed_by, USER_A)

        await declare_period(
            self.staff,
            GUILD_ID,
            period_key="2026-W36",
            fund=100,
            starts_at=RANGE_START,
            ends_at=RANGE_END,
        )
        bonus = await approve_period(self.reviewer, GUILD_ID, "2026-W36")
        self.assertEqual(bonus.period.status, "approved")
        bonus_ob = await find_obligation(
            family_id=member.family_id,
            basis_kind="bonus",
            basis_id=f"2026-W36:{USER_A}",
        )
        assert bonus_ob is not None
        bonus_approved = await approve_obligation(
            self.reviewer, GUILD_ID, bonus_ob.id
        )
        bonus_paid = await mark_paid(
            self.payer, GUILD_ID, bonus_approved.id, proof_ref="screen-bonus"
        )
        bonus_confirmed = await confirm_obligation(self.alpha, GUILD_ID, bonus_paid.id)
        self.assertEqual(bonus_confirmed.confirmed_by, USER_A)

    async def test_event_of_fifty_ready_reschedule_attendance_debrief(self) -> None:
        start, end = _window()
        event = await create_event(
            self.staff,
            GUILD_ID,
            "capt_mansion",
            starts_at=start,
            ends_at=end,
            meetup="офис организации",
            commander_id=STAFF_ID,
        )
        self.assertEqual(event.capacity, 50)
        family = await ensure_family(GUILD_ID)
        people: list[object] = []
        for index in range(50):
            discord_id = ROSTER_BASE + index
            await add_member(
                guild_id=GUILD_ID,
                family_id=family.id,
                discord_id=discord_id,
                ic_name=f"Боец {index}",
                actor_id=STAFF_ID,
                actor_name="Staff",
                status="active",
            )
            seat = await join_event(
                self.member_actor(discord_id, f"F{index}"),
                GUILD_ID,
                event.id,
            )
            people.append(seat)
        roster = await list_roster(event.id)
        self.assertEqual(len(roster), 50)
        self.assertEqual(sum(1 for seat in roster if seat.slot == "main"), 50)

        first = self.member_actor(ROSTER_BASE, "F0")
        ready = await confirm_ready(first, GUILD_ID, event.id)
        self.assertEqual(ready.attendance, "ready")

        before = await list_event_reminders(GUILD_ID, event.id)
        old_keys = {item.idempotency_key for item in before}
        new_start = datetime.now(UTC) + timedelta(hours=10)
        new_end = new_start + timedelta(hours=2)
        moved = await reschedule_event(
            self.staff,
            GUILD_ID,
            event.id,
            starts_at=new_start.isoformat(),
            ends_at=new_end.isoformat(),
        )
        after = await list_event_reminders(GUILD_ID, moved.id)
        dead = [item for item in after if item.idempotency_key in old_keys]
        self.assertTrue(dead)
        self.assertTrue(all(item.state == "dead" for item in dead))

        locked = await lock_roster(self.staff, GUILD_ID, event.id)
        self.assertEqual(locked.id, event.id)
        attended = await confirm_attendance(
            self.staff, GUILD_ID, event.id, ROSTER_BASE, "attended"
        )
        self.assertEqual(attended.attendance, "attended")
        debrief = await record_event_outcome(self.staff, GUILD_ID, event.id, "won")
        stored = await get_event(debrief.id)
        assert stored is not None
        self.assertEqual(stored.snapshot.get("outcome"), "won")

    async def test_supply_kit_reserve_issue_return_reconcile(self) -> None:
        start, end = _window(hours=6)
        event = await create_event(
            self.staff,
            GUILD_ID,
            "meeting",
            starts_at=start,
            ends_at=end,
            meetup="офис организации",
            capacity=4,
        )
        await self.register_verified(USER_A, "ICA")
        await create_item(
            self.staff, GUILD_ID, sku="ammo", title="ammo", unit="шт", low_stock=0
        )
        await receive_stock(self.staff, GUILD_ID, sku="ammo", quantity=10)
        kit = await request_kit(
            self.alpha, GUILD_ID, event_id=event.id, sku="ammo", quantity=10
        )
        reserved = await reserve_kit(self.staff, GUILD_ID, kit.id)
        self.assertEqual(reserved.status, "reserved")
        issued = await issue_kit(self.staff, GUILD_ID, kit.id)
        self.assertEqual(issued.status, "issued")
        family = await ensure_family(GUILD_ID)
        warehouse = await get_location_by_kind(family.id, "warehouse")
        item = await get_item_by_sku(family.id, "ammo")
        assert warehouse is not None
        assert item is not None
        after_issue = await get_balance(warehouse.id, item.id)
        assert after_issue is not None
        self.assertEqual(after_issue.on_hand, 0)
        await return_kit_item(
            self.staff, GUILD_ID, kit_id=kit.id, sku="ammo", quantity=4
        )
        after_return = await get_balance(warehouse.id, item.id)
        handout = await get_location_by_kind(family.id, "handout")
        assert after_return is not None
        assert handout is not None
        leftover = await get_balance(handout.id, item.id)
        assert leftover is not None
        self.assertEqual(after_return.on_hand, 4)
        self.assertEqual(leftover.on_hand, 6)
        spec = await reconcile_location(
            self.staff,
            GUILD_ID,
            location_kind="warehouse",
            note="сверка после возврата остатка",
        )
        self.assertEqual(spec.code, "INV.RECONCILED")
        self.assertIn("Сверка записана", spec.summary or "")
