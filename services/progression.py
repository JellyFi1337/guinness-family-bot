"""Organization development, direction change and seasonal right."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from core.permissions import AccessDecision, Permission, evaluate_access
from core.profile import active_profile
from core.settings import get_registry
from infrastructure.database import DATABASE_INTEGRITY_ERRORS
from repositories.family import update_family_direction
from repositories.inventory import get_item_by_sku, list_balances, list_locations
from repositories.members import get_current_member, list_clearance_activities
from repositories.progression import (
    HOURS_SOURCE,
    REVIEW_DONE,
    REVIEW_KINDS,
    TRACKS,
    FamilySeason,
    OrgDevelopment,
    OrgUpgrade,
    SeasonHours,
    UpgradeProgress,
    ensure_development,
    get_family_season_by_key,
    get_progress,
    get_season_hours,
    get_upgrade,
    get_upgrade_by_key,
    insert_family_season,
    insert_upgrade,
    list_open_reviews,
    list_progress,
    list_upgrades,
    reset_all_progress,
    set_progress_level,
    track_has_progress,
    update_development,
    upsert_lore_review,
    upsert_season_hours,
)
from repositories.reports import count_accepted_reports
from services.audit import record_decision
from services.event_planning import EVENT_TEMPLATES
from services.membership import ensure_family
from services.onboarding import SeasonalCutoff, seasonal_cutoff
from ui.command_registry import cmd
from ui.design import Accent
from ui.message_specs import MessageSpec, MessageTone
from ui.templates import notice

DIRECTIONS = frozenset({"legal", "criminal"})
JUNIOR_ACTIONS = (
    "собрать ресурс",
    "помочь в контракте",
    "пройти обучение",
)


class ProgressionError(Exception):
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
class DirectionPreview:
    current: str
    target: str
    cooldown_days: int
    spent_reputation: int
    returned_reputation: int
    loss_per_mille: int
    version: int
    summary: str


@dataclass(frozen=True, slots=True)
class MemberSeasonRight:
    cutoff: SeasonalCutoff
    joined_on: date
    days_ok: bool
    hours: int
    hours_missing: int
    hours_human: bool
    hours_source: str
    eligible: bool
    summary: str


@dataclass(frozen=True, slots=True)
class DirectionChange:
    before: str
    after: str
    spent: int
    returned: int
    development: OrgDevelopment


def _actor_id(actor: Any) -> int:
    return int(actor.id)


def _actor_name(actor: Any) -> str:
    return str(getattr(actor, "display_name", None) or getattr(actor, "name", actor.id))


async def _require(actor: Any, permission: Permission) -> None:
    decision = await evaluate_access(actor, permission)
    if not decision.allowed:
        raise ProgressionError(
            "Недостаточно полномочий. Действие не выполнено.",
            code="PROG.DENIED",
            decision=decision,
        )


async def _int_setting(guild_id: int, key: str) -> int:
    return int(await get_registry().get(guild_id, key))


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise ProgressionError(
            f"Дата {field} должна быть ГГГГ-ММ-ДД. Значение не записано.",
            code="PROG.DATE",
            retryable=True,
        ) from exc


def _joined_on(stamp: str) -> date:
    text = stamp.strip()
    try:
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text[:10])
    except ValueError:
        return datetime.now(UTC).date()


def convert_reputation(spent: int, loss_per_mille: int) -> int:
    if spent < 0 or loss_per_mille < 0:
        return 0
    keep = 1000 - loss_per_mille
    if keep < 0:
        keep = 0
    return spent * keep // 1000


async def define_upgrade(
    actor: Any,
    guild_id: int,
    *,
    key: str,
    title: str,
    track: str,
    max_level: int | None = None,
    depends_on: str = "",
    depends_level: int = 0,
    requires_org_level: int = 0,
    requires_contracts: int = 0,
    requires_sku: str = "",
    requires_qty: int = 0,
    cost_reputation: int = 0,
    source: str = "",
    verified_at: str | None = None,
) -> OrgUpgrade:
    await _require(actor, Permission.PROGRESSION_MANAGE)
    family = await ensure_family(guild_id)
    await ensure_development(guild_id, family.id)
    code = key.strip()
    name = title.strip()
    if not code or not name:
        raise ProgressionError(
            "Нужны ключ и название улучшения. Запись не создана.",
            code="PROG.FIELDS",
            retryable=True,
        )
    if track not in TRACKS:
        raise ProgressionError(
            "Ветка — легальные контракты, нелегальные контракты или другое. "
            "Запись не создана.",
            code="PROG.TRACK",
            retryable=True,
        )
    cap = max_level if max_level is not None else await _int_setting(
        guild_id, "PROGRESSION_MAX_LEVEL"
    )
    ceiling = await _int_setting(guild_id, "PROGRESSION_MAX_LEVEL")
    if cap < 1 or cap > ceiling:
        raise ProgressionError(
            f"Уровней может быть от 1 до {ceiling}. Запись не создана.",
            code="PROG.LEVEL",
            retryable=True,
        )
    if cost_reputation < 0 or requires_qty < 0 or requires_contracts < 0:
        raise ProgressionError(
            "Стоимость и требования не могут быть отрицательными.",
            code="PROG.FIELDS",
            retryable=True,
        )
    dev = await ensure_development(guild_id, family.id)
    try:
        item = await insert_upgrade(
            guild_id=guild_id,
            family_id=family.id,
            key=code,
            title=name,
            track=track,
            max_level=cap,
            depends_on=depends_on.strip(),
            depends_level=depends_level,
            requires_org_level=requires_org_level,
            requires_contracts=requires_contracts,
            requires_sku=requires_sku.strip(),
            requires_qty=requires_qty,
            cost_reputation=cost_reputation,
            source=source.strip(),
            verified_at=verified_at,
            lore_revision=dev.lore_revision,
        )
    except DATABASE_INTEGRITY_ERRORS as exc:
        raise ProgressionError(
            "Улучшение с таким ключом уже есть. Запись не создана.",
            code="PROG.DUPLICATE",
            retryable=True,
        ) from exc
    await record_decision(
        guild_id=guild_id,
        action="PROG.DEFINE",
        subject_type="org_upgrade",
        subject_id=item.key,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        source="command",
    )
    return item


async def _progress_map(family_id: int) -> dict[int, UpgradeProgress]:
    return {item.upgrade_id: item for item in await list_progress(family_id)}


async def _available_of(family_id: int, sku: str) -> tuple[int, int]:
    if not sku:
        return 0, 0
    item = await get_item_by_sku(family_id, sku)
    if item is None:
        return 0, 0
    on_hand = 0
    reserved = 0
    for location in await list_locations(family_id):
        balance = None
        for row in await list_balances(location.id):
            if row.item_id == item.id:
                balance = row
                break
        if balance is None:
            continue
        on_hand += balance.on_hand
        reserved += balance.reserved
    return on_hand - reserved, reserved


async def _requirement_gap(
    family_id: int,
    node: OrgUpgrade,
    levels: dict[int, UpgradeProgress],
    development: OrgDevelopment,
) -> str | None:
    if node.requires_org_level > 0:
        if development.org_level <= 0:
            return (
                "Уровень организации не подтверждён источником. "
                "Старший состав вводит его отдельно."
            )
        if development.org_level < node.requires_org_level:
            return (
                f"Нужен уровень организации {node.requires_org_level}, "
                f"сейчас {development.org_level}."
            )
    if node.requires_contracts > 0:
        done = await count_accepted_reports(family_id)
        if done < node.requires_contracts:
            return (
                f"Нужно принятых контрактов: {node.requires_contracts}, "
                f"сейчас {done}."
            )
    if node.requires_sku and node.requires_qty > 0:
        available, _reserved = await _available_of(family_id, node.requires_sku)
        if available < node.requires_qty:
            return (
                f"Нужен ресурс {node.requires_sku}: {node.requires_qty}, "
                f"доступно {available}."
            )
    if node.depends_on:
        parent = await get_upgrade_by_key(family_id, node.depends_on)
        if parent is None:
            return f"Зависимость {node.depends_on} не найдена в дереве."
        have = levels.get(parent.id)
        current = 0 if have is None else have.current_level
        need = node.depends_level if node.depends_level > 0 else 1
        if current < need:
            return (
                f"Сначала поднимите {parent.title} до уровня {need} "
                f"(сейчас {current})."
            )
    return None


def _opposite_track(track: str) -> str | None:
    if track == "legal_contracts":
        return "illegal_contracts"
    if track == "illegal_contracts":
        return "legal_contracts"
    return None


async def advance_upgrade(
    actor: Any, guild_id: int, key: str
) -> tuple[OrgUpgrade, UpgradeProgress]:
    await _require(actor, Permission.PROGRESSION_MANAGE)
    family = await ensure_family(guild_id)
    development = await ensure_development(guild_id, family.id)
    node = await get_upgrade_by_key(family.id, key.strip())
    if node is None:
        raise ProgressionError(
            "Улучшение не найдено. Уровень не изменён.",
            code="PROG.NOT_FOUND",
            retryable=True,
        )
    opposite = _opposite_track(node.track)
    if opposite is not None and await track_has_progress(family.id, opposite):
        raise ProgressionError(
            "Легальные и нелегальные контракты нельзя улучшать одновременно. "
            "Уровень не изменён.",
            code="PROG.TRACK_LOCK",
            retryable=False,
        )
    levels = await _progress_map(family.id)
    gap = await _requirement_gap(family.id, node, levels, development)
    if gap is not None:
        raise ProgressionError(gap, code="PROG.REQUIREMENT", retryable=True)
    progress = levels.get(node.id)
    current = 0 if progress is None else progress.current_level
    if current >= node.max_level:
        raise ProgressionError(
            "Этот узел уже на максимуме. Уровень не изменён.",
            code="PROG.MAX",
            retryable=False,
        )
    updated = await set_progress_level(node.id, current + 1)
    if updated is None:
        raise ProgressionError(
            "Прогресс изменился. Откройте дерево и повторите.",
            code="PROG.CONFLICT",
            retryable=True,
        )
    extra = 0 if node.cost_reputation <= 0 else node.cost_reputation
    if extra:
        await update_development(
            family.id,
            spent_reputation=development.spent_reputation + extra,
        )
    await record_decision(
        guild_id=guild_id,
        action="PROG.ADVANCE",
        subject_type="org_upgrade",
        subject_id=node.key,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=str(current),
        after=str(updated.current_level),
        source="command",
    )
    return node, updated


async def set_goal(
    actor: Any,
    guild_id: int,
    key: str,
    *,
    assignee_discord_id: int | None = None,
    note: str = "",
) -> tuple[OrgDevelopment, OrgUpgrade]:
    await _require(actor, Permission.PROGRESSION_MANAGE)
    family = await ensure_family(guild_id)
    await ensure_development(guild_id, family.id)
    node = await get_upgrade_by_key(family.id, key.strip())
    if node is None:
        raise ProgressionError(
            "Улучшение не найдено. Цель не записана.",
            code="PROG.NOT_FOUND",
            retryable=True,
        )
    assignee = assignee_discord_id
    if assignee is not None:
        member = await get_current_member(family.id, assignee)
        if member is None:
            raise ProgressionError(
                "Назначенный не найден в составе. Цель не записана.",
                code="PROG.ASSIGNEE",
                retryable=True,
            )
    updated = await update_development(
        family.id,
        goal_upgrade_id=node.id,
        goal_assignee_id=assignee,
        goal_note=note.strip(),
    )
    if updated is None:
        raise ProgressionError(
            "Состояние изменилось. Откройте карточку и повторите.",
            code="PROG.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="PROG.GOAL",
        subject_type="org_upgrade",
        subject_id=node.key,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        source="command",
    )
    return updated, node


async def set_org_level(actor: Any, guild_id: int, org_level: int) -> OrgDevelopment:
    await _require(actor, Permission.PROGRESSION_MANAGE)
    family = await ensure_family(guild_id)
    await ensure_development(guild_id, family.id)
    if org_level < 0:
        raise ProgressionError(
            "Уровень организации не может быть отрицательным.",
            code="PROG.LEVEL",
            retryable=True,
        )
    updated = await update_development(family.id, org_level=org_level)
    if updated is None:
        raise ProgressionError(
            "Состояние изменилось. Уровень не записан.",
            code="PROG.CONFLICT",
            retryable=True,
        )
    return updated


def _next_step(
    node: OrgUpgrade | None,
    progress: UpgradeProgress | None,
    gap: str | None,
) -> str:
    if node is None:
        return (
            "Лидер ещё не выбрал текущую цель. "
            "Оптимальную прокачку бот не предлагает."
        )
    if gap is not None:
        if node.requires_sku and "ресурс" in gap:
            return f"{JUNIOR_ACTIONS[0]}: {gap}"
        if node.requires_contracts and "контракт" in gap:
            return f"{JUNIOR_ACTIONS[1]}: {gap}"
        return f"{JUNIOR_ACTIONS[2]}: {gap}"
    level = 0 if progress is None else progress.current_level
    if level >= node.max_level:
        return "Текущая цель закрыта. Лидер выбирает следующую."
    return f"Следующий шаг: поднять {node.title} до уровня {level + 1}."


async def development_card(actor: Any, guild_id: int) -> MessageSpec:
    family = await ensure_family(guild_id)
    development = await ensure_development(guild_id, family.id)
    levels = await _progress_map(family.id)
    nodes = await list_upgrades(family.id)
    goal = None if development.goal_upgrade_id is None else await get_upgrade(
        development.goal_upgrade_id
    )
    goal_progress = None if goal is None else levels.get(goal.id)
    gap = None
    reserved = 0
    available = 0
    if goal is not None:
        gap = await _requirement_gap(family.id, goal, levels, development)
        if goal.requires_sku:
            available, reserved = await _available_of(family.id, goal.requires_sku)
    staff = await evaluate_access(actor, Permission.PROGRESSION_MANAGE)
    lines: list[tuple[str, str]] = [
        ("Направление семьи", family.direction),
        (
            "Уровень организации",
            "не подтверждён"
            if development.org_level <= 0
            else str(development.org_level),
        ),
        ("Узлов в дереве", str(len(nodes))),
    ]
    if goal is None:
        lines.append(("Текущая цель", "не выбрана"))
    else:
        level = 0 if goal_progress is None else goal_progress.current_level
        lines.append(("Текущая цель", f"{goal.title} ({level}/{goal.max_level})"))
        lines.append(("Следующий шаг", _next_step(goal, goal_progress, gap)))
        lines.append(
            (
                "Полезные действия",
                ", ".join(JUNIOR_ACTIONS)
                + ". Оптимальную прокачку бот не заявляет.",
            )
        )
        if staff.allowed:
            assignee = (
                "не назначен"
                if development.goal_assignee_id is None
                else f"<@{development.goal_assignee_id}>"
            )
            lines.append(("Кто назначен", assignee))
            if goal.requires_sku:
                lines.append(
                    (
                        "Что тормозит",
                        gap or f"{goal.requires_sku}: доступно {available}, в резерве {reserved}",
                    )
                )
            elif gap:
                lines.append(("Что тормозит", gap))
            if development.goal_note:
                lines.append(("Заметка", development.goal_note))
    if development.lore_revision:
        open_reviews = await list_open_reviews(family.id, development.lore_revision)
        lines.append(
            (
                "Сведения",
                f"сборка {development.lore_revision}"
                + (
                    f", к пересмотру {len(open_reviews)}"
                    if open_reviews
                    else ", пересмотр закрыт"
                ),
            )
        )
    return notice(
        code="PROG.SHOW",
        title="Развитие",
        summary="Одна текущая цель и следующий шаг. Репутацию улучшения не меняют.",
        details=tuple(lines),
        next_step=(
            f"Цель — {cmd('progress goal')}."
            if staff.allowed
            else f"Сезонное право — {cmd('progress season')}."
        ),
        accent=Accent.STAFF,
        reference=str(family.id),
    )


async def preview_direction_change(
    actor: Any, guild_id: int, target: str, *, spent_reputation: int | None = None
) -> DirectionPreview:
    await _require(actor, Permission.PROGRESSION_MANAGE)
    family = await ensure_family(guild_id)
    development = await ensure_development(guild_id, family.id)
    goal = target.strip()
    if goal not in DIRECTIONS:
        raise ProgressionError(
            "Направление — legal или criminal. Предпросмотр не записан.",
            code="PROG.DIRECTION",
            retryable=True,
        )
    if family.direction not in DIRECTIONS:
        raise ProgressionError(
            "Смена с потерей 5% нужна только между легальным и нелегальным. "
            "Для первого выбора подтвердите направление без сброса.",
            code="PROG.DIRECTION",
            retryable=True,
        )
    if goal == family.direction:
        raise ProgressionError(
            "Семья уже в этом направлении. Предпросмотр не нужен.",
            code="PROG.DIRECTION",
            retryable=False,
        )
    spent = development.spent_reputation if spent_reputation is None else spent_reputation
    if spent < 0:
        raise ProgressionError(
            "Потраченная репутация не может быть отрицательной.",
            code="PROG.REP",
            retryable=True,
        )
    loss = await _int_setting(guild_id, "PROGRESSION_REP_LOSS_PER_MILLE")
    cooldown = await _int_setting(guild_id, "PROGRESSION_DIRECTION_COOLDOWN_DAYS")
    returned = convert_reputation(spent, loss)
    updated = await update_development(
        family.id,
        pending_direction=goal,
        pending_spent=spent,
        pending_preview_at=datetime.now(UTC).isoformat(),
    )
    if updated is None:
        raise ProgressionError(
            "Состояние изменилось. Предпросмотр не записан.",
            code="PROG.CONFLICT",
            retryable=True,
        )
    summary = (
        f"Смена {family.direction} → {goal}: перерыв {cooldown} дней, "
        f"сброс всех улучшений, из потраченных {spent} репутации вернётся "
        f"{returned} (потеря {loss} тысячных, это 5% при значении 50). "
        "Улучшения не влияют на репутацию в другую сторону. "
        f"Подтвердите отдельным решением {cmd('progress direction')}."
    )
    return DirectionPreview(
        current=family.direction,
        target=goal,
        cooldown_days=cooldown,
        spent_reputation=spent,
        returned_reputation=returned,
        loss_per_mille=loss,
        version=updated.version,
        summary=summary,
    )


async def confirm_direction_change(
    actor: Any,
    guild_id: int,
    target: str,
    *,
    confirm: bool,
    expected_version: int,
) -> DirectionChange:
    await _require(actor, Permission.PROGRESSION_MANAGE)
    if not confirm:
        raise ProgressionError(
            "Сначала посмотрите последствия командой предпросмотра, затем "
            "подтвердите явно. Направление не изменено.",
            code="PROG.CONFIRM",
            retryable=True,
        )
    family = await ensure_family(guild_id)
    development = await ensure_development(guild_id, family.id)
    goal = target.strip()
    if goal not in DIRECTIONS:
        raise ProgressionError(
            "Направление — legal или criminal. Значение не записано.",
            code="PROG.DIRECTION",
            retryable=True,
        )
    if family.direction not in DIRECTIONS:
        await update_family_direction(family.id, goal)
        await get_registry().set(
            guild_id, "direction", goal, updated_by=str(_actor_id(actor))
        )
        await record_decision(
            guild_id=guild_id,
            action="PROG.DIRECTION",
            subject_type="family",
            subject_id=str(family.id),
            actor_id=_actor_id(actor),
            actor_name=_actor_name(actor),
            before=family.direction,
            after=goal,
            reason="первый выбор, без сброса и без потери репутации",
            source="command",
        )
        return DirectionChange(
            before=family.direction,
            after=goal,
            spent=0,
            returned=0,
            development=development,
        )
    if development.pending_direction != goal:
        raise ProgressionError(
            "Предпросмотр не совпадает с выбранным направлением. "
            "Покажите последствия ещё раз, затем подтвердите.",
            code="PROG.PREVIEW",
            retryable=True,
        )
    if development.version != expected_version:
        raise ProgressionError(
            "Дерево изменилось после предпросмотра. Покажите последствия снова.",
            code="PROG.CONFLICT",
            retryable=True,
        )
    now = datetime.now(UTC)
    if development.cooldown_until:
        until = datetime.fromisoformat(development.cooldown_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        if now < until:
            raise ProgressionError(
                f"Перерыв после прошлой смены ещё действует до {until.isoformat()}. "
                "Направление не изменено.",
                code="PROG.COOLDOWN",
                retryable=True,
            )
    loss = await _int_setting(guild_id, "PROGRESSION_REP_LOSS_PER_MILLE")
    cooldown_days = await _int_setting(guild_id, "PROGRESSION_DIRECTION_COOLDOWN_DAYS")
    spent = development.pending_spent if development.pending_spent is not None else 0
    returned = convert_reputation(spent, loss)
    await reset_all_progress(family.id)
    cooldown_until = (now + timedelta(days=cooldown_days)).isoformat()
    updated = await update_development(
        family.id,
        spent_reputation=returned,
        direction_changed_at=now.isoformat(),
        cooldown_until=cooldown_until,
        clear_pending=True,
        clear_goal=True,
        expected_version=expected_version,
    )
    if updated is None:
        raise ProgressionError(
            "Состояние изменилось. Направление не записано; повторите предпросмотр.",
            code="PROG.CONFLICT",
            retryable=True,
        )
    await update_family_direction(family.id, goal)
    await get_registry().set(
        guild_id, "direction", goal, updated_by=str(_actor_id(actor))
    )
    await record_decision(
        guild_id=guild_id,
        action="PROG.DIRECTION",
        subject_type="family",
        subject_id=str(family.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        before=family.direction,
        after=goal,
        reason=(
            f"сброс улучшений, возврат {returned} из {spent}, "
            f"перерыв {cooldown_days} дн."
        ),
        source="command",
    )
    return DirectionChange(
        before=family.direction,
        after=goal,
        spent=spent,
        returned=returned,
        development=updated,
    )


async def set_play_hours(
    actor: Any,
    guild_id: int,
    hours: int,
    *,
    for_discord_id: int | None = None,
) -> SeasonHours:
    family = await ensure_family(guild_id)
    target_id = for_discord_id if for_discord_id is not None else _actor_id(actor)
    if target_id != _actor_id(actor):
        await _require(actor, Permission.PROGRESSION_MANAGE)
    if hours < 0:
        raise ProgressionError(
            "Часы игры не могут быть отрицательными. Значение не записано.",
            code="PROG.HOURS",
            retryable=True,
        )
    member = await get_current_member(family.id, target_id)
    if member is None:
        raise ProgressionError(
            "Участник не найден в составе. Часы не записаны.",
            code="PROG.MEMBER",
            retryable=True,
        )
    profile = await active_profile(guild_id)
    row = await upsert_season_hours(
        guild_id=guild_id,
        family_id=family.id,
        member_id=member.id,
        season_key=profile.season_key,
        hours=hours,
        entered_by=_actor_id(actor),
    )
    await record_decision(
        guild_id=guild_id,
        action="PROG.HOURS",
        subject_type="member",
        subject_id=str(member.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=str(hours),
        reason="human",
        source="command",
    )
    return row


async def member_season_right(
    guild_id: int, discord_id: int, *, as_of: date | None = None
) -> MemberSeasonRight:
    family = await ensure_family(guild_id)
    member = await get_current_member(family.id, discord_id)
    if member is None:
        raise ProgressionError(
            "Участник не найден в составе.",
            code="PROG.MEMBER",
            retryable=True,
        )
    profile = await active_profile(guild_id)
    today = as_of or datetime.now(UTC).date()
    days = await _int_setting(guild_id, "PROGRESSION_SEASON_MEMBERSHIP_DAYS")
    need_hours = await _int_setting(guild_id, "PROGRESSION_SEASON_PLAY_HOURS")
    cutoff = seasonal_cutoff(
        profile.season_end,
        as_of=today,
        membership_days=days,
        play_hours=need_hours,
    )
    joined = _joined_on(member.joined_at)
    days_ok = joined <= cutoff.cutoff_date
    hours_row = await get_season_hours(family.id, member.id, profile.season_key)
    if hours_row is None:
        hours = 0
        human = False
        source = ""
        missing = need_hours
    else:
        hours = hours_row.hours
        human = hours_row.source == HOURS_SOURCE
        source = hours_row.source
        missing = max(0, need_hours - hours)
    hours_ok = human and hours >= need_hours
    if days_ok and hours_ok:
        summary = (
            f"По сроку членства вы успеваете (вступили {joined.isoformat()}, "
            f"отсечка {cutoff.cutoff_date.isoformat()}). "
            f"Часы игры {hours} из {need_hours} введены человеком, бот игру не читает."
        )
    elif days_ok:
        summary = (
            f"По сроку членства вы успеваете (отсечка {cutoff.cutoff_date.isoformat()}). "
            + (
                f"Часов не хватает: {missing} (введено человеком {hours} из {need_hours})."
                if human
                else f"Часы игры ({need_hours}+) ещё не введены человеком. "
                "Голосовой канал часами игры не является."
            )
        )
    else:
        summary = (
            f"По сроку членства сезонную награду за этот сезон вы не успеете: "
            f"вступили {joined.isoformat()}, отсечка {cutoff.cutoff_date.isoformat()}."
        )
    return MemberSeasonRight(
        cutoff=cutoff,
        joined_on=joined,
        days_ok=days_ok,
        hours=hours,
        hours_missing=missing,
        hours_human=human,
        hours_source=source,
        eligible=days_ok and hours_ok,
        summary=summary,
    )


async def admission_cutoff(
    guild_id: int, *, as_of: date | None = None
) -> SeasonalCutoff:
    profile = await active_profile(guild_id)
    today = as_of or datetime.now(UTC).date()
    days = await _int_setting(guild_id, "PROGRESSION_SEASON_MEMBERSHIP_DAYS")
    hours = await _int_setting(guild_id, "PROGRESSION_SEASON_PLAY_HOURS")
    return seasonal_cutoff(
        profile.season_end,
        as_of=today,
        membership_days=days,
        play_hours=hours,
    )


async def announce_season(
    actor: Any,
    guild_id: int,
    *,
    season_key: str,
    starts_on: str,
    ends_on: str,
    influence_daily_limit: int = 0,
    prize_places: str = "",
    half_loss_threshold: int = 0,
) -> FamilySeason:
    await _require(actor, Permission.PROGRESSION_MANAGE)
    family = await ensure_family(guild_id)
    key = season_key.strip()
    start = _parse_date(starts_on, "начала сезона")
    end = _parse_date(ends_on, "конца сезона")
    if end < start:
        raise ProgressionError(
            "Конец сезона не может быть раньше начала. Сезон не объявлен.",
            code="PROG.DATE",
            retryable=True,
        )
    if not key:
        raise ProgressionError(
            "Нужен ключ сезона. Даты в код не зашиваются.",
            code="PROG.SEASON",
            retryable=True,
        )
    existing = await get_family_season_by_key(family.id, key)
    if existing is not None:
        raise ProgressionError(
            "Сезон с таким ключом уже объявлен. Даты не перезаписаны этим ключом.",
            code="PROG.SEASON",
            retryable=True,
        )
    row = await insert_family_season(
        guild_id=guild_id,
        family_id=family.id,
        season_key=key,
        starts_on=start.isoformat(),
        ends_on=end.isoformat(),
        announced_by=_actor_id(actor),
        influence_daily_limit=max(0, influence_daily_limit),
        prize_places=prize_places.strip(),
        half_loss_threshold=max(0, half_loss_threshold),
    )
    registry = get_registry()
    actor_key = str(_actor_id(actor))
    await registry.set(guild_id, "season_key", key, updated_by=actor_key)
    await registry.set(guild_id, "season_start", start.isoformat(), updated_by=actor_key)
    await registry.set(guild_id, "season_end", end.isoformat(), updated_by=actor_key)
    await record_decision(
        guild_id=guild_id,
        action="PROG.SEASON",
        subject_type="family_season",
        subject_id=key,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=f"{start.isoformat()}—{end.isoformat()}",
        source="command",
    )
    return row


async def publish_lore_revision(
    actor: Any,
    guild_id: int,
    revision: str,
    *,
    published_at: str | None = None,
) -> tuple[OrgDevelopment, int]:
    await _require(actor, Permission.PROGRESSION_MANAGE)
    family = await ensure_family(guild_id)
    await ensure_development(guild_id, family.id)
    label = revision.strip()
    if not label:
        raise ProgressionError(
            "Нужен номер сборки или дата публикации. Сведения не обновлены.",
            code="PROG.LORE",
            retryable=True,
        )
    published = published_at.strip() if published_at else datetime.now(UTC).date().isoformat()
    updated = await update_development(
        family.id,
        lore_revision=label,
        lore_published_at=published,
    )
    if updated is None:
        raise ProgressionError(
            "Состояние изменилось. Версию сведений не записали.",
            code="PROG.CONFLICT",
            retryable=True,
        )
    flagged = 0
    for node in await list_upgrades(family.id):
        if node.lore_revision != label:
            await upsert_lore_review(
                guild_id=guild_id,
                family_id=family.id,
                kind="upgrade",
                subject_key=node.key,
                lore_revision=label,
            )
            flagged += 1
    for template_key in EVENT_TEMPLATES:
        await upsert_lore_review(
            guild_id=guild_id,
            family_id=family.id,
            kind="template",
            subject_key=template_key,
            lore_revision=label,
        )
        flagged += 1
    for activity in await list_clearance_activities(family.id):
        await upsert_lore_review(
            guild_id=guild_id,
            family_id=family.id,
            kind="clearance",
            subject_key=activity,
            lore_revision=label,
        )
        flagged += 1
    await record_decision(
        guild_id=guild_id,
        action="PROG.LORE",
        subject_type="lore_revision",
        subject_id=label,
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=published,
        source="command",
    )
    return updated, flagged


async def mark_lore_reviewed(
    actor: Any,
    guild_id: int,
    kind: str,
    subject_key: str,
) -> None:
    await _require(actor, Permission.PROGRESSION_MANAGE)
    family = await ensure_family(guild_id)
    development = await ensure_development(guild_id, family.id)
    if not development.lore_revision:
        raise ProgressionError(
            "Текущая версия сведений не задана. Пересмотр не отмечен.",
            code="PROG.LORE",
            retryable=True,
        )
    if kind not in REVIEW_KINDS:
        raise ProgressionError(
            "Пересматривают улучшение, шаблон или допуск. Отметка не записана.",
            code="PROG.LORE",
            retryable=True,
        )
    await upsert_lore_review(
        guild_id=guild_id,
        family_id=family.id,
        kind=kind,
        subject_key=subject_key.strip(),
        lore_revision=development.lore_revision,
        status=REVIEW_DONE,
    )


async def member_progress_items(
    guild_id: int, discord_id: int
) -> tuple[tuple[str, str], ...]:
    try:
        right = await member_season_right(guild_id, discord_id)
    except ProgressionError:
        return (
            (
                "Сезонное право",
                "Не удалось посчитать право. Откройте карточку позже.",
            ),
        )
    family = await ensure_family(guild_id)
    development = await ensure_development(guild_id, family.id)
    goal = None if development.goal_upgrade_id is None else await get_upgrade(
        development.goal_upgrade_id
    )
    progress = None if goal is None else await get_progress(goal.id)
    levels = await _progress_map(family.id)
    gap = None
    if goal is not None:
        gap = await _requirement_gap(family.id, goal, levels, development)
    hours_line = (
        f"введены человеком ({right.hours} ч), бот игру не читает"
        if right.hours_human
        else "ещё не введены человеком; голос часами игры не является"
    )
    return (
        ("Сезонное право", right.summary),
        ("Часы игры", hours_line),
        ("Цель семьи", _next_step(goal, progress, gap)),
    )


def direction_preview_spec(preview: DirectionPreview) -> MessageSpec:
    return notice(
        code="PROG.DIRECTION_PREVIEW",
        title="Последствия смены направления",
        summary=preview.summary,
        details=(
            ("Сейчас", preview.current),
            ("Станет", preview.target),
            ("Перерыв, дни", str(preview.cooldown_days)),
            ("Потрачено репутации", str(preview.spent_reputation)),
            ("Вернётся", str(preview.returned_reputation)),
            ("Версия состояния", str(preview.version)),
        ),
        next_step=(
            f"Если согласны — {cmd('progress direction')} с подтверждением "
            f"и версией {preview.version}."
        ),
        accent=Accent.STAFF,
        tone=MessageTone.WARNING,
        reference=str(preview.version),
    )


def hours_spec(row: SeasonHours) -> MessageSpec:
    return notice(
        code="PROG.HOURS",
        title="Часы игры за сезон",
        summary=(
            f"Записано {row.hours} ч. Источник — человек, не голосовой канал "
            f"и не клиент игры."
        ),
        details=(
            ("Сезон", row.season_key),
            ("Источник", row.source),
        ),
        next_step=f"Проверка права — {cmd('progress season')}.",
        accent=Accent.STAFF,
        reference=row.season_key,
    )


async def tree_card(actor: Any, guild_id: int) -> MessageSpec:
    del actor
    family = await ensure_family(guild_id)
    await ensure_development(guild_id, family.id)
    nodes = await list_upgrades(family.id)
    levels = await _progress_map(family.id)
    if not nodes:
        details: tuple[tuple[str, str], ...] = (
            ("Дерево", "Старший состав ещё не добавил узлы."),
        )
    else:
        rows: list[tuple[str, str]] = []
        for node in nodes:
            progress = levels.get(node.id)
            level = 0 if progress is None else progress.current_level
            rows.append(
                (f"{node.title} ({node.track})", f"{level}/{node.max_level}")
            )
        details = tuple(rows[:20])
    return notice(
        code="PROG.TREE",
        title="Дерево улучшений",
        summary=(
            "Ветки качаются параллельно, кроме легальных и нелегальных "
            "контрактов. Улучшения сами по себе репутацию не меняют."
        ),
        details=details,
        next_step=f"Цель — {cmd('progress goal')}.",
        accent=Accent.STAFF,
        reference=str(family.id),
    )


async def season_card(actor: Any, guild_id: int, discord_id: int) -> MessageSpec:
    if discord_id != _actor_id(actor):
        await _require(actor, Permission.PROGRESSION_MANAGE)
    right = await member_season_right(guild_id, discord_id)
    staff = await evaluate_access(actor, Permission.PROGRESSION_MANAGE)
    details: list[tuple[str, str]] = [
        ("Членство", "успеваете" if right.days_ok else "не успеваете"),
        ("Вступили", right.joined_on.isoformat()),
        ("Отсечка", right.cutoff.cutoff_date.isoformat()),
        (
            "Часы",
            (
                f"{right.hours} из {right.cutoff.hours_required}, ввёл человек"
                if right.hours_human
                else "ещё не введены человеком"
            ),
        ),
        ("Не хватает часов", str(right.hours_missing)),
    ]
    if staff.allowed:
        cutoff = await admission_cutoff(guild_id)
        details.append(("Приём до", cutoff.cutoff_date.isoformat()))
        details.append(("Кадровику", cutoff.summary))
    return notice(
        code="PROG.SEASON",
        title="Сезонное право",
        summary=right.summary,
        details=tuple(details),
        next_step=(
            f"Часы — {cmd('progress hours')}."
            if not right.eligible
            else "Дополнительных действий не требуется."
        ),
        accent=Accent.STAFF,
        reference=right.cutoff.cutoff_date.isoformat(),
    )


def defined_spec(item: OrgUpgrade) -> MessageSpec:
    return notice(
        code="PROG.DEFINE",
        title="Улучшение добавлено",
        summary=f"{item.title} записано в дерево. Прогресс с нуля.",
        details=(
            ("Ключ", item.key),
            ("Ветка", item.track),
            ("Уровней", str(item.max_level)),
            ("Источник", item.source or "не указан"),
        ),
        next_step=f"Поднять уровень — {cmd('progress advance')}.",
        accent=Accent.STAFF,
        reference=item.key,
    )


def advanced_spec(item: OrgUpgrade, progress: UpgradeProgress) -> MessageSpec:
    return notice(
        code="PROG.ADVANCE",
        title="Уровень поднят",
        summary=f"{item.title}: {progress.current_level}/{item.max_level}.",
        details=(
            ("Ветка", item.track),
            ("Репутацию улучшение не меняет", "да"),
        ),
        next_step=f"Дерево — {cmd('progress tree')}.",
        accent=Accent.STAFF,
        reference=item.key,
    )


def goal_spec(node: OrgUpgrade) -> MessageSpec:
    return notice(
        code="PROG.GOAL",
        title="Текущая цель",
        summary=(
            f"Цель — {node.title}. Оптимальную прокачку бот не заявляет: "
            "виден один следующий шаг."
        ),
        details=(("Ключ", node.key), ("Ветка", node.track)),
        next_step=f"Карточка — {cmd('progress show')}.",
        accent=Accent.STAFF,
        reference=node.key,
    )


def org_level_spec(development: OrgDevelopment) -> MessageSpec:
    level_text = (
        "Уровень записан со слов старшего состава. "
        "Бот игровой уровень сам не читает."
        if development.org_level > 0
        else "Уровень сброшен: требования узлов ждут подтверждения источником."
    )
    return notice(
        code="PROG.ORG_LEVEL",
        title="Уровень организации",
        summary=level_text,
        details=(("Уровень", str(development.org_level)),),
        next_step=f"Дерево — {cmd('progress tree')}.",
        accent=Accent.STAFF,
        reference=str(development.version),
    )


def direction_done_spec(change: DirectionChange) -> MessageSpec:
    if change.before not in DIRECTIONS:
        summary = (
            f"Направление семьи — {change.after}. Это первый выбор: "
            "улучшения не сбрасывали, репутацию не конвертировали."
        )
    else:
        summary = (
            f"Направление {change.before} → {change.after}. Все улучшения сброшены. "
            f"Из потраченных {change.spent} репутации записано {change.returned} "
            "после потери 5%."
        )
    return notice(
        code="PROG.DIRECTION",
        title="Направление семьи",
        summary=summary,
        details=(("Было", change.before), ("Стало", change.after)),
        next_step=f"Дерево — {cmd('progress tree')}.",
        accent=Accent.STAFF,
        tone=MessageTone.WARNING,
        reference=change.after,
    )


def lore_spec(revision: str, flagged: int) -> MessageSpec:
    return notice(
        code="PROG.LORE",
        title="Версия сведений",
        summary=(
            f"Сборка {revision}. К пересмотру отмечено {flagged} "
            "шаблонов, допусков и улучшений. Допуски сами не отзываются."
        ),
        details=(("Сборка или дата", revision),),
        next_step=f"Отметить просмотр — {cmd('progress review')}.",
        accent=Accent.STAFF,
        reference=revision,
    )


def review_spec(kind: str, subject_key: str) -> MessageSpec:
    return notice(
        code="PROG.REVIEW",
        title="Пересмотр отмечен",
        summary=f"{kind} «{subject_key}» сверен с текущей сборкой.",
        details=(("Объект", subject_key),),
        next_step=f"Карточка — {cmd('progress show')}.",
        accent=Accent.STAFF,
        reference=subject_key,
    )


def season_announced_spec(row: FamilySeason) -> MessageSpec:
    influence = (
        str(row.influence_daily_limit)
        if row.influence_daily_limit > 0
        else "не подтверждён"
    )
    prizes = row.prize_places or "не подтверждены"
    half = (
        str(row.half_loss_threshold)
        if row.half_loss_threshold > 0
        else "не подтверждён"
    )
    return notice(
        code="PROG.SEASON_ANNOUNCE",
        title="Сезон объявлен",
        summary=(
            f"{row.season_key}: {row.starts_on} — {row.ends_on}. "
            "Даты скопированы в профиль. Влияние, места и порог потерь "
            "остаются 0, пока состав не заполнит."
        ),
        details=(
            ("Дневной лимит влияния", influence),
            ("Призовые места", prizes),
            ("Порог половинных потерь", half),
        ),
        next_step=f"Отсечка приёма — {cmd('progress season')}.",
        accent=Accent.STAFF,
        reference=row.season_key,
    )
