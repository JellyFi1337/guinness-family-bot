from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord

from core.permissions import Permission, grant_role_permission
from cogs.member_panel import (
    PersistentMeButton,
    build_me_view,
    open_personal_panel,
)
from repositories.members import grant_clearance
from services.member_panel import (
    SECTION_EVENTS,
    SECTION_HELP,
    SECTION_PROGRESS,
    SECTION_REPORT,
    SECTION_RESOURCES,
    SECTION_TASKS,
    PanelItem,
    build_member_panel,
    panel_contains_personal_leak,
    public_copy_text,
    public_me_spec,
    register_panel_source,
    reset_panel_sources,
)
from services.membership import register_member
from services.panels import known_panel_keys, panel_definition
from ui.design import validate_layout
from utils import database

GUILD_ID = 4_310
FAMILY_ROLE_ID = 81
PERM_ROLE_ID = 82
STAFF_ID = 910
USER_ID = 1101
OTHER_ID = 1102


def _role(role_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, name=name)


def _guild(roles: list[SimpleNamespace], guild_id: int = GUILD_ID) -> SimpleNamespace:
    by_id = {role.id: role for role in roles}

    def get_role(role_id: int):
        return by_id.get(int(role_id))

    return SimpleNamespace(id=guild_id, roles=roles, get_role=get_role)


def _actor(
    *,
    user_id: int,
    roles: list[SimpleNamespace],
    guild: SimpleNamespace,
    display_name: str = "officer",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        name=display_name,
        display_name=display_name,
        roles=roles,
        guild=guild,
        guild_id=guild.id,
    )


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
    return "\n".join(parts)


def _walk(root: object) -> list[object]:
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


def _select_values(root: object) -> list[str]:
    values: list[str] = []
    for item in _walk(root):
        if isinstance(item, discord.ui.Select):
            values.extend(option.value for option in item.options)
    return values


class _FixedSource:
    def __init__(self, rows: list[PanelItem]) -> None:
        self.rows = rows

    async def list_items(self, guild_id: int, member) -> list[PanelItem]:
        del guild_id
        if member.discord_id != USER_ID:
            return []
        return list(self.rows)


class _Response:
    def __init__(self) -> None:
        self.kwargs: dict | None = None
        self.deferred_ephemeral: bool | None = None
        self._done = False
        self.type = None

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, **_kwargs):
        self.deferred_ephemeral = ephemeral
        self._done = True
        self.type = discord.InteractionResponseType.deferred_channel_message
        return None

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
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int | None = GUILD_ID,
        custom_id: str = "",
    ) -> None:
        self.response = _Response()
        self.followup = _Followup()
        self.extras: dict = {}
        self.id = user_id + 50_000
        self.user = SimpleNamespace(id=user_id, name="tester")
        self.guild_id = guild_id
        self.data = {"custom_id": custom_id, "values": []}
        self.message = None
        self.edited: dict | None = None

    async def edit_original_response(self, **kwargs):
        self.edited = kwargs
        return object()


class MemberPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "member_panel.db",
        )
        self.path_patch.start()
        await database.init_db()
        reset_panel_sources()
        family = _role(FAMILY_ROLE_ID, "High")
        perms = _role(PERM_ROLE_ID, "Кадровик")
        self.guild = _guild([family, perms])
        self.staff = _actor(user_id=STAFF_ID, roles=[family, perms], guild=self.guild)
        await grant_role_permission(GUILD_ID, Permission.MEMBERS_MANAGE, PERM_ROLE_ID)
        await grant_role_permission(GUILD_ID, Permission.MEMBERS_VIEW, PERM_ROLE_ID)
        created, _limit = await register_member(
            self.staff,
            GUILD_ID,
            discord_id=USER_ID,
            ic_name="ИгрокАльфа",
            rank_name="Runner",
            direction="logistics",
        )
        self.member_id = created.id

    async def asyncTearDown(self) -> None:
        reset_panel_sources()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_hides_gated_sections_without_clearance_or_items(self) -> None:
        panel = await build_member_panel(GUILD_ID, USER_ID)
        self.assertEqual(
            panel.visible_keys,
            (SECTION_RESOURCES, SECTION_PROGRESS, SECTION_HELP),
        )
        view = build_me_view(panel, active=SECTION_RESOURCES, owner_id=USER_ID)
        self.assertEqual(
            set(_select_values(view)),
            {SECTION_RESOURCES, SECTION_PROGRESS, SECTION_HELP},
        )

    async def test_clearance_without_items_keeps_tab_and_uses_empty_state(self) -> None:
        await grant_clearance(
            member_id=self.member_id,
            activity="contracts",
            granted_by=STAFF_ID,
        )
        panel = await build_member_panel(GUILD_ID, USER_ID)
        self.assertIn(SECTION_TASKS, panel.visible_keys)
        section = panel.section(SECTION_TASKS)
        self.assertTrue(section.empty)
        view = build_me_view(panel, active=SECTION_TASKS, owner_id=USER_ID)
        text = _all_text(view)
        self.assertIn(section.hint, text)
        self.assertIn("Что дальше", text)
        self.assertIn(section.next_step, text)
        validate_layout(view)

    async def test_public_copy_has_no_personal_data(self) -> None:
        panel = await build_member_panel(GUILD_ID, USER_ID)
        public = public_copy_text()
        self.assertFalse(panel_contains_personal_leak(public, panel.member))
        self.assertNotIn("ИгрокАльфа", public)
        self.assertNotIn(str(USER_ID), public)
        spec = public_me_spec()
        self.assertEqual(spec.kind.value, "public_panel")
        validate_layout(spec.to_layout())
        button = PersistentMeButton()
        labels = [
            item.label
            for item in button.children
            if isinstance(item, discord.ui.Button)
        ]
        self.assertEqual(labels, ["Открыть мою панель"])

    async def test_open_is_always_ephemeral(self) -> None:
        interaction = _Interaction(user_id=USER_ID)
        await open_personal_panel(interaction)
        self.assertTrue(interaction.response.deferred_ephemeral)
        self.assertIsNotNone(interaction.edited)
        view = (interaction.edited or {}).get("view")
        self.assertIsNotNone(view)
        validate_layout(view)

    async def test_foreign_user_cannot_use_personal_view(self) -> None:
        panel = await build_member_panel(GUILD_ID, USER_ID)
        view = build_me_view(panel, active=SECTION_PROGRESS, owner_id=USER_ID)
        custom_id = next(
            str(item.custom_id)
            for item in _walk(view)
            if getattr(item, "custom_id", None)
        )
        stranger = _Interaction(user_id=OTHER_ID, custom_id=custom_id)
        allowed = await view.interaction_check(stranger)
        self.assertFalse(allowed)
        owner = _Interaction(user_id=USER_ID, custom_id=custom_id)
        self.assertTrue(await view.interaction_check(owner))

    async def test_other_member_items_are_not_mixed_in(self) -> None:
        register_panel_source(
            SECTION_TASKS,
            _FixedSource(
                [PanelItem("Контракт А", "Только для первого участника", item_id="c-1")]
            ),
        )
        own = await build_member_panel(GUILD_ID, USER_ID)
        self.assertIn(SECTION_TASKS, own.visible_keys)
        self.assertIn("Контракт А", " ".join(own.section(SECTION_TASKS).lines))
        await register_member(
            self.staff, GUILD_ID, discord_id=OTHER_ID, ic_name="ИгрокБета"
        )
        other = await build_member_panel(GUILD_ID, OTHER_ID)
        self.assertNotIn(SECTION_TASKS, other.visible_keys)
        other_text = " ".join(
            line for section in other.visible for line in section.lines
        )
        self.assertNotIn("Контракт А", other_text)
        self.assertNotIn("ИгрокАльфа", other_text)

    async def test_full_panel_stays_within_v2_limits(self) -> None:
        long = "подробности " * 40
        register_panel_source(
            SECTION_TASKS,
            _FixedSource(
                [
                    PanelItem(
                        f"Контракт {index}",
                        f"{long} состояние в работе, нужно закрыть точку",
                        due="сегодня",
                        item_id=f"c{index}",
                    )
                    for index in range(1, 8)
                ]
            ),
        )
        register_panel_source(
            SECTION_EVENTS,
            _FixedSource(
                [
                    PanelItem(
                        "Капт",
                        "Основа, роль атака, требование: быть в форме",
                        due="завтра",
                        needs_ready=True,
                        item_id="ev-1",
                    ),
                    PanelItem(
                        "Резерв",
                        "Запас, роль логистика",
                        item_id="ev-2",
                    ),
                ]
            ),
        )
        register_panel_source(
            SECTION_REPORT,
            _FixedSource(
                [
                    PanelItem(
                        "Отчёт по запуску",
                        "Известны контракт и состав. Форма не спросит их снова.",
                        item_id="rep-1",
                    )
                ]
            ),
        )
        register_panel_source(
            SECTION_RESOURCES,
            _FixedSource(
                [
                    PanelItem("Комплект", "Выдан набор, срок возврата не задан"),
                    PanelItem("Бронь", "Транспорт до вечера"),
                ]
            ),
        )
        panel = await build_member_panel(GUILD_ID, USER_ID)
        self.assertEqual(
            panel.visible_keys,
            (
                SECTION_TASKS,
                SECTION_EVENTS,
                SECTION_REPORT,
                SECTION_RESOURCES,
                SECTION_PROGRESS,
                SECTION_HELP,
            ),
        )
        for key in panel.visible_keys:
            view = build_me_view(panel, active=key, owner_id=USER_ID)
            validate_layout(view)
        events = build_me_view(panel, active=SECTION_EVENTS, owner_id=USER_ID)
        labels = [
            str(item.label)
            for item in _walk(events)
            if isinstance(item, discord.ui.Button) and item.label
        ]
        self.assertIn("Подтвердить готовность", labels)

    async def test_registry_publishes_me_on_family_features(self) -> None:
        self.assertIn("me", known_panel_keys())
        definition = panel_definition("me")
        self.assertEqual(definition.channel_key, "family_features")
        self.assertEqual(definition.storage_keys, ("me",))


if __name__ == "__main__":
    unittest.main()
