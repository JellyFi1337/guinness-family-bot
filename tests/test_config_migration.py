from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from dotenv import dotenv_values

from scripts.migrate_env_to_toml import migrate


class ConfigMigrationTests(unittest.TestCase):
    def test_moves_public_values_and_preserves_all_secret_like_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            config_path = root / "bot_config.toml"
            env_path.write_text(
                "TOKEN=discord-placeholder\n"
                "BETA_TOKEN=beta-placeholder\n"
                "DATABASE_URL=postgresql://user:pass@localhost/db\n"
                "GUILD_ID=123\n"
                'CHANNEL_HELP="help-channel"\n',
                encoding="utf-8",
            )
            config_path.write_text(
                '[discord]\nGUILD_ID = ""\n'
                '[channels]\nCHANNEL_HELP = "default-help"\n',
                encoding="utf-8",
            )

            moved, ignored = migrate(env_path, config_path)

            with config_path.open("rb") as file:
                config = tomllib.load(file)
            secrets = dotenv_values(env_path)

        self.assertEqual(moved, 2)
        self.assertEqual(ignored, [])
        self.assertEqual(config["discord"]["GUILD_ID"], "123")
        self.assertEqual(config["channels"]["CHANNEL_HELP"], "help-channel")
        self.assertEqual(
            set(secrets),
            {"TOKEN", "BETA_TOKEN", "DATABASE_URL"},
        )


if __name__ == "__main__":
    unittest.main()
