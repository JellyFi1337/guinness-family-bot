from __future__ import annotations

import hashlib
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, cast

import discord

from repositories.static_messages import (
    StaticMessageRecord,
    delete_static_message,
    get_static_message,
    save_static_message,
    static_panels_enabled,
)
from ui.layouts import marker_component_id
from ui.message_specs import MessageSpec
from ui.messages import edit_message_v2, send_v2
from utils.database import get_setting
from utils.locks import KeyedLockPool


logger = logging.getLogger(__name__)
_LOCKS: KeyedLockPool[tuple[int, str]] = KeyedLockPool()

STATE_DEPLOYED = "deployed"
STATE_MISSING = "missing"
STATE_MESSAGE_DELETED = "message_deleted"
STATE_CHANNEL_NOT_FOUND = "channel_not_found"
STATE_CHANNEL_MOVED = "channel_moved"
STATE_BINDING_LOST = "binding_lost"


@dataclass(frozen=True, slots=True)
class BindingInspection:
    panel_key: str
    state: str
    channel_id: int | None
    message_id: int | None
    revision: int | None
    updated_at: str
    detail: str = ""


async def inspect_static_binding(
    *,
    guild_id: int,
    panel_key: str,
    expected_channel_id: int | None,
    fetch_message: Callable[[int, int], Awaitable[object]] | None = None,
    record: StaticMessageRecord | None = None,
) -> BindingInspection:
    """Read-only check of one stored binding. Does not create or delete messages."""

    stored = record if record is not None else await get_static_message(guild_id, panel_key)
    if expected_channel_id is None:
        return BindingInspection(
            panel_key=panel_key,
            state=STATE_CHANNEL_NOT_FOUND,
            channel_id=stored.channel_id if stored else None,
            message_id=stored.message_id if stored else None,
            revision=stored.revision if stored else None,
            updated_at=stored.updated_at if stored else "",
            detail="канал панели не найден",
        )
    if stored is None:
        return BindingInspection(
            panel_key=panel_key,
            state=STATE_MISSING,
            channel_id=None,
            message_id=None,
            revision=None,
            updated_at="",
            detail="привязка отсутствует",
        )
    if stored.channel_id != expected_channel_id:
        return BindingInspection(
            panel_key=panel_key,
            state=STATE_CHANNEL_MOVED,
            channel_id=stored.channel_id,
            message_id=stored.message_id,
            revision=stored.revision,
            updated_at=stored.updated_at,
            detail="привязка указывает на другой канал",
        )
    if fetch_message is None:
        return BindingInspection(
            panel_key=panel_key,
            state=STATE_DEPLOYED,
            channel_id=stored.channel_id,
            message_id=stored.message_id,
            revision=stored.revision,
            updated_at=stored.updated_at,
        )
    try:
        await fetch_message(stored.channel_id, stored.message_id)
    except discord.NotFound:
        return BindingInspection(
            panel_key=panel_key,
            state=STATE_MESSAGE_DELETED,
            channel_id=stored.channel_id,
            message_id=stored.message_id,
            revision=stored.revision,
            updated_at=stored.updated_at,
            detail="сообщение удалено",
        )
    except discord.Forbidden:
        return BindingInspection(
            panel_key=panel_key,
            state=STATE_BINDING_LOST,
            channel_id=stored.channel_id,
            message_id=stored.message_id,
            revision=stored.revision,
            updated_at=stored.updated_at,
            detail="нет прав прочитать сообщение панели",
        )
    return BindingInspection(
        panel_key=panel_key,
        state=STATE_DEPLOYED,
        channel_id=stored.channel_id,
        message_id=stored.message_id,
        revision=stored.revision,
        updated_at=stored.updated_at,
    )


def _digest_card(card: MessageSpec | discord.Embed) -> dict:
    if isinstance(card, MessageSpec):
        return {
            "code": card.code,
            "title": card.title,
            "summary": card.summary,
            "details": list(card.details),
            "next_step": card.next_step,
            "tone": card.tone.name,
            "reference": card.reference,
            "kind": str(card.kind),
            "accent": None if card.accent is None else int(card.accent),
            "status": card.status,
            "number": card.number,
            "author": card.author,
            "category": card.category,
        }
    return cast(dict[str, Any], card.to_dict())


@dataclass(slots=True)
class StaticPayload:
    content: str | None = None
    embed: MessageSpec | discord.Embed | None = None
    embeds: list[MessageSpec | discord.Embed] | None = None
    view: discord.ui.View | discord.ui.LayoutView | None = None
    file: discord.File | None = None
    files: list[discord.File] | None = None
    allowed_mentions: discord.AllowedMentions | None = None
    revision: int = 1
    asset_digest: str | None = None
    extra: dict = field(default_factory=dict)

    def kwargs(self, marker: str) -> dict:
        result = dict(self.extra)
        if self.content is not None:
            result["content"] = self.content
        if self.embed is not None:
            result["embed"] = self.embed
        if self.embeds is not None:
            result["embeds"] = self.embeds
        if self.view is not None:
            result["view"] = self.view
        if self.file is not None:
            result["file"] = self.file
        if self.files is not None:
            result["files"] = self.files
        if self.allowed_mentions is not None:
            result["allowed_mentions"] = self.allowed_mentions
        result["component_marker"] = marker
        return result

    def digest(self, marker: str) -> str:
        embeds = []
        if self.embed is not None:
            embeds.append(_digest_card(self.embed))
        if self.embeds is not None:
            embeds.extend(_digest_card(embed) for embed in self.embeds)
        components = None
        if self.view is not None:
            to_components = getattr(self.view, "to_components", None)
            if callable(to_components):
                components = to_components()
        filenames = []
        if self.file is not None:
            filenames.append(self.file.filename)
        if self.files:
            filenames.extend(file.filename for file in self.files)
        serialised = json.dumps(
            {
                "content": self.content,
                "embeds": embeds,
                "components": components,
                "files": filenames,
                "revision": self.revision,
                "asset_digest": self.asset_digest,
                "marker": marker,
                "extra": self.extra,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialised.encode("utf-8")).hexdigest()

    def close_files(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
        if self.files:
            for upload in self.files:
                upload.close()
            self.files = None


def _component_text(message: discord.Message) -> str:
    values: list[str] = []

    def visit(component) -> None:
        content = getattr(component, "content", None)
        if content:
            values.append(str(content))
        for child in getattr(component, "children", ()):
            visit(child)

    for component in message.components:
        visit(component)
    return "\n".join(values)


def _has_component_id(message: discord.Message, component_id: int) -> bool:
    def visit(component) -> bool:
        if getattr(component, "id", None) == component_id:
            return True
        return any(visit(child) for child in getattr(component, "children", ()))

    return any(visit(component) for component in message.components)


async def _matches(
    message: discord.Message,
    marker: str,
    legacy_match: Callable[[discord.Message], bool | Awaitable[bool]] | None,
) -> bool:
    if _has_component_id(message, marker_component_id(marker)) or marker in _component_text(message):
        return True
    if legacy_match is None:
        return False
    result = legacy_match(message)
    return bool(await result) if inspect.isawaitable(result) else bool(result)


async def _find_candidates(
    channel: discord.TextChannel | discord.VoiceChannel | discord.Thread,
    marker: str,
    legacy_match: Callable[[discord.Message], bool | Awaitable[bool]] | None,
) -> list[discord.Message]:
    bot_user = channel.guild.me
    if bot_user is None:
        return []
    candidates: list[discord.Message] = []
    try:
        async for message in channel.history(limit=100):
            if message.author.id != bot_user.id:
                continue
            if await _matches(message, marker, legacy_match):
                candidates.append(message)
    except discord.Forbidden:
        logger.warning("Cannot reconcile static messages in channel %s", channel.id)
    return candidates


async def upsert_static_message(
    *,
    guild: discord.Guild,
    channel: discord.TextChannel | discord.VoiceChannel | discord.Thread,
    panel_key: str,
    build: Callable[[], StaticPayload | Awaitable[StaticPayload]],
    legacy_setting_key: str | None = None,
    legacy_match: Callable[[discord.Message], bool | Awaitable[bool]] | None = None,
) -> discord.Message | None:
    """Reconcile, update and persist exactly one guild-scoped static panel."""

    if not await static_panels_enabled(guild.id):
        logger.debug(
            "Static panel publication is disabled guild=%s panel=%s",
            guild.id,
            panel_key,
        )
        return None

    marker = f"panel:{panel_key}:guild:{guild.id}"
    async with _LOCKS.hold((guild.id, panel_key)):
        record = await get_static_message(guild.id, panel_key)
        configured_id = record.message_id if record and record.channel_id == channel.id else None
        if configured_id is None and legacy_setting_key:
            legacy = await get_setting(legacy_setting_key)
            if legacy and legacy.isdigit():
                configured_id = int(legacy)

        canonical: discord.Message | None = None
        if configured_id:
            try:
                canonical = await channel.fetch_message(configured_id)
            except discord.NotFound:
                if record is not None:
                    await delete_static_message(guild.id, panel_key)
            except discord.Forbidden:
                # Do not erase a valid mapping because the bot temporarily lost
                # Read Message History permission.
                raise

        payload = build()
        if inspect.isawaitable(payload):
            payload = await payload
        # Artwork digest is kept in the schema but no longer filled: panels
        # compare payload text, components and revision only.
        payload.asset_digest = None
        payload_digest = payload.digest(marker)

        if (
            canonical is not None
            and record is not None
            and record.channel_id == channel.id
            and record.message_id == canonical.id
            and record.revision == payload.revision
            and record.payload_digest == payload_digest
        ):
            payload.close_files()
            return canonical

        candidates: list[discord.Message] = []
        if canonical is None:
            candidates = await _find_candidates(channel, marker, legacy_match)
            if candidates:
                canonical = max(candidates, key=lambda item: item.id)

        # Reuse an already-uploaded file when a panel still carries one.
        # Static artwork is no longer attached; this only avoids re-uploading
        # an incidental file that the builder still passed.
        if canonical is not None and payload.file is not None:
            if any(
                attachment.filename == payload.file.filename
                for attachment in canonical.attachments
            ):
                payload.file.close()
                payload.file = None
        if canonical is not None and payload.files:
            existing_names = {item.filename for item in canonical.attachments}
            retained: list[discord.File] = []
            for upload in payload.files:
                if upload.filename in existing_names:
                    upload.close()
                else:
                    retained.append(upload)
            payload.files = retained or None
        kwargs = payload.kwargs(marker)

        if canonical is None:
            canonical = await send_v2(channel, **kwargs)
        else:
            canonical = await edit_message_v2(canonical, **kwargs)

        await save_static_message(
            guild.id,
            panel_key,
            channel.id,
            canonical.id,
            revision=payload.revision,
            asset_digest=None,
            payload_digest=payload_digest,
        )

        # Delete only confirmed duplicates after the canonical edit succeeded.
        # Message.delete() takes no ``reason``: unlike Role/Channel/Invite it has
        # no audit-log reason parameter, so passing one raises TypeError at the
        # moment a duplicate is actually found — i.e. only in production.
        for duplicate in candidates:
            if duplicate.id == canonical.id:
                continue
            try:
                await duplicate.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException:
                logger.exception(
                    "Failed to remove duplicate panel=%s message=%s",
                    panel_key,
                    duplicate.id,
                )
        return canonical
