"""Contract reports, evidence hashes and payout obligations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from infrastructure.database import DATABASE_INTEGRITY_ERRORS, connection as _connect, utc_now
from infrastructure.transactions import begin_write

REPORT_STATUSES = frozenset(
    {"submitted", "accepted", "rework", "rejected", "reversed"}
)
OBLIGATION_STATUSES = frozenset({"open", "cancelled"})

_REPORT_COLUMNS = (
    "id, guild_id, family_id, run_id, attempt, idempotency_key, "
    "result_amount, result_note, volumes_json, duration_minutes, "
    "wants_payout, disputed, detailed, status, submitted_by, submitted_at, "
    "reviewed_by, reviewed_at, review_reason, version, "
    "cash_amount, reputation_delta, has_profit"
)


@dataclass(frozen=True, slots=True)
class ReportShare:
    discord_id: int
    amount: int


@dataclass(frozen=True, slots=True)
class ReportEvidence:
    id: int
    kind: str
    url: str | None
    content_hash: str
    byte_size: int
    duplicate_report_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PayoutObligation:
    id: int
    report_id: int
    run_id: int
    amount: int
    status: str


@dataclass(frozen=True, slots=True)
class ReportReversal:
    id: int
    report_id: int
    actor_id: int
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ReportPurchase:
    item_name: str
    quantity: int
    unit_price: int


@dataclass(frozen=True, slots=True)
class ReportExpense:
    label: str
    amount: int


@dataclass(frozen=True, slots=True)
class ReportItemPayout:
    item_name: str
    quantity: int
    valuation: int


@dataclass(frozen=True, slots=True)
class ContractReport:
    id: int
    guild_id: int
    family_id: int
    run_id: int
    attempt: int
    idempotency_key: str
    result_amount: int
    result_note: str | None
    volumes: tuple[str, ...]
    duration_minutes: int
    wants_payout: bool
    disputed: bool
    detailed: bool
    status: str
    submitted_by: int
    submitted_at: str
    reviewed_by: int | None
    reviewed_at: str | None
    review_reason: str | None
    version: int
    cash_amount: int | None
    reputation_delta: int | None
    has_profit: bool
    shares: tuple[ReportShare, ...]
    evidence: tuple[ReportEvidence, ...]
    purchases: tuple[ReportPurchase, ...]
    expenses: tuple[ReportExpense, ...]
    item_payouts: tuple[ReportItemPayout, ...]
    obligation: PayoutObligation | None
    reversals: tuple[ReportReversal, ...]


def _inserted_id(cursor: Any) -> int:
    row_id = int(cursor.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("insert did not return an id")
    return row_id


def _parse_volumes(raw: object) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw if str(item).strip())
    parsed = json.loads(str(raw))
    if isinstance(parsed, list):
        return tuple(str(item) for item in parsed if str(item).strip())
    return ()


def _to_share(row: Any) -> ReportShare:
    return ReportShare(discord_id=int(row[0]), amount=int(row[1]))


def _to_reversal(row: Any) -> ReportReversal:
    return ReportReversal(
        id=int(row[0]),
        report_id=int(row[1]),
        actor_id=int(row[2]),
        reason=str(row[3]),
        created_at=str(row[4]),
    )


def _to_obligation(row: Any) -> PayoutObligation:
    return PayoutObligation(
        id=int(row[0]),
        report_id=int(row[1]),
        run_id=int(row[2]),
        amount=int(row[3]),
        status=str(row[4]),
    )


async def _shares_for(db: Any, report_id: int) -> tuple[ReportShare, ...]:
    rows = await db.execute_fetchall(
        """SELECT discord_id, amount FROM contract_report_shares
           WHERE report_id = ? ORDER BY discord_id""",
        (report_id,),
    )
    return tuple(_to_share(row) for row in rows)


async def _evidence_for(db: Any, report_id: int, guild_id: int) -> tuple[ReportEvidence, ...]:
    rows = await db.execute_fetchall(
        """SELECT id, kind, url, content_hash, byte_size
           FROM contract_report_evidence
           WHERE report_id = ? ORDER BY id""",
        (report_id,),
    )
    items: list[ReportEvidence] = []
    for row in rows:
        others = await db.execute_fetchall(
            """SELECT DISTINCT report_id FROM contract_report_evidence
               WHERE guild_id = ? AND content_hash = ? AND report_id != ?""",
            (guild_id, str(row[3]), report_id),
        )
        items.append(
            ReportEvidence(
                id=int(row[0]),
                kind=str(row[1]),
                url=str(row[2]) if row[2] else None,
                content_hash=str(row[3]),
                byte_size=int(row[4]),
                duplicate_report_ids=tuple(int(item[0]) for item in others),
            )
        )
    return tuple(items)


async def _obligation_for(db: Any, report_id: int) -> PayoutObligation | None:
    rows = await db.execute_fetchall(
        """SELECT id, report_id, run_id, amount, status
           FROM report_payout_obligations WHERE report_id = ?""",
        (report_id,),
    )
    return _to_obligation(rows[0]) if rows else None


async def _reversals_for(db: Any, report_id: int) -> tuple[ReportReversal, ...]:
    rows = await db.execute_fetchall(
        """SELECT id, report_id, actor_id, reason, created_at
           FROM contract_report_reversals
           WHERE report_id = ? ORDER BY id""",
        (report_id,),
    )
    return tuple(_to_reversal(row) for row in rows)


async def _purchases_for(db: Any, report_id: int) -> tuple[ReportPurchase, ...]:
    rows = await db.execute_fetchall(
        """SELECT item_name, quantity, unit_price
           FROM contract_report_purchases
           WHERE report_id = ? ORDER BY id""",
        (report_id,),
    )
    return tuple(
        ReportPurchase(item_name=str(row[0]), quantity=int(row[1]), unit_price=int(row[2]))
        for row in rows
    )


async def _expenses_for(db: Any, report_id: int) -> tuple[ReportExpense, ...]:
    rows = await db.execute_fetchall(
        """SELECT label, amount FROM contract_report_expenses
           WHERE report_id = ? ORDER BY id""",
        (report_id,),
    )
    return tuple(ReportExpense(label=str(row[0]), amount=int(row[1])) for row in rows)


async def _item_payouts_for(db: Any, report_id: int) -> tuple[ReportItemPayout, ...]:
    rows = await db.execute_fetchall(
        """SELECT item_name, quantity, valuation
           FROM contract_report_item_payouts
           WHERE report_id = ? ORDER BY id""",
        (report_id,),
    )
    return tuple(
        ReportItemPayout(
            item_name=str(row[0]), quantity=int(row[1]), valuation=int(row[2])
        )
        for row in rows
    )


def _to_report(
    row: Any,
    shares: tuple[ReportShare, ...],
    evidence: tuple[ReportEvidence, ...],
    obligation: PayoutObligation | None,
    reversals: tuple[ReportReversal, ...],
    purchases: tuple[ReportPurchase, ...],
    expenses: tuple[ReportExpense, ...],
    item_payouts: tuple[ReportItemPayout, ...],
) -> ContractReport:
    return ContractReport(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        run_id=int(row[3]),
        attempt=int(row[4]),
        idempotency_key=str(row[5]),
        result_amount=int(row[6]),
        result_note=str(row[7]) if row[7] else None,
        volumes=_parse_volumes(row[8]),
        duration_minutes=int(row[9]),
        wants_payout=bool(int(row[10])),
        disputed=bool(int(row[11])),
        detailed=bool(int(row[12])),
        status=str(row[13]),
        submitted_by=int(row[14]),
        submitted_at=str(row[15]),
        reviewed_by=int(row[16]) if row[16] is not None else None,
        reviewed_at=str(row[17]) if row[17] else None,
        review_reason=str(row[18]) if row[18] else None,
        version=int(row[19]),
        cash_amount=int(row[20]) if row[20] is not None else None,
        reputation_delta=int(row[21]) if row[21] is not None else None,
        has_profit=bool(int(row[22] or 0)),
        shares=shares,
        evidence=evidence,
        purchases=purchases,
        expenses=expenses,
        item_payouts=item_payouts,
        obligation=obligation,
        reversals=reversals,
    )


async def _load(db: Any, report_id: int) -> ContractReport | None:
    rows = await db.execute_fetchall(
        f"SELECT {_REPORT_COLUMNS} FROM contract_reports WHERE id = ?",
        (report_id,),
    )
    if not rows:
        return None
    row = rows[0]
    guild_id = int(row[1])
    return _to_report(
        row,
        await _shares_for(db, report_id),
        await _evidence_for(db, report_id, guild_id),
        await _obligation_for(db, report_id),
        await _reversals_for(db, report_id),
        await _purchases_for(db, report_id),
        await _expenses_for(db, report_id),
        await _item_payouts_for(db, report_id),
    )


async def get_report(report_id: int) -> ContractReport | None:
    async with _connect() as db:
        return await _load(db, report_id)


async def latest_report_for_run(run_id: int) -> ContractReport | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_REPORT_COLUMNS} FROM contract_reports
                WHERE run_id = ? ORDER BY attempt DESC, id DESC LIMIT 1""",
            (run_id,),
        )
        if not rows:
            return None
        return await _load(db, int(rows[0][0]))


async def list_submitted_report_refs(
    family_id: int,
) -> list[tuple[int, int, str]]:
    """Live submitted reports: id, author, submitted_at. No queue copy."""

    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id, submitted_by, submitted_at FROM contract_reports
                WHERE family_id = ? AND status = 'submitted'
                ORDER BY submitted_at, id""",
            (family_id,),
        )
    return [(int(row[0]), int(row[1]), str(row[2])) for row in rows]


async def list_open_reports_for_member(
    family_id: int, discord_id: int
) -> list[ContractReport]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_REPORT_COLUMNS} FROM contract_reports
                WHERE family_id = ? AND submitted_by = ?
                  AND status IN ('submitted', 'rework')
                ORDER BY submitted_at, id""",
            (family_id, discord_id),
        )
        items: list[ContractReport] = []
        for row in rows:
            loaded = await _load(db, int(row[0]))
            if loaded is not None:
                items.append(loaded)
    return items


async def insert_submitted_report(
    *,
    guild_id: int,
    family_id: int,
    run_id: int,
    attempt: int,
    result_amount: int,
    result_note: str | None,
    volumes: tuple[str, ...],
    duration_minutes: int,
    wants_payout: bool,
    disputed: bool,
    detailed: bool,
    submitted_by: int,
    shares: tuple[ReportShare, ...],
    evidence: tuple[tuple[str, str | None, str, int], ...],
    run_status: str,
    purchases: tuple[ReportPurchase, ...] = (),
    expenses: tuple[ReportExpense, ...] = (),
    item_payouts: tuple[ReportItemPayout, ...] = (),
    cash_amount: int | None = None,
    reputation_delta: int | None = None,
    has_profit: bool = False,
) -> ContractReport:
    stamp = utc_now()
    key = f"run:{run_id}:attempt:{attempt}"
    conflict: BaseException | None = None
    report_id = 0
    async with _connect() as db:
        await begin_write(db, "contract-report-submit", run_id)
        try:
            cursor = await db.execute(
                """INSERT INTO contract_reports (
                       guild_id, family_id, run_id, attempt, idempotency_key,
                       result_amount, result_note, volumes_json, duration_minutes,
                       wants_payout, disputed, detailed, status, submitted_by,
                       submitted_at, version, cash_amount, reputation_delta,
                       has_profit
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted',
                             ?, ?, 1, ?, ?, ?)""",
                (
                    guild_id,
                    family_id,
                    run_id,
                    attempt,
                    key,
                    result_amount,
                    result_note,
                    json.dumps(list(volumes), ensure_ascii=False),
                    duration_minutes,
                    1 if wants_payout else 0,
                    1 if disputed else 0,
                    1 if detailed else 0,
                    submitted_by,
                    stamp,
                    cash_amount,
                    reputation_delta,
                    1 if has_profit else 0,
                ),
            )
        except DATABASE_INTEGRITY_ERRORS as exc:
            await db.rollback()
            conflict = exc
        else:
            report_id = _inserted_id(cursor)
            for share in shares:
                await db.execute(
                    """INSERT INTO contract_report_shares (
                           report_id, discord_id, amount
                       ) VALUES (?, ?, ?)""",
                    (report_id, share.discord_id, share.amount),
                )
            for kind, url, digest, size in evidence:
                await db.execute(
                    """INSERT INTO contract_report_evidence (
                           guild_id, family_id, report_id, kind, url,
                           content_hash, byte_size, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (guild_id, family_id, report_id, kind, url, digest, size, stamp),
                )
            for purchase in purchases:
                await db.execute(
                    """INSERT INTO contract_report_purchases (
                           guild_id, family_id, report_id, item_name,
                           quantity, unit_price
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id,
                        family_id,
                        report_id,
                        purchase.item_name,
                        purchase.quantity,
                        purchase.unit_price,
                    ),
                )
            for expense in expenses:
                await db.execute(
                    """INSERT INTO contract_report_expenses (
                           guild_id, family_id, report_id, label, amount
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (guild_id, family_id, report_id, expense.label, expense.amount),
                )
            for payout in item_payouts:
                await db.execute(
                    """INSERT INTO contract_report_item_payouts (
                           guild_id, family_id, report_id, item_name,
                           quantity, valuation
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id,
                        family_id,
                        report_id,
                        payout.item_name,
                        payout.quantity,
                        payout.valuation,
                    ),
                )
            await db.execute(
                """UPDATE contract_runs
                   SET status = ?, version = version + 1, updated_at = ?
                   WHERE id = ?""",
                (run_status, stamp, run_id),
            )
            await db.commit()
    if conflict is not None:
        async with _connect() as lookup:
            existing = await lookup.execute_fetchall(
                "SELECT id FROM contract_reports WHERE idempotency_key = ?",
                (key,),
            )
            if existing:
                loaded = await _load(lookup, int(existing[0][0]))
                if loaded is not None:
                    return loaded
        raise conflict
    loaded = await get_report(report_id)
    if loaded is None:
        raise RuntimeError("report insert did not persist")
    return loaded


async def try_set_review(
    report_id: int,
    *,
    status: str,
    expected_version: int,
    reviewer_id: int,
    reason: str,
    run_status: str,
    create_obligation: bool,
) -> str:
    if status not in REPORT_STATUSES:
        raise ValueError("unknown report status")
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "contract-report", report_id)
        rows = await db.execute_fetchall(
            """SELECT status, version, run_id, family_id, guild_id, result_amount
               FROM contract_reports WHERE id = ?""",
            (report_id,),
        )
        if not rows:
            return "missing"
        current, version, run_id, family_id, guild_id, amount = rows[0]
        if str(current) != "submitted":
            return "already"
        if int(version) != expected_version:
            return "conflict"
        cursor = await db.execute(
            """UPDATE contract_reports
               SET status = ?, reviewed_by = ?, reviewed_at = ?,
                   review_reason = ?, version = version + 1
               WHERE id = ? AND status = 'submitted' AND version = ?""",
            (status, reviewer_id, stamp, reason, report_id, expected_version),
        )
        if int(cursor.rowcount or 0) != 1:
            return "conflict"
        await db.execute(
            """UPDATE contract_runs
               SET status = ?, version = version + 1, updated_at = ?
               WHERE id = ?""",
            (run_status, stamp, int(run_id)),
        )
        if create_obligation:
            try:
                await db.execute(
                    """INSERT INTO report_payout_obligations (
                           guild_id, family_id, report_id, run_id, amount,
                           status, created_at
                       ) VALUES (?, ?, ?, ?, ?, 'open', ?)""",
                    (
                        int(guild_id),
                        int(family_id),
                        report_id,
                        int(run_id),
                        int(amount),
                        stamp,
                    ),
                )
            except DATABASE_INTEGRITY_ERRORS:
                await db.rollback()
                return "already"
        await db.commit()
    return "ok"


async def reverse_accepted_report(
    report_id: int, *, actor_id: int, reason: str
) -> str:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "contract-report", report_id)
        rows = await db.execute_fetchall(
            "SELECT status FROM contract_reports WHERE id = ?",
            (report_id,),
        )
        if not rows:
            return "missing"
        if str(rows[0][0]) != "accepted":
            return "not_accepted"
        await db.execute(
            """INSERT INTO contract_report_reversals (
                   guild_id, family_id, report_id, actor_id, reason, created_at
               )
               SELECT guild_id, family_id, id, ?, ?, ?
               FROM contract_reports WHERE id = ?""",
            (actor_id, reason, stamp, report_id),
        )
        await db.execute(
            """UPDATE contract_reports
               SET status = 'reversed', version = version + 1
               WHERE id = ?""",
            (report_id,),
        )
        await db.execute(
            """UPDATE report_payout_obligations
               SET status = 'cancelled'
               WHERE report_id = ?""",
            (report_id,),
        )
        await db.commit()
    return "ok"


async def list_purchase_history(
    family_id: int,
    catalog_id: int,
    *,
    exclude_report_id: int | None = None,
    lookback: int = 0,
) -> list[ReportPurchase]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT p.item_name, p.quantity, p.unit_price, r.id
               FROM contract_report_purchases p
               JOIN contract_reports r ON r.id = p.report_id
               JOIN contract_runs u ON u.id = r.run_id
               WHERE r.family_id = ? AND u.catalog_id = ?
                 AND r.status = 'accepted' AND r.has_profit = 1
               ORDER BY r.submitted_at DESC, r.id DESC, p.id""",
            (family_id, catalog_id),
        )
    seen: list[int] = []
    items: list[ReportPurchase] = []
    for row in rows:
        report_id = int(row[3])
        if exclude_report_id is not None and report_id == exclude_report_id:
            continue
        if report_id not in seen:
            if lookback > 0 and len(seen) >= lookback:
                continue
            seen.append(report_id)
        items.append(
            ReportPurchase(
                item_name=str(row[0]), quantity=int(row[1]), unit_price=int(row[2])
            )
        )
    return items


async def list_accepted_profit_reports(
    family_id: int, catalog_id: int | None = None
) -> list[ContractReport]:
    query = """SELECT r.id FROM contract_reports r
                JOIN contract_runs u ON u.id = r.run_id
                WHERE r.family_id = ? AND r.status = 'accepted'
                  AND r.has_profit = 1"""
    params: list[object] = [family_id]
    if catalog_id is not None:
        query += " AND u.catalog_id = ?"
        params.append(catalog_id)
    query += " ORDER BY r.submitted_at, r.id"
    async with _connect() as db:
        rows = await db.execute_fetchall(query, tuple(params))
        items: list[ContractReport] = []
        for row in rows:
            loaded = await _load(db, int(row[0]))
            if loaded is not None:
                items.append(loaded)
    return items


async def count_accepted_reports(family_id: int) -> int:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT COUNT(*) FROM contract_reports
               WHERE family_id = ? AND status = 'accepted'""",
            (family_id,),
        )
    return int(rows[0][0]) if rows else 0
