import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from ui.layouts import (
    APPROVED,
    DENIED,
    WHITE,
    ModernLayoutView,
    build_layout,
    marker_component_id,
    replace_controls,
)
from ui.messages import _status_layout, edit_message_v2, send_v2
from utils import views as safe_views


class _Destination:
    def __init__(self):
        self.args = None
        self.kwargs = None

    async def send(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return object()


class _EditableMessage:
    def __init__(self):
        self.flags = SimpleNamespace(components_v2=True)
        self.edit = AsyncMock(return_value=self)


class ComponentsV2LayoutTests(unittest.IsolatedAsyncioTestCase):
    def test_default_and_decision_accents_are_consistent(self):
        status_denied = discord.Embed(
            title="Одобрение кастомной роли",
            color=DENIED,
        )
        status_denied.add_field(name="Статус", value="🔴 Отклонено")
        cases = (
            (discord.Embed(description="Обычное уведомление"), WHITE),
            (discord.Embed(description="Цвет сохранён", color=0x123456), 0x123456),
            (discord.Embed(description="Статус: Одобрено", color=APPROVED), APPROVED),
            (discord.Embed(description="Заявка отклонена", color=DENIED), DENIED),
            (status_denied, DENIED),
        )

        for embed, expected in cases:
            with self.subTest(expected=expected):
                components = build_layout(embed=embed).to_components()
                self.assertEqual(components[0]["type"], 17)
                self.assertEqual(components[0]["accent_color"], expected)

    def test_mentions_media_and_controls_are_rendered_as_v2_components(self):
        embed = discord.Embed(title="Сбор", description="Описание")
        embed.set_image(url="attachment://event.png")
        legacy_view = discord.ui.View(timeout=None)
        button = discord.ui.Button(label="Участвовать", custom_id="summit:join")
        legacy_view.add_item(button)

        layout = build_layout(
            content="<@&123>",
            embed=embed,
            view=legacy_view,
            marker="summit:42",
        )
        components = layout.to_components()

        self.assertIsInstance(layout, ModernLayoutView)
        self.assertIs(layout.legacy_view, legacy_view)
        self.assertTrue(layout.is_persistent())
        self.assertIs(button, next(item for item in layout.walk_children() if item is button))
        self.assertEqual(len(components), 2)
        self.assertEqual(components[0]["type"], 10)
        self.assertEqual(components[0]["content"], "<@&123>")
        self.assertEqual(components[1]["type"], 17)
        nested = [
            item
            for component in components
            if component["type"] == 17
            for item in component["components"]
        ]
        self.assertTrue(any(item["type"] == 12 for item in nested))
        self.assertTrue(
            any(
                component["type"] == 10
                and "<@&123>" in component["content"]
                for component in components
            )
        )
        self.assertTrue(
            any(
                item["type"] == 1
                and item["components"][0].get("custom_id") == "summit:join"
                for item in nested
            )
        )
        self.assertTrue(
            any(
                component.get("id") == marker_component_id("summit:42")
                for component in components
            )
        )

    def test_component_limits_and_repeated_control_replacement(self):
        embed = discord.Embed(title="Панель", description="Описание")
        for index in range(10):
            embed.add_field(name=f"Поле {index}", value="Значение")
        embed.set_image(url="attachment://panel.png")
        embed.set_footer(text="Guinness Family")
        view = discord.ui.View(timeout=None)
        for index in range(5):
            view.add_item(
                discord.ui.Button(
                    label=f"Кнопка {index}",
                    custom_id=f"panel:{index}",
                    row=index,
                )
            )

        layout = build_layout(embed=embed, view=view, marker="panel:test")
        for _ in range(2):
            layout = replace_controls(layout, view)
        containers = [
            item for item in layout.children if isinstance(item, discord.ui.Container)
        ]
        self.assertTrue(containers)
        self.assertTrue(all(len(item.children) <= 10 for item in containers))
        self.assertEqual(
            sum(
                isinstance(item, discord.ui.ActionRow)
                for item in layout.walk_children()
            ),
            5,
        )
        body = containers[0]
        self.assertTrue(
            any(isinstance(item, discord.ui.TextDisplay) for item in body.children)
        )
        self.assertEqual(
            sum(isinstance(item, discord.ui.ActionRow) for item in body.children),
            5,
        )

    def test_control_replacement_prunes_empty_v2_containers(self):
        layout = build_layout(embed=discord.Embed(title="Заявка"))
        layout.add_item(discord.ui.Container(accent_color=WHITE))

        updated = replace_controls(layout, None)
        containers = [
            item
            for item in updated.children
            if isinstance(item, discord.ui.Container)
        ]

        self.assertTrue(containers)
        self.assertTrue(all(container.children for container in containers))
        self.assertTrue(
            all(component["components"] for component in updated.to_components())
        )

    def test_status_image_replacement_does_not_leave_empty_container(self):
        layout = build_layout(embed=discord.Embed(title="Заявка"))
        old_gallery = discord.ui.MediaGallery()
        old_gallery.add_item(media="attachment://old.png")
        layout.add_item(discord.ui.Container(old_gallery, accent_color=WHITE))

        with patch("ui.messages._layout_from_existing", return_value=layout):
            updated = _status_layout(
                SimpleNamespace(),
                status="✅ Одобрено",
                accent_color=APPROVED,
                view=None,
                image_filename="approved.png",
            )

        components = updated.to_components()
        self.assertTrue(all(component["components"] for component in components))
        media = [
            item
            for item in updated.walk_children()
            if isinstance(item, discord.ui.MediaGallery)
        ]
        self.assertEqual(len(media), 1)
        self.assertEqual(
            str(media[0].items[0].media.url),
            "attachment://approved.png",
        )

    def test_budget_accounts_for_formatting_suffixes(self):
        embed = discord.Embed(description="x" * 3_999)
        embed.set_footer(text="footer")
        layout = build_layout(embed=embed)
        total = sum(
            len(item.content)
            for item in layout.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertLessEqual(total, 4_000)

    def test_combined_mention_heading_and_description_fit_budget(self):
        embed = discord.Embed(title="Заголовок", description="x" * 3_995)
        layout = build_layout(content="<@123>", embed=embed)
        total = sum(
            len(item.content)
            for item in layout.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertLessEqual(total, 4_000)

    def test_multiple_attachments_share_one_gallery(self):
        embed = discord.Embed(title="Фотографии")
        layout = build_layout(
            embed=embed,
            attachment_urls=(
                "attachment://one.png",
                "attachment://two.png",
                "attachment://three.png",
            ),
        )
        gallery = next(
            item
            for item in layout.walk_children()
            if isinstance(item, discord.ui.MediaGallery)
        )
        self.assertEqual(len(gallery.items), 3)

    def test_text_displays_stay_within_discord_message_budget(self):
        layout = build_layout(content="x" * 5_000)
        text = [
            item.content
            for item in layout.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        ]

        self.assertLessEqual(sum(map(len, text)), 4_000)
        self.assertIn("Часть текста скрыта", text[-1])

    async def test_send_transport_removes_legacy_message_fields(self):
        destination = _Destination()
        embed = discord.Embed(title="Современное сообщение")

        result = await send_v2(
            destination,
            "<@123>",
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.assertIsNotNone(result)
        self.assertEqual(destination.args, ())
        self.assertNotIn("content", destination.kwargs)
        self.assertNotIn("embed", destination.kwargs)
        self.assertNotIn("embeds", destination.kwargs)
        self.assertIsInstance(destination.kwargs["view"], ModernLayoutView)
        rendered = destination.kwargs["view"].to_components()
        self.assertEqual(rendered[0]["type"], 10)
        self.assertEqual(rendered[0]["content"], "<@123>")
        card = rendered[1]
        self.assertEqual(card["type"], 17)
        self.assertIsInstance(
            destination.kwargs["allowed_mentions"], discord.AllowedMentions
        )

    async def test_body_edit_preserves_hidden_reconciliation_marker(self):
        original = build_layout(
            embed=discord.Embed(title="До"), marker="poll:42"
        )
        message = _EditableMessage()

        with patch("ui.messages._layout_from_existing", return_value=original):
            await edit_message_v2(
                message,
                embed=discord.Embed(title="После"),
                view=discord.ui.View(timeout=None),
            )

        layout = message.edit.await_args.kwargs["view"]
        self.assertTrue(
            any(
                isinstance(item, discord.ui.Container)
                and item.id == marker_component_id("poll:42")
                for item in layout.children
            )
        )

    async def test_modal_cooldown_is_stable_across_instances(self):
        class UploadModal(safe_views.SafeModal, title="Upload"):
            cooldown_seconds = 15.0

        safe_views._COMPONENT_HITS.clear()
        interaction = SimpleNamespace(
            guild_id=1,
            user=SimpleNamespace(id=2),
            client=SimpleNamespace(),
        )
        first = UploadModal()
        second = UploadModal()
        self.assertNotEqual(first.custom_id, second.custom_id)
        with patch.object(safe_views, "respond_v2", AsyncMock()) as response:
            self.assertTrue(await first.interaction_check(interaction))
            self.assertFalse(await second.interaction_check(interaction))
        response.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
