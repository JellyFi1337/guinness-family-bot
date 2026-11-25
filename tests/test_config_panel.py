from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import Permission, grant_role_permission
from core.settings import (
    DatabaseSettingsStore,
    SettingDescriptor,
    SettingsRegistry,
    SettingsStore,
)
from repositories.settings import set_runtime_setting
from services.audit import list_decisions
from services.config_panel import (
    ConfigError,
    assert_config_manage,
    autocomplete_keys,
    diff_settings,
    export_settings,
    format_setting_lines,
    is_hidden_setting,
    list_settings,
    reset_setting,
    set_setting,
)
from utils import database


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


def _registry(store: SettingsStore | None = None) -> SettingsRegistry:
    return SettingsRegistry(
        (
            _descriptor("COINS_DAILY", "int", 100),
            _descriptor(
                "CHANNEL_HELP_ID",
                "channel_id",
                None,
                group="discord.channels",
            ),
            _descriptor(
                "GUILD_ID",
                "string",
                "1",
                group="access",
                restart_required=True,
            ),
            _descriptor(
                "TOKEN",
                "string",
                "",
                group="access",
                restart_required=True,
            ),
            _descriptor(
                "API_KEY",
                "string",
                "hidden",
                group="access",
                restart_required=True,
            ),
        ),
        store=store or MemorySettingsStore(),
        toml_values={"COINS_DAILY": 50, "GUILD_ID": "1"},
    )


def _member(*, user_id: int, roles: list[SimpleNamespace], guild: SimpleNamespace):
    return SimpleNamespace(
        id=user_id,
        name="tester",
        display_name="tester",
        roles=roles,
        guild=guild,
        guild_id=guild.id,
    )


class ConfigPanelServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = _registry()

    async def test_set_applies_without_restart(self) -> None:
        result = await set_setting(
            7,
            "COINS_DAILY",
            "25",
            actor_id=10,
            actor_name="tester",
            registry=self.registry,
        )
        self.assertTrue(result.changed)
        self.assertEqual(result.before, "50")
        self.assertEqual(result.after, "25")
        self.assertEqual(await self.registry.get(7, "COINS_DAILY"), 25)

    async def test_rejects_wrong_type(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            await set_setting(
                7,
                "COINS_DAILY",
                "twelve",
                actor_id=10,
                actor_name="tester",
                registry=self.registry,
            )
        self.assertEqual(ctx.exception.code, "SETTINGS_INVALID_TYPE")
        self.assertIn("Допустимый формат", str(ctx.exception))
        self.assertEqual(await self.registry.get(7, "COINS_DAILY"), 50)

    async def test_rejects_restart_required(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            await set_setting(
                7,
                "GUILD_ID",
                "99",
                actor_id=10,
                actor_name="tester",
                registry=self.registry,
            )
        self.assertEqual(ctx.exception.code, "SETTINGS_RESTART_REQUIRED")
        with self.assertRaises(ConfigError) as ctx:
            await reset_setting(
                7,
                "GUILD_ID",
                actor_id=10,
                actor_name="tester",
                registry=self.registry,
            )
        self.assertEqual(ctx.exception.code, "SETTINGS_RESTART_REQUIRED")

    async def test_rejects_missing_discord_object(self) -> None:
        guild = SimpleNamespace(get_channel=lambda _id: None, get_role=lambda _id: None)
        with self.assertRaises(ConfigError) as ctx:
            await set_setting(
                7,
                "CHANNEL_HELP_ID",
                "123",
                actor_id=10,
                actor_name="tester",
                guild=guild,
                registry=self.registry,
            )
        self.assertEqual(ctx.exception.code, "CONFIG.UNKNOWN_OBJECT")

    async def test_accepts_existing_discord_object(self) -> None:
        guild = SimpleNamespace(
            get_channel=lambda object_id: object() if object_id == 123 else None,
            get_role=lambda _id: None,
        )
        result = await set_setting(
            7,
            "CHANNEL_HELP_ID",
            "123",
            actor_id=10,
            actor_name="tester",
            guild=guild,
            registry=self.registry,
        )
        self.assertEqual(result.after, "123")
        self.assertEqual(await self.registry.get(7, "CHANNEL_HELP_ID"), 123)

    async def test_secrets_are_absent_from_show_diff_and_export(self) -> None:
        await set_setting(
            7,
            "COINS_DAILY",
            "80",
            actor_id=10,
            actor_name="tester",
            registry=self.registry,
        )
        views = await list_settings(7, registry=self.registry)
        keys = {item.key for item in views}
        self.assertIn("COINS_DAILY", keys)
        self.assertIn("GUILD_ID", keys)
        self.assertNotIn("TOKEN", keys)
        self.assertNotIn("API_KEY", keys)
        self.assertNotIn("DATABASE_URL", keys)
        joined = "\n".join(format_setting_lines(views))
        self.assertNotIn("***", joined)
        self.assertNotIn("TOKEN", joined)
        self.assertIn("изменяется только в файле, требует перезапуска", joined)
        diffs = await diff_settings(7, registry=self.registry)
        self.assertEqual([item.key for item in diffs], ["COINS_DAILY"])
        self.assertEqual(diffs[0].file_text, "50")
        self.assertEqual(diffs[0].database_text, "80")
        dumped = await export_settings(7, registry=self.registry)
        self.assertNotIn("TOKEN", dumped)
        self.assertNotIn("API_KEY", dumped)
        self.assertNotIn("DATABASE_URL", dumped)
        self.assertNotIn("***", dumped)
        self.assertIn("COINS_DAILY = 80", dumped)
        self.assertIn("[channels]", dumped)
        self.assertTrue(is_hidden_setting("TOKEN"))
        self.assertTrue(is_hidden_setting("DATABASE_URL"))

    async def test_autocomplete_skips_secrets_and_restart_required(self) -> None:
        writable = autocomplete_keys("", writable=True, registry=self.registry)
        visible = autocomplete_keys("", writable=False, registry=self.registry)
        self.assertIn("COINS_DAILY", writable)
        self.assertNotIn("GUILD_ID", writable)
        self.assertNotIn("TOKEN", writable)
        self.assertIn("GUILD_ID", visible)
        self.assertNotIn("TOKEN", visible)

    async def test_reset_without_database_row_is_unchanged(self) -> None:
        result = await reset_setting(
            7,
            "COINS_DAILY",
            actor_id=10,
            actor_name="tester",
            registry=self.registry,
        )
        self.assertFalse(result.changed)
        self.assertEqual(result.after, "50")


class ConfigPanelPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "config-panel.db",
        )
        self.path_patch.start()
        await database.init_db()
        self.registry = _registry(DatabaseSettingsStore())

    async def asyncTearDown(self) -> None:
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_change_is_written_to_the_decision_journal(self) -> None:
        await set_setting(
            3,
            "COINS_DAILY",
            "80",
            actor_id=15,
            actor_name="cadet",
            registry=self.registry,
        )
        rows = await list_decisions(3, action="SETTINGS.SET")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].subject_id, "COINS_DAILY")
        self.assertEqual(rows[0].before, "50")
        self.assertEqual(rows[0].after, "80")
        self.assertEqual(await self.registry.get(3, "COINS_DAILY"), 80)

        await reset_setting(
            3,
            "COINS_DAILY",
            actor_id=15,
            actor_name="cadet",
            registry=self.registry,
        )
        rows = await list_decisions(3, action="SETTINGS.RESET")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].before, "80")
        self.assertEqual(rows[0].after, "50")
        self.assertEqual(await self.registry.get(3, "COINS_DAILY"), 50)

    async def test_setup_wizard_key_is_not_listed(self) -> None:
        await set_runtime_setting(4, "setup.wizard", "{}", updated_by="tester")
        views = await list_settings(4, registry=self.registry)
        self.assertNotIn("setup.wizard", {item.key for item in views})
        dumped = await export_settings(4, registry=self.registry)
        self.assertNotIn("setup.wizard", dumped)
        diffs = await diff_settings(4, registry=self.registry)
        self.assertNotIn("setup.wizard", {item.key for item in diffs})

    async def test_config_manage_is_required(self) -> None:
        role = SimpleNamespace(id=10, name="Main")
        guild = SimpleNamespace(
            id=1,
            roles=[role],
            get_role=lambda role_id: role if int(role_id) == 10 else None,
        )
        member = _member(user_id=20, roles=[role], guild=guild)
        with self.assertRaises(ConfigError) as ctx:
            await assert_config_manage(member)
        self.assertEqual(ctx.exception.code, "ACCESS.MISSING_PERMISSION")
        await grant_role_permission(1, Permission.CONFIG_MANAGE, role.id)
        await assert_config_manage(member)


if __name__ == "__main__":
    unittest.main()
