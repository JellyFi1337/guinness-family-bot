from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord

from repositories.static_messages import get_static_message, save_static_message
from services.panels import (
    SOURCE_UNAVAILABLE,
    STATE_MESSAGE_DELETED,
    STATE_UNAVAILABLE,
    PanelError,
    deploy_panel,
    format_panel_lines,
    list_panels,
    register_deployer,
    repair_panels,
    unavailable_reason,
    unregister_deployers,
)
from services.preflight import PreflightReport, Severity
from services.static_messages import inspect_static_binding
from utils import database


def _not_found() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(response, {"code": 10008, "message": "Unknown Message"})


def _channel(channel_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=channel_id)


class PanelAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "panels.db",
        )
        self.path_patch.start()
        await database.init_db()
        unregister_deployers()

    async def asyncTearDown(self) -> None:
        unregister_deployers()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_detects_deleted_message(self) -> None:
        await save_static_message(1, "setup-help", 10, 99, revision=3)

        async def fetch(_channel_id: int, _message_id: int):
            raise _not_found()

        inspection = await inspect_static_binding(
            guild_id=1,
            panel_key="setup-help",
            expected_channel_id=10,
            fetch_message=fetch,
        )
        self.assertEqual(inspection.state, STATE_MESSAGE_DELETED)

        items = await list_panels(
            1,
            resolve_channel=lambda key: _channel(10) if key == "help" else _channel(11),
            fetch_message=fetch,
        )
        help_item = next(item for item in items if item.key == "help")
        self.assertEqual(help_item.state, STATE_MESSAGE_DELETED)
        self.assertIn("удалено", help_item.reason)

    async def test_repair_restores_lost_binding(self) -> None:
        await save_static_message(1, "setup-help", 10, 99, revision=1)
        deployed: list[str] = []

        async def fetch(_channel_id: int, _message_id: int):
            raise _not_found()

        async def deploy_help(_guild: object) -> None:
            deployed.append("help")
            await save_static_message(1, "setup-help", 10, 555, revision=2)

        register_deployer("help", deploy_help)
        results = await repair_panels(
            SimpleNamespace(id=1),
            resolve_channel=lambda key: _channel(10) if key == "help" else None,
            fetch_message=fetch,
        )
        help_result = next(item for item in results if item.key == "help")
        self.assertTrue(help_result.changed)
        self.assertEqual(deployed, ["help"])
        stored = await get_static_message(1, "setup-help")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.message_id, 555)

    async def test_refuses_capt_without_territory_control(self) -> None:
        reason = await unavailable_reason(1, "capt")
        self.assertIn("Капт", reason)
        with self.assertRaises(PanelError) as ctx:
            await deploy_panel(SimpleNamespace(id=1), "capt")
        self.assertEqual(ctx.exception.code, "PANELS.UPGRADE_REQUIRED")
        self.assertIsNone(await get_static_message(1, "capt"))

    async def test_refuses_insights_without_api_source(self) -> None:
        reason = await unavailable_reason(1, "insights-rating")
        self.assertEqual(reason, SOURCE_UNAVAILABLE)
        items = await list_panels(1)
        rating = next(item for item in items if item.key == "insights-rating")
        self.assertEqual(rating.state, STATE_UNAVAILABLE)
        line = format_panel_lines((rating,))[0]
        self.assertIn("недоступна: источник не настроен", line)
        with self.assertRaises(PanelError) as ctx:
            await deploy_panel(SimpleNamespace(id=1), "insights-rating")
        self.assertEqual(ctx.exception.code, "PANELS.SOURCE_REQUIRED")
        self.assertIsNone(await get_static_message(1, "insights-rating"))

    async def test_redeploy_one_panel_does_not_touch_neighbors(self) -> None:
        await save_static_message(
            1, "setup-help", 10, 100, revision=3, payload_digest="help"
        )
        await save_static_message(
            1, "treasury", 20, 200, revision=4, payload_digest="keep"
        )
        touched: list[str] = []

        async def deploy_help(_guild: object) -> None:
            touched.append("help")
            await save_static_message(
                1, "setup-help", 10, 101, revision=3, payload_digest="help-new"
            )

        register_deployer("help", deploy_help)
        await deploy_panel(SimpleNamespace(id=1), "help")
        self.assertEqual(touched, ["help"])
        help_row = await get_static_message(1, "setup-help")
        treasury = await get_static_message(1, "treasury")
        self.assertEqual(help_row.message_id, 101)
        self.assertEqual(treasury.message_id, 200)
        self.assertEqual(treasury.revision, 4)
        self.assertEqual(treasury.payload_digest, "keep")


class PreflightVerdictTests(unittest.TestCase):
    def test_three_tier_verdict(self) -> None:
        report = PreflightReport()
        self.assertEqual(report.verdict(), "ready")
        report.add(Severity.WARNING, "runtime", "psycopg", "не установлен")
        self.assertTrue(report.ready())
        self.assertEqual(report.verdict(), "limited")
        report.add(Severity.CRITICAL, "intents", "members", "выключен")
        self.assertEqual(report.verdict(), "not_ready")


if __name__ == "__main__":
    unittest.main()
