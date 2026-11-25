"""Catalog of Majestic family contracts and run lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.permissions import AccessDecision, Permission, evaluate_access
from core.profile import KNOWN_UPGRADES, active_profile
from repositories.contracts import (
    CATALOG_STATUSES,
    JOINABLE_RUN_STATUSES,
    SHARE_TOTAL_BPS,
    CatalogItem,
    ContractRun,
    get_catalog_item,
    get_run,
    insert_catalog_item,
    insert_run,
    join_run,
    list_catalog,
    list_open_runs,
    list_runs_for_member,
    set_catalog_status,
    set_run_status,
    snapshot_from_catalog,
    update_catalog_item,
)
from repositories.members import get_current_member
from services.audit import record_decision
from services.member_panel import (
    SECTION_TASKS,
    PanelItem,
    register_panel_source,
)
from services.membership import (
    OpenObligation,
    can_take_internal_tasks,
    ensure_family,
    ensure_obligation_source,
)
from ui.command_registry import cmd
from ui.design import Accent
from ui.message_specs import MessageSpec
from ui.templates import instruction_material, instruction_summary, notice, public_panel

DIRECTION_LABELS = {"legal": "легальное", "criminal": "криминальное"}
CATALOG_LABELS = {
    "draft": "черновик",
    "available": "доступен",
    "archived": "в архиве",
}
RUN_LABELS = {
    "draft": "черновик",
    "available": "доступен",
    "in_work": "в работе",
    "report_submitted": "отчёт подан",
    "accepted": "принят",
    "rework": "на доработке",
    "rejected": "отклонён",
    "closed": "закрыт",
    "cancelled": "отменён",
    "partial": "частично выполнен",
}
CATALOG_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"available", "archived"}),
    "available": frozenset({"archived"}),
    "archived": frozenset({"available"}),
}
RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"available", "cancelled"}),
    "available": frozenset({"in_work", "cancelled"}),
    "in_work": frozenset({"report_submitted", "cancelled", "partial"}),
    "report_submitted": frozenset({"accepted", "rework", "rejected"}),
    "rework": frozenset({"in_work", "report_submitted", "cancelled"}),
    "accepted": frozenset({"closed"}),
    "rejected": frozenset({"closed"}),
    "partial": frozenset({"closed"}),
    "cancelled": frozenset(),
    "closed": frozenset(),
}
TERMINAL_RUN = frozenset({"closed", "cancelled"})


class ContractError(Exception):
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
class CatalogAvailability:
    item: CatalogItem
    startable: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class CatalogView:
    items: tuple[CatalogAvailability, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StaffQueue:
    taken: tuple[str, ...]
    pending_reviews: tuple[str, ...]


def _actor_id(actor: Any) -> int:
    raw = getattr(actor, "id", 0)
    if raw is None:
        return 0
    return int(raw)


def _actor_name(actor: Any) -> str:
    return str(
        getattr(actor, "display_name", None)
        or getattr(actor, "name", None)
        or "unknown"
    )


def _csv_parts(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _share_label(share_bps: int) -> str:
    return f"{share_bps}/{SHARE_TOTAL_BPS}"


def snapshot_team_max(run: ContractRun) -> int:
    raw = run.snapshot.get("team_max", 0)
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 0


def snapshot_title(run: ContractRun) -> str:
    return str(run.snapshot.get("title") or f"запуск {run.id}")


def taken_share(run: ContractRun) -> int:
    return sum(item.share_bps for item in run.participants)


async def require_manage(actor: Any) -> None:
    decision = await evaluate_access(actor, Permission.CONTRACTS_MANAGE)
    if decision.allowed:
        return
    raise ContractError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


async def _current_worker(actor: Any, guild_id: int):
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, _actor_id(actor))
    if member is None:
        raise ContractError(
            "Вас нет в текущем составе. Запуск не изменён; "
            "обратитесь к кадровику.",
            code="CONTRACTS.NOT_A_MEMBER",
        )
    if not can_take_internal_tasks(member):
        raise ContractError(
            "Внутренние задачи доступны после испытательного срока "
            "или подтверждения в игре. Запуск не изменён.",
            code="CONTRACTS.NOT_READY",
            retryable=False,
        )
    return family, member


def missing_upgrades(item: CatalogItem, enabled: frozenset[str]) -> tuple[str, ...]:
    return tuple(key for key in item.required_upgrades if key not in enabled)


async def catalog_availability(
    guild_id: int, item: CatalogItem
) -> CatalogAvailability:
    profile = await active_profile(guild_id)
    missing = missing_upgrades(item, profile.enabled_upgrades)
    if item.status != "available":
        return CatalogAvailability(
            item,
            False,
            f"запись в статусе «{CATALOG_LABELS[item.status]}»",
        )
    if missing:
        return CatalogAvailability(
            item,
            False,
            "не открыто улучшение: " + ", ".join(missing),
        )
    return CatalogAvailability(item, True, None)


def _normalized_profit_fields(raw: str | None) -> str:
    from services.profit import ProfitError, parse_profit_fields

    text = (raw or "").strip()
    try:
        parse_profit_fields(text)
    except ProfitError as exc:
        raise ContractError(str(exc), code=exc.code) from exc
    return text


async def add_catalog_item(
    actor: Any,
    guild_id: int,
    *,
    title: str,
    direction: str,
    team_max: int,
    team_min: int = 1,
    upgrades: str | None = None,
    resources: str | None = None,
    instruction: str | None = None,
    verified_at: str | None = None,
    reward_note: str | None = None,
    profit_fields: str | None = None,
) -> CatalogItem:
    await require_manage(actor)
    family = await ensure_family(guild_id)
    name = title.strip()
    if not name:
        raise ContractError(
            "Название обязательно. Запись не создана; укажите название.",
            code="CONTRACTS.TITLE",
        )
    if direction not in {"legal", "criminal"}:
        raise ContractError(
            "Направление: legal или criminal. Запись не создана.",
            code="CONTRACTS.DIRECTION",
        )
    if team_min < 1 or team_max < team_min:
        raise ContractError(
            "Размер команды задаёт старший состав: минимум 1 и не больше максимума. "
            "Запись не создана.",
            code="CONTRACTS.TEAM",
        )
    required = _csv_parts(upgrades)
    unknown = tuple(key for key in required if key not in KNOWN_UPGRADES)
    if unknown:
        raise ContractError(
            "Неизвестный идентификатор улучшения: "
            + ", ".join(unknown)
            + ". Запись не создана; сверьте ключ с профилем семьи.",
            code="CONTRACTS.UNKNOWN_UPGRADE",
        )
    item = await insert_catalog_item(
        guild_id=guild_id,
        family_id=family.id,
        direction=direction,
        title=name,
        required_upgrades=required,
        required_resources=_csv_parts(resources),
        team_min=team_min,
        team_max=team_max,
        instruction_url=(instruction or "").strip() or None,
        verified_at=(verified_at or "").strip() or None,
        reward_note=(reward_note or "").strip() or None,
        created_by=_actor_id(actor),
        status="draft",
        profit_fields=_normalized_profit_fields(profit_fields),
    )
    await record_decision(
        guild_id=guild_id,
        action="CONTRACTS.ADD",
        subject_type="contract_catalog",
        subject_id=str(item.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after="draft",
        source="command",
    )
    return item


async def change_catalog_status(
    actor: Any, guild_id: int, catalog_id: int, status: str
) -> CatalogItem:
    await require_manage(actor)
    if status not in CATALOG_STATUSES:
        raise ContractError(
            "Недопустимый статус каталога. Запись не изменена.",
            code="CONTRACTS.STATUS",
        )
    item = await get_catalog_item(catalog_id)
    if item is None:
        raise ContractError(
            "Запись каталога не найдена. Ничего не изменено.",
            code="CONTRACTS.NOT_FOUND",
        )
    allowed = CATALOG_TRANSITIONS.get(item.status, frozenset())
    if status not in allowed:
        raise ContractError(
            f"Нельзя перевести каталог из «{CATALOG_LABELS[item.status]}» "
            f"в «{CATALOG_LABELS.get(status, status)}». Запись не изменена.",
            code="CONTRACTS.BAD_TRANSITION",
            retryable=False,
        )
    updated = await set_catalog_status(catalog_id, status)
    if updated is None:
        raise ContractError(
            "Запись каталога не найдена. Ничего не изменено.",
            code="CONTRACTS.NOT_FOUND",
        )
    await record_decision(
        guild_id=guild_id,
        action="CONTRACTS.CATALOG_STATUS",
        subject_type="contract_catalog",
        subject_id=str(catalog_id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=item.status,
        after=status,
        source="command",
    )
    return updated


async def edit_catalog_item(
    actor: Any,
    guild_id: int,
    catalog_id: int,
    **fields: Any,
) -> CatalogItem:
    await require_manage(actor)
    del guild_id
    item = await get_catalog_item(catalog_id)
    if item is None:
        raise ContractError(
            "Запись каталога не найдена. Ничего не изменено.",
            code="CONTRACTS.NOT_FOUND",
        )
    if "profit_fields" in fields:
        fields["profit_fields"] = _normalized_profit_fields(fields.get("profit_fields"))
    updated = await update_catalog_item(catalog_id, **fields)
    if updated is None:
        raise ContractError(
            "Запись каталога не найдена. Ничего не изменено.",
            code="CONTRACTS.NOT_FOUND",
        )
    return updated


async def build_catalog(actor: Any, guild_id: int) -> CatalogView:
    await require_manage(actor)
    family = await ensure_family(guild_id)
    items = await list_catalog(family.id, include_archived=True)
    rows: list[CatalogAvailability] = []
    lines: list[str] = []
    for item in items:
        row = await catalog_availability(guild_id, item)
        rows.append(row)
        reward = item.reward_note or "внутренняя награда требует проверки"
        gate = "можно запускать" if row.startable else row.reason
        lines.append(
            f"#{item.id} {item.title} · {DIRECTION_LABELS[item.direction]} · "
            f"{CATALOG_LABELS[item.status]} · команда {item.team_min}–{item.team_max} · "
            f"{reward} · {gate}"
        )
    return CatalogView(items=tuple(rows), lines=tuple(lines))


async def start_run(
    actor: Any,
    guild_id: int,
    catalog_id: int,
    *,
    share_bps: int | None = None,
) -> ContractRun:
    ensure_default_integrations()
    family, member = await _current_worker(actor, guild_id)
    item = await get_catalog_item(catalog_id)
    if item is None or item.family_id != family.id:
        raise ContractError(
            "Запись каталога не найдена. Запуск не создан.",
            code="CONTRACTS.NOT_FOUND",
        )
    availability = await catalog_availability(guild_id, item)
    if not availability.startable:
        raise ContractError(
            f"Контракт недоступен к запуску: {availability.reason}. "
            "Запуск не создан.",
            code="CONTRACTS.UNAVAILABLE",
            retryable=False,
        )
    if share_bps is None:
        share = SHARE_TOTAL_BPS if item.team_max == 1 else 0
    else:
        share = share_bps
    if share < 0 or share > SHARE_TOTAL_BPS:
        raise ContractError(
            f"Доля задаётся целым числом от 0 до {SHARE_TOTAL_BPS}. "
            "Запуск не создан.",
            code="CONTRACTS.SHARE",
        )
    snapshot = snapshot_from_catalog(item)
    run = await insert_run(
        guild_id=guild_id,
        family_id=family.id,
        catalog_id=item.id,
        snapshot=snapshot,
        responsible_discord_id=member.discord_id,
        share_bps=share,
        issued_resources=item.required_resources,
        status="in_work",
    )
    await record_decision(
        guild_id=guild_id,
        action="CONTRACTS.START",
        subject_type="contract_run",
        subject_id=str(run.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after="in_work",
        source="command",
    )
    return run


async def take_run(
    actor: Any,
    guild_id: int,
    run_id: int,
    *,
    share_bps: int,
) -> ContractRun:
    ensure_default_integrations()
    family, member = await _current_worker(actor, guild_id)
    if share_bps < 0 or share_bps > SHARE_TOTAL_BPS:
        raise ContractError(
            f"Доля задаётся целым числом от 0 до {SHARE_TOTAL_BPS}. "
            "Состав не изменён.",
            code="CONTRACTS.SHARE",
        )
    run = await get_run(run_id)
    if run is None or run.family_id != family.id:
        raise ContractError(
            "Запуск не найден. Состав не изменён.",
            code="CONTRACTS.NOT_FOUND",
        )
    if run.status not in JOINABLE_RUN_STATUSES:
        raise ContractError(
            f"К запуску нельзя присоединиться в статусе "
            f"«{RUN_LABELS[run.status]}». Состав не изменён.",
            code="CONTRACTS.BAD_TRANSITION",
            retryable=False,
        )
    team_max = snapshot_team_max(run)
    result = await join_run(
        run_id=run.id,
        guild_id=guild_id,
        family_id=family.id,
        discord_id=member.discord_id,
        share_bps=share_bps,
        team_max=team_max,
    )
    messages = {
        "full": (
            "Мест в команде больше нет. Состав не изменён.",
            "CONTRACTS.TEAM_FULL",
            False,
        ),
        "duplicate": (
            "Вы уже в этом запуске. Состав не изменён.",
            "CONTRACTS.ALREADY_IN",
            False,
        ),
        "share": (
            "Сумма долей не может превышать полную выручку. "
            "Состав не изменён; укажите меньшую долю.",
            "CONTRACTS.SHARE_EXCEEDED",
            False,
        ),
        "closed": (
            "Запуск уже недоступен для присоединения. Состав не изменён.",
            "CONTRACTS.BAD_TRANSITION",
            False,
        ),
        "missing": (
            "Запуск не найден. Состав не изменён.",
            "CONTRACTS.NOT_FOUND",
            False,
        ),
        "conflict": (
            "Состав только что изменился. Ничего не записано; повторите.",
            "CONTRACTS.CONFLICT",
            True,
        ),
    }
    if result != "ok":
        summary, code, retryable = messages[result]
        raise ContractError(summary, code=code, retryable=retryable)
    joined = await get_run(run.id)
    if joined is None:
        raise ContractError(
            "Запуск не найден после присоединения. Повторите просмотр.",
            code="CONTRACTS.NOT_FOUND",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="CONTRACTS.TAKE",
        subject_type="contract_run",
        subject_id=str(run.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=str(member.discord_id),
        source="command",
    )
    return joined


async def transition_run(
    actor: Any,
    guild_id: int,
    run_id: int,
    status: str,
    *,
    reason: str | None = None,
) -> ContractRun:
    await require_manage(actor)
    if status not in RUN_TRANSITIONS:
        raise ContractError(
            "Неизвестный статус запуска. Запись не изменена.",
            code="CONTRACTS.STATUS",
        )
    run = await get_run(run_id)
    if run is None:
        raise ContractError(
            "Запуск не найден. Запись не изменена.",
            code="CONTRACTS.NOT_FOUND",
        )
    allowed = RUN_TRANSITIONS.get(run.status, frozenset())
    if status not in allowed:
        raise ContractError(
            f"Нельзя перевести запуск из «{RUN_LABELS[run.status]}» "
            f"в «{RUN_LABELS.get(status, status)}». Запись не изменена.",
            code="CONTRACTS.BAD_TRANSITION",
            retryable=False,
        )
    finished = status in {"closed", "cancelled", "partial"}
    updated = await set_run_status(run.id, status, finished=finished)
    if updated is None:
        raise ContractError(
            "Запуск изменился одновременно. Запись не обновлена; повторите.",
            code="CONTRACTS.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="CONTRACTS.TRANSITION",
        subject_type="contract_run",
        subject_id=str(run.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=run.status,
        after=status,
        reason=reason,
        source="command",
    )
    return updated


async def my_runs(actor: Any, guild_id: int) -> tuple[ContractRun, ...]:
    family, _member = await _current_worker(actor, guild_id)
    return tuple(await list_runs_for_member(family.id, _actor_id(actor)))


def run_share_lines(run: ContractRun) -> tuple[str, ...]:
    return tuple(
        f"<@{item.discord_id}> · доля {_share_label(item.share_bps)}"
        for item in run.participants
    )


async def staff_overview(guild_id: int) -> StaffQueue:
    family = await ensure_family(guild_id)
    runs = await list_open_runs(family.id)
    taken: list[str] = []
    pending: list[str] = []
    now = datetime.now(UTC)
    for run in runs:
        title = snapshot_title(run)
        people = ", ".join(f"<@{item.discord_id}>" for item in run.participants)
        taken.append(
            f"#{run.id} {title} · {RUN_LABELS[run.status]} · {people or 'никого'}"
        )
        if run.status == "report_submitted":
            submitted = _parse_stamp(run.updated_at)
            wait = "время неизвестно"
            if submitted is not None:
                hours = max(0, int((now - submitted).total_seconds() // 3600))
                wait = f"{hours} ч"
            pending.append(f"#{run.id} {title} · ждёт проверки {wait}")
    return StaffQueue(taken=tuple(taken), pending_reviews=tuple(pending))


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def compose_public_panel(
    rows: tuple[CatalogAvailability, ...],
    runs: tuple[ContractRun, ...],
) -> MessageSpec:
    rules: list[tuple[str, str]] = []
    for row in rows:
        if row.item.status == "archived":
            continue
        item = row.item
        reward = item.reward_note or "требует проверки старшим составом"
        prep = ", ".join(item.required_resources) or "ресурсы не указаны"
        upgrades = ", ".join(item.required_upgrades) or "без улучшения"
        gate = "можно взять" if row.startable else row.reason
        rules.append(
            (
                item.title,
                (
                    f"{DIRECTION_LABELS[item.direction]} · "
                    f"команда {item.team_min}–{item.team_max} · "
                    f"подготовка: {prep} · улучшения: {upgrades} · "
                    f"награда: {reward} · {gate}"
                ),
            )
        )
    open_runs = [run for run in runs if run.status in JOINABLE_RUN_STATUSES]
    if open_runs:
        seats = []
        for run in open_runs:
            team_max = snapshot_team_max(run)
            seats.append(
                f"{snapshot_title(run)} #{run.id}: "
                f"{len(run.participants)}/{team_max}"
            )
        rules.append(("Открытые запуски", "; ".join(seats)))
    if not rules:
        rules.append(
            (
                "Каталог",
                "Старший состав ещё не заполнил доступные контракты. "
                "Списки и награды в код не зашиты.",
            )
        )
    rules.append(
        (
            "Инструкции",
            "Правила конкретной работы прикреплены к задаче во вкладке «Мои задачи»: "
            "тема, сервер, источник, дата проверки, ответственный, версия. "
            "Эта панель их не дублирует.",
        )
    )
    return public_panel(
        code="PANEL.CONTRACTS",
        title="Контракты семьи",
        purpose=(
            "Доступные семейные контракты Majestic. "
            "Запуск фиксирует условия; правка каталога уже идущий запуск не меняет."
        ),
        rules=tuple(rules),
        action=(
            f"Запуск — {cmd('contract start')}, присоединиться — "
            f"{cmd('contract take')}, свои — {cmd('contract my')}."
        ),
    )


async def build_public_panel(guild_id: int) -> MessageSpec:
    ensure_default_integrations()
    family = await ensure_family(guild_id)
    items = await list_catalog(family.id, include_archived=False)
    rows = tuple([await catalog_availability(guild_id, item) for item in items])
    runs = tuple(await list_open_runs(family.id))
    return compose_public_panel(rows, runs)


def catalog_spec(view: CatalogView, queue: StaffQueue) -> MessageSpec:
    details = [("Каталог", "\n".join(view.lines) if view.lines else "пусто")]
    if queue.taken:
        details.append(("Кто что взял", "\n".join(queue.taken)))
    if queue.pending_reviews:
        details.append(("Проверки", "\n".join(queue.pending_reviews)))
    else:
        details.append(("Проверки", "Ожидающих отчётов нет."))
    return notice(
        code="CONTRACTS.CATALOG",
        title="Каталог контрактов",
        summary=(
            "Черновики и архив видит старший состав. "
            "Награды и кулдауны в код не зашиты."
        ),
        details=tuple(details),
        next_step=(
            f"Новая запись — {cmd('contract catalog')} с названием. "
            f"Запуск — {cmd('contract start')}."
        ),
        accent=Accent.MEMBER,
    )


def _instruction_fields(run: ContractRun, *, server: str | None = None) -> tuple[tuple[str, str], ...]:
    snap = run.snapshot
    return instruction_material(
        topic=snapshot_title(run),
        server=server or str(snap.get("server_label") or ""),
        source=str(snap.get("instruction_url") or "") or None,
        verified_at=str(snap.get("verified_at") or "") or None,
        responsible=f"<@{run.responsible_discord_id}>",
        version=str(snap.get("lore_revision") or "") or None,
    )


def run_spec(run: ContractRun, *, title: str, summary: str) -> MessageSpec:
    return notice(
        code="CONTRACTS.RUN",
        title=title,
        summary=summary,
        details=(
            ("Контракт", snapshot_title(run)),
            ("Статус", RUN_LABELS[run.status]),
            ("Доли", "\n".join(run_share_lines(run)) or "никого"),
            (
                "Снимок условий",
                f"команда до {snapshot_team_max(run)}, "
                f"улучшения: {', '.join(run.snapshot.get('required_upgrades') or []) or 'нет'}",
            ),
        )
        + _instruction_fields(run),
        next_step=(
            f"Свои запуски — {cmd('contract my')}. "
            "Условия этого запуска больше не меняются правкой каталога."
        ),
        reference=str(run.id),
        accent=Accent.MEMBER,
    )


class ContractObligationSource:
    async def list_open(self, guild_id: int, discord_id: int) -> list[OpenObligation]:
        family = await ensure_family(guild_id)
        runs = await list_runs_for_member(family.id, discord_id, open_only=True)
        items: list[OpenObligation] = []
        for run in runs:
            if run.status in TERMINAL_RUN:
                continue
            items.append(
                OpenObligation(
                    kind="contract",
                    subject_id=str(run.id),
                    summary=(
                        f"Контракт «{snapshot_title(run)}» "
                        f"({RUN_LABELS[run.status]})"
                    ),
                    outstanding=True,
                    needs_reconciliation=run.status
                    in {"in_work", "report_submitted", "rework", "partial"},
                )
            )
        return items


class ContractTaskSource:
    async def list_items(self, guild_id: int, member: Any) -> tuple[PanelItem, ...]:
        family = await ensure_family(guild_id)
        profile = await active_profile(guild_id)
        runs = await list_runs_for_member(family.id, member.discord_id, open_only=True)
        items: list[PanelItem] = []
        for run in runs:
            if run.status in TERMINAL_RUN:
                continue
            share = _share_label(
                next(
                    (
                        part.share_bps
                        for part in run.participants
                        if part.discord_id == member.discord_id
                    ),
                    0,
                )
            )
            meta = instruction_summary(
                topic=snapshot_title(run),
                server=profile.server_label,
                source=str(run.snapshot.get("instruction_url") or "") or None,
                verified_at=str(run.snapshot.get("verified_at") or "") or None,
                responsible=f"<@{run.responsible_discord_id}>",
                version=str(run.snapshot.get("lore_revision") or "") or None,
            )
            items.append(
                PanelItem(
                    snapshot_title(run),
                    f"{RUN_LABELS[run.status]} · доля {share}. {meta}",
                    item_id=str(run.id),
                )
            )
        return tuple(items)


def ensure_default_integrations() -> None:
    ensure_obligation_source(ContractObligationSource())
    register_panel_source(SECTION_TASKS, ContractTaskSource())


ensure_default_integrations()
