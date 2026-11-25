from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Any

import discord

from .message_specs import MessageKind, MessageSpec
from services.metrics import metrics
from .layouts import (
    CONTROL_CONTAINER_ID,
    STATUS_COMPONENT_ID,
    ModernLayoutView,
    build_layout,
    replace_controls,
)


MISSING = object()


def observe_interaction_ack(interaction: discord.Interaction) -> None:
    """Record how long it took to acknowledge the interaction.

    This is the number that matters for "Приложение не отвечает": Discord
    closes the window three seconds after dispatch, regardless of how long the
    work behind the ack takes. It used to be indistinguishable from total
    completion, because the single timer was only read when the final message
    was sent — and 121 handlers call ``interaction.response.defer()`` directly,
    so the timer survived until their followup.
    """

    started_at = interaction.extras.get("metrics.command_started_at")
    if not isinstance(started_at, (int, float)):
        return
    if interaction.extras.get("metrics.ack_observed"):
        return
    interaction.extras["metrics.ack_observed"] = True
    elapsed_ms = max(0.0, (time.perf_counter() - started_at) * 1_000)
    metrics.observe("interaction_ack_ms", elapsed_ms)
    if elapsed_ms > 2_500:
        # Within 500 ms of Discord's hard limit: the next slow database read
        # turns this command into a visible failure.
        metrics.increment("interaction_ack_near_limit")


def _observe_interaction_response(interaction: discord.Interaction) -> None:
    """Record end-to-end completion, separately from the acknowledgement."""

    started_at = interaction.extras.pop("metrics.command_started_at", None)
    if not isinstance(started_at, (int, float)):
        return
    command = getattr(interaction.command, "qualified_name", "unknown")
    elapsed_ms = max(0.0, (time.perf_counter() - started_at) * 1_000)
    metrics.observe(
        f"command.{str(command).replace(' ', '.')}.response_ms",
        elapsed_ms,
    )
    metrics.observe("interaction_complete_ms", elapsed_ms)
    # Retained under its historical name so existing dashboards keep working.
    metrics.observe("command.response_ms", elapsed_ms)
_COMPLETED_DEFERRED: OrderedDict[int, None] = OrderedDict()
_MEDIA_EXTENSIONS = {
    ".apng", ".gif", ".jpeg", ".jpg", ".png", ".webp",
    ".mp4", ".mov", ".webm",
}


def _mark_deferred_completed(interaction_id: int) -> None:
    _COMPLETED_DEFERRED[interaction_id] = None
    _COMPLETED_DEFERRED.move_to_end(interaction_id)
    while len(_COMPLETED_DEFERRED) > 20_000:
        _COMPLETED_DEFERRED.popitem(last=False)


def _pop_message_parts(kwargs: dict[str, Any], positional_content: object) -> tuple:
    content = kwargs.pop("content", positional_content)
    embed = kwargs.pop("embed", None)
    embeds = kwargs.pop("embeds", None)
    view = kwargs.pop("view", None)
    marker = kwargs.pop("component_marker", None)
    return content, embed, embeds, view, marker


def _as_control_view(
    view: object,
) -> discord.ui.View | discord.ui.LayoutView | None:
    if isinstance(view, (discord.ui.View, discord.ui.LayoutView)):
        return view
    return None


def _layout_from_spec(
    spec: MessageSpec,
    view: object,
) -> discord.ui.LayoutView:
    layout: discord.ui.LayoutView = _with_reference(spec).to_layout()
    extra = _as_control_view(view)
    if extra is not None:
        layout = replace_controls(layout, extra)
    return layout


def _modern_payload(positional_content: object, kwargs: dict[str, Any]) -> dict[str, Any]:
    result = dict(kwargs)
    content, embed, embeds, view, marker = _pop_message_parts(result, positional_content)

    if isinstance(embed, MessageSpec):
        view = _layout_from_spec(embed, view)
        embed = None
    if embeds:
        leftover = []
        spec_layouts: list[discord.ui.LayoutView] = []
        for item in embeds:
            if isinstance(item, MessageSpec):
                spec_layouts.append(_layout_from_spec(item, None))
            else:
                leftover.append(item)
        if spec_layouts:
            primary = spec_layouts[0]
            for extra in spec_layouts[1:]:
                for child in list(extra.children):
                    primary.add_item(child)
            extra_view = _as_control_view(view)
            if extra_view is not None:
                primary = replace_controls(primary, extra_view)
            view = primary
        embeds = leftover or None

    attachment_urls: list[str] = []
    attachment_files: list[str] = []
    uploads = []
    if result.get("file") is not None:
        uploads.append(result["file"])
    uploads.extend(result.get("files") or ())
    uploads.extend(result.get("attachments") or ())
    for upload in uploads:
        url = getattr(upload, "url", None)
        filename = getattr(upload, "filename", None)
        media = str(url) if url else f"attachment://{filename}" if filename else None
        if media:
            content_type = str(getattr(upload, "content_type", "") or "").casefold()
            suffix = Path(str(filename or "")).suffix.casefold()
            if content_type.startswith(("image/", "video/")) or suffix in _MEDIA_EXTENSIONS:
                attachment_urls.append(media)
            else:
                attachment_files.append(media)
    layout = build_layout(
        content=None if content is MISSING else content,
        embed=embed,
        embeds=embeds,
        view=view,
        marker=marker,
        attachment_urls=attachment_urls,
        attachment_files=attachment_files,
    )
    result["view"] = layout
    result.setdefault("allowed_mentions", discord.AllowedMentions.none())
    return result


async def send_v2(destination, content: object = MISSING, **kwargs):
    """Send a Components v2 message through any Discord messageable target."""

    # Discord explicitly disables native polls and stickers in Components v2.
    # The project-level /poll command uses its own component implementation, but
    # retaining this guard makes the transport safe for third-party extensions.
    if kwargs.get("poll") is not None or kwargs.get("stickers") is not None:
        raise ValueError(
            "Native polls and stickers are incompatible with Components v2; "
            "use the project poll component or MediaGallery instead."
        )
    return await destination.send(**_modern_payload(content, kwargs))


def _with_reference(spec: MessageSpec) -> MessageSpec:
    if spec.kind is MessageKind.PUBLIC_PANEL:
        return spec
    if spec.reference:
        return spec
    from core.log_context import current_reference

    reference = current_reference()
    if reference == "-":
        return spec
    return replace(spec, reference=reference)


def _layout_kwargs(spec: MessageSpec, kwargs: dict[str, Any]) -> dict[str, Any]:
    payload = dict(kwargs)
    extra_view = payload.pop("view", None)
    layout: discord.ui.LayoutView = _with_reference(spec).to_layout()
    if extra_view is not None:
        layout = replace_controls(layout, extra_view)
    payload["view"] = layout
    return payload


async def send_spec(destination, spec: MessageSpec, **kwargs):
    return await send_v2(destination, **_layout_kwargs(spec, kwargs))


async def respond_v2(
    interaction: discord.Interaction,
    content: object = MISSING,
    **kwargs,
):
    """Create the initial interaction response with Components v2."""

    if interaction.response.is_done():
        return await followup_v2(interaction, content, **kwargs)
    response = await interaction.response.send_message(
        **_modern_payload(content, kwargs)
    )
    observe_interaction_ack(interaction)
    _observe_interaction_response(interaction)
    return response


async def respond_spec(
    interaction: discord.Interaction,
    spec: MessageSpec,
    **kwargs,
):
    return await respond_v2(interaction, **_layout_kwargs(spec, kwargs))


async def followup_v2(
    interaction: discord.Interaction,
    content: object = MISSING,
    **kwargs,
):
    """Complete a deferred response first, then send real follow-ups."""

    if (
        interaction.response.type
        is discord.InteractionResponseType.deferred_channel_message
        and interaction.id not in _COMPLETED_DEFERRED
    ):
        kwargs.pop("ephemeral", None)
        kwargs.pop("wait", None)
        response = await edit_original_response_v2(interaction, content, **kwargs)
        _observe_interaction_response(interaction)
        return response
    response = await interaction.followup.send(**_modern_payload(content, kwargs))
    _observe_interaction_response(interaction)
    return response


async def followup_spec(
    interaction: discord.Interaction,
    spec: MessageSpec,
    **kwargs,
):
    return await followup_v2(interaction, **_layout_kwargs(spec, kwargs))


def _layout_from_existing(message: discord.Message) -> discord.ui.LayoutView:
    if message.flags.components_v2:
        return discord.ui.LayoutView.from_message(message, timeout=None)
    embed = message.embeds[0] if message.embeds else None
    return build_layout(content=message.content or None, embed=embed)


def _replace_controls(
    layout: discord.ui.LayoutView,
    view: discord.ui.View | discord.ui.LayoutView | None,
) -> discord.ui.LayoutView:
    return replace_controls(layout, view)


def _preserve_body_marker(
    message: discord.Message, layout: discord.ui.LayoutView
) -> discord.ui.LayoutView:
    """Keep a hidden reconciliation marker across full body replacements."""

    if not message.flags.components_v2:
        return layout
    existing = _layout_from_existing(message)
    marker_id = next(
        (
            item.id
            for item in existing.children
            if isinstance(item, discord.ui.Container)
            and item.id not in (None, CONTROL_CONTAINER_ID)
        ),
        None,
    )
    if marker_id is None:
        return layout
    targets = [
        item
        for item in layout.children
        if isinstance(item, discord.ui.Container)
        and item.id != CONTROL_CONTAINER_ID
    ]
    if targets and not any(item.id is not None for item in targets):
        targets[0].id = marker_id
    return layout


def _replace_status_text(
    layout: discord.ui.LayoutView,
    status: str,
    accent_color: int | discord.Color,
) -> None:
    displays = [
        item
        for item in layout.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    total = sum(len(item.content) for item in displays)
    current = next(
        (item for item in displays if item.id == STATUS_COMPONENT_ID),
        None,
    )
    if current is not None:
        heading = "### Статус\n"
        available = max(0, 4_000 - (total - len(current.content)) - len(heading))
        current.content = heading + status[:available]
        return

    pattern = re.compile(r"(?s)(### Статус\n).*?(?=\n\n### |\Z)")
    for item in reversed(displays):
        match = pattern.search(item.content)
        if match is None:
            continue
        available = max(0, 4_000 - (total - len(item.content)))
        prefix = item.content[: match.start()]
        suffix = item.content[match.end() :]
        room = max(0, available - len(prefix) - len(suffix) - len("### Статус\n"))
        value = status[:room]
        item.content = prefix + "### Статус\n" + value + suffix
        return

    heading = "### Статус\n"
    available = max(0, 4_000 - total - len(heading))
    if available <= 0:
        return
    layout.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(
                heading + status[:available],
                id=STATUS_COMPONENT_ID,
            ),
            accent_color=accent_color,
        )
    )


def _status_layout(
    message: discord.Message,
    *,
    status: str,
    accent_color: int | discord.Color,
    view: discord.ui.View | discord.ui.LayoutView | None,
    image_filename: str | None = None,
) -> discord.ui.LayoutView:
    layout = replace_controls(_layout_from_existing(message), None)
    body = [
        item
        for item in layout.children
        if isinstance(item, discord.ui.Container)
        and item.id != CONTROL_CONTAINER_ID
    ]
    for container in body:
        container.accent_color = accent_color
    _replace_status_text(layout, status, accent_color)

    if image_filename:
        for container in body:
            for child in list(container.children):
                if isinstance(child, discord.ui.MediaGallery):
                    children = container.children
                    index = children.index(child)
                    container.remove_item(child)
                    if index and isinstance(children[index - 1], discord.ui.Separator):
                        container.remove_item(children[index - 1])
        # The previous media is often stored in its own container. Removing
        # its gallery leaves an empty container, which Discord rejects with
        # components.N.components length=0. Rebuilding once prunes it before
        # the replacement gallery is appended.
        layout = replace_controls(layout, None)
        body = [
            item
            for item in layout.children
            if isinstance(item, discord.ui.Container)
            and item.id != CONTROL_CONTAINER_ID
        ]
        gallery: discord.ui.MediaGallery[ModernLayoutView] = (
            discord.ui.MediaGallery()
        )
        gallery.add_item(
            media=f"attachment://{image_filename}",
            description="Результат рассмотрения заявки",
        )
        layout.add_item(
            discord.ui.Container(gallery, accent_color=accent_color)
        )
    return replace_controls(layout, view)


async def edit_status_message_v2(
    message: discord.Message,
    *,
    status: str,
    accent_color: int | discord.Color,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
    file: discord.File | None = None,
):
    """Patch status/media/controls without losing an existing v2 message body."""

    layout = _status_layout(
        message,
        status=status,
        accent_color=accent_color,
        view=view,
        image_filename=file.filename if file is not None else None,
    )
    payload: dict[str, Any] = {
        "content": None,
        "embeds": [],
        "view": layout,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if file is not None:
        payload["attachments"] = [file]
    return await message.edit(**payload)


async def edit_interaction_status_v2(
    interaction: discord.Interaction,
    *,
    status: str,
    accent_color: int | discord.Color,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
):
    if interaction.message is None:
        raise RuntimeError("Status interaction has no source message")
    layout = _status_layout(
        interaction.message,
        status=status,
        accent_color=accent_color,
        view=view,
    )
    return await interaction.response.edit_message(
        content=None,
        embeds=[],
        view=layout,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def edit_message_v2(
    message: discord.Message,
    content: object = MISSING,
    **kwargs,
):
    """Edit a message and permanently upgrade it to Components v2."""

    has_body = any(key in kwargs for key in ("content", "embed", "embeds")) or content is not MISSING
    view_supplied = "view" in kwargs

    if not has_body and view_supplied:
        view = kwargs.pop("view")
        layout = _replace_controls(_layout_from_existing(message), view)
        payload = dict(kwargs)
        payload.update(content=None, embeds=[], view=layout)
    else:
        payload = _modern_payload(content, kwargs)
        payload["view"] = _preserve_body_marker(message, payload["view"])
        payload.update(content=None, embeds=[])

    file = payload.pop("file", MISSING)
    files = payload.pop("files", MISSING)
    if file is not MISSING:
        payload["attachments"] = [file]
    elif files is not MISSING:
        payload["attachments"] = list(files)
    payload.setdefault("allowed_mentions", discord.AllowedMentions.none())
    return await message.edit(**payload)


async def edit_interaction_message_v2(
    interaction: discord.Interaction,
    content: object = MISSING,
    **kwargs,
):
    """Update the message attached to a component interaction."""

    has_body = any(key in kwargs for key in ("content", "embed", "embeds")) or content is not MISSING
    view_supplied = "view" in kwargs
    if not has_body and view_supplied and interaction.message is not None:
        view = kwargs.pop("view")
        layout = _replace_controls(_layout_from_existing(interaction.message), view)
        payload = dict(kwargs)
        payload.update(content=None, embeds=[], view=layout)
    else:
        payload = _modern_payload(content, kwargs)
        if interaction.message is not None:
            payload["view"] = _preserve_body_marker(
                interaction.message, payload["view"]
            )
        payload.update(content=None, embeds=[])
    file = payload.pop("file", MISSING)
    files = payload.pop("files", MISSING)
    if file is not MISSING:
        payload["attachments"] = [file]
    elif files is not MISSING:
        payload["attachments"] = list(files)
    payload.setdefault("allowed_mentions", discord.AllowedMentions.none())
    return await interaction.response.edit_message(**payload)


async def edit_original_response_v2(
    interaction: discord.Interaction,
    content: object = MISSING,
    **kwargs,
):
    """Edit a deferred/original response using the API-recommended v2 path."""

    payload = _modern_payload(content, kwargs)
    payload.update(content=None, embeds=[])
    file = payload.pop("file", MISSING)
    files = payload.pop("files", MISSING)
    if file is not MISSING:
        payload["attachments"] = [file]
    elif files is not MISSING:
        payload["attachments"] = list(files)
    payload.setdefault("allowed_mentions", discord.AllowedMentions.none())
    result = await interaction.edit_original_response(**payload)
    _mark_deferred_completed(interaction.id)
    return result


def install_ack_instrumentation() -> None:
    """Measure acknowledgement latency for handlers that defer directly.

    ``interaction.response.defer()`` is called from 121 handlers. Editing each
    one to report its own ack time would be a large diff that drifts the moment
    somebody adds handler 122, so the library method is wrapped once instead.
    Idempotent: installing twice does not stack wrappers.
    """

    response_type = discord.InteractionResponse
    if getattr(response_type.defer, "__guinness_instrumented__", False):
        return

    original_defer = response_type.defer

    async def instrumented_defer(self, *args, **kwargs):
        result = await original_defer(self, *args, **kwargs)
        parent = getattr(self, "_parent", None)
        if parent is not None:
            observe_interaction_ack(parent)
        return result

    # Assigning metadata to a function object and replacing a library method
    # are both things the type checker cannot model; the intent is deliberate.
    instrumented_defer.__guinness_instrumented__ = True  # type: ignore[attr-defined]
    response_type.defer = instrumented_defer  # type: ignore[method-assign]
