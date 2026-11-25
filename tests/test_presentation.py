"""Presentation guarantees: nothing critical is dropped, nothing is hidden.

Two classes of regression are covered. The Components v2 budget must spend
itself on identifiers and outcomes before prose, and long lists must page
rather than truncate — both were silent failures that got worse as the server
grew.
"""

import unittest

from ui.layouts import (
    MAX_TEXT_DISPLAY_LENGTH,
    _is_next_step_field,
    _is_status_field,
    _TextBudget,
)
from ui.pagination import paginate_lines
from ui.design import validate_layout
from ui.templates import (
    admin_receipt,
    notice,
    public_panel,
    staff_work_item,
    system_error,
)


class _Field:
    def __init__(self, name: str, value: str = "value", inline: bool = False) -> None:
        self.name = name
        self.value = value
        self.inline = inline


class CriticalFieldTests(unittest.TestCase):
    def test_status_and_next_step_are_recognised(self):
        self.assertTrue(_is_status_field(_Field("Статус")))
        self.assertTrue(_is_status_field(_Field(" статус ")))
        self.assertTrue(_is_status_field(_Field("Status")))
        self.assertTrue(_is_next_step_field(_Field("Что дальше")))
        self.assertTrue(_is_next_step_field(_Field("Следующий шаг")))

    def test_ordinary_fields_are_not_critical(self):
        self.assertFalse(_is_status_field(_Field("Машина")))
        self.assertFalse(_is_next_step_field(_Field("Принадлежит")))


class TextBudgetTests(unittest.TestCase):
    PROSE = "Очень длинное описание ситуации. " * 400
    STATUS = "**Статус:** ✅ Одобрено • <@400000000000000001>"
    FOOTER = "-# Guinness Family • TICKET.CREATED • 1408123456789012345"

    def test_reserved_blocks_survive_an_oversized_payload(self):
        budget = _TextBudget()
        footer = budget.reserve(self.FOOTER)
        status = budget.reserve(self.STATUS)
        next_step = budget.reserve("**Что дальше:** Опишите детали в канале.")
        description = budget.take(self.PROSE)

        self.assertEqual(footer, self.FOOTER)
        self.assertEqual(status, self.STATUS)
        self.assertTrue(next_step.startswith("**Что дальше:**"))
        self.assertLess(len(description), len(self.PROSE))
        self.assertLessEqual(
            len(footer) + len(status) + len(next_step) + len(description),
            MAX_TEXT_DISPLAY_LENGTH,
        )

    def test_the_old_ordering_would_have_lost_the_status(self):
        """Regression guard for the bug this reordering fixes."""

        budget = _TextBudget()
        budget.take(self.PROSE)
        self.assertEqual(budget.take(self.STATUS), "")

    def test_truncation_is_reported_once(self):
        budget = _TextBudget()
        budget.take(self.PROSE)
        budget.take(self.PROSE)
        self.assertTrue(budget.truncated)

    def test_an_ordinary_message_is_untouched(self):
        budget = _TextBudget()
        budget.reserve("## Бронирование №0001")
        budget.reserve("-# Guinness Family • CAR.BOOKED")
        text = budget.take("Машина закреплена за вами до 21:30.")
        self.assertFalse(budget.truncated)
        self.assertTrue(text.endswith("21:30."))

    def test_worst_case_twenty_five_maximum_length_rows(self):
        rows = "\n".join(f"**{'Ы' * 100}:** {'Я' * 100}" for _ in range(25))
        budget = _TextBudget()
        footer = budget.reserve(self.FOOTER)
        status = budget.reserve(self.STATUS)
        budget.take(rows)
        self.assertEqual(footer, self.FOOTER)
        self.assertEqual(status, self.STATUS)


class PaginationTests(unittest.TestCase):
    def test_every_entry_survives_paging(self):
        lines = [f"{day:02d}.01 — <@40000000000000000{day % 10}>" for day in range(1, 121)]
        pages = paginate_lines(lines, limit=200)
        self.assertEqual("\n".join(pages).split("\n"), lines)

    def test_no_page_exceeds_the_limit_unless_one_entry_does(self):
        lines = ["a" * 50 for _ in range(50)]
        for page in paginate_lines(lines, limit=200):
            self.assertLessEqual(len(page), 200)

    def test_an_oversized_entry_gets_its_own_page(self):
        self.assertEqual(paginate_lines(["x" * 5000], limit=200), ["x" * 5000])

    def test_empty_input_yields_one_empty_page(self):
        self.assertEqual(paginate_lines([], limit=200), [""])


class TemplateTests(unittest.TestCase):
    def test_staff_work_item_puts_status_in_a_reserved_field(self):
        spec = staff_work_item(
            code="TICKET.CARD",
            title="Тикет: zhaloba-0007",
            number="zhaloba-0007",
            author="<@1>",
            category="Жалоба",
            status="🟡 Ожидает рассмотрения",
        )
        names = [name for name, _ in spec.details]
        self.assertIn("Статус", names)
        self.assertTrue(_is_status_field(_Field("Статус")))
        self.assertEqual(spec.reference, "zhaloba-0007")

    def test_staff_work_item_orders_identity_before_state(self):
        spec = staff_work_item(
            code="TICKET.CARD", title="t", number="n", author="a",
            category="c", assignee="x", status="s",
        )
        names = [name for name, _ in spec.details]
        self.assertLess(names.index("Номер"), names.index("Ответственный"))
        self.assertEqual(names[-1], "Статус")

    def test_admin_receipt_records_the_previous_value(self):
        spec = admin_receipt(
            code="TREASURY.SET",
            title="Баланс казны изменён",
            actor="<@1>",
            changed="Баланс казны",
            before="1 000$",
            after="2 000$",
        )
        rendered = dict(spec.details)
        self.assertIn("Было → стало", rendered)
        self.assertIn("1 000$", rendered["Было → стало"])
        self.assertIn("2 000$", rendered["Было → стало"])

    def test_system_error_states_whether_a_retry_is_safe(self):
        retryable = system_error(
            code="COMMAND.INTERNAL", summary="s", retryable=True, reference="1"
        )
        risky = system_error(
            code="COMMAND.INTERNAL", summary="s", retryable=False, reference="1"
        )
        self.assertIn("Повторите", retryable.next_step)
        self.assertIn("Не повторяйте", risky.next_step)
        self.assertNotEqual(retryable.next_step, risky.next_step)

    def test_every_template_carries_a_code(self):
        specs = [
            public_panel(code="PANEL.HELP", title="t", purpose="p"),
            staff_work_item(
                code="TICKET.CARD", title="t", number="n", author="a", category="c"
            ),
            admin_receipt(code="ADMIN.X", title="t", actor="a", changed="c"),
            system_error(code="SYS.X", summary="s", retryable=True, reference="1"),
        ]
        for spec in specs:
            self.assertTrue(spec.code)
            self.assertTrue(spec.title)

    def test_every_staff_card_reserves_a_status_field_last(self):
        """The layout reserves budget for "Статус"; the card must produce it."""

        cards = (
            staff_work_item(
                code="REQUEST.CARD", title="Жалоба", number="FI-0001",
                author="<@1>", category="Жалоба", status="🟡 Ожидает",
            ),
            staff_work_item(
                code="ROLE_REQUEST.CARD", title="Запрос на роль", number="RR-0002",
                author="<@2>", category="Main", status="🟡 Ожидает",
            ),
            staff_work_item(
                code="ANONYMOUS.CARD", title="Анонимное обращение", number="AN-0003",
                author="скрыт", category="Анонимное обращение", status="🟡 Ожидает",
            ),
        )
        for card in cards:
            names = [name for name, _ in card.details]
            self.assertTrue(_is_status_field(_Field(names[-1])), names)
            self.assertEqual(card.reference, card.details[0][1])

    def test_the_anonymous_card_never_carries_a_mention(self):
        card = staff_work_item(
            code="ANONYMOUS.CARD", title="Анонимное обращение", number="AN-0003",
            author="скрыт", category="Анонимное обращение", summary="текст",
            status="🟡 Ожидает",
        )
        rendered = " ".join(
            [card.title, card.summary or "", *(value for _, value in card.details)]
        )
        self.assertNotIn("<@", rendered)

    def test_titles_do_not_duplicate_the_tone_emoji(self):
        """The tone already contributes an icon; a second one reads as noise."""

        spec = system_error(code="SYS.X", summary="s", retryable=True, reference="1")
        self.assertFalse(spec.title.startswith(("❌", "✅", "⚠️", "ℹ️")))

    def test_worst_case_staff_card_stays_within_layout_limits(self):
        long_name = "А" * 100
        long_reason = "Причина: " + ("я" * 1800)
        roster = "\n".join(f"{index:02d}. <@1{index:017d}> {long_name}" for index in range(50))
        spec = staff_work_item(
            code="EVENT.ROSTER",
            title=f"Сбор • {long_name}",
            number="EV-9999",
            author=f"<@1> {long_name}",
            category="Событие на 50 человек",
            summary=f"{long_reason}\n\n{roster}",
            status="🟡 Ожидает рассмотрения",
            deadline="<t:1893456000:R>",
            history="Первое решение отменено; карточка пересобрана.",
        )
        layout = spec.to_layout()
        validate_layout(layout)

    def test_worst_case_generic_notice_stays_within_layout_limits(self):
        rows = tuple((f"{'Ы' * 40} {index}", "Я" * 200) for index in range(25))
        spec = notice(
            code="LIST.FULL",
            title="Полный состав события",
            summary="Длинное имя участника и длинная причина не должны ронять лимиты.",
            details=rows,
            next_step="Откройте следующую страницу списка, если карточка обрезана.",
            reference="EV-9999",
        )
        validate_layout(spec.to_layout())


if __name__ == "__main__":
    unittest.main()
