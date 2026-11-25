"""Non-transferable merits (заслуги) for confirmed family results."""

from __future__ import annotations

from typing import Any

from core.permissions import AccessDecision
from core.settings import get_registry
from repositories.contribution import (
    CATEGORIES,
    ContributionEntry,
    RuleVersion,
    find_entry,
    get_entry,
    insert_bonus_mark,
    insert_entry,
    insert_rule_version,
    latest_rule_version,
    list_entries_for_basis,
    list_entries_for_member,
    member_category_totals,
)
from repositories.events import get_seat
from repositories.members import get_member
from repositories.reports import get_report
from repositories.training import get_assignment
from services.audit import record_decision
from services.membership import ensure_family

UNIT_NAME = "заслуги"
SETTING_KEYS = {
    "contract": "CONTRIBUTION_CONTRACT",
    "attendance": "CONTRIBUTION_ATTENDANCE",
    "mentoring": "CONTRIBUTION_MENTORING",
    "warehouse": "CONTRIBUTION_WAREHOUSE",
    "logistics": "CONTRIBUTION_LOGISTICS",
    "duty": "CONTRIBUTION_DUTY",
}
CATEGORY_LABELS = {
    "contract": "контракт",
    "attendance": "явка",
    "mentoring": "наставничество",
    "warehouse": "склад",
    "logistics": "логистика",
    "duty": "дежурство",
}


class ContributionError(Exception):
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


def _actor_id(actor: Any) -> int:
    return int(actor.id)


def _actor_name(actor: Any) -> str:
    return str(getattr(actor, "display_name", None) or getattr(actor, "name", actor.id))


async def _settings_amounts(guild_id: int) -> dict[str, int]:
    registry = get_registry()
    amounts: dict[str, int] = {}
    for category, key in SETTING_KEYS.items():
        raw = await registry.get(guild_id, key)
        amounts[category] = 0 if raw is None or raw == "" else int(raw)
    return amounts


def _same_amounts(version: RuleVersion, amounts: dict[str, int]) -> bool:
    return all(version.amount_for(key) == amounts[key] for key in CATEGORIES)


async def resolve_rules(guild_id: int, actor_id: int) -> RuleVersion | None:
    family = await ensure_family(guild_id)
    settings = await _settings_amounts(guild_id)
    latest = await latest_rule_version(family.id)
    if latest is not None and _same_amounts(latest, settings):
        return latest
    if latest is not None and all(value == 0 for value in settings.values()):
        return latest
    if all(value == 0 for value in settings.values()) and latest is None:
        return None
    created = await insert_rule_version(
        guild_id=guild_id,
        family_id=family.id,
        amounts=settings,
        created_by=actor_id,
    )
    await record_decision(
        guild_id=guild_id,
        action="CONTRIB.RULES",
        subject_type="contribution_rules",
        subject_id=str(created.id),
        actor_id=actor_id,
        actor_name=str(actor_id),
        after=str(created.version),
        source="system",
    )
    return created


async def publish_rules(
    actor: Any, guild_id: int, amounts: dict[str, int]
) -> RuleVersion:
    family = await ensure_family(guild_id)
    cleaned: dict[str, int] = {}
    for key in CATEGORIES:
        raw = amounts.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ContributionError(
                "Сумма заслуг — неотрицательное целое. Версия правил не записана.",
                code="CONTRIB.AMOUNT",
                retryable=True,
            )
        cleaned[key] = raw
    created = await insert_rule_version(
        guild_id=guild_id,
        family_id=family.id,
        amounts=cleaned,
        created_by=_actor_id(actor),
    )
    await record_decision(
        guild_id=guild_id,
        action="CONTRIB.RULES",
        subject_type="contribution_rules",
        subject_id=str(created.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=str(created.version),
        source="command",
    )
    return created


async def _award(
    actor: Any,
    guild_id: int,
    *,
    member_discord_id: int,
    category: str,
    basis_kind: str,
    basis_id: str,
) -> ContributionEntry | None:
    family = await ensure_family(guild_id)
    existing = await find_entry(
        family_id=family.id,
        member_discord_id=member_discord_id,
        basis_kind=basis_kind,
        basis_id=basis_id,
        operation="award",
    )
    if existing is not None:
        return existing
    rules = await resolve_rules(guild_id, _actor_id(actor))
    if rules is None:
        return None
    amount = rules.amount_for(category)
    if amount <= 0:
        return None
    created = await insert_entry(
        guild_id=guild_id,
        family_id=family.id,
        member_discord_id=member_discord_id,
        category=category,
        amount=amount,
        operation="award",
        basis_kind=basis_kind,
        basis_id=basis_id,
        rule_version=rules.version,
        actor_id=_actor_id(actor),
    )
    await record_decision(
        guild_id=guild_id,
        action="CONTRIB.AWARD",
        subject_type="contribution_entry",
        subject_id=str(created.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=f"{category}:{amount}",
        source="system",
    )
    return created


async def reverse_entry(
    actor: Any, guild_id: int, entry_id: int, *, reason: str
) -> ContributionEntry:
    family = await ensure_family(guild_id)
    entry = await get_entry(entry_id)
    if entry is None or entry.family_id != family.id:
        raise ContributionError(
            "Запись заслуг не найдена. Отмена не создана.",
            code="CONTRIB.NOT_FOUND",
            retryable=True,
        )
    if entry.operation == "reverse":
        raise ContributionError(
            "Отменяющую запись ещё раз отменить нельзя.",
            code="CONTRIB.ALREADY_REVERSED",
        )
    text = reason.strip()
    if not text:
        raise ContributionError(
            "Укажите причину отмены. Журнал заслуг не изменён.",
            code="CONTRIB.REASON",
            retryable=True,
        )
    created = await insert_entry(
        guild_id=guild_id,
        family_id=family.id,
        member_discord_id=entry.member_discord_id,
        category=entry.category,
        amount=entry.amount,
        operation="reverse",
        basis_kind="reverse",
        basis_id=str(entry.id),
        rule_version=entry.rule_version,
        actor_id=_actor_id(actor),
        reversal_of=entry.id,
    )
    await record_decision(
        guild_id=guild_id,
        action="CONTRIB.REVERSE",
        subject_type="contribution_entry",
        subject_id=str(entry.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        reason=text,
        source="command",
    )
    return created


async def _reverse_basis(
    actor: Any, guild_id: int, basis_kind: str, basis_id: str, *, reason: str
) -> list[ContributionEntry]:
    family = await ensure_family(guild_id)
    reversed_rows: list[ContributionEntry] = []
    for entry in await list_entries_for_basis(family.id, basis_kind, basis_id):
        if entry.operation != "award":
            continue
        already = await find_entry(
            family_id=family.id,
            member_discord_id=entry.member_discord_id,
            basis_kind="reverse",
            basis_id=str(entry.id),
            operation="reverse",
        )
        if already is not None:
            reversed_rows.append(already)
            continue
        reversed_rows.append(
            await reverse_entry(actor, guild_id, entry.id, reason=reason)
        )
    return reversed_rows


async def award_from_accepted_report(
    actor: Any, guild_id: int, report_id: int
) -> list[ContributionEntry]:
    report = await get_report(report_id)
    if report is None or report.guild_id != guild_id:
        raise ContributionError(
            "Отчёт не найден. Заслуги не начислены.",
            code="CONTRIB.NOT_FOUND",
            retryable=True,
        )
    if report.status != "accepted":
        raise ContributionError(
            "Заслуги пишут только по принятому отчёту. Начисление не записано.",
            code="CONTRIB.NOT_CONFIRMED",
        )
    awarded: list[ContributionEntry] = []
    for share in report.shares:
        item = await _award(
            actor,
            guild_id,
            member_discord_id=share.discord_id,
            category="contract",
            basis_kind="report",
            basis_id=str(report.id),
        )
        if item is not None:
            awarded.append(item)
    return awarded


async def reverse_from_report(
    actor: Any, guild_id: int, report_id: int, *, reason: str
) -> list[ContributionEntry]:
    return await _reverse_basis(
        actor, guild_id, "report", str(report_id), reason=reason
    )


async def award_from_attendance(
    actor: Any, guild_id: int, event_id: int, discord_id: int
) -> ContributionEntry | None:
    seat = await get_seat(event_id, discord_id)
    if seat is None or seat.guild_id != guild_id:
        raise ContributionError(
            "Участника нет в составе. Заслуги за явку не начислены.",
            code="CONTRIB.NOT_FOUND",
            retryable=True,
        )
    if seat.attendance != "attended":
        raise ContributionError(
            "Заслуги за явку пишет только отметка командира «участвовал». "
            "Голос и готовность не считаются.",
            code="CONTRIB.NOT_CONFIRMED",
        )
    return await _award(
        actor,
        guild_id,
        member_discord_id=discord_id,
        category="attendance",
        basis_kind="attendance",
        basis_id=f"{event_id}:{discord_id}",
    )


async def reverse_from_attendance(
    actor: Any, guild_id: int, event_id: int, discord_id: int, *, reason: str
) -> list[ContributionEntry]:
    return await _reverse_basis(
        actor,
        guild_id,
        "attendance",
        f"{event_id}:{discord_id}",
        reason=reason,
    )


async def award_from_training(
    actor: Any, guild_id: int, assignment_id: int
) -> ContributionEntry | None:
    item = await get_assignment(assignment_id)
    if item is None or item.guild_id != guild_id:
        raise ContributionError(
            "Задание не найдено. Заслуги наставника не начислены.",
            code="CONTRIB.NOT_FOUND",
            retryable=True,
        )
    if item.status != "done":
        raise ContributionError(
            "Заслуги наставника пишут только после закрытого обучения новичка.",
            code="CONTRIB.NOT_CONFIRMED",
        )
    if item.mentor_id is None:
        return None
    mentor = await get_member(item.mentor_id)
    if mentor is None:
        return None
    return await _award(
        actor,
        guild_id,
        member_discord_id=mentor.discord_id,
        category="mentoring",
        basis_kind="training",
        basis_id=str(item.id),
    )


async def award_from_warehouse(
    actor: Any, guild_id: int, movement_id: int
) -> ContributionEntry | None:
    from repositories.inventory import get_location, get_movement

    movement = await get_movement(movement_id)
    if movement is None or movement.guild_id != guild_id:
        raise ContributionError(
            "Движение склада не найдено. Заслуги не начислены.",
            code="CONTRIB.NOT_FOUND",
            retryable=True,
        )
    if movement.operation != "receive" or movement.to_location_id is None:
        raise ContributionError(
            "Заслуги за сдачу пишут только по поступлению на склад организации.",
            code="CONTRIB.NOT_CONFIRMED",
        )
    location = await get_location(movement.to_location_id)
    if location is None or location.kind != "warehouse":
        raise ContributionError(
            "Заслуги за сдачу пишут только по складу организации, не по другим местам.",
            code="CONTRIB.NOT_CONFIRMED",
        )
    return await _award(
        actor,
        guild_id,
        member_discord_id=movement.actor_id,
        category="warehouse",
        basis_kind="warehouse",
        basis_id=str(movement.id),
    )


async def award_from_logistics(
    actor: Any, guild_id: int, kit_id: int
) -> ContributionEntry | None:
    from repositories.inventory import get_kit

    kit = await get_kit(kit_id)
    if kit is None or kit.guild_id != guild_id:
        raise ContributionError(
            "Комплект не найден. Заслуги логиста не начислены.",
            code="CONTRIB.NOT_FOUND",
            retryable=True,
        )
    if kit.status not in {"issued", "returned"} or kit.issued_by is None:
        raise ContributionError(
            "Заслуги логиста пишут только после выдачи комплекта.",
            code="CONTRIB.NOT_CONFIRMED",
        )
    return await _award(
        actor,
        guild_id,
        member_discord_id=kit.issued_by,
        category="logistics",
        basis_kind="kit",
        basis_id=str(kit.id),
    )


async def award_from_duty(*_args: Any, **_kwargs: Any) -> None:
    raise ContributionError(
        "Дежурство ещё не подтверждается отдельным модулем. "
        "Заслуги не начислены.",
        code="CONTRIB.NOT_READY",
    )


async def acknowledge_bonus_payout(
    actor: Any, guild_id: int, period_key: str
) -> None:
    family = await ensure_family(guild_id)
    text = period_key.strip()
    if not text:
        raise ContributionError(
            "Укажите период премии. Заслуги не менялись.",
            code="CONTRIB.PERIOD",
            retryable=True,
        )
    await insert_bonus_mark(
        guild_id=guild_id,
        family_id=family.id,
        period_key=text,
        marked_by=_actor_id(actor),
    )


async def member_breakdown(guild_id: int, discord_id: int) -> dict[str, int]:
    family = await ensure_family(guild_id)
    return await member_category_totals(family.id, discord_id)


async def member_history(guild_id: int, discord_id: int) -> list[ContributionEntry]:
    family = await ensure_family(guild_id)
    return await list_entries_for_member(family.id, discord_id)


def panel_lines(totals: dict[str, int]) -> tuple[tuple[str, str], ...]:
    if not any(totals.values()):
        return (
            (
                "Заслуги",
                "Подтверждённого результата ещё нет. "
                "Сообщения, голос и VIP сюда не входят.",
            ),
        )
    lines = []
    for key in CATEGORIES:
        value = totals.get(key, 0)
        if value <= 0:
            continue
        lines.append(
            (
                f"Заслуги · {CATEGORY_LABELS[key]}",
                f"{value} {UNIT_NAME}. Общего рейтинга нет.",
            )
        )
    return tuple(lines)


def ensure_contribution_integrations() -> None:
    return


ensure_contribution_integrations()
