import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from cogs.shop import ShopCog, _SHOP_VIP_ROLES
from infrastructure.database import connection
from repositories.shop import (
    claim_role_sale,
    complete_role_sale,
    complete_shop_purchase,
    count_active_role_holders,
    create_shop_purchase_intent,
    get_pending_role_rejection_presentations,
    get_recoverable_role_approval_claims,
    get_role_sale,
    get_shop_purchase_intent,
    mark_role_rejection_presented,
    mark_shop_intent_provisioning,
    refund_shop_purchase,
    release_role_approval_claim,
    set_shop_intent_discord_role,
)
from utils import database


class ShopRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "shop.db"
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_intent_charge_and_outbox_are_atomic_and_idempotent(self):
        await database.adjust_coins(42, 1_000, "grant")
        first, duplicate = await asyncio.gather(
            create_shop_purchase_intent(
                interaction_id=900,
                guild_id=1,
                user_id=42,
                purchase_kind="color",
                role_name="Colour",
                price=400,
                primary_color=0x112233,
            ),
            create_shop_purchase_intent(
                interaction_id=900,
                guild_id=1,
                user_id=42,
                purchase_kind="color",
                role_name="Colour",
                price=400,
                primary_color=0x112233,
            ),
        )
        self.assertIsNotNone(first)
        self.assertEqual(first.id, duplicate.id)
        self.assertEqual((await database.get_coin_account(42))[0], 600)
        async with connection() as db:
            rows = await db.execute_fetchall(
                "SELECT kind, state FROM operation_outbox WHERE aggregate_id = ?",
                (str(first.id),),
            )
        self.assertEqual(rows, [("shop.fulfill", "pending")])

    async def test_color_intent_finalizes_without_second_charge(self):
        await database.adjust_coins(7, 500, "grant")
        intent = await create_shop_purchase_intent(
            interaction_id=901,
            guild_id=1,
            user_id=7,
            purchase_kind="color",
            role_name="Colour",
            price=300,
            primary_color=0xABCDEF,
        )
        self.assertTrue(await mark_shop_intent_provisioning(intent.id))
        self.assertTrue(await set_shop_intent_discord_role(intent.id, 777))
        self.assertTrue(await complete_shop_purchase(intent.id, 777))
        self.assertTrue(await complete_shop_purchase(intent.id, 777))
        self.assertEqual((await database.get_coin_account(7))[0], 200)
        roles = await database.get_user_purchased_roles(7)
        self.assertEqual([(item[1], item[3]) for item in roles], [(777, 300)])

    async def test_same_colour_role_can_have_multiple_paying_holders(self):
        for user_id in (71, 72):
            await database.adjust_coins(user_id, 500, "grant")
        first = await create_shop_purchase_intent(
            interaction_id=9_071,
            guild_id=1,
            user_id=71,
            purchase_kind="color",
            role_name="🪙 Цвет",
            price=300,
            primary_color=0x123456,
        )
        second = await create_shop_purchase_intent(
            interaction_id=9_072,
            guild_id=1,
            user_id=72,
            purchase_kind="color",
            role_name="🪙 Цвет",
            price=300,
            primary_color=0x123456,
        )
        self.assertTrue(await complete_shop_purchase(first.id, 7_777))
        self.assertTrue(await complete_shop_purchase(second.id, 7_777))
        self.assertEqual(await count_active_role_holders(7_777), 2)

        purchase_id = (await database.get_user_purchased_roles(71))[0][0]
        sale = await claim_role_sale(
            purchase_id,
            71,
            guild_id=1,
            source="user_sale",
        )
        self.assertIsNotNone(await complete_role_sale(sale.purchase_id, 71))
        self.assertEqual(await count_active_role_holders(7_777), 1)

    async def test_intent_refund_is_exactly_once(self):
        await database.adjust_coins(8, 500, "grant")
        intent = await create_shop_purchase_intent(
            interaction_id=902,
            guild_id=1,
            user_id=8,
            purchase_kind="color",
            role_name="Colour",
            price=300,
            primary_color=0,
        )
        results = await asyncio.gather(
            *(refund_shop_purchase(intent.id, "test_refund") for _ in range(10))
        )
        self.assertEqual(sum(results), 1)
        self.assertEqual((await database.get_coin_account(8))[0], 500)

    async def test_vip_until_is_committed_only_on_fulfilment(self):
        await database.adjust_coins(9, 800, "grant")
        intent = await create_shop_purchase_intent(
            interaction_id=903,
            guild_id=1,
            user_id=9,
            purchase_kind="vip",
            role_name="VIP",
            price=400,
            vip_days=7,
        )
        self.assertIsNone((await database.get_coin_account(9))[2])
        self.assertTrue(await complete_shop_purchase(intent.id, 999))
        account = await database.get_coin_account(9)
        completed = await get_shop_purchase_intent(intent.id)
        self.assertEqual(account[2], completed.vip_until)
        self.assertEqual(account[0], 400)

    async def test_queued_vip_purchases_extend_instead_of_overwrite(self):
        await database.adjust_coins(11, 1_000, "grant")
        first = await create_shop_purchase_intent(
            interaction_id=904,
            guild_id=1,
            user_id=11,
            purchase_kind="vip",
            role_name="VIP",
            price=300,
            vip_days=7,
        )
        second = await create_shop_purchase_intent(
            interaction_id=905,
            guild_id=1,
            user_id=11,
            purchase_kind="vip",
            role_name="VIP",
            price=300,
            vip_days=7,
        )
        await refund_shop_purchase(first.id, "first_vip_failed")
        before = datetime.now(UTC)
        await complete_shop_purchase(second.id, 999)
        final = datetime.fromisoformat((await database.get_coin_account(11))[2])
        # A refunded queued purchase must not donate its duration to the next one.
        self.assertGreater((final - before).total_seconds(), 6.9 * 86_400)
        self.assertLess((final - before).total_seconds(), 7.1 * 86_400)

    async def test_custom_request_with_null_message_is_recoverable(self):
        await database.adjust_coins(10, 500, "grant")
        request_id = await database.purchase_role_request(
            10,
            "Custom",
            0x123456,
            None,
            None,
            300,
            guild_id=55,
            channel_id=66,
        )
        self.assertIn((request_id, None), await database.get_pending_role_purchase_requests())
        async with connection() as db:
            rows = await db.execute_fetchall(
                "SELECT kind, state FROM operation_outbox WHERE aggregate_id = ?",
                (str(request_id),),
            )
        self.assertEqual(rows, [("role.request.publish", "pending")])
        await database.set_role_purchase_request_message(
            request_id, 77, guild_id=55, channel_id=66
        )
        publication = await database.get_role_purchase_publication(request_id)
        self.assertEqual((publication[8], publication[11]), (77, "published"))

    async def test_expired_custom_role_claim_requires_explicit_reconciliation(self):
        await database.adjust_coins(12, 500, "grant")
        request_id = await database.purchase_role_request(
            12,
            "Custom",
            0x123456,
            None,
            None,
            300,
            guild_id=55,
            channel_id=66,
        )
        self.assertIsNotNone(
            await database.claim_role_purchase_request(request_id, 99)
        )
        async with connection() as db:
            await db.execute(
                "UPDATE role_purchase_requests SET lease_until = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", request_id),
            )
            await db.commit()

        claims = await get_recoverable_role_approval_claims()
        self.assertEqual([item.request_id for item in claims], [request_id])
        self.assertTrue(await release_role_approval_claim(request_id, 99))
        publication = await database.get_role_purchase_publication(request_id)
        self.assertEqual((publication[7], publication[11]), ("pending", "pending"))

    async def test_role_sale_reservation_and_outbox_are_atomic(self):
        await database.adjust_coins(21, 1_000, "grant")
        self.assertTrue(await database.spend_coins(21, 400, "purchase"))
        await database.register_purchased_role(21, 9_001, "Custom", 400, "custom")
        purchase_id = (await database.get_user_purchased_roles(21))[0][0]

        claims = await asyncio.gather(
            *(
                claim_role_sale(
                    purchase_id,
                    21,
                    guild_id=55,
                    source="user_sale",
                )
                for _ in range(10)
            )
        )
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertEqual((await get_role_sale(purchase_id)).active, 2)
        async with connection() as db:
            outbox = await db.execute_fetchall(
                """SELECT kind, state FROM operation_outbox
                   WHERE idempotency_key = ?""",
                (f"shop-role-sale:{purchase_id}",),
            )
        self.assertEqual([tuple(row) for row in outbox], [("shop.role.sale", "pending")])

        completed = await asyncio.gather(
            *(complete_role_sale(purchase_id, 21) for _ in range(10))
        )
        self.assertEqual(sum(item is not None for item in completed), 1)
        self.assertEqual((await get_role_sale(purchase_id)).active, 0)
        self.assertEqual((await database.get_coin_account(21))[0], 800)

    async def test_role_rejection_refund_queues_durable_presentation(self):
        await database.adjust_coins(22, 1_000, "grant")
        request_id = await database.purchase_role_request(
            22,
            "Custom",
            0x123456,
            None,
            None,
            400,
            guild_id=55,
            channel_id=66,
        )
        self.assertIsNotNone(
            await database.claim_role_purchase_request(request_id, 99)
        )
        self.assertTrue(
            await database.reject_role_purchase_with_refund(
                request_id,
                99,
                200,
                "role_rejection_refund",
                reason="Провокационное название",
            )
        )
        pending = await get_pending_role_rejection_presentations()
        self.assertEqual([item.request_id for item in pending], [request_id])
        self.assertEqual((pending[0].refund, pending[0].reason), (200, "Провокационное название"))
        async with connection() as db:
            outbox = await db.execute_fetchall(
                """SELECT kind, state FROM operation_outbox
                   WHERE idempotency_key = ?""",
                (f"role-request:{request_id}:reject-present",),
            )
        self.assertEqual(
            [tuple(row) for row in outbox],
            [("role.reject.present", "pending")],
        )
        self.assertTrue(await mark_role_rejection_presented(request_id))
        self.assertEqual(await get_pending_role_rejection_presentations(), [])


class _FakeRole:
    def __init__(self, role_id: int, name: str, colour: int = 0):
        self.id = role_id
        self.name = name
        self.colour = discord.Colour(colour)
        self.position = 1
        self.created_at = datetime.now(UTC)
        self.deleted = False

    async def delete(self, **_kwargs):
        self.deleted = True


class _FakeMember:
    def __init__(self, user_id: int):
        self.id = user_id
        self.roles = []

    async def add_roles(self, role, **_kwargs):
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, role, **_kwargs):
        if role in self.roles:
            self.roles.remove(role)


class _AuditLog:
    def __init__(self, entries):
        self.entries = entries

    def __aiter__(self):
        self.iterator = iter(self.entries)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeGuild:
    def __init__(self, members):
        self.id = 321
        self.members = {member.id: member for member in members}
        self.roles = []
        self.created_roles = []
        self.audit_entries = []
        self.features = []

    def get_member(self, user_id):
        return self.members.get(user_id)

    async def fetch_member(self, user_id):
        return self.members[user_id]

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    async def create_role(self, *, name, reason, **_kwargs):
        await asyncio.sleep(0.01)
        colour = _kwargs.get("colour", discord.Colour.default())
        role = _FakeRole(
            10_000 + len(self.created_roles),
            name,
            colour.value if hasattr(colour, "value") else int(colour),
        )
        self.created_roles.append(role)
        self.audit_entries.insert(0, SimpleNamespace(reason=reason, target=role))
        # Deliberately omit guild.roles to model gateway-cache delay.
        return role

    async def fetch_roles(self):
        return list(self.roles)

    def audit_logs(self, **_kwargs):
        return _AuditLog(self.audit_entries)

    def get_channel(self, _channel_id):
        return None


class _FakeBot:
    def __init__(self, guild):
        self.guilds = [guild]
        self.guild = guild
        self.user = SimpleNamespace(id=999)

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


class ShopDiscordReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "discord-shop.db"
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        await database.init_db()
        _SHOP_VIP_ROLES.clear()

    async def asyncTearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_audit_marker_adopts_role_created_before_crash(self):
        member = _FakeMember(20)
        guild = _FakeGuild([member])
        bot = _FakeBot(guild)
        cog = ShopCog(bot)
        await database.adjust_coins(member.id, 500, "grant")
        intent = await create_shop_purchase_intent(
            interaction_id=1001,
            guild_id=guild.id,
            user_id=member.id,
            purchase_kind="color",
            role_name="Colour",
            price=300,
            primary_color=0x123456,
        )
        await mark_shop_intent_provisioning(intent.id)
        orphan = _FakeRole(88_888, "Colour")
        guild.audit_entries.append(
            SimpleNamespace(reason=f"shop-purchase:{intent.id}", target=orphan)
        )
        method_globals = cog.fulfill_purchase_intent.__func__.__globals__
        with patch.dict(
            method_globals,
            {"_order_cosmetic_roles": AsyncMock()},
        ):
            role_id = await cog.fulfill_purchase_intent(intent.id)
        self.assertEqual(role_id, orphan.id)
        self.assertIn(orphan, member.roles)
        self.assertEqual(len(guild.created_roles), 0)
        self.assertEqual((await get_shop_purchase_intent(intent.id)).state, "completed")

    async def test_role_sale_recovery_deletes_role_and_refunds_once(self):
        member = _FakeMember(25)
        guild = _FakeGuild([member])
        role = _FakeRole(90_001, "Custom")
        guild.roles.append(role)
        cog = ShopCog(_FakeBot(guild))
        await database.adjust_coins(member.id, 1_000, "grant")
        self.assertTrue(await database.spend_coins(member.id, 400, "purchase"))
        await database.register_purchased_role(
            member.id,
            role.id,
            role.name,
            400,
            "custom",
        )
        purchase_id = (await database.get_user_purchased_roles(member.id))[0][0]
        sale = await claim_role_sale(
            purchase_id,
            member.id,
            guild_id=guild.id,
            source="user_sale",
        )

        first = await cog._process_role_sale(guild, sale)
        second = await cog._process_role_sale(guild, sale)

        self.assertTrue(role.deleted)
        self.assertEqual((first.active, second.active), (0, 0))
        self.assertEqual((await database.get_coin_account(member.id))[0], 800)

    async def test_concurrent_vip_intents_share_role_during_cache_delay(self):
        members = [_FakeMember(31), _FakeMember(32)]
        guild = _FakeGuild(members)
        cog = ShopCog(_FakeBot(guild))
        intents = []
        for index, member in enumerate(members):
            await database.adjust_coins(member.id, 500, "grant")
            intents.append(
                await create_shop_purchase_intent(
                    interaction_id=2000 + index,
                    guild_id=guild.id,
                    user_id=member.id,
                    purchase_kind="vip",
                    role_name="VIP",
                    price=300,
                    vip_days=7,
                )
            )
        with (
            patch.object(cog, "_apply_vip_voice_access", new=AsyncMock()),
            patch("cogs.shop._refresh_user_summits", new=AsyncMock()),
        ):
            role_ids = await asyncio.gather(
                *(cog.fulfill_purchase_intent(intent.id) for intent in intents)
            )
        self.assertEqual(len(set(role_ids)), 1)
        self.assertEqual(len(guild.created_roles), 1)
        role = guild.created_roles[0]
        self.assertTrue(all(role in member.roles for member in members))
        states = [
            (await get_shop_purchase_intent(intent.id)).state for intent in intents
        ]
        self.assertEqual(states, ["completed", "completed"])

    async def test_matching_colour_is_reused_and_survives_first_sale(self):
        first_member = _FakeMember(41)
        second_member = _FakeMember(42)
        guild = _FakeGuild([first_member, second_member])
        cog = ShopCog(_FakeBot(guild))
        intents = []
        for index, member in enumerate((first_member, second_member)):
            await database.adjust_coins(member.id, 500, "grant")
            intents.append(
                await create_shop_purchase_intent(
                    interaction_id=3_000 + index,
                    guild_id=guild.id,
                    user_id=member.id,
                    purchase_kind="color",
                    role_name="🪙 Цвет",
                    price=300,
                    primary_color=0x445566,
                )
            )

        method_globals = cog.fulfill_purchase_intent.__func__.__globals__
        with patch.dict(
            method_globals,
            {"_order_cosmetic_roles": AsyncMock()},
        ):
            first_role_id = await cog.fulfill_purchase_intent(intents[0].id)
            shared_role = guild.created_roles[0]
            guild.roles.append(shared_role)
            second_role_id = await cog.fulfill_purchase_intent(intents[1].id)

        self.assertEqual(first_role_id, second_role_id)
        self.assertEqual(len(guild.created_roles), 1)
        self.assertIn(shared_role, first_member.roles)
        self.assertIn(shared_role, second_member.roles)

        first_purchase = (await database.get_user_purchased_roles(first_member.id))[0]
        sale = await claim_role_sale(
            first_purchase[0],
            first_member.id,
            guild_id=guild.id,
            source="user_sale",
        )
        await cog._process_role_sale(guild, sale)
        self.assertFalse(shared_role.deleted)
        self.assertNotIn(shared_role, first_member.roles)
        self.assertIn(shared_role, second_member.roles)

    async def test_legacy_role_request_is_upgraded_with_marker(self):
        message = MagicMock(spec=discord.Message)
        message.id = 444
        message.content = "<@&123>"
        message.components = []
        message.flags = SimpleNamespace(components_v2=False)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 66
        channel.fetch_message = AsyncMock(return_value=message)
        guild = MagicMock(spec=discord.Guild)
        guild.id = 55
        guild.get_channel.return_value = channel
        bot = MagicMock()
        bot.user = SimpleNamespace(id=999)
        cog = ShopCog(bot)
        request = (
            12,
            10,
            "Custom",
            0x123456,
            None,
            None,
            300,
            "pending",
            444,
            55,
            66,
            "published",
        )
        edit = AsyncMock()
        # Extension loading tests intentionally unload/reload cogs.shop. Patch
        # the method's own globals so this remains stable in full-suite order.
        method_globals = cog.publish_role_request.__func__.__globals__
        with patch.dict(
            method_globals,
            {
                "get_role_purchase_publication": AsyncMock(
                    return_value=request
                ),
                "edit_message_v2": edit,
                "set_role_purchase_request_message": AsyncMock(),
            },
        ):
            result = await cog.publish_role_request(12, guild)
        self.assertEqual(result, 444)
        self.assertEqual(edit.await_args.kwargs["component_marker"], "role-purchase:12")
        self.assertEqual(edit.await_args.kwargs["content"], "<@&123>")
