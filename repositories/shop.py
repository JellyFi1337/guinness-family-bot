from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from infrastructure.database import connection, utc_now
from infrastructure.transactions import begin_write


@dataclass(frozen=True, slots=True)
class ShopPurchaseIntent:
    id: int
    interaction_id: int
    guild_id: int
    user_id: int
    purchase_kind: str
    role_name: str
    price: int
    primary_color: int | None
    secondary_color: int | None
    tertiary_color: int | None
    vip_until: str | None
    vip_days: int | None
    state: str
    discord_role_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RoleApprovalClaim:
    request_id: int
    guild_id: int
    user_id: int
    reviewer_id: int
    role_name: str
    price: int
    discord_role_id: int | None
    operation_state: str
    lease_until: str | None
    reviewed_at: str | None


@dataclass(frozen=True, slots=True)
class RoleSale:
    purchase_id: int
    user_id: int
    role_id: int
    role_name: str
    price: int
    role_kind: str
    active: int

    @property
    def refund(self) -> int:
        return self.price // 2


@dataclass(frozen=True, slots=True)
class RoleRejectionPresentation:
    request_id: int
    guild_id: int
    channel_id: int | None
    message_id: int | None
    user_id: int
    reviewer_id: int
    role_name: str
    primary_color: int
    secondary_color: int | None
    tertiary_color: int | None
    price: int
    refund: int
    reason: str
    presentation_state: str


_ROLE_SALE_SOURCES = {"user_sale", "vip_expired"}


def _role_sale(row) -> RoleSale:
    return RoleSale(
        purchase_id=int(row[0]),
        user_id=int(row[1]),
        role_id=int(row[2]),
        role_name=str(row[3]),
        price=int(row[4]),
        role_kind=str(row[5]),
        active=int(row[6]),
    )


def _role_rejection(row) -> RoleRejectionPresentation:
    return RoleRejectionPresentation(
        request_id=int(row[0]),
        guild_id=int(row[1]),
        channel_id=int(row[2]) if row[2] is not None else None,
        message_id=int(row[3]) if row[3] is not None else None,
        user_id=int(row[4]),
        reviewer_id=int(row[5]),
        role_name=str(row[6]),
        primary_color=int(row[7]),
        secondary_color=int(row[8]) if row[8] is not None else None,
        tertiary_color=int(row[9]) if row[9] is not None else None,
        price=int(row[10]),
        refund=int(row[11]),
        reason=str(row[12] or "Заявка отклонена"),
        presentation_state=str(row[13]),
    )


def _intent(row) -> ShopPurchaseIntent:
    return ShopPurchaseIntent(
        id=int(row[0]),
        interaction_id=int(row[1]),
        guild_id=int(row[2]),
        user_id=int(row[3]),
        purchase_kind=str(row[4]),
        role_name=str(row[5]),
        price=int(row[6]),
        primary_color=int(row[7]) if row[7] is not None else None,
        secondary_color=int(row[8]) if row[8] is not None else None,
        tertiary_color=int(row[9]) if row[9] is not None else None,
        vip_until=str(row[10]) if row[10] is not None else None,
        vip_days=int(row[11]) if row[11] is not None else None,
        state=str(row[12]),
        discord_role_id=int(row[13]) if row[13] is not None else None,
        created_at=str(row[14]),
        updated_at=str(row[15]),
    )


_INTENT_COLUMNS = """id, interaction_id, guild_id, user_id, purchase_kind,
    role_name, price, primary_color, secondary_color, tertiary_color, vip_until,
    vip_days, state, discord_role_id, created_at, updated_at"""


async def create_shop_purchase_intent(
    *,
    interaction_id: int,
    guild_id: int,
    user_id: int,
    purchase_kind: str,
    role_name: str,
    price: int,
    primary_color: int | None = None,
    secondary_color: int | None = None,
    tertiary_color: int | None = None,
    vip_days: int | None = None,
) -> ShopPurchaseIntent | None:
    """Atomically charge once, persist an intent and enqueue its fulfilment."""

    if purchase_kind not in {"color", "vip"}:
        raise ValueError("Unsupported shop purchase kind")
    if price < 1:
        raise ValueError("Purchase price must be positive")
    if purchase_kind == "vip" and (vip_days is None or vip_days < 1):
        raise ValueError("VIP duration must be positive")

    async with connection() as db:
        try:
            await begin_write(db, "shop-interaction", interaction_id)
            existing = await db.execute_fetchall(
                f"SELECT {_INTENT_COLUMNS} FROM shop_purchase_intents WHERE interaction_id = ?",
                (interaction_id,),
            )
            if existing:
                await db.commit()
                return _intent(existing[0])

            await db.execute(
                "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)",
                (user_id,),
            )
            account = await db.execute_fetchall(
                "SELECT balance FROM coin_accounts WHERE user_id = ?",
                (user_id,),
            )
            if not account or int(account[0][0]) < price:
                await db.rollback()
                return None

            now = utc_now()
            vip_until = None

            await db.execute(
                "UPDATE coin_accounts SET balance = balance - ? WHERE user_id = ?",
                (price, user_id),
            )
            cursor = await db.execute(
                """INSERT INTO shop_purchase_intents
                   (interaction_id, guild_id, user_id, purchase_kind, role_name,
                    price, primary_color, secondary_color, tertiary_color,
                    vip_until, vip_days, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    interaction_id,
                    guild_id,
                    user_id,
                    purchase_kind,
                    role_name,
                    price,
                    primary_color,
                    secondary_color,
                    tertiary_color,
                    vip_until,
                    vip_days,
                    now,
                    now,
                ),
            )
            intent_id = int(cursor.lastrowid)
            await db.execute(
                """INSERT INTO coin_transactions
                   (user_id, amount, action, created_at) VALUES (?, ?, ?, ?)""",
                (user_id, -price, f"purchase_{purchase_kind}", now),
            )
            payload = json.dumps(
                {"intent_id": intent_id},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await db.execute(
                """INSERT OR IGNORE INTO operation_outbox
                   (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                    available_at, created_at, updated_at)
                   VALUES (?, ?, 'shop.fulfill', ?, ?, ?, ?, ?)""",
                (
                    f"shop-purchase:{intent_id}:fulfill",
                    guild_id,
                    str(intent_id),
                    payload,
                    now,
                    now,
                    now,
                ),
            )
            rows = await db.execute_fetchall(
                f"SELECT {_INTENT_COLUMNS} FROM shop_purchase_intents WHERE id = ?",
                (intent_id,),
            )
            await db.commit()
            return _intent(rows[0])
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception:
            await db.rollback()
            raise


async def get_shop_purchase_intent(intent_id: int) -> ShopPurchaseIntent | None:
    async with connection() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_INTENT_COLUMNS} FROM shop_purchase_intents WHERE id = ?",
            (intent_id,),
        )
    return _intent(rows[0]) if rows else None


async def get_unfinished_shop_purchase_intents(limit: int = 500) -> list[ShopPurchaseIntent]:
    async with connection() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_INTENT_COLUMNS} FROM shop_purchase_intents
                WHERE state IN ('pending', 'provisioning', 'discord_created')
                ORDER BY id LIMIT ?""",
            (max(1, min(int(limit), 2_000)),),
        )
    return [_intent(row) for row in rows]


async def get_recoverable_role_approval_claims(
    limit: int = 250,
) -> list[RoleApprovalClaim]:
    """Return only expired/uncertain approval claims safe for reconciliation."""

    now = utc_now()
    async with connection() as db:
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, user_id, reviewer_id, role_name, price,
                      discord_role_id, operation_state, lease_until, reviewed_at
               FROM role_purchase_requests
               WHERE status = 'processing' AND reviewer_id IS NOT NULL
                 AND guild_id IS NOT NULL
                 AND (
                      discord_role_id IS NOT NULL
                      OR operation_state = 'needs_reconcile'
                      OR (lease_until IS NOT NULL AND lease_until <= ?)
                 )
               ORDER BY id LIMIT ?""",
            (now, max(1, min(int(limit), 1_000))),
        )
    return [
        RoleApprovalClaim(
            request_id=int(row[0]),
            guild_id=int(row[1]),
            user_id=int(row[2]),
            reviewer_id=int(row[3]),
            role_name=str(row[4]),
            price=int(row[5]),
            discord_role_id=int(row[6]) if row[6] is not None else None,
            operation_state=str(row[7]),
            lease_until=str(row[8]) if row[8] is not None else None,
            reviewed_at=str(row[9]) if row[9] is not None else None,
        )
        for row in rows
    ]


async def release_role_approval_claim(
    request_id: int, reviewer_id: int
) -> bool:
    """Release one claim only after Discord was checked for an orphan role."""

    async with connection() as db:
        cursor = await db.execute(
            """UPDATE role_purchase_requests
               SET status = 'pending', reviewer_id = NULL, reviewed_at = NULL,
                   operation_state = 'pending', lease_until = NULL
               WHERE id = ? AND reviewer_id = ? AND status = 'processing'
                 AND discord_role_id IS NULL""",
            (request_id, reviewer_id),
        )
        await db.commit()
    return cursor.rowcount == 1


async def claim_role_sale(
    purchase_id: int,
    user_id: int,
    *,
    guild_id: int,
    source: str,
) -> RoleSale | None:
    """Reserve a sale and persist its Discord deletion/refund intent atomically."""

    if source not in _ROLE_SALE_SOURCES:
        raise ValueError("Unsupported role sale source")
    now = utc_now()
    async with connection() as db:
        try:
            await begin_write(db, "shop-role-sale", purchase_id)
            rows = await db.execute_fetchall(
                """SELECT id, user_id, role_id, role_name, price, role_kind, active
                   FROM purchased_roles
                   WHERE id = ? AND user_id = ? AND active = 1""",
                (purchase_id, user_id),
            )
            if not rows:
                await db.rollback()
                return None
            sale = _role_sale(rows[0])
            cursor = await db.execute(
                """UPDATE purchased_roles SET active = 2
                   WHERE id = ? AND user_id = ? AND active = 1""",
                (purchase_id, user_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            payload = json.dumps(
                {
                    "purchase_id": sale.purchase_id,
                    "user_id": sale.user_id,
                    "role_id": sale.role_id,
                    "role_name": sale.role_name,
                    "price": sale.price,
                    "role_kind": sale.role_kind,
                    "source": source,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await db.execute(
                """INSERT OR IGNORE INTO operation_outbox
                   (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                    available_at, created_at, updated_at)
                   VALUES (?, ?, 'shop.role.sale', ?, ?, ?, ?, ?)""",
                (
                    f"shop-role-sale:{sale.purchase_id}",
                    guild_id,
                    str(sale.purchase_id),
                    payload,
                    now,
                    now,
                    now,
                ),
            )
            await db.commit()
            return RoleSale(
                sale.purchase_id,
                sale.user_id,
                sale.role_id,
                sale.role_name,
                sale.price,
                sale.role_kind,
                2,
            )
        except BaseException:
            await db.rollback()
            raise


async def get_role_sale(purchase_id: int) -> RoleSale | None:
    async with connection() as db:
        rows = await db.execute_fetchall(
            """SELECT id, user_id, role_id, role_name, price, role_kind, active
               FROM purchased_roles WHERE id = ?""",
            (purchase_id,),
        )
    return _role_sale(rows[0]) if rows else None


async def get_reserved_role_sales(
    *, user_id: int | None = None, limit: int = 1_000
) -> list[RoleSale]:
    where = "active = 2"
    params: list[int] = []
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(int(user_id))
    params.append(max(1, min(int(limit), 5_000)))
    async with connection() as db:
        rows = await db.execute_fetchall(
            f"""SELECT id, user_id, role_id, role_name, price, role_kind, active
                FROM purchased_roles WHERE {where} ORDER BY id LIMIT ?""",
            tuple(params),
        )
    return [_role_sale(row) for row in rows]


async def ensure_role_sale_outbox(
    sale: RoleSale,
    *,
    guild_id: int,
    source: str,
) -> None:
    if source not in _ROLE_SALE_SOURCES:
        raise ValueError("Unsupported role sale source")
    now = utc_now()
    payload = json.dumps(
        {
            "purchase_id": sale.purchase_id,
            "user_id": sale.user_id,
            "role_id": sale.role_id,
            "role_name": sale.role_name,
            "price": sale.price,
            "role_kind": sale.role_kind,
            "source": source,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    key = f"shop-role-sale:{sale.purchase_id}"
    async with connection() as db:
        await db.execute(
            """INSERT OR IGNORE INTO operation_outbox
               (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                available_at, created_at, updated_at)
               VALUES (?, ?, 'shop.role.sale', ?, ?, ?, ?, ?)""",
            (key, guild_id, str(sale.purchase_id), payload, now, now, now),
        )
        await db.execute(
            """UPDATE operation_outbox
               SET state = 'retry', attempts = 0, available_at = ?,
                   lease_until = NULL, last_error = NULL, updated_at = ?
               WHERE idempotency_key = ? AND state = 'dead'""",
            (now, now, key),
        )
        await db.commit()


async def complete_role_sale(purchase_id: int, user_id: int) -> RoleSale | None:
    """Finalize a reserved sale and credit its 50% refund exactly once."""

    async with connection() as db:
        try:
            await begin_write(db, "shop-role-sale", purchase_id)
            rows = await db.execute_fetchall(
                """SELECT id, user_id, role_id, role_name, price, role_kind, active
                   FROM purchased_roles
                   WHERE id = ? AND user_id = ? AND active = 2""",
                (purchase_id, user_id),
            )
            if not rows:
                await db.rollback()
                return None
            sale = _role_sale(rows[0])
            now = utc_now()
            await db.execute(
                """UPDATE purchased_roles SET active = 0, sold_at = ?
                   WHERE id = ? AND user_id = ? AND active = 2""",
                (now, purchase_id, user_id),
            )
            await db.execute(
                "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)",
                (user_id,),
            )
            await db.execute(
                "UPDATE coin_accounts SET balance = balance + ? WHERE user_id = ?",
                (sale.refund, user_id),
            )
            await db.execute(
                """INSERT INTO coin_transactions
                   (user_id, amount, action, created_at)
                   VALUES (?, ?, 'role_sale', ?)""",
                (user_id, sale.refund, now),
            )
            await db.commit()
            return RoleSale(
                sale.purchase_id,
                sale.user_id,
                sale.role_id,
                sale.role_name,
                sale.price,
                sale.role_kind,
                0,
            )
        except BaseException:
            await db.rollback()
            raise


async def get_role_rejection_presentation(
    request_id: int,
) -> RoleRejectionPresentation | None:
    async with connection() as db:
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, channel_id, message_id, user_id, reviewer_id,
                      role_name, primary_color, secondary_color, tertiary_color,
                      price, refund_amount, rejection_reason, presentation_state
               FROM role_purchase_requests
               WHERE id = ? AND status = 'rejected' AND guild_id IS NOT NULL
                 AND reviewer_id IS NOT NULL""",
            (request_id,),
        )
    return _role_rejection(rows[0]) if rows else None


async def get_pending_role_rejection_presentations(
    limit: int = 500,
) -> list[RoleRejectionPresentation]:
    async with connection() as db:
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, channel_id, message_id, user_id, reviewer_id,
                      role_name, primary_color, secondary_color, tertiary_color,
                      price, refund_amount, rejection_reason, presentation_state
               FROM role_purchase_requests
               WHERE status = 'rejected' AND presentation_state = 'pending'
                 AND guild_id IS NOT NULL AND reviewer_id IS NOT NULL
               ORDER BY id LIMIT ?""",
            (max(1, min(int(limit), 2_000)),),
        )
    return [_role_rejection(row) for row in rows]


async def ensure_role_rejection_outbox(
    rejection: RoleRejectionPresentation,
) -> None:
    now = utc_now()
    key = f"role-request:{rejection.request_id}:reject-present"
    payload = json.dumps(
        {"request_id": rejection.request_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    async with connection() as db:
        await db.execute(
            """INSERT OR IGNORE INTO operation_outbox
               (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                available_at, created_at, updated_at)
               VALUES (?, ?, 'role.reject.present', ?, ?, ?, ?, ?)""",
            (
                key,
                rejection.guild_id,
                str(rejection.request_id),
                payload,
                now,
                now,
                now,
            ),
        )
        await db.execute(
            """UPDATE operation_outbox
               SET state = 'retry', attempts = 0, available_at = ?,
                   lease_until = NULL, last_error = NULL, updated_at = ?
               WHERE idempotency_key = ? AND state = 'dead'""",
            (now, now, key),
        )
        await db.commit()


async def mark_role_rejection_presented(request_id: int) -> bool:
    async with connection() as db:
        cursor = await db.execute(
            """UPDATE role_purchase_requests SET presentation_state = 'completed'
               WHERE id = ? AND status = 'rejected'
                 AND presentation_state = 'pending'""",
            (request_id,),
        )
        await db.commit()
    return cursor.rowcount == 1


async def ensure_shop_purchase_outbox(intent: ShopPurchaseIntent) -> None:
    now = utc_now()
    payload = json.dumps({"intent_id": intent.id}, separators=(",", ":"))
    async with connection() as db:
        await db.execute(
            """INSERT OR IGNORE INTO operation_outbox
               (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                available_at, created_at, updated_at)
               VALUES (?, ?, 'shop.fulfill', ?, ?, ?, ?, ?)""",
            (
                f"shop-purchase:{intent.id}:fulfill",
                intent.guild_id,
                str(intent.id),
                payload,
                now,
                now,
                now,
            ),
        )
        await db.execute(
            """UPDATE operation_outbox
               SET state = 'retry', attempts = 0, available_at = ?,
                   lease_until = NULL, last_error = NULL, updated_at = ?
               WHERE idempotency_key = ? AND state = 'dead'""",
            (now, now, f"shop-purchase:{intent.id}:fulfill"),
        )
        await db.commit()


async def set_shop_intent_discord_role(intent_id: int, role_id: int) -> bool:
    async with connection() as db:
        cursor = await db.execute(
            """UPDATE shop_purchase_intents
               SET discord_role_id = ?, state = 'discord_created', updated_at = ?,
                   last_error = NULL
               WHERE id = ? AND state IN ('pending', 'provisioning', 'discord_created')""",
            (role_id, utc_now(), intent_id),
        )
        await db.commit()
    return cursor.rowcount == 1


async def mark_shop_intent_provisioning(intent_id: int) -> bool:
    async with connection() as db:
        cursor = await db.execute(
            """UPDATE shop_purchase_intents
               SET state = 'provisioning', updated_at = ?, last_error = NULL
               WHERE id = ? AND state = 'pending'""",
            (utc_now(), intent_id),
        )
        await db.commit()
    return cursor.rowcount == 1


async def complete_shop_purchase(intent_id: int, role_id: int) -> bool:
    """Finalize the durable intent without charging a second time."""

    async with connection() as db:
        try:
            await begin_write(db, "shop-intent", intent_id)
            rows = await db.execute_fetchall(
                f"SELECT {_INTENT_COLUMNS} FROM shop_purchase_intents WHERE id = ?",
                (intent_id,),
            )
            if not rows:
                await db.rollback()
                return False
            intent = _intent(rows[0])
            if intent.state == "completed":
                await db.commit()
                return True
            if intent.state == "refunded":
                await db.rollback()
                return False
            now = utc_now()
            if intent.purchase_kind == "color":
                await db.execute(
                    """INSERT INTO purchased_roles
                       (user_id, role_id, role_name, price, role_kind, purchased_at)
                       VALUES (?, ?, ?, ?, 'color', ?)""",
                    (intent.user_id, role_id, intent.role_name, intent.price, now),
                )
            else:
                if intent.vip_days is None or intent.vip_days < 1:
                    raise RuntimeError("VIP intent has no duration")
                account = await db.execute_fetchall(
                    "SELECT vip_until FROM coin_accounts WHERE user_id = ?",
                    (intent.user_id,),
                )
                now_dt = datetime.now(UTC)
                current = (
                    datetime.fromisoformat(str(account[0][0]))
                    if account and account[0][0]
                    else now_dt
                )
                final_vip_until = (
                    max(now_dt, current) + timedelta(days=intent.vip_days)
                ).isoformat()
                await db.execute(
                    "UPDATE coin_accounts SET vip_until = ? WHERE user_id = ?",
                    (final_vip_until, intent.user_id),
                )
                await db.execute(
                    "UPDATE shop_purchase_intents SET vip_until = ? WHERE id = ?",
                    (final_vip_until, intent.id),
                )
            await db.execute(
                """UPDATE shop_purchase_intents
                   SET discord_role_id = ?, state = 'completed', completed_at = ?,
                       updated_at = ?, last_error = NULL WHERE id = ?""",
                (role_id, now, now, intent_id),
            )
            await db.commit()
            return True
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception:
            await db.rollback()
            raise


async def get_reusable_color_role_ids(
    guild_id: int,
    primary_color: int,
) -> list[int]:
    """Return bot-provisioned colour roles that still have an active holder."""

    async with connection() as db:
        rows = await db.execute_fetchall(
            """SELECT DISTINCT i.discord_role_id
               FROM shop_purchase_intents AS i
               JOIN purchased_roles AS p
                 ON p.role_id = i.discord_role_id
                AND p.active IN (1, 2)
               WHERE i.guild_id = ?
                 AND i.purchase_kind = 'color'
                 AND i.primary_color = ?
                 AND i.state = 'completed'
                 AND i.discord_role_id IS NOT NULL
               ORDER BY i.id""",
            (guild_id, primary_color),
        )
    return [int(row[0]) for row in rows]


async def count_active_role_holders(
    role_id: int,
    *,
    exclude_purchase_id: int | None = None,
) -> int:
    where = "role_id = ? AND active IN (1, 2)"
    params: list[int] = [role_id]
    if exclude_purchase_id is not None:
        where += " AND id != ?"
        params.append(exclude_purchase_id)
    async with connection() as db:
        rows = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM purchased_roles WHERE {where}",
            tuple(params),
        )
    return int(rows[0][0]) if rows else 0


async def refund_shop_purchase(intent_id: int, action: str) -> bool:
    """Refund a non-completed intent exactly once."""

    async with connection() as db:
        try:
            await begin_write(db, "shop-intent", intent_id)
            rows = await db.execute_fetchall(
                """SELECT user_id, price, state FROM shop_purchase_intents
                   WHERE id = ?""",
                (intent_id,),
            )
            if not rows or rows[0][2] in {"completed", "refunded"}:
                await db.rollback()
                return False
            user_id, price, _ = rows[0]
            now = utc_now()
            await db.execute(
                "UPDATE coin_accounts SET balance = balance + ? WHERE user_id = ?",
                (price, user_id),
            )
            await db.execute(
                """INSERT INTO coin_transactions
                   (user_id, amount, action, created_at) VALUES (?, ?, ?, ?)""",
                (user_id, price, action, now),
            )
            await db.execute(
                """UPDATE shop_purchase_intents
                   SET state = 'refunded', completed_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now, now, intent_id),
            )
            await db.commit()
            return True
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception:
            await db.rollback()
            raise


async def set_shop_purchase_error(intent_id: int, error: BaseException) -> None:
    async with connection() as db:
        await db.execute(
            """UPDATE shop_purchase_intents SET last_error = ?, updated_at = ?
               WHERE id = ? AND state IN ('pending', 'provisioning', 'discord_created')""",
            (f"{type(error).__name__}: {error}"[:1_000], utc_now(), intent_id),
        )
        await db.commit()
