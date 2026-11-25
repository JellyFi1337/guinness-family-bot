"""Period bonus: propose game-money shares from merits, then 7.1 obligations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.permissions import AccessDecision, Permission, evaluate_access
from core.profile import active_profile
from core.settings import get_registry
from repositories.accounts import find_obligation, insert_obligation
from repositories.bonus import (
    BonusPeriod,
    BonusShare,
    find_period,
    insert_period,
    latest_period,
    list_shares,
    replace_shares,
    update_period,
    update_share_amount,
)
from repositories.contribution import (
    CATEGORIES,
    category_totals_in_range,
    latest_rule_version,
    member_entries_in_range,
)
from services.audit import record_decision
from services.contribution import UNIT_NAME, acknowledge_bonus_payout
from services.membership import ensure_family
from services.treasury import ensure_family_accounts
from ui.command_registry import cmd
from ui.message_specs import MessageSpec, MessageTone
from ui.templates import notice

WEEK_KEY = re.compile(r"^(\d{4})-W(\d{2})$")
POOLS = ("combat", "mentoring", "logistics")
POOL_LABELS = {
    "combat": "боевой",
    "mentoring": "наставники",
    "logistics": "логисты",
}
POOL_CATEGORIES = {
    "combat": ("contract", "attendance", "duty"),
    "mentoring": ("mentoring",),
    "logistics": ("logistics", "warehouse"),
}
STATUS_LABELS = {
    "draft": "черновик",
    "proposed": "предложение",
    "approved": "утверждено",
    "cancelled": "отменено",
}
KIND_LABELS = {"week": "неделя", "rating": "период рейтинга"}
DEFAULT_RANK_THRESHOLDS = (500, 1300, 2600, 4200, 6300, 8700, 11500)
DEFAULT_RANK_NAMES = (
    "Чужаки",
    "Местные",
    "Авангард",
    "Синдикат",
    "Картель",
    "Империя",
    "Орден",
)
DEFAULT_RANK_MULTIPLIER_MILLI = (1100, 1200, 1350, 1500, 1650, 1800, 2000)
DEFAULT_RANK_DC = (100, 250, 450, 750, 1100, 1500, 2000)


class BonusError(Exception):
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
class RankTier:
    threshold: int
    name: str
    multiplier_milli: int
    dc_season: int


@dataclass(frozen=True, slots=True)
class PeriodBundle:
    period: BonusPeriod
    shares: list[BonusShare]


def _actor_id(actor: Any) -> int:
    return int(actor.id)


def _actor_name(actor: Any) -> str:
    return str(getattr(actor, "display_name", None) or getattr(actor, "name", actor.id))


def _require_non_negative_int(value: object, *, code: str, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BonusError(
            f"{label} — неотрицательное целое. Значение не записано. "
            "Можно повторить с целым числом.",
            code=code,
            retryable=True,
        )
    return value


async def _require(actor: Any, permission: Permission) -> None:
    decision = await evaluate_access(actor, permission)
    if decision.allowed:
        return
    raise BonusError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


async def _int_setting(guild_id: int, key: str) -> int:
    raw = await get_registry().get(guild_id, key)
    if raw is None or raw == "":
        return 0
    return int(raw)


async def _text_setting(guild_id: int, key: str, default: str) -> str:
    raw = await get_registry().get(guild_id, key)
    if raw is None or raw == "":
        return default
    return str(raw).strip()


def format_multiplier(milli: int) -> str:
    whole, frac = divmod(int(milli), 1000)
    if frac == 0:
        return f"×{whole}"
    frac_text = f"{frac:03d}".rstrip("0")
    return f"×{whole},{frac_text}"


def default_ranks() -> tuple[RankTier, ...]:
    return tuple(
        RankTier(
            threshold=DEFAULT_RANK_THRESHOLDS[index],
            name=DEFAULT_RANK_NAMES[index],
            multiplier_milli=DEFAULT_RANK_MULTIPLIER_MILLI[index],
            dc_season=DEFAULT_RANK_DC[index],
        )
        for index in range(len(DEFAULT_RANK_THRESHOLDS))
    )


def resolve_rank(influence: int, ranks: tuple[RankTier, ...]) -> RankTier | None:
    if isinstance(influence, bool) or not isinstance(influence, int) or influence < 0:
        raise ValueError("influence must be a non-negative integer")
    current: RankTier | None = None
    for tier in ranks:
        if influence >= tier.threshold:
            current = tier
    return current


def next_rank(
    influence: int, ranks: tuple[RankTier, ...]
) -> tuple[RankTier | None, int | None]:
    current = resolve_rank(influence, ranks)
    if current is None:
        first = ranks[0] if ranks else None
        if first is None:
            return None, None
        return first, first.threshold - influence
    for tier in ranks:
        if tier.threshold > current.threshold:
            return tier, tier.threshold - influence
    return None, None


def distribute_amounts(
    fund: int,
    weights: dict[int, int],
    *,
    max_share_percent: int,
) -> dict[int, int]:
    fund = int(fund)
    if fund <= 0:
        return dict.fromkeys(weights, 0)
    eligible = {
        member_id: points
        for member_id, points in weights.items()
        if points > 0
    }
    if not eligible:
        return dict.fromkeys(weights, 0)
    total = sum(eligible.values())
    raw = {
        member_id: fund * points // total for member_id, points in eligible.items()
    }
    remainder = fund - sum(raw.values())
    order = sorted(
        eligible,
        key=lambda member_id: (
            fund * eligible[member_id] % total,
            eligible[member_id],
            -member_id,
        ),
        reverse=True,
    )
    for member_id in order:
        if remainder <= 0:
            break
        raw[member_id] += 1
        remainder -= 1
    if max_share_percent > 0:
        cap = fund * max_share_percent // 100
        for member_id, amount in raw.items():
            if amount > cap:
                raw[member_id] = cap
    return {member_id: raw.get(member_id, 0) for member_id in weights}


def _pool_points(totals: dict[str, int], pool: str) -> int:
    return sum(max(0, int(totals.get(category, 0))) for category in POOL_CATEGORIES[pool])


def _parse_csv_ints(text: str) -> tuple[int, ...]:
    parts = [chunk.strip() for chunk in text.split(",") if chunk.strip()]
    values: list[int] = []
    for part in parts:
        values.append(int(part))
    return tuple(values)


def parse_ranks(
    *,
    thresholds: str,
    names: str,
    multipliers: str,
    dc_values: str,
) -> tuple[RankTier, ...]:
    if not thresholds.strip() and not names.strip():
        return default_ranks()
    parsed_thresholds = _parse_csv_ints(thresholds)
    parsed_multipliers = _parse_csv_ints(multipliers)
    parsed_dc = _parse_csv_ints(dc_values)
    parsed_names = tuple(chunk.strip() for chunk in names.split(",") if chunk.strip())
    length = len(parsed_thresholds)
    if not (
        length
        and length == len(parsed_names)
        and length == len(parsed_multipliers)
        and length == len(parsed_dc)
    ):
        raise BonusError(
            "Справочник рангов по влиянию задан неполностью. "
            "Четыре списка должны быть одной длины. Значение не записано.",
            code="BONUS.RANKS",
            retryable=True,
        )
    return tuple(
        RankTier(
            threshold=parsed_thresholds[index],
            name=parsed_names[index],
            multiplier_milli=parsed_multipliers[index],
            dc_season=parsed_dc[index],
        )
        for index in range(length)
    )


async def load_ranks(guild_id: int) -> tuple[RankTier, ...]:
    return parse_ranks(
        thresholds=await _text_setting(guild_id, "BONUS_RANK_THRESHOLDS", ""),
        names=await _text_setting(guild_id, "BONUS_RANK_NAMES", ""),
        multipliers=await _text_setting(guild_id, "BONUS_RANK_MULTIPLIER_MILLI", ""),
        dc_values=await _text_setting(guild_id, "BONUS_RANK_DC", ""),
    )


def week_bounds(period_key: str, timezone_name: str) -> tuple[str, str]:
    match = WEEK_KEY.fullmatch(period_key.strip())
    if match is None:
        raise BonusError(
            "Ключ недели выглядит как 2026-W36. Период не создан. "
            "Можно повторить с ключом ISO-недели.",
            code="BONUS.PERIOD",
            retryable=True,
        )
    year = int(match.group(1))
    week = int(match.group(2))
    try:
        start_date = date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise BonusError(
            "Такой ISO-недели нет. Период не создан. Проверьте номер недели.",
            code="BONUS.PERIOD",
            retryable=True,
        ) from exc
    tz = ZoneInfo(timezone_name)
    start_local = datetime.combine(start_date, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=7)
    return (
        start_local.astimezone(UTC).isoformat(),
        end_local.astimezone(UTC).isoformat(),
    )


async def _period_kind(guild_id: int) -> str:
    raw = (await _text_setting(guild_id, "BONUS_PERIOD_KIND", "week")).lower()
    if raw not in ("week", "rating"):
        raise BonusError(
            "Вид периода — week или rating. Значение в настройках не принято.",
            code="BONUS.KIND",
            retryable=True,
        )
    return raw


async def _load_period(guild_id: int, period_key: str) -> BonusPeriod:
    family = await ensure_family(guild_id)
    found = await find_period(family_id=family.id, period_key=period_key.strip())
    if found is None:
        raise BonusError(
            "Период премии не найден. Можно объявить его заново, "
            f"если ключ верный. Код: BONUS.NOT_FOUND. Дальше — {cmd('bonus declare')}.",
            code="BONUS.NOT_FOUND",
            retryable=True,
        )
    return found


def _unallocated(period: BonusPeriod, shares: list[BonusShare]) -> int:
    paid = sum(item.adjusted_amount for item in shares)
    leftover = period.total_fund() - paid
    return leftover if leftover > 0 else 0


def _place_map(shares: list[BonusShare]) -> dict[int, int]:
    totals: dict[int, int] = {}
    for item in shares:
        totals[item.member_discord_id] = (
            totals.get(item.member_discord_id, 0) + item.adjusted_amount
        )
    ranked = sorted(totals, key=lambda member_id: (-totals[member_id], member_id))
    return {member_id: index + 1 for index, member_id in enumerate(ranked)}


async def declare_period(
    actor: Any,
    guild_id: int,
    *,
    period_key: str,
    fund: int,
    mentor_fund: int = 0,
    logistics_fund: int = 0,
    starts_at: str | None = None,
    ends_at: str | None = None,
) -> BonusPeriod:
    await _require(actor, Permission.TREASURY_APPROVE)
    key = period_key.strip()
    if not key:
        raise BonusError(
            "Ключ периода пуст. Период не создан. Укажите неделю или ключ рейтинга.",
            code="BONUS.PERIOD",
            retryable=True,
        )
    fund = _require_non_negative_int(fund, code="BONUS.AMOUNT", label="Фонд")
    mentor_fund = _require_non_negative_int(
        mentor_fund, code="BONUS.AMOUNT", label="Строка наставников"
    )
    logistics_fund = _require_non_negative_int(
        logistics_fund, code="BONUS.AMOUNT", label="Строка логистов"
    )
    if fund + mentor_fund + logistics_fund <= 0:
        raise BonusError(
            "Премиальный фонд в игровых деньгах должен быть больше нуля. "
            "Период не создан. Можно повторить с суммой.",
            code="BONUS.AMOUNT",
            retryable=True,
        )
    family = await ensure_family(guild_id)
    existing = await find_period(family_id=family.id, period_key=key)
    if existing is not None and existing.status == "approved":
        raise BonusError(
            "Этот период уже утверждён. Формула его не пересчитывает. "
            f"Карточка — {cmd('bonus show')}.",
            code="BONUS.CLOSED",
            retryable=False,
        )
    kind = await _period_kind(guild_id)
    start_text = (starts_at or "").strip()
    end_text = (ends_at or "").strip()
    if not start_text or not end_text:
        if kind == "week":
            profile = await active_profile(guild_id)
            start_text, end_text = week_bounds(key, profile.timezone)
        else:
            raise BonusError(
                "Для периода рейтинга нужны начало и конец. Период не создан. "
                "Укажите обе границы.",
                code="BONUS.RANGE",
                retryable=True,
            )
    if start_text >= end_text:
        raise BonusError(
            "Конец периода должен быть позже начала. Период не создан.",
            code="BONUS.RANGE",
            retryable=True,
        )
    min_points = await _int_setting(guild_id, "BONUS_MIN_POINTS")
    max_share = await _int_setting(guild_id, "BONUS_MAX_SHARE_PERCENT")
    if max_share > 100:
        raise BonusError(
            "Потолок доли — целое от 0 до 100. 0 значит «потолка нет».",
            code="BONUS.CAP",
            retryable=True,
        )
    latest = await latest_rule_version(family.id)
    rule_version = latest.version if latest is not None else 0
    if existing is not None and existing.status in {"draft", "proposed"}:
        updated = await update_period(
            existing.id,
            expected_version=existing.version,
            fund_amount=fund,
            mentor_fund=mentor_fund,
            logistics_fund=logistics_fund,
            starts_at=start_text,
            ends_at=end_text,
            min_points=min_points,
            max_share_percent=max_share,
            rule_version=rule_version,
        )
        if updated is None:
            raise BonusError(
                "Период изменили параллельно. Повторите объявление.",
                code="BONUS.CONFLICT",
                retryable=True,
            )
        created = updated
    else:
        created = await insert_period(
            guild_id=guild_id,
            family_id=family.id,
            period_key=key,
            period_kind=kind,
            starts_at=start_text,
            ends_at=end_text,
            fund_amount=fund,
            mentor_fund=mentor_fund,
            logistics_fund=logistics_fund,
            min_points=min_points,
            max_share_percent=max_share,
            rule_version=rule_version,
            declared_by=_actor_id(actor),
        )
    await record_decision(
        guild_id=guild_id,
        action="BONUS.DECLARED",
        subject_type="bonus_period",
        subject_id=str(created.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=key,
        source="command",
    )
    return created


def _pool_fund(period: BonusPeriod, pool: str) -> int:
    if pool == "combat":
        return period.fund_amount
    if pool == "mentoring":
        return period.mentor_fund
    return period.logistics_fund


async def _compute_shares(period: BonusPeriod) -> list[tuple[int, str, int, int, int]]:
    totals = await category_totals_in_range(
        period.family_id, starts_at=period.starts_at, ends_at=period.ends_at
    )
    previous = {
        (item.member_discord_id, item.pool): item
        for item in await list_shares(period.id)
    }
    computed: list[tuple[int, str, int, int, int]] = []
    for pool in POOLS:
        fund = _pool_fund(period, pool)
        if fund <= 0:
            continue
        weights: dict[int, int] = {}
        for member_id, categories in totals.items():
            points = _pool_points(categories, pool)
            if points >= period.min_points and points > 0:
                weights[member_id] = points
        amounts = distribute_amounts(
            fund, weights, max_share_percent=period.max_share_percent
        )
        for member_id, points in weights.items():
            proposed = amounts.get(member_id, 0)
            prev = previous.get((member_id, pool))
            if (
                prev is not None
                and prev.proposed_amount == proposed
                and prev.adjusted_amount != prev.proposed_amount
            ):
                adjusted = prev.adjusted_amount
            else:
                adjusted = proposed
            computed.append((member_id, pool, points, proposed, adjusted))
    return computed


async def _refresh_proposal(period: BonusPeriod) -> PeriodBundle:
    if period.status == "approved":
        return PeriodBundle(period=period, shares=await list_shares(period.id))
    if period.status == "cancelled":
        raise BonusError(
            "Период отменён. Предложение не считается.",
            code="BONUS.CANCELLED",
            retryable=False,
        )
    computed = await _compute_shares(period)
    shares = await replace_shares(
        guild_id=period.guild_id,
        family_id=period.family_id,
        period_id=period.id,
        shares=computed,
    )
    if period.status == "draft":
        updated = await update_period(
            period.id, expected_version=period.version, status="proposed"
        )
        if updated is not None:
            period = updated
    return PeriodBundle(period=period, shares=shares)


async def build_proposal(guild_id: int, period_key: str) -> PeriodBundle:
    period = await _load_period(guild_id, period_key)
    return await _refresh_proposal(period)


async def preview_period(
    actor: Any, guild_id: int, period_key: str
) -> PeriodBundle:
    await _require(actor, Permission.TREASURY_VIEW)
    return await build_proposal(guild_id, period_key)


async def adjust_share(
    actor: Any,
    guild_id: int,
    period_key: str,
    *,
    member_discord_id: int,
    pool: str,
    amount: int,
) -> PeriodBundle:
    await _require(actor, Permission.TREASURY_APPROVE)
    period = await _load_period(guild_id, period_key)
    if period.status == "approved":
        raise BonusError(
            "Утверждённый период нельзя пересчитать. Доли уже зафиксированы.",
            code="BONUS.CLOSED",
            retryable=False,
        )
    if pool not in POOLS:
        raise BonusError(
            "Строка фонда — combat, mentoring или logistics. Доля не изменена.",
            code="BONUS.POOL",
            retryable=True,
        )
    amount = _require_non_negative_int(amount, code="BONUS.AMOUNT", label="Доля")
    if period.status == "draft":
        bundle = await _refresh_proposal(period)
        period = bundle.period
    shares = await list_shares(period.id)
    target = next(
        (
            item
            for item in shares
            if item.member_discord_id == member_discord_id and item.pool == pool
        ),
        None,
    )
    if target is None:
        raise BonusError(
            "У этого человека нет строки в выбранном пуле. "
            "Сначала соберите предложение, затем правьте долю.",
            code="BONUS.SHARE",
            retryable=True,
        )
    updated = await update_share_amount(target.id, adjusted_amount=amount)
    if updated is None:
        raise BonusError(
            "Долю не удалось записать. Можно повторить.",
            code="BONUS.CONFLICT",
            retryable=True,
        )
    await record_decision(
        guild_id=guild_id,
        action="BONUS.ADJUSTED",
        subject_type="bonus_share",
        subject_id=str(updated.id),
        actor_id=_actor_id(actor),
        actor_name=_actor_name(actor),
        after=str(amount),
        source="command",
    )
    return PeriodBundle(period=period, shares=await list_shares(period.id))


async def approve_period(
    actor: Any, guild_id: int, period_key: str
) -> PeriodBundle:
    await _require(actor, Permission.TREASURY_APPROVE)
    period = await _load_period(guild_id, period_key)
    if period.status == "approved":
        return PeriodBundle(period=period, shares=await list_shares(period.id))
    bundle = await _refresh_proposal(period)
    period = bundle.period
    actor_id = _actor_id(actor)
    own = [
        item
        for item in bundle.shares
        if item.member_discord_id == actor_id and item.adjusted_amount > 0
    ]
    if own:
        raise BonusError(
            "Свою премию утвердить нельзя. Попросите другого человека "
            f"с правом согласования. Дальше — {cmd('bonus approve')}.",
            code="BONUS.SELF_APPROVE",
            retryable=True,
        )
    await ensure_family_accounts(guild_id)
    totals: dict[int, int] = {}
    for item in bundle.shares:
        if item.adjusted_amount <= 0:
            continue
        totals[item.member_discord_id] = (
            totals.get(item.member_discord_id, 0) + item.adjusted_amount
        )
    terms = f"bonus:{period.period_key}:rules_v{period.rule_version}"
    for payee_id, amount in totals.items():
        existing = await find_obligation(
            family_id=period.family_id,
            basis_kind="bonus",
            basis_id=f"{period.period_key}:{payee_id}",
        )
        if existing is not None:
            continue
        await insert_obligation(
            guild_id=guild_id,
            family_id=period.family_id,
            basis_kind="bonus",
            basis_id=f"{period.period_key}:{payee_id}",
            payee_id=payee_id,
            currency="game_money",
            amount=amount,
            terms_version=terms,
            promised_by=actor_id,
        )
    updated = await update_period(
        period.id,
        expected_version=period.version,
        status="approved",
        approved_by=actor_id,
    )
    if updated is None:
        raise BonusError(
            "Период изменили параллельно. Повторите утверждение — "
            "второго фонда не будет.",
            code="BONUS.CONFLICT",
            retryable=True,
        )
    await acknowledge_bonus_payout(actor, guild_id, period.period_key)
    await record_decision(
        guild_id=guild_id,
        action="BONUS.APPROVED",
        subject_type="bonus_period",
        subject_id=str(updated.id),
        actor_id=actor_id,
        actor_name=_actor_name(actor),
        after=period.period_key,
        source="command",
    )
    return PeriodBundle(period=updated, shares=await list_shares(updated.id))


async def show_period(
    actor: Any, guild_id: int, period_key: str
) -> PeriodBundle:
    await _require(actor, Permission.TREASURY_VIEW)
    period = await _load_period(guild_id, period_key)
    return PeriodBundle(period=period, shares=await list_shares(period.id))


async def member_view(
    actor: Any, guild_id: int, period_key: str | None = None
) -> tuple[BonusPeriod, list[BonusShare], dict[str, int], int]:
    family = await ensure_family(guild_id)
    if period_key:
        period = await _load_period(guild_id, period_key)
    else:
        found = await latest_period(family.id)
        if found is None:
            raise BonusError(
                "Открытого периода премии нет. Когда лидер объявит фонд, "
                f"здесь появятся ваши заслуги и доля. Дальше — {cmd('bonus ranks')}.",
                code="BONUS.NOT_FOUND",
                retryable=True,
            )
        period = found
    shares = [
        item
        for item in await list_shares(period.id)
        if item.member_discord_id == _actor_id(actor)
    ]
    totals = await category_totals_in_range(
        period.family_id, starts_at=period.starts_at, ends_at=period.ends_at
    )
    categories = totals.get(_actor_id(actor), dict.fromkeys(CATEGORIES, 0))
    place = _place_map(await list_shares(period.id)).get(_actor_id(actor), 0)
    return period, shares, categories, place


async def member_progress_note(guild_id: int, discord_id: int) -> str | None:
    family = await ensure_family(guild_id)
    period = await latest_period(family.id)
    if period is None:
        return None
    shares = [
        item
        for item in await list_shares(period.id)
        if item.member_discord_id == discord_id
    ]
    amount = sum(item.adjusted_amount for item in shares)
    status = STATUS_LABELS.get(period.status, period.status)
    if amount <= 0:
        return f"{period.period_key}: {status}, в распределении пока нет суммы."
    return f"{period.period_key}: {status}, предложенная сумма {amount}."


async def member_breakdown_lines(
    guild_id: int, discord_id: int, period: BonusPeriod
) -> tuple[tuple[str, str], ...]:
    family = await ensure_family(guild_id)
    entries = await member_entries_in_range(
        family.id,
        discord_id,
        starts_at=period.starts_at,
        ends_at=period.ends_at,
    )
    lines: list[tuple[str, str]] = []
    for entry in entries:
        sign = "+" if entry.operation == "award" else "−"
        lines.append(
            (
                f"{sign}{entry.amount} {UNIT_NAME}",
                f"{entry.category} · {entry.basis_kind} {entry.basis_id}",
            )
        )
    return tuple(lines)


async def ranks_view(guild_id: int) -> tuple[tuple[RankTier, ...], int | None, RankTier | None]:
    ranks = await load_ranks(guild_id)
    influence_raw = await _int_setting(guild_id, "BONUS_ORG_INFLUENCE")
    if influence_raw <= 0:
        return ranks, None, None
    return ranks, influence_raw, resolve_rank(influence_raw, ranks)


def period_card_spec(bundle: PeriodBundle) -> MessageSpec:
    period = bundle.period
    leftover = _unallocated(period, bundle.shares)
    places = _place_map(bundle.shares)
    details: list[tuple[str, str]] = [
        (
            "Фонд",
            f"боевой {period.fund_amount} · наставники {period.mentor_fund} · "
            f"логисты {period.logistics_fund}",
        ),
        (
            "Правила",
            f"версия заслуг {period.rule_version} · порог {period.min_points} · "
            f"потолок {period.max_share_percent or 'нет'}%",
        ),
        ("Статус", STATUS_LABELS.get(period.status, period.status)),
        ("Нераспределено", str(leftover)),
    ]
    by_member: dict[int, list[BonusShare]] = {}
    for item in bundle.shares:
        by_member.setdefault(item.member_discord_id, []).append(item)
    for member_id in sorted(by_member, key=lambda item: (places.get(item, 99), item)):
        rows = by_member[member_id]
        total = sum(item.adjusted_amount for item in rows)
        parts = ", ".join(
            f"{POOL_LABELS[item.pool]} {item.adjusted_amount} ({item.points} {UNIT_NAME})"
            for item in rows
        )
        details.append(
            (
                f"Место {places[member_id]} · <@{member_id}>",
                f"{total} · {parts}",
            )
        )
    next_step = (
        f"Выплаты — {cmd('treasury obligation')}."
        if period.status == "approved"
        else f"Правка доли — {cmd('bonus adjust')}. Утверждение — {cmd('bonus approve')}."
    )
    return notice(
        code="BONUS.PERIOD",
        title=f"Премия {period.period_key}",
        summary=(
            f"{KIND_LABELS.get(period.period_kind, period.period_kind)}. "
            "Формула — предложение, не решение. Фонд не смешивается "
            f"с операционным бюджетом и {cmd('fundraiser')}. "
            f"Единица вклада — {UNIT_NAME}."
        ),
        details=tuple(details),
        next_step=next_step,
        reference=str(period.id),
        tone=MessageTone.NEUTRAL,
    )


def member_card_spec(
    period: BonusPeriod,
    shares: list[BonusShare],
    categories: dict[str, int],
    place: int,
    breakdown: tuple[tuple[str, str], ...],
) -> MessageSpec:
    amount = sum(item.adjusted_amount for item in shares)
    details: list[tuple[str, str]] = [
        ("Период", period.period_key),
        ("Место", str(place) if place else "вне распределения"),
        ("Сумма", str(amount)),
    ]
    for key in CATEGORIES:
        value = int(categories.get(key, 0))
        if value:
            details.append((key, f"{value} {UNIT_NAME}"))
    details.extend(breakdown[:8])
    return notice(
        code="BONUS.MINE",
        title="Ваша премия",
        summary=(
            "Здесь ваши заслуги за период, из чего они сложились, "
            "место в распределении и сумма. Dark Coins сервер считает сам — "
            f"семья число не обещает. Справочник ранга — {cmd('bonus ranks')}."
        ),
        details=tuple(details) or (("Заслуги", "за период записей нет"),),
        next_step=f"Карточка периода — {cmd('bonus show')}.",
        reference=period.period_key,
        tone=MessageTone.NEUTRAL,
    )


def ranks_card_spec(
    ranks: tuple[RankTier, ...],
    influence: int | None,
    current: RankTier | None,
) -> MessageSpec:
    details: list[tuple[str, str]] = []
    if influence is None:
        details.append(
            (
                "Влияние семьи",
                "не подтверждено. Ранг не считается, пока старший состав "
                "не укажет BONUS_ORG_INFLUENCE.",
            )
        )
    else:
        nxt, need = next_rank(influence, ranks)
        if current is None:
            first = ranks[0] if ranks else None
            gap = first.threshold - influence if first is not None else 0
            details.append(
                (
                    "Сейчас",
                    f"влияние {influence}, ниже первого порога "
                    f"(ещё {gap} до {first.name if first else 'таблицы'}).",
                )
            )
        else:
            details.append(
                (
                    "Сейчас",
                    f"{current.name}: влияние {influence}, "
                    f"множитель {format_multiplier(current.multiplier_milli)}, "
                    f"справочник разового начисления ранга {current.dc_season}.",
                )
            )
            if nxt is not None and need is not None:
                details.append(
                    (
                        "До следующего",
                        f"{need} влияния до {nxt.name} "
                        f"({format_multiplier(nxt.multiplier_milli)}).",
                    )
                )
            else:
                details.append(("До следующего", "выше таблицы нет."))
    for tier in ranks:
        details.append(
            (
                f"{tier.threshold}+ · {tier.name}",
                f"{format_multiplier(tier.multiplier_milli)} · "
                f"справочник DC ранга {tier.dc_season}",
            )
        )
    return notice(
        code="BONUS.RANKS",
        title="Ранг по влиянию",
        summary=(
            "Справочник сезона: множитель и разовое начисление Dark Coins "
            "считает сервер по местам и рангу в конце сезона. "
            "Семья конкретное число Dark Coins не обещает и не выплачивает."
        ),
        details=tuple(details),
        next_step=f"Премия семьи — {cmd('bonus show')}.",
        tone=MessageTone.NEUTRAL,
    )
