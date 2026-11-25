"""Family applications, adaptation, clearances and promotion proposals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from core.permissions import Permission, evaluate_access
from core.profile import active_profile
from core.settings import get_registry
from repositories.members import (
    Clearance,
    Member,
    acknowledge_clearance_review,
    get_current_member,
    get_member,
    grant_clearance,
    mark_clearances_for_review,
)
from repositories.training import (
    ONBOARDING_STEPS,
    OnboardingTrack,
    PromotionCriteria,
    PromotionProposal,
    RulesVersion,
    TrainingAssignment,
    complete_assignment,
    create_assignment,
    create_proposal,
    create_track,
    decide_proposal,
    get_assignment,
    get_pending_proposal,
    get_proposal,
    get_track,
    get_track_for_member,
    latest_criteria,
    latest_rules_version,
    list_assignments_for_member,
    list_tracks_for_mentor,
    publish_criteria,
    publish_rules_version,
    set_track_mentor,
    set_track_step,
)
from services.audit import record_decision
from services.calendar import DAY_ALIASES, prime_day_labels, prime_days, rest_activity_days
from services.membership import (
    LimitNotice,
    MembershipError,
    ensure_family,
    register_member,
)

# Plan 4.4 §7: seasonal award window. Not a guessed game number.
SEASONAL_MEMBERSHIP_DAYS = 28
SEASONAL_PLAY_HOURS = 30

SPECIALIZATIONS: dict[str, str] = {
    "combat": "боевой состав",
    "logistics": "логистика",
    "contracts": "контракты",
    "mentoring": "наставничество",
}
PRIME_DAYS: dict[str, str] = prime_day_labels()
STEP_LABELS: dict[str, str] = {
    "rules": "Знакомство с правилами",
    "comms": "Связь и Discord",
    "first_task": "Первая задача с наставником",
    "skill_check": "Проверка навыка",
    "main_review": "Обсуждение перехода в основной состав",
}
SURNAME_POLICIES = frozenset({"off", "ask", "required"})


class OnboardingError(MembershipError):
    """Domain refusal for applications, training and promotions."""


@dataclass(frozen=True, slots=True)
class ApplicationDraft:
    ooc_name: str
    ooc_age: int | None
    ic_name: str
    character_id: int
    specialization: str
    prime_time: str
    source: str
    change_surname: str | None
    invited_by: int | None


@dataclass(frozen=True, slots=True)
class SeasonalCutoff:
    season_end: date
    cutoff_date: date
    days_ok: bool
    hours_confirmed: bool
    hours_required: int
    membership_days: int
    summary: str


@dataclass(frozen=True, slots=True)
class ApplicationReview:
    number: str
    draft: ApplicationDraft
    cutoff: SeasonalCutoff
    member: Member | None
    track: OnboardingTrack | None


@dataclass(frozen=True, slots=True)
class MenteeCard:
    member: Member
    track: OnboardingTrack
    next_step: str
    assignments: tuple[TrainingAssignment, ...]


def _actor_id(actor: Any) -> int:
    return int(getattr(actor, "id", 0) or 0)


def _actor_name(actor: Any) -> str:
    return str(
        getattr(actor, "display_name", None)
        or getattr(actor, "name", None)
        or "unknown"
    )


async def require_permission(actor: Any, permission: Permission) -> None:
    decision = await evaluate_access(actor, permission)
    if decision.allowed:
        return
    raise OnboardingError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


async def surname_policy(guild_id: int) -> str:
    value = await get_registry().get(guild_id, "FAMILY_SURNAME_POLICY")
    text = str(value or "ask").casefold()
    return text if text in SURNAME_POLICIES else "ask"


async def configured_days(guild_id: int, key: str) -> int | None:
    value = await get_registry().get(guild_id, key)
    if value is None or value == "":
        return None
    days = int(value)
    return days if days > 0 else None


def parse_ooc(raw: str) -> tuple[str, int | None]:
    text = raw.strip()
    if not text:
        raise OnboardingError(
            "Укажите OOC-имя и возраст. Заявка не принята.",
            code="APP.OOC_REQUIRED",
        )
    if "," in text:
        name, _, rest = text.partition(",")
        age_part = rest.strip()
    else:
        parts = text.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].isdigit():
            name, age_part = parts
        else:
            return text, None
    if not name.strip():
        raise OnboardingError(
            "OOC-имя пустое. Заявка не принята; укажите имя и возраст.",
            code="APP.OOC_REQUIRED",
        )
    if not age_part.isdigit():
        return name.strip(), None
    age = int(age_part)
    if age < 16 or age > 80:
        raise OnboardingError(
            "Возраст вне допустимого диапазона. Заявка не принята.",
            code="APP.AGE",
        )
    return name.strip(), age


def parse_character_id(raw: str) -> int:
    text = raw.strip()
    if not text or not text.isdigit():
        raise OnboardingError(
            "Идентификатор персонажа Memphis — целое число. "
            "Идентификатор другого проекта сюда не подставляется. Заявка не принята.",
            code="APP.CHARACTER_ID",
        )
    return int(text)


def parse_prime(raw: str) -> str:
    tokens = [item.strip().casefold() for item in raw.replace(";", ",").split(",")]
    allowed = set(prime_days())
    rest = set(rest_activity_days())
    days: list[str] = []
    for token in tokens:
        if not token:
            continue
        key = DAY_ALIASES.get(token)
        if key is None:
            raise OnboardingError(
                "Прайм — только игровые дни организаций из расписания профиля. "
                "Заявка не принята.",
                code="APP.PRIME",
            )
        if key in rest:
            raise OnboardingError(
                "Прайм — только игровые дни организаций. "
                "Выходные организаций оставляют для обучения, наставничества, "
                "инвентаризации и разборов. Заявка не принята.",
                code="APP.PRIME",
            )
        if key not in allowed:
            raise OnboardingError(
                "Прайм — только игровые дни организаций из расписания профиля. "
                "Заявка не принята.",
                code="APP.PRIME",
            )
        if key not in days:
            days.append(key)
    if not days:
        labels = "/".join(PRIME_DAYS.values()) or "игровые дни организаций"
        raise OnboardingError(
            f"Выберите хотя бы один день прайма ({labels}). Заявка не принята.",
            code="APP.PRIME",
        )
    return ",".join(days)


def parse_specialization(raw: str) -> str:
    key = raw.strip().casefold()
    aliases = {
        "боевой состав": "combat",
        "боевой": "combat",
        "логистика": "logistics",
        "контракты": "contracts",
        "наставничество": "mentoring",
        **{item: item for item in SPECIALIZATIONS},
    }
    resolved = aliases.get(key)
    if resolved is None:
        raise OnboardingError(
            "Специализация: боевой состав, логистика, контракты или наставничество. "
            "Заявка не принята.",
            code="APP.SPECIALIZATION",
        )
    return resolved


def parse_source(raw: str) -> tuple[str, int | None]:
    text = raw.strip()
    if not text:
        raise OnboardingError(
            "Укажите, кто пригласил. Заявка не принята.",
            code="APP.SOURCE",
        )
    digits = "".join(ch for ch in text if ch.isdigit())
    invited_by = int(digits) if digits and len(digits) >= 15 else None
    return text, invited_by


async def build_application_draft(
    guild_id: int,
    *,
    ooc_name: str,
    ic_name: str,
    character_id: str,
    specialization: str,
    prime_time: str,
    source: str,
    change_surname: str | None,
) -> ApplicationDraft:
    name, age = parse_ooc(ooc_name)
    ic = ic_name.strip()
    if not ic:
        raise OnboardingError(
            "IC-имя обязательно. Заявка не принята.",
            code="APP.IC_NAME",
        )
    policy = await surname_policy(guild_id)
    surname = (change_surname or "").strip() or None
    if policy == "required" and (surname is None or surname.casefold() not in {"да", "нет"}):
        raise OnboardingError(
            "По политике Guinness нужно ответить, готовы ли вы сменить фамилию. "
            "Это не правило Majestic. Заявка не принята.",
            code="APP.SURNAME",
        )
    if policy == "off":
        surname = None
    invited_text, invited_by = parse_source(source)
    return ApplicationDraft(
        ooc_name=name,
        ooc_age=age,
        ic_name=ic,
        character_id=parse_character_id(character_id),
        specialization=parse_specialization(specialization),
        prime_time=parse_prime(prime_time),
        source=invited_text,
        change_surname=surname,
        invited_by=invited_by,
    )


def draft_payload(draft: ApplicationDraft) -> dict[str, str | int | None]:
    return {
        "ooc_name": draft.ooc_name,
        "ooc_age": draft.ooc_age,
        "ic_name": draft.ic_name,
        "character_id": draft.character_id,
        "specialization": draft.specialization,
        "prime_time": draft.prime_time,
        "source": draft.source,
        "change_surname": draft.change_surname,
        "invited_by": draft.invited_by,
    }


def draft_from_payload(payload: dict[str, Any]) -> ApplicationDraft | None:
    try:
        character_raw = payload.get("character_id")
        if character_raw in {None, ""}:
            return None
        spec = str(payload.get("specialization") or "")
        prime = str(payload.get("prime_time") or "")
        return ApplicationDraft(
            ooc_name=str(payload.get("ooc_name") or ""),
            ooc_age=int(payload["ooc_age"]) if payload.get("ooc_age") else None,
            ic_name=str(payload.get("ic_name") or ""),
            character_id=int(character_raw),
            specialization=parse_specialization(spec) if spec else "combat",
            prime_time=parse_prime(prime) if prime else "mon",
            source=str(payload.get("source") or ""),
            change_surname=(
                str(payload["change_surname"])
                if payload.get("change_surname")
                else None
            ),
            invited_by=(
                int(payload["invited_by"]) if payload.get("invited_by") else None
            ),
        )
    except (OnboardingError, TypeError, ValueError):
        return None


def seasonal_cutoff(
    season_end: date,
    *,
    as_of: date,
    membership_days: int = SEASONAL_MEMBERSHIP_DAYS,
    play_hours: int = SEASONAL_PLAY_HOURS,
) -> SeasonalCutoff:
    cutoff_date = season_end - timedelta(days=membership_days)
    days_ok = as_of <= cutoff_date
    if days_ok:
        summary = (
            f"До конца сезона ({season_end.isoformat()}) ещё хватает "
            f"{membership_days} дней членства, если вступите не позже "
            f"{cutoff_date.isoformat()}. Часы игры ({play_hours}+) "
            "пока не подтверждены источником."
        )
    else:
        summary = (
            f"До конца сезона ({season_end.isoformat()}) не набирается "
            f"{membership_days} дней членства: крайний день вступления — "
            f"{cutoff_date.isoformat()}. Сезонную награду за этот сезон "
            "кандидат, скорее всего, не успеет."
        )
    return SeasonalCutoff(
        season_end=season_end,
        cutoff_date=cutoff_date,
        days_ok=days_ok,
        hours_confirmed=False,
        hours_required=play_hours,
        membership_days=membership_days,
        summary=summary,
    )


async def application_cutoff(
    guild_id: int, *, as_of: date | None = None
) -> SeasonalCutoff:
    profile = await active_profile(guild_id)
    today = as_of or datetime.now(UTC).date()
    registry = get_registry()
    days = int(await registry.get(guild_id, "PROGRESSION_SEASON_MEMBERSHIP_DAYS"))
    hours = int(await registry.get(guild_id, "PROGRESSION_SEASON_PLAY_HOURS"))
    return seasonal_cutoff(
        profile.season_end,
        as_of=today,
        membership_days=days,
        play_hours=hours,
    )


def _probation_until(days: int | None, started_at: datetime) -> str | None:
    if days is None:
        return None
    return (started_at + timedelta(days=days)).isoformat()


async def accept_application(
    actor: Any,
    guild_id: int,
    *,
    discord_id: int,
    number: str,
    draft: ApplicationDraft,
    reason: str | None = None,
) -> tuple[Member, OnboardingTrack, LimitNotice]:
    await require_permission(actor, Permission.APPLICATIONS_REVIEW)
    await require_permission(actor, Permission.MEMBERS_MANAGE)
    member, limit = await register_member(
        actor,
        guild_id,
        discord_id=discord_id,
        ic_name=draft.ic_name,
        game_character_id=draft.character_id,
        direction=draft.specialization,
        mentor_discord_id=draft.invited_by,
        prime_time=draft.prime_time,
        reason=reason or f"заявка {number}",
    )
    family = await ensure_family(guild_id)
    days = await configured_days(guild_id, "ONBOARDING_PROBATION_DAYS")
    started = datetime.now(UTC)
    track = await create_track(
        guild_id=guild_id,
        family_id=family.id,
        member_id=member.id,
        specialization=draft.specialization,
        application_number=number,
        mentor_id=member.mentor_id,
        probation_until=_probation_until(days, started),
        started_at=started.isoformat(),
    )
    await record_decision(
        guild_id=guild_id,
        action="APPLICATIONS.ACCEPT",
        subject_type="application",
        subject_id=number,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after="candidate",
        reason=reason,
        source="component",
    )
    return member, track, limit


async def reject_application(
    actor: Any, guild_id: int, *, number: str, reason: str
) -> None:
    await require_permission(actor, Permission.APPLICATIONS_REVIEW)
    await record_decision(
        guild_id=guild_id,
        action="APPLICATIONS.REJECT",
        subject_type="application",
        subject_id=number,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after="rejected",
        reason=reason,
        source="component",
    )


async def invite_to_interview(
    actor: Any, guild_id: int, *, number: str, reason: str
) -> None:
    await require_permission(actor, Permission.APPLICATIONS_REVIEW)
    await record_decision(
        guild_id=guild_id,
        action="APPLICATIONS.INTERVIEW",
        subject_type="application",
        subject_id=number,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after="interview",
        reason=reason,
        source="component",
    )


def next_step_after(current: str) -> str | None:
    if current not in ONBOARDING_STEPS:
        return ONBOARDING_STEPS[0]
    index = ONBOARDING_STEPS.index(current)
    if index + 1 >= len(ONBOARDING_STEPS):
        return None
    return ONBOARDING_STEPS[index + 1]


async def list_mentees(actor: Any, guild_id: int) -> tuple[MenteeCard, ...]:
    family = await ensure_family(guild_id)
    mentor = await get_current_member(family.id, _actor_id(actor))
    manage = await evaluate_access(actor, Permission.TRAINING_MANAGE)
    if mentor is None and not manage.allowed:
        raise OnboardingError(
            "Вас нет в текущем составе, подопечных показать нельзя.",
            code="TRAINING.NOT_A_MEMBER",
            retryable=False,
        )
    if mentor is None:
        return ()
    tracks = await list_tracks_for_mentor(family.id, mentor.id)
    cards: list[MenteeCard] = []
    for track in tracks:
        member = await get_member(track.member_id)
        if member is None:
            continue
        assignments = await list_assignments_for_member(member.id, open_only=True)
        cards.append(
            MenteeCard(
                member=member,
                track=track,
                next_step=STEP_LABELS.get(track.current_step, track.current_step),
                assignments=tuple(assignments),
            )
        )
    return tuple(cards)


def _owns_mentee(mentor: Member | None, track: OnboardingTrack) -> bool:
    if mentor is None:
        return False
    return track.mentor_id == mentor.id


async def _mentor_or_manager(
    actor: Any, guild_id: int, track: OnboardingTrack
) -> None:
    family = await ensure_family(guild_id)
    mentor = await get_current_member(family.id, _actor_id(actor))
    if _owns_mentee(mentor, track):
        return
    await require_permission(actor, Permission.TRAINING_MANAGE)


async def assign_training(
    actor: Any,
    guild_id: int,
    *,
    discord_id: int,
    title: str,
    step_key: str | None = None,
) -> TrainingAssignment:
    family = await ensure_family(guild_id)
    mentee = await get_current_member(family.id, discord_id)
    if mentee is None:
        raise OnboardingError(
            "Подопечный не найден в текущем составе. Задание не создано.",
            code="TRAINING.MENTEE_UNKNOWN",
        )
    track = await get_track_for_member(family.id, mentee.id)
    if track is None or track.status != "active":
        raise OnboardingError(
            "У этого человека нет активного маршрута адаптации. "
            "Задание не создано.",
            code="TRAINING.NO_TRACK",
        )
    await _mentor_or_manager(actor, guild_id, track)
    if track.specialization not in SPECIALIZATIONS:
        raise OnboardingError(
            "Специализация маршрута неизвестна. Задание не создано.",
            code="TRAINING.SPECIALIZATION",
        )
    step = step_key or track.current_step
    if step not in ONBOARDING_STEPS:
        raise OnboardingError(
            "Неизвестный шаг адаптации. Задание не создано.",
            code="TRAINING.STEP",
        )
    name = title.strip()
    if not name:
        raise OnboardingError(
            "Название задания пустое. Задание не создано.",
            code="TRAINING.TITLE",
        )
    item = await create_assignment(
        guild_id=guild_id,
        family_id=family.id,
        track_id=track.id,
        member_id=mentee.id,
        specialization=track.specialization,
        step_key=step,
        title=name,
        mentor_id=track.mentor_id,
    )
    await record_decision(
        guild_id=guild_id,
        action="TRAINING.ASSIGN",
        subject_type="training",
        subject_id=str(item.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=name,
        source="command",
    )
    return item


async def complete_training(
    actor: Any, guild_id: int, *, assignment_id: int
) -> TrainingAssignment:
    item = await get_assignment(assignment_id)
    if item is None:
        raise OnboardingError(
            "Задание не найдено. Ничего не изменено.",
            code="TRAINING.ASSIGNMENT_UNKNOWN",
        )
    track = await get_track(item.track_id)
    if track is None:
        raise OnboardingError(
            "Маршрут задания потерян. Ничего не изменено.",
            code="TRAINING.NO_TRACK",
        )
    await _mentor_or_manager(actor, guild_id, track)
    done = await complete_assignment(assignment_id)
    if done is None or done.status != "done":
        raise OnboardingError(
            "Задание уже закрыто. Повторно выполнить его не нужно.",
            code="TRAINING.ALREADY_DONE",
            retryable=False,
        )
    from services.contribution import award_from_training

    await award_from_training(actor, guild_id, done.id)
    return done


async def advance_track(
    actor: Any, guild_id: int, *, discord_id: int
) -> OnboardingTrack:
    family = await ensure_family(guild_id)
    mentee = await get_current_member(family.id, discord_id)
    if mentee is None:
        raise OnboardingError(
            "Участник не найден. Шаг не сдвинут.",
            code="TRAINING.MENTEE_UNKNOWN",
        )
    track = await get_track_for_member(family.id, mentee.id)
    if track is None:
        raise OnboardingError(
            "Маршрута адаптации нет. Шаг не сдвинут.",
            code="TRAINING.NO_TRACK",
        )
    await _mentor_or_manager(actor, guild_id, track)
    nxt = next_step_after(track.current_step)
    if nxt is None:
        updated = await set_track_step(track.id, track.current_step, status="completed")
        await record_decision(
            guild_id=guild_id,
            action="TRAINING.COMPLETE",
            subject_type="member",
            subject_id=str(mentee.id),
            actor_id=_actor_id(actor),
            actor_name=_actor_name(actor),
            after="completed",
            source="command",
        )
        return updated
    updated = await set_track_step(track.id, nxt)
    await record_decision(
        guild_id=guild_id,
        action="TRAINING.ADVANCE",
        subject_type="member",
        subject_id=str(mentee.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=track.current_step,
        after=nxt,
        source="command",
    )
    return updated


async def grant_activity_clearance(
    actor: Any,
    guild_id: int,
    *,
    discord_id: int,
    activity: str,
    rules_version: str,
    expires_at: str | None = None,
) -> Clearance:
    await require_permission(actor, Permission.TRAINING_MANAGE)
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, discord_id)
    if member is None:
        raise OnboardingError(
            "Участник не найден. Допуск не выдан.",
            code="TRAINING.MENTEE_UNKNOWN",
        )
    key = activity.strip().casefold()
    version = rules_version.strip()
    if not key or not version:
        raise OnboardingError(
            "Нужны направление допуска и версия правил. Допуск не выдан.",
            code="CLEARANCE.FIELDS",
        )
    item = await grant_clearance(
        member_id=member.id,
        activity=key,
        granted_by=_actor_id(actor),
        expires_at=expires_at,
        rules_version=version,
    )
    await record_decision(
        guild_id=guild_id,
        action="CLEARANCE.GRANT",
        subject_type="clearance",
        subject_id=str(item.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=f"{key}:{version}",
        source="command",
    )
    return item


async def publish_rules(
    actor: Any,
    guild_id: int,
    *,
    activity: str,
    version: str,
) -> tuple[RulesVersion, int]:
    await require_permission(actor, Permission.TRAINING_MANAGE)
    family = await ensure_family(guild_id)
    key = activity.strip().casefold()
    ver = version.strip()
    if not key or not ver:
        raise OnboardingError(
            "Нужны направление и версия правил. Публикация не записана.",
            code="RULES.FIELDS",
        )
    previous = await latest_rules_version(family.id, key)
    published = await publish_rules_version(
        guild_id=guild_id,
        family_id=family.id,
        activity=key,
        version=ver,
        published_by=_actor_id(actor),
    )
    marked = 0
    if previous is not None and previous.version != ver:
        marked = await mark_clearances_for_review(
            family_id=family.id,
            activity=key,
            previous_version=previous.version,
        )
    await record_decision(
        guild_id=guild_id,
        action="RULES.PUBLISH",
        subject_type="rules",
        subject_id=f"{key}:{ver}",
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=previous.version if previous else None,
        after=ver,
        source="command",
    )
    return published, marked


async def acknowledge_clearance(actor: Any, clearance_id: int) -> Clearance:
    await require_permission(actor, Permission.TRAINING_MANAGE)
    item = await acknowledge_clearance_review(clearance_id)
    if item is None:
        raise OnboardingError(
            "Допуск не найден. Ознакомление не записано.",
            code="CLEARANCE.UNKNOWN",
        )
    return item


def clearance_needs_review(item: Clearance) -> bool:
    return item.revoked_at is None and item.review_required_at is not None


async def set_promotion_criteria(
    actor: Any, guild_id: int, *, title: str, body: str
) -> PromotionCriteria:
    await require_permission(actor, Permission.MEMBERS_MANAGE)
    family = await ensure_family(guild_id)
    heading = title.strip()
    text = body.strip()
    if not heading or not text:
        raise OnboardingError(
            "Критерии повышения должны быть опубликованы текстом. "
            "Автоматических порогов нет.",
            code="PROMOTION.CRITERIA",
        )
    item = await publish_criteria(
        guild_id=guild_id,
        family_id=family.id,
        title=heading,
        body=text,
        published_by=_actor_id(actor),
    )
    await record_decision(
        guild_id=guild_id,
        action="PROMOTION.CRITERIA",
        subject_type="criteria",
        subject_id=str(item.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=heading,
        source="command",
    )
    return item


async def propose_promotion(
    actor: Any, guild_id: int, *, discord_id: int
) -> PromotionProposal:
    await require_permission(actor, Permission.TRAINING_MANAGE)
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, discord_id)
    if member is None:
        raise OnboardingError(
            "Участник не найден. Предложение не создано.",
            code="PROMOTION.MEMBER",
        )
    criteria = await latest_criteria(family.id)
    if criteria is None:
        raise OnboardingError(
            "Критерии повышения ещё не опубликованы. "
            "Автоматического повышения нет; сначала запишите критерии.",
            code="PROMOTION.NO_CRITERIA",
        )
    existing = await get_pending_proposal(family.id, member.id, criteria.id)
    if existing is not None:
        return existing
    item = await create_proposal(
        guild_id=guild_id,
        family_id=family.id,
        member_id=member.id,
        criteria_id=criteria.id,
        proposed_by=_actor_id(actor),
    )
    await record_decision(
        guild_id=guild_id,
        action="PROMOTION.PROPOSE",
        subject_type="proposal",
        subject_id=str(item.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=str(member.id),
        source="command",
    )
    return item


async def decide_promotion(
    actor: Any,
    guild_id: int,
    *,
    proposal_id: int,
    accepted: bool,
    reason: str | None = None,
) -> PromotionProposal:
    await require_permission(actor, Permission.MEMBERS_MANAGE)
    current = await get_proposal(proposal_id)
    if current is None:
        raise OnboardingError(
            "Предложение не найдено. Решение не записано.",
            code="PROMOTION.UNKNOWN",
        )
    if current.status != "pending":
        return current
    updated = await decide_proposal(
        proposal_id,
        accepted=accepted,
        decided_by=_actor_id(actor),
        reason=reason,
    )
    if updated is None:
        raise OnboardingError(
            "Не удалось записать решение. Повторите.",
            code="PROMOTION.UNKNOWN",
        )
    await record_decision(
        guild_id=guild_id,
        action="PROMOTION.DECIDE",
        subject_type="proposal",
        subject_id=str(proposal_id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after="accepted" if accepted else "rejected",
        reason=reason,
        source="command",
    )
    return updated


async def assign_mentor(
    actor: Any, guild_id: int, *, discord_id: int, mentor_discord_id: int
) -> OnboardingTrack:
    await require_permission(actor, Permission.TRAINING_MANAGE)
    family = await ensure_family(guild_id)
    mentee = await get_current_member(family.id, discord_id)
    mentor = await get_current_member(family.id, mentor_discord_id)
    if mentee is None or mentor is None:
        raise OnboardingError(
            "Подопечный или наставник не найдены в составе. Связь не записана.",
            code="TRAINING.MENTOR",
        )
    track = await get_track_for_member(family.id, mentee.id)
    if track is None:
        raise OnboardingError(
            "Маршрута адаптации нет. Сначала примите заявку.",
            code="TRAINING.NO_TRACK",
        )
    return await set_track_mentor(track.id, mentor.id)
