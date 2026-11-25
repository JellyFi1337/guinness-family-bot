"""Submit and review contract reports. Does not award contribution points."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from core.permissions import AccessDecision, Permission, evaluate_access
from core.settings import get_registry
from repositories.contracts import get_run
from repositories.members import get_current_member
from repositories.reports import (
    ContractReport,
    PayoutObligation,
    ReportExpense,
    ReportItemPayout,
    ReportPurchase,
    ReportShare,
    get_report,
    insert_submitted_report,
    latest_report_for_run,
    list_accepted_profit_reports,
    list_open_reports_for_member,
    list_purchase_history,
    reverse_accepted_report,
    try_set_review,
)
from services.audit import record_decision
from services.contracts import RUN_LABELS, snapshot_title
from services.profit import (
    CatalogProfitStats,
    ProfitBreakdown,
    ProfitError,
    PurchaseLine,
    allowed_profit_fields,
    catalog_stats,
    compute_profit,
    draft_from_form,
    draft_from_report,
    member_share,
    weighted_average_unit_price,
)
from services.member_panel import SECTION_REPORT, PanelItem, register_panel_source
from services.membership import can_take_internal_tasks, ensure_family
from ui.command_registry import cmd
from ui.design import Accent
from ui.message_specs import MessageSpec
from ui.templates import notice

SUBMITTABLE_RUN = frozenset({"in_work", "rework"})
REVIEW_TARGETS = {
    "accept": ("accepted", "accepted", True),
    "rework": ("rework", "rework", False),
    "reject": ("rejected", "rejected", False),
}


class ReportError(Exception):
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
class EvidenceDraft:
    kind: str
    payload: bytes
    url: str | None = None


def _actor_id(actor: Any) -> int:
    raw = getattr(actor, "id", 0)
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    return int(str(raw))


def _actor_name(actor: Any) -> str:
    return str(
        getattr(actor, "display_name", None)
        or getattr(actor, "name", None)
        or "unknown"
    )


def hash_evidence(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def normalize_link(url: str) -> str:
    text = url.strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ReportError(
            "Ссылка подтверждения должна быть http или https. "
            "Отчёт не создан; исправьте адрес.",
            code="REPORTS.BAD_LINK",
        )
    return text


def parse_shares(raw: str) -> tuple[ReportShare, ...]:
    items: list[ReportShare] = []
    seen: set[int] = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        if ":" not in piece:
            raise ReportError(
                "Доли пишутся как discord_id:сумма через запятую. "
                "Отчёт не создан.",
                code="REPORTS.SHARES",
            )
        ident, amount_text = piece.split(":", 1)
        try:
            discord_id = int(ident.strip())
            amount = int(amount_text.strip())
        except ValueError as exc:
            raise ReportError(
                "Доля должна быть целым числом. Отчёт не создан.",
                code="REPORTS.SHARES",
            ) from exc
        if amount < 0:
            raise ReportError(
                "Доля не может быть отрицательной. Отчёт не создан.",
                code="REPORTS.SHARES",
            )
        if discord_id in seen:
            raise ReportError(
                "Один участник указан дважды. Отчёт не создан.",
                code="REPORTS.SHARES",
            )
        seen.add(discord_id)
        items.append(ReportShare(discord_id=discord_id, amount=amount))
    return tuple(items)


async def require_review(actor: Any) -> None:
    decision = await evaluate_access(actor, Permission.REPORTS_REVIEW)
    if decision.allowed:
        return
    raise ReportError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


async def _worker(actor: Any, guild_id: int):
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, _actor_id(actor))
    if member is None:
        raise ReportError(
            "Вас нет в текущем составе. Отчёт не создан; обратитесь к кадровику.",
            code="REPORTS.NOT_A_MEMBER",
        )
    if not can_take_internal_tasks(member):
        raise ReportError(
            "Сдать отчёт можно после испытательного срока или "
            "подтверждения в игре. Запись не создана.",
            code="REPORTS.NOT_READY",
        )
    return family, member


async def _policies(guild_id: int) -> tuple[int, int]:
    registry = get_registry()
    threshold = await registry.get(guild_id, "REPORT_DETAIL_THRESHOLD")
    max_bytes = await registry.get(guild_id, "REPORT_EVIDENCE_MAX_BYTES")
    return int(threshold or 0), int(max_bytes or 0)


def needs_detailed(
    *,
    result_amount: int,
    wants_payout: bool,
    disputed: bool,
    threshold: int,
) -> bool:
    if disputed:
        return True
    if not wants_payout:
        return False
    if threshold <= 0:
        return True
    return result_amount >= threshold


async def submit_report(
    actor: Any,
    guild_id: int,
    run_id: int,
    *,
    result_amount: int,
    shares: tuple[ReportShare, ...] | str,
    duration_minutes: int,
    volumes: str | None = None,
    links: str | None = None,
    note: str | None = None,
    disputed: bool = False,
    wants_payout: bool = True,
    attachments: tuple[EvidenceDraft, ...] = (),
    purchases: str | None = None,
    expenses: str | None = None,
    cash: int | None = None,
    items: str | None = None,
    reputation: int | None = None,
) -> ContractReport:
    ensure_report_integrations()
    family, member = await _worker(actor, guild_id)
    if result_amount < 0 or duration_minutes < 0:
        raise ReportError(
            "Результат и время — неотрицательные целые. Отчёт не создан.",
            code="REPORTS.AMOUNT",
        )
    parsed = parse_shares(shares) if isinstance(shares, str) else shares
    if not parsed:
        raise ReportError(
            "Укажите хотя бы одну долю. Отчёт не создан.",
            code="REPORTS.SHARES",
        )
    if sum(item.amount for item in parsed) != result_amount:
        raise ReportError(
            "Сумма долей должна равняться результату группы. "
            "Отчёт не создан; исправьте числа.",
            code="REPORTS.SHARE_SUM",
        )
    run = await get_run(run_id)
    if run is None or run.family_id != family.id:
        raise ReportError(
            "Запуск не найден. Отчёт не создан.",
            code="REPORTS.NOT_FOUND",
        )
    participant_ids = {item.discord_id for item in run.participants}
    if member.discord_id not in participant_ids:
        raise ReportError(
            "Отчёт сдаёт участник запуска. Запись не создана.",
            code="REPORTS.NOT_IN_RUN",
        )
    unknown = [item.discord_id for item in parsed if item.discord_id not in participant_ids]
    if unknown:
        raise ReportError(
            "В долях есть человек вне запуска. Отчёт не создан.",
            code="REPORTS.SHARE_MEMBER",
        )
    latest = await latest_report_for_run(run.id)
    if latest is not None and latest.status == "submitted":
        return latest
    if latest is not None and latest.status == "accepted":
        raise ReportError(
            "По этому запуску уже есть принятый отчёт. Новый не создан. "
            "Исправление — только отменяющей записью.",
            code="REPORTS.ALREADY_ACCEPTED",
        )
    if run.status not in SUBMITTABLE_RUN:
        raise ReportError(
            f"Отчёт можно сдать, когда запуск «{RUN_LABELS.get(run.status, run.status)}» "
            "в работе или на доработке. Запись не создана.",
            code="REPORTS.BAD_STATUS",
        )
    threshold, max_bytes = await _policies(guild_id)
    detailed = needs_detailed(
        result_amount=result_amount,
        wants_payout=wants_payout,
        disputed=disputed,
        threshold=threshold,
    )
    evidence: list[tuple[str, str | None, str, int]] = []
    for url in (part.strip() for part in (links or "").split(",") if part.strip()):
        clean = normalize_link(url)
        evidence.append(("link", clean, hash_evidence(clean.encode("utf-8")), 0))
    for item in attachments:
        if max_bytes > 0 and len(item.payload) > max_bytes:
            raise ReportError(
                "Вложение больше допустимого размера. Отчёт не создан; "
                "сожмите файл или поднимите лимит в настройках.",
                code="REPORTS.TOO_LARGE",
            )
        evidence.append(
            (
                "attachment",
                item.url,
                hash_evidence(item.payload),
                len(item.payload),
            )
        )
    if detailed and not evidence:
        raise ReportError(
            "Для выплаты или спорного результата нужны ссылка или вложение. "
            "Отчёт не создан. Подтверждение не считается автоматическим доказательством.",
            code="REPORTS.NEED_EVIDENCE",
        )
    attempt = 1 if latest is None else latest.attempt + 1
    if latest is not None and latest.status in {"rework", "rejected"}:
        attempt = latest.attempt + 1
    volume_items = tuple(
        part.strip() for part in (volumes or "").split(",") if part.strip()
    )
    try:
        profit_draft = draft_from_form(
            purchases=purchases,
            expenses=expenses,
            items=items,
            cash=cash,
            reputation=reputation,
            allowed=allowed_profit_fields(run.snapshot),
        )
    except ProfitError as exc:
        raise ReportError(str(exc), code=exc.code) from exc
    report = await insert_submitted_report(
        guild_id=guild_id,
        family_id=family.id,
        run_id=run.id,
        attempt=attempt,
        result_amount=result_amount,
        result_note=(note or "").strip() or None,
        volumes=volume_items,
        duration_minutes=duration_minutes,
        wants_payout=wants_payout,
        disputed=disputed,
        detailed=detailed,
        submitted_by=member.discord_id,
        shares=parsed,
        evidence=tuple(evidence),
        run_status="report_submitted",
        purchases=tuple(
            ReportPurchase(
                item_name=line.item_name,
                quantity=line.quantity,
                unit_price=line.unit_price,
            )
            for line in profit_draft.purchases
        ),
        expenses=tuple(
            ReportExpense(label=line.label, amount=line.amount)
            for line in profit_draft.expenses
        ),
        item_payouts=tuple(
            ReportItemPayout(
                item_name=line.item_name,
                quantity=line.quantity,
                valuation=line.valuation,
            )
            for line in profit_draft.item_payouts
        ),
        cash_amount=profit_draft.cash_amount,
        reputation_delta=profit_draft.reputation_delta,
        has_profit=profit_draft.recorded,
    )
    await record_decision(
        guild_id=guild_id,
        action="REPORTS.SUBMIT",
        subject_type="contract_report",
        subject_id=str(report.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after="submitted",
        source="command",
    )
    return report


async def review_report(
    actor: Any,
    guild_id: int,
    report_id: int,
    decision: str,
    *,
    reason: str,
) -> ContractReport:
    await require_review(actor)
    target = REVIEW_TARGETS.get(decision)
    if target is None:
        raise ReportError(
            "Решение: accept, rework или reject. Запись не изменена.",
            code="REPORTS.DECISION",
        )
    text = reason.strip()
    if not text:
        raise ReportError(
            "Причина решения обязательна. Запись не изменена.",
            code="REPORTS.REASON",
        )
    report = await get_report(report_id)
    if report is None or report.guild_id != guild_id:
        raise ReportError(
            "Отчёт не найден. Решение не записано.",
            code="REPORTS.NOT_FOUND",
        )
    if report.status != "submitted":
        if report.status == "accepted" and decision == "accept":
            return report
        raise ReportError(
            "Этот отчёт уже разобран. Повторное решение не записано.",
            code="REPORTS.ALREADY_REVIEWED",
        )
    if decision == "accept" and _actor_id(actor) == report.submitted_by:
        raise ReportError(
            "Нельзя принять собственный отчёт. Решение не записано; "
            "передайте проверку другому уполномоченному.",
            code="REPORTS.SELF_REVIEW",
        )
    result = await try_set_review(
        report.id,
        status=target[0],
        expected_version=report.version,
        reviewer_id=_actor_id(actor),
        reason=text,
        run_status=target[1],
        create_obligation=target[2],
    )
    if result == "conflict":
        current = await get_report(report.id)
        if current is not None and current.status == "accepted" and decision == "accept":
            return current
        raise ReportError(
            "Отчёт только что разобрал другой проверяющий. "
            "Ваше решение не записано; обновите карточку.",
            code="REPORTS.CONFLICT",
            retryable=True,
        )
    if result == "already":
        current = await get_report(report.id)
        if current is not None:
            return current
        raise ReportError(
            "Отчёт уже разобран. Решение не записано.",
            code="REPORTS.ALREADY_REVIEWED",
        )
    if result == "missing":
        raise ReportError(
            "Отчёт не найден. Решение не записано.",
            code="REPORTS.NOT_FOUND",
        )
    updated = await get_report(report.id)
    if updated is None:
        raise ReportError(
            "Отчёт не найден после решения. Повторите просмотр.",
            code="REPORTS.NOT_FOUND",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action=f"REPORTS.{decision.upper()}",
        subject_type="contract_report",
        subject_id=str(report.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before="submitted",
        after=updated.status,
        reason=text,
        source="command",
    )
    if updated.status == "accepted":
        from services.contribution import award_from_accepted_report
        from services.treasury import create_from_accepted_report

        await create_from_accepted_report(actor, guild_id, updated.id)
        await award_from_accepted_report(actor, guild_id, updated.id)
    return updated


async def reverse_report(
    actor: Any,
    guild_id: int,
    report_id: int,
    *,
    reason: str,
) -> ContractReport:
    await require_review(actor)
    text = reason.strip()
    if not text:
        raise ReportError(
            "Причина отмены обязательна. Числа отчёта не изменены.",
            code="REPORTS.REASON",
        )
    report = await get_report(report_id)
    if report is None or report.guild_id != guild_id:
        raise ReportError(
            "Отчёт не найден. Отменяющая запись не создана.",
            code="REPORTS.NOT_FOUND",
        )
    result = await reverse_accepted_report(
        report.id, actor_id=_actor_id(actor), reason=text
    )
    if result == "missing":
        raise ReportError(
            "Отчёт не найден. Отменяющая запись не создана.",
            code="REPORTS.NOT_FOUND",
        )
    if result == "not_accepted":
        raise ReportError(
            "Отменить можно только принятый отчёт. Числа не менялись.",
            code="REPORTS.NOT_ACCEPTED",
        )
    updated = await get_report(report.id)
    if updated is None:
        raise ReportError(
            "Отчёт не найден после отмены. Повторите просмотр.",
            code="REPORTS.NOT_FOUND",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="REPORTS.REVERSE",
        subject_type="contract_report",
        subject_id=str(report.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before="accepted",
        after="reversed",
        reason=text,
        source="command",
    )
    from services.contribution import reverse_from_report
    from services.treasury import cancel_from_reversed_report

    await cancel_from_reversed_report(updated.id)
    await reverse_from_report(
        actor, guild_id, updated.id, reason=text
    )
    return updated


async def show_report(actor: Any, guild_id: int, report_id: int) -> ContractReport:
    report = await get_report(report_id)
    if report is None or report.guild_id != guild_id:
        raise ReportError(
            "Отчёт не найден. Ничего не показано.",
            code="REPORTS.NOT_FOUND",
        )
    if _actor_id(actor) == report.submitted_by:
        return report
    decision = await evaluate_access(actor, Permission.REPORTS_REVIEW)
    if decision.allowed:
        return report
    raise ReportError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


def breakdown_of(report: ContractReport) -> ProfitBreakdown | None:
    if not report.has_profit:
        return None
    return compute_profit(
        draft_from_report(report),
        people=max(len(report.shares), 1),
        duration_minutes=report.duration_minutes,
    )


async def _lookback(guild_id: int) -> int:
    registry = get_registry()
    raw = await registry.get(guild_id, "PROFIT_PURCHASE_LOOKBACK")
    return int(raw or 0)


async def catalog_profit_stats(
    family_id: int, catalog_id: int, title: str
) -> CatalogProfitStats | None:
    rows = await list_accepted_profit_reports(family_id, catalog_id)
    breakdowns = [item for item in (breakdown_of(row) for row in rows) if item]
    return catalog_stats(catalog_id=catalog_id, title=title, breakdowns=breakdowns)


async def family_profit_stats(family_id: int) -> tuple[CatalogProfitStats, ...]:
    from repositories.contracts import get_run

    rows = await list_accepted_profit_reports(family_id)
    grouped: dict[int, list[ProfitBreakdown]] = {}
    titles: dict[int, str] = {}
    for report in rows:
        run = await get_run(report.run_id)
        if run is None:
            continue
        piece = breakdown_of(report)
        if piece is None:
            continue
        grouped.setdefault(run.catalog_id, []).append(piece)
        titles[run.catalog_id] = snapshot_title(run)
    items = [
        catalog_stats(catalog_id=key, title=titles[key], breakdowns=values)
        for key, values in grouped.items()
    ]
    return tuple(item for item in items if item is not None)


def _stats_lines(stats: CatalogProfitStats) -> str:
    return (
        f"выборок {stats.samples} · медиана {stats.median_net} · "
        f"разброс {stats.net_min}…{stats.net_max} · "
        f"убыточных {stats.losing} из {stats.samples} · "
        f"людей (медиана) {stats.people_median}"
        + (
            f" · на человека в час {stats.median_per_person_per_hour}"
            if stats.median_per_person_per_hour is not None
            else ""
        )
        + (
            f" · репутация (медиана) {stats.reputation_median}"
            if stats.reputation_median is not None
            else ""
        )
    )


async def profit_details(
    report: ContractReport, *, viewer_id: int, staff: bool
) -> list[tuple[str, str]]:
    details: list[tuple[str, str]] = []
    breakdown = breakdown_of(report)
    if breakdown is None:
        return details
    share_row = next(
        (item for item in report.shares if item.discord_id == viewer_id), None
    )
    own_net = (
        member_share(breakdown.net, share_row.amount, report.result_amount)
        if share_row is not None
        else None
    )
    own_cost = (
        member_share(breakdown.costs, share_row.amount, report.result_amount)
        if share_row is not None
        else None
    )
    details.append(
        (
            "Расчёт семьи",
            f"затраты {breakdown.costs} · выручка {breakdown.revenue} "
            f"(деньги {breakdown.cash} + оценка предметов {breakdown.item_valuation_total}) "
            f"· чистая {breakdown.net}",
        )
    )
    if own_cost is not None and own_net is not None and share_row is not None:
        details.append(
            (
                "Ваша доля",
                f"вложено {own_cost} · доля прибыли {own_net} "
                f"потому что {share_row.amount} из результата {report.result_amount}",
            )
        )
    if breakdown.per_person is not None:
        details.append(("На человека", str(breakdown.per_person)))
    if breakdown.per_hour is not None:
        details.append(("В час", str(breakdown.per_hour)))
    if breakdown.reputation_delta is not None:
        details.append(
            (
                "Репутация",
                f"{breakdown.reputation_delta} — отдельно от денег, "
                "в «выгодно / невыгодно» не входит",
            )
        )
    if breakdown.item_payouts:
        items = ", ".join(
            f"{line.item_name} × {line.quantity} = {line.valuation} (оценка, не деньги)"
            for line in breakdown.item_payouts
        )
        details.append(("Предметы", items))
        from services.insights import lookup_market_price

        hint = await lookup_market_price(
            report.guild_id, breakdown.item_payouts[0].item_name
        )
        details.append(("Рынок", hint.note))
    if not staff:
        return details
    if breakdown.purchases:
        details.append(
            (
                "Закупки",
                ", ".join(
                    f"{line.item_name} × {line.quantity} по {line.unit_price}"
                    for line in breakdown.purchases
                ),
            )
        )
    if breakdown.expenses:
        details.append(
            (
                "Расходы",
                ", ".join(f"{line.label} {line.amount}" for line in breakdown.expenses),
            )
        )
    from repositories.contracts import get_run

    run = await get_run(report.run_id)
    if run is None:
        return details
    lookback = await _lookback(report.guild_id)
    history = await list_purchase_history(
        report.family_id,
        run.catalog_id,
        exclude_report_id=report.id,
        lookback=lookback,
    )
    averages: list[str] = []
    names = {
        line.item_name.casefold(): line.item_name for line in breakdown.purchases
    }
    for name in names.values():
        past = [
            PurchaseLine(
                item_name=item.item_name,
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
            for item in history
            if item.item_name.casefold() == name.casefold()
        ]
        avg = weighted_average_unit_price(past)
        current = next(
            (line.unit_price for line in breakdown.purchases if line.item_name.casefold() == name.casefold()),
            None,
        )
        if avg is None:
            averages.append(f"{name}: средней ещё нет")
        elif current is None:
            averages.append(f"{name}: средняя {avg}")
        elif current > avg:
            averages.append(f"{name}: сейчас {current}, средняя {avg} — дороже")
        elif current < avg:
            averages.append(f"{name}: сейчас {current}, средняя {avg} — дешевле")
        else:
            averages.append(f"{name}: сейчас {current}, как средняя {avg}")
    if averages:
        details.append(("Средняя закупка", "\n".join(averages)))
    stats = await catalog_profit_stats(
        report.family_id, run.catalog_id, snapshot_title(run)
    )
    if stats is not None:
        details.append((f"Сводка «{stats.title}»", _stats_lines(stats)))
    return details


async def report_card(
    report: ContractReport, actor: Any, *, staff: bool | None = None
) -> MessageSpec:
    if staff is None:
        decision = await evaluate_access(actor, Permission.REPORTS_REVIEW)
        staff = bool(decision.allowed)
    extra = await profit_details(
        report, viewer_id=_actor_id(actor), staff=bool(staff)
    )
    return report_spec(report, extra_details=tuple(extra))


async def show_family_profit(actor: Any, guild_id: int) -> MessageSpec:
    await require_review(actor)
    family = await ensure_family(guild_id)
    rows = await family_profit_stats(family.id)
    if not rows:
        return notice(
            code="PROFIT.STATS_EMPTY",
            title="Сводка по контрактам",
            summary=(
                "Принятых отчётов с расчётом ещё нет. "
                "Список лучших контрактов не зашит — он появится из ваших данных."
            ),
            next_step=f"Сдать отчёт с расчётом — {cmd('report submit')}.",
            accent=Accent.MEMBER,
        )
    details = tuple((f"#{item.catalog_id} {item.title}", _stats_lines(item)) for item in rows)
    return notice(
        code="PROFIT.STATS",
        title="Какие контракты брать",
        summary=(
            "Сводка по принятым отчётам семьи. Репутация не смешана с деньгами. "
            "Отдельного калькулятора нет — цифры те же, что в отчёте."
        ),
        details=details,
        next_step=f"Карточка запуска — {cmd('report show')}.",
        accent=Accent.MEMBER,
    )


def report_spec(
    report: ContractReport,
    extra_details: tuple[tuple[str, str], ...] = (),
) -> MessageSpec:
    shares = "\n".join(
        f"<@{item.discord_id}> · {item.amount}" for item in report.shares
    )
    evidence_lines = []
    for item in report.evidence:
        mark = (
            f"повтор (отчёты {', '.join(str(n) for n in item.duplicate_report_ids)})"
            if item.duplicate_report_ids
            else "новое"
        )
        evidence_lines.append(f"{item.kind} · {item.content_hash[:12]} · {mark}")
    details = [
        ("Запуск", str(report.run_id)),
        ("Результат группы", str(report.result_amount)),
        ("Время, мин", str(report.duration_minutes)),
        ("Доли", shares or "нет"),
        ("Статус", report.status),
        ("Подтверждения", "\n".join(evidence_lines) or "нет"),
    ]
    if report.volumes:
        details.append(("Объёмы", ", ".join(report.volumes)))
    if report.review_reason:
        details.append(("Причина решения", report.review_reason))
    if report.obligation is not None:
        details.append(
            (
                "Обязательство выплаты",
                f"#{report.obligation.id} · {report.obligation.status} · "
                f"{report.obligation.amount}",
            )
        )
    if report.reversals:
        last = report.reversals[-1]
        details.append(("Отмена", f"{last.reason} · {last.created_at}"))
    details.extend(extra_details)
    return notice(
        code="REPORTS.CARD",
        title=f"Отчёт #{report.id}",
        summary=(
            "Подтверждения не считаются автоматическим доказательством. "
            "Принятые числа не правятся — только отменяющая запись. "
            "Оценка предметов не считается полученными деньгами."
        ),
        details=tuple(details),
        next_step=(
            f"Проверка — {cmd('report review')}. "
            f"Отмена принятого — {cmd('report reverse')}."
        ),
        reference=str(report.id),
        accent=Accent.MEMBER,
    )


class ReportPanelSource:
    async def list_items(self, guild_id: int, member: Any) -> tuple[PanelItem, ...]:
        family = await ensure_family(guild_id)
        rows = await list_open_reports_for_member(family.id, member.discord_id)
        items = [
            PanelItem(
                f"Отчёт #{row.id}",
                f"запуск {row.run_id} · {row.status}",
                item_id=str(row.id),
            )
            for row in rows
        ]
        run_ids = {row.run_id for row in rows}
        from repositories.contracts import list_runs_for_member

        for run in await list_runs_for_member(family.id, member.discord_id):
            if run.status in SUBMITTABLE_RUN and run.id not in run_ids:
                items.append(
                    PanelItem(
                        snapshot_title(run),
                        f"нужен отчёт · запуск {run.id}",
                        item_id=f"run:{run.id}",
                    )
                )
        return tuple(items)


def ensure_report_integrations() -> None:
    register_panel_source(SECTION_REPORT, ReportPanelSource())


def obligation_of(report: ContractReport) -> PayoutObligation | None:
    return report.obligation


ensure_report_integrations()
