import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import discord

from cogs.recruiting import (
    RecruitingCog,
    _upsert_recruiting_log,
    build_recruiting_embed,
    record_family_decision,
)
from cogs.admin import AdminCog
from repositories.recruiting import RecruiterTotals, RecruitingSummary


class RecruitingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_recruiting_embed_has_compact_funnel_and_period_leaders(self):
        guild = SimpleNamespace(id=5, members=[])
        summary = RecruitingSummary(
            joined=3,
            applied=2,
            accepted=1,
            rejected=0,
            pending=1,
            per_recruiter=(
                RecruiterTotals(201, 2, 2, 1),
                RecruiterTotals(202, 1, 0, 0),
            ),
            accepted_recent=((201, 1),),
            accepted_total=((201, 1),),
        )

        with patch.dict(
            build_recruiting_embed.__globals__,
            {"get_recruiting_summary": AsyncMock(return_value=summary)},
        ):
            embed = await build_recruiting_embed(guild)

        fields = dict(embed.details)
        self.assertEqual(embed.title, "Рекрутинг · сводка")
        self.assertIn("3", fields["Воронка"])
        self.assertIn("2", fields["Воронка"])
        self.assertEqual(fields["Ожидают решения"], "**1**")
        self.assertIn("Результаты рекрутеров", fields)
        self.assertIn("Приняты за 7 дней", fields)
        self.assertIn("Всего принято", fields)

    async def test_recruiting_log_edits_existing_marker_message(self):
        class HistoryChannel:
            def __init__(self, message):
                self.message = message

            def history(self, *, limit):
                async def iterate():
                    yield self.message

                return iterate()

        bot_member = SimpleNamespace(id=900)
        existing = SimpleNamespace(author=bot_member)
        channel = HistoryChannel(existing)
        guild = SimpleNamespace(id=5, me=bot_member)
        edited = SimpleNamespace(id=123)
        edit = AsyncMock(return_value=edited)

        with patch.dict(
            _upsert_recruiting_log.__globals__,
            {
                "get_text_channel": lambda *_args: channel,
                "_has_marker": lambda *_args: True,
                "edit_message_v2": edit,
            },
        ):
            result = await _upsert_recruiting_log(
                guild,
                discord.Embed(title="Обновлённый статус"),
                marker="recruiting-application:5:FI-0001",
            )

        self.assertIs(result, edited)
        edit.assert_awaited_once()
        self.assertEqual(
            edit.await_args.kwargs["component_marker"],
            "recruiting-application:5:FI-0001",
        )

    async def test_recruiting_log_skips_history_for_new_card(self):
        class NewCardChannel:
            def history(self, *, limit):
                raise AssertionError("new application must not scan channel history")

        channel = NewCardChannel()
        guild = SimpleNamespace(id=5, me=SimpleNamespace(id=900))
        sent = SimpleNamespace(id=124)
        send = AsyncMock(return_value=sent)

        with patch.dict(
            _upsert_recruiting_log.__globals__,
            {
                "get_text_channel": lambda *_args: channel,
                "send_v2": send,
            },
        ):
            result = await _upsert_recruiting_log(
                guild,
                discord.Embed(title="Новая заявка"),
                marker="recruiting-application:5:FI-0002",
                find_existing=False,
            )

        self.assertIs(result, sent)
        send.assert_awaited_once()

    async def test_stale_recruiting_decision_preserves_existing_card(self):
        class HistoryChannel:
            def __init__(self, message):
                self.message = message

            def history(self, *, limit):
                async def iterate():
                    yield self.message

                return iterate()

        bot_member = SimpleNamespace(id=900)
        existing = SimpleNamespace(author=bot_member)
        channel = HistoryChannel(existing)
        guild = SimpleNamespace(id=5, me=bot_member)
        edit = AsyncMock()

        with patch.dict(
            _upsert_recruiting_log.__globals__,
            {
                "get_text_channel": lambda *_args: channel,
                "_has_marker": lambda *_args: True,
                "edit_message_v2": edit,
            },
        ):
            result = await _upsert_recruiting_log(
                guild,
                discord.Embed(title="Повторное решение"),
                marker="recruiting-application:5:FI-0001",
                edit_existing=False,
            )

        self.assertIs(result, existing)
        edit.assert_not_awaited()

    async def test_recruiting_decision_updates_application_log_card(self):
        guild = SimpleNamespace(id=5)
        member = SimpleNamespace(id=101)
        reviewer = SimpleNamespace(id=301)
        bot = SimpleNamespace(get_cog=Mock(return_value=None))
        upsert = AsyncMock()

        with patch.dict(
            record_family_decision.__globals__,
            {
                "decide_recruiting_referral": AsyncMock(
                    return_value=(201, True)
                ),
                "_upsert_recruiting_log": upsert,
            },
        ):
            await record_family_decision(
                bot,
                guild,
                member,
                "FI-0001",
                True,
                reviewer,
                "Роль Main",
            )

        self.assertEqual(
            upsert.await_args.kwargs["marker"],
            "recruiting-application:5:FI-0001",
        )
        updated_embed = upsert.await_args.args[1]
        self.assertEqual(updated_embed.title, "Заявка FI-0001 · принята")
        self.assertIn("той же карточке", updated_embed.summary)
        fields = dict(updated_embed.details)
        self.assertEqual(fields["Выданная роль"], "Роль Main")
        self.assertTrue(upsert.await_args.kwargs["edit_existing"])

    async def test_recruiting_log_keeps_persisted_decision_on_stale_action(self):
        guild = SimpleNamespace(id=5)
        member = SimpleNamespace(id=101)
        reviewer = SimpleNamespace(id=999)
        bot = SimpleNamespace(get_cog=Mock(return_value=None))
        upsert = AsyncMock()

        with patch.dict(
            record_family_decision.__globals__,
            {
                "decide_recruiting_referral": AsyncMock(
                    return_value=(201, False)
                ),
                "get_referral_decision": AsyncMock(
                    return_value=("accepted", 301)
                ),
                "_upsert_recruiting_log": upsert,
            },
        ):
            await record_family_decision(
                bot,
                guild,
                member,
                "FI-0001",
                False,
                reviewer,
                "Устаревший отказ",
            )

        updated_embed = upsert.await_args.args[1]
        self.assertEqual(updated_embed.title, "Заявка FI-0001 · принята")
        self.assertIn("Ранее зафиксированное решение", updated_embed.summary)
        fields = dict(updated_embed.details)
        self.assertEqual(fields["Решение принял"], "<@301>")
        self.assertFalse(upsert.await_args.kwargs["edit_existing"])

    async def test_setup_guinness_refreshes_recruiting_static_panel(self):
        guild = SimpleNamespace(id=5)
        recruiting = SimpleNamespace(refresh_stats=AsyncMock())
        bot = SimpleNamespace(
            get_cog=Mock(return_value=recruiting),
        )
        interaction = SimpleNamespace(
            guild=guild,
            client=bot,
            response=SimpleNamespace(defer=AsyncMock()),
        )
        cog = AdminCog(bot)

        callback = AdminCog.setup_guinness_slash.callback
        with patch.dict(
            callback.__globals__,
            {
                "set_static_panels_enabled": AsyncMock(),
                "setup_all_persistent_messages": AsyncMock(),
                "followup_v2": AsyncMock(),
            },
        ):
            await AdminCog.setup_guinness_slash.callback(cog, interaction)

        recruiting.refresh_stats.assert_awaited_once_with(guild)

    async def test_on_ready_does_not_duplicate_supervised_initialization(self):
        guild = SimpleNamespace(id=10)
        bot = SimpleNamespace(guilds=[guild], task_supervisor=object())
        cog = RecruitingCog(bot)
        cog.ensure_guild = AsyncMock()
        cog.refresh_stats = AsyncMock()

        await cog.on_ready()

        cog.ensure_guild.assert_not_awaited()
        cog.refresh_stats.assert_not_awaited()

    async def test_supervised_worker_owns_full_initialization(self):
        guild = SimpleNamespace(id=20)
        bot = SimpleNamespace(guilds=[guild], is_closed=Mock(return_value=False))
        cog = RecruitingCog(bot)
        cog.ensure_guild = AsyncMock()
        cog.refresh_stats = AsyncMock()

        await cog.stats_updater()

        cog.ensure_guild.assert_awaited_once_with(guild)
        cog.refresh_stats.assert_awaited_once_with(guild)

    async def test_disconnect_during_shutdown_is_not_logged_as_error(self):
        guild = SimpleNamespace(id=30)
        bot = SimpleNamespace(guilds=[guild], is_closed=Mock(return_value=True))
        cog = RecruitingCog(bot)
        cog.ensure_guild = AsyncMock(
            side_effect=aiohttp.ServerDisconnectedError()
        )
        cog.refresh_stats = AsyncMock()

        with (
            patch("cogs.recruiting.logger.info") as info,
            patch("cogs.recruiting.logger.exception") as error,
        ):
            await cog.stats_updater()

        info.assert_called_once()
        error.assert_not_called()
        cog.refresh_stats.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
