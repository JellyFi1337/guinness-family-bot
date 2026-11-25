"""Idempotency and races for reports, payouts, roster, stock, outbox, callbacks."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord

from repositories.accounts import book_balance, find_account, find_obligation, list_ledger
from repositories.events import list_roster
from repositories.inventory import get_balance, get_item_by_sku, get_location_by_kind
from repositories.operations import (
    claim_operation,
    complete_operation,
    enqueue_operation,
)
from repositories.reports import get_report
from services.contracts import add_catalog_item, change_catalog_status, start_run, take_run
from services.event_planning import EventError, create_event, join_event
from services.inventory import (
    InventoryError,
    create_item,
    receive_stock,
    reserve_for_event,
)
from services.membership import ensure_family
from services.report_review import ReportError, review_report, submit_report
from services.treasury import approve_obligation, confirm_obligation, mark_paid
from ui.custom_ids import parse_custom_id
from ui.navigation import ConfirmDialog, dispatch_dynamic_click
from utils import database

try:
    from memphis_harness import MemphisBackendCase
except ImportError:
    from tests.memphis_harness import MemphisBackendCase

GUILD_ID = 9_200
FAMILY_ROLE_ID = 93
PERM_ROLE_ID = 94
STAFF_ID = 9_020
REVIEWER_ID = 9_021
PAYER_ID = 9_022
USER_A = 9_201
USER_B = 9_202
USER_C = 9_203


def _window(*, hours: int = 3, duration: int = 2) -> tuple[str, str]:
    start = datetime.now(UTC) + timedelta(hours=hours)
    end = start + timedelta(hours=duration)
    return start.isoformat(), end.isoformat()


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
    def __init__(self, *, user_id: int = 1, custom_id: str = "") -> None:
        self.response = _Response()
        self.followup = _Followup()
        self.extras: dict = {}
        self.command = None
        self.id = 11
        self.user = SimpleNamespace(id=user_id, name="tester", display_name="tester")
        self.guild_id = GUILD_ID
        self.channel_id = 22
        self.channel = SimpleNamespace(id=22, name="lists")
        self.data = {"custom_id": custom_id, "values": []}
        self.values: list[str] = []
        self.message = None


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


class IdempotencyTests(MemphisBackendCase, unittest.IsolatedAsyncioTestCase):
    guild_id = GUILD_ID
    family_role_id = FAMILY_ROLE_ID
    perm_role_id = PERM_ROLE_ID
    staff_id = STAFF_ID
    db_name = "memphis-idempotency.db"

    async def asyncSetUp(self) -> None:
        await self.open_backend()
        self.build_guild()
        self.reviewer = self.member_actor(REVIEWER_ID, "R")
        self.reviewer.roles = [self.family_role, self.perm_role]
        self.payer = self.member_actor(PAYER_ID, "P")
        self.payer.roles = [self.family_role, self.perm_role]
        self.alpha = self.member_actor(USER_A, "A")
        self.beta = self.member_actor(USER_B, "B")
        self.gamma = self.member_actor(USER_C, "C")
        await self.grant_staff()
        await self.seed_upgrades("air_cargo")
        await self.register_verified(STAFF_ID, "Staff")
        await self.register_verified(REVIEWER_ID, "Reviewer")
        await self.register_verified(PAYER_ID, "Payer")
        await self.register_verified(USER_A, "ICA")
        await self.register_verified(USER_B, "ICB")
        await self.register_verified(USER_C, "ICC")

    async def asyncTearDown(self) -> None:
        await self.close_backend()

    async def _published(self, *, team_max: int = 2):
        item = await add_catalog_item(
            self.staff,
            GUILD_ID,
            title="Доставка",
            direction="criminal",
            team_max=team_max,
            upgrades="air_cargo",
        )
        return await change_catalog_status(
            self.staff, GUILD_ID, item.id, "available"
        )

    async def _accepted_report(self):
        published = await self._published(team_max=1)
        run = await start_run(self.alpha, GUILD_ID, published.id)
        report = await submit_report(
            self.alpha,
            GUILD_ID,
            run.id,
            result_amount=80,
            shares=f"{USER_A}:80",
            duration_minutes=20,
            links="https://example.com/a.png",
            cash=80,
        )
        return await review_report(
            self.staff, GUILD_ID, report.id, "accept", reason="принято"
        )

    async def test_double_report_submit_keeps_first_result(self) -> None:
        published = await self._published()
        run = await start_run(self.alpha, GUILD_ID, published.id, share_bps=5000)
        await take_run(self.beta, GUILD_ID, run.id, share_bps=5000)
        first = await submit_report(
            self.alpha,
            GUILD_ID,
            run.id,
            result_amount=100,
            shares=f"{USER_A}:60,{USER_B}:40",
            duration_minutes=30,
            links="https://example.com/a.png",
        )
        second = await submit_report(
            self.alpha,
            GUILD_ID,
            run.id,
            result_amount=999,
            shares=f"{USER_A}:999",
            duration_minutes=1,
            links="https://example.com/b.png",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.result_amount, 100)

    async def test_parallel_accept_creates_one_obligation(self) -> None:
        published = await self._published()
        run = await start_run(self.alpha, GUILD_ID, published.id, share_bps=5000)
        await take_run(self.beta, GUILD_ID, run.id, share_bps=5000)
        report = await submit_report(
            self.alpha,
            GUILD_ID,
            run.id,
            result_amount=80,
            shares=f"{USER_A}:40,{USER_B}:40",
            duration_minutes=20,
            links="https://example.com/shot.png",
            cash=80,
        )

        async def _accept(who: SimpleNamespace) -> str:
            try:
                accepted = await review_report(
                    who, GUILD_ID, report.id, "accept", reason="проверка"
                )
                return f"ok:{accepted.obligation.id if accepted.obligation else 0}"
            except ReportError as exc:
                return exc.code

        first, second = await asyncio.gather(
            _accept(self.staff), _accept(self.reviewer)
        )
        codes = {first, second}
        self.assertTrue(any(item.startswith("ok:") for item in codes))
        leftovers = {item for item in codes if not item.startswith("ok:")}
        self.assertTrue(
            not leftovers or leftovers <= {"REPORTS.CONFLICT", "REPORTS.ALREADY_REVIEWED"}
        )
        stored = await get_report(report.id)
        assert stored is not None
        self.assertEqual(stored.status, "accepted")
        assert stored.obligation is not None
        self.assertEqual(stored.obligation.amount, 80)

    async def test_outbox_redelivery_does_not_duplicate(self) -> None:
        first = await enqueue_operation(
            idempotency_key="report.notify:9200:1",
            guild_id=GUILD_ID,
            kind="report.notify",
            aggregate_id=1,
            payload={"report_id": 1},
        )
        duplicate = await enqueue_operation(
            idempotency_key="report.notify:9200:1",
            guild_id=GUILD_ID,
            kind="report.notify",
            aggregate_id=1,
            payload={"report_id": 99},
        )
        self.assertEqual(first, duplicate)
        claimed = await claim_operation(kinds=("report.notify",))
        assert claimed is not None
        await complete_operation(claimed.id, external_id=501)
        await complete_operation(claimed.id, external_id=777)
        self.assertIsNone(await claim_operation(kinds=("report.notify",)))
        again = await enqueue_operation(
            idempotency_key="report.notify:9200:1",
            guild_id=GUILD_ID,
            kind="report.notify",
            aggregate_id=1,
            payload={"report_id": 1},
        )
        self.assertEqual(again, first)

    async def test_double_payout_confirm_does_not_double_debit(self) -> None:
        accepted = await self._accepted_report()
        family = await ensure_family(GUILD_ID)
        obligation = await find_obligation(
            family_id=family.id,
            basis_kind="report",
            basis_id=str(accepted.id),
        )
        assert obligation is not None
        approved = await approve_obligation(self.reviewer, GUILD_ID, obligation.id)
        first = await mark_paid(self.payer, GUILD_ID, approved.id, proof_ref="screen-1")
        second = await mark_paid(self.payer, GUILD_ID, first.id, proof_ref="screen-2")
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.status, "paid")
        self.assertEqual(second.proof_ref, "screen-1")
        account = await find_account(
            family_id=family.id, currency="game_money", context="memphis"
        )
        assert account is not None
        self.assertEqual(await book_balance(account.id), -80)
        entries = await list_ledger(account.id)
        self.assertEqual(len(entries), 1)
        once = await confirm_obligation(self.alpha, GUILD_ID, second.id)
        twice = await confirm_obligation(self.alpha, GUILD_ID, once.id)
        self.assertEqual(once.confirmed_by, USER_A)
        self.assertEqual(twice.confirmed_by, USER_A)
        self.assertEqual(await book_balance(account.id), -80)

    async def test_last_seat_race_has_one_winner(self) -> None:
        start, end = _window()
        event = await create_event(
            self.staff,
            GUILD_ID,
            "meeting",
            starts_at=start,
            ends_at=end,
            meetup="офис организации",
            capacity=1,
            reserve_capacity=0,
        )
        results = await asyncio.gather(
            join_event(self.alpha, GUILD_ID, event.id),
            join_event(self.beta, GUILD_ID, event.id),
            return_exceptions=True,
        )
        ok = [item for item in results if not isinstance(item, BaseException)]
        failed = [item for item in results if isinstance(item, EventError)]
        unexpected = [
            item
            for item in results
            if isinstance(item, BaseException) and not isinstance(item, EventError)
        ]
        self.assertEqual(unexpected, [])
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(failed), 1)
        self.assertIn(failed[0].code, {"EVENTS.FULL", "EVENTS.CONFLICT"})
        roster = await list_roster(event.id)
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0].slot, "main")

    async def test_stock_remainder_race_cannot_go_negative(self) -> None:
        await create_item(
            self.staff, GUILD_ID, sku="ammo", title="ammo", unit="шт", low_stock=0
        )
        await receive_stock(self.staff, GUILD_ID, sku="ammo", quantity=1)
        start, end = _window(hours=5)
        first = await create_event(
            self.staff,
            GUILD_ID,
            "meeting",
            starts_at=start,
            ends_at=end,
            meetup="офис организации",
            capacity=4,
        )
        later_start = datetime.now(UTC) + timedelta(hours=9)
        later_end = later_start + timedelta(hours=2)
        second = await create_event(
            self.staff,
            GUILD_ID,
            "meeting",
            starts_at=later_start.isoformat(),
            ends_at=later_end.isoformat(),
            meetup="офис организации",
            capacity=4,
        )
        results = await asyncio.gather(
            reserve_for_event(
                self.staff, GUILD_ID, event_id=first.id, sku="ammo", quantity=1
            ),
            reserve_for_event(
                self.staff, GUILD_ID, event_id=second.id, sku="ammo", quantity=1
            ),
            return_exceptions=True,
        )
        ok = [item for item in results if not isinstance(item, BaseException)]
        failed = [item for item in results if isinstance(item, InventoryError)]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].code, "INV.STOCK")
        family = await ensure_family(GUILD_ID)
        location = await get_location_by_kind(family.id, "warehouse")
        item = await get_item_by_sku(family.id, "ammo")
        assert location is not None
        assert item is not None
        balance = await get_balance(location.id, item.id)
        assert balance is not None
        self.assertGreaterEqual(balance.available, 0)
        self.assertEqual(balance.on_hand, 1)
        self.assertEqual(balance.reserved, 1)

    async def test_db_write_survives_missing_discord_delivery(self) -> None:
        accepted = await self._accepted_report()
        family = await ensure_family(GUILD_ID)
        obligation = await find_obligation(
            family_id=family.id,
            basis_kind="report",
            basis_id=str(accepted.id),
        )
        assert obligation is not None
        key = f"treasury.paid:{obligation.id}"
        op_id = await enqueue_operation(
            idempotency_key=key,
            guild_id=GUILD_ID,
            kind="treasury.notify",
            aggregate_id=obligation.id,
            payload={"obligation_id": obligation.id},
        )
        claimed = await claim_operation(kinds=("treasury.notify",))
        assert claimed is not None
        self.assertEqual(claimed.id, op_id)
        self.assertIsNone(await claim_operation(kinds=("treasury.notify",)))
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        async with database._connect() as db:
            await db.execute(
                "UPDATE operation_outbox SET lease_until = ? WHERE id = ?",
                (expired, op_id),
            )
            await db.commit()
        reclaimed = await claim_operation(kinds=("treasury.notify",))
        assert reclaimed is not None
        self.assertEqual(reclaimed.id, op_id)
        await complete_operation(reclaimed.id, external_id="msg-1")
        retry_id = await enqueue_operation(
            idempotency_key=key,
            guild_id=GUILD_ID,
            kind="treasury.notify",
            aggregate_id=obligation.id,
            payload={"obligation_id": obligation.id},
        )
        self.assertEqual(retry_id, op_id)
        self.assertIsNone(await claim_operation(kinds=("treasury.notify",)))
        still = await find_obligation(
            family_id=family.id,
            basis_kind="report",
            basis_id=str(accepted.id),
        )
        assert still is not None
        self.assertEqual(still.id, obligation.id)
        self.assertEqual(still.amount, obligation.amount)

    async def test_stale_callback_is_rejected(self) -> None:
        async def on_confirm(_interaction: discord.Interaction) -> None:
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
        parsed = parse_custom_id("g:9:roster:next:list:p=1", allow_stale=True)
        stale = _Interaction(user_id=10, custom_id="g:9:roster:next:list:p=1")
        await dispatch_dynamic_click(stale, parsed, selected=())
        self.assertIn("устарела", _all_text(stale.response.kwargs["view"]))
        self.assertIn("UI.STALE_BUTTON", _all_text(stale.response.kwargs["view"]))
