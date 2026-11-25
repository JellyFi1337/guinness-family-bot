import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from utils import database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_counters_are_unique_under_concurrency(self):
        numbers = await asyncio.gather(
            *(database.get_next_number("TEST") for _ in range(25))
        )
        self.assertEqual(len(numbers), len(set(numbers)))

    async def test_treasury_transaction_updates_balance_once(self):
        number = await database.add_treasury_transaction(
            1, "member", 500, "add", "deposit"
        )
        self.assertEqual(number, 1)
        self.assertEqual(await database.get_treasury_balance(), 500)
        self.assertEqual(len(await database.get_last_transactions()), 1)

        await database.add_treasury_transaction(1, "member", 200, "remove", "cost")
        self.assertEqual(await database.get_treasury_balance(), 300)

        with self.assertRaisesRegex(ValueError, "Недостаточно средств"):
            await database.add_treasury_transaction(
                1, "member", 301, "remove", "too much"
            )
        self.assertEqual(await database.get_treasury_balance(), 300)

    async def test_request_review_can_only_be_claimed_once(self):
        await database.log_request("RR-0001", "role", 42, "request")

        claims = await asyncio.gather(
            *(
                database.claim_request_review("RR-0001", reviewer_id)
                for reviewer_id in range(100, 120)
            )
        )
        self.assertEqual(sum(claims), 1)
        reviewer_id = 100 + claims.index(True)
        self.assertTrue(
            await database.finish_request_review(
                "RR-0001", reviewer_id, "approved"
            )
        )
        self.assertFalse(await database.claim_request_review("RR-0001", 999))

    async def test_role_request_charge_and_refund_are_exactly_once(self):
        await database.adjust_coins(42, 1_000, "test_grant")
        request_id = await database.purchase_role_request(
            42, "Custom", 0x112233, None, None, 400
        )
        self.assertIsNotNone(request_id)
        self.assertEqual((await database.get_coin_account(42))[0], 600)

        claims = await asyncio.gather(
            *(
                database.claim_role_purchase_request(request_id, reviewer_id)
                for reviewer_id in range(200, 220)
            )
        )
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        reviewer_id = 200 + next(
            index for index, claim in enumerate(claims) if claim is not None
        )
        self.assertTrue(
            await database.reject_role_purchase_with_refund(
                request_id, reviewer_id, 200, "test_refund"
            )
        )
        self.assertFalse(
            await database.reject_role_purchase_with_refund(
                request_id, reviewer_id, 200, "duplicate_refund"
            )
        )
        self.assertEqual((await database.get_coin_account(42))[0], 800)

    async def test_unpublished_role_request_cancellation_refunds_once(self):
        await database.adjust_coins(52, 1_000, "test_grant")
        request_id = await database.purchase_role_request(
            52, "Custom", 0x112233, None, None, 400
        )
        cancellations = await asyncio.gather(
            *(
                database.cancel_role_purchase_request_with_refund(
                    request_id, 52, 400
                )
                for _ in range(10)
            )
        )
        self.assertEqual(sum(cancellations), 1)
        self.assertEqual((await database.get_coin_account(52))[0], 1_000)

    async def test_role_sale_is_reserved_before_refund(self):
        await database.adjust_coins(7, 1_000, "test_grant")
        self.assertTrue(await database.spend_coins(7, 400, "test_purchase"))
        await database.register_purchased_role(7, 987_654, "Role", 400, "custom")
        purchase_id = (await database.get_user_purchased_roles(7))[0][0]

        claims = await asyncio.gather(
            *(database.claim_purchased_role_sale(purchase_id, 7) for _ in range(10))
        )
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        sold = await database.complete_purchased_role_sale(purchase_id, 7)
        self.assertEqual(sold[3], 200)
        self.assertIsNone(
            await database.complete_purchased_role_sale(purchase_id, 7)
        )
        self.assertEqual((await database.get_coin_account(7))[0], 800)

    async def test_message_statistics_are_written_in_batches(self):
        await database.record_stats_messages_batch(
            [
                (1001, 1, 10, "general", 7, "2026-07-14T10:00:00+00:00"),
                (1002, 1, 10, "general", 8, "2026-07-14T10:01:00+00:00"),
            ],
            [1002],
        )
        data = await database.get_weekly_stats_data(
            1,
            "2026-07-14T00:00:00+00:00",
            "2026-07-21T00:00:00+00:00",
        )
        self.assertEqual(data["message_summary"], (2, 2, 1))

    async def test_legacy_summit_schema_is_migrated_before_index_creation(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                """
                CREATE TABLE summit_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    capacity INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER,
                    creator_id INTEGER NOT NULL,
                    is_open INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        with patch.object(database, "DB_PATH", legacy_path):
            await database.init_db()
            with closing(sqlite3.connect(legacy_path)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(summit_lists)")
                }
                indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(summit_lists)")
                }
        self.assertIn("launched_at", columns)
        self.assertIn("idx_summit_launch_period", indexes)

    async def test_birthday_is_validated_and_sorted_by_month(self):
        await database.add_birthday(1, "20.12", "December")
        await database.add_birthday(2, "10.01", "January")
        birthdays = await database.get_all_birthdays()
        self.assertEqual([item["user_id"] for item in birthdays], [2, 1])

        with self.assertRaises(ValueError):
            await database.add_birthday(3, "31.02", "Invalid")

    async def test_reset_deletes_data_but_preserves_schema(self):
        await database.add_birthday(1, "01.01", "Member")
        await database.set_setting("message_id", "123")
        await database.add_treasury_transaction(1, "member", 500, "add", "test")
        await database.get_next_number("AB")

        await database.reset_database_data()

        self.assertEqual(await database.get_all_birthdays(), [])
        self.assertIsNone(await database.get_setting("message_id"))
        self.assertEqual(await database.get_treasury_balance(), 0)
        self.assertEqual(await database.get_last_transactions(), [])
        self.assertEqual(await database.get_next_number("AB"), "AB-0001")

        async with database._connect() as db:
            await db.execute(
                """INSERT INTO family_context (
                       guild_id, project, server_key, family_name, family_tag,
                       direction, member_limit, created_at, archived_at
                   ) VALUES (1, 'majestic', 'memphis', 'Guinness', 'GN',
                             'unset', NULL, '2026-01-01T00:00:00+00:00', NULL)"""
            )
            await db.commit()
        await database.reset_database_data()
        async with database._connect() as db:
            rows = await db.execute_fetchall("SELECT COUNT(*) FROM family_context")
        self.assertEqual(int(rows[0][0]), 0)

    async def test_summit_priority_replaces_lower_member(self):
        list_id = await database.create_summit_list("Test", 2, 1, 99)
        self.assertEqual(
            (await database.join_summit_list(list_id, 1, "one", 0))[0], "joined"
        )
        self.assertEqual(
            (await database.join_summit_list(list_id, 2, "two", 1))[0], "joined"
        )
        result, bumped_id, _ = await database.join_summit_list(
            list_id, 3, "three", 3
        )
        self.assertEqual((result, bumped_id), ("replaced", 1))
        self.assertEqual(
            [member[0] for member in await database.get_summit_list_members(list_id)],
            [3, 2],
        )
        self.assertEqual(
            [reserve[0] for reserve in await database.get_summit_list_reserves(list_id)],
            [1],
        )

    async def test_vzp_positions_toggle_and_promote(self):
        list_id = await database.create_vzp_summit("VZP", 3, 1, 99)
        self.assertEqual(
            await database.vzp_take_position(list_id, 2, 7, "member"), "taken_ok"
        )
        self.assertTrue(await database.vzp_promote(list_id, 7))
        positions = await database.get_vzp_positions(list_id)
        self.assertEqual(positions[0][1], 7)
        self.assertEqual(
            await database.vzp_take_position(list_id, 1, 7, "member"), "left"
        )

    async def test_vzp_gathering_persists_map_approval_and_thread(self):
        gathering_id = await database.create_summit_list(
            "Gathering", 3, 10, 99, kind="vzp_gathering"
        )
        await database.set_vzp_gathering_map(
            gathering_id, "Байкерка", "assets/maps/biker.png"
        )
        await database.set_vzp_gathering_thread(gathering_id, 20)
        position_list_id = await database.create_vzp_summit(
            "Байкерка", 3, 20, 99
        )
        await database.set_vzp_gathering_position_list(
            gathering_id, position_list_id
        )
        await database.approve_vzp_gathering(gathering_id, position_list_id)

        gathering = await database.get_summit_list(gathering_id)
        self.assertEqual(
            gathering[7:14],
            (
                "vzp_gathering",
                "Байкерка",
                "assets/maps/biker.png",
                1,
                20,
                position_list_id,
                1,
            ),
        )
        self.assertIsNotNone(gathering[14])
        self.assertEqual(gathering[6], 0)

    async def test_voice_room_is_persisted(self):
        await database.create_voice_room(101, 202)
        await database.set_voice_room_message(101, 303)
        self.assertEqual(await database.get_voice_room(101), (101, 202, 303))
        await database.set_voice_room_owner(101, 404)
        self.assertEqual((await database.get_voice_room(101))[1], 404)
        self.assertEqual(await database.get_all_voice_rooms(), [(101, 404, 303)])
        await database.delete_voice_room(101)
        self.assertIsNone(await database.get_voice_room(101))

    async def test_ticket_assignment_is_atomic_and_can_be_released(self):
        await database.create_ticket_assignment(10, 20, "жалоба-0001")
        results = await asyncio.gather(
            database.assign_ticket(10, 30),
            database.assign_ticket(10, 40),
        )
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        assigned = (await database.get_ticket_assignment(10))[2]
        self.assertIn(assigned, {30, 40})
        self.assertEqual((await database.set_ticket_assignee(10, 50))[0], "ok")
        assigned = (await database.get_ticket_assignment(10))[2]
        self.assertEqual(assigned, 50)
        self.assertEqual(
            (await database.unassign_ticket(10, assigned))[0], "ok"
        )
        self.assertIsNone((await database.get_ticket_assignment(10))[2])

    async def test_processed_actions_are_in_weekly_stats(self):
        await database.record_stats_processed_action(1, "ticket", "closed", 10)
        await database.record_stats_processed_action(1, "application", "approved", 10)
        await database.record_stats_processed_action(1, "application", "rejected", 11)
        data = await database.get_weekly_stats_data(
            1,
            "2000-01-01T00:00:00+00:00",
            "2999-01-01T00:00:00+00:00",
        )
        self.assertEqual(
            set(data["processed_actions"]),
            {
                ("ticket", "closed", 1),
                ("application", "approved", 1),
                ("application", "rejected", 1),
            },
        )

    async def test_giveaway_membership_and_finish_are_persisted(self):
        giveaway_id = await database.create_giveaway(
            "Test", "Prize", 1, 10, 20, "2999-01-01T00:00:00+00:00"
        )
        await database.set_giveaway_message(giveaway_id, 30)
        self.assertEqual(
            await database.toggle_giveaway_entry(giveaway_id, 40), "joined"
        )
        self.assertEqual(await database.get_giveaway_entries(giveaway_id), [40])
        self.assertTrue(await database.finish_giveaway(giveaway_id))
        self.assertEqual(
            await database.toggle_giveaway_entry(giveaway_id, 50), "closed"
        )

    async def test_car_can_be_edited(self):
        car_id = await database.add_car("Old", "A001AA", "Family", 1)
        old, new = await database.edit_car(
            car_id, name="New", plate="B002BB", owner="Organization"
        )
        self.assertEqual(old[1:], ("Old", "A001AA", "Family"))
        self.assertEqual(new[1:], ("New", "B002BB", "Organization"))

    async def test_administrator_can_force_cancel_active_car_booking(self):
        car_id = await database.add_car("Sultan", "TEST", "Family", 1)
        result, booking = await database.create_car_booking(
            car_id,
            42,
            "Player",
            datetime.now(UTC).isoformat(),
            (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        self.assertEqual(result, "booked")

        cancelled = await database.force_cancel_car_booking(booking[0], 99)

        self.assertEqual(cancelled[0], booking[0])
        self.assertEqual(cancelled[3], 42)
        self.assertEqual(await database.get_active_car_bookings(), [])

    async def test_recruiting_counts_only_matching_accepted_application(self):
        await database.upsert_recruiter_invite(
            1, 100, "personal-code", "https://discord.gg/personal-code", 10
        )
        self.assertTrue(
            await database.record_recruiting_join(1, 200, 100, "personal-code")
        )
        self.assertEqual(
            await database.mark_recruiting_application(1, 200, "FI-0001", "John"),
            100,
        )
        self.assertIsNone(
            await database.decide_recruiting_referral(
                1, 200, "FI-9999", True, 300
            )
        )
        self.assertEqual(
            await database.decide_recruiting_referral(
                1, 200, "FI-0001", True, 300
            ),
            (100, True),
        )
        self.assertEqual(
            await database.decide_recruiting_referral(
                1, 200, "FI-0001", True, 300
            ),
            (100, False),
        )
        rows = await database.get_recruiting_referrals(1)
        self.assertEqual(rows[0][6], "accepted")
        self.assertEqual(rows[0][8], 300)

        # Rejoining cannot overwrite a recruit who has already been credited.
        self.assertFalse(
            await database.record_recruiting_join(1, 200, 999, "another-code")
        )
        self.assertEqual((await database.get_recruiting_referrals(1))[0][1], 100)
