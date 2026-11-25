"""Family accounts, immutable ledger, payout obligations and game reconciliations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.database import (
    DATABASE_INTEGRITY_ERRORS,
    connection as _connect,
    utc_now,
)
from infrastructure.transactions import begin_write

CURRENCIES = (
    "game_money",
    "guinn_coins",
    "contribution",
    "reputation",
    "influence",
    "dark_coins",
)
CONTEXTS = ("memphis", "milton")
HOLDER_KINDS = ("family", "member")
LEDGER_OPS = ("credit", "debit", "reverse")
OBLIGATION_STATUSES = ("promised", "approved", "paid", "cancelled")

_ACCOUNT_COLUMNS = (
    "id, guild_id, family_id, currency, name, holder_kind, holder_id, "
    "responsible_id, context, archived, version, created_at"
)
_LEDGER_COLUMNS = (
    "id, guild_id, family_id, account_id, operation, amount, basis_kind, "
    "basis_id, actor_id, created_at, source_ref, reversal_of"
)
_OBLIGATION_COLUMNS = (
    "id, guild_id, family_id, basis_kind, basis_id, payee_id, currency, "
    "amount, terms_version, status, promised_by, approved_by, "
    "second_approved_by, paid_by, confirmed_by, proof_ref, batch_id, "
    "version, created_at"
)
_RECON_COLUMNS = (
    "id, guild_id, family_id, account_id, game_balance, book_balance, "
    "delta, explanation, actor_id, created_at"
)


@dataclass(frozen=True, slots=True)
class Account:
    id: int
    guild_id: int
    family_id: int
    currency: str
    name: str
    holder_kind: str
    holder_id: int
    responsible_id: int | None
    context: str
    archived: bool
    version: int
    created_at: str


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    id: int
    guild_id: int
    family_id: int
    account_id: int
    operation: str
    amount: int
    basis_kind: str
    basis_id: str
    actor_id: int
    created_at: str
    source_ref: str | None
    reversal_of: int | None


@dataclass(frozen=True, slots=True)
class Obligation:
    id: int
    guild_id: int
    family_id: int
    basis_kind: str
    basis_id: str
    payee_id: int
    currency: str
    amount: int | None
    terms_version: str | None
    status: str
    promised_by: int | None
    approved_by: int | None
    second_approved_by: int | None
    paid_by: int | None
    confirmed_by: int | None
    proof_ref: str | None
    batch_id: str | None
    version: int
    created_at: str


@dataclass(frozen=True, slots=True)
class Reconciliation:
    id: int
    guild_id: int
    family_id: int
    account_id: int
    game_balance: int
    book_balance: int
    delta: int
    explanation: str
    actor_id: int
    created_at: str


def _inserted_id(cursor: Any) -> int:
    row_id = int(cursor.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("insert did not return an id")
    return row_id


def _to_account(row: Any) -> Account:
    return Account(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        currency=str(row[3]),
        name=str(row[4]),
        holder_kind=str(row[5]),
        holder_id=int(row[6]),
        responsible_id=int(row[7]) if row[7] is not None else None,
        context=str(row[8]),
        archived=bool(int(row[9])),
        version=int(row[10]),
        created_at=str(row[11]),
    )


def _to_ledger(row: Any) -> LedgerEntry:
    return LedgerEntry(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        account_id=int(row[3]),
        operation=str(row[4]),
        amount=int(row[5]),
        basis_kind=str(row[6]),
        basis_id=str(row[7]),
        actor_id=int(row[8]),
        created_at=str(row[9]),
        source_ref=str(row[10]) if row[10] is not None else None,
        reversal_of=int(row[11]) if row[11] is not None else None,
    )


def _to_obligation(row: Any) -> Obligation:
    return Obligation(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        basis_kind=str(row[3]),
        basis_id=str(row[4]),
        payee_id=int(row[5]),
        currency=str(row[6]),
        amount=int(row[7]) if row[7] is not None else None,
        terms_version=str(row[8]) if row[8] is not None else None,
        status=str(row[9]),
        promised_by=int(row[10]) if row[10] is not None else None,
        approved_by=int(row[11]) if row[11] is not None else None,
        second_approved_by=int(row[12]) if row[12] is not None else None,
        paid_by=int(row[13]) if row[13] is not None else None,
        confirmed_by=int(row[14]) if row[14] is not None else None,
        proof_ref=str(row[15]) if row[15] is not None else None,
        batch_id=str(row[16]) if row[16] is not None else None,
        version=int(row[17]),
        created_at=str(row[18]),
    )


def _to_recon(row: Any) -> Reconciliation:
    return Reconciliation(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        account_id=int(row[3]),
        game_balance=int(row[4]),
        book_balance=int(row[5]),
        delta=int(row[6]),
        explanation=str(row[7]),
        actor_id=int(row[8]),
        created_at=str(row[9]),
    )


async def get_account(account_id: int) -> Account | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE id = ?",
            (account_id,),
        )
    return _to_account(rows[0]) if rows else None


async def find_account(
    *,
    family_id: int,
    currency: str,
    holder_kind: str = "family",
    holder_id: int = 0,
    context: str = "memphis",
) -> Account | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_ACCOUNT_COLUMNS} FROM accounts
                WHERE family_id = ? AND currency = ? AND holder_kind = ?
                  AND holder_id = ? AND context = ?""",
            (family_id, currency, holder_kind, holder_id, context),
        )
    return _to_account(rows[0]) if rows else None


async def list_accounts(
    family_id: int, *, include_archived: bool = False
) -> list[Account]:
    where = "family_id = ?"
    params: list[Any] = [family_id]
    if not include_archived:
        where += " AND archived = 0"
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_ACCOUNT_COLUMNS} FROM accounts
                WHERE {where}
                ORDER BY context, currency, id""",
            tuple(params),
        )
    return [_to_account(row) for row in rows]


async def upsert_account(
    *,
    guild_id: int,
    family_id: int,
    currency: str,
    name: str,
    holder_kind: str = "family",
    holder_id: int = 0,
    responsible_id: int | None = None,
    context: str = "memphis",
    archived: bool = False,
) -> Account:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "account-upsert", f"{family_id}:{currency}:{context}")
        try:
            await db.execute(
                """INSERT OR IGNORE INTO accounts (
                       guild_id, family_id, currency, name, holder_kind,
                       holder_id, responsible_id, context, archived, version,
                       created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    guild_id,
                    family_id,
                    currency,
                    name,
                    holder_kind,
                    holder_id,
                    responsible_id,
                    context,
                    1 if archived else 0,
                    stamp,
                ),
            )
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
    found = await find_account(
        family_id=family_id,
        currency=currency,
        holder_kind=holder_kind,
        holder_id=holder_id,
        context=context,
    )
    if found is None:
        raise RuntimeError("account upsert did not persist")
    return found


def _require_positive_int(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


async def book_balance(account_id: int) -> int:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id, operation, amount, reversal_of FROM ledger_entries
               WHERE account_id = ?
               ORDER BY id""",
            (account_id,),
        )
    originals = {int(row[0]): str(row[1]) for row in rows}
    total = 0
    for _entry_id, operation, amount, reversal_of in rows:
        value = int(amount)
        if operation == "credit":
            total += value
        elif operation == "debit":
            total -= value
        elif operation == "reverse":
            source = originals.get(
                int(reversal_of) if reversal_of is not None else 0
            )
            if source == "debit":
                total += value
            else:
                total -= value
    return total


async def insert_ledger_entry(
    *,
    guild_id: int,
    family_id: int,
    account_id: int,
    operation: str,
    amount: int,
    basis_kind: str,
    basis_id: str,
    actor_id: int,
    source_ref: str | None = None,
    reversal_of: int | None = None,
) -> LedgerEntry:
    amount = _require_positive_int(amount, label="ledger amount")
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "ledger", f"{account_id}:{basis_kind}:{basis_id}:{operation}")
        existing = await db.execute_fetchall(
            f"""SELECT {_LEDGER_COLUMNS} FROM ledger_entries
                WHERE account_id = ? AND basis_kind = ? AND basis_id = ?
                  AND operation = ?""",
            (account_id, basis_kind, basis_id, operation),
        )
        if existing:
            await db.commit()
            return _to_ledger(existing[0])
        try:
            cursor = await db.execute(
                """INSERT INTO ledger_entries (
                       guild_id, family_id, account_id, operation, amount,
                       basis_kind, basis_id, actor_id, created_at, source_ref,
                       reversal_of
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    account_id,
                    operation,
                    amount,
                    basis_kind,
                    basis_id,
                    actor_id,
                    stamp,
                    source_ref,
                    reversal_of,
                ),
            )
            entry_id = _inserted_id(cursor)
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            loaded = await db.execute_fetchall(
                f"""SELECT {_LEDGER_COLUMNS} FROM ledger_entries
                    WHERE account_id = ? AND basis_kind = ? AND basis_id = ?
                      AND operation = ?""",
                (account_id, basis_kind, basis_id, operation),
            )
            if loaded:
                return _to_ledger(loaded[0])
            raise
    rows = await _connect_fetch_ledger(entry_id)
    return rows


async def _connect_fetch_ledger(entry_id: int) -> LedgerEntry:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_LEDGER_COLUMNS} FROM ledger_entries WHERE id = ?",
            (entry_id,),
        )
    if not rows:
        raise RuntimeError("ledger insert did not persist")
    return _to_ledger(rows[0])


async def get_ledger_entry(entry_id: int) -> LedgerEntry | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_LEDGER_COLUMNS} FROM ledger_entries WHERE id = ?",
            (entry_id,),
        )
    return _to_ledger(rows[0]) if rows else None


async def list_ledger(account_id: int, *, limit: int = 20) -> list[LedgerEntry]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_LEDGER_COLUMNS} FROM ledger_entries
                WHERE account_id = ?
                ORDER BY id DESC LIMIT ?""",
            (account_id, max(1, min(int(limit), 100))),
        )
    return [_to_ledger(row) for row in rows]


async def get_obligation(obligation_id: int) -> Obligation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_OBLIGATION_COLUMNS} FROM obligations WHERE id = ?",
            (obligation_id,),
        )
    return _to_obligation(rows[0]) if rows else None


async def find_obligation(
    *, family_id: int, basis_kind: str, basis_id: str
) -> Obligation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_OBLIGATION_COLUMNS} FROM obligations
                WHERE family_id = ? AND basis_kind = ? AND basis_id = ?""",
            (family_id, basis_kind, basis_id),
        )
    return _to_obligation(rows[0]) if rows else None


async def list_obligations(
    family_id: int, *, status: str | None = None
) -> list[Obligation]:
    where = "family_id = ?"
    params: list[Any] = [family_id]
    if status:
        where += " AND status = ?"
        params.append(status)
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_OBLIGATION_COLUMNS} FROM obligations
                WHERE {where}
                ORDER BY id""",
            tuple(params),
        )
    return [_to_obligation(row) for row in rows]


async def insert_obligation(
    *,
    guild_id: int,
    family_id: int,
    basis_kind: str,
    basis_id: str,
    payee_id: int,
    currency: str,
    amount: int | None,
    terms_version: str | None,
    promised_by: int,
) -> Obligation:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "obligation", f"{family_id}:{basis_kind}:{basis_id}")
        existing = await db.execute_fetchall(
            f"""SELECT {_OBLIGATION_COLUMNS} FROM obligations
                WHERE family_id = ? AND basis_kind = ? AND basis_id = ?""",
            (family_id, basis_kind, basis_id),
        )
        if existing:
            await db.commit()
            return _to_obligation(existing[0])
        try:
            cursor = await db.execute(
                """INSERT INTO obligations (
                       guild_id, family_id, basis_kind, basis_id, payee_id,
                       currency, amount, terms_version, status, promised_by,
                       version, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'promised', ?, 1, ?)""",
                (
                    guild_id,
                    family_id,
                    basis_kind,
                    basis_id,
                    payee_id,
                    currency,
                    amount,
                    terms_version,
                    promised_by,
                    stamp,
                ),
            )
            obligation_id = _inserted_id(cursor)
            await db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            await db.rollback()
            loaded = await find_obligation(
                family_id=family_id, basis_kind=basis_kind, basis_id=basis_id
            )
            if loaded is None:
                raise
            return loaded
    found = await get_obligation(obligation_id)
    if found is None:
        raise RuntimeError("obligation insert did not persist")
    return found


async def update_obligation(
    obligation_id: int,
    *,
    expected_version: int,
    status: str | None = None,
    amount: int | None = None,
    approved_by: int | None = None,
    second_approved_by: int | None = None,
    paid_by: int | None = None,
    confirmed_by: int | None = None,
    proof_ref: str | None = None,
    batch_id: str | None = None,
) -> Obligation | None:
    async with _connect() as db:
        await begin_write(db, "obligation-update", obligation_id)
        rows = await db.execute_fetchall(
            f"SELECT {_OBLIGATION_COLUMNS} FROM obligations WHERE id = ?",
            (obligation_id,),
        )
        if not rows:
            await db.commit()
            return None
        current = _to_obligation(rows[0])
        if current.version != expected_version:
            await db.commit()
            return None
        fields = ["version = version + 1"]
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if amount is not None:
            fields.append("amount = ?")
            params.append(amount)
        if approved_by is not None:
            fields.append("approved_by = ?")
            params.append(approved_by)
        if second_approved_by is not None:
            fields.append("second_approved_by = ?")
            params.append(second_approved_by)
        if paid_by is not None:
            fields.append("paid_by = ?")
            params.append(paid_by)
        if confirmed_by is not None:
            fields.append("confirmed_by = ?")
            params.append(confirmed_by)
        if proof_ref is not None:
            fields.append("proof_ref = ?")
            params.append(proof_ref)
        if batch_id is not None:
            fields.append("batch_id = ?")
            params.append(batch_id)
        params.extend((obligation_id, expected_version))
        cursor = await db.execute(
            f"""UPDATE obligations SET {', '.join(fields)}
                WHERE id = ? AND version = ?""",
            tuple(params),
        )
        await db.commit()
        if int(cursor.rowcount or 0) <= 0:
            return None
    return await get_obligation(obligation_id)


async def insert_reconciliation(
    *,
    guild_id: int,
    family_id: int,
    account_id: int,
    game_balance: int,
    book_balance: int,
    explanation: str,
    actor_id: int,
) -> Reconciliation:
    stamp = utc_now()
    delta = game_balance - book_balance
    async with _connect() as db:
        await begin_write(db, "reconcile", account_id)
        cursor = await db.execute(
            """INSERT INTO account_reconciliations (
                   guild_id, family_id, account_id, game_balance, book_balance,
                   delta, explanation, actor_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                family_id,
                account_id,
                game_balance,
                book_balance,
                delta,
                explanation,
                actor_id,
                stamp,
            ),
        )
        recon_id = _inserted_id(cursor)
        await db.commit()
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_RECON_COLUMNS} FROM account_reconciliations WHERE id = ?",
            (recon_id,),
        )
    if not rows:
        raise RuntimeError("reconciliation insert did not persist")
    return _to_recon(rows[0])


async def latest_reconciliation(account_id: int) -> Reconciliation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_RECON_COLUMNS} FROM account_reconciliations
                WHERE account_id = ?
                ORDER BY id DESC LIMIT 1""",
            (account_id,),
        )
    return _to_recon(rows[0]) if rows else None
