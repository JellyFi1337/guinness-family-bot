from __future__ import annotations

import asyncio
import inspect
import unittest
from types import SimpleNamespace

import discord

from core.bot import GuinnessBot
from ui.custom_ids import (
    CURRENT_VERSION,
    DISCORD_CUSTOM_ID_LIMIT,
    CustomIdError,
    StaleCustomId,
    build_custom_id,
    parse_custom_id,
)
from ui.design import validate_layout
from ui.navigation import (
    ConfirmDialog,
    FilterBar,
    FilterSpec,
    PageState,
    Paginator,
    apply_filter_to_state,
    Tab,
    TabbedPanel,
    apply_page_action,
    dispatch_dynamic_click,
    register_page_source,
    register_tab_source,
    unregister_sources,
    _debug_source_domains,
)
from ui.pagination import EphemeralPager, format_page_caption


def _all_text(root: object) -> str:
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
        kwargs = current if isinstance(current, dict) else getattr(current, "kwargs", None)
        if isinstance(kwargs, dict):
            nested = kwargs.get("view")
            if nested is not None:
                stack.append(nested)
    return "\n".join(parts)


def _walk_items(root: object) -> list[object]:
    found: list[object] = []
    stack: list[object] = [root]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        children = getattr(current, "children", None)
        if children:
            stack.extend(children)
        accessory = getattr(current, "accessory", None)
        if accessory is not None:
            stack.append(accessory)
    return found


def _custom_ids(root: object) -> list[str]:
    ids: list[str] = []
    for item in _walk_items(root):
        raw = getattr(item, "custom_id", None)
        if raw:
            ids.append(str(raw))
    return ids


def _button_labels(root: object) -> list[str]:
    return [
        str(item.label)
        for item in _walk_items(root)
        if isinstance(item, discord.ui.Button) and item.label
    ]


def _select_options(root: object) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for item in _walk_items(root):
        if isinstance(item, discord.ui.Select):
            options.extend(item.options)
    return options


class _Response:
    def __init__(self) -> None:
        self.kwargs: dict | None = None
        self.edit_kwargs: dict | None = None
        self._done = False
        self.type = None

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, **kwargs):
        self.kwargs = kwargs
        self._done = True
        return object()

    async def edit_message(self, **kwargs):
        self.edit_kwargs = kwargs
        self._done = True
        return object()


class _Followup:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def send(self, **kwargs):
        self.kwargs = kwargs
        return object()


class _Interaction:
    def __init__(
        self,
        *,
        user_id: int = 1,
        custom_id: str = "",
        values: list[str] | None = None,
    ) -> None:
        self.response = _Response()
        self.followup = _Followup()
        self.extras: dict = {}
        self.command = None
        self.id = 11
        self.user = SimpleNamespace(id=user_id, name="tester")
        self.guild_id = 15
        self.channel_id = 22
        self.channel = SimpleNamespace(id=22, name="lists")
        self.data = {"custom_id": custom_id, "values": values or []}
        self.values = values or []
        self.message = None


def _lines(count: int = 5) -> list[str]:
    return [f"строка-{index}" for index in range(count)]


def _pager(**overrides: object) -> Paginator:
    values: dict[str, object] = {
        "domain": "roster",
        "object_id": "list",
        "lines": _lines(),
        "state": PageState(owner_id=10),
        "title": "Состав",
        "page_limit": 10,
    }
    values.update(overrides)
    return Paginator(**values)  # type: ignore[arg-type]


class CustomIdTests(unittest.TestCase):
    def test_roundtrip_and_owner_page_params(self) -> None:
        raw = build_custom_id(
            domain="roster",
            action="next",
            object_id="list",
            params={"p": 2, "f": "active", "o": 10},
        )
        parsed = parse_custom_id(raw)
        self.assertEqual(parsed.version, CURRENT_VERSION)
        self.assertEqual(parsed.page(), 2)
        self.assertEqual(parsed.param("f"), "active")
        self.assertEqual(parsed.owner_id(), 10)
        self.assertLessEqual(len(raw), DISCORD_CUSTOM_ID_LIMIT)

    def test_unknown_version_is_stale(self) -> None:
        raw = "g:9:roster:next:list:p=1"
        with self.assertRaises(StaleCustomId):
            parse_custom_id(raw)
        parsed = parse_custom_id(raw, allow_stale=True)
        self.assertEqual(parsed.version, 9)

    def test_rejects_overlong_id(self) -> None:
        with self.assertRaises(CustomIdError):
            build_custom_id(
                domain="roster",
                action="refresh",
                object_id="very-long-object-identifier",
                params={"f": "x" * 40, "s": "y" * 40, "p": 99},
            )


class PageActionTests(unittest.TestCase):
    def test_circular_walk_and_nonwrapping_ends(self) -> None:
        self.assertEqual(apply_page_action("next", 1, 4), 2)
        self.assertEqual(apply_page_action("next", 4, 4), 1)
        self.assertEqual(apply_page_action("prev", 1, 4), 4)
        self.assertEqual(apply_page_action("prev", 2, 4), 1)
        self.assertEqual(apply_page_action("first", 3, 4), 1)
        self.assertEqual(apply_page_action("first", 1, 4), 1)
        self.assertEqual(apply_page_action("last", 2, 4), 4)
        self.assertEqual(apply_page_action("last", 4, 4), 4)
        self.assertEqual(apply_page_action("refresh", 9, 4), 1)

    def test_page_caption_is_lowercase_and_one_based(self) -> None:
        self.assertEqual(format_page_caption(2, 5), "страница 2 из 5")


class PaginatorTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        unregister_sources()

    def test_nav_row_hidden_on_single_page(self) -> None:
        pager = _pager(lines=["одна запись"], page_limit=1800)
        self.assertEqual(len(pager.pages), 1)
        labels = _button_labels(pager)
        self.assertNotIn("Вперёд", labels)
        self.assertNotIn("Назад", labels)
        self.assertNotIn("страница 1 из 1", _all_text(pager))
        self.assertIsNone(pager.timeout)

    def test_multi_page_has_five_nav_buttons_and_caption(self) -> None:
        pager = _pager()
        self.assertGreater(len(pager.pages), 1)
        labels = _button_labels(pager)
        self.assertEqual(
            set(labels),
            {"Первая", "Назад", "Вперёд", "Последняя", "Обновить"},
        )
        self.assertEqual(len(labels), 5)
        self.assertIn("страница 1 из", _all_text(pager))
        validate_layout(pager)

    async def test_circular_button_walk_keeps_filter(self) -> None:
        pager = _pager(state=PageState(page=1, filter="active", sort="name", owner_id=10))
        total = len(pager.pages)
        interaction = _Interaction(user_id=10, custom_id=pager._base_id("next").render())
        await pager._handle_nav(interaction, "next")
        moved = interaction.response.edit_kwargs["view"]
        self.assertIsInstance(moved, Paginator)
        self.assertEqual(moved.state.page, 2)
        self.assertEqual(moved.state.filter, "active")
        self.assertEqual(moved.state.sort, "name")
        self.assertTrue(any("f=active" in item for item in _custom_ids(moved)))
        self.assertTrue(any("s=name" in item for item in _custom_ids(moved)))
        self.assertTrue(any("o=10" in item for item in _custom_ids(moved)))

        last = _Interaction(user_id=10)
        await moved._handle_nav(last, "prev")
        for _ in range(total):
            step = _Interaction(user_id=10)
            await moved._handle_nav(step, "next")
            moved = step.response.edit_kwargs["view"]
        self.assertEqual(moved.state.page, 2)
        self.assertEqual(moved.state.filter, "active")

        ends = _Interaction(user_id=10)
        await moved._handle_nav(ends, "first")
        first_view = ends.response.edit_kwargs["view"]
        self.assertEqual(first_view.state.page, 1)
        again = _Interaction(user_id=10)
        await first_view._handle_nav(again, "first")
        self.assertEqual(again.response.edit_kwargs["view"].state.page, 1)

        tail = _Interaction(user_id=10)
        await first_view._handle_nav(tail, "last")
        last_view = tail.response.edit_kwargs["view"]
        self.assertEqual(last_view.state.page, total)
        stay = _Interaction(user_id=10)
        await last_view._handle_nav(stay, "last")
        self.assertEqual(stay.response.edit_kwargs["view"].state.page, total)

        wrap = _Interaction(user_id=10)
        await last_view._handle_nav(wrap, "next")
        self.assertEqual(wrap.response.edit_kwargs["view"].state.page, 1)
        self.assertEqual(wrap.response.edit_kwargs["view"].state.filter, "active")

    async def test_unknown_version_tells_user_to_reopen(self) -> None:
        pager = _pager()
        interaction = _Interaction(user_id=10, custom_id="g:9:roster:next:list:o=10&p=1")
        allowed = await pager.interaction_check(interaction)
        self.assertFalse(allowed)
        text = _all_text(interaction.response.kwargs["view"])
        self.assertIn("устарела", text)
        self.assertIn("заново", text)
        self.assertIn("UI.STALE_BUTTON", text)

    async def test_stranger_is_rejected_on_personal_panel(self) -> None:
        pager = _pager()
        custom_id = pager._base_id("next").render()
        self.assertIn("o=10", custom_id)
        interaction = _Interaction(user_id=99, custom_id=custom_id)
        allowed = await pager.interaction_check(interaction)
        self.assertFalse(allowed)
        text = _all_text(interaction.response.kwargs["view"])
        self.assertIn("другим", text)
        self.assertIn("UI.PANEL_OWNER", text)

    async def test_owner_passes_click_time_check(self) -> None:
        pager = _pager()
        interaction = _Interaction(user_id=10, custom_id=pager._base_id("next").render())
        self.assertTrue(await pager.interaction_check(interaction))

    async def test_filter_bar_writes_selection_and_resets_page(self) -> None:
        pager = FilterBar(
            (
                FilterSpec(
                    key="f",
                    placeholder="Фильтр",
                    options=(("all", "Все"), ("active", "Активные")),
                ),
            )
        ).attach(_pager(state=PageState(page=3, owner_id=10)))
        self.assertTrue(any("k=f" in item for item in _custom_ids(pager)))
        interaction = _Interaction(user_id=10, values=["active"])
        await pager._handle_filter(interaction, "f", "active")
        updated = interaction.response.edit_kwargs["view"]
        self.assertEqual(updated.state.page, 1)
        self.assertEqual(updated.state.filter, "active")

    async def test_extra_filter_keys_survive_bar_and_nav(self) -> None:
        state = apply_filter_to_state(
            PageState(page=3, filter="active", sort="name", owner_id=10),
            "rk",
            "4",
        )
        self.assertEqual(state.page, 1)
        self.assertEqual(state.filter, "active")
        self.assertEqual(state.extra, (("rk", "4"),))
        pager = FilterBar(
            (
                FilterSpec(
                    key="rk",
                    placeholder="Должность",
                    options=(("all", "Все"), ("4", "Runner")),
                ),
            )
        ).attach(_pager(state=state))
        self.assertTrue(any("rk=4" in item for item in _custom_ids(pager)))
        interaction = _Interaction(user_id=10, values=["all"])
        await pager._handle_filter(interaction, "rk", "all")
        updated = interaction.response.edit_kwargs["view"]
        self.assertEqual(updated.state.extra, ())
        self.assertEqual(updated.state.filter, "active")

    def test_source_registry_is_not_page_state(self) -> None:
        self.assertEqual(_debug_source_domains(), ())

        class _Source:
            async def get_lines(self, interaction, parsed):
                return _lines()

            async def allowed(self, interaction, parsed):
                return True

            def title(self, parsed):
                return "Состав"

        register_page_source("roster", _Source())
        self.assertEqual(_debug_source_domains(), ("roster",))
        pager = _pager(state=PageState(page=2, filter="active"))
        self.assertEqual(pager.state.page, 2)
        self.assertEqual(_debug_source_domains(), ("roster",))

    def test_ephemeral_pager_still_owns_in_memory_state(self) -> None:
        pager = EphemeralPager(["a", "b"], title="Список", colour=1, owner_id=7)
        self.assertEqual(pager.owner_id, 7)
        self.assertEqual(pager.index, 0)
        self.assertIn("Страница 1 из 2", pager.embed().footer.text)


class TabbedPanelTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        unregister_sources()

    async def test_active_tab_is_marked_and_stored(self) -> None:
        panel = TabbedPanel(
            domain="member",
            object_id="card",
            tabs=(Tab("info", "Сводка"), Tab("cars", "Транспорт")),
            active="cars",
            body="Кузов 1",
            title="Участник",
            owner_id=10,
        )
        self.assertTrue(any("t=cars" in item for item in _custom_ids(panel)))
        options = _select_options(panel)
        marked = next(option for option in options if option.value == "cars")
        self.assertTrue(marked.default)
        self.assertTrue(marked.label.startswith("• "))
        other = next(option for option in options if option.value == "info")
        self.assertFalse(other.default)
        self.assertEqual(other.label, "Сводка")

        interaction = _Interaction(user_id=10, values=["info"])
        await panel._on_tab(interaction)
        switched = interaction.response.edit_kwargs["view"]
        self.assertEqual(switched.active, "info")
        self.assertTrue(any("t=info" in item for item in _custom_ids(switched)))

    def test_nav_hidden_on_single_page(self) -> None:
        panel = TabbedPanel(
            domain="member",
            object_id="card",
            tabs=(Tab("info", "Сводка"),),
            active="info",
            body="Одна страница",
            title="Участник",
        )
        labels = _button_labels(panel)
        self.assertNotIn("Вперёд", labels)
        self.assertNotIn("страница 1 из 1", _all_text(panel))

    def test_multi_page_has_nav_buttons(self) -> None:
        panel = TabbedPanel(
            domain="member",
            object_id="card",
            tabs=(Tab("info", "Сводка"),),
            active="info",
            body="Страница 1",
            title="Участник",
            page=1,
            page_count=3,
        )
        labels = _button_labels(panel)
        self.assertEqual(
            set(labels),
            {"Первая", "Назад", "Вперёд", "Последняя", "Обновить"},
        )
        self.assertIn("страница 1 из 3", _all_text(panel))
        self.assertTrue(any("p=1" in item for item in _custom_ids(panel)))

    async def test_tab_source_paginates_inside_active_group(self) -> None:
        class _Source:
            def tabs(self, parsed):
                del parsed
                return (Tab("economy", "Экономика"), Tab("access", "Доступ"))

            async def get_lines(self, interaction, parsed, tab):
                del interaction, parsed
                prefix = "eco" if tab == "economy" else "acc"
                return [f"{prefix}-{index:04d} " + ("x" * 40) for index in range(80)]

            async def render(self, interaction, parsed, tab):
                lines = await self.get_lines(interaction, parsed, tab)
                return "\n".join(lines[:3])

            async def allowed(self, interaction, parsed):
                del interaction, parsed
                return True

            def title(self, parsed):
                del parsed
                return "Настройки"

        register_tab_source("config", _Source())
        from ui.navigation import build_tabbed_panel

        panel = await build_tabbed_panel(
            _Interaction(user_id=10),
            domain="config",
            object_id="1",
            tab="economy",
            owner_id=10,
        )
        self.assertGreater(panel.page_count, 1)
        self.assertIn("eco-0000", panel.body)
        interaction = _Interaction(user_id=10)
        await panel._handle_nav(interaction, "next")
        moved = interaction.response.edit_kwargs["view"]
        self.assertEqual(moved.page, 2)
        self.assertEqual(moved.active, "economy")
        self.assertNotIn("eco-0000", moved.body)


class ConfirmDialogTests(unittest.IsolatedAsyncioTestCase):
    async def test_double_confirm_runs_once(self) -> None:
        calls = 0

        async def on_confirm(interaction: discord.Interaction) -> None:
            nonlocal calls
            calls += 1

        dialog = ConfirmDialog(
            domain="roster",
            object_id="kick",
            summary="Участник будет исключён из состава.",
            actor="<@10>",
            on_confirm=on_confirm,
            owner_id=10,
            token="deadbeef",
        )
        self.assertIn("Участник будет исключён", _all_text(dialog))
        self.assertIn("Кто запросил", _all_text(dialog))
        self.assertIn("u=0", dialog._id("confirm"))
        first = _Interaction(user_id=10, custom_id=dialog._id("confirm"))
        second = _Interaction(user_id=10, custom_id=dialog._id("confirm"))
        await asyncio.gather(dialog.handle_confirm(first), dialog.handle_confirm(second))
        self.assertEqual(calls, 1)
        rejected = first if first.response.kwargs else second
        accepted = second if first.response.kwargs else first
        self.assertIsNotNone(accepted.response.edit_kwargs)
        text = _all_text(rejected.response.kwargs["view"])
        self.assertIn("уже выполнено", text)
        self.assertIn("UI.ALREADY_DONE", text)
        used_view = accepted.response.edit_kwargs["view"]
        self.assertTrue(used_view.used)
        self.assertIn("u=1", used_view._id("confirm"))

    async def test_used_button_is_rejected_at_click_time(self) -> None:
        async def on_confirm(interaction: discord.Interaction) -> None:
            return None

        dialog = ConfirmDialog(
            domain="roster",
            object_id="kick",
            summary="Участник будет исключён из состава.",
            actor="<@10>",
            on_confirm=on_confirm,
            owner_id=10,
            token="cafebabe",
            used=True,
        )
        interaction = _Interaction(user_id=10, custom_id=dialog._id("confirm"))
        allowed = await dialog.interaction_check(interaction)
        self.assertFalse(allowed)
        self.assertIn("уже выполнено", _all_text(interaction.response.kwargs["view"]))

    async def test_dynamic_unknown_version_is_not_silent(self) -> None:
        parsed = parse_custom_id("g:9:roster:next:list:p=1", allow_stale=True)
        interaction = _Interaction(user_id=10, custom_id="g:9:roster:next:list:p=1")
        await dispatch_dynamic_click(interaction, parsed, selected=())
        self.assertIn("устарела", _all_text(interaction.response.kwargs["view"]))


class BotNavigationHookTests(unittest.TestCase):
    def test_setup_hook_registers_dynamic_navigation(self) -> None:
        source = inspect.getsource(GuinnessBot.setup_hook)
        self.assertIn("register_navigation(self)", source)


if __name__ == "__main__":
    unittest.main()
