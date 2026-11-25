from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.settings import (
    SettingChange,
    SettingDescriptor,
    SettingsError,
    SettingsRegistry,
    SettingsStore,
    build_descriptors,
    get_registry,
)
from repositories.settings import get_runtime_setting, set_runtime_setting
from utils import database, discord_lookup
from utils.database import get_setting, set_setting


class MemorySettingsStore(SettingsStore):
    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], str] = {}

    async def get(self, guild_id: int, key: str) -> str | None:
        return self.rows.get((guild_id, key))

    async def list(self, guild_id: int) -> dict[str, str]:
        return {key: value for (gid, key), value in self.rows.items() if gid == guild_id}

    async def set(
        self, guild_id: int, key: str, value: str, *, updated_by: str | None
    ) -> None:
        self.rows[(guild_id, key)] = value

    async def delete(self, guild_id: int, key: str) -> None:
        self.rows.pop((guild_id, key), None)


def _descriptor(
    key: str,
    value_type: str,
    default: object,
    *,
    group: str = "economy",
    restart_required: bool = False,
) -> SettingDescriptor:
    return SettingDescriptor(
        key=key,
        group=group,
        value_type=value_type,
        default=default,
        title=key,
        description=key,
        restart_required=restart_required,
    )


class SettingsRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = MemorySettingsStore()
        self.changes: list[SettingChange] = []

        async def on_change(change: SettingChange) -> None:
            self.changes.append(change)

        self.registry = SettingsRegistry(
            (
                _descriptor("COINS_DAILY", "int", 100),
                _descriptor("CHANNEL_HELP_ID", "channel_id", None, group="discord.channels"),
                _descriptor("REQUEST_PING_ROLE_IDS", "csv_ids", (), group="access"),
                _descriptor("ENABLED_FLAG", "bool", False, group="profile"),
                _descriptor("RATE", "decimal", Decimal("1.5"), group="economy"),
                _descriptor(
                    "GUILD_ID",
                    "string",
                    "1",
                    group="access",
                    restart_required=True,
                ),
                _descriptor("TOKEN", "string", "", group="access", restart_required=True),
            ),
            store=self.store,
            toml_values={"COINS_DAILY": 50, "CHANNEL_HELP_ID": ""},
            on_change=on_change,
        )

    async def test_priority_is_database_then_toml_then_default(self) -> None:
        self.assertEqual(await self.registry.get(7, "COINS_DAILY"), 50)
        await self.registry.set(7, "COINS_DAILY", 25, updated_by="tester")
        self.assertEqual(await self.registry.get(7, "COINS_DAILY"), 25)
        other = SettingsRegistry(
            (_descriptor("COINS_DAILY", "int", 100),),
            store=MemorySettingsStore(),
            toml_values={},
        )
        self.assertEqual(await other.get(7, "COINS_DAILY"), 100)

    async def test_write_invalidates_cached_value(self) -> None:
        self.assertEqual(await self.registry.get(8, "COINS_DAILY"), 50)
        self.registry._cache[(8, "COINS_DAILY")] = "40"
        self.assertEqual(await self.registry.get(8, "COINS_DAILY"), 40)
        await self.registry.set(8, "COINS_DAILY", 12, updated_by="tester")
        self.assertEqual(await self.registry.get(8, "COINS_DAILY"), 12)
        self.assertEqual(self.store.rows[(8, "COINS_DAILY")], "12")

    async def test_type_validation_and_unknown_key(self) -> None:
        with self.assertRaisesRegex(SettingsError, "SETTINGS_INVALID_TYPE"):
            await self.registry.set(1, "COINS_DAILY", "twelve", updated_by="tester")
        with self.assertRaisesRegex(SettingsError, "SETTINGS_UNKNOWN_KEY"):
            await self.registry.get(1, "not_a_real_key")
        with self.assertRaisesRegex(SettingsError, "SETTINGS_UNKNOWN_KEY"):
            await self.registry.set(1, "not_a_real_key", "1", updated_by="tester")

    async def test_restart_required_and_secrets_cannot_be_written(self) -> None:
        with self.assertRaisesRegex(SettingsError, "SETTINGS_RESTART_REQUIRED"):
            await self.registry.set(1, "GUILD_ID", "99", updated_by="tester")
        with self.assertRaisesRegex(SettingsError, "SETTINGS_SECRET"):
            await self.registry.set(1, "TOKEN", "abc", updated_by="tester")
        self.assertEqual(self.store.rows, {})

    async def test_change_event_contains_before_and_after(self) -> None:
        await self.registry.set(3, "COINS_DAILY", 80, updated_by="cadet")
        self.assertEqual(len(self.changes), 1)
        change = self.changes[0]
        self.assertEqual(change.key, "COINS_DAILY")
        self.assertEqual(change.before, "50")
        self.assertEqual(change.after, "80")
        self.assertEqual(change.updated_by, "cadet")

    async def test_csv_ids_and_decimal_and_bool(self) -> None:
        await self.registry.set(
            4, "REQUEST_PING_ROLE_IDS", "10,20", updated_by="tester"
        )
        self.assertEqual(await self.registry.get(4, "REQUEST_PING_ROLE_IDS"), (10, 20))
        await self.registry.set(4, "RATE", "2.25", updated_by="tester")
        self.assertEqual(await self.registry.get(4, "RATE"), Decimal("2.25"))
        await self.registry.set(4, "ENABLED_FLAG", True, updated_by="tester")
        self.assertTrue(await self.registry.get(4, "ENABLED_FLAG"))


class DefaultRegistryTests(unittest.TestCase):
    def test_descriptors_are_unique_and_cover_required_groups(self) -> None:
        descriptors = build_descriptors()
        keys = [item.key for item in descriptors]
        self.assertEqual(len(keys), len(set(keys)))
        groups = {item.group for item in descriptors}
        self.assertIn("discord.channels", groups)
        self.assertIn("discord.roles", groups)
        self.assertIn("discord.categories", groups)
        self.assertIn("access", groups)
        self.assertIn("economy", groups)
        self.assertIn("cooldowns", groups)
        self.assertIn("profile", groups)
        self.assertIn("onboarding", groups)
        self.assertIn("roster", groups)
        self.assertIn("reports", groups)
        self.assertIn("queue", groups)
        self.assertIn("calendar", groups)
        self.assertIn("treasury", groups)
        self.assertIn("contribution", groups)
        self.assertIn("bonus", groups)
        self.assertIn("inventory", groups)
        self.assertIn("progression", groups)
        self.assertIn("external", groups)
        self.assertTrue(get_registry().descriptor("GUILD_ID").restart_required)
        self.assertTrue(get_registry().descriptor("member_limit").key)
        self.assertFalse(get_registry().descriptor("COINS_DAILY").restart_required)
        self.assertFalse(get_registry().descriptor("AUDIT_RETENTION_DAYS").restart_required)

    def test_import_time_constants_still_exist(self) -> None:
        from config import (
            CHANNEL_IDS,
            COINS_MESSAGE_COOLDOWN_SECONDS,
            GUILD_ID,
            SHOP_PRICES,
            channel_id,
            setting_int,
        )

        self.assertIsInstance(CHANNEL_IDS, dict)
        self.assertGreater(SHOP_PRICES["daily"], 0)
        self.assertIsNotNone(GUILD_ID)
        self.assertEqual(setting_int("COINS_DAILY"), SHOP_PRICES["daily"])
        self.assertEqual(
            setting_int("COINS_MESSAGE_COOLDOWN_SECONDS"),
            COINS_MESSAGE_COOLDOWN_SECONDS,
        )
        self.assertIsNone(channel_id("help"))


class SettingsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "settings.db",
        )
        self.path_patch.start()
        await database.init_db()
        get_registry().clear_cache()

    async def asyncTearDown(self) -> None:
        get_registry().clear_cache()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_runtime_settings_do_not_use_guild_settings(self) -> None:
        await set_runtime_setting(11, "COINS_DAILY", "333", updated_by="tester")
        self.assertEqual(await get_runtime_setting(11, "COINS_DAILY"), "333")
        self.assertIsNone(await get_setting("COINS_DAILY"))
        await set_setting("panel_marker", "1")
        self.assertEqual(await get_setting("panel_marker"), "1")
        self.assertIsNone(await get_runtime_setting(11, "panel_marker"))

    async def test_runtime_settings_override_profile_toml(self) -> None:
        from core.profile import active_profile
        from repositories.settings import set_runtime_setting as save

        await save(12, "family_name", "RuntimeFamily", updated_by="tester")
        await save(12, "direction", "legal", updated_by="tester")
        profile = await active_profile(12)
        self.assertEqual(profile.family_name, "RuntimeFamily")
        self.assertEqual(profile.direction, "legal")


class LookupAmbiguityTests(unittest.TestCase):
    def test_unique_named_lists_every_candidate_when_ambiguous(self) -> None:
        items = (
            SimpleNamespace(id=1, name="High"),
            SimpleNamespace(id=2, name="High"),
        )
        with self.assertLogs("utils.discord_lookup", level="ERROR") as captured:
            self.assertIsNone(
                discord_lookup._unique_named(
                    items, "High", kind="role:high", guild_id=5
                )
            )
        text = "\n".join(captured.output)
        self.assertIn("id=1", text)
        self.assertIn("id=2", text)


if __name__ == "__main__":
    unittest.main()
