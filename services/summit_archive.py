"""Close leftover gathering and contract lists before the old slash commands go away."""

from __future__ import annotations

from dataclasses import dataclass

from repositories.operations import enqueue_operation
from repositories.summits import (
    ArchivedLegacyGathering,
    ArchivedVzpList,
    archive_legacy_gathering,
    archive_vzp_position_list,
    is_summit_list_archived,
    is_vzp_list_archived,
    list_unarchived_legacy_gathering_ids,
    list_unarchived_vzp_list_ids,
)
from ui.command_registry import cmd
from ui.message_specs import MessageSpec
from ui.templates import notice

ARCHIVE_KIND = "summit.archive"


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    gatherings: int
    position_lists: int
    skipped: int


def archived_gathering_spec(*, reference: str | None = None) -> MessageSpec:
    return notice(
        code="SUMMIT.ARCHIVED",
        title="Сбор архивирован",
        summary=(
            "Этот сбор закрыт и больше не принимает записи. "
            "Согласованный состав сохранён в архивной записи и не удалён. "
            "Повтор нажатия безопасен и ничего не меняет."
        ),
        next_step=(
            f"Новые события — {cmd('event create')} и {cmd('event list')}. "
            f"Семейные контракты — {cmd('contract start')}."
        ),
        reference=reference,
    )


def archive_channel_spec(
    title: str,
    *,
    member_count: int,
    list_id: int,
    kind: str,
) -> MessageSpec:
    if kind == "contract":
        summary = (
            f"Контракт «{title}» закрыт и архивирован. "
            f"Согласованный состав ({member_count} чел.) сохранён. "
            "Старые кнопки больше не записывают участников."
        )
    else:
        summary = (
            f"Сбор «{title}» закрыт и архивирован. "
            f"Согласованный состав ({member_count} чел.) сохранён. "
            "Старые кнопки больше не записывают участников."
        )
    return notice(
        code="SUMMIT.ARCHIVED",
        title="Сбор закрыт и архивирован",
        summary=summary,
        next_step=(
            f"Новые события — {cmd('event create')} и {cmd('event list')}. "
            f"Семейные контракты — {cmd('contract start')}."
        ),
        reference=f"#{list_id}",
    )


async def archived_interaction_spec(
    *,
    list_id: int | None = None,
    vzp_list_id: int | None = None,
) -> MessageSpec | None:
    if list_id is not None and await is_summit_list_archived(list_id):
        return archived_gathering_spec(reference=f"#{list_id}")
    if vzp_list_id is not None and await is_vzp_list_archived(vzp_list_id):
        return archived_gathering_spec(reference=f"#{vzp_list_id}")
    return None


async def archive_open_legacy_gatherings() -> ArchiveReport:
    gatherings = 0
    skipped = 0
    for list_id in await list_unarchived_legacy_gathering_ids():
        record = await archive_legacy_gathering(list_id)
        if record is None:
            skipped += 1
            continue
        await _enqueue_gathering_notice(record)
        gatherings += 1
    position_lists = 0
    for vzp_id in await list_unarchived_vzp_list_ids():
        vzp_record = await archive_vzp_position_list(vzp_id)
        if vzp_record is None:
            skipped += 1
            continue
        await _enqueue_vzp_notice(vzp_record)
        position_lists += 1
    return ArchiveReport(
        gatherings=gatherings,
        position_lists=position_lists,
        skipped=skipped,
    )


async def _enqueue_gathering_notice(record: ArchivedLegacyGathering) -> int:
    member_count = len(record.member_ids) + len(record.reserve_ids)
    return await enqueue_operation(
        idempotency_key=f"summit.archive:list:{record.id}",
        guild_id=int(record.guild_id or 0),
        kind=ARCHIVE_KIND,
        aggregate_id=f"summit_list:{record.id}",
        payload={
            "list_id": record.id,
            "kind": record.kind,
            "title": record.title,
            "creator_id": record.creator_id,
            "channel_id": record.channel_id,
            "message_id": record.message_id,
            "member_count": member_count,
            "member_ids": list(record.member_ids),
            "reserve_ids": list(record.reserve_ids),
        },
    )


async def _enqueue_vzp_notice(record: ArchivedVzpList) -> int:
    return await enqueue_operation(
        idempotency_key=f"summit.archive:vzp:{record.id}",
        guild_id=int(record.guild_id or 0),
        kind=ARCHIVE_KIND,
        aggregate_id=f"summit_vzp:{record.id}",
        payload={
            "list_id": record.id,
            "kind": "vzp_positions",
            "title": record.title,
            "creator_id": record.creator_id,
            "channel_id": record.channel_id,
            "message_id": record.message_id,
            "member_count": len(record.occupant_ids),
            "member_ids": list(record.occupant_ids),
            "reserve_ids": [],
        },
    )
