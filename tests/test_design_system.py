from __future__ import annotations

import ast
import unittest
from pathlib import Path

import discord

from ui.design import (
    Accent,
    LAYOUT_SEQUENCE,
    LayoutLimitError,
    LayoutSlot,
    Status,
    card,
    compose,
    divider,
    empty_state,
    heading,
    key_values,
    progress,
    section_with_action,
    section_with_thumbnail,
    stat_tile,
    status_badge,
    text_budget,
    validate_layout,
)
from ui.layouts import MAX_TEXT_DISPLAY_LENGTH, _TextBudget


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DESIGN_PATH = _REPO_ROOT / "ui" / "design.py"
_DISCORD_ICON = "https://cdn.discordapp.com/icons/1/abcdef.png"


def _text(item: discord.ui.Item) -> str:
    return getattr(item, "content", "")


def _all_text(root: discord.ui.LayoutView | discord.ui.Item) -> str:
    parts: list[str] = []
    stack: list[object] = [root]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        content = getattr(current, "content", None)
        if isinstance(content, str):
            parts.append(content)
        children = getattr(current, "children", None)
        if children:
            stack.extend(children)
        accessory = getattr(current, "accessory", None)
        if accessory is not None:
            stack.append(accessory)
    return "\n".join(parts)


class DesignSystemTests(unittest.TestCase):
    def test_accent_tokens_are_unique_and_cover_domains(self) -> None:
        expected = {
            "MEMBER",
            "STAFF",
            "TREASURY",
            "EVENTS",
            "LOGISTICS",
            "PROGRESSION",
            "SERVICE",
            "SUCCESS",
            "WARNING",
            "ERROR",
        }
        self.assertEqual({item.name for item in Accent}, expected)
        self.assertEqual(len({int(item) for item in Accent}), len(Accent))
        self.assertEqual(Accent.SUCCESS, 0x57F287)
        self.assertEqual(Accent.ERROR, 0xED4245)

    def test_text_budget_reuses_layouts_allocator(self) -> None:
        budget = text_budget()
        self.assertIsInstance(budget, _TextBudget)
        self.assertIs(type(budget), _TextBudget)
        self.assertEqual(budget.remaining, MAX_TEXT_DISPLAY_LENGTH)

    def test_heading_uses_markdown_levels(self) -> None:
        self.assertEqual(_text(heading("Состав", 2)), "## Состав")
        self.assertEqual(_text(heading("### Детали", 3)), "### Детали")
        with self.assertRaises(ValueError):
            heading("Состав", 1)

    def test_key_values_one_and_two_columns(self) -> None:
        pairs = (("Имя", "Иван"), ("Роль", "Казначей"), ("Статус", "В работе"))
        single = key_values(pairs, columns=1)
        self.assertEqual(
            _text(single),
            "**Имя:** Иван\n**Роль:** Казначей\n**Статус:** В работе",
        )
        double = key_values(pairs, columns=2)
        self.assertEqual(
            _text(double),
            "　•　".join(("**Имя:** Иван", "**Роль:** Казначей"))
            + "\n**Статус:** В работе",
        )
        with self.assertRaises(ValueError):
            key_values(pairs, columns=3)

    def test_stat_tile_includes_signed_delta(self) -> None:
        tile = stat_tile("Участники", 42, 3)
        self.assertEqual(_text(tile), "**Участники**\n42\n-# +3")
        negative = stat_tile("Казна", "12 000", -150)
        self.assertIn("-# -150", _text(negative))

    def test_progress_boundaries(self) -> None:
        empty = progress(0, 20, width=12)
        self.assertEqual(_text(empty), f"{'░' * 12} 0 / 20")
        full = progress(20, 20, width=12)
        self.assertEqual(_text(full), f"{'█' * 12} 20 / 20")
        overflow = progress(25, 20, width=12)
        self.assertEqual(_text(overflow), f"{'█' * 12} 25 / 20")
        self.assertEqual(_text(progress(5, 0, width=8)), f"{'█' * 8} 5 / 0")

    def test_status_badge_covers_standard_states(self) -> None:
        expected = {
            Status.DRAFT: "○ **Черновик**",
            Status.WAITING: "◷ **Ожидает**",
            Status.IN_PROGRESS: "◐ **В работе**",
            Status.REWORK: "✎ **На доработке**",
            Status.ACCEPTED: "✓ **Принято**",
            Status.REJECTED: "✕ **Отклонено**",
            Status.CANCELLED: "— **Отменено**",
            Status.OVERDUE: "! **Просрочено**",
        }
        for state, label in expected.items():
            with self.subTest(state=state):
                self.assertEqual(_text(status_badge(state)), label)
                self.assertEqual(_text(status_badge(state.value)), label)

    def test_divider_wraps_separator(self) -> None:
        visible = divider("large", True)
        self.assertIsInstance(visible, discord.ui.Separator)
        self.assertTrue(visible.visible)
        self.assertEqual(visible.spacing, discord.SeparatorSpacing.large)
        hidden = divider(discord.SeparatorSpacing.small, False)
        self.assertFalse(hidden.visible)

    def test_section_with_action_uses_button_accessory(self) -> None:
        button = discord.ui.Button(label="Открыть", custom_id="design:open")
        section = section_with_action("Очередь пуста", button)
        self.assertIsInstance(section, discord.ui.Section)
        self.assertIs(section.accessory, button)
        self.assertEqual(_text(section.children[0]), "Очередь пуста")

    def test_section_with_thumbnail_rejects_local_and_attachment(self) -> None:
        section = section_with_thumbnail("Семья Guinness", _DISCORD_ICON)
        self.assertIsInstance(section.accessory, discord.ui.Thumbnail)
        for forbidden in (
            "attachment://panel.png",
            "file:///tmp/icon.png",
            str(_REPO_ROOT / "local_icon.webp"),
            "https://example.com/icon.png",
        ):
            with self.subTest(url=forbidden):
                with self.assertRaises(ValueError):
                    section_with_thumbnail("Семья Guinness", forbidden)

    def test_card_and_empty_state(self) -> None:
        built = card(
            "Казна",
            "Остаток обновлён.",
            accent=Accent.TREASURY,
            footer="TREASURY.OK • ref-1",
        )
        self.assertIsInstance(built, discord.ui.Container)
        self.assertEqual(built.accent_color, Accent.TREASURY)
        text = _all_text(built)
        self.assertIn("## Казна", text)
        self.assertIn("Остаток обновлён.", text)
        self.assertIn("-# TREASURY.OK • ref-1", text)

        empty = empty_state(
            "Заявок нет",
            "Новые обращения появятся здесь.",
            "Подождите следующее обращение.",
            accent=Accent.STAFF,
        )
        empty_text = _all_text(empty)
        self.assertIn("## Заявок нет", empty_text)
        self.assertIn("-# Новые обращения появятся здесь.", empty_text)
        self.assertIn("**Что дальше:** Подождите следующее обращение.", empty_text)

    def test_compose_follows_layout_sequence(self) -> None:
        self.assertEqual(
            tuple(slot.value for slot in LAYOUT_SEQUENCE),
            (
                "heading",
                "summary",
                "stats",
                "divider",
                "details",
                "divider",
                "actions",
                "footnote",
            ),
        )
        self.assertEqual(LAYOUT_SEQUENCE[0], LayoutSlot.HEADING)
        built = compose(
            accent=Accent.EVENTS,
            heading_text="Сбор",
            summary="Выезд на капт.",
            stats=(stat_tile("Места", "8 / 20"),),
            details=(key_values((("Карта", "Порт"),)),),
            actions=(discord.ui.ActionRow(discord.ui.Button(label="Записаться")),),
            footnote="EVENTS.CREATE • ref-2",
        )
        kinds = [type(child).__name__ for child in built.children]
        self.assertEqual(
            kinds,
            [
                "TextDisplay",
                "TextDisplay",
                "TextDisplay",
                "Separator",
                "TextDisplay",
                "Separator",
                "ActionRow",
                "TextDisplay",
            ],
        )
        self.assertIn("## Сбор", _text(built.children[0]))
        self.assertIn("-# EVENTS.CREATE • ref-2", _text(built.children[-1]))

    def test_validate_layout_rejects_more_than_40_components(self) -> None:
        allowed = discord.ui.Container()
        for index in range(39):
            allowed.add_item(discord.ui.TextDisplay(f"{index}"))
        validate_layout(allowed)

        overflow = discord.ui.Container()
        for index in range(40):
            overflow.add_item(discord.ui.TextDisplay(f"{index}"))
        with self.assertRaises(LayoutLimitError) as ctx:
            validate_layout(overflow)
        self.assertIn("40", str(ctx.exception))
        self.assertIn("41", str(ctx.exception))

    def test_validate_layout_rejects_more_than_4000_characters(self) -> None:
        view = discord.ui.LayoutView()
        view.add_item(discord.ui.TextDisplay("a" * MAX_TEXT_DISPLAY_LENGTH))
        validate_layout(view)

        over = discord.ui.LayoutView()
        over.add_item(discord.ui.TextDisplay("b" * (MAX_TEXT_DISPLAY_LENGTH + 1)))
        with self.assertRaises(LayoutLimitError) as ctx:
            validate_layout(over)
        self.assertIn("4000", str(ctx.exception))
        self.assertIn("4001", str(ctx.exception))

    def test_card_stays_within_budget_on_long_body(self) -> None:
        built = card(
            "Отчёт",
            "x" * 5000,
            accent=Accent.SERVICE,
            footer="SYS.OK • ref-3",
        )
        validate_layout(built)
        length = sum(
            len(child.content)
            for child in built.children
            if isinstance(child, discord.ui.TextDisplay)
        )
        self.assertLessEqual(length, MAX_TEXT_DISPLAY_LENGTH)
        self.assertIn("-# SYS.OK • ref-3", _all_text(built))

    def test_design_module_has_no_domain_imports(self) -> None:
        tree = ast.parse(_DESIGN_PATH.read_text(encoding="utf-8"))
        forbidden = {"cogs", "services", "repositories"}
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & forbidden)
        self.assertIn("layouts", imported)
