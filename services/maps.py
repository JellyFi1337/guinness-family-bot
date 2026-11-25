"""Staff-filled Memphis map catalog. Discord Select pages are not a catalog cap."""

from __future__ import annotations

from typing import Any, Sequence

from core.permissions import AccessDecision, Permission, evaluate_access
from infrastructure.database import DATABASE_INTEGRITY_ERRORS
from repositories.maps import (
    FamilyMap,
    archive_map as persist_archive,
    get_map,
    insert_map,
    list_active_maps as persist_list,
)
from services.membership import ensure_family
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.templates import notice

# Discord allows at most 25 options on one Select. The catalog itself has no cap.
SELECT_PAGE_SIZE = 25
_FIELD_LIMIT = 100


class MapError(Exception):
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


def catalog_ref(map_id: int) -> str:
    return f"catalog:{map_id}"


def parse_catalog_ref(raw: str) -> int | None:
    prefix = "catalog:"
    if not raw.startswith(prefix):
        return None
    try:
        return int(raw[len(prefix) :])
    except ValueError:
        return None


def map_select_page(
    maps: Sequence[FamilyMap],
    page: int,
) -> tuple[tuple[FamilyMap, ...], int, int]:
    if not maps:
        return (), 0, 1
    total_pages = (len(maps) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE
    current = min(max(0, page), total_pages - 1)
    start = current * SELECT_PAGE_SIZE
    return tuple(maps[start : start + SELECT_PAGE_SIZE]), current, total_pages


def _clean_field(value: str, *, label: str) -> str:
    text = " ".join(value.split())
    if not text:
        raise MapError(
            f"Поле «{label}» пусто. Карта не записана; заполните его и повторите.",
            code="MAPS.REQUIRED",
            retryable=True,
        )
    if len(text) > _FIELD_LIMIT:
        raise MapError(
            f"Поле «{label}» длиннее {_FIELD_LIMIT} символов. Карта не записана.",
            code="MAPS.TOO_LONG",
            retryable=True,
        )
    return text


def _actor_id(actor: Any) -> int:
    return int(getattr(actor, "id", 0) or 0)


async def _require(actor: Any, permission: Permission) -> None:
    decision = await evaluate_access(actor, permission)
    if decision.allowed:
        return
    raise MapError(
        decision.summary,
        code=decision.code,
        retryable=False,
        decision=decision,
    )


async def list_catalog(guild_id: int) -> tuple[FamilyMap, ...]:
    family = await ensure_family(guild_id)
    return await persist_list(guild_id, family.id)


async def list_maps(actor: Any, guild_id: int) -> tuple[FamilyMap, ...]:
    await _require(actor, Permission.EVENTS_CREATE)
    return await list_catalog(guild_id)


async def add_map(
    actor: Any,
    guild_id: int,
    *,
    name: str,
    event_type: str,
    version: str,
    source: str,
) -> FamilyMap:
    await _require(actor, Permission.EVENTS_MANAGE_ALL)
    family = await ensure_family(guild_id)
    cleaned_name = _clean_field(name, label="название")
    cleaned_type = _clean_field(event_type, label="тип события")
    cleaned_version = _clean_field(version, label="версия")
    cleaned_source = _clean_field(source, label="источник")
    try:
        return await insert_map(
            guild_id=guild_id,
            family_id=family.id,
            name=cleaned_name,
            event_type=cleaned_type,
            version=cleaned_version,
            source=cleaned_source,
            created_by=_actor_id(actor),
        )
    except DATABASE_INTEGRITY_ERRORS as exc:
        raise MapError(
            f"Карта «{cleaned_name}» уже есть в каталоге. "
            "Вторая запись не создана.",
            code="MAPS.DUPLICATE",
            retryable=False,
        ) from exc


async def archive_map(actor: Any, guild_id: int, map_id: int) -> FamilyMap:
    await _require(actor, Permission.EVENTS_MANAGE_ALL)
    current = await get_map(guild_id, map_id)
    if current is None:
        raise MapError(
            "Карта не найдена. Каталог не изменён.",
            code="MAPS.NOT_FOUND",
            retryable=False,
        )
    if current.archived:
        raise MapError(
            f"Карта «{current.name}» уже в архиве. Повтор не создаёт вторую запись.",
            code="MAPS.ARCHIVED",
            retryable=False,
        )
    archived = await persist_archive(
        guild_id,
        map_id,
        archived_by=_actor_id(actor),
    )
    if archived is None:
        raise MapError(
            "Карта не найдена. Каталог не изменён.",
            code="MAPS.NOT_FOUND",
            retryable=False,
        )
    return archived


def catalog_spec(maps: Sequence[FamilyMap]) -> MessageSpec:
    if not maps:
        return notice(
            code="MAPS.EMPTY",
            title="Каталог карт",
            summary=(
                "В каталоге пока нет записей. Совпадение названия местности "
                "с другой игрой не доказывает совпадение карты — записи "
                "вносит старший состав."
            ),
            next_step=(
                f"Добавьте карту через {cmd('event map_add')} "
                "с версией, типом события и источником."
            ),
        )
    details = tuple(
        (
            f"#{item.id} {item.name}",
            f"{item.event_type} · {item.version} · {item.source}",
        )
        for item in maps
    )
    return notice(
        code="MAPS.LIST",
        title="Каталог карт",
        summary=(
            f"Записей: {len(maps)}. Выбор идёт страницами по "
            f"{SELECT_PAGE_SIZE} пунктам Discord Select, лимита каталога нет."
        ),
        details=details,
        next_step=f"Архив записи — {cmd('event map_archive')}.",
    )
