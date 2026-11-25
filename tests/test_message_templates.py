from __future__ import annotations

import unittest
from types import SimpleNamespace

import discord

from ui.design import LayoutLimitError, Status, validate_layout
from ui.layouts import MAX_TEXT_DISPLAY_LENGTH, ModernLayoutView
from ui.message_specs import MessageKind, resolve_status
from ui.messages import followup_spec, respond_spec
from ui.templates import (
    UserResult,
    admin_receipt,
    completed,
    failed,
    public_panel,
    staff_work_item,
    system_error,
)


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
    return "\n".join(reversed(parts))


def _child_types(container: discord.ui.Container) -> list[str]:
    return [type(child).__name__ for child in container.children]


def _container(layout: discord.ui.LayoutView) -> discord.ui.Container:
    item = next(child for child in layout.children if isinstance(child, discord.ui.Container))
    return item


class _Response:
    def __init__(self) -> None:
        self.kwargs: dict | None = None
        self._done = False
        self.type = None

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, **kwargs):
        self.kwargs = kwargs
        self._done = True
        return object()


class _Followup:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def send(self, **kwargs):
        self.kwargs = kwargs
        return object()


class _Interaction:
    def __init__(self) -> None:
        self.response = _Response()
        self.followup = _Followup()
        self.extras: dict = {}
        self.command = None
        self.id = 1


class MessageTemplateTests(unittest.IsolatedAsyncioTestCase):
    def test_public_panel_structure_and_empty_optional_fields(self) -> None:
        full = public_panel(
            code="PANEL.HELP",
            title="Помощь",
            purpose="Канал для обращений к старшему составу.",
            rules=(("Кто", "Участник семьи"), ("Когда", "В любое время")),
            action="Нажмите кнопку ниже.",
        )
        self.assertEqual(full.kind, MessageKind.PUBLIC_PANEL)
        layout = full.to_layout()
        self.assertIsInstance(layout, ModernLayoutView)
        validate_layout(layout)
        text = _all_text(layout)
        self.assertIn("## Помощь", text)
        self.assertIn("Канал для обращений к старшему составу.", text)
        self.assertIn("**Кто:** Участник семьи", text)
        self.assertIn("**Когда:** В любое время", text)
        self.assertIn("**Что дальше:** Нажмите кнопку ниже.", text)
        self.assertIn("PANEL.HELP", text)
        self.assertNotIn("<t:", text)
        self.assertNotIn("<@", text)

        empty = public_panel(code="PANEL.HELP", title="Помощь", purpose="Канал для обращений.")
        empty_text = _all_text(empty.to_layout())
        self.assertIn("## Помощь", empty_text)
        self.assertNotIn("**Что дальше:**", empty_text)
        self.assertNotIn("**Кто:**", empty_text)
        self.assertEqual(_child_types(_container(empty.to_layout())), ["TextDisplay", "TextDisplay", "TextDisplay"])

        first = full.to_layout().to_components()
        second = full.to_layout().to_components()
        self.assertEqual(first, second)

    def test_staff_work_item_header_columns_and_collapsed_history(self) -> None:
        spec = staff_work_item(
            code="TICKET.CARD",
            title="Тикет",
            number="zhaloba-0007",
            author="<@1>",
            category="Жалоба",
            summary="Суть обращения.",
            assignee="<@2>",
            status="🟡 Ожидает рассмотрения",
            deadline="сегодня 21:00",
            history="принято в работу вчера",
        )
        self.assertEqual(resolve_status(spec.status or ""), Status.WAITING)
        layout = spec.to_layout()
        validate_layout(layout)
        text = _all_text(layout)
        self.assertIn("## Тикет", text)
        self.assertIn("### zhaloba-0007", text)
        self.assertIn("**Ожидает**", text)
        self.assertIn("　•　".join(("**Заявитель:** <@1>", "**Категория:** Жалоба")), text)
        self.assertIn("Суть обращения.", text)
        self.assertIn("**Срок:** сегодня 21:00", text)
        self.assertIn("-# История: принято в работу вчера", text)
        self.assertIn("**Ответственный:** <@2>", text)
        kinds = _child_types(_container(layout))
        self.assertEqual(kinds[0], "TextDisplay")
        self.assertIn("Separator", kinds)

        bare = staff_work_item(
            code="TICKET.CARD",
            title="Тикет",
            number="n-1",
            author="скрыт",
            category="Жалоба",
        )
        bare_text = _all_text(bare.to_layout())
        self.assertIn("### n-1", bare_text)
        self.assertNotIn("**Ожидает**", bare_text)
        self.assertNotIn("**Срок:**", bare_text)
        self.assertNotIn("История:", bare_text)
        self.assertNotIn("**Ответственный:**", bare_text)
        self.assertNotIn("**Решение:**", bare_text)

    def test_admin_receipt_change_section_and_audit_link(self) -> None:
        spec = admin_receipt(
            code="TREASURY.SET",
            title="Баланс казны изменён",
            actor="<@1>",
            changed="Баланс казны",
            before="1 000$",
            after="2 000$",
            reason="корректировка",
            reference="42",
        )
        text = _all_text(spec.to_layout())
        self.assertIn("## Баланс казны изменён", text)
        self.assertIn("### Было → стало", text)
        self.assertIn("**Было:** 1 000$", text)
        self.assertIn("**Стало:** 2 000$", text)
        self.assertIn("**Кем:** <@1>", text)
        self.assertIn("**Основание:** корректировка", text)
        self.assertIn("/audit show", text)
        self.assertIn("`42`", text)
        validate_layout(spec.to_layout())

        bare = admin_receipt(
            code="ADMIN.X",
            title="Настройка",
            actor="<@1>",
            changed="Ключ",
        )
        bare_text = _all_text(bare.to_layout())
        self.assertNotIn("Было → стало", bare_text)
        self.assertNotIn("Основание", bare_text)
        self.assertNotIn("/audit show", bare_text)

    def test_system_error_states_retry_without_exception_details(self) -> None:
        retryable = system_error(
            code="COMMAND.INTERNAL",
            summary="Не удалось сохранить изменение.",
            retryable=True,
            reference="ref-1",
        )
        risky = system_error(
            code="COMMAND.INTERNAL",
            summary="Действие уже применено.",
            retryable=False,
            reference="ref-2",
        )
        retry_text = _all_text(retryable.to_layout())
        risky_text = _all_text(risky.to_layout())
        self.assertIn("Повторять безопасно", retry_text)
        self.assertIn("COMMAND.INTERNAL", retry_text)
        self.assertIn("ref-1", retry_text)
        self.assertNotIn("Traceback", retry_text)
        self.assertNotIn("Exception", retry_text)
        self.assertIn("Не повторяйте", risky_text)
        self.assertIn("ref-2", risky_text)
        self.assertNotEqual(retry_text, risky_text)
        validate_layout(retryable.to_layout())

    def test_user_result_is_a_short_card_with_one_action(self) -> None:
        spec = completed(
            code="CAR.BOOKED",
            title="Машина закреплена",
            summary="Бронь действует до 21:30.",
        )
        self.assertIsInstance(spec, UserResult)
        self.assertEqual(spec.kind, MessageKind.USER_RESULT)
        text = _all_text(spec.to_layout())
        self.assertIn("## Машина закреплена", text)
        self.assertIn("Бронь действует до 21:30.", text)
        self.assertEqual(text.count("**Что дальше:**"), 1)
        self.assertNotIn("**Изменено:**", text)
        validate_layout(spec.to_layout())

        failed_spec = failed(
            code="CAR.FAILED",
            title="Бронь не создана",
            summary="Слот занят.",
            details=(("Машина", "Patriot"),),
        )
        failed_text = _all_text(failed_spec.to_layout())
        self.assertIn("**Машина:** Patriot", failed_text)
        self.assertEqual(failed_text.count("**Что дальше:**"), 1)

    def test_templates_respect_component_limits(self) -> None:
        spec = public_panel(
            code="PANEL.LONG",
            title="Панель",
            purpose="x" * 5000,
            rules=tuple((f"Правило {index}", "значение") for index in range(8)),
            action="Нажмите кнопку.",
        )
        layout = spec.to_layout()
        validate_layout(layout)
        length = sum(
            len(item.content)
            for item in layout.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertLessEqual(length, MAX_TEXT_DISPLAY_LENGTH)
        with self.assertRaises(LayoutLimitError):
            validate_layout(discord.ui.TextDisplay("y" * (MAX_TEXT_DISPLAY_LENGTH + 1)))

    def test_to_embed_is_gone(self) -> None:
        spec = system_error(
            code="SYS.X",
            summary="s",
            retryable=True,
            reference="1",
        )
        self.assertFalse(hasattr(spec, "to_embed"))
        layout = spec.to_layout()
        self.assertTrue(any(
            isinstance(item, discord.ui.TextDisplay) and "SYS.X" in item.content
            for item in layout.walk_children()
        ))

    async def test_respond_and_followup_spec_use_to_layout(self) -> None:
        spec = completed(
            code="TEST.OK",
            title="Готово",
            summary="Изменение сохранено.",
        )
        interaction = _Interaction()
        await respond_spec(interaction, spec, ephemeral=True)
        payload = interaction.response.kwargs or {}
        self.assertNotIn("embed", payload)
        self.assertNotIn("embeds", payload)
        self.assertIsInstance(payload.get("view"), ModernLayoutView)
        self.assertTrue(payload.get("ephemeral"))

        deferred = _Interaction()
        deferred.response._done = True
        deferred.response.type = SimpleNamespace()
        await followup_spec(deferred, spec, ephemeral=True)
        follow_payload = deferred.followup.kwargs or {}
        self.assertNotIn("embed", follow_payload)
        self.assertIsInstance(follow_payload.get("view"), ModernLayoutView)
