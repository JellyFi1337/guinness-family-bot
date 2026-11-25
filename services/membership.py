"""Roster lifecycle: register, trial, in-game verify, leave. No Discord-rank sync."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from core.permissions import AccessDecision, Permission, evaluate_access
from core.profile import active_profile
from infrastructure.database import DATABASE_INTEGRITY_ERRORS
from repositories.family import (
    FamilyContext,
    Rank,
    create_family_context,
    create_rank,
    get_active_family,
    list_ranks,
)
from repositories.members import (
    MEMBER_STATUSES,
    Clearance,
    Member,
    MemberProfile,
    RosterLimit,
    add_member,
    check_roster_limit,
    get_current_member,
    get_member,
    get_member_profile,
    list_person_history,
    list_roster,
    mark_exit,
    set_member_status,
    verify_member as _verify_member,
)
from services.audit import list_decisions, record_decision

STATUS_LABELS: dict[str, str] = {
    "candidate": "Кандидат",
    "probation": "Испытательный срок",
    "active": "В составе",
    "leave": "Отпуск",
    "left": "Вышел",
}
SORT_LABELS: dict[str, str] = {
    "name": "Имя",
    "joined": "Дата вступления",
    "status": "Статус",
    "rank": "Должность",
}
_STATUS_FILTERS = ("all", "candidate", "probation", "active", "leave", "left")


class MembershipError(ValueError):
    """Domain refusal with a stable code for the Discord surface."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = True,
        decision: AccessDecision | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.decision = decision


@dataclass(frozen=True, slots=True)
class OpenObligation:
    kind: str
    subject_id: str
    summary: str
    needs_reconciliation: bool = False
    outstanding: bool = False
    action: str | None = None
    reference: str | None = None


class ObligationSource(Protocol):
    async def list_open(self, guild_id: int, discord_id: int) -> list[OpenObligation]: ...


@dataclass(frozen=True, slots=True)
class LimitNotice:
    current: int
    limit: int | None
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class RosterView:
    family: FamilyContext
    members: tuple[Member, ...]
    lines: tuple[str, ...]
    ranks: tuple[Rank, ...]
    limit: LimitNotice
    filters: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class MemberCard:
    profile: MemberProfile
    discord_name: str
    tenure_days: int
    verified: bool
    obligations: tuple[OpenObligation, ...]
    decisions: tuple[str, ...]
    can_take_internal_tasks: bool


@dataclass(frozen=True, slots=True)
class LeaveResult:
    member: Member
    obligations: tuple[OpenObligation, ...]
    tasks_closed: bool
    reserves_released: bool


@dataclass(frozen=True, slots=True)
class PromotePreview:
    members: tuple[Member, ...]
    lines: tuple[str, ...]


class CarBookingObligationSource:
    async def list_open(self, guild_id: int, discord_id: int) -> list[OpenObligation]:
        del guild_id
        from repositories.cars import get_active_user_car_bookings

        rows = await get_active_user_car_bookings(discord_id)
        items: list[OpenObligation] = []
        for row in rows:
            name = str(row[1])
            plate = str(row[2])
            items.append(
                OpenObligation(
                    kind="booking",
                    subject_id=str(row[0]),
                    summary=f"Бронь {name} ({plate}) до {row[3]}",
                    needs_reconciliation=True,
                    outstanding=True,
                )
            )
        return items


_OBLIGATION_SOURCES: list[ObligationSource] = [CarBookingObligationSource()]


def register_obligation_source(source: ObligationSource) -> None:
    _OBLIGATION_SOURCES.append(source)


def ensure_obligation_source(source: ObligationSource) -> None:
    if any(type(existing) is type(source) for existing in _OBLIGATION_SOURCES):
        return
    _OBLIGATION_SOURCES.append(source)


def reset_obligation_sources() -> None:
    _OBLIGATION_SOURCES.clear()
    _OBLIGATION_SOURCES.append(CarBookingObligationSource())


def can_take_internal_tasks(member: Member) -> bool:
    return member.status in {"probation", "active"}


def _actor_name(actor: Any) -> str:
    return str(
        getattr(actor, "display_name", None)
        or getattr(actor, "name", None)
        or "unknown"
    )


def _actor_id(actor: Any) -> int:
    return int(getattr(actor, "id", 0) or 0)


async def require_permission(actor: Any, permission: Permission) -> None:
    decision = await evaluate_access(actor, permission)
    if decision.allowed:
        return
    raise MembershipError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


async def ensure_family(guild_id: int) -> FamilyContext:
    existing = await get_active_family(guild_id)
    if existing is not None:
        return existing
    profile = await active_profile(guild_id)
    return await create_family_context(
        guild_id=guild_id,
        project=profile.project,
        server_key=profile.server_key,
        family_name=profile.family_name,
        family_tag=profile.family_tag,
        direction=profile.direction,
        member_limit=profile.member_limit,
    )


def describe_limit(limit: RosterLimit) -> LimitNotice:
    current = limit.current
    configured = limit.limit
    if configured is None:
        return LimitNotice(
            current=current,
            limit=None,
            kind="unset",
            message=(
                f"Сейчас в составе {current}. Игровой предел не задан "
                "в профиле и не подставляется."
            ),
        )
    if current >= configured:
        return LimitNotice(
            current=current,
            limit=configured,
            kind="reached",
            message=(
                f"Состав {current} из {configured}: предел достигнут или превышен. "
                "Новых людей заводите только после решения кадровика."
            ),
        )
    if current + 1 >= configured:
        return LimitNotice(
            current=current,
            limit=configured,
            kind="approaching",
            message=(
                f"Состав {current} из {configured}: следующий человек закроет предел."
            ),
        )
    return LimitNotice(
        current=current,
        limit=configured,
        kind="ok",
        message=f"Состав {current} из {configured}.",
    )


async def roster_limit_notice(guild_id: int, family_id: int) -> LimitNotice:
    return describe_limit(await check_roster_limit(guild_id, family_id))


async def _resolve_rank(
    *,
    guild_id: int,
    family_id: int,
    rank_name: str | None,
) -> Rank | None:
    if not rank_name or not rank_name.strip():
        return None
    wanted = rank_name.strip()
    for rank in await list_ranks(family_id):
        if rank.name.casefold() == wanted.casefold():
            return rank
    return await create_rank(
        guild_id=guild_id,
        family_id=family_id,
        name=wanted,
        sort_order=len(await list_ranks(family_id)) + 1,
    )


async def _mentor_row(
    family_id: int, mentor_discord_id: int | None
) -> Member | None:
    if mentor_discord_id is None:
        return None
    mentor = await get_current_member(family_id, mentor_discord_id)
    if mentor is None:
        raise MembershipError(
            "Наставник не найден в текущем составе. Участник не заведён; "
            "укажите действующего наставника или оставьте поле пустым.",
            code="MEMBERS.MENTOR_UNKNOWN",
        )
    return mentor


async def list_open_obligations(guild_id: int, discord_id: int) -> list[OpenObligation]:
    found: list[OpenObligation] = []
    for source in _OBLIGATION_SOURCES:
        found.extend(await source.list_open(guild_id, discord_id))
    return found


async def build_roster(
    actor: Any,
    guild_id: int,
    *,
    status: str | None = None,
    rank_id: int | None = None,
    direction: str | None = None,
    mentor_id: int | None = None,
    prime_time: str | None = None,
    sort: str = "name",
) -> RosterView:
    await require_permission(actor, Permission.MEMBERS_VIEW)
    if status and status != "all" and status not in MEMBER_STATUSES:
        raise MembershipError(
            "Неизвестный статус фильтра. Состав не изменён; выберите статус "
            "из списка команды.",
            code="MEMBERS.BAD_FILTER",
        )
    if sort not in SORT_LABELS:
        sort = "name"
    family = await ensure_family(guild_id)
    chosen_status = None if status in {None, "", "all"} else status
    members = await list_roster(
        family.id,
        status=chosen_status,
        rank_id=rank_id,
        direction=direction or None,
        mentor_id=mentor_id,
        prime_time=prime_time or None,
        include_left=chosen_status == "left",
        sort=sort,
    )
    ranks = {rank.id: rank for rank in await list_ranks(family.id)}
    lines: list[str] = []
    for member in members:
        rank_name = ranks[member.rank_id].name if member.rank_id in ranks else "—"
        spec = member.direction or "—"
        lines.append(
            f"<@{member.discord_id}> · {member.ic_name} · "
            f"{STATUS_LABELS[member.status]} · {rank_name} · {spec}"
        )
    notice = await roster_limit_notice(guild_id, family.id)
    if not lines:
        lines.append("По заданным фильтрам никого нет.")
    lines.append(notice.message)
    return RosterView(
        family=family,
        members=tuple(members),
        lines=tuple(lines),
        ranks=tuple(ranks.values()),
        limit=notice,
        filters=(
            ("status", chosen_status or "all"),
            ("sort", sort),
        ),
    )


def _tenure_days(joined_at: str) -> int:
    try:
        joined = datetime.fromisoformat(joined_at)
    except ValueError:
        return 0
    if joined.tzinfo is None:
        joined = joined.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - joined).days)


def _clearance_line(item: Clearance) -> str:
    if item.revoked_at:
        return f"{item.activity}: отозван {item.revoked_at}"
    until = item.expires_at or "бессрочно"
    return f"{item.activity}: до {until}"


async def build_card(
    actor: Any,
    guild_id: int,
    discord_id: int,
    *,
    discord_name: str,
) -> MemberCard:
    await require_permission(actor, Permission.MEMBERS_VIEW)
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, discord_id, selected_only=False)
    if member is None:
        history = await list_person_history(family.id, discord_id)
        if not history:
            raise MembershipError(
                "Этот человек не найден в реестре семьи. Карточка не собрана; "
                "заведите участника или проверьте, что указан нужный аккаунт.",
                code="MEMBERS.NOT_FOUND",
            )
        member = await get_member(history[-1].member_id)
        if member is None:
            raise MembershipError(
                "История есть, но запись участника не прочитана. "
                "Карточка не собрана; повторите запрос.",
                code="MEMBERS.NOT_FOUND",
            )
    profile = await get_member_profile(member.id)
    if profile is None:
        raise MembershipError(
            "Не удалось собрать профиль участника. Данные не изменились; повторите.",
            code="MEMBERS.NOT_FOUND",
        )
    obligations = tuple(await list_open_obligations(guild_id, discord_id))
    rows = await list_decisions(
        guild_id,
        subject_type="member",
        subject_id=str(member.id),
        limit=5,
    )
    decisions = tuple(
        f"{row.created_at[:19].replace('T', ' ')} {row.action}" for row in rows
    )
    return MemberCard(
        profile=profile,
        discord_name=discord_name,
        tenure_days=_tenure_days(member.joined_at),
        verified=member.verified_at is not None and member.status == "active",
        obligations=obligations,
        decisions=decisions,
        can_take_internal_tasks=can_take_internal_tasks(member),
    )


async def register_member(
    actor: Any,
    guild_id: int,
    *,
    discord_id: int,
    ic_name: str,
    game_character_id: int | None = None,
    rank_name: str | None = None,
    direction: str | None = None,
    mentor_discord_id: int | None = None,
    prime_time: str | None = None,
    reason: str | None = None,
) -> tuple[Member, LimitNotice]:
    await require_permission(actor, Permission.MEMBERS_MANAGE)
    name = ic_name.strip()
    if not name:
        raise MembershipError(
            "IC-имя обязательно. Участник не заведён; укажите имя и повторите.",
            code="MEMBERS.IC_NAME",
        )
    family = await ensure_family(guild_id)
    existing = await get_current_member(family.id, discord_id)
    if existing is not None:
        raise MembershipError(
            "Этот Discord-аккаунт уже есть в текущем составе. "
            "Новая запись не создана; откройте карточку или оформите выход.",
            code="MEMBERS.ALREADY_CURRENT",
            retryable=False,
        )
    rank = await _resolve_rank(
        guild_id=guild_id, family_id=family.id, rank_name=rank_name
    )
    mentor = await _mentor_row(family.id, mentor_discord_id)
    try:
        member = await add_member(
            guild_id=guild_id,
            family_id=family.id,
            discord_id=discord_id,
            ic_name=name,
            actor_id=_actor_id(actor),
            actor_name=_actor_name(actor),
            status="candidate",
            game_character_id=game_character_id,
            rank_id=rank.id if rank else None,
            mentor_id=mentor.id if mentor else None,
            direction=(direction or "").strip() or None,
            prime_time=(prime_time or "").strip() or None,
            reason=reason,
        )
    except DATABASE_INTEGRITY_ERRORS as exc:
        raise MembershipError(
            "Нельзя завести второе выбранное членство для этого аккаунта. "
            "Запись не создана.",
            code="MEMBERS.DUPLICATE",
            retryable=False,
        ) from exc
    await record_decision(
        guild_id=guild_id,
        action="MEMBERS.REGISTER",
        subject_type="member",
        subject_id=str(member.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after="candidate",
        reason=reason,
        source="command",
    )
    return member, await roster_limit_notice(guild_id, family.id)


async def start_probation(
    actor: Any,
    guild_id: int,
    discord_id: int,
    *,
    reason: str | None = None,
) -> Member:
    await require_permission(actor, Permission.MEMBERS_MANAGE)
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, discord_id)
    if member is None:
        raise MembershipError(
            "Текущего членства нет. Испытательный срок не назначен.",
            code="MEMBERS.NOT_FOUND",
        )
    if member.status == "active":
        raise MembershipError(
            "Участник уже подтверждён в игре. Статус не изменён.",
            code="MEMBERS.ALREADY_ACTIVE",
            retryable=False,
        )
    if member.status == "probation":
        return member
    updated = await set_member_status(
        member.id,
        "probation",
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        event_type="promote",
        reason=reason or "испытательный срок",
    )
    await record_decision(
        guild_id=guild_id,
        action="MEMBERS.PROBATION",
        subject_type="member",
        subject_id=str(updated.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=member.status,
        after="probation",
        reason=reason,
        source="command",
    )
    return updated


async def verify_in_game(
    actor: Any,
    guild_id: int,
    discord_id: int,
    *,
    reason: str | None = None,
) -> Member:
    await require_permission(actor, Permission.MEMBERS_MANAGE)
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, discord_id)
    if member is None:
        raise MembershipError(
            "Текущего членства нет. Подтверждение в игре не записано.",
            code="MEMBERS.NOT_FOUND",
        )
    if member.status == "active" and member.verified_at:
        await _record_recruiting_verify(guild_id, discord_id)
        return member
    updated = await _verify_member(
        member.id,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        reason=reason or "подтверждение вступления в игре",
    )
    await record_decision(
        guild_id=guild_id,
        action="MEMBERS.VERIFY",
        subject_type="member",
        subject_id=str(updated.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=member.status,
        after="active",
        reason=reason,
        source="command",
    )
    await _record_recruiting_verify(guild_id, discord_id)
    return updated


async def _record_recruiting_verify(guild_id: int, discord_id: int) -> None:
    from core.settings import get_registry
    from repositories.recruiting import (
        mark_referral_verified,
        maybe_mark_referral_retained,
    )

    await mark_referral_verified(guild_id, discord_id)
    raw = await get_registry().get(guild_id, "ONBOARDING_RETENTION_DAYS")
    try:
        days = int(raw or 0)
    except (TypeError, ValueError):
        days = 0
    if days > 0:
        await maybe_mark_referral_retained(
            guild_id, discord_id, retention_days=days
        )


async def preview_promote_newcomers(
    actor: Any, guild_id: int
) -> PromotePreview:
    await require_permission(actor, Permission.MEMBERS_MANAGE)
    family = await ensure_family(guild_id)
    newcomers = await list_roster(family.id, status="candidate", sort="name")
    lines = tuple(
        f"<@{item.discord_id}> · {item.ic_name}" for item in newcomers
    )
    return PromotePreview(members=tuple(newcomers), lines=lines)


async def apply_promote_newcomers(
    actor: Any,
    guild_id: int,
    member_ids: Sequence[int],
    *,
    reason: str | None = None,
) -> tuple[Member, ...]:
    await require_permission(actor, Permission.MEMBERS_MANAGE)
    updated: list[Member] = []
    for member_id in member_ids:
        member = await get_member(int(member_id))
        if member is None or member.status != "candidate":
            continue
        row = await start_probation(
            actor, guild_id, member.discord_id, reason=reason
        )
        updated.append(row)
    if updated:
        await record_decision(
            guild_id=guild_id,
            action="MEMBERS.PROMOTE_BATCH",
            subject_type="family",
            subject_id=str(updated[0].family_id),
            actor_id=_actor_id(actor),
            actor_name=_actor_name(actor),
            after=",".join(str(item.id) for item in updated),
            reason=reason,
            source="command",
        )
    return tuple(updated)


async def leave_member(
    actor: Any,
    guild_id: int,
    discord_id: int,
    *,
    reason: str | None = None,
) -> LeaveResult:
    await require_permission(actor, Permission.MEMBERS_MANAGE)
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, discord_id)
    if member is None:
        raise MembershipError(
            "Текущего членства нет. Выход не оформлен.",
            code="MEMBERS.NOT_FOUND",
        )
    before = await list_open_obligations(guild_id, discord_id)
    departed = await mark_exit(
        member.id,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        reason=reason,
    )
    after = await list_open_obligations(guild_id, discord_id)
    await record_decision(
        guild_id=guild_id,
        action="MEMBERS.LEAVE",
        subject_type="member",
        subject_id=str(departed.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=member.status,
        after="left",
        reason=reason,
        source="command",
    )
    from services.vehicles import mark_owner_departure

    await mark_owner_departure(departed)
    return LeaveResult(
        member=departed,
        obligations=tuple(after or before),
        tasks_closed=True,
        reserves_released=True,
    )


def filter_bar_options(
    ranks: Sequence[Rank],
    members: Sequence[Member],
) -> tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...]:
    """Return (key, placeholder, options) for FilterBar."""

    status_options = (("all", "Все текущие"),) + tuple(
        (key, STATUS_LABELS[key]) for key in _STATUS_FILTERS if key != "all"
    )
    sort_options = tuple((key, label) for key, label in SORT_LABELS.items())
    rank_options = (("all", "Все должности"),) + tuple(
        (str(rank.id), rank.name) for rank in ranks[:24]
    )
    specs = sorted({item.direction for item in members if item.direction})
    spec_options = (("all", "Все специализации"),) + tuple(
        (item, item) for item in specs[:24]
    )
    mentors = {
        item.mentor_id: f"наставник #{item.mentor_id}"
        for item in members
        if item.mentor_id is not None
    }
    mentor_options = (("all", "Все наставники"),) + tuple(
        (str(key), value) for key, value in list(mentors.items())[:24]
    )
    primes = sorted({item.prime_time for item in members if item.prime_time})
    prime_options = (("all", "Любой прайм"),) + tuple(
        (item, item) for item in primes[:24]
    )
    return (
        ("f", "Статус", status_options),
        ("rk", "Должность", rank_options),
        ("sp", "Специализация", spec_options),
        ("mn", "Наставник", mentor_options),
        ("pr", "Прайм", prime_options),
        ("s", "Сортировка", sort_options),
    )


async def resolve_rank_id(guild_id: int, rank_name: str | None) -> int | None:
    if not rank_name or not rank_name.strip():
        return None
    if rank_name.isdigit():
        return int(rank_name)
    family = await ensure_family(guild_id)
    wanted = rank_name.strip().casefold()
    for rank in await list_ranks(family.id):
        if rank.name.casefold() == wanted:
            return rank.id
    return -1


async def resolve_mentor_id(guild_id: int, discord_id: int | None) -> int | None:
    if discord_id is None:
        return None
    family = await ensure_family(guild_id)
    mentor = await get_current_member(family.id, discord_id)
    return mentor.id if mentor is not None else -1


def extra_from_filters(
    *,
    rank_id: int | None,
    direction: str | None,
    mentor_id: int | None,
    prime_time: str | None,
) -> tuple[tuple[str, str], ...]:
    extra: list[tuple[str, str]] = []
    if rank_id is not None:
        extra.append(("rk", str(rank_id)))
    if direction:
        extra.append(("sp", direction))
    if mentor_id is not None:
        extra.append(("mn", str(mentor_id)))
    if prime_time:
        extra.append(("pr", prime_time))
    return tuple(extra)


def roster_query_from_state(
    filter_value: str,
    extra: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    values = dict(extra)
    rank_raw = values.get("rk")
    mentor_raw = values.get("mn")
    return {
        "status": filter_value or None,
        "rank_id": int(rank_raw) if rank_raw and rank_raw.isdigit() else None,
        "direction": values.get("sp") or None,
        "mentor_id": int(mentor_raw) if mentor_raw and mentor_raw.isdigit() else None,
        "prime_time": values.get("pr") or None,
    }
