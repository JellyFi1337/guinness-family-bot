import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from repositories.static_messages import (
    set_static_panels_enabled,
    static_panels_enabled,
)
from services.static_messages import StaticPayload, upsert_static_message
from utils import database


class StaticMessageGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "static-gate.db",
        )
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_panels_are_enabled_by_default_and_flag_is_durable(self):
        self.assertTrue(await static_panels_enabled(10))

        await set_static_panels_enabled(10, False)
        self.assertFalse(await static_panels_enabled(10))

        await set_static_panels_enabled(10, True)
        self.assertTrue(await static_panels_enabled(10))

    async def test_disabled_gate_stops_build_and_discord_publication(self):
        await set_static_panels_enabled(20, False)
        build = Mock(return_value=StaticPayload(content="should not publish"))

        result = await upsert_static_message(
            guild=SimpleNamespace(id=20),
            channel=SimpleNamespace(),
            panel_key="test-panel",
            build=build,
        )

        self.assertIsNone(result)
        build.assert_not_called()

    def test_payload_digest_is_stable_without_artwork(self):
        first = StaticPayload(content="панель")
        second = StaticPayload(content="панель", asset_digest=None)
        self.assertEqual(first.digest("panel:x"), second.digest("panel:x"))
        with_art = StaticPayload(content="панель", asset_digest="obsolete-key")
        self.assertNotEqual(first.digest("panel:x"), with_art.digest("panel:x"))


if __name__ == "__main__":
    unittest.main()
