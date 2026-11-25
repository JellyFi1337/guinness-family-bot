"""Preflight and setup-report behaviour.

These cover the two failure modes that made the previous release unsafe: a
privileged intent that was never granted, and `/admin setup` reporting success
while silently skipping every panel whose channel could not be resolved.
"""

import inspect
import unittest
from types import SimpleNamespace

from cogs.interactions import SetupReport
from core.bot import GuinnessBot
from services.preflight import (
    PreflightReport,
    Severity,
    _check_intents,
    _check_role_list,
)


class IntentPreflightTests(unittest.TestCase):
    def test_missing_message_content_is_critical(self):
        report = PreflightReport()
        bot = SimpleNamespace(
            intents=SimpleNamespace(
                members=True,
                message_content=False,
                guilds=True,
                voice_states=True,
            )
        )
        _check_intents(bot, report)
        critical = report.critical()
        self.assertEqual(len(critical), 1)
        self.assertEqual(critical[0].target, "Message Content Intent")
        self.assertFalse(report.ready())

    def test_all_intents_granted_reports_no_problem(self):
        report = PreflightReport()
        bot = SimpleNamespace(
            intents=SimpleNamespace(
                members=True,
                message_content=True,
                guilds=True,
                voice_states=True,
            )
        )
        _check_intents(bot, report)
        self.assertEqual(report.critical(), [])
        self.assertEqual(report.by_area("intents"), [])

    def test_bot_requests_message_content_intent(self):
        """The Coins reward path is dead without this intent.

        Constructing GuinnessBot would build a real client, so the declaration
        is asserted at the source level — cheap, and it fails the moment
        someone drops the line while trimming intents.
        """

        source = inspect.getsource(GuinnessBot.__init__)
        self.assertIn("intents.message_content = True", source)
        self.assertIn("intents.members = True", source)


class RoleListPreflightTests(unittest.TestCase):
    def test_configured_ids_missing_from_guild_are_critical(self):
        report = PreflightReport()
        guild = SimpleNamespace(roles=[], get_role=lambda role_id: None)
        _check_role_list(
            guild, report, "POLL_ALLOWED_ROLE_IDS", "POLL_ALLOWED_ROLES", (12, 34), ()
        )
        self.assertEqual(len(report.critical()), 1)
        self.assertIn("12", report.critical()[0].message)

    def test_name_resolution_is_recorded_as_a_name_only_route(self):
        report = PreflightReport()
        owner = SimpleNamespace(id=7, name="Owner")
        guild = SimpleNamespace(roles=[owner], get_role=lambda role_id: None)
        _check_role_list(
            guild, report, "POLL_ALLOWED_ROLE_IDS", "POLL_ALLOWED_ROLES", (), ("Owner",)
        )
        self.assertEqual(report.critical(), [])
        self.assertEqual(len(report.name_only_routes), 1)
        self.assertIn("POLL_ALLOWED_ROLE_IDS", report.name_only_routes[0])
        # A route resolved only by name still blocks release.
        self.assertFalse(report.ready())

    def test_duplicate_role_name_is_reported_rather_than_guessed(self):
        report = PreflightReport()
        first = SimpleNamespace(id=1, name="High")
        second = SimpleNamespace(id=2, name="High")
        guild = SimpleNamespace(roles=[first, second], get_role=lambda role_id: None)
        _check_role_list(
            guild, report, "POLL_ALLOWED_ROLE_IDS", "POLL_ALLOWED_ROLES", (), ("High",)
        )
        self.assertEqual(len(report.critical()), 1)
        self.assertIn("дубликат", report.critical()[0].message)


class SetupReportTests(unittest.TestCase):
    def test_missing_channel_is_not_reported_as_success(self):
        report = SetupReport()
        report.record("help", "Поддержка семьи", "updated", "#help")
        report.record("shop", "Магазин", "missing", "канал #shop не найден")
        self.assertFalse(report.ok)
        self.assertEqual([item.key for item in report.missing], ["shop"])
        self.assertEqual([item.key for item in report.updated], ["help"])

    def test_merged_panel_is_not_a_problem(self):
        report = SetupReport()
        report.record("shop", "Магазин", "merged", "общая панель")
        self.assertTrue(report.ok)
        self.assertEqual(len(report.merged), 1)

    def test_failed_panel_keeps_its_error_detail(self):
        report = SetupReport()
        report.record("treasury", "Казна", "failed", "Forbidden: 50013")
        self.assertFalse(report.ok)
        self.assertIn("50013", report.failed[0].detail)


class SeverityTests(unittest.TestCase):
    def test_ready_requires_no_critical_and_no_name_only_routes(self):
        report = PreflightReport()
        self.assertTrue(report.ready())
        report.add(Severity.WARNING, "runtime", "psycopg", "не установлен")
        self.assertTrue(report.ready())
        report.name_only_routes.append("CHANNEL_SHOP_ID → #shop (1)")
        self.assertFalse(report.ready())
        self.assertEqual(report.verdict(), "not_ready")

    def test_warnings_mean_limited_not_blocked(self) -> None:
        report = PreflightReport()
        report.add(Severity.WARNING, "online", "online_source", "адаптер ещё не подключён")
        self.assertTrue(report.ready())
        self.assertEqual(report.verdict(), "limited")


if __name__ == "__main__":
    unittest.main()
