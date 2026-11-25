from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from config import GUILD_ID, is_authorized_guild


class AuthorizedGuildTests(unittest.TestCase):
    def test_active_env_guild_is_authorized(self) -> None:
        self.assertTrue(is_authorized_guild(GUILD_ID))

    def test_foreign_guild_is_rejected(self) -> None:
        self.assertFalse(is_authorized_guild(GUILD_ID + 1))
        self.assertFalse(is_authorized_guild(None))

    def test_leave_helper_skips_authorized_guild(self) -> None:
        from core.bot import GuinnessBot

        bot = GuinnessBot.__new__(GuinnessBot)
        guild = SimpleNamespace(id=GUILD_ID, name="ok", leave=mock.AsyncMock())

        async def run() -> None:
            await GuinnessBot._leave_unauthorized_guild(
                bot, guild, reason="unit"
            )

        import asyncio

        asyncio.run(run())
        guild.leave.assert_not_called()

    def test_leave_helper_leaves_foreign_guild(self) -> None:
        from core.bot import GuinnessBot

        bot = GuinnessBot.__new__(GuinnessBot)
        guild = SimpleNamespace(id=GUILD_ID + 99, name="foreign", leave=mock.AsyncMock())

        async def run() -> None:
            await GuinnessBot._leave_unauthorized_guild(
                bot, guild, reason="unit"
            )

        import asyncio

        asyncio.run(run())
        guild.leave.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
