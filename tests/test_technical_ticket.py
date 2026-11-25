import unittest
from types import SimpleNamespace

from cogs.forms import SSRequestTypeSelect, _ticket_review_roles
from cogs.technical_status import (
    TECHNICAL_NOTICES,
    OccupiedVoiceChannel,
    VaborityChannelSelect,
    VaborityView,
    build_vabority_pages,
    occupied_voice_channels,
    technical_status_embed,
)
from ui.layouts import build_layout


class TechnicalStatusAndTicketTests(unittest.TestCase):
    def test_technical_status_selector_has_all_supported_notices(self):
        self.assertEqual(
            set(TECHNICAL_NOTICES),
            {"maintenance", "restored", "problems", "investigating"},
        )
        for notice in TECHNICAL_NOTICES.values():
            embed = technical_status_embed(notice)
            self.assertEqual(embed.summary, notice.description)
            self.assertIn("Технический статус бота", embed.title)

    def test_staff_request_selector_contains_bot_request(self):
        selector = SSRequestTypeSelect()
        options = {option.value: option.label for option in selector.options}

        self.assertEqual(options["bot-request"], "Обращение по работе бота")

    def test_owner_request_only_grants_configured_owner_role(self):
        owner = SimpleNamespace(name="Owner")
        high = SimpleNamespace(name="High")
        deputy = SimpleNamespace(name="Dep. Owner")
        guild = SimpleNamespace(roles=[owner, high, deputy])

        self.assertEqual(_ticket_review_roles(guild, "owner-request"), [owner])

    def test_vabority_lists_hidden_occupied_channels_without_bots(self):
        hidden_channel = SimpleNamespace(
            id=101,
            name="Скрытый штаб",
            members=[
                SimpleNamespace(id=1, bot=False),
                SimpleNamespace(id=2, bot=False),
                SimpleNamespace(id=3, bot=True),
            ],
        )
        empty_channel = SimpleNamespace(id=102, name="Пустой", members=[])
        guild = SimpleNamespace(voice_channels=[hidden_channel, empty_channel])

        channels = occupied_voice_channels(guild)

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].channel_id, 101)
        self.assertEqual(channels[0].member_ids, (1, 2))

    def test_vabority_pages_respect_select_option_limit(self):
        guild = SimpleNamespace(
            voice_channels=[
                SimpleNamespace(
                    id=index,
                    name=f"Канал {index}",
                    members=[SimpleNamespace(id=10_000 + index, bot=False)],
                )
                for index in range(30)
            ]
        )

        pages = build_vabority_pages(occupied_voice_channels(guild))

        self.assertEqual(sum(len(page) for page in pages), 30)
        self.assertTrue(all(len(page) <= 25 for page in pages))

    def test_vabority_selector_keeps_controller_after_components_v2_conversion(self):
        controller = VaborityView(
            owner_id=777,
            pages=[
                (
                    OccupiedVoiceChannel(
                        channel_id=101,
                        name="Скрытый штаб",
                        member_ids=(777,),
                    ),
                )
            ],
        )

        layout = build_layout(content="Каналы", view=controller)
        selector = next(
            item
            for item in layout.walk_children()
            if isinstance(item, VaborityChannelSelect)
        )

        self.assertIs(selector.controller, controller)
        self.assertIsNot(selector.view, controller)


if __name__ == "__main__":
    unittest.main()
