import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.dev import ConfirmServerResetView, DevCog
from cogs.groups import _channel_slug
from cogs.technical_status import TechnicalStatusCog
from utils import access


class AccessAndGroupsTests(unittest.TestCase):
    def test_configured_bot_owner_bypasses_administrator_requirement(self):
        member = SimpleNamespace(id=777)
        with patch.object(access, "BOT_OWNER_ID", 777):
            self.assertTrue(access.is_bot_owner(member))
            self.assertTrue(access.has_administrator_access(member))

    def test_global_owner_bypass_wraps_every_command_check(self):
        async def denied(_interaction):
            return False

        command = SimpleNamespace(checks=[denied])
        tree = SimpleNamespace(walk_commands=lambda: [command])
        interaction = SimpleNamespace(user=SimpleNamespace(id=777))

        with (
            patch.object(access, "BOT_OWNER_ID", 777),
            patch.object(access, "_record_owner_bypass", new_callable=AsyncMock),
        ):
            self.assertEqual(access.install_bot_owner_command_bypass(tree), 1)
            self.assertTrue(asyncio.run(command.checks[0](interaction)))
            self.assertEqual(access.install_bot_owner_command_bypass(tree), 0)

        interaction.user.id = 888
        self.assertFalse(asyncio.run(command.checks[0](interaction)))

    def test_vabority_rejects_server_administrator_who_is_not_bot_owner(self):
        owner_check = TechnicalStatusCog.vabority.checks[0]
        administrator = SimpleNamespace(
            id=888,
            guild_permissions=SimpleNamespace(administrator=True),
        )
        owner = SimpleNamespace(
            id=777,
            guild_permissions=SimpleNamespace(administrator=False),
        )

        with patch.object(access, "BOT_OWNER_ID", 777):
            self.assertFalse(
                asyncio.run(owner_check(SimpleNamespace(user=administrator)))
            )
            self.assertTrue(asyncio.run(owner_check(SimpleNamespace(user=owner))))

    def test_dev_reset_rejects_server_administrator_who_is_not_bot_owner(self):
        owner_check = DevCog.dev_reset_server.checks[-1]
        administrator = SimpleNamespace(
            id=888,
            guild_permissions=SimpleNamespace(administrator=True),
        )
        owner = SimpleNamespace(
            id=777,
            guild_permissions=SimpleNamespace(administrator=False),
        )

        with patch.object(access, "BOT_OWNER_ID", 777):
            self.assertFalse(
                asyncio.run(owner_check(SimpleNamespace(user=administrator)))
            )
            self.assertTrue(asyncio.run(owner_check(SimpleNamespace(user=owner))))

    def test_group_channel_name_is_safe_and_compact(self):
        self.assertEqual(_channel_slug("  Важная группа № 1  "), "важная-группа-1")


class DevResetIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_stops_music_and_restores_live_statistics(self):
        music = SimpleNamespace(shutdown_guild=AsyncMock(return_value=(2, True)))
        stats = SimpleNamespace(
            discard_pending_events=AsyncMock(return_value=0),
            restore_after_reset=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=5,
            threads=[],
            text_channels=[],
            forums=[],
        )
        client = SimpleNamespace(
            get_cog=lambda name: {
                "MusicCog": music,
                "WeeklyStatsCog": stats,
            }.get(name),
        )
        interaction = SimpleNamespace(
            guild=guild,
            client=client,
            user=SimpleNamespace(id=777),
        )
        reset_database = AsyncMock()
        callback_globals = ConfirmServerResetView._perform_reset.__globals__

        with patch.dict(
            callback_globals,
            {
                "get_voice_channel": lambda *_args, **_kwargs: None,
                "get_category": lambda *_args, **_kwargs: None,
                "reset_database_data": reset_database,
                "set_static_panels_enabled": AsyncMock(),
            },
        ):
            result = await ConfirmServerResetView(777)._perform_reset(interaction)

        music.shutdown_guild.assert_awaited_once_with(
            guild,
            reason="server_reset:777",
        )
        reset_database.assert_awaited_once()
        stats.discard_pending_events.assert_awaited_once()
        stats.restore_after_reset.assert_awaited_once()
        self.assertIn("База данных: **очищена**", result)
