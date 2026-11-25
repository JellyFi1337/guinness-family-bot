import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.summits import SummitsCog
from repositories.summits import set_summit_thread
from services.cooldowns import CommandCooldowns, CooldownResult
from ui.templates import notice
from utils import database


class CommandCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cooldowns.db"
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_duration_is_persisted_and_applied(self):
        limiter = CommandCooldowns()
        await limiter.configure(100, "help_request", 300)

        self.assertTrue((await limiter.acquire(100, "help_request", 42)).allowed)
        blocked = await limiter.acquire(100, "help_request", 42)
        self.assertFalse(blocked.allowed)
        self.assertGreater(blocked.retry_after, 299)

        restored = CommandCooldowns()
        self.assertEqual(await restored.duration(100, "help_request"), 300)

    async def test_staff_bypass_and_disabled_cooldown(self):
        limiter = CommandCooldowns()
        await limiter.configure(100, "help_request", 300)
        for _ in range(3):
            self.assertTrue(
                (
                    await limiter.acquire(
                        100,
                        "help_request",
                        42,
                        bypass=True,
                    )
                ).allowed
            )

        await limiter.configure(100, "help_request", 0)
        for _ in range(3):
            self.assertTrue(
                (await limiter.acquire(100, "help_request", 42)).allowed
            )

    async def test_failed_operation_can_release_claim(self):
        limiter = CommandCooldowns()
        await limiter.configure(100, "summit_list", 30)
        self.assertTrue((await limiter.acquire(100, "summit_list", 42)).allowed)
        limiter.release(100, "summit_list", 42)
        self.assertTrue((await limiter.acquire(100, "summit_list", 42)).allowed)

    async def test_admin_can_grant_once_or_temporarily_and_restore(self):
        limiter = CommandCooldowns()
        await limiter.configure(100, "help_request", 300)

        self.assertTrue((await limiter.acquire(100, "help_request", 42)).allowed)
        self.assertFalse((await limiter.acquire(100, "help_request", 42)).allowed)

        limiter.grant_once(100, "help_request", 42)
        self.assertTrue((await limiter.acquire(100, "help_request", 42)).allowed)
        self.assertFalse((await limiter.acquire(100, "help_request", 42)).allowed)

        limiter.grant_for(100, "help_request", 42, 60)
        self.assertTrue((await limiter.acquire(100, "help_request", 42)).allowed)
        self.assertTrue((await limiter.acquire(100, "help_request", 42)).allowed)

        self.assertTrue(limiter.restore(100, "help_request", 42))
        self.assertTrue((await limiter.acquire(100, "help_request", 42)).allowed)
        self.assertFalse((await limiter.acquire(100, "help_request", 42)).allowed)

    async def test_summit_thread_is_saved_before_launch(self):
        list_id = await database.create_summit_list(
            "Общий сбор",
            10,
            200,
            42,
            guild_id=100,
        )
        self.assertTrue(await set_summit_thread(list_id, 900))
        summit = await database.get_summit_list(list_id)
        self.assertEqual(summit[11], 900)
        self.assertEqual(summit[13], 0)

    async def test_contract_creation_immediately_creates_discussion_thread(self):
        cog = SummitsCog(SimpleNamespace())
        thread = SimpleNamespace(id=900, mention="<#900>", delete=AsyncMock())
        message = SimpleNamespace(
            id=800,
            jump_url="https://discord.test/messages/800",
            create_thread=AsyncMock(return_value=thread),
            delete=AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild_id=100,
            guild=SimpleNamespace(id=100, roles=[]),
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock()),
        )
        set_thread = AsyncMock(return_value=True)
        followup = AsyncMock()
        callback = SummitsCog.summit_contract.callback
        with patch.dict(
            callback.__globals__,
            {
                "command_cooldowns": SimpleNamespace(
                    acquire=AsyncMock(return_value=CooldownResult(True))
                ),
                "has_administrator_access": lambda _member: False,
                "_channel": AsyncMock(return_value=SimpleNamespace(id=700)),
                "create_summit_list": AsyncMock(return_value=1),
                "get_summit_list": AsyncMock(
                    return_value=(None, None, None, None, None, None, 1)
                ),
                "contract_embed": lambda *_args: notice(
                    code="SUMMIT.CONTRACT", title="Контракт", summary="Тест"
                ),
                "send_v2": AsyncMock(return_value=message),
                "set_summit_list_message": AsyncMock(),
                "set_summit_thread": set_thread,
                "followup_v2": followup,
            },
        ):
            await callback(cog, interaction, "Тест", 10)

        message.create_thread.assert_awaited_once()
        set_thread.assert_awaited_once_with(1, 900)
        self.assertIn("Ветка для общения", followup.await_args.args[1])
