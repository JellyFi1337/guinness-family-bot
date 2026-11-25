from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import Permission, grant_role_permission
from repositories.members import get_current_member
from services.membership import (
    OpenObligation,
    register_member,
    register_obligation_source,
    reset_obligation_sources,
    verify_in_game,
)
from services.roster_watch import (
    KIND_DISCORD_LEFT,
    KIND_ROLE_LOST,
    AuditHint,
    DepartureCard,
    build_departure,
    classify_audience,
    compose_departure_spec,
    decide_watch,
    describe_departure_reason,
    dispatch_leftover_action,
    ensure_default_sources,
    removed_org_role_names,
    reset_leftover_actions,
)
from ui.command_registry import cmd
from ui.templates import notice
from utils import database

GUILD_ID = 4_500
FAMILY_ROLE_ID = 76
PERM_ROLE_ID = 77
STAFF_ID = 900
USER_ID = 1_101


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


class _EventLeftover:
    async def list_open(self, guild_id: int, discord_id: int) -> list[OpenObligation]:
        del guild_id, discord_id
        return [
            OpenObligation(
                kind="event",
                subject_id="evt-1",
                summary="Событие «Смена», основной состав",
                outstanding=True,
                action="promote_reserve",
                reference="https://discord.com/channels/1/2/3",
            )
        ]


class RosterWatchPureTests(unittest.TestCase):
    def test_guest_is_not_pinged_and_can_be_skipped(self) -> None:
        self.assertEqual(
            classify_audience(
                is_bot=False, held_keys={"guest"}, in_roster=False
            ),
            "visitor",
        )
        skipped = decide_watch("visitor", visitor_policy="skip", bot_policy="skip")
        silent = decide_watch("visitor", visitor_policy="silent", bot_policy="skip")
        self.assertFalse(skipped.notify)
        self.assertFalse(skipped.ping)
        self.assertTrue(silent.notify)
        self.assertFalse(silent.ping)

    def test_org_role_is_pinged(self) -> None:
        decision = decide_watch(
            classify_audience(
                is_bot=False, held_keys={"main"}, in_roster=False
            ),
            visitor_policy="skip",
            bot_policy="skip",
        )
        self.assertTrue(decision.notify)
        self.assertTrue(decision.ping)

    def test_reason_without_audit_permission_is_not_a_guess(self) -> None:
        self.assertEqual(
            describe_departure_reason(
                can_read_audit=False,
                hint=AuditHint(action="kick"),
            ),
            "покинул сервер",
        )
        self.assertNotIn(
            "вышел сам",
            describe_departure_reason(can_read_audit=True, hint=None),
        )
        self.assertEqual(
            describe_departure_reason(
                can_read_audit=True, hint=AuditHint(action="kick")
            ),
            "исключён по журналу аудита",
        )
        self.assertEqual(
            describe_departure_reason(
                can_read_audit=True, hint=AuditHint(action="ban")
            ),
            "заблокирован по журналу аудита",
        )

    def test_removed_org_roles_ignore_visitor_roles(self) -> None:
        names = removed_org_role_names(
            before_ids={1, 2, 3},
            after_ids={3},
            org_roles={1: "Main", 9: "Young"},
        )
        self.assertEqual(names, ("Main",))


class RosterWatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "roster_watch.db",
        )
        self.path_patch.start()
        await database.init_db()
        reset_obligation_sources()
        reset_leftover_actions()
        ensure_default_sources()
        family = _role(FAMILY_ROLE_ID, "High")
        perms = _role(PERM_ROLE_ID, "Кадровик")
        self.guild = _guild([family, perms])
        self.staff = _actor(
            user_id=STAFF_ID, roles=[family, perms], guild=self.guild
        )
        await grant_role_permission(GUILD_ID, Permission.MEMBERS_MANAGE, PERM_ROLE_ID)
        await grant_role_permission(GUILD_ID, Permission.MEMBERS_VIEW, PERM_ROLE_ID)

    async def asyncTearDown(self) -> None:
        reset_obligation_sources()
        reset_leftover_actions()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_card_lists_roles_tenure_and_hides_empty_leftovers(self) -> None:
        member, _limit = await register_member(
            self.staff,
            GUILD_ID,
            discord_id=USER_ID,
            ic_name="Alex",
            rank_name="Runner",
            direction="logistics",
        )
        card = await build_departure(
            GUILD_ID,
            discord_id=USER_ID,
            display_name="Alex",
            mention=f"<@{USER_ID}>",
            role_names=("Main", "Help"),
            discord_joined_at=member.joined_at,
            is_bot=False,
            held_keys={"main"},
            kind=KIND_DISCORD_LEFT,
            can_read_audit=False,
            audit=None,
            visitor_policy="skip",
            bot_policy="skip",
        )
        assert card is not None
        fields = dict(card.spec.details)
        self.assertIn(f"<@{USER_ID}>", fields["Участник"])
        self.assertIn("Alex", fields["Участник"])
        self.assertEqual(fields["ID"], str(USER_ID))
        self.assertEqual(fields["Роли на момент события"], "Main, Help")
        self.assertEqual(fields["Должность"], "Runner")
        self.assertEqual(fields["Специализация"], "logistics")
        self.assertIn("Срок членства", fields)
        self.assertNotIn("Что осталось", fields)
        self.assertEqual(card.reason, "покинул сервер")
        self.assertNotIn("вышел сам", card.reason)
        self.assertTrue(card.ping)
        self.assertEqual(card.member_status, "candidate")
        current = await get_current_member(member.family_id, USER_ID)
        assert current is not None
        self.assertEqual(current.status, "candidate")

    async def test_leftovers_and_reserve_subjects_come_from_registry(self) -> None:
        await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID, ic_name="Alex"
        )
        register_obligation_source(_EventLeftover())
        card = await build_departure(
            GUILD_ID,
            discord_id=USER_ID,
            display_name="Alex",
            mention=f"<@{USER_ID}>",
            role_names=("Main",),
            discord_joined_at=None,
            is_bot=False,
            held_keys={"main"},
            kind=KIND_DISCORD_LEFT,
            can_read_audit=False,
            audit=None,
            visitor_policy="skip",
            bot_policy="skip",
        )
        assert card is not None
        self.assertEqual(card.reserve_subjects, ("evt-1",))
        leftovers = dict(card.spec.details)["Что осталось"]
        self.assertIn("Событие «Смена»", leftovers)
        self.assertIn("https://discord.com/channels/1/2/3", leftovers)

    async def test_default_reserve_action_does_not_invent_events(self) -> None:
        text = await dispatch_leftover_action(
            None, GUILD_ID, action="promote_reserve", subject_id="evt-1"
        )
        self.assertIn("не подключены", text.casefold())
        self.assertIn("не поднят", text.casefold())

    async def test_role_loss_card_does_not_leave_the_roster(self) -> None:
        created, _limit = await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID, ic_name="Alex"
        )
        await verify_in_game(self.staff, GUILD_ID, USER_ID)
        card = await build_departure(
            GUILD_ID,
            discord_id=USER_ID,
            display_name="Alex",
            mention=f"<@{USER_ID}>",
            role_names=("Main",),
            discord_joined_at=created.joined_at,
            is_bot=False,
            held_keys={"main"},
            kind=KIND_ROLE_LOST,
            can_read_audit=False,
            audit=None,
            visitor_policy="skip",
            bot_policy="skip",
            removed_role_names=("Main",),
        )
        assert card is not None
        self.assertEqual(card.kind, KIND_ROLE_LOST)
        self.assertIn("Main", card.reason)
        current = await get_current_member(created.family_id, USER_ID)
        assert current is not None
        self.assertEqual(current.status, "active")

    async def test_guest_skip_returns_no_card(self) -> None:
        card = await build_departure(
            GUILD_ID,
            discord_id=USER_ID,
            display_name="Guest",
            mention=f"<@{USER_ID}>",
            role_names=("Гость",),
            discord_joined_at=None,
            is_bot=False,
            held_keys={"guest"},
            kind=KIND_DISCORD_LEFT,
            can_read_audit=False,
            audit=None,
            visitor_policy="skip",
            bot_policy="skip",
        )
        self.assertIsNone(card)

    async def test_guest_silent_card_has_no_ping(self) -> None:
        card = await build_departure(
            GUILD_ID,
            discord_id=USER_ID,
            display_name="Guest",
            mention=f"<@{USER_ID}>",
            role_names=("Гость",),
            discord_joined_at=None,
            is_bot=False,
            held_keys={"guest"},
            kind=KIND_DISCORD_LEFT,
            can_read_audit=False,
            audit=None,
            visitor_policy="silent",
            bot_policy="skip",
        )
        assert card is not None
        self.assertFalse(card.ping)
        self.assertEqual(card.audience, "visitor")

    async def test_roster_row_is_org_even_without_game_role(self) -> None:
        await register_member(
            self.staff, GUILD_ID, discord_id=USER_ID, ic_name="Alex"
        )
        card = await build_departure(
            GUILD_ID,
            discord_id=USER_ID,
            display_name="Alex",
            mention=f"<@{USER_ID}>",
            role_names=(),
            discord_joined_at=None,
            is_bot=False,
            held_keys=set(),
            kind=KIND_DISCORD_LEFT,
            can_read_audit=False,
            audit=None,
            visitor_policy="skip",
            bot_policy="skip",
        )
        assert card is not None
        self.assertEqual(card.audience, "org")
        self.assertTrue(card.ping)

    async def test_view_contains_leave_and_optional_reserve(self) -> None:
        from cogs.roster_watch import RosterDepartureView

        empty = RosterDepartureView(USER_ID, ())
        self.assertEqual(
            [item.custom_id for item in empty.children],
            [f"rw:leave:{USER_ID}"],
        )
        with_event = RosterDepartureView(USER_ID, ("evt-1",))
        self.assertIn(f"rw:reserve:{USER_ID}:evt-1", {item.custom_id for item in with_event.children})
        spec = compose_departure_spec(
            DepartureCard(
                kind=KIND_DISCORD_LEFT,
                discord_id=USER_ID,
                display_name="Alex",
                mention=f"<@{USER_ID}>",
                audience="org",
                ping=True,
                reason="покинул сервер",
                role_names=("Main",),
                discord_joined_at=None,
                family_joined_at=None,
                tenure_days=None,
                rank_name=None,
                specialization=None,
                in_roster=True,
                member_status="active",
                leftovers=(),
                reserve_subjects=(),
                spec=notice(code="ROSTER.BUILDING", title="Состав", summary=""),
            )
        )
        self.assertIn(cmd("members leave"), spec.next_step or "")
