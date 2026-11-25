from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import profile_toml_values
from core.bot import GuinnessBot
from core.profile import (
    DEFAULT_TIMEZONE,
    KNOWN_UPGRADES,
    PROFILE_REGISTRY,
    ProfileConfigError,
    ServerProfile,
    active_profile,
    build_profile,
    format_profile_log,
    profile_requires,
)
from repositories.static_messages import set_guild_setting
from utils import database


def _memphis_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "server_key": "memphis",
        "family_name": "Guinness",
        "family_tag": "",
        "direction": "unset",
        "timezone": "Europe/Moscow",
        "season_key": "season-2",
        "season_start": "2026-07-28",
        "season_end": "2026-10-27",
        "online_source": "none",
        "enabled_upgrades": (),
        "member_limit": None,
        "allow_archived": False,
        "external_server_id": "",
    }
    values.update(overrides)
    return values


class ProfileBuildTests(unittest.TestCase):
    def test_loads_profile_from_tracked_toml(self):
        profile = build_profile(profile_toml_values())

        self.assertEqual(profile.project, "majestic")
        self.assertEqual(profile.server_key, "memphis")
        self.assertEqual(profile.server_label, "Majestic RP Memphis")
        self.assertEqual(profile.external_server_id, "RU19")
        self.assertEqual(profile.family_name, "Guinness")
        self.assertEqual(profile.family_tag, "")
        self.assertEqual(profile.direction, "unset")
        self.assertEqual(profile.timezone, DEFAULT_TIMEZONE)
        self.assertEqual(profile.season_key, "season-2")
        self.assertEqual(profile.season_start, date(2026, 7, 28))
        self.assertEqual(profile.season_end, date(2026, 10, 27))
        self.assertEqual(profile.online_source, "none")
        self.assertEqual(profile.enabled_upgrades, frozenset())
        self.assertIsNone(profile.member_limit)
        self.assertFalse(profile.archived)

    def test_season_dates_come_from_settings_not_code_constants(self):
        profile = build_profile(
            _memphis_values(
                season_key="custom-season",
                season_start="2027-01-15",
                season_end="2027-04-15",
            )
        )

        self.assertEqual(profile.season_key, "custom-season")
        self.assertEqual(profile.season_start, date(2027, 1, 15))
        self.assertEqual(profile.season_end, date(2027, 4, 15))

    def test_unknown_upgrade_is_a_configuration_error(self):
        with self.assertRaisesRegex(ProfileConfigError, r"(?i)неизвестн"):
            build_profile(_memphis_values(enabled_upgrades="not_a_real_upgrade"))

    def test_profile_requires_enabled_and_disabled_upgrades(self):
        profile = build_profile(
            _memphis_values(enabled_upgrades="territory_control, psn")
        )

        self.assertTrue(profile_requires(profile, "territory_control"))
        self.assertTrue(profile.requires("psn"))
        self.assertFalse(profile_requires(profile, "air_cargo"))
        with self.assertRaisesRegex(ProfileConfigError, r"(?i)неизвестн"):
            profile_requires(profile, "missing_upgrade")

    def test_archived_milton_cannot_become_active_without_explicit_setting(self):
        with self.assertRaisesRegex(ProfileConfigError, "архивн"):
            build_profile(_memphis_values(server_key="milton"))

        archived = build_profile(
            _memphis_values(server_key="milton", allow_archived=True)
        )
        self.assertTrue(archived.archived)
        self.assertEqual(archived.project, "gta5rp")
        self.assertEqual(archived.server_key, "milton")
        self.assertEqual(archived.server_label, "GTA 5 RP Milton")

    def test_member_limit_stays_unset_until_configured(self):
        self.assertIsNone(build_profile(_memphis_values()).member_limit)
        self.assertEqual(
            build_profile(_memphis_values(member_limit="80")).member_limit,
            80,
        )
        with self.assertRaisesRegex(ProfileConfigError, r"(?i)лимит состава"):
            build_profile(_memphis_values(member_limit="0"))

    def test_unknown_server_key_is_rejected(self):
        with self.assertRaisesRegex(ProfileConfigError, "ключ сервера"):
            build_profile(_memphis_values(server_key="eclipse"))

    def test_registry_contains_exactly_memphis_and_milton(self):
        self.assertEqual(set(PROFILE_REGISTRY), {"memphis", "milton"})
        self.assertFalse(PROFILE_REGISTRY["memphis"].archived)
        self.assertTrue(PROFILE_REGISTRY["milton"].archived)
        self.assertTrue(KNOWN_UPGRADES)

    def test_format_profile_log_includes_required_fields(self):
        profile = build_profile(
            _memphis_values(enabled_upgrades=("territory_control",))
        )
        line = format_profile_log(profile)

        self.assertIn("project=majestic", line)
        self.assertIn("server=memphis", line)
        self.assertIn("family=Guinness", line)
        self.assertIn("direction=unset", line)
        self.assertIn("season=season-2", line)
        self.assertIn("upgrades=1", line)


class ProfileOverlayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "profile.db",
        )
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_guild_settings_override_toml(self):
        guild_id = 9_001
        await set_guild_setting(guild_id, "profile.family_name", "Override")
        await set_guild_setting(guild_id, "profile.direction", "criminal")
        await set_guild_setting(
            guild_id, "profile.enabled_upgrades", "territory_control,air_cargo"
        )
        await set_guild_setting(guild_id, "profile.member_limit", "90")
        await set_guild_setting(guild_id, "profile.external_server_id", "RU19")

        profile = await active_profile(guild_id)

        self.assertEqual(profile.family_name, "Override")
        self.assertEqual(profile.direction, "criminal")
        self.assertEqual(
            profile.enabled_upgrades,
            frozenset({"territory_control", "air_cargo"}),
        )
        self.assertEqual(profile.member_limit, 90)
        self.assertEqual(profile.server_key, "memphis")
        self.assertTrue(profile_requires(profile, "air_cargo"))

    async def test_overlay_cannot_activate_archived_profile(self):
        guild_id = 9_002
        await set_guild_setting(guild_id, "profile.server_key", "milton")

        with self.assertRaisesRegex(ProfileConfigError, "архивн"):
            await active_profile(guild_id)

    async def test_overlay_unknown_upgrade_is_rejected(self):
        guild_id = 9_003
        await set_guild_setting(guild_id, "profile.enabled_upgrades", "made_up")

        with self.assertRaisesRegex(ProfileConfigError, r"(?i)неизвестн"):
            await active_profile(guild_id)

    async def test_empty_overlay_uses_toml_profile(self):
        profile = await active_profile(9_004)
        toml_profile = build_profile(profile_toml_values())
        self.assertEqual(profile, toml_profile)


class ProfileStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_logs_active_profile_line(self):
        profile = build_profile(_memphis_values())
        bot = SimpleNamespace()

        with (
            patch("core.bot.active_profile", AsyncMock(return_value=profile)),
            patch("core.bot.GUILD_ID", 1_525_003_649_653_276_813),
            self.assertLogs("core.bot", level="INFO") as captured,
        ):
            await GuinnessBot._log_active_profile(bot)

        text = "\n".join(captured.output)
        self.assertIn("Active profile", text)
        self.assertIn("project=majestic", text)
        self.assertIn("server=memphis", text)
        self.assertIn("family=Guinness", text)
        self.assertIn("direction=unset", text)
        self.assertIn("season=season-2", text)
        self.assertIn("upgrades=0", text)

    def test_server_profile_is_frozen(self):
        profile = build_profile(_memphis_values())
        with self.assertRaises(AttributeError):
            profile.family_name = "Other"  # type: ignore[misc]
        self.assertIsInstance(profile, ServerProfile)


if __name__ == "__main__":
    unittest.main()
