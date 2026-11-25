"""Integer profit breakdown for a contract report. Not a standalone calculator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

PROFIT_FIELD_GROUPS = frozenset(
    {"purchase", "expenses", "cash", "items", "reputation"}
)


class ProfitError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PurchaseLine:
    item_name: str
    quantity: int
    unit_price: int

    @property
    def total(self) -> int:
        return self.quantity * self.unit_price


@dataclass(frozen=True, slots=True)
class ExpenseLine:
    label: str
    amount: int


@dataclass(frozen=True, slots=True)
class ItemPayoutLine:
    item_name: str
    quantity: int
    valuation: int
    is_estimate: bool = True


@dataclass(frozen=True, slots=True)
class ProfitDraft:
    purchases: tuple[PurchaseLine, ...] = ()
    expenses: tuple[ExpenseLine, ...] = ()
    item_payouts: tuple[ItemPayoutLine, ...] = ()
    cash_amount: int | None = None
    reputation_delta: int | None = None

    @property
    def recorded(self) -> bool:
        return bool(
            self.purchases
            or self.expenses
            or self.item_payouts
            or self.cash_amount is not None
            or self.reputation_delta is not None
        )


@dataclass(frozen=True, slots=True)
class ProfitBreakdown:
    purchase_total: int
    expense_total: int
    costs: int
    cash: int
    item_valuation_total: int
    revenue: int
    net: int
    people: int
    duration_minutes: int
    reputation_delta: int | None
    per_person: int | None
    per_hour: int | None
    per_person_per_hour: int | None
    item_valuations_are_estimates: bool
    purchases: tuple[PurchaseLine, ...]
    expenses: tuple[ExpenseLine, ...]
    item_payouts: tuple[ItemPayoutLine, ...]


@dataclass(frozen=True, slots=True)
class PriceHint:
    item_name: str
    amount: int | None
    source: str
    note: str


@dataclass(frozen=True, slots=True)
class CatalogProfitStats:
    catalog_id: int
    title: str
    samples: int
    median_net: int
    median_per_person_per_hour: int | None
    net_min: int
    net_max: int
    losing: int
    people_median: int
    reputation_median: int | None


def require_int(value: object, *, field: str) -> int:
    if type(value) is bool or not isinstance(value, int):
        raise ProfitError(
            f"{field} должно быть целым числом. Расчёт не записан.",
            code="PROFIT.NOT_INT",
        )
    return value


def integer_median(values: Sequence[int]) -> int:
    if not values:
        raise ProfitError(
            "Недостаточно чисел для медианы.",
            code="PROFIT.EMPTY",
        )
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def parse_profit_fields(raw: object) -> frozenset[str]:
    text = str(raw or "").strip()
    if not text:
        return PROFIT_FIELD_GROUPS
    items = {part.strip() for part in text.split(",") if part.strip()}
    unknown = items - PROFIT_FIELD_GROUPS
    if unknown:
        raise ProfitError(
            "Неизвестная группа полей расчёта: "
            + ", ".join(sorted(unknown))
            + ". Допустимы: "
            + ", ".join(sorted(PROFIT_FIELD_GROUPS))
            + ".",
            code="PROFIT.FIELDS",
        )
    return frozenset(items)


def allowed_profit_fields(snapshot: dict[str, Any] | None) -> frozenset[str]:
    if not snapshot:
        return PROFIT_FIELD_GROUPS
    return parse_profit_fields(snapshot.get("profit_fields"))


def _split_triples(raw: str, *, code: str, what: str) -> tuple[tuple[str, int, int], ...]:
    items: list[tuple[str, int, int]] = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        bits = piece.split(":")
        if len(bits) != 3:
            raise ProfitError(
                f"{what} пишутся как имя:количество:цена через запятую. "
                "Отчёт не создан.",
                code=code,
            )
        name = bits[0].strip()
        if not name:
            raise ProfitError(
                f"У {what.lower()} должно быть имя. Отчёт не создан.",
                code=code,
            )
        try:
            quantity = int(bits[1].strip())
            price = int(bits[2].strip())
        except ValueError as exc:
            raise ProfitError(
                f"Количество и цена {what.lower()} — целые числа. "
                "Отчёт не создан.",
                code=code,
            ) from exc
        if quantity < 0 or price < 0:
            raise ProfitError(
                f"Количество и цена {what.lower()} не могут быть отрицательными. "
                "Отчёт не создан.",
                code=code,
            )
        items.append((name, require_int(quantity, field="количество"), require_int(price, field="цена")))
    return tuple(items)


def parse_purchases(raw: str | None) -> tuple[PurchaseLine, ...]:
    if not raw or not raw.strip():
        return ()
    return tuple(
        PurchaseLine(item_name=name, quantity=qty, unit_price=price)
        for name, qty, price in _split_triples(
            raw, code="PROFIT.PURCHASE", what="Закупки"
        )
    )


def parse_item_payouts(raw: str | None) -> tuple[ItemPayoutLine, ...]:
    if not raw or not raw.strip():
        return ()
    return tuple(
        ItemPayoutLine(
            item_name=name,
            quantity=qty,
            valuation=price,
            is_estimate=True,
        )
        for name, qty, price in _split_triples(
            raw, code="PROFIT.ITEMS", what="Предметы"
        )
    )


def parse_expenses(raw: str | None) -> tuple[ExpenseLine, ...]:
    if not raw or not raw.strip():
        return ()
    items: list[ExpenseLine] = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        if ":" not in piece:
            raise ProfitError(
                "Расходы пишутся как статья:сумма через запятую. Отчёт не создан.",
                code="PROFIT.EXPENSE",
            )
        label, amount_text = piece.split(":", 1)
        name = label.strip()
        if not name:
            raise ProfitError(
                "У статьи расхода должно быть имя. Отчёт не создан.",
                code="PROFIT.EXPENSE",
            )
        try:
            amount = int(amount_text.strip())
        except ValueError as exc:
            raise ProfitError(
                "Сумма расхода — целое число. Отчёт не создан.",
                code="PROFIT.EXPENSE",
            ) from exc
        if amount < 0:
            raise ProfitError(
                "Сумма расхода не может быть отрицательной. Отчёт не создан.",
                code="PROFIT.EXPENSE",
            )
        items.append(ExpenseLine(label=name, amount=require_int(amount, field="расход")))
    return tuple(items)


def draft_from_report(report: Any) -> ProfitDraft:
    return ProfitDraft(
        purchases=tuple(
            PurchaseLine(
                item_name=item.item_name,
                quantity=int(item.quantity),
                unit_price=int(item.unit_price),
            )
            for item in getattr(report, "purchases", ())
        ),
        expenses=tuple(
            ExpenseLine(label=item.label, amount=int(item.amount))
            for item in getattr(report, "expenses", ())
        ),
        item_payouts=tuple(
            ItemPayoutLine(
                item_name=item.item_name,
                quantity=int(item.quantity),
                valuation=int(item.valuation),
                is_estimate=True,
            )
            for item in getattr(report, "item_payouts", ())
        ),
        cash_amount=report.cash_amount,
        reputation_delta=report.reputation_delta,
    )


def draft_from_form(
    *,
    purchases: str | None = None,
    expenses: str | None = None,
    items: str | None = None,
    cash: int | None = None,
    reputation: int | None = None,
    allowed: frozenset[str] | None = None,
) -> ProfitDraft:
    groups = allowed if allowed is not None else PROFIT_FIELD_GROUPS
    parsed_purchases = parse_purchases(purchases)
    parsed_expenses = parse_expenses(expenses)
    parsed_items = parse_item_payouts(items)
    cash_amount = None if cash is None else require_int(cash, field="выплата деньгами")
    reputation_delta = (
        None if reputation is None else require_int(reputation, field="репутация")
    )
    if cash_amount is not None and cash_amount < 0:
        raise ProfitError(
            "Выплата деньгами не может быть отрицательной. Отчёт не создан.",
            code="PROFIT.CASH",
        )
    blocked: list[str] = []
    if parsed_purchases and "purchase" not in groups:
        blocked.append("закупка")
    if parsed_expenses and "expenses" not in groups:
        blocked.append("расходы")
    if cash_amount is not None and "cash" not in groups:
        blocked.append("выплата деньгами")
    if parsed_items and "items" not in groups:
        blocked.append("предметы")
    if reputation_delta is not None and "reputation" not in groups:
        blocked.append("репутация")
    if blocked:
        raise ProfitError(
            "Для этого контракта из каталога не нужны поля: "
            + ", ".join(blocked)
            + ". Отчёт не создан; уберите их или измените набор в каталоге.",
            code="PROFIT.FIELD_OFF",
        )
    return ProfitDraft(
        purchases=parsed_purchases,
        expenses=parsed_expenses,
        item_payouts=parsed_items,
        cash_amount=cash_amount,
        reputation_delta=reputation_delta,
    )


def compute_profit(
    draft: ProfitDraft,
    *,
    people: int,
    duration_minutes: int,
) -> ProfitBreakdown:
    people_n = require_int(people, field="участники")
    minutes = require_int(duration_minutes, field="время")
    if people_n < 0 or minutes < 0:
        raise ProfitError(
            "Число людей и время не могут быть отрицательными.",
            code="PROFIT.AMOUNT",
        )
    purchase_total = 0
    for purchase in draft.purchases:
        purchase_total += require_int(purchase.total, field="закупка")
    expense_total = 0
    for expense in draft.expenses:
        expense_total += require_int(expense.amount, field="расход")
    costs = purchase_total + expense_total
    cash = 0 if draft.cash_amount is None else require_int(draft.cash_amount, field="деньги")
    item_total = 0
    for payout in draft.item_payouts:
        item_total += require_int(payout.valuation, field="оценка") * require_int(
            payout.quantity, field="предметы"
        )
    revenue = cash + item_total
    net = revenue - costs
    per_person = None if people_n == 0 else net // people_n
    per_hour = None if minutes == 0 else (net * 60) // minutes
    per_person_per_hour = (
        None if people_n == 0 or minutes == 0 else (net * 60) // (people_n * minutes)
    )
    return ProfitBreakdown(
        purchase_total=purchase_total,
        expense_total=expense_total,
        costs=costs,
        cash=cash,
        item_valuation_total=item_total,
        revenue=revenue,
        net=net,
        people=people_n,
        duration_minutes=minutes,
        reputation_delta=draft.reputation_delta,
        per_person=per_person,
        per_hour=per_hour,
        per_person_per_hour=per_person_per_hour,
        item_valuations_are_estimates=True,
        purchases=draft.purchases,
        expenses=draft.expenses,
        item_payouts=draft.item_payouts,
    )


def member_share(net: int, share_amount: int, result_amount: int) -> int:
    require_int(net, field="прибыль")
    require_int(share_amount, field="доля")
    require_int(result_amount, field="результат")
    if result_amount <= 0:
        return 0
    return (net * share_amount) // result_amount


def weighted_average_unit_price(lines: Iterable[PurchaseLine]) -> int | None:
    spent = 0
    qty = 0
    for line in lines:
        spent += require_int(line.total, field="закупка")
        qty += require_int(line.quantity, field="количество")
    if qty <= 0:
        return None
    return spent // qty


def market_price_hint(item_name: str) -> PriceHint:
    del item_name
    return PriceHint(
        item_name="",
        amount=None,
        source="none",
        note="Подсказка рынка появится, когда подключат источник цен.",
    )


def catalog_stats(
    *,
    catalog_id: int,
    title: str,
    breakdowns: Sequence[ProfitBreakdown],
) -> CatalogProfitStats | None:
    if not breakdowns:
        return None
    nets = [item.net for item in breakdowns]
    rates = [
        item.per_person_per_hour
        for item in breakdowns
        if item.per_person_per_hour is not None
    ]
    people = [item.people for item in breakdowns]
    reputations = [
        item.reputation_delta
        for item in breakdowns
        if item.reputation_delta is not None
    ]
    return CatalogProfitStats(
        catalog_id=catalog_id,
        title=title,
        samples=len(breakdowns),
        median_net=integer_median(nets),
        median_per_person_per_hour=integer_median(rates) if rates else None,
        net_min=min(nets),
        net_max=max(nets),
        losing=sum(1 for item in nets if item < 0),
        people_median=integer_median(people),
        reputation_median=integer_median(reputations) if reputations else None,
    )


def iter_money_ints(breakdown: ProfitBreakdown) -> tuple[int, ...]:
    values = [
        breakdown.purchase_total,
        breakdown.expense_total,
        breakdown.costs,
        breakdown.cash,
        breakdown.item_valuation_total,
        breakdown.revenue,
        breakdown.net,
        breakdown.people,
        breakdown.duration_minutes,
    ]
    if breakdown.per_person is not None:
        values.append(breakdown.per_person)
    if breakdown.per_hour is not None:
        values.append(breakdown.per_hour)
    if breakdown.per_person_per_hour is not None:
        values.append(breakdown.per_person_per_hour)
    if breakdown.reputation_delta is not None:
        values.append(breakdown.reputation_delta)
    for purchase in breakdown.purchases:
        values.extend((purchase.quantity, purchase.unit_price, purchase.total))
    for expense in breakdown.expenses:
        values.append(expense.amount)
    for payout in breakdown.item_payouts:
        values.extend((payout.quantity, payout.valuation))
    return tuple(values)
