"""Memphis API panels: rating, battles, market and property reference.

Missing numbers stay missing. Foreign-family player fields are never rendered.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from core.profile import active_profile
from repositories.events import FamilyEvent, list_recent_events, list_roster
from repositories.family import get_active_family
from repositories.game_online import get_external_snapshot
from repositories.members import get_current_member, list_clearances
from repositories.static_messages import get_guild_setting, set_guild_setting
from services.bonus import RankTier, load_ranks, next_rank, resolve_rank
from services.external.base import (
    RATING_PERIODS,
    RESOURCE_MANSIONS,
    SOURCE_MAJESTIC_API,
    ResourceSnapshot,
    format_fetched_clock,
    snapshot_from_row,
)
from services.external.majestic_api import (
    MajesticApiClient,
    majestic_integration_ready,
)
from services.profit import PriceHint, market_price_hint
from ui.message_specs import MessageKind, MessageSpec, MessageTone

PERIOD_LABELS = {
    "week": "Неделя",
    "period": "Период",
    "season": "Сезон",
}
INFLUENCE_SETTING = "insights:rating:{period}:influence"
POSTED_BATTLES_SETTING = "insights:posted:battles"
_PLAYER_FIELD_KEYS = frozenset(
    {
        "nick",
        "nickname",
        "username",
        "user",
        "userid",
        "user_id",
        "userId",
        "player",
        "playerid",
        "player_id",
        "playerId",
        "masteruser",
        "master-user",
        "masterUser",
        "account",
        "discord",
        "staticid",
        "static_id",
        "staticId",
    }
)
_ORG_NAME_KEYS = (
    "name",
    "title",
    "orgName",
    "organization",
    "family",
    "tag",
    "shortName",
    "org",
)
_PLACE_KEYS = ("place", "rank", "position", "placeNumber", "positionNumber")
_INFLUENCE_KEYS = ("influence", "score", "points", "rating")
_PRICE_KEYS = ("price", "cost", "amount", "value", "minPrice")
_ITEM_NAME_KEYS = ("name", "title", "item", "itemName", "model", "label")
_BATTLE_TITLE_KEYS = ("title", "territory", "location", "place", "area", "zone", "name")
_BATTLE_RESULT_KEYS = ("result", "status", "outcome")
_BATTLE_ID_KEYS = ("id", "eventId", "captureId", "warId", "uuid")
_BATTLE_TIME_KEYS = ("startsAt", "startedAt", "startAt", "date", "time", "createdAt", "at")
_F2_KEYS = ("f2_event_ref", "f2EventRef", "f2", "eventRef")
_LIST_KEYS = (
    "data",
    "items",
    "results",
    "organizations",
    "captures",
    "wars",
    "mansions",
    "multipurpose",
)
_CATALOG_LIMIT = 12
_FEED_LIMIT = 8


@dataclass(frozen=True, slots=True)
class OrgRatingView:
    found: bool
    place: int | None
    influence: int | None


@dataclass(frozen=True, slots=True)
class BattleRow:
    identity: str
    kind: str
    title: str
    result: str | None
    occurred_at: str | None
    f2_ref: str | None
    payload: Mapping[str, Any]


def parse_optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def influence_until_next_rank(
    influence: int | None, ranks: tuple[RankTier, ...]
) -> str:
    if influence is None:
        return "нет данных об влиянии"
    nxt, gap = next_rank(influence, ranks)
    current = resolve_rank(influence, ranks)
    if nxt is None:
        if current is None:
            return "шкала рангов пуста"
        return f"следующего нет, текущий «{current.name}»"
    return f"до «{nxt.name}»: {gap}"


def format_influence_change(current: int | None, previous: int | None) -> str:
    if current is None:
        return "нет в ответе"
    if previous is None:
        return "изменение пока неизвестно"
    delta = current - previous
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def _as_items(payload: object) -> list[Mapping[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in _LIST_KEYS:
        nested = payload.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, Mapping)]
        if isinstance(nested, Mapping):
            return [nested]
    return [payload]


def _text_field(row: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        if key.casefold() in _PLAYER_FIELD_KEYS:
            continue
        raw = row.get(key)
        if isinstance(raw, bool) or raw is None:
            continue
        if isinstance(raw, int):
            continue
        text = str(raw).strip()
        if text:
            return text
    return None


def _first_int(row: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        parsed = parse_optional_int(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _org_matches(
    row: Mapping[str, Any], family_name: str, family_tag: str
) -> bool:
    needles = [
        item.casefold()
        for item in (family_name.strip(), family_tag.strip())
        if item.strip()
    ]
    if not needles:
        return False
    names: list[str] = []
    for key in _ORG_NAME_KEYS:
        raw = row.get(key)
        if isinstance(raw, str) and raw.strip():
            names.append(raw.strip())
    haystack = " ".join(names).casefold()
    return any(needle in haystack for needle in needles)


def extract_org_rating(
    payload: object, family_name: str, family_tag: str
) -> OrgRatingView:
    items = _as_items(payload)
    matches = [
        item for item in items if _org_matches(item, family_name, family_tag)
    ]
    if matches:
        row = matches[0]
        return OrgRatingView(
            found=True,
            place=_first_int(row, _PLACE_KEYS),
            influence=_first_int(row, _INFLUENCE_KEYS),
        )
    if len(items) == 1:
        row = items[0]
        named = any(
            isinstance(row.get(key), str) and str(row.get(key)).strip()
            for key in _ORG_NAME_KEYS
        )
        if named:
            return OrgRatingView(found=False, place=None, influence=None)
        return OrgRatingView(
            found=True,
            place=_first_int(row, _PLACE_KEYS),
            influence=_first_int(row, _INFLUENCE_KEYS),
        )
    return OrgRatingView(found=False, place=None, influence=None)


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        seconds = value
        if seconds >= 1_000_000_000_000:
            seconds = seconds // 1000
        if seconds < 1_000_000_000:
            return None
        return datetime.fromtimestamp(seconds, tz=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _minute_key(stamp: datetime) -> tuple[int, int, int, int, int]:
    utc = stamp.astimezone(UTC)
    return (utc.year, utc.month, utc.day, utc.hour, utc.minute)


def _battle_identity(kind: str, row: Mapping[str, Any]) -> str | None:
    for key in _BATTLE_ID_KEYS:
        raw = row.get(key)
        if isinstance(raw, bool) or raw is None:
            continue
        text = str(raw).strip()
        if text:
            return f"{kind}:{text}"
    title = _text_field(row, _BATTLE_TITLE_KEYS) or ""
    occurred = _text_field(row, _BATTLE_TIME_KEYS) or ""
    if not title and not occurred:
        return None
    return f"{kind}:{title}:{occurred}"


def extract_battles(kind: str, payload: object) -> list[BattleRow]:
    rows: list[BattleRow] = []
    for item in _as_items(payload):
        identity = _battle_identity(kind, item)
        if identity is None:
            continue
        rows.append(
            BattleRow(
                identity=identity,
                kind=kind,
                title=_text_field(item, _BATTLE_TITLE_KEYS) or "без названия",
                result=_text_field(item, _BATTLE_RESULT_KEYS),
                occurred_at=_text_field(item, _BATTLE_TIME_KEYS),
                f2_ref=_text_field(item, _F2_KEYS),
                payload=item,
            )
        )
    return rows


def match_event(
    row: BattleRow, events: Sequence[FamilyEvent]
) -> FamilyEvent | None:
    if row.f2_ref:
        for event in events:
            if event.f2_event_ref and event.f2_event_ref == row.f2_ref:
                return event
    battle_ts = None
    if row.occurred_at:
        battle_ts = _parse_timestamp(row.occurred_at)
    if battle_ts is None:
        for key in _BATTLE_TIME_KEYS:
            battle_ts = _parse_timestamp(row.payload.get(key))
            if battle_ts is not None:
                break
    if battle_ts is None:
        return None
    wanted = _minute_key(battle_ts)
    capt_only = row.kind == "капт"
    for event in events:
        if capt_only and not event.template_key.startswith("capt_"):
            continue
        event_ts = _parse_timestamp(event.starts_at)
        if event_ts is None:
            continue
        if _minute_key(event_ts) == wanted:
            return event
    return None


def _format_missing_value(value: int | None) -> str:
    if value is None:
        return "нет в ответе"
    return str(value)


def _stale_summary(snapshots: Sequence[ResourceSnapshot]) -> str | None:
    for snapshot in snapshots:
        if snapshot.stale and snapshot.fetched_at:
            clock = format_fetched_clock(snapshot.fetched_at)
            if clock:
                return f"Обновление не удалось, показаны данные от {clock}."
        if snapshot.unavailable_reason and snapshot.payload_text is None:
            return (
                "Источник сейчас недоступен. Место и влияние не подставляются."
            )
    return None


def _apply_source(spec: MessageSpec, snapshots: Sequence[ResourceSnapshot]) -> MessageSpec:
    trusted = next((item for item in snapshots if item.fetched_at), None)
    snapshot = trusted or (snapshots[0] if snapshots else None)
    if snapshot is None:
        return spec
    return replace(
        spec,
        source_label=snapshot.source_label,
        source_fetched_at=snapshot.fetched_at,
        source_stale=snapshot.stale,
        source_attribution=snapshot.attribution or None,
    )


def render_rating_spec(
    *,
    family_name: str,
    family_tag: str = "",
    ranks: tuple[RankTier, ...],
    periods: Mapping[str, ResourceSnapshot],
    previous: Mapping[str, int | None],
) -> MessageSpec:
    snapshots = tuple(periods[key] for key in ("week", "period", "season") if key in periods)
    stale = _stale_summary(snapshots)
    details: list[tuple[str, str]] = []
    for period in ("week", "period", "season"):
        snapshot = periods.get(period)
        label = PERIOD_LABELS[period]
        if snapshot is None or snapshot.payload_text is None:
            details.append((label, "нет в ответе"))
            continue
        view = extract_org_rating(snapshot.payload(), family_name, family_tag)
        if not view.found:
            details.append(
                (
                    label,
                    f"организация «{family_name}» в ответе не найдена; "
                    "чужие семьи не показываются",
                )
            )
            continue
        gap = influence_until_next_rank(view.influence, ranks)
        change = format_influence_change(view.influence, previous.get(period))
        details.append(
            (
                label,
                (
                    f"место {_format_missing_value(view.place)}, "
                    f"влияние {_format_missing_value(view.influence)}, "
                    f"с прошлой проверки {change}, {gap}"
                ),
            )
        )
    summary = stale or (
        "Рейтинг Memphis по данным API. В игре это видит один человек в F2 — "
        "здесь карточку видят все."
    )
    spec = MessageSpec(
        code="INSIGHT.RATING",
        title="Рейтинг Memphis",
        summary=summary,
        details=tuple(details),
        next_step="Цифры из игры. Нулевые значения бот не подставляет.",
        tone=MessageTone.NEUTRAL,
        kind=MessageKind.PUBLIC_PANEL,
    )
    return _apply_source(spec, snapshots)


def _catalog_lines(payload: object, *, with_price: bool) -> list[str]:
    lines: list[str] = []
    for item in _as_items(payload):
        name = _text_field(item, _ITEM_NAME_KEYS)
        if name is None:
            continue
        if with_price:
            price = _first_int(item, _PRICE_KEYS)
            if price is None:
                lines.append(f"{name} — цена не указана")
            else:
                lines.append(f"{name} — оценка {price}")
        else:
            extra = _text_field(item, ("type", "kind", "district", "zone"))
            lines.append(f"{name}" + (f" · {extra}" if extra else ""))
        if len(lines) >= _CATALOG_LIMIT:
            break
    return lines


def find_market_price(payload: object, item_name: str) -> int | None:
    needle = item_name.strip().casefold()
    if not needle:
        return None
    for item in _as_items(payload):
        name = _text_field(item, _ITEM_NAME_KEYS)
        if name is None or name.casefold() != needle:
            continue
        return _first_int(item, _PRICE_KEYS)
    return None


def render_market_spec(snapshots: Sequence[ResourceSnapshot]) -> MessageSpec:
    lines: list[str] = []
    for snapshot in snapshots:
        if snapshot.payload_text is None:
            continue
        kind = "предметы" if snapshot.resource.endswith("items") else "транспорт"
        rows = _catalog_lines(snapshot.payload(), with_price=True)
        if rows:
            lines.append(f"{kind}: " + "; ".join(rows))
    stale = _stale_summary(tuple(snapshots))
    if not lines:
        summary = stale or "В справочнике рынка пока нет строк. Цены не подставляются."
    else:
        summary = stale or (
            "Опорные цены Memphis. Каждая сумма — рыночная оценка на дату снимка, "
            "не гарантия сделки."
        )
    spec = MessageSpec(
        code="INSIGHT.MARKET",
        title="Рынок Memphis",
        summary=summary,
        details=(("Справочник", "\n".join(lines)),) if lines else (),
        next_step="Оценку сверяйте с игрой; бот не подставляет ноль вместо цены.",
        tone=MessageTone.NEUTRAL,
        kind=MessageKind.PUBLIC_PANEL,
    )
    return _apply_source(spec, snapshots)


def render_property_spec(snapshots: Sequence[ResourceSnapshot]) -> MessageSpec:
    lines: list[str] = []
    for snapshot in snapshots:
        if snapshot.payload_text is None:
            continue
        label = "особняки" if snapshot.resource == RESOURCE_MANSIONS else "ПСН"
        rows = _catalog_lines(snapshot.payload(), with_price=False)
        if rows:
            lines.append(f"{label}: " + "; ".join(rows))
    stale = _stale_summary(tuple(snapshots))
    if not lines:
        summary = stale or "Справочник ПСН и особняков пуст. Значения не выдумываются."
    else:
        summary = stale or (
            "Справочник ПСН и особняков Memphis для развития и производства."
        )
    spec = MessageSpec(
        code="INSIGHT.PROPERTY",
        title="ПСН и особняки",
        summary=summary,
        details=(("Каталог", "\n".join(lines)),) if lines else (),
        next_step="Поля, которых нет в ответе, бот не заполняет.",
        tone=MessageTone.NEUTRAL,
        kind=MessageKind.PUBLIC_PANEL,
    )
    return _apply_source(spec, snapshots)


def unmatched_event_text() -> str:
    return (
        "Событие в календаре не найдено — состав и допуски бот не сверял."
    )


async def roster_gap_report(event: FamilyEvent) -> str:
    seats = await list_roster(event.id)
    main = [seat for seat in seats if seat.slot == "main"]
    ready = sum(1 for seat in main if seat.attendance == "ready")
    no_show = sum(1 for seat in seats if seat.attendance == "no_show")
    parts = [
        f"в основе {len(main)} из {event.capacity}",
        f"готовы {ready}",
        f"неявки {no_show}",
    ]
    raw_min = event.snapshot.get("min_participants")
    minimum = parse_optional_int(raw_min)
    if minimum is not None and minimum > 0 and len(main) < minimum:
        parts.append(f"не хватает до минимума шаблона ещё {minimum - len(main)}")
    elif len(main) < event.capacity:
        parts.append(f"не хватает ещё {event.capacity - len(main)}")
    activity = event.snapshot.get("rule_activity")
    if isinstance(activity, str) and activity.strip():
        missing = 0
        for seat in seats:
            member = await get_current_member(event.family_id, seat.discord_id)
            if member is None:
                missing += 1
                continue
            clearances = await list_clearances(member.id)
            if not any(
                item.activity == activity and item.revoked_at is None
                for item in clearances
            ):
                missing += 1
        if missing:
            parts.append(f"без допуска «{activity}»: {missing}")
        else:
            parts.append(f"допуск «{activity}» есть у всех записанных")
    return "; ".join(parts)


def _battle_line(row: BattleRow, event: FamilyEvent | None, roster: str) -> str:
    bits = [row.kind, row.title]
    if row.result:
        bits.append(row.result)
    if event is None:
        bits.append(unmatched_event_text())
    else:
        bits.append(f"событие #{event.id} «{event.title}»")
        bits.append(roster)
    return " · ".join(bits)


async def render_battles_spec(
    *,
    captures: ResourceSnapshot,
    wars: ResourceSnapshot,
    events: Sequence[FamilyEvent],
) -> MessageSpec:
    rows = extract_battles("капт", captures.payload()) + extract_battles(
        "война семей", wars.payload()
    )
    details: list[tuple[str, str]] = []
    leadership = unmatched_event_text()
    feed: list[str] = []
    for row in rows[:_FEED_LIMIT]:
        event = match_event(row, events)
        roster = await roster_gap_report(event) if event is not None else unmatched_event_text()
        if event is not None and leadership == unmatched_event_text():
            leadership = roster
        feed.append(_battle_line(row, event, roster))
    details.append(("Состав и допуски", leadership))
    if feed:
        details.append(("Лента", "\n".join(feed)))
    else:
        details.append(("Лента", "В ответе API нет строк каптов и войн семей."))
    snapshots = (captures, wars)
    stale = _stale_summary(snapshots)
    spec = MessageSpec(
        code="INSIGHT.BATTLES",
        title="Разборы каптов и войн семей",
        summary=stale
        or "Результаты сражений Memphis. Командиру остаётся дописать вывод.",
        details=tuple(details),
        next_step="Имена игроков чужих семей бот не публикует.",
        tone=MessageTone.NEUTRAL,
        kind=MessageKind.PUBLIC_PANEL,
    )
    return _apply_source(spec, snapshots)


def render_battle_card(
    row: BattleRow,
    *,
    event: FamilyEvent | None,
    roster: str,
    snapshot: ResourceSnapshot,
) -> MessageSpec:
    details = [
        ("Результат", row.result or "нет в ответе"),
        (
            "Событие",
            (
                f"#{event.id} «{event.title}»"
                if event is not None
                else unmatched_event_text()
            ),
        ),
        ("Состав и допуски", roster),
    ]
    spec = MessageSpec(
        code="INSIGHT.BATTLE_RESULT",
        title=f"{row.kind}: {row.title}",
        summary="Результат сражения опубликован автоматически.",
        details=tuple(details),
        next_step="Командиру: допишите вывод по результату в этом канале.",
        tone=MessageTone.INFO,
        kind=MessageKind.GENERIC,
    )
    return _apply_source(spec, (snapshot,))


def unavailable_panel_spec(*, code: str, title: str) -> MessageSpec:
    spec = MessageSpec(
        code=code,
        title=title,
        summary="Источник API не настроен. Панель не подставляет нули и пустые места.",
        details=(),
        next_step="Заполните настройки API Majestic и ключ, затем разверните панель.",
        tone=MessageTone.WARNING,
        kind=MessageKind.PUBLIC_PANEL,
        source_label="Majestic API",
        source_attribution="Источник не настроен",
    )
    return spec


async def _load_previous_influence(guild_id: int, period: str) -> int | None:
    raw = await get_guild_setting(guild_id, INFLUENCE_SETTING.format(period=period))
    if raw is None or raw == "":
        return None
    return parse_optional_int(raw)


async def _store_influence(guild_id: int, period: str, influence: int) -> None:
    await set_guild_setting(
        guild_id, INFLUENCE_SETTING.format(period=period), str(influence)
    )


async def _load_posted_ids(guild_id: int) -> list[str] | None:
    raw = await get_guild_setting(guild_id, POSTED_BATTLES_SETTING)
    if raw is None or raw == "":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed]


async def _store_posted_ids(guild_id: int, identities: Sequence[str]) -> None:
    await set_guild_setting(
        guild_id,
        POSTED_BATTLES_SETTING,
        json.dumps(list(identities), ensure_ascii=False),
    )


async def _family_events(guild_id: int) -> list[FamilyEvent]:
    family = await get_active_family(guild_id)
    if family is None:
        return []
    return await list_recent_events(family.id)


def _snapshot_from_db(row: Any) -> ResourceSnapshot | None:
    if row is None:
        return None
    return snapshot_from_row(
        resource=row.resource,
        source_key=row.source_key,
        source_label="Majestic API",
        payload_text=row.payload_text,
        fetched_at=row.fetched_at,
        attribution=row.attribution,
        stale=True,
    )


async def lookup_market_price(guild_id: int, item_name: str) -> PriceHint:
    cleaned = item_name.strip()
    if not await majestic_integration_ready(guild_id):
        return market_price_hint(cleaned)
    for kind in ("items", "vehicles"):
        row = await get_external_snapshot(
            guild_id, SOURCE_MAJESTIC_API, f"marketplace/{kind}"
        )
        snapshot = _snapshot_from_db(row)
        if snapshot is None or snapshot.payload_text is None:
            continue
        amount = find_market_price(snapshot.payload(), cleaned)
        if amount is None:
            continue
        clock = format_fetched_clock(snapshot.fetched_at)
        dated = f"на {clock}" if clock else "по снимку рынка"
        return PriceHint(
            item_name=cleaned,
            amount=amount,
            source=SOURCE_MAJESTIC_API,
            note=(
                f"Рыночная оценка {amount} ({dated}). Это оценка, не факт сделки."
            ),
        )
    return PriceHint(
        item_name=cleaned,
        amount=None,
        source="none",
        note=(
            "Рыночная оценка недоступна: в справочнике рынка нет строки "
            f"«{cleaned}»."
            if cleaned
            else "Рыночная оценка недоступна: источник цен не дал сумму."
        ),
    )


async def _client(
    guild_id: int, client: MajesticApiClient | None
) -> MajesticApiClient | None:
    if client is not None:
        return client
    if not await majestic_integration_ready(guild_id):
        return None
    from services.external import majestic_client

    return majestic_client()


async def rating_panel_spec(
    guild_id: int, *, client: MajesticApiClient | None = None
) -> MessageSpec:
    active = await _client(guild_id, client)
    if active is None:
        return unavailable_panel_spec(code="INSIGHT.RATING", title="Рейтинг Memphis")
    profile = await active_profile(guild_id)
    ranks = await load_ranks(guild_id)
    periods: dict[str, ResourceSnapshot] = {}
    previous: dict[str, int | None] = {}
    for period in RATING_PERIODS:
        snapshot = await active.fetch_org_rating(guild_id, period)
        periods[period] = snapshot
        previous[period] = await _load_previous_influence(guild_id, period)
        if snapshot.payload_text is not None:
            view = extract_org_rating(
                snapshot.payload(), profile.family_name, profile.family_tag
            )
            if view.found and view.influence is not None:
                await _store_influence(guild_id, period, view.influence)
    ordered: dict[str, ResourceSnapshot] = {
        key: periods[key] for key in ("week", "period", "season") if key in periods
    }
    return render_rating_spec(
        family_name=profile.family_name,
        family_tag=profile.family_tag,
        ranks=ranks,
        periods=ordered,
        previous=previous,
    )


async def battles_panel_spec(
    guild_id: int, *, client: MajesticApiClient | None = None
) -> MessageSpec:
    active = await _client(guild_id, client)
    if active is None:
        return unavailable_panel_spec(
            code="INSIGHT.BATTLES", title="Разборы каптов и войн семей"
        )
    captures = await active.fetch_captures(guild_id)
    wars = await active.fetch_family_wars(guild_id)
    events = await _family_events(guild_id)
    return await render_battles_spec(captures=captures, wars=wars, events=events)


async def market_panel_spec(
    guild_id: int, *, client: MajesticApiClient | None = None
) -> MessageSpec:
    active = await _client(guild_id, client)
    if active is None:
        return unavailable_panel_spec(code="INSIGHT.MARKET", title="Рынок Memphis")
    items = await active.fetch_marketplace(guild_id, "items")
    vehicles = await active.fetch_marketplace(guild_id, "vehicles")
    return render_market_spec((items, vehicles))


async def property_panel_spec(
    guild_id: int, *, client: MajesticApiClient | None = None
) -> MessageSpec:
    active = await _client(guild_id, client)
    if active is None:
        return unavailable_panel_spec(code="INSIGHT.PROPERTY", title="ПСН и особняки")
    mansions = await active.fetch_mansions(guild_id)
    psn = await active.fetch_multipurpose(guild_id)
    return render_property_spec((mansions, psn))


async def collect_new_battle_cards(
    guild_id: int, *, client: MajesticApiClient | None = None
) -> list[MessageSpec]:
    active = await _client(guild_id, client)
    if active is None:
        return []
    captures = await active.fetch_captures(guild_id)
    wars = await active.fetch_family_wars(guild_id)
    rows = extract_battles("капт", captures.payload()) + extract_battles(
        "война семей", wars.payload()
    )
    identities = [row.identity for row in rows]
    posted = await _load_posted_ids(guild_id)
    await _store_posted_ids(guild_id, identities)
    if posted is None:
        return []
    known = set(posted)
    events = await _family_events(guild_id)
    cards: list[MessageSpec] = []
    for row in rows:
        if row.identity in known:
            continue
        event = match_event(row, events)
        roster = (
            await roster_gap_report(event)
            if event is not None
            else unmatched_event_text()
        )
        source = captures if row.kind == "капт" else wars
        cards.append(
            render_battle_card(row, event=event, roster=roster, snapshot=source)
        )
    return cards
