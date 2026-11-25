import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs import showdown


class _Role:
    def __init__(
        self,
        role_id: int,
        *,
        position: int,
        name: str = "role",
        managed: bool = False,
        default: bool = False,
    ):
        self.id = role_id
        self.position = position
        self.name = name
        self.managed = managed
        self._default = default

    def is_default(self) -> bool:
        return self._default

    def __lt__(self, other: "_Role") -> bool:
        return self.position < other.position


class _Member:
    def __init__(self, member_id: int, roles: list[_Role]):
        self.id = member_id
        self.roles = roles
        self.bot = False


class _Guild:
    def __init__(self, guild_id: int):
        self.id = guild_id
        self.channels = []


class ShowdownStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "showdown.json"
        self.path_patch = patch.object(showdown, "SHOWDOWN_STATE_PATH", self.state_path)
        self.path_patch.start()
        # IsolatedAsyncioTestCase creates a fresh loop for every test.
        showdown._STATE_LOCK = asyncio.Lock()

    async def asyncTearDown(self) -> None:
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_existing_active_snapshot_is_never_replaced(self):
        first = {
            "active": True,
            "phase": "active",
            "members": {"10": [100, 101]},
            "member_overwrites": {},
        }
        replacement = {
            "active": True,
            "phase": "enabling",
            "members": {"10": [999]},
            "member_overwrites": {},
        }

        self.assertTrue(await showdown._create_guild_state(1, first))
        self.assertFalse(await showdown._create_guild_state(1, replacement))

        stored = await showdown._load_guild_state(1)
        self.assertEqual(stored["members"], {"10": [100, 101]})
        self.assertEqual(stored["phase"], "active")

    async def test_new_member_snapshot_is_appended_once_before_mutation(self):
        await showdown._create_guild_state(
            1,
            {
                "active": True,
                "phase": "active",
                "members": {},
                "member_overwrites": {},
            },
        )
        top_role = _Role(900, position=100)
        original_role = _Role(100, position=10)
        member = _Member(55, [original_role])
        guild = _Guild(1)
        bot_member = _Member(999, [top_role])
        bot_member.top_role = top_role

        self.assertTrue(
            await showdown._snapshot_member_if_missing(guild, member, bot_member)
        )
        member.roles = [_Role(200, position=20)]
        self.assertTrue(
            await showdown._snapshot_member_if_missing(guild, member, bot_member)
        )

        stored = await showdown._load_guild_state(1)
        self.assertEqual(stored["members"]["55"], [100])

    async def test_snapshot_fields_cannot_be_patched(self):
        await showdown._create_guild_state(
            1,
            {
                "active": True,
                "phase": "active",
                "members": {},
                "member_overwrites": {},
            },
        )
        with self.assertRaises(ValueError):
            await showdown._patch_guild_state(1, members={"1": []})

    async def test_legacy_snapshot_is_treated_as_active(self):
        self.state_path.write_text(
            json.dumps({"guilds": {"42": {"members": {"7": [8]}}}}),
            encoding="utf-8",
        )

        stored = await showdown._load_guild_state(42)

        self.assertTrue(stored["active"])
        self.assertEqual(stored["phase"], "active")
        self.assertEqual(stored["members"], {"7": [8]})

    async def test_delete_is_last_durable_transition(self):
        await showdown._create_guild_state(
            1,
            {
                "active": True,
                "phase": "disabling",
                "members": {"7": [8]},
                "member_overwrites": {},
            },
        )
        await showdown._delete_guild_state(1)

        self.assertIsNone(await showdown._load_guild_state(1))
        self.assertFalse(self.state_path.with_name(".showdown.json.tmp").exists())

    async def test_failed_role_restore_keeps_channel_and_snapshot(self):
        entry = {
            "active": True,
            "phase": "disabling",
            "channel_id": 123,
            "role_id": 456,
            "members": {"7": [8]},
            "member_overwrites": {},
        }
        await showdown._create_guild_state(1, entry)

        guild = _Guild(1)
        guild.me = object()
        cog = object.__new__(showdown.ShowdownCog)

        async def fail_restore(*args):
            args[-1].fail("роли участника")

        cog._restore_roles = AsyncMock(side_effect=fail_restore)
        cog._restore_member_overwrites = AsyncMock()

        result = await cog._reconcile_disabled(guild, "unit test")

        self.assertFalse(result.complete)
        self.assertIsNotNone(await showdown._load_guild_state(1))
        # Destructive lookups are intentionally never reached after a failure.
        self.assertFalse(hasattr(guild, "get_role"))

    async def test_only_configured_bot_owner_can_toggle(self):
        await showdown._create_guild_state(
            1,
            {
                "active": True,
                "phase": "active",
                "requested_by": 55,
                "members": {"55": [100]},
                "member_overwrites": {},
            },
        )
        guild = _Guild(1)
        guild.owner_id = 999
        guild.get_role = lambda role_id: None
        member = _Member(55, [])
        interaction = SimpleNamespace(
            guild=guild,
            user=member,
            permissions=SimpleNamespace(administrator=False),
        )

        with patch.object(showdown, "is_bot_owner", return_value=False):
            self.assertFalse(await showdown._can_toggle_showdown(interaction))
        with patch.object(showdown, "is_bot_owner", return_value=True):
            self.assertTrue(await showdown._can_toggle_showdown(interaction))


if __name__ == "__main__":
    unittest.main()
