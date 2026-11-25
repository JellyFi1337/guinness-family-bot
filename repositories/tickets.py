"""Recoverable ticket lifecycle.

Ticket creation is four side effects in a row — a Discord channel, a first
message, a staff notification and an audit line — and it used to run inside one
``try`` with no record of how far it had progressed. A failure after the channel
existed produced an orphan channel, showed the user "Не удалось создать тикет",
and a second attempt created a second channel.

Each step is now recorded before it happens, so a retry resumes instead of
restarting, and the notification and audit lines move to the outbox where a
Discord hiccup no longer masks a ticket that was in fact created.

Assignment (``ticket_assignments``) is deliberately left alone: it already works
and rewriting it here would widen the change without making it safer.
"""

from __future__ import annotations

from dataclasses import dataclass

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


PROVISIONING = "provisioning"
ACTIVE = "active"
CLOSING = "closing"
CLOSED = "closed"
FAILED = "failed"

_COLUMNS = (
    "id, guild_id, interaction_id, ticket_type, number, author_id, "
    "channel_id, message_id, status, assignee_id, last_error"
)


@dataclass(frozen=True, slots=True)
class Ticket:
    id: int
    guild_id: int
    interaction_id: int
    ticket_type: str
    number: str
    author_id: int
    channel_id: int | None
    message_id: int | None
    status: str
    assignee_id: int | None
    last_error: str | None

    @property
    def is_provisioned(self) -> bool:
        return self.status == ACTIVE and self.channel_id is not None


def _ticket(row) -> Ticket:
    return Ticket(
        int(row[0]),
        int(row[1]),
        int(row[2]),
        str(row[3]),
        str(row[4]),
        int(row[5]),
        int(row[6]) if row[6] is not None else None,
        int(row[7]) if row[7] is not None else None,
        str(row[8]),
        int(row[9]) if row[9] is not None else None,
        str(row[10]) if row[10] is not None else None,
    )


def channel_marker(interaction_id: int) -> str:
    """Stable marker written into the channel topic.

    Survives a crash between "channel created" and "channel_id saved": the
    retry finds the existing channel by this marker instead of creating a
    duplicate.
    """

    return f"ticket:{interaction_id}"


async def begin_ticket(
    *,
    guild_id: int,
    interaction_id: int,
    ticket_type: str,
    number: str,
    author_id: int,
) -> Ticket:
    """Claim the interaction and return the ticket record.

    Idempotent: a repeated submit of the same interaction returns the existing
    record rather than starting a second provisioning run.
    """

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "ticket-interaction", interaction_id)
        try:
            await db.execute(
                """INSERT OR IGNORE INTO tickets
                   (guild_id, interaction_id, ticket_type, number, author_id,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    interaction_id,
                    ticket_type,
                    number,
                    author_id,
                    PROVISIONING,
                    now,
                    now,
                ),
            )
            rows = await db.execute_fetchall(
                f"SELECT {_COLUMNS} FROM tickets WHERE interaction_id = ?",
                (interaction_id,),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return _ticket(rows[0])


async def attach_ticket_channel(interaction_id: int, channel_id: int) -> None:
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "ticket-interaction", interaction_id)
        try:
            await db.execute(
                """UPDATE tickets
                   SET channel_id = ?, updated_at = ?, last_error = NULL
                   WHERE interaction_id = ?""",
                (channel_id, now, interaction_id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def activate_ticket(interaction_id: int, message_id: int) -> None:
    """Mark provisioning complete once the first message is published."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "ticket-interaction", interaction_id)
        try:
            await db.execute(
                """UPDATE tickets
                   SET message_id = ?, status = ?, updated_at = ?, last_error = NULL
                   WHERE interaction_id = ?""",
                (message_id, ACTIVE, now, interaction_id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def fail_ticket(interaction_id: int, error: str) -> None:
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "ticket-interaction", interaction_id)
        try:
            await db.execute(
                """UPDATE tickets
                   SET status = ?, updated_at = ?, last_error = ?
                   WHERE interaction_id = ? AND status = ?""",
                (FAILED, now, error[:500], interaction_id, PROVISIONING),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def get_ticket_by_interaction(interaction_id: int) -> Ticket | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_COLUMNS} FROM tickets WHERE interaction_id = ?",
            (interaction_id,),
        )
    return _ticket(rows[0]) if rows else None


async def get_ticket_by_channel(channel_id: int) -> Ticket | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_COLUMNS} FROM tickets WHERE channel_id = ?",
            (channel_id,),
        )
    return _ticket(rows[0]) if rows else None


async def begin_ticket_close(
    *,
    guild_id: int,
    channel_id: int,
    number: str,
    author_id: int,
    ticket_type: str,
    closed_by: int,
) -> bool:
    """Atomically move a ticket into ``closing``.

    Returns ``False`` when another click already claimed the close, which is
    what stops two concurrent presses from both deleting and both writing
    statistics. Tickets created before this table existed are adopted here so
    the legacy close button keeps working.
    """

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "ticket-channel", channel_id)
        try:
            rows = await db.execute_fetchall(
                "SELECT status FROM tickets WHERE channel_id = ?",
                (channel_id,),
            )
            if not rows:
                # Legacy ticket: adopt it directly in the closing state so the
                # rest of the flow is identical for old and new tickets.
                await db.execute(
                    """INSERT INTO tickets
                       (guild_id, interaction_id, ticket_type, number, author_id,
                        channel_id, status, closed_by, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id,
                        # Legacy rows have no interaction; the channel id is
                        # unique and never collides with a Discord interaction
                        # id chosen for a live ticket.
                        -abs(int(channel_id)),
                        ticket_type,
                        number,
                        author_id,
                        channel_id,
                        CLOSING,
                        closed_by,
                        now,
                        now,
                    ),
                )
                await db.commit()
                return True
            if str(rows[0][0]) in {CLOSING, CLOSED}:
                await db.commit()
                return False
            await db.execute(
                """UPDATE tickets
                   SET status = ?, closed_by = ?, updated_at = ?
                   WHERE channel_id = ?""",
                (CLOSING, closed_by, now, channel_id),
            )
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


async def complete_ticket_close(channel_id: int) -> None:
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "ticket-channel", channel_id)
        try:
            await db.execute(
                """UPDATE tickets
                   SET status = ?, closed_at = ?, updated_at = ?
                   WHERE channel_id = ?""",
                (CLOSED, now, now, channel_id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def get_stuck_tickets(guild_id: int, *, limit: int = 50) -> list[Ticket]:
    """Tickets left mid-flight by a crash, for reconciliation and diagnostics."""

    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_COLUMNS} FROM tickets
                WHERE guild_id = ? AND status IN (?, ?, ?)
                ORDER BY created_at
                LIMIT ?""",
            (guild_id, PROVISIONING, CLOSING, FAILED, max(1, int(limit))),
        )
    return [_ticket(row) for row in rows]
