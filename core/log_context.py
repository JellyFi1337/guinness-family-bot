"""Request-scoped log fields via contextvars. Never stores secrets."""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, fields
from typing import Any, Final

from configuration import is_secret_env_name

MISSING: Final = "-"
CONTEXT_SOURCES: Final[frozenset[str]] = frozenset(
    {"command", "component", "modal", "task", "event"}
)
CONTEXT_FIELD_NAMES: Final[tuple[str, ...]] = (
    "correlation_id",
    "command",
    "component_id",
    "user_id",
    "user_name",
    "guild_id",
    "channel_id",
    "channel_name",
    "cog",
    "source",
)
_ANONYMOUS_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "response_text",
        "anonymous_text",
        "appeal_text",
        "content",
        "message",
        "text",
        "body",
    }
)
_VALUE_LIMIT = 200
_SECRET_VALUE_MARK = "***"
_POSTGRES_URL = re.compile(r"(?i)postgres(?:ql)?(?:\+psycopg)?://\S+")
_context: ContextVar[LogContext | None] = ContextVar("guinness_log_context", default=None)


@dataclass(frozen=True, slots=True)
class LogContext:
    correlation_id: str = ""
    command: str = ""
    component_id: str = ""
    user_id: str = ""
    user_name: str = ""
    guild_id: str = ""
    channel_id: str = ""
    channel_name: str = ""
    cog: str = ""
    source: str = ""


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def current_context() -> LogContext:
    return _context.get() or LogContext()


def current_reference() -> str:
    value = current_context().correlation_id.strip()
    return value or MISSING


def clear_context() -> None:
    _context.set(None)


def bind_context(*, replace: bool = False, **kwargs: Any) -> Token[LogContext | None]:
    current = LogContext() if replace else current_context()
    values = {item.name: getattr(current, item.name) for item in fields(LogContext)}
    for key, value in kwargs.items():
        if key not in values:
            raise TypeError(f"Unknown log context field: {key}")
        values[key] = "" if value is None else str(value)
    source = values["source"]
    if source and source not in CONTEXT_SOURCES:
        raise ValueError(f"Unknown log context source: {source}")
    return _context.set(LogContext(**values))


def reset_context(token: Token[LogContext | None]) -> None:
    _context.reset(token)


@contextmanager
def use_context(**kwargs: Any) -> Iterator[LogContext]:
    token = bind_context(replace=True, **kwargs)
    try:
        yield current_context()
    finally:
        reset_context(token)


@contextmanager
def task_context(name: str) -> Iterator[LogContext]:
    with use_context(
        correlation_id=new_correlation_id(),
        command=name,
        source="task",
        cog="",
        component_id="",
    ) as ctx:
        yield ctx


def source_from_interaction(interaction: Any) -> str:
    kind = getattr(getattr(interaction, "type", None), "name", "") or ""
    normalized = str(kind).casefold()
    if "modal" in normalized:
        return "modal"
    if "component" in normalized:
        return "component"
    return "command"


def bind_interaction(
    interaction: Any,
    *,
    source: str | None = None,
    command: str | None = None,
    component_id: str | None = None,
    cog: str | None = None,
) -> Token[LogContext | None]:
    """Bind fields for one Discord interaction. correlation_id is interaction.id."""

    cmd = getattr(interaction, "command", None)
    command_name = command or getattr(cmd, "qualified_name", None)
    data = getattr(interaction, "data", None) or {}
    resolved_component = component_id
    if resolved_component is None and isinstance(data, Mapping):
        raw = data.get("custom_id")
        resolved_component = str(raw) if raw else None
    user = getattr(interaction, "user", None)
    channel = getattr(interaction, "channel", None)
    cog_name = cog or _command_cog_name(cmd)
    interaction_id = getattr(interaction, "id", None)
    return bind_context(
        replace=True,
        correlation_id=str(interaction_id) if interaction_id is not None else new_correlation_id(),
        command=command_name or "",
        component_id=resolved_component or "",
        user_id=getattr(user, "id", "") or "",
        user_name=getattr(user, "name", "") or "",
        guild_id=getattr(interaction, "guild_id", "") or "",
        channel_id=getattr(interaction, "channel_id", "") or "",
        channel_name=getattr(channel, "name", "") or "",
        cog=cog_name or "",
        source=source or source_from_interaction(interaction),
    )


def _command_cog_name(command: Any) -> str:
    if command is None:
        return ""
    binding = getattr(command, "binding", None) or getattr(command, "cog", None)
    if binding is None:
        return ""
    return str(
        getattr(binding, "qualified_name", None) or type(binding).__name__
    )


def is_sensitive_name(name: str) -> bool:
    normalized = name.strip()
    if not normalized:
        return False
    if is_secret_env_name(normalized):
        return True
    folded = normalized.casefold().replace("-", "_")
    if folded in _ANONYMOUS_FIELD_NAMES:
        return True
    return any(
        part in folded
        for part in ("token", "password", "passwd", "secret", "api_key", "database_url")
    )


def _runtime_secrets() -> tuple[str, ...]:
    secrets: list[str] = []
    for key, value in os.environ.items():
        if not value or not is_secret_env_name(key):
            continue
        secrets.append(value)
    return tuple(secrets)


def sanitize_text(value: Any, *, name: str = "") -> str:
    if value is None:
        return ""
    text = str(value)
    if is_sensitive_name(name) or _looks_anonymous_payload(name, text):
        return _SECRET_VALUE_MARK
    text = _POSTGRES_URL.sub(_SECRET_VALUE_MARK, text)
    for secret in _runtime_secrets():
        if secret and secret in text:
            text = text.replace(secret, _SECRET_VALUE_MARK)
    if len(text) > _VALUE_LIMIT:
        return text[:_VALUE_LIMIT]
    return text


def _looks_anonymous_payload(name: str, text: str) -> bool:
    ctx = current_context()
    hint = " ".join(
        (
            name,
            ctx.command,
            ctx.component_id,
            ctx.cog,
        )
    ).casefold()
    if "anonymous" not in hint and "anon" not in hint:
        return False
    return bool(text) and name.casefold() in _ANONYMOUS_FIELD_NAMES


def sanitize_mapping(values: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): sanitize_text(value, name=str(key)) for key, value in values.items()}


def collect_interaction_options(interaction: Any) -> dict[str, str]:
    data = getattr(interaction, "data", None) or {}
    if not isinstance(data, Mapping):
        return {}
    collected: dict[str, Any] = {}
    _walk_options(data.get("options"), collected)
    components = data.get("components")
    if isinstance(components, list):
        _walk_components(components, collected)
    return sanitize_mapping(collected)


def _walk_options(options: Any, dest: dict[str, Any], *, prefix: str = "") -> None:
    if not isinstance(options, list):
        return
    for option in options:
        if not isinstance(option, Mapping):
            continue
        name = str(option.get("name") or "")
        key = f"{prefix}{name}" if prefix and name else name or prefix
        nested = option.get("options")
        if nested and option.get("value") is None:
            _walk_options(nested, dest, prefix=f"{key}." if key else "")
            continue
        if name:
            dest[key] = option.get("value")


def _walk_components(components: list[Any], dest: dict[str, Any]) -> None:
    for item in components:
        if not isinstance(item, Mapping):
            continue
        nested = item.get("components")
        if isinstance(nested, list):
            _walk_components(nested, dest)
        custom_id = item.get("custom_id")
        value = item.get("value")
        if custom_id is not None and value is not None:
            dest[str(custom_id)] = value


class ContextFilter(logging.Filter):
    """Attach context fields to every record. Missing values become '-'."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = current_context()
        for name in CONTEXT_FIELD_NAMES:
            value = getattr(ctx, name, "") or ""
            setattr(record, name, value if value else MISSING)
        return True


async def notify_critical_error(
    client: Any,
    *,
    key: str,
    title: str,
    summary: str,
    detail: str,
    next_step: str,
) -> None:
    alerts = getattr(client, "owner_alerts", None)
    if alerts is None:
        return
    from services.alerts import Alert

    await alerts.raise_alert(
        Alert(
            key=key,
            title=title,
            summary=summary,
            detail=sanitize_text(detail),
            next_step=next_step,
        )
    )


async def report_interaction_error(
    interaction: Any,
    error: BaseException,
    *,
    code: str,
    summary: str,
    retryable: bool,
    item: Any = None,
    source: str | None = None,
    alert_key: str | None = None,
) -> None:
    """Log one ERROR line, show a safe card, and optionally alert the owner."""

    if item is not None and source is None:
        source = "component"
    bind_interaction(
        interaction,
        source=source,
        component_id=getattr(item, "custom_id", None),
    )
    reference = current_reference()
    original = getattr(error, "original", error)
    logger = logging.getLogger("core.log_context")
    logger.error(
        "Unhandled %s error type=%s error=%s options=%s reference=%s",
        current_context().source or "interaction",
        type(original).__name__,
        sanitize_text(original),
        collect_interaction_options(interaction),
        reference,
        exc_info=(type(original), original, original.__traceback__),
    )
    if alert_key:
        await notify_critical_error(
            getattr(interaction, "client", None),
            key=alert_key,
            title="Необработанная ошибка взаимодействия",
            summary=summary,
            detail=(
                f"type={type(original).__name__} command={current_context().command} "
                f"reference={reference}"
            ),
            next_step="Проверьте журнал и назовите reference.",
        )
    from ui.messages import followup_spec, respond_spec
    from ui.templates import system_error

    spec = system_error(
        code=code,
        summary=summary,
        retryable=retryable,
        reference=reference,
    )
    try:
        response = getattr(interaction, "response", None)
        if response is not None and response.is_done():
            await followup_spec(interaction, spec, ephemeral=True)
        else:
            await respond_spec(interaction, spec, ephemeral=True)
    except Exception:
        logger.warning(
            "Could not deliver interaction error response reference=%s",
            reference,
            exc_info=True,
        )
