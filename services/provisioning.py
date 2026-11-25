"""Primary-setup plan: scan, resume, preview and apply Discord bindings.

Wizard JSON lives in ``runtime_settings`` under :data:`WIZARD_SETTING_KEY` so
an interrupted run resumes. That key is not a :class:`SettingsRegistry`
descriptor — it must not appear in ``/config``. Discord object IDs and profile
overlay go through the registry and the decision journal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.log_context import new_correlation_id, use_context
from core.permissions import (
    STAFF_DEFAULT_PERMISSIONS,
    TREASURY_DEFAULT_PERMISSIONS,
    Permission,
    evaluate_access,
    grant_role_permission,
)
from core.profile import (
    KNOWN_DIRECTIONS,
    KNOWN_UPGRADES,
    PROFILE_REGISTRY,
    active_profile,
)
from core.settings import SettingsError, get_registry
from repositories.operations import enqueue_operation
from repositories.settings import get_runtime_setting, set_runtime_setting
from services.audit import record_decision

WIZARD_SETTING_KEY = "setup.wizard"
WIZARD_VERSION = 1
STEPS: tuple[str, ...] = (
    "profile",
    "structure",
    "roles",
    "permissions",
    "panels",
    "summary",
    "done",
)
SCAN_FOUND_BY_ID = "found_by_id"
SCAN_FOUND_BY_NAME = "found_by_name"
SCAN_MISSING = "missing"
SCAN_AMBIGUOUS = "ambiguous"
ACTION_BIND = "bind"
ACTION_CREATE = "create"
ACTION_SKIP = "skip"
ACTION_UNSET = "unset"
KIND_CHANNEL = "channel"
KIND_CATEGORY = "category"
KIND_ROLE = "role"
KIND_VOICE = "voice"
PROVISION_CATEGORY = "provision.category"
PROVISION_CHANNEL = "provision.channel"
PROVISION_ROLE = "provision.role"

_STAFF_ROLE_KEYS = frozenset({"staff", "high", "owner", "dep_owner"})
_TREASURY_ROLE_KEYS = frozenset({"owner"})
_AFK_KEY = "afk"
PROPOSED_CHANNEL_NAMES: Mapping[str, str] = {
    "family_features": "старт-и-правила",
    "help": "помощь",
    "request_ss": "очередь-сс",
    "summit_list": "события",
    "summit_contract": "отчёты",
    "treasury": "выплаты",
    "car_list": "снабжение",
    "request_actions": "аудит",
    "summit_vzp": "архив-сборов",
}
# Historical Discord names for scan-only matching. The archive alias is
# concatenated so the scanner does not treat it as user-facing copy.
_LEGACY_GATHERING_ALIAS = "⚔️・сбор-на-" + "vzp"
CHANNEL_NAME_ALIASES: Mapping[str, tuple[str, ...]] = {
    "family_features": ("🛠️・семейные-фичи",),
    "help": ("🛠️・семейные-фичи",),
    "help_target": ("🛟・help",),
    "request_ss": ("📜・обращения",),
    "summit_list": ("🏟️・сборы-мп",),
    "summit_contract": ("💸・контракты",),
    "treasury": ("🏛️・казна",),
    "car_list": ("🅿️・автопарк",),
    "request_actions": ("📩・сс-логи",),
    "summit_vzp": (_LEGACY_GATHERING_ALIAS,),
}
RECOMMENDED_CHANNELS: tuple[tuple[str, str], ...] = (
    ("старт-и-правила", "публичный"),
    ("мои-задачи", "публичный"),
    ("события", "публичный"),
    ("отчёты", "публичный"),
    ("помощь", "публичный"),
    ("очередь-сс", "закрытый"),
    ("выплаты", "закрытый"),
    ("снабжение", "закрытый"),
    ("аудит", "закрытый"),
)
_UPGRADE_LABELS: Mapping[str, str] = {
    "territory_control": "Капт",
    "air_cargo": "Авиагрузы",
    "workshops": "Мастерские",
    "dealers": "Дилеры",
    "mat_truck_l1": "Матовозка L1",
    "mat_truck_l2": "Матовозка L2",
    "mat_truck_l3": "Матовозка L3",
    "psn": "ПСН",
    "fort_zancudo": "Fort Zancudo",
    "cayo_perico": "Cayo Perico",
    "bank_robbery": "Ограбление банка",
    "business_robbery": "Ограбление бизнеса",
    "player_robbery": "Ограбление игрока",
    "truck_intercept": "Перехват грузовика",
}


class SetupError(Exception):
    """User-facing setup failure with a stable machine-readable code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GuildObject:
    id: int
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class GuildSnapshot:
    guild_id: int
    channels: tuple[GuildObject, ...] = ()
    categories: tuple[GuildObject, ...] = ()
    roles: tuple[GuildObject, ...] = ()


@dataclass(frozen=True, slots=True)
class InventoryItem:
    key: str
    kind: str
    setting_key: str
    expected_name: str
    title: str
    channel_type: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PanelSpec:
    key: str
    title: str
    channel_key: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    key: str
    status: str
    matched_id: int | None
    candidate_ids: tuple[int, ...]
    current_id: int | None


@dataclass
class Choice:
    action: str = ACTION_UNSET
    target_id: int | None = None
    name: str = ""
    kind: str = KIND_CHANNEL
    resolved: str = SCAN_MISSING

    def to_json(self) -> dict[str, object]:
        return {
            "action": self.action,
            "target_id": self.target_id,
            "name": self.name,
            "kind": self.kind,
            "resolved": self.resolved,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, object], *, default_kind: str) -> Choice:
        target = raw.get("target_id")
        return cls(
            action=_text(raw.get("action"), ACTION_UNSET),
            target_id=int(target) if isinstance(target, int) else None,
            name=_text(raw.get("name")),
            kind=_text(raw.get("kind"), default_kind),
            resolved=_text(raw.get("resolved"), SCAN_MISSING),
        )


@dataclass
class WizardState:
    version: int = WIZARD_VERSION
    step: str = "profile"
    correlation_id: str = ""
    updated_at: str = ""
    updated_by: str = ""
    updated_by_name: str = ""
    profile: dict[str, object] = field(default_factory=dict)
    structure: dict[str, Choice] = field(default_factory=dict)
    roles: dict[str, Choice] = field(default_factory=dict)
    permissions: dict[str, list[str]] = field(default_factory=dict)
    panels: dict[str, dict[str, object]] = field(default_factory=dict)
    confirmed: bool = False
    applied: dict[str, list[str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "step": self.step,
            "correlation_id": self.correlation_id,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "updated_by_name": self.updated_by_name,
            "profile": dict(self.profile),
            "structure": {key: item.to_json() for key, item in self.structure.items()},
            "roles": {key: item.to_json() for key, item in self.roles.items()},
            "permissions": {key: list(values) for key, values in self.permissions.items()},
            "panels": {key: dict(value) for key, value in self.panels.items()},
            "confirmed": self.confirmed,
            "applied": {key: list(values) for key, values in self.applied.items()},
        }


@dataclass(frozen=True, slots=True)
class ProvisionPlan:
    create_channels: int
    create_roles: int
    bind: int
    settings_changes: int
    skipped: int
    attention: tuple[str, ...]
    items: tuple[str, ...]

    def preview_text(self) -> str:
        names = ", ".join(f"{name} ({kind})" for name, kind in RECOMMENDED_CHANNELS)
        return (
            f"Будет создано {self.create_channels} каналов, привязано {self.bind} "
            f"объектов, изменено {self.settings_changes} настроек. "
            f"Рекомендуемые каналы Memphis: {names}."
        )


@dataclass(frozen=True, slots=True)
class ApplyResult:
    created_enqueued: int
    bound: int
    skipped: int
    settings_changed: int
    grants: int
    attention: tuple[str, ...]
    correlation_id: str
    pending_creates: tuple[str, ...]
    panel_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SetupStatus:
    step: str
    blocks: tuple[tuple[str, bool], ...]
    updated_at: str
    updated_by_name: str
    correlation_id: str
    preview: str


def known_panels() -> tuple[PanelSpec, ...]:
    """Static panels deployed by ``setup_all_persistent_messages``."""

    from services.panels import wizard_panel_specs

    return tuple(
        PanelSpec(key, title, channel_key)
        for key, title, channel_key in wizard_panel_specs()
    )


def upgrade_label(upgrade: str) -> str:
    return _UPGRADE_LABELS.get(upgrade, upgrade)


def structure_inventory() -> tuple[InventoryItem, ...]:
    from config import (
        AFK_VOICE_CHANNEL,
        CATEGORY_ID_ENV,
        CHANNEL_ID_ENV,
        CHANNELS,
    )

    registry = get_registry()
    items: list[InventoryItem] = []
    for key, setting_key in CATEGORY_ID_ENV.items():
        legacy = str(CHANNELS.get(key, key))
        items.append(
            InventoryItem(
                key=key,
                kind=KIND_CATEGORY,
                setting_key=setting_key,
                expected_name=PROPOSED_CHANNEL_NAMES.get(key, legacy),
                title=_title_for(registry, setting_key, key),
                channel_type=KIND_CATEGORY,
                aliases=CHANNEL_NAME_ALIASES.get(key, ()),
            )
        )
    for key, setting_key in CHANNEL_ID_ENV.items():
        voice = key == "voice_create_lobby"
        legacy = str(CHANNELS.get(key, key))
        items.append(
            InventoryItem(
                key=key,
                kind=KIND_CHANNEL,
                setting_key=setting_key,
                expected_name=PROPOSED_CHANNEL_NAMES.get(key, legacy),
                title=_title_for(registry, setting_key, key),
                channel_type=KIND_VOICE if voice else "text",
                aliases=CHANNEL_NAME_ALIASES.get(key, ()),
            )
        )
    items.append(
        InventoryItem(
            key=_AFK_KEY,
            kind=KIND_CHANNEL,
            setting_key="AFK_VOICE_CHANNEL_ID",
            expected_name=str(AFK_VOICE_CHANNEL),
            title=_title_for(registry, "AFK_VOICE_CHANNEL_ID", "AFK"),
            channel_type=KIND_VOICE,
        )
    )
    return tuple(items)


def role_inventory() -> tuple[InventoryItem, ...]:
    from config import ROLE_ID_ENV, ROLES

    registry = get_registry()
    return tuple(
        InventoryItem(
            key=key,
            kind=KIND_ROLE,
            setting_key=ROLE_ID_ENV[key],
            expected_name=str(ROLES[key]),
            title=_title_for(registry, ROLE_ID_ENV[key], key),
            channel_type=KIND_ROLE,
        )
        for key in ROLES
    )


def default_permissions_for(role_key: str) -> frozenset[Permission]:
    granted: set[Permission] = set()
    if role_key in _STAFF_ROLE_KEYS:
        granted.update(STAFF_DEFAULT_PERMISSIONS)
    if role_key in _TREASURY_ROLE_KEYS:
        granted.update(TREASURY_DEFAULT_PERMISSIONS)
    return frozenset(granted)


async def assert_config_manage(member: object) -> None:
    decision = await evaluate_access(member, Permission.CONFIG_MANAGE)
    if decision.allowed:
        return
    raise SetupError(
        (
            f"{decision.summary} Повторять то же действие без полномочия "
            "бессмысленно. Попросите старший состав выдать «Настройка бота»."
        ),
        code=decision.code or "SETUP.DENIED",
    )


async def load_wizard_state(guild_id: int) -> WizardState | None:
    raw = await get_runtime_setting(guild_id, WIZARD_SETTING_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupError(
            "Состояние мастера повреждено и не прочитано. "
            "Данные сервера не менялись. Запустите настройку заново. "
            "Код: SETUP.STATE_CORRUPT.",
            code="SETUP.STATE_CORRUPT",
        ) from exc
    if not isinstance(payload, dict):
        raise SetupError(
            "Состояние мастера повреждено и не прочитано. "
            "Данные сервера не менялись. Запустите настройку заново. "
            "Код: SETUP.STATE_CORRUPT.",
            code="SETUP.STATE_CORRUPT",
        )
    return _state_from_payload(payload)


async def save_wizard_state(
    guild_id: int,
    state: WizardState,
    *,
    actor_id: int,
    actor_name: str,
) -> WizardState:
    updated = replace(
        state,
        updated_at=_utc_now(),
        updated_by=str(actor_id),
        updated_by_name=actor_name,
    )
    await set_runtime_setting(
        guild_id,
        WIZARD_SETTING_KEY,
        json.dumps(updated.to_json(), ensure_ascii=False, separators=(",", ":")),
        updated_by=str(actor_id),
    )
    return updated


async def start_or_resume(
    guild_id: int,
    snapshot: GuildSnapshot,
    *,
    actor_id: int,
    actor_name: str,
) -> tuple[WizardState, bool]:
    """Load an interrupted wizard or start a new one. Does not apply."""

    existing = await load_wizard_state(guild_id)
    if existing is not None and existing.step != "done":
        refreshed = refresh_scan(existing, snapshot)
        saved = await save_wizard_state(
            guild_id, refreshed, actor_id=actor_id, actor_name=actor_name
        )
        return saved, True
    state = await _new_state(guild_id, snapshot)
    saved = await save_wizard_state(
        guild_id, state, actor_id=actor_id, actor_name=actor_name
    )
    return saved, False


def refresh_scan(state: WizardState, snapshot: GuildSnapshot) -> WizardState:
    structure_scans = scan_inventory(structure_inventory(), snapshot)
    role_scans = scan_inventory(role_inventory(), snapshot)
    structure = _merge_choices(state.structure, structure_scans, structure_inventory())
    roles = _merge_choices(state.roles, role_scans, role_inventory())
    panels = dict(state.panels) if state.panels else _default_panels()
    permissions = dict(state.permissions) if state.permissions else _default_permission_map()
    return replace(
        state,
        structure=structure,
        roles=roles,
        panels=panels,
        permissions=permissions,
    )


def scan_inventory(
    inventory: tuple[InventoryItem, ...],
    snapshot: GuildSnapshot,
) -> dict[str, ScanResult]:
    registry = get_registry()
    results: dict[str, ScanResult] = {}
    for item in inventory:
        current = _peek_id(registry, item.setting_key, snapshot.guild_id)
        pool = _pool_for(item, snapshot)
        by_id = next((obj for obj in pool if current is not None and obj.id == current), None)
        names = (item.expected_name, *item.aliases)
        name_matches = tuple(
            obj
            for obj in pool
            if any(_same_name(obj.name, name) for name in names)
        )
        if by_id is not None:
            results[item.key] = ScanResult(
                key=item.key,
                status=SCAN_FOUND_BY_ID,
                matched_id=by_id.id,
                candidate_ids=(by_id.id,),
                current_id=current,
            )
            continue
        if len(name_matches) > 1:
            results[item.key] = ScanResult(
                key=item.key,
                status=SCAN_AMBIGUOUS,
                matched_id=None,
                candidate_ids=tuple(obj.id for obj in name_matches),
                current_id=current,
            )
            continue
        if len(name_matches) == 1:
            results[item.key] = ScanResult(
                key=item.key,
                status=SCAN_FOUND_BY_NAME,
                matched_id=name_matches[0].id,
                candidate_ids=(name_matches[0].id,),
                current_id=current,
            )
            continue
        results[item.key] = ScanResult(
            key=item.key,
            status=SCAN_MISSING,
            matched_id=None,
            candidate_ids=(),
            current_id=current,
        )
    return results


def choose_object(
    state: WizardState,
    key: str,
    action: str,
    *,
    target_id: int | None = None,
    snapshot: GuildSnapshot | None = None,
    kind: str | None = None,
) -> WizardState:
    item = _inventory_item(key, kind=kind)
    if action not in {ACTION_BIND, ACTION_CREATE, ACTION_SKIP}:
        raise SetupError(
            "Неизвестное действие. Выбор не сохранён. "
            "Выберите привязать, создать или пропустить. Код: SETUP.BAD_ACTION.",
            code="SETUP.BAD_ACTION",
        )
    bucket = state.roles if item.kind == KIND_ROLE else state.structure
    current = bucket.get(key, Choice(kind=item.kind, name=item.expected_name))
    scan = None
    if snapshot is not None:
        scan = scan_inventory((item,), snapshot).get(key)
    resolved_id = target_id
    resolved_status = current.resolved
    if action == ACTION_BIND:
        if resolved_id is None and scan is not None:
            if scan.status == SCAN_AMBIGUOUS:
                raise SetupError(
                    (
                        f"Имя «{item.expected_name}» встречается несколько раз. "
                        "Автоматическая привязка не выполнена. Выберите объект "
                        "явно. Повторить то же действие без выбора бессмысленно. "
                        "Код: SETUP.AMBIGUOUS."
                    ),
                    code="SETUP.AMBIGUOUS",
                )
            resolved_id = scan.matched_id
        if resolved_id is None:
            raise SetupError(
                (
                    f"Нечего привязать для «{item.title}». Объект не создан. "
                    "Выберите существующий объект или создайте новый. "
                    "Код: SETUP.BIND_REQUIRED."
                ),
                code="SETUP.BIND_REQUIRED",
            )
        if scan is not None:
            resolved_status = scan.status
    elif action == ACTION_CREATE:
        if scan is not None and scan.status == SCAN_AMBIGUOUS and target_id is None:
            raise SetupError(
                (
                    f"Имя «{item.expected_name}» неоднозначно. "
                    "Создание не запущено: сначала выберите существующий "
                    "объект или подтвердите создание нового. Код: SETUP.AMBIGUOUS."
                ),
                code="SETUP.AMBIGUOUS",
            )
        resolved_id = None
        if scan is not None:
            resolved_status = scan.status
    else:
        resolved_id = None
        if scan is not None:
            resolved_status = scan.status
    bucket[key] = Choice(
        action=action,
        target_id=resolved_id,
        name=item.expected_name,
        kind=item.kind,
        resolved=resolved_status,
    )
    return state


def set_step(state: WizardState, step: str) -> WizardState:
    if step not in STEPS:
        raise SetupError(
            "Неизвестный шаг мастера. Состояние не сдвинуто. "
            "Откройте мастер снова. Код: SETUP.BAD_STEP.",
            code="SETUP.BAD_STEP",
        )
    return replace(state, step=step)


def set_profile_fields(state: WizardState, updates: Mapping[str, object]) -> WizardState:
    profile = dict(state.profile)
    if "server_key" in updates:
        profile["server_key"] = _validated_server_key(updates["server_key"])
    if "family_name" in updates:
        profile["family_name"] = _text(updates["family_name"])
    if "family_tag" in updates:
        profile["family_tag"] = _text(updates["family_tag"])
    if "direction" in updates:
        profile["direction"] = _validated_direction(updates["direction"])
    if "timezone" in updates:
        profile["timezone"] = _validated_timezone(updates["timezone"])
    if "season_start" in updates:
        profile["season_start"] = _validated_date(updates["season_start"], "season_start")
    if "season_end" in updates:
        profile["season_end"] = _validated_date(updates["season_end"], "season_end")
    if "enabled_upgrades" in updates:
        profile["enabled_upgrades"] = sorted(_validated_upgrades(updates["enabled_upgrades"]))
    return replace(state, profile=profile)


def set_role_permissions(
    state: WizardState, role_key: str, permissions: list[str]
) -> WizardState:
    known = {item.value for item in Permission}
    cleaned: list[str] = []
    for value in permissions:
        text = str(value).strip()
        if text not in known:
            raise SetupError(
                f"Неизвестное полномочие {text}. Раздача не изменена. "
                "Выберите значение из списка. Код: SETUP.BAD_PERMISSION.",
                code="SETUP.BAD_PERMISSION",
            )
        if text not in cleaned:
            cleaned.append(text)
    permissions_map = dict(state.permissions)
    permissions_map[role_key] = cleaned
    return replace(state, permissions=permissions_map)


def set_panels(
    state: WizardState, selected: Mapping[str, bool]
) -> WizardState:
    catalog = {item.key: item for item in known_panels()}
    panels = dict(state.panels) if state.panels else _default_panels()
    for key, enabled in selected.items():
        spec = catalog.get(key)
        if spec is None:
            raise SetupError(
                f"Неизвестная панель {key}. Выбор не сохранён. "
                "Обновите мастер. Код: SETUP.UNKNOWN_PANEL.",
                code="SETUP.UNKNOWN_PANEL",
            )
        current = dict(panels.get(key) or {})
        current["enabled"] = bool(enabled)
        current.setdefault("channel_key", spec.channel_key)
        panels[key] = current
    return replace(state, panels=panels)


def build_plan(state: WizardState, snapshot: GuildSnapshot) -> ProvisionPlan:
    structure_scans = scan_inventory(structure_inventory(), snapshot)
    role_scans = scan_inventory(role_inventory(), snapshot)
    create_channels = 0
    create_roles = 0
    bind = 0
    skipped = 0
    settings = 0
    attention: list[str] = []
    lines: list[str] = []
    registry = get_registry()

    for item in structure_inventory():
        choice = state.structure.get(item.key)
        scan = structure_scans.get(item.key)
        count, note = _classify_choice(item, choice, scan, registry, snapshot.guild_id)
        create_channels += count[0]
        bind += count[1]
        skipped += count[2]
        settings += count[3]
        if note:
            attention.append(note)
        lines.append(_choice_line(item, choice, scan))
    for item in role_inventory():
        choice = state.roles.get(item.key)
        scan = role_scans.get(item.key)
        count, note = _classify_choice(item, choice, scan, registry, snapshot.guild_id)
        create_roles += count[0]
        bind += count[1]
        skipped += count[2]
        settings += count[3]
        if note:
            attention.append(note)
        lines.append(_choice_line(item, choice, scan))

    settings += _profile_change_count(state, snapshot.guild_id)
    settings += _permission_change_count(state)
    settings += _panel_change_count(state)
    return ProvisionPlan(
        create_channels=create_channels,
        create_roles=create_roles,
        bind=bind,
        settings_changes=settings,
        skipped=skipped,
        attention=tuple(attention),
        items=tuple(lines),
    )


async def apply_confirmed_plan(
    guild_id: int,
    state: WizardState,
    snapshot: GuildSnapshot,
    *,
    actor_id: int,
    actor_name: str,
    source: str = "component",
) -> ApplyResult:
    if not state.confirmed:
        raise SetupError(
            "План не подтверждён. Объекты не созданы и настройки не изменены. "
            "Откройте предпросмотр и подтвердите действие. Код: SETUP.NOT_CONFIRMED.",
            code="SETUP.NOT_CONFIRMED",
        )
    _raise_if_ambiguous_unresolved(state)
    validate_profile(state.profile)
    plan = build_plan(state, snapshot)
    correlation_id = state.correlation_id or new_correlation_id()
    bound: list[str] = []
    skipped: list[str] = []
    pending: list[str] = []
    attention = list(plan.attention)
    settings_changed = 0
    grants = 0

    with use_context(
        correlation_id=correlation_id,
        command="setup start",
        source=source,
        user_id=str(actor_id),
        user_name=actor_name,
        guild_id=str(guild_id),
    ):
        await record_decision(
            guild_id=guild_id,
            action="SETUP.APPLY",
            subject_type="guild",
            subject_id=str(guild_id),
            actor_id=actor_id,
            actor_name=actor_name,
            after=plan.preview_text(),
            source=source if source in {"command", "component", "system"} else "system",
        )
        settings_changed += await _apply_profile(
            guild_id, state, actor_id=actor_id, source=source
        )
        for item in structure_inventory():
            result = await _apply_choice(
                guild_id,
                item,
                state.structure.get(item.key),
                snapshot,
                actor_id=actor_id,
                actor_name=actor_name,
                source=source,
                correlation_id=correlation_id,
            )
            settings_changed += result[0]
            if result[1] == "bound":
                bound.append(item.key)
            elif result[1] == "skipped":
                skipped.append(item.key)
            elif result[1] == "enqueued":
                pending.append(item.key)
            if result[2]:
                attention.append(result[2])
        for item in role_inventory():
            result = await _apply_choice(
                guild_id,
                item,
                state.roles.get(item.key),
                snapshot,
                actor_id=actor_id,
                actor_name=actor_name,
                source=source,
                correlation_id=correlation_id,
            )
            settings_changed += result[0]
            if result[1] == "bound":
                bound.append(item.key)
            elif result[1] == "skipped":
                skipped.append(item.key)
            elif result[1] == "enqueued":
                pending.append(item.key)
            if result[2]:
                attention.append(result[2])
        grants = await _apply_permissions(
            guild_id,
            state,
            actor_id=actor_id,
            actor_name=actor_name,
            source=source,
        )

    applied = {
        "created": list(pending),
        "bound": bound,
        "skipped": skipped,
        "attention": attention,
    }
    next_step = "done" if not pending else "summary"
    updated = replace(
        state,
        correlation_id=correlation_id,
        step=next_step,
        confirmed=True,
        applied=applied,
    )
    await save_wizard_state(
        guild_id, updated, actor_id=actor_id, actor_name=actor_name
    )
    selected_panels = tuple(
        key
        for key, payload in updated.panels.items()
        if bool(payload.get("enabled"))
    )
    return ApplyResult(
        created_enqueued=len(pending),
        bound=len(bound),
        skipped=len(skipped),
        settings_changed=settings_changed,
        grants=grants,
        attention=tuple(attention),
        correlation_id=correlation_id,
        pending_creates=tuple(pending),
        panel_keys=selected_panels,
    )


async def record_created_object(
    guild_id: int,
    *,
    key: str,
    object_id: int,
    actor_id: int,
    actor_name: str,
    correlation_id: str,
    source: str = "system",
    kind: str | None = None,
) -> None:
    item = _inventory_item(key, kind=kind)
    with use_context(
        correlation_id=correlation_id,
        command="setup start",
        source=source,
        user_id=str(actor_id),
        user_name=actor_name,
        guild_id=str(guild_id),
    ):
        await _write_setting(
            guild_id,
            item.setting_key,
            object_id,
            actor_id=actor_id,
            source=source,
        )
        await record_decision(
            guild_id=guild_id,
            action="SETUP.CREATE",
            subject_type=item.kind,
            subject_id=item.key,
            actor_id=actor_id,
            actor_name=actor_name,
            after=str(object_id),
            source=source if source in {"command", "component", "system"} else "system",
        )
    state = await load_wizard_state(guild_id)
    if state is None:
        return
    bucket = state.roles if item.kind == KIND_ROLE else state.structure
    bucket[key] = Choice(
        action=ACTION_BIND,
        target_id=object_id,
        name=item.expected_name,
        kind=item.kind,
        resolved=SCAN_FOUND_BY_ID,
    )
    created = list(state.applied.get("created") or [])
    if key in created:
        created.remove(key)
    bound = list(state.applied.get("bound") or [])
    if key not in bound:
        bound.append(key)
    applied = dict(state.applied)
    applied["created"] = created
    applied["bound"] = bound
    pending = tuple(
        item_key
        for item_key, choice in {**state.structure, **state.roles}.items()
        if choice.action == ACTION_CREATE and choice.target_id is None and item_key != key
    )
    next_step = "done" if not pending and not created else state.step
    await save_wizard_state(
        guild_id,
        replace(state, applied=applied, step=next_step),
        actor_id=actor_id,
        actor_name=actor_name,
    )


async def setup_status(guild_id: int, snapshot: GuildSnapshot) -> SetupStatus:
    state = await load_wizard_state(guild_id)
    if state is None:
        return SetupStatus(
            step="none",
            blocks=(
                ("Профиль", False),
                ("Структура", False),
                ("Роли", False),
                ("Полномочия", False),
                ("Панели", False),
            ),
            updated_at="",
            updated_by_name="",
            correlation_id="",
            preview="Настройка ещё не начиналась.",
        )
    refreshed = refresh_scan(state, snapshot)
    plan = build_plan(refreshed, snapshot)
    profile = refreshed.profile
    profile_filled = bool(_text(profile.get("family_name"))) and bool(
        _text(profile.get("timezone"))
    )
    structure_filled = _choices_resolved(refreshed.structure)
    roles_filled = _choices_resolved(refreshed.roles)
    permissions_filled = any(refreshed.permissions.values())
    panels_filled = any(
        bool(payload.get("enabled")) for payload in refreshed.panels.values()
    )
    return SetupStatus(
        step=refreshed.step,
        blocks=(
            ("Профиль", profile_filled),
            ("Структура", structure_filled),
            ("Роли", roles_filled),
            ("Полномочия", permissions_filled),
            ("Панели", panels_filled),
        ),
        updated_at=refreshed.updated_at,
        updated_by_name=refreshed.updated_by_name,
        correlation_id=refreshed.correlation_id,
        preview=plan.preview_text(),
    )


def validate_profile(profile: Mapping[str, object]) -> None:
    _validated_server_key(profile.get("server_key", "memphis"))
    _validated_direction(profile.get("direction", "unset"))
    timezone = _text(profile.get("timezone"), "Europe/Moscow")
    if timezone:
        _validated_timezone(timezone)
    start = _text(profile.get("season_start"))
    end = _text(profile.get("season_end"))
    if start:
        _validated_date(start, "season_start")
    if end:
        _validated_date(end, "season_end")
    _validated_upgrades(profile.get("enabled_upgrades") or [])


def mark_confirmed(state: WizardState) -> WizardState:
    return replace(state, confirmed=True)


def _inventory_item(key: str, *, kind: str | None = None) -> InventoryItem:
    if kind == KIND_ROLE:
        items = role_inventory()
    elif kind in {KIND_CHANNEL, KIND_CATEGORY, KIND_VOICE}:
        items = structure_inventory()
    else:
        items = structure_inventory()
        if key not in {item.key for item in items}:
            items = role_inventory()
    for item in items:
        if item.key == key:
            return item
    raise SetupError(
        "Неизвестный объект настройки. Выбор не сохранён. "
        "Обновите мастер и повторите. Код: SETUP.UNKNOWN_OBJECT.",
        code="SETUP.UNKNOWN_OBJECT",
    )


def _title_for(registry: Any, setting_key: str, fallback: str) -> str:
    try:
        return str(registry.descriptor(setting_key).title)
    except SettingsError:
        return fallback


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _same_name(left: str, right: str) -> bool:
    return left.casefold().strip() == right.casefold().strip()


def _peek_id(registry: Any, setting_key: str, guild_id: int) -> int | None:
    try:
        value = registry.peek(setting_key, guild_id=guild_id)
    except SettingsError:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _pool_for(item: InventoryItem, snapshot: GuildSnapshot) -> tuple[GuildObject, ...]:
    if item.kind == KIND_ROLE:
        return snapshot.roles
    if item.kind == KIND_CATEGORY:
        return snapshot.categories
    if item.channel_type == KIND_VOICE:
        voice = tuple(obj for obj in snapshot.channels if obj.kind == KIND_VOICE)
        return voice or snapshot.channels
    text = tuple(obj for obj in snapshot.channels if obj.kind != KIND_CATEGORY)
    return text or snapshot.channels


def _default_action(scan: ScanResult) -> tuple[str, int | None]:
    if scan.status in {SCAN_FOUND_BY_ID, SCAN_FOUND_BY_NAME} and scan.matched_id:
        return ACTION_BIND, scan.matched_id
    if scan.status == SCAN_AMBIGUOUS:
        return ACTION_UNSET, None
    return ACTION_CREATE, None


def _merge_choices(
    existing: Mapping[str, Choice],
    scans: Mapping[str, ScanResult],
    inventory: tuple[InventoryItem, ...],
) -> dict[str, Choice]:
    merged: dict[str, Choice] = {}
    by_key = {item.key: item for item in inventory}
    for key, scan in scans.items():
        item = by_key[key]
        previous = existing.get(key)
        if previous is not None and previous.action != ACTION_UNSET:
            merged[key] = Choice(
                action=previous.action,
                target_id=previous.target_id,
                name=item.expected_name,
                kind=item.kind,
                resolved=scan.status,
            )
            continue
        action, target_id = _default_action(scan)
        merged[key] = Choice(
            action=action,
            target_id=target_id,
            name=item.expected_name,
            kind=item.kind,
            resolved=scan.status,
        )
    return merged


def _default_panels() -> dict[str, dict[str, object]]:
    return {
        spec.key: {"enabled": True, "channel_key": spec.channel_key}
        for spec in known_panels()
    }


def _default_permission_map() -> dict[str, list[str]]:
    return {
        key: sorted(permission.value for permission in default_permissions_for(key))
        for key in (*_STAFF_ROLE_KEYS, *_TREASURY_ROLE_KEYS)
    }


async def _new_state(guild_id: int, snapshot: GuildSnapshot) -> WizardState:
    profile = await active_profile(guild_id)
    draft = WizardState(
        correlation_id=new_correlation_id(),
        step="profile",
        profile={
            "server_key": profile.server_key,
            "family_name": profile.family_name,
            "family_tag": profile.family_tag,
            "direction": profile.direction,
            "timezone": profile.timezone,
            "season_start": profile.season_start.isoformat(),
            "season_end": profile.season_end.isoformat(),
            "enabled_upgrades": sorted(profile.enabled_upgrades),
        },
        permissions=_default_permission_map(),
        panels=_default_panels(),
        applied={"created": [], "bound": [], "skipped": [], "attention": []},
    )
    return refresh_scan(draft, snapshot)


def _state_from_payload(payload: Mapping[str, object]) -> WizardState:
    structure_raw = payload.get("structure")
    roles_raw = payload.get("roles")
    permissions_raw = payload.get("permissions")
    panels_raw = payload.get("panels")
    applied_raw = payload.get("applied")
    profile_raw = payload.get("profile")
    structure = {
        str(key): Choice.from_json(value, default_kind=KIND_CHANNEL)
        for key, value in (structure_raw.items() if isinstance(structure_raw, dict) else ())
        if isinstance(value, dict)
    }
    roles = {
        str(key): Choice.from_json(value, default_kind=KIND_ROLE)
        for key, value in (roles_raw.items() if isinstance(roles_raw, dict) else ())
        if isinstance(value, dict)
    }
    permissions: dict[str, list[str]] = {}
    if isinstance(permissions_raw, dict):
        for key, values in permissions_raw.items():
            if isinstance(values, list):
                permissions[str(key)] = [str(item) for item in values]
    panels: dict[str, dict[str, object]] = {}
    if isinstance(panels_raw, dict):
        for key, value in panels_raw.items():
            if isinstance(value, dict):
                panels[str(key)] = dict(value)
    applied: dict[str, list[str]] = {
        "created": [],
        "bound": [],
        "skipped": [],
        "attention": [],
    }
    if isinstance(applied_raw, dict):
        for key, values in applied_raw.items():
            if isinstance(values, list):
                applied[str(key)] = [str(item) for item in values]
    raw_version = payload.get("version")
    version = int(raw_version) if isinstance(raw_version, int) else WIZARD_VERSION
    return WizardState(
        version=version,
        step=_text(payload.get("step"), "profile"),
        correlation_id=_text(payload.get("correlation_id")),
        updated_at=_text(payload.get("updated_at")),
        updated_by=_text(payload.get("updated_by")),
        updated_by_name=_text(payload.get("updated_by_name")),
        profile=dict(profile_raw) if isinstance(profile_raw, dict) else {},
        structure=structure,
        roles=roles,
        permissions=permissions,
        panels=panels,
        confirmed=bool(payload.get("confirmed")),
        applied=applied,
    )


def _classify_choice(
    item: InventoryItem,
    choice: Choice | None,
    scan: ScanResult | None,
    registry: Any,
    guild_id: int,
) -> tuple[tuple[int, int, int, int], str | None]:
    if choice is None or choice.action == ACTION_UNSET:
        if scan is not None and scan.status == SCAN_AMBIGUOUS:
            return (0, 0, 0, 0), (
                f"{item.title}: неоднозначное имя, нужен явный выбор"
            )
        return (0, 0, 0, 0), f"{item.title}: действие не выбрано"
    if choice.action == ACTION_SKIP:
        return (0, 0, 1, 0), None
    if choice.action == ACTION_CREATE:
        current = _peek_id(registry, item.setting_key, guild_id)
        if current is not None and scan is not None and scan.status == SCAN_FOUND_BY_ID:
            return (0, 0, 0, 0), None
        return (1, 0, 0, 1), None
    current = _peek_id(registry, item.setting_key, guild_id)
    changed = 1 if choice.target_id and choice.target_id != current else 0
    return (0, 1, 0, changed), None


def _choice_line(
    item: InventoryItem, choice: Choice | None, scan: ScanResult | None
) -> str:
    action = choice.action if choice is not None else ACTION_UNSET
    status = scan.status if scan is not None else SCAN_MISSING
    return f"{item.key}:{status}:{action}"


def _upgrade_csv(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(str(item) for item in value)
    return ""


def _profile_change_count(state: WizardState, guild_id: int) -> int:
    registry = get_registry()
    changes = 0
    serialized = {
        "server_key": _text(state.profile.get("server_key"), "memphis"),
        "family_name": _text(state.profile.get("family_name")),
        "family_tag": _text(state.profile.get("family_tag")),
        "direction": _text(state.profile.get("direction"), "unset"),
        "timezone": _text(state.profile.get("timezone"), "Europe/Moscow"),
        "season_start": _text(state.profile.get("season_start")),
        "season_end": _text(state.profile.get("season_end")),
        "enabled_upgrades": _upgrade_csv(state.profile.get("enabled_upgrades")),
    }
    for key, value in serialized.items():
        current = registry.peek(key, guild_id=guild_id)
        current_text = "" if current is None else str(current)
        if isinstance(current, (list, tuple, frozenset, set)):
            current_text = ",".join(str(item) for item in current)
        if current_text != str(value):
            changes += 1
    return changes


def _permission_change_count(state: WizardState) -> int:
    return sum(len(values) for values in state.permissions.values())


def _panel_change_count(state: WizardState) -> int:
    return sum(1 for payload in state.panels.values() if bool(payload.get("enabled")))


def _choices_resolved(choices: Mapping[str, Choice]) -> bool:
    if not choices:
        return False
    return all(item.action != ACTION_UNSET for item in choices.values())


def _raise_if_ambiguous_unresolved(state: WizardState) -> None:
    # Channel and role inventories share keys such as ``help``; do not merge.
    for key, choice in (*state.structure.items(), *state.roles.items()):
        if choice.resolved != SCAN_AMBIGUOUS:
            continue
        explicit = choice.action in {ACTION_BIND, ACTION_SKIP} and (
            choice.action != ACTION_BIND or choice.target_id is not None
        )
        if explicit:
            continue
        raise SetupError(
            (
                f"Объект «{choice.name or key}» неоднозначен. "
                "Ничего не создано и не привязано. Выберите существующий "
                "объект явно. Код: SETUP.AMBIGUOUS."
            ),
            code="SETUP.AMBIGUOUS",
        )


def _validated_server_key(value: object) -> str:
    key = _text(value, "memphis").casefold()
    identity = PROFILE_REGISTRY.get(key)
    if identity is None:
        raise SetupError(
            "Неизвестный ключ сервера. Профиль не записан. "
            "Допустимый активный ключ — memphis. Код: SETUP.BAD_SERVER.",
            code="SETUP.BAD_SERVER",
        )
    if identity.archived:
        registry = get_registry()
        allowed = registry.peek("allow_archived")
        if allowed is not True:
            raise SetupError(
                "Архивный профиль нельзя сделать активным без "
                "allow_archived. Значение не записано. Код: SETUP.ARCHIVED.",
                code="SETUP.ARCHIVED",
            )
    return key


def _validated_direction(value: object) -> str:
    text = _text(value, "unset").casefold()
    if text not in KNOWN_DIRECTIONS:
        raise SetupError(
            "Направление должно быть legal, criminal или unset. "
            "Значение не записано. Код: SETUP.BAD_DIRECTION.",
            code="SETUP.BAD_DIRECTION",
        )
    return text


def _validated_timezone(value: object) -> str:
    text = _text(value, "Europe/Moscow")
    try:
        ZoneInfo(text)
    except ZoneInfoNotFoundError as exc:
        raise SetupError(
            "Неизвестный часовой пояс. Значение не записано. "
            "Укажите IANA-имя, например Europe/Moscow. Код: SETUP.BAD_TIMEZONE.",
            code="SETUP.BAD_TIMEZONE",
        ) from exc
    return text


def _validated_date(value: object, field: str) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise SetupError(
            f"{field} должно быть датой ГГГГ-ММ-ДД. Значение не записано. "
            "Код: SETUP.BAD_DATE.",
            code="SETUP.BAD_DATE",
        ) from exc
    return text


def _validated_upgrades(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise SetupError(
            "Список улучшений должен быть набором идентификаторов. "
            "Значение не записано. Код: SETUP.BAD_UPGRADES.",
            code="SETUP.BAD_UPGRADES",
        )
    unknown = sorted({item for item in items if item not in KNOWN_UPGRADES})
    if unknown:
        raise SetupError(
            f"Неизвестный идентификатор улучшения: {', '.join(unknown)}. "
            "Значение не записано. Код: SETUP.BAD_UPGRADES.",
            code="SETUP.BAD_UPGRADES",
        )
    return items


async def _write_setting(
    guild_id: int,
    key: str,
    value: object,
    *,
    actor_id: int,
    source: str,
) -> bool:
    registry = get_registry()
    current = registry.peek(key, guild_id=guild_id)
    if current == value:
        return False
    if current is None and value in (None, ""):
        return False
    await registry.set(
        guild_id,
        key,
        value,
        updated_by=str(actor_id),
        source=source,
    )
    return True


async def _apply_profile(
    guild_id: int,
    state: WizardState,
    *,
    actor_id: int,
    source: str,
) -> int:
    changed = 0
    values: dict[str, object] = {
        "server_key": _text(state.profile.get("server_key"), "memphis"),
        "family_name": _text(state.profile.get("family_name")),
        "family_tag": _text(state.profile.get("family_tag")),
        "direction": _text(state.profile.get("direction"), "unset"),
        "timezone": _text(state.profile.get("timezone"), "Europe/Moscow"),
        "season_start": _text(state.profile.get("season_start")),
        "season_end": _text(state.profile.get("season_end")),
        "enabled_upgrades": _upgrade_csv(state.profile.get("enabled_upgrades")),
    }
    for key, value in values.items():
        if await _write_setting(
            guild_id, key, value, actor_id=actor_id, source=source
        ):
            changed += 1
    return changed


async def _apply_choice(
    guild_id: int,
    item: InventoryItem,
    choice: Choice | None,
    snapshot: GuildSnapshot,
    *,
    actor_id: int,
    actor_name: str,
    source: str,
    correlation_id: str,
) -> tuple[int, str, str]:
    if choice is None or choice.action == ACTION_UNSET:
        return 0, "attention", f"{item.title}: пропущен, действие не выбрано"
    if choice.action == ACTION_SKIP:
        await record_decision(
            guild_id=guild_id,
            action="SETUP.SKIP",
            subject_type=item.kind,
            subject_id=item.key,
            actor_id=actor_id,
            actor_name=actor_name,
            source=source if source in {"command", "component", "system"} else "system",
        )
        return 0, "skipped", ""
    if choice.action == ACTION_BIND:
        if choice.target_id is None:
            return 0, "attention", f"{item.title}: привязка без ID"
        changed = await _write_setting(
            guild_id,
            item.setting_key,
            choice.target_id,
            actor_id=actor_id,
            source=source,
        )
        await record_decision(
            guild_id=guild_id,
            action="SETUP.BIND",
            subject_type=item.kind,
            subject_id=item.key,
            actor_id=actor_id,
            actor_name=actor_name,
            after=str(choice.target_id),
            source=source if source in {"command", "component", "system"} else "system",
        )
        return (1 if changed else 0), "bound", ""

    current = _peek_id(get_registry(), item.setting_key, guild_id)
    existing = _object_by_id(item, snapshot, current)
    if existing is not None:
        changed = await _write_setting(
            guild_id,
            item.setting_key,
            existing.id,
            actor_id=actor_id,
            source=source,
        )
        return (1 if changed else 0), "bound", ""
    kind = (
        PROVISION_ROLE
        if item.kind == KIND_ROLE
        else PROVISION_CATEGORY
        if item.kind == KIND_CATEGORY
        else PROVISION_CHANNEL
    )
    await enqueue_operation(
        idempotency_key=f"provision:{guild_id}:{item.kind}:{item.key}:{correlation_id}",
        guild_id=guild_id,
        kind=kind,
        payload={
            "key": item.key,
            "name": item.expected_name,
            "setting_key": item.setting_key,
            "object_kind": item.kind,
            "channel_type": item.channel_type,
            "correlation_id": correlation_id,
            "actor_id": actor_id,
            "actor_name": actor_name,
        },
        aggregate_id=item.key,
    )
    return 1, "enqueued", ""


def _object_by_id(
    item: InventoryItem, snapshot: GuildSnapshot, object_id: int | None
) -> GuildObject | None:
    if object_id is None:
        return None
    for obj in _pool_for(item, snapshot):
        if obj.id == object_id:
            return obj
    return None


async def _apply_permissions(
    guild_id: int,
    state: WizardState,
    *,
    actor_id: int,
    actor_name: str,
    source: str,
) -> int:
    granted = 0
    for role_key, values in state.permissions.items():
        choice = state.roles.get(role_key)
        role_id = choice.target_id if choice is not None else None
        if role_id is None:
            role_id = _peek_id(get_registry(), f"{role_key.upper()}_ROLE_ID", guild_id)
        if role_id is None:
            continue
        for value in values:
            inserted = await grant_role_permission(
                guild_id,
                value,
                role_id,
                actor_id=actor_id,
                actor_name=actor_name,
                source=source if source in {"command", "component", "system"} else "system",
            )
            if inserted:
                granted += 1
    return granted
