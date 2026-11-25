import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.weekly_stats import (
    WeeklyStatsCog,
    _channel_is_reportable,
    _recovery_heartbeat,
    _summarize_voice_sessions,
    _voice_channel_sections,
    update_weekly_stats_message,
)
from utils import database


class WeeklyStatsFormattingTests(unittest.TestCase):
    def test_only_channels_visible_to_main_are_reportable(self):
        main_role = SimpleNamespace(name="Main")
        public_channel = SimpleNamespace(
            permissions_for=lambda _role: SimpleNamespace(view_channel=True)
        )
        private_channel = SimpleNamespace(
            permissions_for=lambda _role: SimpleNamespace(view_channel=False)
        )
        channels = {10: public_channel, 20: private_channel}
        guild = SimpleNamespace(
            roles=[main_role],
            default_role=SimpleNamespace(name="@everyone"),
            get_channel=lambda channel_id: channels.get(channel_id),
        )

        self.assertTrue(_channel_is_reportable(guild, 10, False))
        self.assertFalse(_channel_is_reportable(guild, 20, True))
        self.assertTrue(_channel_is_reportable(guild, 30, True))
        self.assertFalse(_channel_is_reportable(guild, 40, False))

    def test_deleted_temporary_rooms_are_grouped_by_creator_and_unique_members(self):
        start = datetime(2026, 7, 20, tzinfo=UTC)
        end = start + timedelta(days=7)
        rows = [
            (1, 101, "Комната A", start.isoformat(), (start + timedelta(minutes=10)).isoformat(), 77),
            (1, 102, "Комната B", start.isoformat(), (start + timedelta(minutes=10)).isoformat(), 77),
            (2, 102, "Комната B", start.isoformat(), (start + timedelta(minutes=10)).isoformat(), 77),
        ]

        summary = _summarize_voice_sessions(
            rows,
            start=start,
            end=end,
            now=end,
        )
        guild = SimpleNamespace(get_channel=lambda _channel_id: None)
        _, temporary, deleted = _voice_channel_sections(guild, summary.by_channel)

        self.assertIsNone(deleted)
        self.assertIn("<@77>", temporary)
        self.assertIn("30 мин", temporary)
        self.assertIn("2 комнаты", temporary)
        # User 1 visited both rooms but is counted once in creator reach.
        self.assertIn("2 участника", temporary)

    def test_naive_session_is_utc_and_malformed_session_is_skipped(self):
        start = datetime(2026, 7, 20, tzinfo=UTC)
        summary = _summarize_voice_sessions(
            [
                (1, 10, "Voice", "2026-07-20T00:00:00", "2026-07-20T00:10:00", None),
                (2, 10, "Voice", "not-a-date", None, None),
            ],
            start=start,
            end=start + timedelta(days=7),
            now=start + timedelta(hours=1),
        )

        self.assertEqual(summary.by_user, {1: 600})
        self.assertEqual(summary.peak_concurrent, 1)

    def test_recovery_heartbeat_clamps_future_and_invalid_values(self):
        now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

        self.assertEqual(
            _recovery_heartbeat("2099-01-01T00:00:00", now=now),
            now.isoformat(),
        )
        self.assertEqual(
            _recovery_heartbeat("invalid", now=now),
            now.isoformat(),
        )
        self.assertEqual(
            _recovery_heartbeat("2026-07-28T11:59:00", now=now),
            "2026-07-28T11:59:00+00:00",
        )


class WeeklyStatsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "weekly-stats.db"
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.url_patch = patch.object(database, "DATABASE_URL", "")
        self.path_patch.start()
        self.url_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        self.url_patch.stop()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_creator_is_immutable_and_snapshotted_into_voice_session(self):
        self.assertTrue(await database.create_voice_room(101, 10))
        self.assertTrue(await database.transfer_voice_room_owner(101, 10, 20))
        self.assertTrue(await database.complete_voice_room_transfer(101, 20))

        await database.transition_stats_voice_session(1, 42, 101, "Temporary")
        await database.transition_stats_voice_session(1, 42)

        async with database._connect() as db:
            room = await db.execute_fetchall(
                "SELECT owner_id, creator_id FROM voice_rooms WHERE channel_id = ?",
                (101,),
            )
            session = await db.execute_fetchall(
                """SELECT room_creator_id FROM stats_voice_sessions
                   WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1""",
                (1, 42),
            )

        self.assertEqual(room, [(20, 10)])
        self.assertEqual(session, [(10,)])

    async def test_private_channel_messages_still_count_for_user_activity(self):
        await database.record_stats_message(
            1,
            100,
            10,
            "family-chat",
            42,
            "2026-07-20T12:00:00+00:00",
            True,
        )
        await database.record_stats_message(
            2,
            100,
            20,
            "owner-ticket",
            42,
            "2026-07-20T12:01:00+00:00",
            False,
        )

        data = await database.get_weekly_stats_data(
            100,
            "2026-07-20T00:00:00+00:00",
            "2026-07-27T00:00:00+00:00",
        )

        channels = {
            channel_id: family_visible
            for channel_id, _, _, family_visible in data["text_channels"]
        }
        self.assertTrue(channels[10])
        self.assertFalse(channels[20])
        self.assertEqual(data["user_messages"], [(42, 2)])

    async def test_channel_delete_closes_session_and_recovery_never_ends_before_start(self):
        self.assertTrue(await database.create_voice_room(202, 30))
        await database.transition_stats_voice_session(1, 50, 202, "Temporary")
        await database.record_stats_channel_event(1, 202, "Temporary", "delete")

        started_at = "2026-07-28T12:00:00+00:00"
        async with database._connect() as db:
            closed = await db.execute_fetchall(
                """SELECT ended_at, room_creator_id FROM stats_voice_sessions
                   WHERE channel_id = ? ORDER BY id DESC LIMIT 1""",
                (202,),
            )
            await db.execute(
                """INSERT INTO stats_voice_sessions
                   (guild_id, user_id, channel_id, channel_name, started_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (1, 51, 203, "Recovered", started_at),
            )
            await db.commit()

        self.assertIsNotNone(closed[0][0])
        self.assertEqual(closed[0][1], 30)

        await database.reset_stats_voice_sessions(
            1,
            "2026-07-28T11:55:00+00:00",
            (),
        )
        async with database._connect() as db:
            recovered = await db.execute_fetchall(
                """SELECT ended_at FROM stats_voice_sessions
                   WHERE channel_id = ? ORDER BY id DESC LIMIT 1""",
                (203,),
            )
        self.assertEqual(recovered, [(started_at,)])


class WeeklyStatsResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_after_reset_reinitializes_live_state(self):
        cog = WeeklyStatsCog(SimpleNamespace(guilds=[]))
        cog._sessions_restored = True
        cog._coin_voice_initialized = True
        cog._economy_settings_cache[1] = (1.0, SimpleNamespace())
        observed = []

        async def restore():
            observed.append(
                (
                    "restore",
                    cog._sessions_restored,
                    cog._coin_voice_initialized,
                    dict(cog._economy_settings_cache),
                )
            )

        async def heartbeat():
            observed.append(("heartbeat", cog._sessions_restored))

        with (
            patch.object(cog, "_restore_voice_sessions", AsyncMock(side_effect=restore)),
            patch.object(cog, "_record_voice_heartbeats", AsyncMock(side_effect=heartbeat)),
        ):
            await cog.restore_after_reset()

        self.assertEqual(observed[0], ("restore", False, False, {}))
        self.assertEqual(observed[1], ("heartbeat", True))
        self.assertTrue(cog._sessions_restored)


class WeeklyStatsRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_week_finalizes_previous_message_and_creates_current(self):
        guild = SimpleNamespace(id=100)
        current_start = datetime(2026, 7, 27, tzinfo=UTC)
        current_end = current_start + timedelta(days=7)
        old_message = SimpleNamespace(id=1)
        current_message = SimpleNamespace(id=2)
        upsert = AsyncMock(side_effect=[old_message, current_message])
        settings = AsyncMock()

        with (
            patch.dict(
                update_weekly_stats_message.__globals__,
                {
                    "_week_period": lambda _now=None: (
                        current_start,
                        current_end,
                        current_start,
                        current_end,
                    ),
                    "get_setting": AsyncMock(return_value="2026-07-20"),
                    "_upsert_weekly_stats_message": upsert,
                    "set_setting": settings,
                },
            ),
        ):
            result = await update_weekly_stats_message(guild)

        self.assertIs(result, current_message)
        self.assertEqual(upsert.await_count, 2)
        self.assertEqual(upsert.await_args_list[0].args[1], date(2026, 7, 20))
        self.assertFalse(
            upsert.await_args_list[0].kwargs["allow_legacy_adoption"]
        )
        self.assertEqual(upsert.await_args_list[1].args[1], date(2026, 7, 27))
        self.assertFalse(
            upsert.await_args_list[1].kwargs["allow_legacy_adoption"]
        )
        # Three writes: the guild-scoped message id, the legacy global key kept
        # in step for one release, and the week marker.
        self.assertEqual(settings.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in settings.await_args_list],
            [
                "weekly_stats_message_id:100",
                "weekly_stats_message_id",
                "weekly_stats_current_week:100",
            ],
        )

    async def test_first_week_adopts_existing_legacy_message(self):
        guild = SimpleNamespace(id=100)
        current_start = datetime(2026, 7, 27, tzinfo=UTC)
        current_end = current_start + timedelta(days=7)
        current_message = SimpleNamespace(id=2)
        upsert = AsyncMock(return_value=current_message)

        with patch.dict(
            update_weekly_stats_message.__globals__,
            {
                "_week_period": lambda _now=None: (
                    current_start,
                    current_end,
                    current_start,
                    current_end,
                ),
                "get_setting": AsyncMock(return_value=None),
                "_upsert_weekly_stats_message": upsert,
                "set_setting": AsyncMock(),
            },
        ):
            result = await update_weekly_stats_message(guild)

        self.assertIs(result, current_message)
        self.assertTrue(upsert.await_args.kwargs["allow_legacy_adoption"])


if __name__ == "__main__":
    unittest.main()
