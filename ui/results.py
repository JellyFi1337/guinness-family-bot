"""Four distinguishable outcomes for any user-facing operation.

Every unclassified failure used to render the same sentence — "Произошла
внутренняя ошибка. Действие не было завершено." — including cases where the
operation had in fact completed and only a follow-up Discord call had failed. A
user told their booking did not happen, when it did, retries and creates a
second one.

An operation now reports which of four things actually happened:

``COMPLETED``  everything, including side effects, is done;
``QUEUED``     the durable change is committed, a side effect is still running;
``UNCHANGED``  nothing was written — a precondition rejected the request;
``FAILED``     the durable change did not happen and a retry is safe.

The distinction matters most for QUEUED: it is the honest answer whenever a
Discord side effect has been handed to the outbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .message_specs import MessageKind, MessageSpec, MessageTone


@dataclass(frozen=True, slots=True)
class UserResult(MessageSpec):
    """Short result card: what happened and the one next action."""


class ResultKind(Enum):
    COMPLETED = "completed"
    QUEUED = "queued"
    UNCHANGED = "unchanged"
    FAILED = "failed"

    @property
    def tone(self) -> MessageTone:
        return {
            ResultKind.COMPLETED: MessageTone.SUCCESS,
            ResultKind.QUEUED: MessageTone.INFO,
            ResultKind.UNCHANGED: MessageTone.WARNING,
            ResultKind.FAILED: MessageTone.ERROR,
        }[self]

    @property
    def default_next_step(self) -> str:
        return {
            ResultKind.COMPLETED: "Дополнительных действий не требуется.",
            ResultKind.QUEUED: "Панель обновится автоматически в течение минуты.",
            ResultKind.UNCHANGED: "Измените условия и повторите действие.",
            ResultKind.FAILED: "Повторите действие — изменения не сохранены.",
        }[self]


def result(
    kind: ResultKind,
    *,
    code: str,
    title: str,
    summary: str,
    next_step: str | None = None,
    details: tuple[tuple[str, str], ...] = (),
    reference: str | None = None,
) -> UserResult:
    """Build the canonical response for one operation outcome."""

    return UserResult(
        code=code,
        title=title,
        summary=summary,
        details=details,
        next_step=next_step or kind.default_next_step,
        tone=kind.tone,
        reference=reference,
        kind=MessageKind.USER_RESULT,
    )


def completed(*, code: str, title: str, summary: str, **kwargs) -> UserResult:
    """The durable change and every side effect are done."""

    return result(ResultKind.COMPLETED, code=code, title=title, summary=summary, **kwargs)


def queued(*, code: str, title: str, summary: str, **kwargs) -> UserResult:
    """The durable change is committed; a side effect is still in the outbox."""

    return result(ResultKind.QUEUED, code=code, title=title, summary=summary, **kwargs)


def unchanged(*, code: str, title: str, summary: str, **kwargs) -> UserResult:
    """A precondition rejected the request; nothing was written."""

    return result(ResultKind.UNCHANGED, code=code, title=title, summary=summary, **kwargs)


def failed(*, code: str, title: str, summary: str, **kwargs) -> UserResult:
    """The durable change did not happen; retrying is safe."""

    return result(ResultKind.FAILED, code=code, title=title, summary=summary, **kwargs)
