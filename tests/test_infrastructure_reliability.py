import asyncio
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from infrastructure.migrations import (
    MIGRATIONS,
    _preflight_backup_sync,
    apply_migrations,
)
from repositories.operations import (
    claim_operation,
    complete_operation,
    enqueue_operation,
)
from repositories.static_messages import get_static_message, save_static_message
from repositories.requests import (
    get_request_decision,
    mark_request_presentation_complete,
)
from repositories.stats import record_processed_action_once
from repositories.summits import complete_summit_launch, request_summit_launch
from repositories.polls import (
    cast_poll_vote,
    close_poll,
    create_poll,
    get_poll,
    get_poll_id_by_message,
    get_polls_needing_sync,
    mark_poll_message_missing,
    mark_poll_synced,
)
from utils import database


class InfrastructureReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "reliability.db"
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_migrations_are_versioned_and_idempotent(self):
        self.assertEqual(
            [item.version for item in MIGRATIONS],
            list(range(1, len(MIGRATIONS) + 1)),
        )
        async with database._connect() as db:
            before = await db.execute_fetchall(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            )
            current = await apply_migrations(db, self.db_path)
            after = await db.execute_fetchall(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            )

        self.assertEqual(current, MIGRATIONS[-1].version)
        self.assertEqual([row[0] for row in before], [item.version for item in MIGRATIONS])
        self.assertEqual(before, after)
        self.assertTrue(all(len(row[2]) == 64 for row in after))

    async def test_removed_magi_tables_are_dropped(self):
        async with database._connect() as db:
            rows = await db.execute_fetchall(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table' AND name LIKE 'magi_%'"""
            )

        self.assertEqual(rows, [])

    async def test_preflight_backup_precedes_legacy_schema_changes(self):
        legacy = Path(self.temp_dir.name) / "legacy.db"
        source = sqlite3.connect(legacy)
        try:
            source.execute("CREATE TABLE important (value TEXT NOT NULL)")
            source.execute("INSERT INTO important VALUES ('preserved')")
            source.commit()
        finally:
            source.close()

        backup = await asyncio.to_thread(_preflight_backup_sync, legacy)
        self.assertIsNotNone(backup)
        restored = sqlite3.connect(backup)
        try:
            value = restored.execute("SELECT value FROM important").fetchone()[0]
        finally:
            restored.close()
        self.assertEqual(value, "preserved")

    async def test_static_message_keys_are_isolated_by_guild(self):
        await save_static_message(100, "treasury", 1_001, 2_001, revision=2)
        await save_static_message(200, "treasury", 1_002, 2_002, revision=3)

        first = await get_static_message(100, "treasury")
        second = await get_static_message(200, "treasury")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual((first.channel_id, first.message_id, first.revision), (1_001, 2_001, 2))
        self.assertEqual((second.channel_id, second.message_id, second.revision), (1_002, 2_002, 3))

        await save_static_message(100, "treasury", 1_003, 2_003, revision=4)
        self.assertEqual((await get_static_message(100, "treasury")).message_id, 2_003)
        self.assertEqual((await get_static_message(200, "treasury")).message_id, 2_002)

    async def test_outbox_is_idempotent_claimed_once_and_recovers_expired_lease(self):
        operation_id = await enqueue_operation(
            idempotency_key="role:approve:42",
            guild_id=100,
            kind="role.approve",
            aggregate_id=42,
            payload={"role_id": 9001},
        )
        duplicate_id = await enqueue_operation(
            idempotency_key="role:approve:42",
            guild_id=100,
            kind="role.approve",
            aggregate_id=42,
            payload={"role_id": 1234},
        )
        self.assertEqual(operation_id, duplicate_id)

        claims = await asyncio.gather(
            *(claim_operation(kinds=("role.approve",)) for _ in range(8))
        )
        claimed = [item for item in claims if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].payload, {"role_id": 9001})
        self.assertEqual(claimed[0].attempts, 1)

        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        async with database._connect() as db:
            await db.execute(
                "UPDATE operation_outbox SET lease_until = ? WHERE id = ?",
                (expired, operation_id),
            )
            await db.commit()

        reclaimed = await claim_operation(kinds=("role.approve",))
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.id, operation_id)
        self.assertEqual(reclaimed.attempts, 2)
        await complete_operation(reclaimed.id, external_id=9001)
        self.assertIsNone(await claim_operation(kinds=("role.approve",)))

        async with database._connect() as db:
            row = (
                await db.execute_fetchall(
                    "SELECT state, attempts, external_id FROM operation_outbox WHERE id = ?",
                    (operation_id,),
                )
            )[0]
        self.assertEqual(tuple(row), ("completed", 2, "9001"))

    async def test_poll_ui_revision_never_loses_a_concurrent_vote(self):
        poll_id = await create_poll(
            guild_id=100,
            channel_id=200,
            creator_id=300,
            question="Карта?",
            options=["Первая", "Вторая"],
            multiple=False,
            anonymous=False,
            closes_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        poll = await get_poll(poll_id)
        self.assertEqual(poll.ui_revision, 0)
        await mark_poll_synced(poll_id, poll.ui_revision, 400)
        self.assertEqual(await get_polls_needing_sync(), [])

        self.assertEqual(await cast_poll_vote(poll_id, 500, (1,)), "recorded")
        dirty = await get_polls_needing_sync()
        self.assertEqual([item.id for item in dirty], [poll_id])
        self.assertEqual(dirty[0].ui_revision, 1)

        # A stale HTTP completion must not acknowledge a newer revision.
        await mark_poll_synced(poll_id, 0, 400)
        self.assertEqual(len(await get_polls_needing_sync()), 1)
        await mark_poll_synced(poll_id, 1, 400)
        self.assertEqual(await get_polls_needing_sync(), [])

        self.assertTrue(await close_poll(poll_id))
        closed = await get_polls_needing_sync()
        self.assertEqual(closed[0].status, "closed")
        self.assertEqual(closed[0].ui_revision, 2)

    async def test_deleted_poll_panel_is_marked_for_recreation(self):
        poll_id = await create_poll(
            guild_id=100,
            channel_id=200,
            creator_id=300,
            question="Маршрут?",
            options=["Север", "Юг"],
            multiple=False,
            anonymous=False,
            closes_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        await mark_poll_synced(poll_id, 0, 400)
        self.assertEqual(await get_poll_id_by_message(100, 400), poll_id)
        self.assertEqual(await get_polls_needing_sync(), [])

        # A delayed deletion for a previous message cannot erase a replacement.
        self.assertFalse(
            await mark_poll_message_missing(
                poll_id,
                guild_id=100,
                message_id=399,
            )
        )
        self.assertEqual((await get_poll(poll_id)).message_id, 400)

        self.assertTrue(
            await mark_poll_message_missing(
                poll_id,
                guild_id=100,
                message_id=400,
            )
        )
        dirty = await get_polls_needing_sync()
        self.assertEqual([item.id for item in dirty], [poll_id])
        self.assertIsNone(dirty[0].message_id)

    async def test_treasury_commit_always_creates_one_publication_intent(self):
        first = await database.add_treasury_transaction(
            42,
            "member",
            500,
            "add",
            "deposit",
            guild_id=100,
            interaction_id=9_001,
        )
        duplicate = await database.add_treasury_transaction(
            42,
            "member",
            500,
            "add",
            "deposit",
            guild_id=100,
            interaction_id=9_001,
        )
        self.assertEqual((first, duplicate), (1, 1))
        self.assertEqual(await database.get_treasury_balance(), 500)

        operation = await claim_operation(kinds=("treasury.publish",))
        self.assertIsNotNone(operation)
        self.assertEqual(operation.idempotency_key, "treasury:100:1:publish")
        self.assertEqual(operation.payload["transaction_number"], 1)
        self.assertIsNone(await claim_operation(kinds=("treasury.publish",)))

    async def test_summit_launch_intent_is_atomic_and_idempotent(self):
        list_id = await database.create_summit_list(
            "Ночной сбор",
            5,
            200,
            300,
            kind="list",
            guild_id=100,
        )
        self.assertEqual(
            await database.join_contract_list(list_id, 42, "member", 0),
            "joined",
        )
        states = await asyncio.gather(
            *(request_summit_launch(list_id, 100) for _ in range(8))
        )
        self.assertEqual(states.count("queued"), 1)
        self.assertEqual(states.count("pending"), 7)

        operation = await claim_operation(kinds=("summit.launch",))
        self.assertIsNotNone(operation)
        self.assertEqual(operation.payload, {"list_id": list_id})
        self.assertIsNone(await claim_operation(kinds=("summit.launch",)))
        self.assertTrue(await complete_summit_launch(list_id, 900))
        summit = await database.get_summit_list(list_id)
        self.assertEqual((summit[11], summit[13]), (900, 1))

    async def test_pending_voice_transfer_blocks_duplicate_owner_room(self):
        self.assertTrue(await database.create_voice_room(500, 10))
        self.assertTrue(await database.transfer_voice_room_owner(500, 10, 20))
        self.assertEqual((await database.get_voice_room_by_owner(10))[0], 500)
        self.assertEqual((await database.get_voice_room_by_owner(20))[0], 500)
        self.assertFalse(await database.create_voice_room(501, 10))
        self.assertTrue(await database.complete_voice_room_transfer(500, 20))
        self.assertTrue(await database.create_voice_room(501, 10))

    async def test_final_request_decision_is_forward_recoverable(self):
        await database.log_request(
            "RR-0099",
            "role_request",
            42,
            "Main",
            guild_id=100,
            payload={"role_name": "Main"},
        )
        self.assertTrue(
            await database.claim_request_review(
                "RR-0099",
                7,
                outcome="rejected",
                token="attempt-1",
            )
        )
        self.assertTrue(
            await database.finish_request_review(
                "RR-0099",
                7,
                "rejected",
                token="attempt-1",
                reason="Недостаточно данных",
            )
        )
        decision = await get_request_decision("RR-0099")
        self.assertEqual(decision.presentation_state, "pending")
        self.assertEqual(decision.reason, "Недостаточно данных")

        # Make the short presentation delay immediately claimable in the test.
        async with database._connect() as db:
            await db.execute(
                "UPDATE operation_outbox SET available_at = ? WHERE kind = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "request.present"),
            )
            await db.commit()
        operation = await claim_operation(kinds=("request.present",))
        self.assertIsNotNone(operation)
        self.assertEqual(operation.payload, {"number": "RR-0099"})

        self.assertTrue(await mark_request_presentation_complete("RR-0099"))
        self.assertFalse(await mark_request_presentation_complete("RR-0099"))
        async with database._connect() as db:
            rows = await db.execute_fetchall(
                """SELECT outcome, actor_id FROM stats_processed_actions
                   WHERE action_kind = 'application'"""
            )
        self.assertEqual([tuple(row) for row in rows], [("rejected", 7)])

    async def test_processed_action_receipt_is_atomic_and_idempotent(self):
        results = await asyncio.gather(
            *(
                record_processed_action_once(
                    100,
                    "custom-role:42:approved",
                    "application",
                    "approved",
                    actor_id=7,
                )
                for _ in range(12)
            )
        )
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 11)
        async with database._connect() as db:
            rows = await db.execute_fetchall(
                """SELECT outcome, actor_id FROM stats_processed_actions
                   WHERE guild_id = ? AND action_kind = 'application'""",
                (100,),
            )
            receipts = await db.execute_fetchall(
                """SELECT event_key FROM stats_action_receipts
                   WHERE guild_id = ?""",
                (100,),
            )
        self.assertEqual([tuple(row) for row in rows], [("approved", 7)])
        self.assertEqual(
            [tuple(row) for row in receipts],
            [("custom-role:42:approved",)],
        )


if __name__ == "__main__":
    unittest.main()
