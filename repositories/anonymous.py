from __future__ import annotations

from dataclasses import dataclass

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


@dataclass(frozen=True, slots=True)
class AnonymousConversation:
    id: int
    guild_id: int
    number: str
    author_id: int
    public_channel_id: int
    public_message_id: int
    log_channel_id: int
    thread_id: int | None
    status: str


@dataclass(frozen=True, slots=True)
class AnonymousMessageLink:
    message_id: int
    channel_id: int
    direction: str


def _conversation(row) -> AnonymousConversation:
    return AnonymousConversation(
        id=int(row[0]),
        guild_id=int(row[1]),
        number=str(row[2]),
        author_id=int(row[3]),
        public_channel_id=int(row[4]),
        public_message_id=int(row[5]),
        log_channel_id=int(row[6]),
        thread_id=int(row[7]) if row[7] is not None else None,
        status=str(row[8]),
    )


async def create_anonymous_conversation(
    *,
    guild_id: int,
    number: str,
    author_id: int,
    public_channel_id: int,
    public_message_id: int,
    log_channel_id: int,
    log_message_id: int,
    thread_id: int,
    thread_message_id: int,
) -> AnonymousConversation:
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "anonymous-conversation", number)
        try:
            cursor = await db.execute(
                """INSERT INTO anonymous_conversations
                   (guild_id, number, author_id, public_channel_id,
                    public_message_id, log_channel_id, thread_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    number,
                    author_id,
                    public_channel_id,
                    public_message_id,
                    log_channel_id,
                    thread_id,
                    now,
                ),
            )
            conversation_id = int(cursor.lastrowid)
            await db.executemany(
                """INSERT INTO anonymous_message_links
                   (message_id, conversation_id, channel_id, direction, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    (
                        public_message_id,
                        conversation_id,
                        public_channel_id,
                        "public",
                        now,
                    ),
                    (
                        log_message_id,
                        conversation_id,
                        log_channel_id,
                        "log",
                        now,
                    ),
                    (
                        thread_message_id,
                        conversation_id,
                        thread_id,
                        "thread",
                        now,
                    ),
                ),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return AnonymousConversation(
        id=conversation_id,
        guild_id=guild_id,
        number=number,
        author_id=author_id,
        public_channel_id=public_channel_id,
        public_message_id=public_message_id,
        log_channel_id=log_channel_id,
        thread_id=thread_id,
        status="open",
    )


async def link_anonymous_message(
    conversation_id: int,
    *,
    message_id: int,
    channel_id: int,
    direction: str,
) -> None:
    if direction not in {"public", "log", "staff_dm", "thread"}:
        raise ValueError("Unsupported anonymous message direction")
    async with _connect() as db:
        await db.execute(
            """INSERT OR IGNORE INTO anonymous_message_links
               (message_id, conversation_id, channel_id, direction, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (message_id, conversation_id, channel_id, direction, _utc_now()),
        )
        await db.commit()


async def get_anonymous_conversation(
    conversation_id: int,
) -> AnonymousConversation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, number, author_id,
                      public_channel_id, public_message_id,
                      log_channel_id, thread_id, status
               FROM anonymous_conversations
               WHERE id = ?""",
            (conversation_id,),
        )
    return _conversation(rows[0]) if rows else None


async def set_anonymous_thread(
    conversation_id: int,
    *,
    thread_id: int,
    thread_message_id: int,
) -> AnonymousConversation | None:
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "anonymous-conversation", conversation_id)
        try:
            await db.execute(
                """UPDATE anonymous_conversations
                   SET thread_id = ?
                   WHERE id = ?""",
                (thread_id, conversation_id),
            )
            await db.execute(
                """INSERT OR IGNORE INTO anonymous_message_links
                   (message_id, conversation_id, channel_id, direction, created_at)
                   VALUES (?, ?, ?, 'thread', ?)""",
                (thread_message_id, conversation_id, thread_id, now),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return await get_anonymous_conversation(conversation_id)


async def get_anonymous_conversation_by_message(
    message_id: int,
) -> AnonymousConversation | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT c.id, c.guild_id, c.number, c.author_id,
                      c.public_channel_id, c.public_message_id,
                      c.log_channel_id, c.thread_id, c.status
               FROM anonymous_message_links AS l
               JOIN anonymous_conversations AS c ON c.id = l.conversation_id
               WHERE l.message_id = ?""",
            (message_id,),
        )
    return _conversation(rows[0]) if rows else None


async def get_anonymous_message_links(
    conversation_id: int,
) -> list[AnonymousMessageLink]:
    """Return every bot message belonging to an anonymous conversation."""

    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT message_id, channel_id, direction
               FROM anonymous_message_links
               WHERE conversation_id = ?
               ORDER BY created_at, message_id""",
            (conversation_id,),
        )
    return [
        AnonymousMessageLink(
            message_id=int(row[0]),
            channel_id=int(row[1]),
            direction=str(row[2]),
        )
        for row in rows
    ]


async def close_anonymous_conversation_by_message(
    message_id: int,
    author_id: int,
) -> AnonymousConversation | None:
    """Atomically close an open conversation from one of its author DM messages."""

    return await _close_anonymous_conversation(
        message_id,
        expected_author_id=author_id,
    )


async def close_anonymous_conversation_by_staff_message(
    message_id: int,
) -> AnonymousConversation | None:
    """Atomically close an open conversation after the caller verified staff access."""

    return await _close_anonymous_conversation(
        message_id,
        expected_author_id=None,
    )


async def _close_anonymous_conversation(
    message_id: int,
    *,
    expected_author_id: int | None,
) -> AnonymousConversation | None:
    async with _connect() as db:
        await begin_write(db, "anonymous-close", message_id)
        try:
            author_filter = (
                "AND c.author_id = ?" if expected_author_id is not None else ""
            )
            parameters: tuple[int, ...] = (
                (message_id, expected_author_id)
                if expected_author_id is not None
                else (message_id,)
            )
            rows = await db.execute_fetchall(
                f"""SELECT c.id, c.guild_id, c.number, c.author_id,
                           c.public_channel_id, c.public_message_id,
                           c.log_channel_id, c.thread_id, c.status
                    FROM anonymous_message_links AS l
                    JOIN anonymous_conversations AS c
                      ON c.id = l.conversation_id
                    WHERE l.message_id = ?
                      {author_filter}
                      AND c.status = 'open'""",
                parameters,
            )
            if not rows:
                await db.rollback()
                return None
            conversation = _conversation(rows[0])
            cursor = await db.execute(
                """UPDATE anonymous_conversations
                   SET status = 'closed', closed_at = ?
                   WHERE id = ? AND status = 'open'""",
                (_utc_now(), conversation.id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return AnonymousConversation(
        id=conversation.id,
        guild_id=conversation.guild_id,
        number=conversation.number,
        author_id=conversation.author_id,
        public_channel_id=conversation.public_channel_id,
        public_message_id=conversation.public_message_id,
        log_channel_id=conversation.log_channel_id,
        thread_id=conversation.thread_id,
        status="closed",
    )
