import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import discord

from core.bot import GuinnessBot

from cogs.dev import _is_protected
from cogs.forms import (
    AddRolesView,
    HelpRequestModal,
    RoleApprovalView,
    RoleRequestSelect,
    TicketCloseView,
    can_assign_ticket_member,
    _ticket_notification,
    _ticket_review_roles,
)
from cogs.fundraiser import _family_roles
from utils import discord_lookup
from ui.design import Accent
from utils.embeds import WHITE_COLOR, apply_accent


class UiTests(unittest.TestCase):
    def test_panel_accent_replaces_local_artwork(self):
        from ui.templates import notice

        self.assertEqual(WHITE_COLOR, 0xF2F3F5)
        spec = notice(code="PANEL.TREASURY", title="Казна", summary="Баланс казны.")
        painted = apply_accent(spec, Accent.TREASURY)
        self.assertEqual(int(painted.accent or 0), int(Accent.TREASURY))
        layout = painted.to_layout()
        rendered = " ".join(
            item.content
            for item in layout.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertNotIn("attachment://", rendered)

    def test_bot_startup_does_not_preload_local_artwork(self) -> None:
        source = inspect.getsource(GuinnessBot.setup_hook)
        self.assertNotIn("preload_embed_assets", source)
        self.assertNotIn("ASSETS_PATH", source)

    def test_help_upload_is_wrapped_in_modal_label(self):
        components = HelpRequestModal().to_components()
        self.assertEqual(components[1]["type"], 18)
        self.assertEqual(components[1]["component"]["type"], 19)
        self.assertEqual(components[1]["component"]["max_values"], 5)

    def test_ticket_claim_button_is_dynamic(self):
        open_view = TicketCloseView(1, "ticket-0001")
        assigned_view = TicketCloseView(1, "ticket-0001", 2)
        open_button = next(
            item for item in open_view.children if item.custom_id == "claim_ticket"
        )
        assigned_button = next(
            item for item in assigned_view.children if item.custom_id == "claim_ticket"
        )
        self.assertEqual(open_button.label, "Взять тикет")
        self.assertEqual(assigned_button.label, "Сняться с тикета")

    def test_role_buttons_have_separate_request_groups(self):
        rows = AddRolesView().to_components()
        self.assertEqual(
            [[component["label"] for component in row["components"]] for row in rows],
            [
                [
                    "Guest · включить / выключить",
                    "Help · включить / выключить",
                    "Contract · включить / выключить",
                ],
                [
                    "Запросить Young / Main",
                    "Запросить Friend / Super Friend",
                ],
            ],
        )
        self.assertTrue(
            all(
                component["style"] == discord.ButtonStyle.secondary.value
                for component in rows[0]["components"]
            )
        )
        self.assertTrue(
            all(
                component["style"] == discord.ButtonStyle.primary.value
                for component in rows[1]["components"]
            )
        )

    def test_role_request_includes_friend_roles_and_matching_review_choices(self):
        self.assertEqual(
            [option.value for option in RoleRequestSelect().options],
            ["Young", "Main", "Friend", "Super Friend"],
        )
        self.assertEqual(
            [option.value for option in RoleRequestSelect(("Young", "Main")).options],
            ["Young", "Main"],
        )
        self.assertEqual(
            [
                option.value
                for option in RoleRequestSelect(("Friend", "Super Friend")).options
            ],
            ["Friend", "Super Friend"],
        )
        target = SimpleNamespace(id=42)
        friend_view = RoleApprovalView(target, "Friend", "AR-0001")
        self.assertEqual(
            [
                item.label
                for item in friend_view.children
                if item.custom_id.startswith("approve_role:")
            ],
            ["Одобрить + Friend", "Одобрить + Super Friend"],
        )

    def test_family_review_contains_nickname_request(self):
        from cogs.applications import FamilyApprovalView

        view = FamilyApprovalView(
            SimpleNamespace(id=42),
            "FI-0001",
            None,
            "John Guinness",
            "Иван",
        )
        self.assertIn(
            "family:request_nickname",
            {item.custom_id for item in view.children},
        )

    def test_ticket_assignment_respects_configured_staff_hierarchy(self):
        def member(user_id, *roles):
            return SimpleNamespace(
                id=user_id,
                roles=[
                    SimpleNamespace(id=index, name=name)
                    for index, name in enumerate(roles, 1)
                ],
            )

        high = member(10, "High")
        deputy = member(11, "Dep. Owner")
        another_high = member(12, "High")
        unranked = member(13, "Senior Staff")
        with (
            patch("cogs.forms.TICKET_ASSIGNMENT_ROLE_HIERARCHY", (
                "Owner",
                "Dep. Owner",
                "High",
            )),
            patch("cogs.forms.TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS", ()),
        ):
            self.assertFalse(can_assign_ticket_member(high, deputy))
            self.assertTrue(can_assign_ticket_member(high, another_high))
            self.assertTrue(can_assign_ticket_member(high, unranked))

        spoofed_owner = SimpleNamespace(
            id=14,
            roles=[SimpleNamespace(id=999, name="Owner")],
        )
        real_high = SimpleNamespace(
            id=15,
            roles=[SimpleNamespace(id=103, name="Другое имя")],
        )
        with (
            patch(
                "cogs.forms.TICKET_ASSIGNMENT_ROLE_HIERARCHY",
                ("Owner", "Dep. Owner", "High"),
            ),
            patch(
                "cogs.forms.TICKET_ASSIGNMENT_ROLE_HIERARCHY_IDS",
                (101, 102, 103),
            ),
        ):
            self.assertTrue(can_assign_ticket_member(real_high, spoofed_owner))

    def test_afk_bedroom_is_protected_from_dev_reset(self):
        self.assertTrue(_is_protected(SimpleNamespace(name="💤・Спальня", id=1)))

    def test_developer_category_channels_are_protected_from_dev_reset(self):
        channel = SimpleNamespace(
            name="private-showdown",
            id=2,
            category=SimpleNamespace(id=3, name="⚙️・DEVELOPER・⚙️"),
        )
        self.assertTrue(_is_protected(channel))

    def test_legacy_individual_channels_are_not_protected_from_dev_reset(self):
        for name in ("мейн", "заметки", "private-showdown"):
            with self.subTest(name=name):
                self.assertFalse(
                    _is_protected(
                        SimpleNamespace(name=name, id=4, category=None)
                    )
                )

    def test_owner_request_only_grants_configured_owner_role(self):
        roles = [
            SimpleNamespace(id=1, name="Owner"),
            SimpleNamespace(id=2, name="Dep. Owner"),
            SimpleNamespace(id=3, name="High"),
        ]
        guild = SimpleNamespace(roles=roles)

        self.assertEqual(
            [role.name for role in _ticket_review_roles(guild, "owner-request")],
            ["Owner"],
        )
        self.assertEqual(
            [role.name for role in _ticket_review_roles(guild, "жалоба")],
            ["Owner", "Dep. Owner", "High"],
        )

    def test_bot_request_mentions_only_the_configured_bot_owner(self):
        with patch("cogs.forms.BOT_OWNER_ID", 77):
            content, mentions = _ticket_notification(
                SimpleNamespace(),
                "bot-request",
                [],
            )

        self.assertEqual(content, "<@77>")
        self.assertEqual(mentions.to_dict()["users"], [77])
        self.assertNotIn("roles", mentions.to_dict())

    def test_birthday_secret_mentions_all_family_roles(self):
        names = ("Owner", "Dep. Owner", "High", "Main", "Young")
        guild = SimpleNamespace(
            roles=[
                SimpleNamespace(id=index, name=name)
                for index, name in enumerate(names, 1)
            ]
        )

        self.assertEqual(
            [role.name for role in _family_roles(guild)],
            list(names),
        )

    def test_high_tools_role_id_does_not_fall_back_to_spoofed_name(self):
        member = SimpleNamespace(
            roles=[SimpleNamespace(id=999, name="Owner")]
        )
        with (
            patch.object(
                discord_lookup, "HIGH_TOOLS_ALLOWED_ROLE_IDS", (123,)
            ),
            patch.object(
                discord_lookup, "HIGH_TOOLS_ALLOWED_ROLE_NAMES", ("Owner",)
            ),
        ):
            self.assertFalse(discord_lookup.member_has_high_tools_access(member))

    def test_high_tools_category_id_is_authoritative(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(),
            channel=SimpleNamespace(
                category=SimpleNamespace(id=999, name="🔰・HIGH TOOLS ・🔰")
            ),
        )
        category_ids = dict(discord_lookup.CATEGORY_IDS)
        category_ids["high_tools_category"] = 123
        with patch.object(discord_lookup, "CATEGORY_IDS", category_ids):
            self.assertFalse(discord_lookup.is_high_tools_interaction(interaction))

    def test_missing_channel_id_falls_back_to_unique_name_with_warning(self):
        named = SimpleNamespace(id=999, name=discord_lookup.CHANNELS["vzp_invite"])
        guild = SimpleNamespace(
            id=1,
            channels=[named],
            get_channel=lambda channel_id: None,
        )
        channel_ids = dict(discord_lookup.CHANNEL_IDS)
        channel_ids["vzp_invite"] = 123
        with (
            patch.object(discord_lookup, "CHANNEL_IDS", channel_ids),
            patch("config.channel_id", return_value=123),
            self.assertLogs("utils.discord_lookup", level="WARNING") as captured,
        ):
            self.assertIs(discord_lookup.get_channel(guild, "vzp_invite"), named)
        self.assertIn("123", "\n".join(captured.output))
        self.assertIn("999", "\n".join(captured.output))

    def test_ambiguous_channel_name_returns_none_and_lists_candidates(self):
        channel_name = discord_lookup.CHANNELS["vzp_invite"]
        first = SimpleNamespace(id=11, name=channel_name)
        second = SimpleNamespace(id=22, name=channel_name)
        guild = SimpleNamespace(
            id=1,
            channels=[first, second],
            get_channel=lambda channel_id: None,
        )
        channel_ids = dict(discord_lookup.CHANNEL_IDS)
        channel_ids["vzp_invite"] = None
        with (
            patch.object(discord_lookup, "CHANNEL_IDS", channel_ids),
            patch("config.channel_id", return_value=None),
            self.assertLogs("utils.discord_lookup", level="ERROR") as captured,
        ):
            self.assertIsNone(discord_lookup.get_channel(guild, "vzp_invite"))
        text = "\n".join(captured.output)
        self.assertIn("11", text)
        self.assertIn("22", text)
