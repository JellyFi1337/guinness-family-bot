from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from configuration import (
    apply_file_secrets,
    load_public_settings,
    load_secret_env,
    normalize_bot_profile,
    resolve_bot_profile,
)


class PublicSettingsTests(unittest.TestCase):
    def test_flattens_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot_config.toml"
            path.write_text(
                '[channels]\nCHANNEL_HELP = "help"\n[roles]\nOWNER_ROLE = "Owner"\n',
                encoding="utf-8",
            )

            settings = load_public_settings(path)

        self.assertEqual(settings["CHANNEL_HELP"], "help")
        self.assertEqual(settings["OWNER_ROLE"], "Owner")

    def test_rejects_duplicate_leaf_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot_config.toml"
            path.write_text(
                '[one]\nCHANNEL = "a"\n[two]\nCHANNEL = "b"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Duplicate setting"):
                load_public_settings(path)

    def test_rejects_secrets_in_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot_config.toml"
            path.write_text('[discord]\nBOT_TOKEN = "secret"\n', encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Secrets are forbidden"):
                load_public_settings(path)

    def test_rejects_identity_keys_in_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot_config.toml"
            path.write_text(
                '[discord]\nGUILD_ID = "1"\nBOT_OWNER_ID = "2"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Identity settings are forbidden"):
                load_public_settings(path)


class SecretEnvTests(unittest.TestCase):
    def test_rejects_regular_settings_in_env(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("TOKEN=\nCHANNEL_HELP=help\n", encoding="utf-8")
            if os.name == "posix":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)

            with self.assertRaisesRegex(RuntimeError, "CHANNEL_HELP"):
                load_secret_env(path)

    def test_accepts_profile_and_guild_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "BOT_PROFILE=test",
                        "TOKEN_TEST=abc",
                        "TOKEN_MAIN=def",
                        "GUILD_ID_TEST=1",
                        "GUILD_ID_MAIN=2",
                        "BOT_OWNER_ID=3",
                        "COMMAND_SYNC_FORCE=false",
                        "COMMAND_CLEAR_GLOBAL=false",
                        "DATABASE_URL=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)

            with mock.patch.dict(os.environ, {}, clear=False):
                load_secret_env(path)
                self.assertEqual(os.environ.get("BOT_PROFILE"), "test")
                self.assertEqual(os.environ.get("TOKEN_TEST"), "abc")
                self.assertEqual(os.environ.get("GUILD_ID_MAIN"), "2")
                self.assertEqual(os.environ.get("COMMAND_SYNC_FORCE"), "false")

    def test_env_overrides_existing_process_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("TOKEN_TEST=from-env\n", encoding="utf-8")
            if os.name == "posix":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)

            with mock.patch.dict(
                os.environ, {"TOKEN_TEST": "from-shell"}, clear=False
            ):
                load_secret_env(path)
                self.assertEqual(os.environ.get("TOKEN_TEST"), "from-env")

    def test_empty_env_does_not_clear_process_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("GUILD_ID_TEST=\n", encoding="utf-8")
            if os.name == "posix":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)

            with mock.patch.dict(
                os.environ, {"GUILD_ID_TEST": "999"}, clear=False
            ):
                load_secret_env(path)
                self.assertEqual(os.environ.get("GUILD_ID_TEST"), "999")

    def test_accepts_secret_like_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "TOKEN=\nDATABASE_URL=\nBETA_TOKEN=\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)

            with mock.patch.dict(os.environ, {}, clear=False):
                load_secret_env(path)

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_rejects_world_readable_env_on_posix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("TOKEN=\n", encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

            with self.assertRaisesRegex(RuntimeError, "chmod 600"):
                load_secret_env(path)


class FileSecretsTests(unittest.TestCase):
    def test_empty_token_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "discord_token"
            secret.write_text("", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"TOKEN_FILE": str(secret), "TOKEN": ""},
                clear=False,
            ):
                os.environ.pop("TOKEN", None)
                apply_file_secrets()
                self.assertFalse(os.environ.get("TOKEN", "").strip())

    def test_env_token_not_overwritten_by_file(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "discord_token"
            secret.write_text("from-file", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"TOKEN_FILE": str(secret), "TOKEN": "from-env"},
                clear=False,
            ):
                apply_file_secrets()
                self.assertEqual(os.environ["TOKEN"], "from-env")


class BotProfileTests(unittest.TestCase):
    def test_normalize_aliases(self):
        self.assertEqual(normalize_bot_profile("family"), "main")
        self.assertEqual(normalize_bot_profile("dev"), "test")
        self.assertEqual(normalize_bot_profile(None), "test")

    def test_resolve_test_profile(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_PROFILE": "test",
                "TOKEN_TEST": "tok-test",
                "TOKEN_MAIN": "tok-main",
                "GUILD_ID_TEST": "111",
                "GUILD_ID_MAIN": "222",
                "TOKEN": "",
                "GUILD_ID": "",
            },
            clear=False,
        ):
            profile = resolve_bot_profile()
            self.assertEqual(profile, "test")
            self.assertEqual(os.environ["TOKEN"], "tok-test")
            self.assertEqual(os.environ["GUILD_ID"], "111")

    def test_resolve_main_profile(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_PROFILE": "main",
                "TOKEN_TEST": "tok-test",
                "TOKEN_MAIN": "tok-main",
                "GUILD_ID_TEST": "111",
                "GUILD_ID_MAIN": "222",
                "GUILD_ID": "",
            },
            clear=False,
        ):
            profile = resolve_bot_profile()
            self.assertEqual(profile, "main")
            self.assertEqual(os.environ["TOKEN"], "tok-main")
            self.assertEqual(os.environ["GUILD_ID"], "222")

    def test_resolve_requires_token(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_PROFILE": "test",
                "TOKEN_TEST": "",
                "TOKEN_MAIN": "",
                "TOKEN": "",
                "GUILD_ID_TEST": "111",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "TOKEN_TEST"):
                resolve_bot_profile()

    def test_resolve_requires_guild(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_PROFILE": "test",
                "TOKEN_TEST": "tok-test",
                "GUILD_ID_TEST": "",
                "GUILD_ID": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "GUILD_ID_TEST"):
                resolve_bot_profile()


if __name__ == "__main__":
    unittest.main()
