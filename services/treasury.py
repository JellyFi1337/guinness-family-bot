"""Accounts, payout obligations and game reconciliation. The bot does not move in-game funds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.permissions import AccessDecision, Permission, evaluate_access
from core.settings import get_registry
from repositories.accounts import (
    Account,
    LedgerEntry,
    Obligation,
    Reconciliation,
    book_balance,
    find_account,
    find_obligation,
    get_account,
    get_ledger_entry,
    get_obligation,
    insert_ledger_entry,
    insert_obligation,
    insert_reconciliation,
    latest_reconciliation,
    list_accounts,
    list_obligations,
    update_obligation,
    upsert_account,
)
from repositories.reports import get_report
from repositories.treasury import get_last_transactions, get_treasury_balance
from services.audit import record_decision
from services.membership import (
    OpenObligation,
    ensure_family,
    ensure_obligation_source,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec, MessageTone
from ui.templates import notice

CURRENCY_LABELS = {
    "game_money": "игровые деньги",
    "guinn_coins": "Guinn Coins",
    "contribution": "очки внутреннего вклада",
    "reputation": "игровая репутация",
    "influence": "игровое влияние",
    "dark_coins": "Dark Coins",
}
STATUS_LABELS = {
    "promised": "обещано",
    "approved": "согласовано",
    "paid": "выплачено",
    "cancelled": "отменено",
}
CONTEXT_LABELS = {
    "memphis": "Memphis",
    "milton": "архив предыдущего проекта (не актив Memphis)",
}
FAMILY_ACCOUNT_SEED = (
    ("game_money", "Игровые деньги семьи"),
    ("guinn_coins", "Guinn Coins семьи"),
    ("contribution", "Очки внутреннего вклада"),
    ("reputation", "Игровая репутация"),
    ("influence", "Игровое влияние"),
    ("dark_coins", "Dark Coins (начисляет сервер)"),
)


class TreasuryError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        decision: AccessDecision | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.decision = decision


@dataclass(frozen=True, slots=True)
class AccountCard:
    account: Account
    balance: int
    last_reconciliation: Reconciliation | None


def _actor_id(actor: Any) -> int:
    return int(actor.id)


def _actor_name(actor: Any) -> str:
    return str(getattr(actor, "display_name", None) or getattr(actor, "name", actor.id))


async def _require(actor: Any, permission: Permission) -> None:
    decision = await evaluate_access(actor, permission)
    if decision.allowed:
        return
    raise TreasuryError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


async def _family(guild_id: int):
    return await ensure_family(guild_id)


async def batch_threshold(guild_id: int) -> int | None:
    raw = await get_registry().get(guild_id, "TREASURY_BATCH_THRESHOLD")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


async def ensure_family_accounts(guild_id: int) -> list[Account]:
    family = await _family(guild_id)
    items: list[Account] = []
    for currency, name in FAMILY_ACCOUNT_SEED:
        items.append(
            await upsert_account(
                guild_id=guild_id,
                family_id=family.id,
                currency=currency,
                name=name,
                context="memphis",
                archived=False,
            )
        )
    await upsert_account(
        guild_id=guild_id,
        family_id=family.id,
        currency="game_money",
        name="Архивная казна предыдущего проекта",
        context="milton",
        archived=True,
    )
    return items


async def list_live_accounts(actor: Any, guild_id: int) -> list[AccountCard]:
    await _require(actor, Permission.TREASURY_VIEW)
    await ensure_family_accounts(guild_id)
    family = await _family(guild_id)
    cards: list[AccountCard] = []
    for account in await list_accounts(family.id, include_archived=False):
        if account.context != "memphis":
            continue
        cards.append(
            AccountCard(
                account,
                await book_balance(account.id),
                await latest_reconciliation(account.id),
            )
        )
    return cards


async def show_account(actor: Any, guild_id: int, account_id: int) -> AccountCard:
    await _require(actor, Permission.TREASURY_VIEW)
    account = await get_account(account_id)
    family = await _family(guild_id)
    if account is None or account.family_id != family.id:
        raise TreasuryError(
            "Счёт не найден. Карточка не открыта; проверьте номер.",
            code="TREASURY.NOT_FOUND",
            retryable=True,
        )
    return AccountCard(
        account,
        await book_balance(account.id),
        await latest_reconciliation(account.id),
    )


async def legacy_history(actor: Any, guild_id: int) -> tuple[int, tuple[Any, ...]]:
    await _require(actor, Permission.TREASURY_VIEW)
    del guild_id
    balance = await get_treasury_balance()
    rows = tuple(await get_last_transactions(20))
    return balance, rows


def _reward_from_report(report: Any) -> tuple[int | None, str | None]:
    if report.has_profit and report.cash_amount is not None:
        return int(report.cash_amount), f"report:{report.id}:profit"
    return None, None


async def create_from_accepted_report(
    actor: Any | None,
    guild_id: int,
    report_id: int,
) -> Obligation:
    report = await get_report(report_id)
    if report is None or report.guild_id != guild_id:
        raise TreasuryError(
            "Отчёт не найден. Обязательство не создано.",
            code="TREASURY.REPORT_MISSING",
            retryable=True,
        )
    if report.status != "accepted":
        raise TreasuryError(
            "Непринятый отчёт не создаёт денег к выплате. "
            "Обязательство не создано.",
            code="TREASURY.REPORT_NOT_ACCEPTED",
        )
    existing = await find_obligation(
        family_id=report.family_id,
        basis_kind="report",
        basis_id=str(report.id),
    )
    if existing is not None:
        return existing
    amount, terms = _reward_from_report(report)
    promised_by = _actor_id(actor) if actor is not None else int(report.reviewed_by or 0)
    created = await insert_obligation(
        guild_id=report.guild_id,
        family_id=report.family_id,
        basis_kind="report",
        basis_id=str(report.id),
        payee_id=report.submitted_by,
        currency="game_money",
        amount=amount,
        terms_version=terms,
        promised_by=promised_by,
    )
    if actor is not None:
        await record_decision(
            guild_id=guild_id,
            action="TREASURY.PROMISED",
            subject_type="obligation",
            subject_id=str(created.id),
            actor_id=_actor_id(actor),
            actor_name=_actor_name(actor),
            after=created.status,
            source="command",
        )
    return created


async def show_obligation(actor: Any, guild_id: int, obligation_id: int) -> Obligation:
    await _require(actor, Permission.TREASURY_VIEW)
    family = await _family(guild_id)
    item = await get_obligation(obligation_id)
    if item is None or item.family_id != family.id:
        raise TreasuryError(
            "Обязательство не найдено. Карточка не открыта; проверьте номер.",
            code="TREASURY.NOT_FOUND",
            retryable=True,
        )
    return item


async def latest_live_reconciliation(guild_id: int) -> Reconciliation | None:
    await ensure_family_accounts(guild_id)
    family = await _family(guild_id)
    latest: Reconciliation | None = None
    for account in await list_accounts(family.id, include_archived=False):
        if account.context != "memphis":
            continue
        item = await latest_reconciliation(account.id)
        if item is None:
            continue
        if latest is None or item.id > latest.id:
            latest = item
    return latest


async def approve_obligation(
    actor: Any,
    guild_id: int,
    obligation_id: int,
    *,
    batch_id: str | None = None,
) -> Obligation:
    await _require(actor, Permission.TREASURY_APPROVE)
    item = await show_obligation(actor, guild_id, obligation_id)
    actor_id = _actor_id(actor)
    if actor_id == item.payee_id:
        raise TreasuryError(
            "Собственный отчёт и собственную выплату нельзя закрыть единолично. "
            "Согласование не записано; передайте другому казначею.",
            code="TREASURY.SELF_APPROVE",
        )
    if item.status == "cancelled":
        raise TreasuryError(
            "Отменённое обязательство согласовать нельзя.",
            code="TREASURY.CANCELLED",
        )
    if item.status in {"approved", "paid"}:
        return item
    if item.amount is None:
        raise TreasuryError(
            "Сумма по сохранённым условиям ещё не задана. "
            "Согласование не записано; укажите сумму из снимка отчёта.",
            code="TREASURY.AMOUNT",
            retryable=True,
        )
    threshold = await batch_threshold(guild_id)
    needs_second = threshold is not None and item.amount > threshold
    if needs_second and item.approved_by is None:
        updated = await update_obligation(
            item.id,
            expected_version=item.version,
            approved_by=actor_id,
            batch_id=batch_id,
        )
        if updated is None:
            raise TreasuryError(
                "Обязательство изменилось. Согласование не записано; повторите.",
                code="TREASURY.CONFLICT",
                retryable=True,
            )
        await record_decision(
            guild_id=guild_id,
            action="TREASURY.APPROVE_FIRST",
            subject_type="obligation",
            subject_id=str(item.id),
            actor_id=actor_id,
            actor_name=_actor_name(actor),
            source="command",
        )
        return updated
    if needs_second:
        if item.approved_by == actor_id:
            raise TreasuryError(
                "Крупная выплата требует второго решения другого человека. "
                "Повтор вашим голосом не записан.",
                code="TREASURY.SECOND_REQUIRED",
            )
        updated = await update_obligation(
            item.id,
            expected_version=item.version,
            status="approved",
            second_approved_by=actor_id,
            batch_id=batch_id,
        )
    else:
        updated = await update_obligation(
            item.id,
            expected_version=item.version,
            status="approved",
            approved_by=actor_id,
            batch_id=batch_id,
        )
    if updated is None:
        raise TreasuryError(
            "Обязательство изменилось. Согласование не записано; повторите.",
            code="TREASURY.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="TREASURY.APPROVED",
        subject_type="obligation",
        subject_id=str(item.id),
        actor_id=actor_id,
        actor_name=_actor_name(actor),
        after="approved",
        source="command",
    )
    return updated


async def batch_approve(
    actor: Any, guild_id: int, obligation_ids: tuple[int, ...]
) -> list[Obligation]:
    await _require(actor, Permission.TREASURY_APPROVE)
    threshold = await batch_threshold(guild_id)
    if threshold is None:
        raise TreasuryError(
            "Порог пакетного согласования не задан лидером. "
            "Пакет не записан; согласуйте крупные выплаты по одной "
            f"через {cmd('treasury approve')} или задайте порог в настройках.",
            code="TREASURY.THRESHOLD",
            retryable=True,
        )
    batch_id = f"batch:{_actor_id(actor)}:{guild_id}:{len(obligation_ids)}"
    updated: list[Obligation] = []
    for obligation_id in obligation_ids:
        item = await show_obligation(actor, guild_id, obligation_id)
        if item.amount is None or item.amount > threshold:
            raise TreasuryError(
                f"Обязательство #{item.id} выше порога пакета. "
                "Пакет не записан; согласуйте его отдельно.",
                code="TREASURY.TOO_LARGE",
                retryable=True,
            )
        updated.append(
            await approve_obligation(
                actor, guild_id, obligation_id, batch_id=batch_id
            )
        )
    return updated


async def mark_paid(
    actor: Any,
    guild_id: int,
    obligation_id: int,
    *,
    proof_ref: str,
) -> Obligation:
    await _require(actor, Permission.TREASURY_PAY)
    await ensure_family_accounts(guild_id)
    item = await show_obligation(actor, guild_id, obligation_id)
    actor_id = _actor_id(actor)
    if actor_id == item.payee_id:
        raise TreasuryError(
            "Собственную выплату нельзя закрыть единолично. "
            "Отметка перевода не записана.",
            code="TREASURY.SELF_PAY",
        )
    if item.status == "paid":
        return item
    if item.status != "approved":
        raise TreasuryError(
            "Перевод отмечают только после согласования. "
            "Отметка не записана.",
            code="TREASURY.NOT_APPROVED",
        )
    text = proof_ref.strip()
    if not text:
        raise TreasuryError(
            "Приложите подтверждение перевода. Отметка не записана.",
            code="TREASURY.PROOF",
            retryable=True,
        )
    if item.amount is None or item.amount <= 0:
        raise TreasuryError(
            "Сумма не задана. Отметка не записана.",
            code="TREASURY.AMOUNT",
            retryable=True,
        )
    family = await _family(guild_id)
    account = await find_account(
        family_id=family.id, currency=item.currency, context="memphis"
    )
    if account is None or account.archived:
        raise TreasuryError(
            "Живой счёт этой валюты не найден. Отметка не записана.",
            code="TREASURY.ACCOUNT",
        )
    await insert_ledger_entry(
        guild_id=guild_id,
        family_id=family.id,
        account_id=account.id,
        operation="debit",
        amount=item.amount,
        basis_kind="obligation",
        basis_id=str(item.id),
        actor_id=actor_id,
        source_ref=text,
    )
    updated = await update_obligation(
        item.id,
        expected_version=item.version,
        status="paid",
        paid_by=actor_id,
        proof_ref=text,
    )
    if updated is None:
        raise TreasuryError(
            "Обязательство изменилось. Отметка не записана; повторите.",
            code="TREASURY.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="TREASURY.PAID",
        subject_type="obligation",
        subject_id=str(item.id),
        actor_id=actor_id,
        actor_name=_actor_name(actor),
        after="paid",
        source="command",
    )
    return updated


async def confirm_obligation(
    actor: Any, guild_id: int, obligation_id: int
) -> Obligation:
    family = await _family(guild_id)
    item = await get_obligation(obligation_id)
    if item is None or item.family_id != family.id:
        raise TreasuryError(
            "Обязательство не найдено. Подтверждение не записано; проверьте номер.",
            code="TREASURY.NOT_FOUND",
            retryable=True,
        )
    actor_id = _actor_id(actor)
    if item.status != "paid":
        raise TreasuryError(
            "Подтверждают только уже отмеченный перевод. "
            "Подтверждение не записано.",
            code="TREASURY.NOT_PAID",
        )
    if item.confirmed_by is not None:
        return item
    is_payee = actor_id == item.payee_id
    can_recon = (await evaluate_access(actor, Permission.TREASURY_PAY)).allowed
    if not is_payee and not can_recon:
        raise TreasuryError(
            "Подтверждает получатель или казначей сверки. "
            "Подтверждение не записано.",
            code="TREASURY.NOT_CONFIRMER",
        )
    if is_payee and actor_id == item.paid_by:
        raise TreasuryError(
            "Собственную выплату нельзя закрыть единолично. "
            "Подтверждение не записано.",
            code="TREASURY.SELF_CONFIRM",
        )
    updated = await update_obligation(
        item.id,
        expected_version=item.version,
        confirmed_by=actor_id,
    )
    if updated is None:
        raise TreasuryError(
            "Обязательство изменилось. Подтверждение не записано; повторите.",
            code="TREASURY.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="TREASURY.CONFIRMED",
        subject_type="obligation",
        subject_id=str(item.id),
        actor_id=actor_id,
        actor_name=_actor_name(actor),
        source="command",
    )
    return updated


async def cancel_obligation(
    actor: Any, guild_id: int, obligation_id: int
) -> Obligation:
    await _require(actor, Permission.TREASURY_APPROVE)
    item = await show_obligation(actor, guild_id, obligation_id)
    if item.status == "cancelled":
        return item
    if item.status == "paid":
        raise TreasuryError(
            "Выплаченное обязательство отменяют только отменяющей записью "
            f"журнала через {cmd('treasury reverse')}.",
            code="TREASURY.ALREADY_PAID",
        )
    updated = await update_obligation(
        item.id,
        expected_version=item.version,
        status="cancelled",
    )
    if updated is None:
        raise TreasuryError(
            "Обязательство изменилось. Отмена не записана; повторите.",
            code="TREASURY.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="TREASURY.CANCELLED",
        subject_type="obligation",
        subject_id=str(item.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after="cancelled",
        source="command",
    )
    return updated


async def reverse_ledger(
    actor: Any,
    guild_id: int,
    entry_id: int,
    *,
    reason: str,
) -> LedgerEntry:
    await _require(actor, Permission.TREASURY_PAY)
    family = await _family(guild_id)
    entry = await get_ledger_entry(entry_id)
    if entry is None or entry.family_id != family.id:
        raise TreasuryError(
            "Запись журнала не найдена. Отмена не создана.",
            code="TREASURY.NOT_FOUND",
            retryable=True,
        )
    if entry.operation == "reverse":
        raise TreasuryError(
            "Отменяющую запись ещё раз отменить нельзя.",
            code="TREASURY.ALREADY_REVERSED",
        )
    text = reason.strip()
    if not text:
        raise TreasuryError(
            "Укажите причину отмены. Журнал не изменён.",
            code="TREASURY.REASON",
            retryable=True,
        )
    created = await insert_ledger_entry(
        guild_id=guild_id,
        family_id=family.id,
        account_id=entry.account_id,
        operation="reverse",
        amount=entry.amount,
        basis_kind="reverse",
        basis_id=str(entry.id),
        actor_id=_actor_id(actor),
        source_ref=text,
        reversal_of=entry.id,
    )
    await record_decision(
        guild_id=guild_id,
        action="TREASURY.REVERSED",
        subject_type="ledger_entry",
        subject_id=str(entry.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        reason=text,
        source="command",
    )
    return created


async def reconcile_account(
    actor: Any,
    guild_id: int,
    account_id: int,
    *,
    game_balance: int,
    explanation: str,
) -> Reconciliation:
    await _require(actor, Permission.TREASURY_PAY)
    card = await show_account(actor, guild_id, account_id)
    if card.account.context != "memphis" or card.account.archived:
        raise TreasuryError(
            "Архивный контекст предыдущего проекта не сверяют как актив Memphis. "
            "Сверка не записана.",
            code="TREASURY.ARCHIVED",
        )
    text = explanation.strip()
    if not text:
        raise TreasuryError(
            "Объясните расхождение. Сверка не записана.",
            code="TREASURY.REASON",
            retryable=True,
        )
    if game_balance < 0:
        raise TreasuryError(
            "Игровой остаток не может быть отрицательным. Сверка не записана.",
            code="TREASURY.BALANCE",
            retryable=True,
        )
    family = await _family(guild_id)
    created = await insert_reconciliation(
        guild_id=guild_id,
        family_id=family.id,
        account_id=card.account.id,
        game_balance=game_balance,
        book_balance=card.balance,
        explanation=text,
        actor_id=_actor_id(actor),
    )
    await record_decision(
        guild_id=guild_id,
        action="TREASURY.RECONCILED",
        subject_type="account",
        subject_id=str(card.account.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=str(created.delta),
        reason=text,
        source="command",
    )
    return created


async def cancel_from_reversed_report(report_id: int) -> Obligation | None:
    report = await get_report(report_id)
    if report is None:
        return None
    item = await find_obligation(
        family_id=report.family_id,
        basis_kind="report",
        basis_id=str(report.id),
    )
    if item is None or item.status in {"cancelled", "paid"}:
        return item
    return await update_obligation(
        item.id,
        expected_version=item.version,
        status="cancelled",
    )


def account_card_spec(card: AccountCard) -> MessageSpec:
    account = card.account
    last = card.last_reconciliation
    recon = (
        f"{last.created_at} · расхождение {last.delta}"
        if last is not None
        else "ещё не сверяли"
    )
    return notice(
        code="TREASURY.ACCOUNT",
        title=account.name,
        summary=(
            "Бот ведёт учёт и не переводит игровые средства. "
            "Валюты между собой не меняются."
        ),
        details=(
            ("Валюта", CURRENCY_LABELS.get(account.currency, account.currency)),
            ("Контекст", CONTEXT_LABELS.get(account.context, account.context)),
            ("Книжный остаток", str(card.balance)),
            ("Последняя сверка", recon),
            ("Архив", "да" if account.archived else "нет"),
        ),
        next_step=f"Сверка — {cmd('treasury reconcile')}.",
        reference=str(account.id),
        tone=MessageTone.NEUTRAL,
    )


def obligation_card_spec(item: Obligation) -> MessageSpec:
    amount = (
        str(item.amount)
        if item.amount is not None
        else "требует проверки по снимку условий"
    )
    return notice(
        code="TREASURY.OBLIGATION",
        title=f"Обязательство #{item.id}",
        summary=(
            f"Статус: {STATUS_LABELS.get(item.status, item.status)}. "
            "Одно основание — одно обязательство."
        ),
        details=(
            ("Основание", f"{item.basis_kind}:{item.basis_id}"),
            ("Получатель", f"<@{item.payee_id}>"),
            ("Валюта", CURRENCY_LABELS.get(item.currency, item.currency)),
            ("Сумма", amount),
            ("Условия", item.terms_version or "требует проверки"),
            ("Подтверждение перевода", item.proof_ref or "ещё нет"),
        ),
        next_step=(
            f"Согласовать — {cmd('treasury approve')}. "
            f"Отметить перевод — {cmd('treasury pay')}."
        ),
        reference=str(item.id),
        tone=MessageTone.NEUTRAL,
    )


class PayoutObligationSource:
    async def list_open(self, guild_id: int, discord_id: int) -> list[OpenObligation]:
        family = await ensure_family(guild_id)
        items: list[OpenObligation] = []
        for row in await list_obligations(family.id):
            if row.payee_id != discord_id or row.status == "cancelled":
                continue
            outstanding = row.status in {"promised", "approved"} or (
                row.status == "paid" and row.confirmed_by is None
            )
            if not outstanding:
                continue
            items.append(
                OpenObligation(
                    kind="payout",
                    subject_id=str(row.id),
                    summary=(
                        f"Выплата #{row.id}: "
                        f"{STATUS_LABELS.get(row.status, row.status)}"
                    ),
                    outstanding=True,
                    needs_reconciliation=(
                        row.status == "paid" and row.confirmed_by is None
                    ),
                    reference=str(row.id),
                )
            )
        return items


def ensure_treasury_integrations() -> None:
    ensure_obligation_source(PayoutObligationSource())


ensure_treasury_integrations()
