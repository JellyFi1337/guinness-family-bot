from dataclasses import replace

from ui.design import Accent
from ui.message_specs import MessageKind, MessageSpec, MessageTone
from ui.results import completed, failed
from ui.templates import notice, public_panel, staff_work_item

# Guinness visual system: quiet neutral surfaces with semantic state accents.
WHITE_COLOR = 0xF2F3F5
PRIMARY_COLOR = 0x2B2D31
ACCENT_GOLD = 0xF1C40F
PUBLIC_COLOR = 0x5865F2
STAFF_COLOR = 0xD4A72C
ADMIN_COLOR = 0x7C5CFC
SUCCESS_COLOR = MessageTone.SUCCESS.color
ERROR_COLOR = MessageTone.ERROR.color
INFO_COLOR = MessageTone.INFO.color
ANONYMOUS_COLOR = 0x5865F2

_TONE_PREFIXES = ("✅ ", "❌ ", "⚠️ ", "ℹ️ ")


def _plain_title(title: str) -> str:
    for prefix in _TONE_PREFIXES:
        if title.startswith(prefix):
            return title[len(prefix):]
    return title


def apply_accent(spec: MessageSpec, accent: Accent | int) -> MessageSpec:
    """Paint a card with a design-system accent. Never a local file or attachment."""

    return replace(spec, accent=accent)


def panel_embed(
    title: str,
    description: str,
    *,
    sections: tuple[tuple[str, str], ...] = (),
    color: Accent | int = Accent.MEMBER,
    footer: str = "Guinness Family • Интерактивная панель",
    code: str = "PANEL.PUBLIC",
    action: str | None = None,
) -> MessageSpec:
    """Build a stable public panel without a volatile timestamp.

    Static panels are reconciled by payload digest, so their presentation must
    not contain a fresh timestamp on every refresh. ``footer`` is accepted for
    call-site compatibility; the design-system footnote already carries the
    family mark and the panel code.
    """

    del footer
    return apply_accent(
        public_panel(
            code=code,
            title=title,
            purpose=description,
            rules=sections,
            action=action,
        ),
        color,
    )


def request_embed(
    title: str,
    user,
    description: str,
    number: str,
    color: int = STAFF_COLOR,
) -> MessageSpec:
    """One canonical staff-review card, from the StaffWorkItem template."""

    import discord

    return apply_accent(
        staff_work_item(
            code="REQUEST.CARD",
            title=title,
            number=number,
            author=user.mention,
            category=title,
            summary=description,
            deadline=f"<t:{int(discord.utils.utcnow().timestamp())}:R>",
            status="🟡 Ожидает рассмотрения",
        ),
        color,
    )


def anonymous_request_embed(description: str, number: str) -> MessageSpec:
    """A review card that never exposes the author's identity.

    Uses the same template as every other staff card, with the author slot
    filled by a placeholder rather than omitted — a missing field would read as
    a rendering bug, an explicit "скрыт" reads as the intent.
    """

    return apply_accent(
        staff_work_item(
            code="ANONYMOUS.CARD",
            title="Анонимное обращение",
            number=number,
            author="скрыт",
            category="Анонимное обращение",
            summary=description,
            status="🟡 Ожидает ответа старшего состава",
        ),
        ANONYMOUS_COLOR,
    )


def ticket_embed(
    title: str,
    user,
    content: str,
    number: str,
    ticket_type: str,
    *,
    assignee: str | None = None,
    status: str = "🟡 Ожидает рассмотрения",
) -> MessageSpec:
    """A ticket card built from the shared StaffWorkItem template."""

    return apply_accent(
        staff_work_item(
            code="TICKET.CARD",
            title=title,
            number=number,
            author=user.mention,
            category=ticket_type,
            summary=content,
            assignee=assignee,
            status=status,
        ),
        STAFF_COLOR,
    )


def success_embed(
    title: str,
    description: str | None = None,
    number: str | None = None,
) -> MessageSpec:
    plain = _plain_title(title)
    return completed(
        code="UI.SUCCESS",
        title=plain,
        summary=description or plain,
        reference=number,
    )


def error_embed(title: str, description: str | None = None) -> MessageSpec:
    plain = _plain_title(title)
    return failed(
        code="UI.ERROR",
        title=plain,
        summary=description or plain,
    )


def info_embed(title: str, description: str | None = None) -> MessageSpec:
    plain = _plain_title(title)
    return notice(
        code="UI.INFO",
        title=plain,
        summary=description or plain,
        tone=MessageTone.INFO,
    )


def birthday_list_embed(birthdays: list) -> MessageSpec:
    """Birthday channel card assembled from the public-panel template."""

    if not birthdays:
        summary = (
            "Пока нет записей. Добавьте свой день рождения через канал add-birthday!"
        )
    else:
        sorted_bd = sorted(
            birthdays,
            key=lambda item: (
                int(item["date"].split(".")[1]),
                int(item["date"].split(".")[0]),
            ),
        )
        summary = "".join(
            f"**{item['date']}** — {item.get('name', 'Участник')} "
            f"{item.get('mention', '')}\n"
            for item in sorted_bd
        )
    return apply_accent(
        public_panel(
            code="PANEL.BIRTHDAYS",
            title="🎂 Дни рождения семьи Guinness",
            purpose=summary,
        ),
        Accent.MEMBER,
    )


def role_request_embed(user, requested_role: str, number: str) -> MessageSpec:
    return apply_accent(
        staff_work_item(
            code="ROLE_REQUEST.CARD",
            title="Запрос на роль",
            number=number,
            author=user.mention,
            category=requested_role,
            summary=(
                "Проверьте соответствие участника требованиям и зафиксируйте решение "
                "кнопкой под карточкой."
            ),
            status="🟡 Ожидает рассмотрения",
        ),
        STAFF_COLOR,
    )


# Re-export for call sites that already import presentation types from here.
__all__ = (
    "ACCENT_GOLD",
    "ADMIN_COLOR",
    "ANONYMOUS_COLOR",
    "ERROR_COLOR",
    "INFO_COLOR",
    "PRIMARY_COLOR",
    "PUBLIC_COLOR",
    "STAFF_COLOR",
    "SUCCESS_COLOR",
    "WHITE_COLOR",
    "MessageKind",
    "MessageSpec",
    "MessageTone",
    "anonymous_request_embed",
    "apply_accent",
    "birthday_list_embed",
    "error_embed",
    "info_embed",
    "panel_embed",
    "request_embed",
    "role_request_embed",
    "success_embed",
    "ticket_embed",
)
