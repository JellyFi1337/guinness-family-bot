from __future__ import annotations

import json

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write
from utils.validation import normalize_positive_amount, normalize_treasury_action


async def _one(db, sql: str, params: tuple = ()):
    rows = await db.execute_fetchall(sql, params)
    return rows[0] if rows else None


async def _enqueue_publication(
    db,
    *,
    guild_id: int,
    transaction_number: int,
    payload: dict,
) -> None:
    now = _utc_now()
    await db.execute(
        """INSERT OR IGNORE INTO operation_outbox
           (idempotency_key, guild_id, kind, aggregate_id, payload_json,
            available_at, created_at, updated_at)
           VALUES (?, ?, 'treasury.publish', ?, ?, ?, ?, ?)""",
        (
            f"treasury:{guild_id}:{transaction_number}:publish",
            guild_id,
            str(transaction_number),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            now,
            now,
            now,
        ),
    )


async def get_treasury_balance() -> int:
    async with _connect() as db:
        row = await _one(db, "SELECT balance FROM treasury WHERE id = 1")
    return int(row[0]) if row else 0


async def set_treasury_balance(amount: int) -> None:
    amount = int(amount)
    if amount < 0:
        raise ValueError("Баланс казны не может быть отрицательным")
    async with _connect() as db:
        await db.execute(
            """INSERT INTO treasury (id, balance) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET balance = excluded.balance""",
            (amount,),
        )
        await db.commit()


async def set_treasury_balance_with_transaction(
    user_id: int,
    username: str,
    amount: int,
    *,
    guild_id: int | None = None,
    interaction_id: int | None = None,
) -> tuple[int | None, int, int]:
    amount = int(amount)
    if amount < 0:
        raise ValueError("Баланс казны не может быть отрицательным")
    receipt_kind = "treasury.set"
    async with _connect() as db:
        await begin_write(db, "treasury", guild_id or 0)
        if guild_id is not None and interaction_id is not None:
            existing = await _one(
                db,
                """SELECT state, result_ref FROM command_receipts
                   WHERE guild_id = ? AND interaction_id = ? AND kind = ?""",
                (guild_id, interaction_id, receipt_kind),
            )
            if existing and existing[0] == "completed" and existing[1]:
                await db.commit()
                saved = json.loads(existing[1])
                return saved[0], saved[1], saved[2]
            now = _utc_now()
            await db.execute(
                """INSERT OR IGNORE INTO command_receipts
                   (guild_id, interaction_id, kind, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'processing', ?, ?)""",
                (guild_id, interaction_id, receipt_kind, now, now),
            )
        await db.execute("INSERT OR IGNORE INTO treasury (id, balance) VALUES (1, 0)")
        await db.execute(
            "INSERT OR IGNORE INTO treasury_counter (id, count) VALUES (1, 0)"
        )
        old_amount = int((await _one(db, "SELECT balance FROM treasury WHERE id = 1"))[0])
        difference = amount - old_amount
        if difference == 0:
            if guild_id is not None and interaction_id is not None:
                await db.execute(
                    """UPDATE command_receipts SET state = 'completed',
                              result_ref = ?, updated_at = ?
                       WHERE guild_id = ? AND interaction_id = ? AND kind = ?""",
                    (
                        json.dumps([None, old_amount, 0]),
                        _utc_now(),
                        guild_id,
                        interaction_id,
                        receipt_kind,
                    ),
                )
            await db.commit()
            return None, old_amount, 0

        await db.execute("UPDATE treasury_counter SET count = count + 1 WHERE id = 1")
        transaction_number = int(
            (await _one(db, "SELECT count FROM treasury_counter WHERE id = 1"))[0]
        )
        await db.execute("UPDATE treasury SET balance = ? WHERE id = 1", (amount,))
        await db.execute(
            """INSERT INTO treasury_transactions
               (user_id, username, amount, action, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                username,
                difference,
                "add" if difference > 0 else "remove",
                f"Установка значения казны: {amount}",
                _utc_now(),
            ),
        )
        if guild_id is not None:
            await _enqueue_publication(
                db,
                guild_id=guild_id,
                transaction_number=transaction_number,
                payload={
                    "transaction_number": transaction_number,
                    "kind": "set",
                    "user_id": user_id,
                    "username": username,
                    "old_amount": old_amount,
                    "new_amount": amount,
                    "difference": difference,
                },
            )
        if guild_id is not None and interaction_id is not None:
            await db.execute(
                """UPDATE command_receipts SET state = 'completed', result_ref = ?,
                          updated_at = ?
                   WHERE guild_id = ? AND interaction_id = ? AND kind = ?""",
                (
                    json.dumps([transaction_number, old_amount, difference]),
                    _utc_now(),
                    guild_id,
                    interaction_id,
                    receipt_kind,
                ),
            )
        await db.commit()
    return transaction_number, old_amount, difference


async def add_treasury_transaction(
    user_id: int,
    username: str,
    amount: int,
    action: str,
    reason: str,
    *,
    guild_id: int | None = None,
    interaction_id: int | None = None,
) -> int:
    action = normalize_treasury_action(action)
    unsigned_amount = normalize_positive_amount(abs(amount))
    signed_amount = unsigned_amount if action == "add" else -unsigned_amount
    reason = reason.strip()
    if not reason:
        raise ValueError("Причина не может быть пустой")
    receipt_kind = "treasury.transaction"

    async with _connect() as db:
        await begin_write(db, "treasury", guild_id or 0)
        if guild_id is not None and interaction_id is not None:
            existing = await _one(
                db,
                """SELECT state, result_ref FROM command_receipts
                   WHERE guild_id = ? AND interaction_id = ? AND kind = ?""",
                (guild_id, interaction_id, receipt_kind),
            )
            if existing and existing[0] == "completed" and existing[1]:
                await db.commit()
                return int(existing[1])
            now = _utc_now()
            await db.execute(
                """INSERT OR IGNORE INTO command_receipts
                   (guild_id, interaction_id, kind, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'processing', ?, ?)""",
                (guild_id, interaction_id, receipt_kind, now, now),
            )
        await db.execute("INSERT OR IGNORE INTO treasury (id, balance) VALUES (1, 0)")
        await db.execute(
            "INSERT OR IGNORE INTO treasury_counter (id, count) VALUES (1, 0)"
        )
        if signed_amount < 0:
            balance = int((await _one(db, "SELECT balance FROM treasury WHERE id = 1"))[0])
            if balance + signed_amount < 0:
                await db.rollback()
                raise ValueError("Недостаточно средств в казне")
        await db.execute("UPDATE treasury_counter SET count = count + 1 WHERE id = 1")
        transaction_number = int(
            (await _one(db, "SELECT count FROM treasury_counter WHERE id = 1"))[0]
        )
        await db.execute(
            "UPDATE treasury SET balance = balance + ? WHERE id = 1",
            (signed_amount,),
        )
        await db.execute(
            """INSERT INTO treasury_transactions
               (user_id, username, amount, action, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, username, signed_amount, action, reason, _utc_now()),
        )
        if guild_id is not None:
            await _enqueue_publication(
                db,
                guild_id=guild_id,
                transaction_number=transaction_number,
                payload={
                    "transaction_number": transaction_number,
                    "kind": "transaction",
                    "user_id": user_id,
                    "username": username,
                    "amount": unsigned_amount,
                    "action": action,
                    "reason": reason,
                },
            )
        if guild_id is not None and interaction_id is not None:
            await db.execute(
                """UPDATE command_receipts SET state = 'completed', result_ref = ?,
                          updated_at = ?
                   WHERE guild_id = ? AND interaction_id = ? AND kind = ?""",
                (
                    str(transaction_number),
                    _utc_now(),
                    guild_id,
                    interaction_id,
                    receipt_kind,
                ),
            )
        await db.commit()
    return transaction_number


async def get_last_transactions(limit: int = 10):
    limit = max(1, min(int(limit), 100))
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT user_id, username, amount, action, reason, created_at
               FROM treasury_transactions ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
