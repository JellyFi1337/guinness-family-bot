import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs import fundraiser, polls


class _TextChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fundraiser_reconcile_refreshes_an_existing_panel(self):
        channel = _TextChannel(200)
        guild = SimpleNamespace(get_channel=lambda channel_id: channel)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        cog = fundraiser.FundraiserCog(bot)

        with (
            patch.object(fundraiser.discord, "TextChannel", _TextChannel),
            patch.object(
                fundraiser,
                "get_fundraiser_panels",
                AsyncMock(return_value=[(10, 100, 200, 300)]),
            ),
            patch.object(
                fundraiser,
                "update_fundraiser_message",
                AsyncMock(),
            ) as update,
        ):
            await cog.reconcile_panels()

        update.assert_awaited_once_with(channel, 10)

    async def test_fundraiser_refresh_is_serialized_per_fundraiser(self):
        message = SimpleNamespace(id=300)
        channel = _TextChannel(200)
        channel.fetch_message = AsyncMock(return_value=message)
        active = 0
        peak = 0

        async def edit(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

        with (
            patch.object(
                fundraiser,
                "get_fundraiser_by_channel",
                AsyncMock(return_value=(10, 400, "Игрок", 500, 300, 100)),
            ),
            patch.object(
                fundraiser,
                "get_contributions",
                AsyncMock(return_value=[]),
            ),
            patch.object(fundraiser, "edit_message_v2", AsyncMock(side_effect=edit)) as edit_mock,
        ):
            await asyncio.gather(
                fundraiser.update_fundraiser_message(channel, 10),
                fundraiser.update_fundraiser_message(channel, 10),
            )

        self.assertEqual(edit_mock.await_count, 2)
        self.assertEqual(peak, 1)

    async def test_raw_and_bulk_deletes_mark_poll_panels_missing(self):
        cog = polls.PollsCog(SimpleNamespace())
        resolve = AsyncMock(side_effect=lambda guild_id, message_id: message_id - 900)
        mark = AsyncMock(return_value=True)
        bulk_mark = AsyncMock(return_value=(2, 3))

        with (
            patch.object(polls, "get_poll_id_by_message", resolve),
            patch.object(polls, "mark_poll_message_missing", mark),
            patch.object(polls, "mark_poll_messages_missing", bulk_mark),
        ):
            await cog.on_raw_message_delete(
                SimpleNamespace(guild_id=100, message_id=901)
            )
            await cog.on_raw_bulk_message_delete(
                SimpleNamespace(guild_id=100, message_ids={902, 903})
            )

        resolve.assert_awaited_once_with(100, 901)
        mark.assert_awaited_once_with(
            1,
            guild_id=100,
            message_id=901,
        )
        bulk_mark.assert_awaited_once_with(100, {902, 903})


if __name__ == "__main__":
    unittest.main()
