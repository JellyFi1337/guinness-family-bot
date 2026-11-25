from __future__ import annotations

import unittest

from core.profile import build_profile
from services.help import AUDIENCE_MEMBER, AUDIENCE_STAFF, build_help_spec, help_index_spec
from tests.test_profile import _memphis_values
from ui.command_registry import cmd


def _text(spec) -> str:
    parts = [spec.title, spec.summary or ""]
    parts.extend(f"{title} {body}" for title, body in spec.details)
    return "\n".join(parts)


class HelpCopyTests(unittest.TestCase):
    def test_index_points_to_two_audiences(self) -> None:
        spec = help_index_spec()
        text = _text(spec)
        self.assertEqual(spec.code, "HELP.OVERVIEW")
        self.assertIn("Участник", text)
        self.assertIn("Старший состав", text)
        self.assertIn(cmd("me"), text)
        self.assertNotIn("VZP", text)
        self.assertNotIn("ВЗП", text)
        self.assertNotIn("Milton", text)

    def test_member_help_hides_gated_activities_when_upgrades_are_empty(self) -> None:
        profile = build_profile(_memphis_values())
        spec = build_help_spec(profile, AUDIENCE_MEMBER)
        text = _text(spec)
        self.assertEqual(spec.code, "HELP.MEMBER")
        self.assertIn("семейные контракты", text.lower())
        self.assertIn(cmd("me"), text)
        self.assertNotIn("Капт", text)
        self.assertNotIn("ВЗА", text)
        self.assertNotIn("ПСН", text)
        self.assertNotIn("Матовозка", text)
        self.assertNotIn("VZP", text)

    def test_member_help_mentions_capt_only_when_upgrade_is_open(self) -> None:
        profile = build_profile(_memphis_values(enabled_upgrades=("territory_control",)))
        spec = build_help_spec(profile, AUDIENCE_MEMBER)
        text = _text(spec)
        self.assertIn("Капт", text)
        self.assertIn("контроль территорий", text.lower())
        closed = _text(build_help_spec(build_profile(_memphis_values()), AUDIENCE_MEMBER))
        self.assertNotIn("Капт", closed)

    def test_staff_help_lists_six_operations(self) -> None:
        profile = build_profile(_memphis_values())
        spec = build_help_spec(profile, AUDIENCE_STAFF)
        text = _text(spec)
        self.assertEqual(spec.code, "HELP.STAFF")
        for label in (
            "Требует решения",
            "Состав",
            "План событий",
            "Выплаты",
            "Снабжение",
            "Развитие",
        ):
            self.assertIn(label, text)
        self.assertIn(cmd("staff panel"), text)
        self.assertIn(cmd("queue list"), text)
        self.assertNotIn("VZP", text)
