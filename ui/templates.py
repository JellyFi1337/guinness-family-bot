"""Five message shapes every bot response fits into.

`MessageSpec` existed but was reachable from exactly one place — the global
command-error handler. Everything else hand-built a `discord.Embed` (62 call
sites) or replied with a bare string, so the same concept looked different in
every cog: some replies carried a code, most did not; some named a next step,
most left the user guessing; identifiers appeared as a title, a footer, or not
at all.

These builders cover the five audiences the bot actually writes for. They all
produce a `MessageSpec`, so every response gets the same guarantees:

* a stable ``code`` support can search for;
* a title without a duplicated emoji (the tone already supplies one);
* one sentence of outcome;
* a next step;
* a reference the owner can trace.

`ui.results` covers the fifth shape (`UserResult`) and is re-exported here so
callers have one import. Native delivery goes through `MessageSpec.to_layout()`.
"""

from __future__ import annotations

from .design import Accent
from .message_specs import (
    MessageKind,
    MessageSpec,
    MessageTone,
    format_source_attribution,
)
from .results import ResultKind, UserResult, completed, failed, queued, result, unchanged


__all__ = [
    "MessageKind",
    "MessageSpec",
    "MessageTone",
    "ResultKind",
    "UserResult",
    "UNVERIFIED",
    "admin_receipt",
    "completed",
    "failed",
    "format_source_attribution",
    "instruction_material",
    "instruction_summary",
    "notice",
    "public_panel",
    "queued",
    "result",
    "staff_work_item",
    "missing_permission",
    "system_error",
    "unchanged",
]

UNVERIFIED = "требует проверки"


def instruction_material(
    *,
    topic: str | None = None,
    server: str | None = None,
    source: str | None = None,
    verified_at: str | None = None,
    responsible: str | None = None,
    version: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Metadata of one instruction attached to a task — never a wall of rules."""

    def _value(raw: str | None) -> str:
        text = (raw or "").strip()
        return text if text else UNVERIFIED

    return (
        ("Тема", _value(topic)),
        ("Сервер", _value(server)),
        ("Источник", _value(source)),
        ("Дата проверки", _value(verified_at)),
        ("Ответственный", _value(responsible)),
        ("Версия", _value(version)),
    )


def instruction_summary(
    *,
    topic: str | None = None,
    server: str | None = None,
    source: str | None = None,
    verified_at: str | None = None,
    responsible: str | None = None,
    version: str | None = None,
) -> str:
    return " · ".join(
        f"{label}: {value}"
        for label, value in instruction_material(
            topic=topic,
            server=server,
            source=source,
            verified_at=verified_at,
            responsible=responsible,
            version=version,
        )
    )


def notice(
    *,
    code: str,
    title: str,
    summary: str | None = None,
    details: tuple[tuple[str, str], ...] = (),
    next_step: str | None = None,
    tone: MessageTone = MessageTone.NEUTRAL,
    reference: str | None = None,
    accent: Accent | int | None = None,
) -> MessageSpec:
    """A generic card assembled from design primitives, never an embed."""

    return MessageSpec(
        code=code,
        title=title,
        summary=summary,
        details=details,
        next_step=next_step or "Дополнительных действий не требуется.",
        tone=tone,
        reference=reference,
        kind=MessageKind.GENERIC,
        accent=accent,
    )


def public_panel(
    *,
    code: str,
    title: str,
    purpose: str,
    rules: tuple[tuple[str, str], ...] = (),
    action: str | None = None,
) -> MessageSpec:
    """A permanent panel in a public channel.

    Short statement of purpose, the rules that matter before acting, and the
    one action the reader is meant to take. No status, no identifiers — nothing
    here is about a particular case.
    """

    return MessageSpec(
        code=code,
        title=title,
        summary=purpose,
        details=rules,
        next_step=action,
        tone=MessageTone.NEUTRAL,
        kind=MessageKind.PUBLIC_PANEL,
    )


def staff_work_item(
    *,
    code: str,
    title: str,
    number: str,
    author: str,
    category: str,
    summary: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
    deadline: str | None = None,
    history: str | None = None,
) -> MessageSpec:
    """A case staff has to act on: ticket, application, request.

    Ordering is deliberate. Number, author and category identify the case;
    assignee and status say whether anyone owns it. ``status`` is rendered as a
    field named "Статус", which the layout reserves budget for before any
    descriptive text — on a long card the status is the last thing that may be
    lost, not the first.
    """

    details: list[tuple[str, str]] = [
        ("Номер", number),
        ("Заявитель", author),
        ("Категория", category),
    ]
    if assignee:
        details.append(("Ответственный", assignee))
    if deadline:
        details.append(("Срок", deadline))
    if history:
        details.append(("История", history))
    if status:
        details.append(("Статус", status))
    return MessageSpec(
        code=code,
        title=title,
        summary=summary,
        details=tuple(details),
        tone=MessageTone.NEUTRAL,
        reference=number,
        kind=MessageKind.STAFF_WORK_ITEM,
        status=status,
        number=number,
        author=author,
        category=category,
        deadline=deadline,
        history=history,
        assignee=assignee,
    )


def admin_receipt(
    *,
    code: str,
    title: str,
    actor: str,
    changed: str,
    before: str | None = None,
    after: str | None = None,
    reason: str | None = None,
    reference: str | None = None,
) -> MessageSpec:
    """Proof that an administrative change happened, and what it replaced.

    An audit line without the previous value cannot be reviewed or reversed, so
    ``before``/``after`` are rendered as one field rather than left to prose.
    """

    details: list[tuple[str, str]] = [("Изменено", changed), ("Кем", actor)]
    if before is not None or after is not None:
        details.append(
            ("Было → стало", f"{before or '—'} → **{after or '—'}**")
        )
    if reason:
        details.append(("Основание", reason))
    return MessageSpec(
        code=code,
        title=title,
        details=tuple(details),
        tone=MessageTone.SUCCESS,
        reference=reference,
        kind=MessageKind.ADMIN_RECEIPT,
        actor=actor,
        changed=changed,
        before=before,
        after=after,
        reason=reason,
    )


def missing_permission(decision, *, reference: str) -> MessageSpec:
    """Refusal that names the missing domain permission."""

    permission = getattr(decision, "missing", None)
    title = getattr(permission, "label", None)
    value = getattr(permission, "value", None)
    named = f"«{title}» ({value})" if title and value else "требуемое полномочие"
    summary = getattr(decision, "summary", None) or (
        f"Не хватает полномочия {named}. Действие не выполнено."
    )
    return MessageSpec(
        code=getattr(decision, "code", None) or "ACCESS.MISSING_PERMISSION",
        title="Действие недоступно",
        summary=summary,
        next_step=(
            "Обратитесь к старшему составу, чтобы выдали названное полномочие. "
            "Повторять то же действие без полномочия бессмысленно."
        ),
        tone=MessageTone.WARNING,
        reference=reference,
        kind=MessageKind.USER_RESULT,
    )


def system_error(
    *,
    code: str,
    title: str = "Действие не выполнено",
    summary: str,
    retryable: bool,
    reference: str,
) -> MessageSpec:
    """A failure the user can report.

    ``summary`` must stay safe to show in public: exception text can carry URLs,
    file paths and library internals, and it belongs in the log. Saying plainly
    whether a retry is safe removes the guesswork that makes users repeat an
    operation that already succeeded.
    """

    return MessageSpec(
        code=code,
        title=title,
        summary=summary,
        next_step=(
            "Повторите действие — изменения не сохранены."
            if retryable
            else "Не повторяйте действие: сообщите владельцу код и reference."
        ),
        tone=MessageTone.ERROR,
        reference=reference,
        kind=MessageKind.SYSTEM_ERROR,
        retryable=retryable,
    )
