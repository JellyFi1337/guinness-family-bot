from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import Permission, grant_role_permission
from infrastructure.migrations import MIGRATIONS
from infrastructure.postgresql import translate_sql
from repositories.static_messages import set_guild_setting
from services.contracts import add_catalog_item, change_catalog_status, start_run, take_run
from services.membership import register_member, reset_obligation_sources, verify_in_game
from services.profit import (
    ProfitDraft,
    ProfitError,
    PurchaseLine,
    catalog_stats,
    compute_profit,
    draft_from_form,
    integer_median,
    iter_money_ints,
    market_price_hint,
    member_share,
    parse_purchases,
)
from services.report_review import (
    ReportError,
    breakdown_of,
    catalog_profit_stats,
    ensure_report_integrations,
    family_profit_stats,
    review_report,
    submit_report,
)
from utils import database

GUILD_ID = 5_300
FAMILY_ROLE_ID = 76
PERM_ROLE_ID = 77
STAFF_ID = 900
USER_A = 4_001
USER_B = 4_002


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


class ProfitMathTests(unittest.TestCase):
    def test_multiple_purchases_and_rates(self) -> None:
        draft = ProfitDraft(
            purchases=(
                PurchaseLine("рыба", 300, 120),
                PurchaseLine("лёд", 10, 50),
            ),
            expenses=draft_from_form(expenses="топливо:200,ремонт:80").expenses,
            cash_amount=40_000,
            item_payouts=draft_from_form(items="ящик:2:500").item_payouts,
            reputation_delta=15,
        )
        result = compute_profit(draft, people=4, duration_minutes=60)
        self.assertEqual(result.purchase_total, 36_500)
        self.assertEqual(result.expense_total, 280)
        self.assertEqual(result.costs, 36_780)
        self.assertEqual(result.item_valuation_total, 1_000)
        self.assertEqual(result.revenue, 41_000)
        self.assertEqual(result.net, 4_220)
        self.assertEqual(result.per_person, 1_055)
        self.assertEqual(result.per_hour, 4_220)
        self.assertEqual(result.per_person_per_hour, 1_055)
        self.assertTrue(result.item_valuations_are_estimates)
        self.assertTrue(all(line.is_estimate for line in result.item_payouts))
        self.assertEqual(result.reputation_delta, 15)
        self.assertNotEqual(result.net + (result.reputation_delta or 0), result.net)

    def test_reputation_is_not_mixed_into_money(self) -> None:
        draft = ProfitDraft(cash_amount=100, reputation_delta=50)
        result = compute_profit(draft, people=1, duration_minutes=30)
        self.assertEqual(result.net, 100)
        self.assertEqual(result.reputation_delta, 50)

    def test_item_valuation_is_marked_estimate(self) -> None:
        lines = parse_purchases("соль:2:10")
        self.assertEqual(lines[0].total, 20)
        items = draft_from_form(items="соль:2:10").item_payouts
        self.assertTrue(items[0].is_estimate)
        hint = market_price_hint("соль")
        self.assertIsNone(hint.amount)
        self.assertEqual(hint.source, "none")

    def test_integer_median_and_no_float_money(self) -> None:
        self.assertEqual(integer_median((10, 20, 30)), 20)
        self.assertEqual(integer_median((10, 20, 30, 40)), 20)
        draft = ProfitDraft(
            purchases=(PurchaseLine("a", 3, 7), PurchaseLine("b", 1, 2)),
            cash_amount=100,
        )
        result = compute_profit(draft, people=3, duration_minutes=90)
        self.assertEqual(result.per_hour, (77 * 60) // 90)
        for value in iter_money_ints(result):
            self.assertIs(type(value), int)
        self.assertIs(type(member_share(result.net, 40, 100)), int)

    def test_catalog_aggregate(self) -> None:
        first = compute_profit(
            ProfitDraft(cash_amount=100, expenses=draft_from_form(expenses="x:20").expenses),
            people=2,
            duration_minutes=60,
        )
        second = compute_profit(
            ProfitDraft(cash_amount=10, expenses=draft_from_form(expenses="x:50").expenses),
            people=1,
            duration_minutes=60,
        )
        stats = catalog_stats(
            catalog_id=1, title="Доставка", breakdowns=(first, second)
        )
        assert stats is not None
        self.assertEqual(stats.samples, 2)
        self.assertEqual(stats.median_net, second.net)
        self.assertEqual(stats.losing, 1)
        self.assertEqual(stats.net_min, second.net)
        self.assertEqual(stats.net_max, first.net)
        self.assertEqual(stats.people_median, 1)
        self.assertIs(type(stats.median_net), int)


class ProfitSchemaTests(unittest.TestCase):
    def test_migration_translates_to_postgresql(self) -> None:
        migration = next(item for item in MIGRATIONS if item.version == 40)
        joined = "\n".join(
            translate_sql(statement, include_returning=False)
            for statement in migration.statements
        )
        self.assertNotIn("AUTOINCREMENT", joined)
        self.assertNotIn("?", joined)
        self.assertIn("contract_report_purchases", joined)
        self.assertIn("BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", joined)


class ProfitFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "profit.db",
        )
        self.path_patch.start()
        await database.init_db()
        reset_obligation_sources()
        ensure_report_integrations()
        family = _role(FAMILY_ROLE_ID, "High")
        perms = _role(PERM_ROLE_ID, "Кадровик")
        self.guild = _guild([family, perms])
        self.staff = _actor(user_id=STAFF_ID, roles=[family, perms], guild=self.guild)
        self.alpha = _actor(
            user_id=USER_A, roles=[family], guild=self.guild, display_name="A"
        )
        self.beta = _actor(
            user_id=USER_B, roles=[family], guild=self.guild, display_name="B"
        )
        await grant_role_permission(GUILD_ID, Permission.MEMBERS_MANAGE, PERM_ROLE_ID)
        await grant_role_permission(GUILD_ID, Permission.MEMBERS_VIEW, PERM_ROLE_ID)
        await grant_role_permission(GUILD_ID, Permission.CONTRACTS_MANAGE, PERM_ROLE_ID)
        await grant_role_permission(GUILD_ID, Permission.REPORTS_REVIEW, PERM_ROLE_ID)
        await set_guild_setting(GUILD_ID, "profile.enabled_upgrades", "air_cargo")
        for user_id in (USER_A, USER_B):
            await register_member(
                self.staff, GUILD_ID, discord_id=user_id, ic_name=f"IC{user_id}"
            )
            await verify_in_game(self.staff, GUILD_ID, user_id)

    async def asyncTearDown(self) -> None:
        reset_obligation_sources()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def _published(self, *, title: str = "Доставка", team_max: int = 2):
        item = await add_catalog_item(
            self.staff,
            GUILD_ID,
            title=title,
            direction="criminal",
            team_max=team_max,
            upgrades="air_cargo",
        )
        return await change_catalog_status(self.staff, GUILD_ID, item.id, "available")

    async def test_report_stores_integer_profit(self) -> None:
        item = await self._published(team_max=2)
        run = await start_run(self.alpha, GUILD_ID, item.id, share_bps=5000)
        run = await take_run(self.beta, GUILD_ID, run.id, share_bps=5000)
        report = await submit_report(
            self.alpha,
            GUILD_ID,
            run.id,
            result_amount=100,
            shares=f"{USER_A}:60,{USER_B}:40",
            duration_minutes=60,
            links="https://example.com/a.png",
            purchases="рыба:10:20,лёд:2:5",
            expenses="топливо:30",
            cash=300,
            items="ящик:1:40",
            reputation=-3,
        )
        self.assertTrue(report.has_profit)
        breakdown = breakdown_of(report)
        assert breakdown is not None
        self.assertEqual(breakdown.purchase_total, 210)
        self.assertEqual(breakdown.costs, 240)
        self.assertEqual(breakdown.revenue, 340)
        self.assertEqual(breakdown.net, 100)
        self.assertEqual(breakdown.reputation_delta, -3)
        self.assertTrue(breakdown.item_valuations_are_estimates)
        self.assertEqual(member_share(breakdown.net, 60, 100), 60)
        for value in iter_money_ints(breakdown):
            self.assertIs(type(value), int)

    async def test_disabled_catalog_field_is_rejected(self) -> None:
        item = await add_catalog_item(
            self.staff,
            GUILD_ID,
            title="Узкий",
            direction="criminal",
            team_max=1,
            upgrades="air_cargo",
            profit_fields="cash,reputation",
        )
        published = await change_catalog_status(
            self.staff, GUILD_ID, item.id, "available"
        )
        run = await start_run(self.alpha, GUILD_ID, published.id)
        # snapshot taken at start, before? we updated catalog before start. Good.
        with self.assertRaises(ReportError) as raised:
            await submit_report(
                self.alpha,
                GUILD_ID,
                run.id,
                result_amount=10,
                shares=f"{USER_A}:10",
                duration_minutes=10,
                links="https://example.com/x.png",
                purchases="соль:1:1",
            )
        self.assertEqual(raised.exception.code, "PROFIT.FIELD_OFF")

    async def test_catalog_stats_from_accepted_reports(self) -> None:
        first_item = await self._published(title="Море", team_max=1)
        first_run = await start_run(self.alpha, GUILD_ID, first_item.id)
        first = await submit_report(
            self.alpha,
            GUILD_ID,
            first_run.id,
            result_amount=10,
            shares=f"{USER_A}:10",
            duration_minutes=60,
            links="https://example.com/1.png",
            purchases="рыба:10:10",
            cash=200,
        )
        await review_report(self.staff, GUILD_ID, first.id, "accept", reason="ок")
        extra = await start_run(self.beta, GUILD_ID, first_item.id)
        second = await submit_report(
            self.beta,
            GUILD_ID,
            extra.id,
            result_amount=5,
            shares=f"{USER_B}:5",
            duration_minutes=60,
            links="https://example.com/2.png",
            purchases="рыба:10:20",
            cash=50,
        )
        await review_report(self.staff, GUILD_ID, second.id, "accept", reason="ок")
        stats = await catalog_profit_stats(first.family_id, first_item.id, "Море")
        assert stats is not None
        self.assertEqual(stats.samples, 2)
        self.assertEqual(stats.losing, 1)
        self.assertIs(type(stats.median_net), int)
        family = await family_profit_stats(first.family_id)
        self.assertEqual(len(family), 1)

    async def test_rejects_float_like_purchase_text(self) -> None:
        with self.assertRaises(ProfitError):
            parse_purchases("рыба:1:10.5")
