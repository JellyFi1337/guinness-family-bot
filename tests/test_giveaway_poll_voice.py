import unittest
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import discord

from cogs.giveaways import _can_finish_giveaway, _giveaway_result_embed
from cogs.polls import _anonymous_poll_log_embed
from cogs.voice_rooms import VoiceRoomsCog
from repositories.polls import PollRecord
from utils import database


class GiveawayPollVoiceTests(unittest.TestCase):
    def test_only_creator_or_administrator_can_finish_giveaway(self):
        creator = SimpleNamespace(id=10)
        staff = SimpleNamespace(id=20)

        with patch.dict(
            _can_finish_giveaway.__globals__,
            {"has_administrator_access": lambda _: False},
        ):
            self.assertTrue(_can_finish_giveaway(creator, 10))
            self.assertFalse(_can_finish_giveaway(staff, 10))
            owner = SimpleNamespace(
                id=30,
                roles=[SimpleNamespace(name="Owner")],
            )
            self.assertTrue(_can_finish_giveaway(owner, 10))

        with patch.dict(
            _can_finish_giveaway.__globals__,
            {"has_administrator_access": lambda _: True},
        ):
            self.assertTrue(_can_finish_giveaway(staff, 10))

    def test_forced_giveaway_result_names_the_administrator(self):
        embed = _giveaway_result_embed(
            "Тестовый розыгрыш",
            [101],
            forced_by_id=202,
        )

        self.assertEqual(embed.details[-1][0], "Принудительно завершил")
        self.assertEqual(embed.details[-1][1], "<@202>")

    def test_manual_finish_can_name_the_creator_too(self):
        embed = _giveaway_result_embed(
            "Тестовый розыгрыш",
            [101],
            forced_by_id=10,
        )

        self.assertEqual(embed.details[-1][1], "<@10>")

    def test_regular_giveaway_result_has_no_forced_completion_field(self):
        embed = _giveaway_result_embed("Тестовый розыгрыш", [101])

        self.assertNotIn(
            "Принудительно завершил",
            {name for name, _ in embed.details},
        )

    def test_anonymous_poll_log_reveals_creator_only_in_audit_embed(self):
        poll = PollRecord(
            id=7,
            guild_id=1,
            channel_id=2,
            message_id=3,
            creator_id=4,
            question="Анонимный вопрос",
            options=("Да", "Нет"),
            multiple=False,
            anonymous=True,
            closes_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            status="open",
            ui_revision=0,
        )

        embed = _anonymous_poll_log_embed(
            poll,
            SimpleNamespace(jump_url="https://discord.com/channels/1/2/3"),
        )

        fields = dict(embed.details)
        self.assertEqual(fields["Инициатор"], "<@4>")
        self.assertEqual(fields["Канал"], "<#2>")
        self.assertEqual(
            fields["Опрос"],
            "https://discord.com/channels/1/2/3",
        )

    def test_give_access_uses_a_mentionable_parameter(self):
        command = VoiceRoomsCog.give_access
        parameter = next(item for item in command.parameters if item.name == "цель")

        self.assertEqual(parameter.type, discord.AppCommandOptionType.mentionable)

    def test_stale_voice_control_view_is_stopped_without_client_remove_view(self):
        bot = SimpleNamespace()
        cog = VoiceRoomsCog(bot)
        view = SimpleNamespace(
            is_finished=Mock(return_value=False),
            stop=Mock(),
        )
        cog._registered_views[777] = (888, view)

        cog._remove_control_view(777)

        view.stop.assert_called_once_with()
        self.assertNotIn(777, cog._registered_views)


class GiveawayOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "giveaway.db",
        )
        self.db_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def test_forced_finisher_is_persisted_for_retry_worker(self):
        giveaway_id = await database.create_giveaway(
            "Test",
            "Prize",
            1,
            10,
            20,
            "2999-01-01T00:00:00+00:00",
            guild_id=1,
        )

        prepared = await database.prepare_giveaway_draw(
            giveaway_id,
            [30],
            forced_by_id=99,
        )

        self.assertIsNotNone(prepared)
        async with database._connect() as db:
            row = await db.execute_fetchall(
                "SELECT payload_json FROM operation_outbox "
                "WHERE kind = 'giveaway.announce'"
            )
        self.assertEqual(json.loads(row[0][0])["forced_by_id"], 99)


if __name__ == "__main__":
    unittest.main()
