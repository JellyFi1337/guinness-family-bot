"""Single write point for the decision journal."""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from configuration import is_secret_env_name
from core.log_context import (
    MISSING,
    current_reference,
    is_sensitive_name,
    new_correlation_id,
)
from infrastructure.database import connection as _connect, utc_now
from infrastructure.transactions import begin_write
from repositories.audit import Decision, get_decision as _get_decision
from repositories.audit import insert_decision
from repositories.audit import list_decisions as _list_decisions
from repositories.audit import prune_expired_decisions

logger = logging.getLogger(__name__)

AUDIT_SOURCES: frozenset[str] = frozenset(
    {"command", "component", "system", "owner_bypass"}
)
LIST_LIMIT = 200
_ACTION_RE = re.compile(r"^[A-Z][A-Z0-9_]*(\.[A-Z][A-Z0-9_]*)+$")
_POSTGRES_URL = re.compile(r"(?i)postgres(?:ql)?(?:\+psycopg)?://\S+")
_SECRET_MARK = "***"
_PAYLOAD_LIMIT = 4000


class AuditError(ValueError):
    """Invalid audit request with a stable machine-readable code."""

    def __init__(self, message: str, *, code: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _runtime_secrets() -> tuple[str, ...]:
    secrets: list[str] = []
    for key, value in os.environ.items():
        if not value or not is_secret_env_name(key):
            continue
        secrets.append(value)
    return tuple(secrets)


def _redact_payload(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    if is_secret_env_name(name) or is_sensitive_name(name):
        return _SECRET_MARK
    text = _POSTGRES_URL.sub(_SECRET_MARK, str(value))
    for secret in _runtime_secrets():
        if secret and secret in text:
            text = text.replace(secret, _SECRET_MARK)
    if len(text) > _PAYLOAD_LIMIT:
        return text[:_PAYLOAD_LIMIT]
    return text


def _parse_period_bound(value: str, *, end: bool) -> str:
    text = value.strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise AuditError(
                "Не удалось разобрать дату периода. Ожидается ГГГГ-ММ-ДД "
                "или дата-время ISO. Записи не прочитаны; исправьте значение "
                "и повторите. Код: AUDIT.INVALID_PERIOD.",
                code="AUDIT.INVALID_PERIOD",
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.isoformat()
    moment = datetime.combine(day, time.max if end else time.min, tzinfo=UTC)
    return moment.isoformat()


def _correlation_id(explicit: str | None) -> str:
    if explicit and explicit.strip() and explicit.strip() != MISSING:
        return explicit.strip()
    current = current_reference()
    if current and current != MISSING:
        return current
    return new_correlation_id()


async def record_decision(
    *,
    guild_id: int,
    action: str,
    subject_type: str,
    subject_id: str,
    actor_id: int | None = None,
    actor_name: str | None = None,
    before: str | None = None,
    after: str | None = None,
    reason: str | None = None,
    source: str = "system",
    correlation_id: str | None = None,
    db: Any | None = None,
) -> int:
    """Insert one decision. When ``db`` is given, the caller owns the commit."""

    if not _ACTION_RE.match(action):
        raise AuditError(
            f"Код действия {action!r} должен быть вида ОБЛАСТЬ.СОБЫТИЕ. "
            "Запись не создана. Код: AUDIT.INVALID_ACTION.",
            code="AUDIT.INVALID_ACTION",
        )
    if source not in AUDIT_SOURCES:
        raise AuditError(
            f"Источник {source!r} не из разрешённого набора. "
            "Запись не создана. Код: AUDIT.INVALID_SOURCE.",
            code="AUDIT.INVALID_SOURCE",
        )
    subject_type_text = subject_type.strip()
    subject_id_text = str(subject_id).strip()
    if not subject_type_text or not subject_id_text:
        raise AuditError(
            "Тип и идентификатор объекта обязательны. Запись не создана. "
            "Код: AUDIT.INVALID_SUBJECT.",
            code="AUDIT.INVALID_SUBJECT",
        )
    payload_name = subject_id_text if subject_type_text == "setting" else action
    created_at = utc_now()
    resolved_correlation = _correlation_id(correlation_id)
    resolved_actor_id = int(actor_id or 0)
    resolved_actor_name = (actor_name or "").strip() or "unknown"
    redacted_before = _redact_payload(before, name=payload_name)
    redacted_after = _redact_payload(after, name=payload_name)
    redacted_reason = _redact_payload(reason, name="reason")
    if db is not None:
        decision_id = await insert_decision(
            db,
            guild_id=int(guild_id),
            created_at=created_at,
            correlation_id=resolved_correlation,
            actor_id=resolved_actor_id,
            actor_name=resolved_actor_name,
            action=action,
            subject_type=subject_type_text,
            subject_id=subject_id_text,
            before=redacted_before,
            after=redacted_after,
            reason=redacted_reason,
            source=source,
        )
    else:
        async with _connect() as conn:
            await begin_write(conn, "decision-log", guild_id)
            decision_id = await insert_decision(
                conn,
                guild_id=int(guild_id),
                created_at=created_at,
                correlation_id=resolved_correlation,
                actor_id=resolved_actor_id,
                actor_name=resolved_actor_name,
                action=action,
                subject_type=subject_type_text,
                subject_id=subject_id_text,
                before=redacted_before,
                after=redacted_after,
                reason=redacted_reason,
                source=source,
            )
            await conn.commit()
    logger.info(
        "Decision recorded id=%s guild=%s action=%s subject=%s:%s source=%s",
        decision_id,
        guild_id,
        action,
        subject_type_text,
        subject_id_text,
        source,
    )
    return decision_id


async def record_owner_bypass(interaction: Any) -> None:
    user = getattr(interaction, "user", None)
    command = getattr(interaction, "command", None)
    qualified = str(getattr(command, "qualified_name", "") or "unknown")
    guild_id = getattr(interaction, "guild_id", None)
    if guild_id is None:
        guild_id = getattr(getattr(interaction, "guild", None), "id", 0)
    actor_id = int(getattr(user, "id", 0) or 0)
    actor_name = (
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or str(actor_id or "unknown")
    )
    correlation = current_reference()
    if correlation == MISSING:
        correlation = str(getattr(interaction, "id", "") or new_correlation_id())
    await record_decision(
        guild_id=int(guild_id or 0),
        action="ACCESS.OWNER_BYPASS",
        subject_type="command",
        subject_id=qualified,
        actor_id=actor_id,
        actor_name=str(actor_name),
        source="owner_bypass",
        correlation_id=correlation,
        reason=(
            "Владелец бота прошёл проверку, которую обычный пользователь не прошёл."
        ),
    )


async def get_decision(guild_id: int, decision_id: int) -> Decision | None:
    return await _get_decision(guild_id, decision_id)


async def list_decisions(
    guild_id: int,
    *,
    actor_id: int | None = None,
    action: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = LIST_LIMIT,
) -> list[Decision]:
    since_bound = _parse_period_bound(since, end=False) if since else None
    until_bound = _parse_period_bound(until, end=True) if until else None
    if since_bound and until_bound and since_bound > until_bound:
        raise AuditError(
            "Начало периода позже конца. Записи не прочитаны; исправьте даты "
            "и повторите. Код: AUDIT.INVALID_PERIOD.",
            code="AUDIT.INVALID_PERIOD",
        )
    return await _list_decisions(
        guild_id,
        actor_id=actor_id,
        action=action.strip() if action else None,
        subject_type=subject_type.strip() if subject_type else None,
        subject_id=str(subject_id).strip() if subject_id else None,
        since=since_bound,
        until=until_bound,
        limit=min(max(1, int(limit)), LIST_LIMIT),
    )


async def prune_old_decisions(retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    removed = await prune_expired_decisions(older_than=cutoff)
    if removed:
        logger.info(
            "Decision log pruned removed=%s retention_days=%s",
            removed,
            retention_days,
        )
    return removed
