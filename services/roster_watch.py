"""HR watch for Discord leave and game-role loss. Does not change membership."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.settings import get_registry
from repositories.family import get_active_family
from repositories.members import get_current_member, get_member, get_member_profile
from repositories.training import list_tracks_for_mentor
from services.audit import record_decision
from services.membership import (
    OpenObligation,
    ensure_obligation_source,
    list_open_obligations,
)
from ui.command_registry import cmd
from ui.design import Accent
from ui.message_specs import MessageSpec
from ui.templates import notice

ORG_GAME_ROLE_KEYS: frozenset[str] = frozenset(
    {
        "young",
        "main",
        "staff",
        "high",
        "owner",
        "dep_owner",
        "lead_vzp",
        "vip",
    }
)
VISITOR_ROLE_KEYS: frozenset[str] = frozenset(
    {"guest", "friend", "super_friend"}
)
WATCH_POLICIES = frozenset({"skip", "silent"})
KIND_DISCORD_LEFT = "discord_left"
KIND_ROLE_LOST = "role_lost"
# Operational window to match a kick/ban entry, not a game number.
AUDIT_LOOKBACK_SECONDS = 120

LeftoverAction = Callable[[Any, int, str], Awaitable[str]]
_ACTION_HANDLERS: dict[str, LeftoverAction] = {}


@dataclass(frozen=True, slots=True)
class AuditHint:
    action: str
    moderator_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class WatchDecision:
    notify: bool
    ping: bool
    audience: str


@dataclass(frozen=True, slots=True)
class DepartureCard:
    kind: str
    discord_id: int
    display_name: str
    mention: str
    audience: str
    ping: bool
    reason: str
    role_names: tuple[str, ...]
    discord_joined_at: str | None
    family_joined_at: str | None
    tenure_days: int | None
    rank_name: str | None
    specialization: str | None
    in_roster: bool
    member_status: str | None
    leftovers: tuple[OpenObligation, ...]
    reserve_subjects: tuple[str, ...]
    spec: MessageSpec


class MentorLeftoverSource:
    async def list_open(self, guild_id: int, discord_id: int) -> list[OpenObligation]:
        family = await get_active_family(guild_id)
        if family is None:
            return []
        mentor = await get_current_member(family.id, discord_id)
        if mentor is None:
            return []
        items: list[OpenObligation] = []
        for track in await list_tracks_for_mentor(family.id, mentor.id):
            mentee = await get_member(track.member_id)
            if mentee is None:
                continue
            items.append(
                OpenObligation(
                    kind="mentee",
                    subject_id=str(mentee.discord_id),
                    summary=(
                        f"Подопечный {mentee.ic_name} (<@{mentee.discord_id}>), "
                        f"шаг {track.current_step}"
                    ),
                    outstanding=True,
                )
            )
        return items


async def _default_promote_reserve(
    actor: Any, guild_id: int, subject_id: str
) -> str:
    del actor, guild_id, subject_id
    return (
        "События ещё не подключены. Резерв не поднят. "
        "Дождитесь модуля событий или поднимите состав вручную."
    )


def register_leftover_action(name: str, handler: LeftoverAction) -> None:
    _ACTION_HANDLERS[name] = handler


def reset_leftover_actions() -> None:
    _ACTION_HANDLERS.clear()
    _ACTION_HANDLERS["promote_reserve"] = _default_promote_reserve


def ensure_default_sources() -> None:
    if "promote_reserve" not in _ACTION_HANDLERS:
        reset_leftover_actions()
    ensure_obligation_source(MentorLeftoverSource())


async def dispatch_leftover_action(
    actor: Any, guild_id: int, *, action: str, subject_id: str
) -> str:
    handler = _ACTION_HANDLERS.get(action)
    if handler is None:
        return (
            "Действие не зарегистрировано. Ничего не изменено; "
            "повторите, когда модуль будет подключён."
        )
    return await handler(actor, guild_id, subject_id)


def classify_audience(
    *, is_bot: bool, held_keys: set[str], in_roster: bool
) -> str:
    if is_bot:
        return "bot"
    if held_keys & set(ORG_GAME_ROLE_KEYS) or in_roster:
        return "org"
    if held_keys & set(VISITOR_ROLE_KEYS):
        return "visitor"
    return "none"


def decide_watch(
    audience: str, *, visitor_policy: str, bot_policy: str
) -> WatchDecision:
    visitor = visitor_policy if visitor_policy in WATCH_POLICIES else "skip"
    bot = bot_policy if bot_policy in WATCH_POLICIES else "skip"
    if audience == "org":
        return WatchDecision(notify=True, ping=True, audience=audience)
    if audience == "visitor":
        return WatchDecision(notify=visitor == "silent", ping=False, audience=audience)
    if audience == "bot":
        return WatchDecision(notify=bot == "silent", ping=False, audience=audience)
    return WatchDecision(notify=False, ping=False, audience=audience)


def describe_departure_reason(
    *, can_read_audit: bool, hint: AuditHint | None
) -> str:
    if not can_read_audit:
        return "покинул сервер"
    if hint is None:
        return "покинул сервер"
    if hint.action == "kick":
        return "исключён по журналу аудита"
    if hint.action == "ban":
        return "заблокирован по журналу аудита"
    return "покинул сервер"


def removed_org_role_names(
    *,
    before_ids: set[int],
    after_ids: set[int],
    org_roles: dict[int, str],
) -> tuple[str, ...]:
    names = [
        org_roles[role_id]
        for role_id in sorted(before_ids - after_ids)
        if role_id in org_roles
    ]
    return tuple(names)


def _tenure_days(joined_at: str | None) -> int | None:
    if not joined_at:
        return None
    try:
        joined = datetime.fromisoformat(joined_at)
    except ValueError:
        return None
    if joined.tzinfo is None:
        joined = joined.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - joined).days)


def _format_stamp(value: str | None) -> str:
    if not value:
        return "неизвестно"
    return value[:19].replace("T", " ")


def compose_departure_spec(card: DepartureCard) -> MessageSpec:
    title = (
        "Снята игровая роль"
        if card.kind == KIND_ROLE_LOST
        else "Участник покинул Discord"
    )
    details: list[tuple[str, str]] = [
        ("Участник", f"{card.mention} · {card.display_name}"),
        ("ID", str(card.discord_id)),
        (
            "Роли на момент события",
            ", ".join(card.role_names) if card.role_names else "нет",
        ),
        ("Вход в Discord", _format_stamp(card.discord_joined_at)),
        ("Принятие в семью", _format_stamp(card.family_joined_at)),
    ]
    if card.tenure_days is not None:
        details.append(("Срок членства", f"{card.tenure_days} дн."))
    if card.rank_name:
        details.append(("Должность", card.rank_name))
    if card.specialization:
        details.append(("Специализация", card.specialization))
    if card.member_status:
        details.append(("Статус в реестре", card.member_status))
    details.append(("Причина", card.reason))
    if card.leftovers:
        leftover_lines: list[str] = []
        for item in card.leftovers:
            if item.reference:
                leftover_lines.append(f"{item.summary}\n{item.reference}")
            else:
                leftover_lines.append(item.summary)
        details.append(("Что осталось", "\n".join(leftover_lines)))
    return notice(
        code="ROSTER.DEPARTURE" if card.kind == KIND_DISCORD_LEFT else "ROSTER.ROLE_LOST",
        title=title,
        summary=(
            "Это задача кадровику: сверить игровое членство. "
            "Выход из Discord сам по себе запись в реестре не закрывает."
        ),
        details=tuple(details),
        next_step=(
            "Оформите выход кнопкой на карточке или командой "
            f"{cmd('members leave')}. Автоматического исключения нет."
        ),
        accent=Accent.MEMBER,
        reference=str(card.discord_id),
    )


async def load_policies(guild_id: int) -> tuple[str, str]:
    registry = get_registry()
    visitor = str(
        await registry.get(guild_id, "ROSTER_WATCH_VISITOR_POLICY") or "skip"
    ).casefold()
    bot = str(
        await registry.get(guild_id, "ROSTER_WATCH_BOT_POLICY") or "skip"
    ).casefold()
    return (
        visitor if visitor in WATCH_POLICIES else "skip",
        bot if bot in WATCH_POLICIES else "skip",
    )


async def build_departure(
    guild_id: int,
    *,
    discord_id: int,
    display_name: str,
    mention: str,
    role_names: tuple[str, ...],
    discord_joined_at: str | None,
    is_bot: bool,
    held_keys: set[str],
    kind: str,
    can_read_audit: bool,
    audit: AuditHint | None,
    visitor_policy: str,
    bot_policy: str,
    removed_role_names: tuple[str, ...] = (),
) -> DepartureCard | None:
    ensure_default_sources()
    family = await get_active_family(guild_id)
    member = (
        await get_current_member(family.id, discord_id) if family is not None else None
    )
    decision = decide_watch(
        classify_audience(
            is_bot=is_bot, held_keys=held_keys, in_roster=member is not None
        ),
        visitor_policy=visitor_policy,
        bot_policy=bot_policy,
    )
    if not decision.notify:
        return None
    leftovers = tuple(await list_open_obligations(guild_id, discord_id))
    profile = await get_member_profile(member.id) if member is not None else None
    if kind == KIND_ROLE_LOST:
        reason = (
            "снята игровая роль: " + ", ".join(removed_role_names)
            if removed_role_names
            else "снята игровая роль"
        )
    else:
        reason = describe_departure_reason(
            can_read_audit=can_read_audit, hint=audit
        )
    card = DepartureCard(
        kind=kind,
        discord_id=discord_id,
        display_name=display_name,
        mention=mention,
        audience=decision.audience,
        ping=decision.ping,
        reason=reason,
        role_names=role_names,
        discord_joined_at=discord_joined_at,
        family_joined_at=member.joined_at if member is not None else None,
        tenure_days=_tenure_days(member.joined_at) if member is not None else None,
        rank_name=profile.rank.name if profile is not None and profile.rank else None,
        specialization=member.direction if member is not None else None,
        in_roster=member is not None,
        member_status=member.status if member is not None else None,
        leftovers=leftovers,
        reserve_subjects=tuple(
            item.subject_id
            for item in leftovers
            if item.action == "promote_reserve"
        ),
        spec=notice(code="ROSTER.BUILDING", title="Состав", summary=""),
    )
    return DepartureCard(
        kind=card.kind,
        discord_id=card.discord_id,
        display_name=card.display_name,
        mention=card.mention,
        audience=card.audience,
        ping=card.ping,
        reason=card.reason,
        role_names=card.role_names,
        discord_joined_at=card.discord_joined_at,
        family_joined_at=card.family_joined_at,
        tenure_days=card.tenure_days,
        rank_name=card.rank_name,
        specialization=card.specialization,
        in_roster=card.in_roster,
        member_status=card.member_status,
        leftovers=card.leftovers,
        reserve_subjects=card.reserve_subjects,
        spec=compose_departure_spec(card),
    )


async def record_watch_event(
    guild_id: int,
    *,
    kind: str,
    discord_id: int,
    reason: str,
) -> None:
    action = (
        "ROSTER.ROLE_LOST" if kind == KIND_ROLE_LOST else "ROSTER.DISCORD_LEFT"
    )
    await record_decision(
        guild_id=guild_id,
        action=action,
        subject_type="member",
        subject_id=str(discord_id),
        actor_id=0,
        actor_name="roster-watch",
        after=kind,
        reason=reason,
        source="event",
    )


reset_leftover_actions()
