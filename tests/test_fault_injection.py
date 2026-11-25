"""Fail between every step of a multi-step operation and check what survives.

The audit's release gate asks for fault injection "между каждым шагом
Discord/БД". These tests do exactly that for the two flows that were rewritten
to be recoverable: ticket provisioning and ticket closing. The property under
test is never "no error happened" — it is "after the error, the recorded state
still says the truth, and a retry does not duplicate anything".
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repositories.tickets import (
    ACTIVE,
    CLOSED,
    CLOSING,
    FAILED,
    PROVISIONING,
    activate_ticket,
    attach_ticket_channel,
    begin_ticket,
    begin_ticket_close,
    channel_marker,
    complete_ticket_close,
    fail_ticket,
    get_stuck_tickets,
    get_ticket_by_channel,
    get_ticket_by_interaction,
)
from utils import database


GUILD = 1500000000000000001


class _TicketFlowCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            database, "DB_PATH", Path(self._temp.name) / "faults.db"
        )
        self._patch.start()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        await database.close_db()
        self._patch.stop()
        self._temp.cleanup()

    async def _begin(self, interaction_id: int = 4001):
        return await begin_ticket(
            guild_id=GUILD,
            interaction_id=interaction_id,
            ticket_type="жалоба",
            number="zhaloba-0001",
            author_id=77,
        )


class ProvisioningFaultTests(_TicketFlowCase):
    async def test_failure_before_the_channel_leaves_a_provisioning_record(self):
        """The old code left nothing at all, so nobody could tell it happened."""

        await self._begin()
        ticket = await get_ticket_by_interaction(4001)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.status, PROVISIONING)
        self.assertIsNone(ticket.channel_id)

    async def test_failure_after_the_channel_records_the_channel(self):
        """This is what stops a retry from creating a second channel."""

        await self._begin()
        await attach_ticket_channel(4001, 900001)
        ticket = await get_ticket_by_interaction(4001)
        self.assertEqual(ticket.channel_id, 900001)
        self.assertEqual(ticket.status, PROVISIONING)
        self.assertFalse(ticket.is_provisioned)

    async def test_a_retry_of_the_same_interaction_does_not_start_over(self):
        first = await self._begin()
        await attach_ticket_channel(4001, 900001)
        second = await self._begin()
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.channel_id, 900001)

    async def test_a_completed_ticket_is_recognised_on_retry(self):
        await self._begin()
        await attach_ticket_channel(4001, 900001)
        await activate_ticket(4001, 900002)
        ticket = await self._begin()
        self.assertTrue(ticket.is_provisioned)
        self.assertEqual(ticket.status, ACTIVE)

    async def test_a_recorded_failure_keeps_its_reason(self):
        await self._begin()
        await fail_ticket(4001, "Forbidden: 50013 Missing Permissions")
        ticket = await get_ticket_by_interaction(4001)
        self.assertEqual(ticket.status, FAILED)
        self.assertIn("50013", ticket.last_error)

    async def test_failing_an_already_active_ticket_is_ignored(self):
        """A late error must not mark a ticket that exists as failed."""

        await self._begin()
        await attach_ticket_channel(4001, 900001)
        await activate_ticket(4001, 900002)
        await fail_ticket(4001, "late notification error")
        ticket = await get_ticket_by_interaction(4001)
        self.assertEqual(ticket.status, ACTIVE)

    async def test_stuck_tickets_are_discoverable(self):
        await self._begin(4001)
        await self._begin(4002)
        await attach_ticket_channel(4002, 900002)
        await activate_ticket(4002, 900003)
        stuck = await get_stuck_tickets(GUILD)
        self.assertEqual([item.interaction_id for item in stuck], [4001])

    async def test_the_channel_marker_is_stable_and_unique(self):
        self.assertEqual(channel_marker(4001), channel_marker(4001))
        self.assertNotEqual(channel_marker(4001), channel_marker(4002))


class ClosingFaultTests(_TicketFlowCase):
    async def _active(self, interaction_id: int = 4001, channel_id: int = 900001):
        await self._begin(interaction_id)
        await attach_ticket_channel(interaction_id, channel_id)
        await activate_ticket(interaction_id, channel_id + 1)

    async def _close(self, channel_id: int = 900001, closed_by: int = 5):
        return await begin_ticket_close(
            guild_id=GUILD,
            channel_id=channel_id,
            number="zhaloba-0001",
            author_id=77,
            ticket_type="жалоба",
            closed_by=closed_by,
        )

    async def test_the_first_close_claims_it(self):
        await self._active()
        self.assertTrue(await self._close())
        ticket = await get_ticket_by_channel(900001)
        self.assertEqual(ticket.status, CLOSING)

    async def test_a_second_click_does_not_claim_it_again(self):
        """Two people pressing at once must not both write statistics."""

        await self._active()
        self.assertTrue(await self._close())
        self.assertFalse(await self._close(closed_by=6))

    async def test_a_failure_before_the_channel_deletion_leaves_it_closing(self):
        """Recoverable: the outbox retries the deletion, state still says so."""

        await self._active()
        await self._close()
        ticket = await get_ticket_by_channel(900001)
        self.assertEqual(ticket.status, CLOSING)
        self.assertIn(ticket.interaction_id, {4001})

    async def test_completion_after_the_deletion_finalises_it(self):
        await self._active()
        await self._close()
        await complete_ticket_close(900001)
        ticket = await get_ticket_by_channel(900001)
        self.assertEqual(ticket.status, CLOSED)

    async def test_completing_twice_is_harmless(self):
        await self._active()
        await self._close()
        await complete_ticket_close(900001)
        await complete_ticket_close(900001)
        ticket = await get_ticket_by_channel(900001)
        self.assertEqual(ticket.status, CLOSED)

    async def test_closing_a_ticket_created_before_this_table_adopts_it(self):
        """The close button on an old channel must keep working."""

        self.assertTrue(await self._close(channel_id=777777))
        ticket = await get_ticket_by_channel(777777)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.status, CLOSING)

    async def test_an_already_closed_ticket_cannot_be_reclaimed(self):
        await self._active()
        await self._close()
        await complete_ticket_close(900001)
        self.assertFalse(await self._close())


class SettingsMigrationFaultTests(_TicketFlowCase):
    """A value parked by migration 32 must survive and re-home itself."""

    async def test_a_legacy_value_is_adopted_on_first_read(self):
        from utils.database import LEGACY_SETTINGS_GUILD_ID, get_setting, set_setting

        async with database._connect() as db:
            await db.execute(
                """INSERT INTO guild_settings (guild_id, key, value, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (LEGACY_SETTINGS_GUILD_ID, "weekly_stats_message_id", "111", "old"),
            )
            await db.commit()
        self.assertEqual(await get_setting("weekly_stats_message_id"), "111")
        # A later write must land on the real guild, not the parking slot.
        await set_setting("weekly_stats_message_id", "222")
        self.assertEqual(await get_setting("weekly_stats_message_id"), "222")

    async def test_a_missing_key_is_still_none(self):
        from utils.database import get_setting

        self.assertIsNone(await get_setting("never-written"))


if __name__ == "__main__":
    unittest.main()
