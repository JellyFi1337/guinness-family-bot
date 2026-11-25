from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core.permissions import Permission, grant_role_permission
from repositories.operations import claim_operations, enqueue_operation
from repositories.static_messages import set_guild_setting
from services.calendar import (
    FORBIDDEN_START_REMINDER_DELTAS,
    REMIND_KIND_AFTERMATH,
    REMIND_KIND_PREP,
    ScheduleConfigError,
    calendar_day_spec,
    day_kind,
    default_schedule,
    discord_timestamp,
    events_conflict_1830,
    is_org_day,
    is_rest_day,
    list_event_reminders,
    parse_schedule,
    reminder_idempotency_key,
    reminder_message,
    warehouse_deadline,
)
from services.event_planning import (
    EventError,
    cancel_event,
    create_event,
    ensure_event_integrations,
    join_event,
    record_event_outcome,
    reschedule_event,
)
from services.membership import register_member, reset_obligation_sources, verify_in_game
from services.onboarding import OnboardingError, parse_prime
from utils import database

GUILD_ID = 6_300
FAMILY_ROLE_ID = 196
PERM_ROLE_ID = 197
STAFF_ID = 2_010
USER_A = 6_001
USER_B = 6_002
MOSCOW = ZoneInfo("Europe/Moscow")


def _role(role_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, name=name)


def _guild(roles: list[SimpleNamespace], guild_id: int = GUILD_ID) -> SimpleNamespace:
    by_id = {role.id: role for role in roles}

    def get_role(role_id: int):
        return by_id.get(int(role_id))

    return SimpleNamespace(id=guild_id, roles=roles, get_role=get_role, members=[])


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


class CalendarPureTests(unittest.TestCase):
    def test_org_and_rest_days_come_from_schedule_not_code_defaults(self) -> None:
        schedule = default_schedule()
        monday = datetime(2026, 9, 7, 12, 0, tzinfo=MOSCOW)
        tuesday = datetime(2026, 9, 8, 12, 0, tzinfo=MOSCOW)
        self.assertEqual(schedule.org_days, ("mon", "wed", "fri", "sat"))
        self.assertEqual(schedule.rest_days, ("tue", "thu", "sun"))
        self.assertTrue(is_org_day(schedule, monday))
        self.assertFalse(is_rest_day(schedule, monday))
        self.assertEqual(day_kind(schedule, monday), "org")
        self.assertTrue(is_rest_day(schedule, tuesday))
        self.assertFalse(is_org_day(schedule, tuesday))
        self.assertEqual(day_kind(schedule, tuesday), "rest")

    def test_timezone_across_midnight_uses_profile_zone(self) -> None:
        schedule = default_schedule()
        utc_monday_evening = datetime(2026, 9, 7, 21, 30, tzinfo=UTC)
        self.assertEqual(day_kind(schedule, utc_monday_evening), "rest")
        self.assertTrue(is_rest_day(schedule, utc_monday_evening))
        self.assertFalse(is_org_day(schedule, utc_monday_evening))

    def test_1830_conflict_uses_configured_slots_same_local_date(self) -> None:
        schedule = default_schedule()
        lottery = datetime(2026, 9, 7, 18, 0, tzinfo=MOSCOW)
        dealers = datetime(2026, 9, 7, 18, 30, tzinfo=MOSCOW)
        morning = datetime(2026, 9, 7, 10, 30, tzinfo=MOSCOW)
        next_day = datetime(2026, 9, 8, 18, 30, tzinfo=MOSCOW)
        self.assertTrue(events_conflict_1830(schedule, lottery, dealers))
        self.assertFalse(events_conflict_1830(schedule, lottery, morning))
        self.assertFalse(events_conflict_1830(schedule, lottery, next_day))

    def test_warehouse_deadline_is_six_hours_and_crosses_midnight(self) -> None:
        schedule = default_schedule()
        self.assertEqual(schedule.warehouse_tag_hours, 6)
        won = datetime(2026, 9, 7, 22, 0, tzinfo=MOSCOW)
        deadline = warehouse_deadline(won, schedule)
        self.assertEqual(deadline, datetime(2026, 9, 8, 4, 0, tzinfo=MOSCOW))

    def test_empty_org_days_require_configuration(self) -> None:
        schedule = parse_schedule({"org_days": "", "rest_days": "tue"})
        with self.assertRaises(ScheduleConfigError):
            is_org_day(schedule, datetime(2026, 9, 7, 12, 0, tzinfo=MOSCOW))

    def test_prime_rejects_rest_days(self) -> None:
        self.assertEqual(parse_prime("пн,ср"), "mon,wed")
        with self.assertRaises(OnboardingError) as raised:
            parse_prime("tue")
        self.assertEqual(raised.exception.code, "APP.PRIME")
        self.assertIn("наставничеств", str(raised.exception).casefold())

    def test_new_copy_avoids_vzp(self) -> None:
        source = inspect.getsource(__import__("services.calendar", fromlist=["*"]))
        lowered = source.casefold()
        self.assertNotIn("vzp", lowered)
        self.assertNotIn("взп", lowered)

    def test_discord_timestamp_is_unix(self) -> None:
        stamp = datetime(2026, 9, 7, 18, 0, tzinfo=MOSCOW)
        self.assertEqual(discord_timestamp(stamp), f"<t:{int(stamp.timestamp())}:F>")


class CalendarReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "calendar.db",
        )
        self.path_patch.start()
        await database.init_db()
        reset_obligation_sources()
        ensure_event_integrations()
        family = _role(FAMILY_ROLE_ID, "High")
        perms = _role(PERM_ROLE_ID, "Кадровик")
        self.guild = _guild([family, perms])
        self.staff = _actor(user_id=STAFF_ID, roles=[family, perms], guild=self.guild)
        self.alpha = _actor(
            user_id=USER_A, roles=[family], guild=self.guild, display_name="A"
        )
        self.beta = _actor(
            user_id=USER_B, roles=[family], guild=self.guild, display_name="B"
        )
        for permission in (
            Permission.MEMBERS_MANAGE,
            Permission.MEMBERS_VIEW,
            Permission.EVENTS_CREATE,
            Permission.EVENTS_MANAGE_OWN,
            Permission.EVENTS_MANAGE_ALL,
        ):
            await grant_role_permission(GUILD_ID, permission, PERM_ROLE_ID)
        await register_member(
            self.staff, GUILD_ID, discord_id=STAFF_ID, ic_name="Staff"
        )
        for user_id in (USER_A, USER_B):
            await register_member(
                self.staff, GUILD_ID, discord_id=user_id, ic_name=f"IC{user_id}"
            )
            await verify_in_game(self.staff, GUILD_ID, user_id)

    async def asyncTearDown(self) -> None:
        reset_obligation_sources()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def _enable(self, *upgrades: str) -> None:
        await set_guild_setting(
            GUILD_ID, "profile.enabled_upgrades", ",".join(upgrades)
        )

    async def test_join_blocks_1830_without_confirm(self) -> None:
        await self._enable("dealers")
        lottery_start = datetime(2026, 9, 7, 18, 0, tzinfo=MOSCOW)
        dealers_start = datetime(2026, 9, 7, 18, 30, tzinfo=MOSCOW)
        lottery = await create_event(
            self.staff,
            GUILD_ID,
            "meeting",
            starts_at=lottery_start.isoformat(),
            ends_at=(lottery_start + timedelta(hours=1)).isoformat(),
            meetup="офис организации",
            capacity=4,
        )
        dealers = await create_event(
            self.staff,
            GUILD_ID,
            "dealers",
            starts_at=dealers_start.isoformat(),
            ends_at=(dealers_start + timedelta(hours=1)).isoformat(),
            meetup="склад организации",
            capacity=10,
        )
        await join_event(self.alpha, GUILD_ID, lottery.id)
        with self.assertRaises(EventError) as raised:
            await join_event(self.alpha, GUILD_ID, dealers.id)
        self.assertEqual(raised.exception.code, "EVENTS.SCHEDULE_CONFLICT")
        confirmed = await join_event(
            self.alpha, GUILD_ID, dealers.id, confirm_conflict=True
        )
        self.assertEqual(confirmed.seat.event_id, dealers.id)

    async def test_reschedule_cancels_old_reminders(self) -> None:
        start = datetime(2026, 9, 9, 12, 0, tzinfo=MOSCOW)
        end = start + timedelta(hours=2)
        event = await create_event(
            self.staff,
            GUILD_ID,
            "meeting",
            starts_at=start.isoformat(),
            ends_at=end.isoformat(),
            meetup="офис организации",
            capacity=4,
        )
        before = await list_event_reminders(GUILD_ID, event.id)
        self.assertTrue(before)
        old_keys = {item.idempotency_key for item in before}
        new_start = start + timedelta(hours=5)
        new_end = new_start + timedelta(hours=2)
        updated = await reschedule_event(
            self.staff,
            GUILD_ID,
            event.id,
            starts_at=new_start.isoformat(),
            ends_at=new_end.isoformat(),
        )
        after = await list_event_reminders(GUILD_ID, updated.id)
        dead = [item for item in after if item.idempotency_key in old_keys]
        self.assertTrue(dead)
        self.assertTrue(all(item.state == "dead" for item in dead))
        pending = [item for item in after if item.state == "pending"]
        self.assertTrue(pending)
        self.assertTrue(
            all(item.idempotency_key not in old_keys for item in pending)
        )

    async def test_restart_does_not_duplicate_reminders(self) -> None:
        start = datetime(2026, 9, 9, 14, 0, tzinfo=MOSCOW)
        end = start + timedelta(hours=1)
        event = await create_event(
            self.staff,
            GUILD_ID,
            "meeting",
            starts_at=start.isoformat(),
            ends_at=end.isoformat(),
            meetup="офис организации",
            capacity=4,
        )
        first = await list_event_reminders(GUILD_ID, event.id)
        await create_event(
            self.staff,
            GUILD_ID,
            "meeting",
            starts_at=start.isoformat(),
            ends_at=end.isoformat(),
            meetup="офис организации",
            capacity=4,
        )
        again = await list_event_reminders(GUILD_ID, event.id)
        self.assertEqual(len(first), len(again))
        key = reminder_idempotency_key(event.id, "prep", event.starts_at)
        first_id = await enqueue_operation(
            idempotency_key=key,
            guild_id=GUILD_ID,
            kind=REMIND_KIND_PREP,
            payload={"event_id": event.id, "phase": "prep"},
            aggregate_id=str(event.id),
        )
        second_id = await enqueue_operation(
            idempotency_key=key,
            guild_id=GUILD_ID,
            kind=REMIND_KIND_PREP,
            payload={"event_id": event.id, "phase": "prep"},
            aggregate_id=str(event.id),
        )
        self.assertEqual(first_id, second_id)

    async def test_cancel_drops_pending_reminders_and_skips_start_copies(self) -> None:
        start = datetime(2026, 9, 10, 16, 0, tzinfo=MOSCOW)
        end = start + timedelta(hours=2)
        event = await create_event(
            self.staff,
            GUILD_ID,
            "meeting",
            starts_at=start.isoformat(),
            ends_at=end.isoformat(),
            meetup="офис организации",
            capacity=4,
        )
        rows = await list_event_reminders(GUILD_ID, event.id)
        kinds = {item.kind for item in rows}
        self.assertIn(REMIND_KIND_PREP, kinds)
        self.assertIn(REMIND_KIND_AFTERMATH, kinds)
        self.assertFalse(any(item.kind.endswith(".start") for item in rows))
        start_utc = start.astimezone(UTC)
        forbidden = {
            (start_utc - delta).isoformat() for delta in FORBIDDEN_START_REMINDER_DELTAS
        }
        self.assertTrue(
            all(item.available_at not in forbidden for item in rows)
        )
        await cancel_event(self.staff, GUILD_ID, event.id)
        after = await list_event_reminders(GUILD_ID, event.id)
        self.assertTrue(all(item.state == "dead" for item in after if item.state != "completed"))
        claimed = await claim_operations(kinds=(REMIND_KIND_PREP, REMIND_KIND_AFTERMATH))
        self.assertFalse(
            any(item.aggregate_id == str(event.id) for item in claimed)
        )

    async def test_dealers_win_schedules_warehouse_deadline(self) -> None:
        await self._enable("dealers")
        start = datetime(2026, 9, 7, 18, 30, tzinfo=MOSCOW)
        event = await create_event(
            self.staff,
            GUILD_ID,
            "dealers",
            starts_at=start.isoformat(),
            ends_at=(start + timedelta(hours=1)).isoformat(),
            meetup="склад организации",
            capacity=10,
        )
        updated = await record_event_outcome(self.staff, GUILD_ID, event.id, "won")
        self.assertEqual(updated.snapshot["outcome"], "won")
        rows = await list_event_reminders(GUILD_ID, event.id)
        warehouse = [item for item in rows if item.kind == "event.remind.warehouse"]
        self.assertEqual(len(warehouse), 1)
        spec = await reminder_message(
            updated,
            {
                "phase": "warehouse",
                "deadline": warehouse_deadline(
                    updated.snapshot["outcome_at"], default_schedule()
                )
                .astimezone(UTC)
                .isoformat(),
            },
        )
        self.assertIn("<t:", spec.summary)
        self.assertNotIn("vzp", spec.summary.casefold())
        self.assertNotIn("взп", spec.summary.casefold())

    async def test_calendar_command_uses_profile_day(self) -> None:
        spec = await calendar_day_spec(GUILD_ID, "2026-09-07")
        self.assertEqual(spec.code, "EVENTS.CALENDAR")
        labels = dict(spec.details)
        self.assertIn("игровой день", labels["День"])
        self.assertIn("<t:", labels["ВЗА"])
        self.assertNotIn("vzp", spec.summary.casefold())
        self.assertNotIn("взп", spec.summary.casefold())
