"""Staff work queue: live items from domain modules plus assignment overlay."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from core.permissions import AccessDecision, Permission, evaluate_access
from core.settings import get_registry
from repositories.members import Member, get_current_member, get_member
from repositories.queue import (
    CLAIM_METADATA_COLUMNS,
    QueueClaim,
    StaffAppeal,
    apply_escalation,
    get_appeal,
    get_claim,
    insert_appeal,
    list_claim_columns,
    list_claims,
    list_open_appeals,
    list_open_appeals_for_author,
    transfer_claim,
    try_claim,
)
from repositories.accounts import list_obligations
from repositories.reports import list_open_reports_for_member, list_submitted_report_refs
from repositories.requests import list_open_requests
from repositories.training import list_pending_proposals
from services.audit import record_decision
from services.member_panel import (
    SECTION_HELP,
    HelpSource,
    PanelItem,
    register_panel_source,
)
from services.membership import ensure_family
from ui.command_registry import cmd
from ui.message_specs import MessageSpec, MessageTone
from ui.templates import notice, staff_work_item

KIND_LABELS: dict[str, str] = {
    "report": "Отчёт",
    "application": "Заявка",
    "appeal": "Обращение",
    "help": "Помощь",
    "promotion": "Повышение",
    "resource": "Ресурс",
    "payout": "Выплата",
}
PERMISSION_BY_KIND: dict[str, Permission] = {
    "report": Permission.REPORTS_REVIEW,
    "application": Permission.APPLICATIONS_REVIEW,
    "appeal": Permission.QUEUE_ASSIGN,
    "help": Permission.QUEUE_ASSIGN,
    "promotion": Permission.TRAINING_MANAGE,
    "resource": Permission.INVENTORY_MANAGE,
    "payout": Permission.TREASURY_APPROVE,
}


class QueueError(Exception):
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
class SourceItem:
    kind: str
    item_id: str
    created_at: str
    subject_discord_id: int
    required_permission: Permission
    card_ref: str
    title: str
    specialization: str | None = None
    accused_id: int | None = None


@dataclass(frozen=True, slots=True)
class WorkItem:
    kind: str
    item_id: str
    created_at: str
    waiting_since: str
    subject_discord_id: int
    required_permission: Permission
    card_ref: str
    title: str
    specialization: str | None = None
    accused_id: int | None = None
    assignee_id: int | None = None
    claimed_at: str | None = None
    escalated_to: int | None = None
    delay_reason: str | None = None
    last_transfer_reason: str | None = None
    version: int = 0
    ping_user_id: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.item_id}"


@dataclass(frozen=True, slots=True)
class QueueSummary:
    open_count: int
    min_age_minutes: int | None
    median_age_minutes: int | None
    max_age_minutes: int | None
    load: tuple[tuple[int | None, int], ...]
    delay_reasons: tuple[str, ...]


class WorkItemSource(Protocol):
    kind: str

    async def list_open(
        self, guild_id: int, family_id: int
    ) -> Sequence[SourceItem]: ...

    async def get_item(
        self, guild_id: int, family_id: int, item_id: str
    ) -> SourceItem | None: ...


_SOURCES: dict[str, WorkItemSource] = {}


def _card_ref(kind: str) -> str:
    if kind == "report":
        return cmd("report show")
    if kind == "promotion":
        return cmd("training decide")
    if kind == "help":
        return cmd("help overview")
    if kind == "payout":
        return cmd("treasury obligation")
    if kind == "resource":
        return cmd("stock alerts")
    return cmd("queue show")


def parse_ref(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if ":" not in text:
        raise QueueError(
            "Ссылка пишется как тип:номер, например report:12. "
            "Запрос не выполнен; исправьте ссылку и повторите.",
            code="QUEUE.BAD_REF",
            retryable=True,
        )
    kind, item_id = text.split(":", 1)
    kind = kind.strip()
    item_id = item_id.strip()
    if kind not in KIND_LABELS or not item_id:
        raise QueueError(
            "Неизвестный тип или пустой номер элемента. "
            "Запрос не выполнен; исправьте ссылку и повторите.",
            code="QUEUE.BAD_REF",
            retryable=True,
        )
    return kind, item_id


def _parse_stamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def age_minutes(waiting_since: str, *, now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    delta = current - _parse_stamp(waiting_since)
    return max(0, int(delta.total_seconds() // 60))


def _actor_id(actor: Any) -> int:
    return int(actor.id)


def _actor_name(actor: Any) -> str:
    return str(getattr(actor, "display_name", None) or getattr(actor, "name", actor.id))


def _merge(source: SourceItem, claim: QueueClaim | None) -> WorkItem:
    return WorkItem(
        kind=source.kind,
        item_id=source.item_id,
        created_at=source.created_at,
        waiting_since=source.created_at,
        subject_discord_id=source.subject_discord_id,
        required_permission=source.required_permission,
        card_ref=source.card_ref,
        title=source.title,
        specialization=source.specialization,
        accused_id=source.accused_id,
        assignee_id=None if claim is None else claim.assignee_id,
        claimed_at=None if claim is None else claim.claimed_at,
        escalated_to=None if claim is None else claim.escalated_to,
        delay_reason=None if claim is None else claim.delay_reason,
        last_transfer_reason=None if claim is None else claim.last_transfer_reason,
        version=0 if claim is None else claim.version,
        ping_user_id=None if claim is None else claim.ping_user_id,
    )


def _kind_specialization(mapping: dict[str, str], kind: str) -> str | None:
    spec = mapping.get(kind)
    return spec or None


async def _kind_map(guild_id: int) -> dict[str, str]:
    raw = await get_registry().get(guild_id, "QUEUE_KIND_SPECIALIZATION")
    text = str(raw or "").strip()
    mapping: dict[str, str] = {}
    for chunk in text.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if ":" not in piece:
            mapping[piece] = ""
            continue
        kind, spec = piece.split(":", 1)
        mapping[kind.strip()] = spec.strip()
    return mapping


class ReportQueueSource:
    kind = "report"

    async def list_open(
        self, guild_id: int, family_id: int
    ) -> Sequence[SourceItem]:
        mapping = await _kind_map(guild_id)
        spec = _kind_specialization(mapping, self.kind)
        return tuple(
            SourceItem(
                kind=self.kind,
                item_id=str(report_id),
                created_at=submitted_at,
                subject_discord_id=author_id,
                required_permission=PERMISSION_BY_KIND[self.kind],
                card_ref=_card_ref(self.kind),
                title=f"Отчёт #{report_id}",
                specialization=spec,
            )
            for report_id, author_id, submitted_at in await list_submitted_report_refs(
                family_id
            )
        )

    async def get_item(
        self, guild_id: int, family_id: int, item_id: str
    ) -> SourceItem | None:
        for item in await self.list_open(guild_id, family_id):
            if item.item_id == item_id:
                return item
        return None


class ApplicationQueueSource:
    kind = "application"

    async def list_open(
        self, guild_id: int, family_id: int
    ) -> Sequence[SourceItem]:
        del family_id
        mapping = await _kind_map(guild_id)
        spec = _kind_specialization(mapping, self.kind)
        return tuple(
            SourceItem(
                kind=self.kind,
                item_id=row.number,
                created_at=row.created_at,
                subject_discord_id=row.user_id,
                required_permission=PERMISSION_BY_KIND[self.kind],
                card_ref=_card_ref(self.kind),
                title=f"Заявка {row.number}",
                specialization=spec,
            )
            for row in await list_open_requests(guild_id, ("family_invite",))
        )

    async def get_item(
        self, guild_id: int, family_id: int, item_id: str
    ) -> SourceItem | None:
        for item in await self.list_open(guild_id, family_id):
            if item.item_id == item_id:
                return item
        return None


class HelpQueueSource:
    kind = "help"

    async def list_open(
        self, guild_id: int, family_id: int
    ) -> Sequence[SourceItem]:
        del family_id
        return tuple(
            SourceItem(
                kind=self.kind,
                item_id=row.number,
                created_at=row.created_at,
                subject_discord_id=row.user_id,
                required_permission=PERMISSION_BY_KIND[self.kind],
                card_ref=_card_ref(self.kind),
                title=f"Помощь {row.number}",
            )
            for row in await list_open_requests(guild_id, ("help",))
        )

    async def get_item(
        self, guild_id: int, family_id: int, item_id: str
    ) -> SourceItem | None:
        for item in await self.list_open(guild_id, family_id):
            if item.item_id == item_id:
                return item
        return None


class AppealQueueSource:
    kind = "appeal"

    async def list_open(
        self, guild_id: int, family_id: int
    ) -> Sequence[SourceItem]:
        del guild_id
        return tuple(
            SourceItem(
                kind=self.kind,
                item_id=str(row.id),
                created_at=row.created_at,
                subject_discord_id=row.author_id,
                required_permission=PERMISSION_BY_KIND[self.kind],
                card_ref=_card_ref(self.kind),
                title=f"Обращение #{row.id}",
                accused_id=row.accused_id,
            )
            for row in await list_open_appeals(family_id)
        )

    async def get_item(
        self, guild_id: int, family_id: int, item_id: str
    ) -> SourceItem | None:
        del guild_id
        try:
            appeal_id = int(item_id)
        except ValueError:
            return None
        row = await get_appeal(appeal_id)
        if row is None or row.family_id != family_id or row.status != "open":
            return None
        return SourceItem(
            kind=self.kind,
            item_id=str(row.id),
            created_at=row.created_at,
            subject_discord_id=row.author_id,
            required_permission=PERMISSION_BY_KIND[self.kind],
            card_ref=_card_ref(self.kind),
            title=f"Обращение #{row.id}",
            accused_id=row.accused_id,
        )


class PromotionQueueSource:
    kind = "promotion"

    async def list_open(
        self, guild_id: int, family_id: int
    ) -> Sequence[SourceItem]:
        mapping = await _kind_map(guild_id)
        spec = _kind_specialization(mapping, self.kind)
        items: list[SourceItem] = []
        for row in await list_pending_proposals(family_id):
            member = await get_member(row.member_id)
            subject = 0 if member is None else member.discord_id
            direction = None if member is None else member.direction
            items.append(
                SourceItem(
                    kind=self.kind,
                    item_id=str(row.id),
                    created_at=row.created_at,
                    subject_discord_id=subject,
                    required_permission=PERMISSION_BY_KIND[self.kind],
                    card_ref=_card_ref(self.kind),
                    title=f"Предложение повышения #{row.id}",
                    specialization=spec or direction,
                )
            )
        return tuple(items)

    async def get_item(
        self, guild_id: int, family_id: int, item_id: str
    ) -> SourceItem | None:
        for item in await self.list_open(guild_id, family_id):
            if item.item_id == item_id:
                return item
        return None


class PayoutQueueSource:
    kind = "payout"

    async def list_open(
        self, guild_id: int, family_id: int
    ) -> Sequence[SourceItem]:
        mapping = await _kind_map(guild_id)
        spec = _kind_specialization(mapping, self.kind)
        return tuple(
            SourceItem(
                kind=self.kind,
                item_id=str(row.id),
                created_at=row.created_at,
                subject_discord_id=row.payee_id,
                required_permission=PERMISSION_BY_KIND[self.kind],
                card_ref=_card_ref(self.kind),
                title=f"Выплата #{row.id}",
                specialization=spec,
            )
            for row in await list_obligations(family_id, status="promised")
        )

    async def get_item(
        self, guild_id: int, family_id: int, item_id: str
    ) -> SourceItem | None:
        for item in await self.list_open(guild_id, family_id):
            if item.item_id == item_id:
                return item
        return None


class ResourceQueueSource:
    kind = "resource"

    async def list_open(
        self, guild_id: int, family_id: int
    ) -> Sequence[SourceItem]:
        from services.inventory import list_alert_items

        mapping = await _kind_map(guild_id)
        spec = _kind_specialization(mapping, self.kind)
        return tuple(
            SourceItem(
                kind=self.kind,
                item_id=item.item_id,
                created_at=item.created_at,
                subject_discord_id=item.subject_discord_id,
                required_permission=PERMISSION_BY_KIND[self.kind],
                card_ref=_card_ref(self.kind),
                title=item.title,
                specialization=spec,
            )
            for item in await list_alert_items(guild_id, family_id)
        )

    async def get_item(
        self, guild_id: int, family_id: int, item_id: str
    ) -> SourceItem | None:
        for item in await self.list_open(guild_id, family_id):
            if item.item_id == item_id:
                return item
        return None


class EmptyQueueSource:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    async def list_open(
        self, guild_id: int, family_id: int
    ) -> Sequence[SourceItem]:
        del guild_id, family_id
        return ()

    async def get_item(
        self, guild_id: int, family_id: int, item_id: str
    ) -> SourceItem | None:
        del guild_id, family_id, item_id
        return None


def register_queue_source(source: WorkItemSource) -> None:
    _SOURCES[source.kind] = source


def reset_queue_sources() -> None:
    _SOURCES.clear()
    _register_default_sources()


def _register_default_sources() -> None:
    register_queue_source(ReportQueueSource())
    register_queue_source(ApplicationQueueSource())
    register_queue_source(HelpQueueSource())
    register_queue_source(AppealQueueSource())
    register_queue_source(PromotionQueueSource())
    register_queue_source(ResourceQueueSource())
    register_queue_source(PayoutQueueSource())


async def _require(
    actor: Any, permission: Permission, *, code: str = "QUEUE.NO_PERMISSION"
) -> None:
    decision = await evaluate_access(actor, permission)
    if decision.allowed:
        return
    raise QueueError(
        decision.summary,
        code=decision.code if decision.code else code,
        retryable=False,
        decision=decision,
    )


def can_handle(actor_id: int, item: WorkItem | SourceItem) -> bool:
    return item.accused_id is None or int(item.accused_id) != int(actor_id)


def _deny_accused(actor_id: int, item: WorkItem | SourceItem) -> None:
    if can_handle(actor_id, item):
        return
    raise QueueError(
        "Жалоба на вас направляется другому уполномоченному. "
        "Назначение не выполнено; возьмите другую задачу или передайте коллеге.",
        code="QUEUE.ACCUSED",
        retryable=False,
    )


async def _resolve_source_item(
    guild_id: int, family_id: int, kind: str, item_id: str
) -> SourceItem:
    source = _SOURCES.get(kind)
    if source is None:
        raise QueueError(
            "Этот тип очереди ещё не подключён. "
            "Запрос не выполнен; повторять бессмысленно, пока модуль не появится.",
            code="QUEUE.KIND_UNKNOWN",
        )
    item = await source.get_item(guild_id, family_id, item_id)
    if item is None:
        raise QueueError(
            "Элемент не найден или уже закрыт в своём модуле. "
            "Очередь не хранит копию; откройте карточку заново.",
            code="QUEUE.NOT_FOUND",
            retryable=True,
        )
    return item


async def collect_items(
    guild_id: int,
    family_id: int,
    *,
    kind: str | None = None,
    assignee_id: int | None = None,
    unassigned: bool = False,
    age_min: int | None = None,
    now: datetime | None = None,
    prefer_direction: str | None = None,
) -> list[WorkItem]:
    claims = {
        (row.item_kind, row.item_id): row for row in await list_claims(family_id)
    }
    collected: list[WorkItem] = []
    if kind is not None:
        source = _SOURCES.get(kind)
        sources = [source] if source is not None else []
    else:
        sources = list(_SOURCES.values())
    for source in sources:
        for raw in await source.list_open(guild_id, family_id):
            item = _merge(raw, claims.get((raw.kind, raw.item_id)))
            if assignee_id is not None and item.assignee_id != assignee_id:
                continue
            if unassigned and item.assignee_id is not None:
                continue
            if age_min is not None and age_minutes(item.waiting_since, now=now) < age_min:
                continue
            collected.append(item)
    collected.sort(
        key=lambda item: (
            0
            if prefer_direction and item.specialization == prefer_direction
            else 1,
            item.waiting_since,
            item.kind,
            item.item_id,
        )
    )
    return collected


async def list_queue(
    actor: Any,
    guild_id: int,
    *,
    kind: str | None = None,
    assignee_id: int | None = None,
    unassigned: bool = False,
    age_min: int | None = None,
    now: datetime | None = None,
) -> list[WorkItem]:
    await _require(actor, Permission.QUEUE_ASSIGN)
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, _actor_id(actor))
    direction = None if member is None else member.direction
    return await collect_items(
        guild_id,
        family.id,
        kind=kind,
        assignee_id=assignee_id,
        unassigned=unassigned,
        age_min=age_min,
        now=now,
        prefer_direction=direction,
    )


async def show_item(actor: Any, guild_id: int, ref: str) -> WorkItem:
    kind, item_id = parse_ref(ref)
    family = await ensure_family(guild_id)
    source = await _resolve_source_item(guild_id, family.id, kind, item_id)
    item = _merge(source, await get_claim(family.id, kind, item_id))
    staff = await evaluate_access(actor, Permission.QUEUE_ASSIGN)
    if not staff.allowed and _actor_id(actor) != item.subject_discord_id:
        raise QueueError(
            staff.summary,
            code=staff.code,
            retryable=False,
            decision=staff,
        )
    return item


async def claim_item(actor: Any, guild_id: int, ref: str) -> WorkItem:
    await _require(actor, Permission.QUEUE_ASSIGN)
    kind, item_id = parse_ref(ref)
    family = await ensure_family(guild_id)
    source = await _resolve_source_item(guild_id, family.id, kind, item_id)
    await _require(actor, source.required_permission)
    _deny_accused(_actor_id(actor), source)
    claimed = await try_claim(
        guild_id=guild_id,
        family_id=family.id,
        item_kind=kind,
        item_id=item_id,
        assignee_id=_actor_id(actor),
    )
    if claimed is None:
        existing = await get_claim(family.id, kind, item_id)
        if existing is not None and existing.assignee_id == _actor_id(actor):
            claimed = existing
        else:
            raise QueueError(
                "Эту задачу уже взял другой человек. "
                "Повторное нажатие не создаёт второго ответственного. "
                "Откройте очередь и возьмите свободную задачу.",
                code="QUEUE.TAKEN",
                retryable=True,
            )
    item = _merge(source, claimed)
    await record_decision(
        guild_id=guild_id,
        action="QUEUE.CLAIMED",
        subject_type="queue_item",
        subject_id=item.ref,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=str(_actor_id(actor)),
        source="command",
    )
    return item


async def transfer_item(
    actor: Any,
    guild_id: int,
    ref: str,
    target: Any,
    reason: str,
) -> WorkItem:
    await _require(actor, Permission.QUEUE_ASSIGN)
    text = reason.strip()
    if not text:
        raise QueueError(
            "Передача без причины не записывается. "
            "Укажите причину и повторите.",
            code="QUEUE.REASON_REQUIRED",
            retryable=True,
        )
    if _actor_id(actor) == _actor_id(target):
        raise QueueError(
            "Передать задачу себе нельзя. "
            "Возьмите её в работу или укажите другого человека.",
            code="QUEUE.TRANSFER_SELF",
            retryable=True,
        )
    kind, item_id = parse_ref(ref)
    family = await ensure_family(guild_id)
    source = await _resolve_source_item(guild_id, family.id, kind, item_id)
    await _require(target, source.required_permission)
    _deny_accused(_actor_id(target), source)
    current = await get_claim(family.id, kind, item_id)
    expected = 0 if current is None else current.version
    if current is None:
        seeded = await try_claim(
            guild_id=guild_id,
            family_id=family.id,
            item_kind=kind,
            item_id=item_id,
            assignee_id=_actor_id(actor),
        )
        if seeded is None:
            raise QueueError(
                "Задачу успели взять параллельно. "
                "Откройте карточку и повторите передачу.",
                code="QUEUE.CONFLICT",
                retryable=True,
            )
        expected = seeded.version
    updated = await transfer_claim(
        family_id=family.id,
        item_kind=kind,
        item_id=item_id,
        actor_id=_actor_id(actor),
        to_id=_actor_id(target),
        reason=text,
        expected_version=expected,
    )
    if updated is None:
        raise QueueError(
            "Состояние очереди изменилось. "
            "Передача не записана; откройте карточку и повторите.",
            code="QUEUE.CONFLICT",
            retryable=True,
        )
    item = _merge(source, updated)
    await record_decision(
        guild_id=guild_id,
        action="QUEUE.TRANSFERRED",
        subject_type="queue_item",
        subject_id=item.ref,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=None if current is None else str(current.assignee_id),
        after=str(_actor_id(target)),
        reason=text,
        source="command",
    )
    return item


def _deputy_members(guild: Any, role_ids: tuple[int, ...]) -> list[Any]:
    if not role_ids:
        return []
    wanted = set(role_ids)
    found: list[Any] = []
    for member in getattr(guild, "members", ()):
        roles = getattr(member, "roles", ())
        if any(int(getattr(role, "id", 0)) in wanted for role in roles):
            found.append(member)
    return found


async def _pick_deputy(
    family_id: int, item: SourceItem, deputies: list[Any]
) -> int | None:
    candidates = [
        member for member in deputies if can_handle(_actor_id(member), item)
    ]
    if not candidates:
        return None
    if item.specialization:
        for member in candidates:
            row = await get_current_member(family_id, _actor_id(member))
            if row is not None and row.direction == item.specialization:
                return _actor_id(member)
    return _actor_id(candidates[0])


async def escalate_item(
    actor: Any,
    guild_id: int,
    ref: str,
    *,
    now: datetime | None = None,
) -> WorkItem:
    await _require(actor, Permission.QUEUE_ASSIGN)
    kind, item_id = parse_ref(ref)
    family = await ensure_family(guild_id)
    source = await _resolve_source_item(guild_id, family.id, kind, item_id)
    sla = int(await get_registry().get(guild_id, "QUEUE_SLA_MINUTES") or 0)
    if sla <= 0:
        raise QueueError(
            "Срок ответа не задан старшим составом. "
            "Автоэскалация не запускалась; наказание не применяется. "
            "Задайте QUEUE_SLA_MINUTES, если нужна эскалация по времени.",
            code="QUEUE.SLA_UNSET",
            retryable=False,
        )
    waited = age_minutes(source.created_at, now=now)
    if waited < sla:
        raise QueueError(
            "Срок ответа ещё не истёк. "
            "Эскалация не записана; дождитесь срока или передайте вручную.",
            code="QUEUE.NOT_DUE",
            retryable=True,
        )
    role_ids = tuple(await get_registry().get(guild_id, "QUEUE_DEPUTY_ROLE_IDS") or ())
    deputies = _deputy_members(getattr(actor, "guild", None), role_ids)
    deputy_id = await _pick_deputy(family.id, source, deputies)
    if deputy_id is None:
        delay = "заместитель не задан"
        claim = await apply_escalation(
            guild_id=guild_id,
            family_id=family.id,
            item_kind=kind,
            item_id=item_id,
            deputy_id=None,
            delay_reason=delay,
        )
    else:
        delay = "срок ответа истёк"
        claim = await apply_escalation(
            guild_id=guild_id,
            family_id=family.id,
            item_kind=kind,
            item_id=item_id,
            deputy_id=deputy_id,
            delay_reason=delay,
        )
    item = _merge(source, claim)
    await record_decision(
        guild_id=guild_id,
        action="QUEUE.ESCALATED",
        subject_type="queue_item",
        subject_id=item.ref,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=None if deputy_id is None else str(deputy_id),
        reason=delay,
        source="command",
    )
    return item


async def file_appeal(
    actor: Any, guild_id: int, accused_id: int, reason: str
) -> StaffAppeal:
    text = reason.strip()
    if not text:
        raise QueueError(
            "Обращение без причины не создаётся. "
            "Опишите ситуацию и повторите.",
            code="QUEUE.REASON_REQUIRED",
            retryable=True,
        )
    if accused_id <= 0:
        raise QueueError(
            "Укажите руководителя, на которого жалоба. "
            "Обращение не создано.",
            code="QUEUE.BAD_REF",
            retryable=True,
        )
    family = await ensure_family(guild_id)
    author = await get_current_member(family.id, _actor_id(actor))
    if author is None:
        raise QueueError(
            "Вас нет в текущем составе. Обращение не создано; "
            "обратитесь к кадровику.",
            code="QUEUE.NOT_A_MEMBER",
        )
    appeal = await insert_appeal(
        guild_id=guild_id,
        family_id=family.id,
        author_id=_actor_id(actor),
        accused_id=accused_id,
        reason=text,
    )
    await record_decision(
        guild_id=guild_id,
        action="QUEUE.APPEAL_FILED",
        subject_type="staff_appeal",
        subject_id=str(appeal.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=str(accused_id),
        reason=text,
        source="command",
    )
    return appeal


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


async def leader_summary(
    actor: Any, guild_id: int, *, now: datetime | None = None
) -> QueueSummary:
    await _require(actor, Permission.CONFIG_MANAGE)
    family = await ensure_family(guild_id)
    items = await collect_items(guild_id, family.id, now=now)
    ages = [age_minutes(item.waiting_since, now=now) for item in items]
    load: dict[int | None, int] = {}
    reasons: list[str] = []
    for item in items:
        load[item.assignee_id] = load.get(item.assignee_id, 0) + 1
        if item.delay_reason:
            reasons.append(item.delay_reason)
    return QueueSummary(
        open_count=len(items),
        min_age_minutes=min(ages) if ages else None,
        median_age_minutes=_median(ages),
        max_age_minutes=max(ages) if ages else None,
        load=tuple(sorted(load.items(), key=lambda pair: (pair[0] is None, pair[0] or 0))),
        delay_reasons=tuple(reasons),
    )


def work_item_spec(item: WorkItem, *, ping: bool = False) -> MessageSpec:
    waiting = age_minutes(item.waiting_since)
    assignee = "не назначен"
    if item.assignee_id is not None:
        assignee = f"<@{item.assignee_id}>"
    history_parts: list[str] = []
    if item.last_transfer_reason:
        history_parts.append(f"передача: {item.last_transfer_reason}")
    if item.delay_reason:
        history_parts.append(item.delay_reason)
    if item.escalated_to is not None:
        history_parts.append(f"эскалация <@{item.escalated_to}>")
    summary = f"{item.title}. Карточка объекта: {item.card_ref}."
    if ping and item.ping_user_id is not None:
        summary = f"{summary} Назначено: <@{item.ping_user_id}>."
    return staff_work_item(
        code="QUEUE.ITEM",
        title=item.title,
        number=item.ref,
        author=f"<@{item.subject_discord_id}>",
        category=KIND_LABELS.get(item.kind, item.kind),
        summary=summary,
        assignee=assignee,
        status="ожидает решения",
        deadline=f"{waiting} мин.",
        history="; ".join(history_parts) if history_parts else None,
    )


def queue_list_spec(items: Sequence[WorkItem]) -> MessageSpec:
    if not items:
        return notice(
            code="QUEUE.EMPTY",
            title="Требует решения",
            summary="Нет задач, которые ждут решения.",
            next_step=f"Новые объекты появятся из своих модулей. Обновить — {cmd('queue list')}.",
        )
    lines: list[tuple[str, str]] = []
    for item in items[:12]:
        owner = "свободно" if item.assignee_id is None else f"<@{item.assignee_id}>"
        lines.append(
            (
                item.ref,
                f"{item.title} · {owner} · {age_minutes(item.waiting_since)} мин. · {item.card_ref}",
            )
        )
    return notice(
        code="QUEUE.LIST",
        title="Требует решения",
        summary=(
            "Обзор живых объектов, не копия данных. "
            f"Показано {min(len(items), 12)} из {len(items)}."
        ),
        details=tuple(lines),
        next_step=(
            f"Взять в работу — {cmd('queue claim')} или кнопка на карточке. "
            f"Передача — {cmd('queue transfer')}."
        ),
    )


def summary_spec(summary: QueueSummary) -> MessageSpec:
    load_lines: list[tuple[str, str]] = []
    for assignee_id, count in summary.load:
        label = "свободные" if assignee_id is None else f"<@{assignee_id}>"
        load_lines.append((label, str(count)))
    ages = "нет открытых задач"
    if summary.max_age_minutes is not None:
        ages = (
            f"мин {summary.min_age_minutes} · "
            f"медиана {summary.median_age_minutes} · "
            f"макс {summary.max_age_minutes} мин."
        )
    delays = ", ".join(summary.delay_reasons) if summary.delay_reasons else "не записаны"
    return notice(
        code="QUEUE.SUMMARY",
        title="Сводка очереди",
        summary=f"Открытых задач: {summary.open_count}. Возраст: {ages}.",
        details=tuple(load_lines) + (("Причины задержек", delays),),
        next_step=f"Распределить работу — {cmd('queue list')}. Просрочка сама по себе не наказывает.",
        tone=MessageTone.NEUTRAL,
    )


def appeal_spec(appeal: StaffAppeal) -> MessageSpec:
    return notice(
        code="QUEUE.APPEAL",
        title=f"Обращение #{appeal.id}",
        summary=(
            "Жалоба направлена мимо указанного руководителя. "
            "Его нельзя назначить ответственным."
        ),
        details=(
            ("Заявитель", f"<@{appeal.author_id}>"),
            ("На кого", f"<@{appeal.accused_id}>"),
            ("Статус", appeal.status),
        ),
        next_step=f"Статус смотрите в {cmd('me')} или через {cmd('queue show')}.",
        reference=str(appeal.id),
    )


class HelpWithQueueSource:
    def __init__(self, inner: HelpSource) -> None:
        self._inner = inner

    async def list_items(self, guild_id: int, member: Member) -> Sequence[PanelItem]:
        base = list(await self._inner.list_items(guild_id, member))
        family = await ensure_family(guild_id)
        extra: list[PanelItem] = []
        for row in await list_open_requests(guild_id, ("family_invite", "help")):
            if row.user_id != member.discord_id:
                continue
            extra.append(
                PanelItem(
                    f"Заявка {row.number}",
                    f"{row.status}. Дальше — решение старшего состава.",
                    item_id=f"application:{row.number}",
                )
            )
        for report in await list_open_reports_for_member(family.id, member.discord_id):
            extra.append(
                PanelItem(
                    f"Отчёт #{report.id}",
                    f"{report.status}. Дальше — проверка через {cmd('report show')}.",
                    item_id=f"report:{report.id}",
                )
            )
        for appeal in await list_open_appeals_for_author(family.id, member.discord_id):
            claim = await get_claim(family.id, "appeal", str(appeal.id))
            nxt = "ожидание назначения"
            if claim is not None and claim.assignee_id is not None:
                nxt = f"рассматривает <@{claim.assignee_id}>"
            extra.append(
                PanelItem(
                    f"Обращение #{appeal.id}",
                    f"{appeal.status}. Дальше — {nxt}.",
                    item_id=f"appeal:{appeal.id}",
                )
            )
        return tuple(base + extra)


def overlay_is_metadata_only(columns: Sequence[str]) -> bool:
    return set(columns) == set(CLAIM_METADATA_COLUMNS)


async def claim_columns() -> tuple[str, ...]:
    return await list_claim_columns()


def ensure_queue_integrations() -> None:
    _register_default_sources()
    register_panel_source(SECTION_HELP, HelpWithQueueSource(HelpSource()))


ensure_queue_integrations()
