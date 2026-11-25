import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from repositories.economy import (
    EconomySettings,
    award_message_activity,
    get_economy_statistics,
    sync_voice_activity,
)
from utils import database


class EconomyRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "economy.db"
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        await database.init_db()
        self.settings = EconomySettings(
            5000,
            500,
            0,
            message_daily_cap=0,
            voice_daily_cap=0,
        )

    async def asyncTearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_message_award_is_batched_and_idempotent(self):
        timestamp = datetime(2026, 7, 22, tzinfo=UTC).isoformat()
        records = [
            (message_id, 1, 9, "general", 42, timestamp)
            for message_id in range(101, 301)
        ]
        self.assertEqual(
            await award_message_activity(records, {1: self.settings}), 1000
        )
        self.assertEqual(
            await award_message_activity(records, {1: self.settings}), 0
        )
        self.assertEqual((await database.get_coin_account(42))[0], 1000)

    async def test_message_daily_cap_discards_overflow(self):
        timestamp = datetime(2026, 7, 22, tzinfo=UTC).isoformat()
        settings = EconomySettings(
            5000,
            500,
            0,
            message_daily_cap=10,
            voice_daily_cap=0,
        )
        records = [
            (message_id, 1, 9, "general", 77, timestamp)
            for message_id in range(1, 10)
        ]
        self.assertEqual(
            await award_message_activity(records, {1: settings}),
            10,
        )
        self.assertEqual((await database.get_coin_account(77))[0], 10)

    async def test_voice_fraction_is_carried_without_float_rounding(self):
        started = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        await sync_voice_activity(1, [(42, 99)], self.settings, reset=True, now=started)
        awarded = await sync_voice_activity(
            1,
            [(42, 99)],
            self.settings,
            now=started + timedelta(minutes=3, seconds=20),
        )
        self.assertEqual(awarded, 1)
        self.assertEqual((await database.get_coin_account(42))[0], 1)
        stats = await get_economy_statistics(1, "2026-07-22")
        self.assertEqual(stats["pending_milli"], 500)
        self.assertEqual(stats["sources"][0][1], 1500)
