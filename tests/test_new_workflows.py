import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from repositories.anonymous import (
    close_anonymous_conversation_by_message,
    close_anonymous_conversation_by_staff_message,
    create_anonymous_conversation,
    get_anonymous_conversation_by_message,
    get_anonymous_message_links,
    link_anonymous_message,
)
from cogs.anonymous_relay import (
    AnonymousStaffReplyView,
    AnonymousUserReplyView,
    _author_reply_embed,
)
from repositories.game_online import (
    get_game_online_days,
    record_game_online_sample,
)
from repositories.requests import (
    claim_nickname_change_request,
    complete_nickname_change_delivery,
    confirm_nickname_change,
    log_request,
)
from utils import database


class NewWorkflowRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "workflows.db"
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_nickname_request_is_bound_to_applicant_dm(self):
        await log_request(
            "FI-0001",
            "family_invite",
            42,
            "application",
            guild_id=1,
        )

        self.assertEqual(
            await claim_nickname_change_request("FI-0001", 777),
            "claimed",
        )
        self.assertEqual(
            await claim_nickname_change_request("FI-0001", 888),
            "sending",
        )
        self.assertTrue(
            await complete_nickname_change_delivery("FI-0001", 9_001)
        )
        self.assertIsNone(await confirm_nickname_change(9_001, 999))
        decision = await confirm_nickname_change(9_001, 42)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.nickname_status, "confirmed")
        self.assertEqual(decision.nickname_requested_by, 777)
        self.assertIsNone(await confirm_nickname_change(9_001, 42))

    async def test_anonymous_messages_resolve_to_same_conversation(self):
        conversation = await create_anonymous_conversation(
            guild_id=1,
            number="ANR-0001",
            author_id=42,
            public_channel_id=100,
            public_message_id=101,
            log_channel_id=200,
            log_message_id=201,
            thread_id=250,
            thread_message_id=251,
        )
        await link_anonymous_message(
            conversation.id,
            message_id=301,
            channel_id=300,
            direction="staff_dm",
        )

        for message_id in (101, 201, 251, 301):
            resolved = await get_anonymous_conversation_by_message(message_id)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.id, conversation.id)
            self.assertEqual(resolved.author_id, 42)
            self.assertEqual(resolved.thread_id, 250)

        links = await get_anonymous_message_links(conversation.id)
        self.assertEqual(
            [(link.message_id, link.channel_id, link.direction) for link in links],
            [
                (101, 100, "public"),
                (201, 200, "log"),
                (251, 250, "thread"),
                (301, 300, "staff_dm"),
            ],
        )

        author_embed = _author_reply_embed(conversation, "Ответ без раскрытия автора")
        rendered = " ".join(
            [
                author_embed.title,
                author_embed.summary or "",
                *(f"{name} {value}" for name, value in author_embed.details),
            ]
        )
        self.assertNotIn("<@42>", rendered)
        self.assertNotIn("Автор", {name for name, _ in author_embed.details})

    async def test_anonymous_reply_opens_modal_before_database_lookup(self):
        view = AnonymousStaffReplyView()
        button = next(
            item
            for item in view.children
            if item.custom_id == "anonymous:staff_reply"
        )
        interaction = SimpleNamespace(
            message=SimpleNamespace(id=12_345),
            response=SimpleNamespace(send_modal=AsyncMock()),
        )
        lookup = AsyncMock()
        callback_globals = button.callback.callback.__globals__

        with (
            patch("cogs.forms._has_staff_access", return_value=True),
            patch.dict(
                callback_globals,
                {"get_anonymous_conversation_by_message": lookup},
            ),
        ):
            await button.callback(interaction)

        lookup.assert_not_awaited()
        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual(modal.source_message_id, 12_345)

    async def test_author_can_close_anonymous_conversation_from_dm(self):
        conversation = await create_anonymous_conversation(
            guild_id=1,
            number="ANR-0002",
            author_id=42,
            public_channel_id=100,
            public_message_id=102,
            log_channel_id=200,
            log_message_id=202,
            thread_id=250,
            thread_message_id=252,
        )
        await link_anonymous_message(
            conversation.id,
            message_id=302,
            channel_id=300,
            direction="staff_dm",
        )

        closed = await close_anonymous_conversation_by_message(302, 42)

        self.assertIsNotNone(closed)
        self.assertEqual(closed.status, "closed")
        self.assertIsNone(
            await close_anonymous_conversation_by_message(302, 42)
        )
        self.assertIsNone(
            await close_anonymous_conversation_by_message(302, 999)
        )
        resolved = await get_anonymous_conversation_by_message(302)
        self.assertEqual(resolved.status, "closed")

    async def test_staff_can_close_anonymous_conversation_from_public_message(self):
        conversation = await create_anonymous_conversation(
            guild_id=1,
            number="ANR-0003",
            author_id=42,
            public_channel_id=100,
            public_message_id=103,
            log_channel_id=200,
            log_message_id=203,
            thread_id=250,
            thread_message_id=253,
        )

        closed = await close_anonymous_conversation_by_staff_message(103)

        self.assertIsNotNone(closed)
        self.assertEqual(closed.id, conversation.id)
        self.assertEqual(closed.status, "closed")
        self.assertIsNone(
            await close_anonymous_conversation_by_staff_message(103)
        )
        self.assertIsNone(
            await close_anonymous_conversation_by_message(103, 42)
        )

    def test_anonymous_dm_and_staff_panel_both_have_close_action(self):
        self.assertEqual(
            {item.custom_id for item in AnonymousUserReplyView().children},
            {"anonymous:user_reply", "anonymous:user_close"},
        )
        self.assertEqual(
            {item.custom_id for item in AnonymousStaffReplyView().children},
            {"anonymous:staff_reply", "anonymous:staff_close"},
        )

    async def test_staff_close_button_checks_access_before_repository(self):
        view = AnonymousStaffReplyView()
        button = next(
            item
            for item in view.children
            if item.custom_id == "anonymous:staff_close"
        )
        interaction = SimpleNamespace(message=SimpleNamespace(id=123))
        close_conversation = AsyncMock()
        respond = AsyncMock()
        callback_globals = button.callback.callback.__globals__

        with (
            patch("cogs.forms._has_staff_access", return_value=False),
            patch.dict(
                callback_globals,
                {
                    "close_anonymous_conversation_by_staff_message": close_conversation,
                    "respond_v2": respond,
                },
            ),
        ):
            await button.callback(interaction)

        close_conversation.assert_not_awaited()
        respond.assert_awaited_once_with(
            interaction,
            "Нет прав.",
            ephemeral=True,
        )

    async def test_game_online_daily_aggregation_tracks_peak_and_average(self):
        await record_game_online_sample(
            "Milton",
            "GTA 5 RP Milton",
            100,
            observed_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
        )
        await record_game_online_sample(
            "Milton",
            "GTA 5 RP Milton",
            180,
            observed_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )
        rows = await get_game_online_days("milton", since_day="2026-07-20")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].peak_players, 180)
        self.assertEqual(rows[0].minimum_players, 100)
        self.assertEqual(rows[0].average_players, 140)
        self.assertEqual(rows[0].sample_count, 2)
        self.assertEqual(rows[0].last_players, 180)


if __name__ == "__main__":
    unittest.main()
