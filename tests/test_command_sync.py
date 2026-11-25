import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.bot import (
    GuinnessBot,
    _command_fingerprint,
)


class CommandSyncTests(unittest.IsolatedAsyncioTestCase):
    def test_fingerprint_ignores_discord_generated_fields(self):
        local = [
            {
                "name": "help",
                "description": "Помощь",
                "type": 1,
                "options": [],
            }
        ]
        remote = [
            {
                **local[0],
                "id": 123,
                "application_id": 456,
                "version": 789,
                "name_localizations": None,
            }
        ]

        self.assertEqual(
            _command_fingerprint(local),
            _command_fingerprint(remote),
        )

    async def test_unchanged_cached_schema_skips_all_discord_requests(self):
        payload = [
            {
                "name": "help",
                "description": "Помощь",
                "type": 1,
                "options": [],
            }
        ]
        fingerprint = _command_fingerprint(payload)
        tree = SimpleNamespace(
            fetch_commands=AsyncMock(),
            sync=AsyncMock(),
        )
        bot = SimpleNamespace(
            tree=tree,
            _local_command_payload=Mock(return_value=payload),
        )
        callback = GuinnessBot._sync_command_scope

        with patch.dict(
            callback.__globals__,
            {
                "get_setting": AsyncMock(return_value=fingerprint),
                "set_setting": AsyncMock(),
                "COMMAND_SYNC_FORCE": False,
            },
        ):
            await callback(bot, None, "global")

        tree.fetch_commands.assert_not_awaited()
        tree.sync.assert_not_awaited()

    async def test_matching_remote_schema_is_adopted_without_bulk_put(self):
        payload = [
            {
                "name": "help",
                "description": "Помощь",
                "type": 1,
                "options": [],
            }
        ]
        remote_command = object()
        tree = SimpleNamespace(
            fetch_commands=AsyncMock(return_value=[remote_command]),
            sync=AsyncMock(),
        )
        bot = SimpleNamespace(
            tree=tree,
            _local_command_payload=Mock(return_value=payload),
        )
        save = AsyncMock()
        callback = GuinnessBot._sync_command_scope

        with patch.dict(
            callback.__globals__,
            {
                "get_setting": AsyncMock(return_value=None),
                "set_setting": save,
                "_remote_command_payload": lambda command: payload[0],
                "COMMAND_SYNC_FORCE": False,
            },
        ):
            await callback(bot, SimpleNamespace(id=1), "guild:1")

        tree.fetch_commands.assert_awaited_once()
        tree.sync.assert_not_awaited()
        save.assert_awaited_once()

    async def test_guild_start_does_not_clear_global_commands_by_default(self):
        tree = SimpleNamespace(copy_global_to=Mock())
        bot = SimpleNamespace(
            tree=tree,
            _command_sync_lock=asyncio.Lock(),
            _commands_synced=False,
            _sync_command_scope=AsyncMock(),
            _clear_global_commands_once=AsyncMock(),
        )
        callback = GuinnessBot._sync_commands

        with patch.dict(
            callback.__globals__,
            {
                "GUILD_ID": 123,
                "COMMAND_CLEAR_GLOBAL": False,
            },
        ):
            await callback(bot)

        tree.copy_global_to.assert_called_once()
        bot._clear_global_commands_once.assert_not_awaited()
        bot._sync_command_scope.assert_awaited_once()
        self.assertTrue(bot._commands_synced)


if __name__ == "__main__":
    unittest.main()
