"""Registry and diagnostics for persistent guild panels."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from core.permissions import Permission
from core.profile import active_profile, profile_requires

SOURCE_UNAVAILABLE = "источник не настроен"
from repositories.static_messages import (
    StaticMessageRecord,
    list_static_messages,
)
from services.static_messages import (
    STATE_BINDING_LOST,
    STATE_CHANNEL_MOVED,
    STATE_CHANNEL_NOT_FOUND,
    STATE_DEPLOYED,
    STATE_MESSAGE_DELETED,
    STATE_MISSING,
    BindingInspection,
    inspect_static_binding,
)

STATE_UNAVAILABLE = "unavailable"
STATUS_LABELS: dict[str, str] = {
    STATE_DEPLOYED: "развёрнута",
    STATE_MISSING: "отсутствует",
    STATE_MESSAGE_DELETED: "сообщение удалено",
    STATE_CHANNEL_NOT_FOUND: "канал не найден",
    STATE_CHANNEL_MOVED: "перенесена в другой канал",
    STATE_BINDING_LOST: "потеряна привязка",
    STATE_UNAVAILABLE: "недоступна для текущего профиля",
}
_WORST_ORDER: tuple[str, ...] = (
    STATE_UNAVAILABLE,
    STATE_CHANNEL_NOT_FOUND,
    STATE_MESSAGE_DELETED,
    STATE_CHANNEL_MOVED,
    STATE_BINDING_LOST,
    STATE_MISSING,
    STATE_DEPLOYED,
)

DeployFn = Callable[[Any], Awaitable[Any]]


class PanelError(Exception):
    """User-facing panel failure with a stable machine-readable code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PanelDefinition:
    key: str
    title: str
    channel_key: str
    storage_keys: tuple[str, ...]
    view_permission: Permission | None = None
    required_upgrades: tuple[str, ...] = ()
    storage_prefix: str | None = None
    requires_external: bool = False


@dataclass(frozen=True, slots=True)
class PanelStatus:
    key: str
    title: str
    channel_key: str
    channel_id: int | None
    state: str
    revision: int | None
    updated_at: str
    reason: str
    view_permission: str


@dataclass(frozen=True, slots=True)
class PanelChangeResult:
    key: str
    title: str
    state: str
    detail: str
    changed: bool


_DEPLOYERS: dict[str, DeployFn] = {}


def _definitions() -> tuple[PanelDefinition, ...]:
    return (
        PanelDefinition(
            "help",
            "Поддержка семьи",
            "help",
            ("setup-help",),
        ),
        PanelDefinition(
            "request-ss",
            "Обращение к старшему составу",
            "request_ss",
            ("setup-ss",),
        ),
        PanelDefinition(
            "vzp",
            "Заявка в боевую команду",
            "vzp",
            ("setup-vzp",),
        ),
        PanelDefinition(
            "family-invite",
            "Вступление в семью",
            "family_invite",
            ("setup-family-application",),
        ),
        PanelDefinition(
            "add-roles",
            "Роли и доступы",
            "add_roles",
            ("setup-roles",),
        ),
        PanelDefinition(
            "happy-birthday",
            "Дни рождения",
            "happy_birthday",
            ("family-birthdays",),
        ),
        PanelDefinition(
            "add-birthday",
            "Добавить день рождения",
            "add_birthday",
            ("setup-add-birthday",),
        ),
        PanelDefinition(
            "treasury",
            "Казна",
            "treasury",
            ("treasury",),
            view_permission=Permission.TREASURY_VIEW,
        ),
        PanelDefinition(
            "car-park",
            "Автопарк",
            "car_list",
            ("car-park",),
        ),
        PanelDefinition(
            "weekly-stats",
            "Еженедельная статистика",
            "weekly_stats",
            (),
            storage_prefix="weekly-statistics:",
        ),
        PanelDefinition(
            "shop",
            "Магазин",
            "shop",
            ("shop",),
        ),
        PanelDefinition(
            "family-features",
            "Семейные панели",
            "family_features",
            (
                "family-features-birthday",
                "family-features-vzp",
                "family-features-help",
            ),
        ),
        PanelDefinition(
            "capt",
            "Капт",
            "family_features",
            ("capt",),
            required_upgrades=("territory_control",),
        ),
        PanelDefinition(
            "me",
            "Моя панель",
            "family_features",
            ("me",),
        ),
        PanelDefinition(
            "contracts",
            "Контракты",
            "summit_contract",
            ("contracts",),
        ),
        PanelDefinition(
            "insights-rating",
            "Рейтинг Memphis",
            "insights_rating",
            ("insights-rating",),
            requires_external=True,
        ),
        PanelDefinition(
            "insights-battles",
            "Разборы каптов и войн семей",
            "insights_battles",
            ("insights-battles",),
            requires_external=True,
        ),
        PanelDefinition(
            "insights-market",
            "Рынок Memphis",
            "insights_market",
            ("insights-market",),
            requires_external=True,
        ),
        PanelDefinition(
            "insights-property",
            "ПСН и особняки",
            "insights_property",
            ("insights-property",),
            requires_external=True,
        ),
    )


def known_panels() -> tuple[PanelDefinition, ...]:
    return _definitions()


def known_panel_keys() -> tuple[str, ...]:
    return tuple(item.key for item in _definitions())


def panel_definition(key: str) -> PanelDefinition:
    for item in _definitions():
        if item.key == key:
            return item
    raise PanelError(
        (
            f"Панель {key} не найдена в реестре. Список не изменён. "
            "Выберите панель из реестра. Код: PANELS.UNKNOWN."
        ),
        code="PANELS.UNKNOWN",
    )


def register_deployer(key: str, deployer: DeployFn) -> None:
    _DEPLOYERS[key] = deployer


def unregister_deployers() -> None:
    _DEPLOYERS.clear()


def wizard_panel_specs() -> tuple[tuple[str, str, str], ...]:
    return tuple((item.key, item.title, item.channel_key) for item in _definitions())


async def unavailable_reason(guild_id: int, key: str) -> str:
    definition = panel_definition(key)
    if definition.requires_external:
        from services.external.majestic_api import majestic_integration_ready

        if not await majestic_integration_ready(guild_id):
            return SOURCE_UNAVAILABLE
    if not definition.required_upgrades:
        return ""
    profile = await active_profile(guild_id)
    missing = [
        upgrade
        for upgrade in definition.required_upgrades
        if not profile_requires(profile, upgrade)
    ]
    if not missing:
        return ""
    from services.provisioning import upgrade_label

    labels = ", ".join(upgrade_label(item) for item in missing)
    return f"не открыто улучшение «{labels}»"


def _storage_records(
    definition: PanelDefinition,
    rows: Sequence[StaticMessageRecord],
) -> tuple[StaticMessageRecord, ...]:
    if definition.storage_prefix:
        matched = tuple(
            row
            for row in rows
            if row.panel_key.startswith(definition.storage_prefix)
        )
        if not matched:
            return ()
        return (max(matched, key=lambda item: item.updated_at or item.panel_key),)
    wanted = set(definition.storage_keys)
    return tuple(row for row in rows if row.panel_key in wanted)


def _worst(inspections: Sequence[BindingInspection]) -> BindingInspection:
    ranked = {state: index for index, state in enumerate(_WORST_ORDER)}
    return min(inspections, key=lambda item: ranked.get(item.state, len(ranked)))


async def _inspect_definition(
    guild_id: int,
    definition: PanelDefinition,
    *,
    expected_channel_id: int | None,
    rows: Sequence[StaticMessageRecord],
    fetch_message: Callable[[int, int], Awaitable[object]] | None,
) -> BindingInspection:
    stored = _storage_records(definition, rows)
    keys = definition.storage_keys or (
        (stored[0].panel_key,) if stored else (definition.key,)
    )
    inspections: list[BindingInspection] = []
    by_key = {row.panel_key: row for row in stored}
    for storage_key in keys:
        inspections.append(
            await inspect_static_binding(
                guild_id=guild_id,
                panel_key=storage_key,
                expected_channel_id=expected_channel_id,
                fetch_message=fetch_message,
                record=by_key.get(storage_key),
            )
        )
    return _worst(inspections)


async def list_panels(
    guild_id: int,
    *,
    resolve_channel: Callable[[str], Any] | None = None,
    fetch_message: Callable[[int, int], Awaitable[object]] | None = None,
) -> tuple[PanelStatus, ...]:
    rows = await list_static_messages(guild_id)
    items: list[PanelStatus] = []
    for definition in _definitions():
        reason = await unavailable_reason(guild_id, definition.key)
        channel = resolve_channel(definition.channel_key) if resolve_channel else None
        channel_id = int(getattr(channel, "id", 0) or 0) or None
        if reason:
            items.append(
                PanelStatus(
                    key=definition.key,
                    title=definition.title,
                    channel_key=definition.channel_key,
                    channel_id=channel_id,
                    state=STATE_UNAVAILABLE,
                    revision=None,
                    updated_at="",
                    reason=reason,
                    view_permission=(
                        definition.view_permission.value
                        if definition.view_permission
                        else "все"
                    ),
                )
            )
            continue
        inspection = await _inspect_definition(
            guild_id,
            definition,
            expected_channel_id=channel_id,
            rows=rows,
            fetch_message=fetch_message,
        )
        items.append(
            PanelStatus(
                key=definition.key,
                title=definition.title,
                channel_key=definition.channel_key,
                channel_id=inspection.channel_id or channel_id,
                state=inspection.state,
                revision=inspection.revision,
                updated_at=inspection.updated_at,
                reason=inspection.detail,
                view_permission=(
                    definition.view_permission.value
                    if definition.view_permission
                    else "все"
                ),
            )
        )
    return tuple(items)


def format_panel_lines(items: tuple[PanelStatus, ...]) -> list[str]:
    lines: list[str] = []
    for item in items:
        if item.state == STATE_UNAVAILABLE and item.reason == SOURCE_UNAVAILABLE:
            state = f"недоступна: {SOURCE_UNAVAILABLE}"
            extra = ""
        else:
            state = STATUS_LABELS.get(item.state, item.state)
            extra = (
                f" — {item.reason}" if item.reason and item.state != STATE_DEPLOYED else ""
            )
        revision = f"rev {item.revision}" if item.revision is not None else "rev —"
        updated = item.updated_at[:19].replace("T", " ") if item.updated_at else "—"
        lines.append(
            f"`{item.key}` {item.title}\n"
            f"{state} · {revision} · {updated}{extra}"
        )
    return lines


async def deploy_panel(
    guild: Any,
    key: str,
    *,
    deployer: DeployFn | None = None,
) -> PanelChangeResult:
    definition = panel_definition(key)
    guild_id = int(getattr(guild, "id", 0))
    reason = await unavailable_reason(guild_id, key)
    if reason:
        if reason == SOURCE_UNAVAILABLE:
            raise PanelError(
                (
                    f"Панель «{definition.title}» недоступна: {SOURCE_UNAVAILABLE}. "
                    "Развёртывание не выполнено. Заполните настройки API Majestic "
                    "и ключ в .env, затем повторите. Код: PANELS.SOURCE_REQUIRED."
                ),
                code="PANELS.SOURCE_REQUIRED",
            )
        raise PanelError(
            (
                f"Панель «{definition.title}» недоступна для текущего профиля: "
                f"{reason}. Развёртывание не выполнено. Откройте улучшение "
                "в профиле семьи и повторите. Код: PANELS.UPGRADE_REQUIRED."
            ),
            code="PANELS.UPGRADE_REQUIRED",
        )
    active = deployer or _DEPLOYERS.get(key)
    if active is None:
        raise PanelError(
            (
                f"Панель {key} не зарегистрирована для развёртывания. "
                "Ничего не изменено. Код: PANELS.NO_DEPLOYER."
            ),
            code="PANELS.NO_DEPLOYER",
        )
    await active(guild)
    return PanelChangeResult(
        key=definition.key,
        title=definition.title,
        state=STATE_DEPLOYED,
        detail="панель развёрнута или пересобрана",
        changed=True,
    )


async def repair_panels(
    guild: Any,
    *,
    resolve_channel: Callable[[str], Any] | None = None,
    fetch_message: Callable[[int, int], Awaitable[object]] | None = None,
    deployer: DeployFn | None = None,
) -> tuple[PanelChangeResult, ...]:
    statuses = await list_panels(
        int(getattr(guild, "id", 0)),
        resolve_channel=resolve_channel,
        fetch_message=fetch_message,
    )
    results: list[PanelChangeResult] = []
    for item in statuses:
        if item.state in {STATE_DEPLOYED, STATE_UNAVAILABLE}:
            results.append(
                PanelChangeResult(
                    key=item.key,
                    title=item.title,
                    state=item.state,
                    detail=item.reason or "исправление не требуется",
                    changed=False,
                )
            )
            continue
        try:
            await deploy_panel(guild, item.key, deployer=deployer)
        except PanelError as exc:
            results.append(
                PanelChangeResult(
                    key=item.key,
                    title=item.title,
                    state=item.state,
                    detail=str(exc),
                    changed=False,
                )
            )
            continue
        results.append(
            PanelChangeResult(
                key=item.key,
                title=item.title,
                state=STATE_DEPLOYED,
                detail=item.reason or "привязка восстановлена",
                changed=True,
            )
        )
    return tuple(results)


def autocomplete_panel_keys(query: str) -> list[str]:
    needle = query.strip().casefold()
    keys: list[str] = []
    for item in _definitions():
        if needle and needle not in item.key.casefold() and needle not in item.title.casefold():
            continue
        keys.append(item.key)
        if len(keys) >= 25:
            break
    return keys
