from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


@dataclass(frozen=True, slots=True)
class OpenRequestRef:
    number: str
    kind: str
    user_id: int
    status: str
    created_at: str
    guild_id: int | None


@dataclass(frozen=True, slots=True)
class RequestDecision:
    number: str
    kind: str
    user_id: int
    content: str
    status: str
    guild_id: int | None
    channel_id: int | None
    message_id: int | None
    reviewer_id: int | None
    reason: str | None
    payload: dict
    presentation_state: str
    nickname_status: str
    nickname_requested_by: int | None
    nickname_requested_at: str | None
    nickname_confirmed_at: str | None
    nickname_dm_message_id: int | None


async def list_open_requests(
    guild_id: int, kinds: tuple[str, ...]
) -> list[OpenRequestRef]:
    if not kinds:
        return []
    placeholders = ", ".join("?" for _ in kinds)
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT number, type, user_id, status, created_at, guild_id
                  FROM requests
                 WHERE guild_id = ?
                   AND type IN ({placeholders})
                   AND status IN ('pending', 'processing')
                 ORDER BY created_at, id""",
            (guild_id, *kinds),
        )
    items: list[OpenRequestRef] = []
    for row in rows:
        items.append(
            OpenRequestRef(
                number=str(row[0]),
                kind=str(row[1]),
                user_id=int(row[2]),
                status=str(row[3]),
                created_at=str(row[4]),
                guild_id=int(row[5]) if row[5] is not None else None,
            )
        )
    return items


async def log_request(
    number: str,
    req_type: str,
    user_id: int,
    content: str,
    *,
    guild_id: int | None = None,
    payload: dict | None = None,
) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO requests
               (number, type, user_id, content, created_at, guild_id, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                number,
                req_type,
                user_id,
                content,
                _utc_now(),
                guild_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if payload
                else None,
            ),
        )
        await db.commit()


async def set_request_publication(
    number: str, guild_id: int, channel_id: int, message_id: int
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE requests
               SET guild_id = ?, channel_id = ?, message_id = ?,
                   publication_error = NULL
               WHERE id = (SELECT id FROM requests WHERE number = ?
                           ORDER BY id DESC LIMIT 1)""",
            (guild_id, channel_id, message_id, number),
        )
        await db.commit()


async def get_pending_request_publications():
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT number, type, user_id, content, guild_id,
                      channel_id, message_id, payload_json,
                      nickname_status, nickname_requested_by
               FROM requests
               WHERE status IN ('pending', 'processing')
                 AND type IN ('role_request', 'vzp', 'birthday', 'family_invite')
               ORDER BY id"""
        )


async def fail_request_publication(number: str, error: str) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE requests SET status = 'failed', publication_error = ?
               WHERE id = (SELECT id FROM requests WHERE number = ?
                           AND status = 'pending' ORDER BY id DESC LIMIT 1)""",
            (error[:1_000], number),
        )
        await db.commit()


async def claim_request_review(
    number: str,
    reviewer_id: int,
    *,
    outcome: str | None = None,
    token: str | None = None,
    lease_minutes: int = 15,
) -> bool:
    if outcome is not None and outcome not in {"approved", "rejected"}:
        raise ValueError("Unsupported request review outcome")
    now = datetime.now(UTC)
    lease_until = (now + timedelta(minutes=max(2, lease_minutes))).isoformat()
    token = token or f"legacy:{reviewer_id}"
    async with _connect() as db:
        await begin_write(db, "request-review", number)
        cursor = await db.execute(
            """UPDATE requests SET status = 'processing', reviewer_id = ?,
                                   reviewed_at = ?,
                                   desired_outcome = COALESCE(desired_outcome, ?),
                                   review_token = ?, review_lease_until = ?
               WHERE id = (
                   SELECT id FROM requests
                   WHERE number = ? AND (
                       status = 'pending'
                       OR (
                           status = 'processing'
                           AND review_lease_until < ?
                           AND (desired_outcome IS NULL OR desired_outcome = ?)
                       )
                   )
                   ORDER BY id DESC LIMIT 1
               )""",
            (
                reviewer_id,
                now.isoformat(),
                outcome,
                token,
                lease_until,
                number,
                now.isoformat(),
                outcome,
            ),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            return False
        await db.commit()
        return True


async def finish_request_review(
    number: str,
    reviewer_id: int,
    status: str,
    *,
    token: str | None = None,
    reason: str | None = None,
) -> bool:
    if status not in {"approved", "rejected"}:
        raise ValueError("Unsupported request review status")
    async with _connect() as db:
        await begin_write(db, "request-review", number)
        cursor = await db.execute(
            """UPDATE requests SET status = ?, reviewer_id = ?, reviewed_at = ?,
                                   review_lease_until = NULL,
                                   decision_reason = ?, presentation_state = 'pending'
               WHERE id = (
                   SELECT id FROM requests
                   WHERE number = ? AND status = 'processing'
                     AND (desired_outcome IS NULL OR desired_outcome = ?)
                   ORDER BY id DESC LIMIT 1
               ) AND reviewer_id = ?
                 AND (CAST(? AS TEXT) IS NULL OR review_token = ?)""",
            (
                status,
                reviewer_id,
                _utc_now(),
                reason[:1_000] if reason else None,
                number,
                status,
                reviewer_id,
                token,
                token,
            ),
        )
        if cursor.rowcount > 0:
            rows = await db.execute_fetchall(
                """SELECT guild_id FROM requests WHERE number = ?
                   ORDER BY id DESC LIMIT 1""",
                (number,),
            )
            if rows and rows[0][0] is not None:
                now = datetime.now(UTC)
                available = (now + timedelta(seconds=10)).isoformat()
                await db.execute(
                    """INSERT OR IGNORE INTO operation_outbox
                       (idempotency_key, guild_id, kind, aggregate_id,
                        payload_json, available_at, created_at, updated_at)
                       VALUES (?, ?, 'request.present', ?, ?, ?, ?, ?)""",
                    (
                        f"request:{number}:present",
                        int(rows[0][0]),
                        number,
                        json.dumps({"number": number}, separators=(",", ":")),
                        available,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
        await db.commit()
        return cursor.rowcount > 0


async def expire_request_review_attempt(
    number: str, reviewer_id: int, token: str
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE requests SET review_lease_until = ?
               WHERE id = (
                   SELECT id FROM requests
                   WHERE number = ? AND status = 'processing'
                   ORDER BY id DESC LIMIT 1
               ) AND reviewer_id = ? AND review_token = ?""",
            (_utc_now(), number, reviewer_id, token),
        )
        await db.commit()


async def get_request_decision(number: str) -> RequestDecision | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT number, type, user_id, content, status, guild_id,
                      channel_id, message_id, reviewer_id, decision_reason,
                      payload_json, presentation_state, nickname_status,
                      nickname_requested_by, nickname_requested_at,
                      nickname_confirmed_at, nickname_dm_message_id
               FROM requests WHERE number = ? ORDER BY id DESC LIMIT 1""",
            (number,),
        )
    if not rows:
        return None
    row = rows[0]
    payload: dict = {}
    if row[10]:
        try:
            loaded = json.loads(row[10])
            if isinstance(loaded, dict):
                payload = loaded
        except (TypeError, ValueError):
            pass
    return RequestDecision(
        number=str(row[0]),
        kind=str(row[1]),
        user_id=int(row[2]),
        content=str(row[3]),
        status=str(row[4]),
        guild_id=int(row[5]) if row[5] is not None else None,
        channel_id=int(row[6]) if row[6] is not None else None,
        message_id=int(row[7]) if row[7] is not None else None,
        reviewer_id=int(row[8]) if row[8] is not None else None,
        reason=str(row[9]) if row[9] else None,
        payload=payload,
        presentation_state=str(row[11]),
        nickname_status=str(row[12] or "idle"),
        nickname_requested_by=int(row[13]) if row[13] is not None else None,
        nickname_requested_at=str(row[14]) if row[14] else None,
        nickname_confirmed_at=str(row[15]) if row[15] else None,
        nickname_dm_message_id=int(row[16]) if row[16] is not None else None,
    )


async def claim_nickname_change_request(
    number: str,
    reviewer_id: int,
    *,
    lease_minutes: int = 15,
) -> str:
    """Claim delivery of a nickname request without sending duplicate DMs."""

    now = datetime.now(UTC)
    stale_before = (now - timedelta(minutes=max(2, lease_minutes))).isoformat()
    async with _connect() as db:
        await begin_write(db, "request-nickname", number)
        cursor = await db.execute(
            """UPDATE requests
               SET nickname_status = 'sending',
                   nickname_requested_by = ?,
                   nickname_requested_at = ?,
                   nickname_confirmed_at = NULL,
                   nickname_dm_message_id = NULL
               WHERE id = (
                   SELECT id FROM requests
                   WHERE number = ? AND type = 'family_invite'
                     AND status = 'pending'
                     AND (
                         nickname_status = 'idle'
                         OR (
                             nickname_status = 'sending'
                             AND nickname_requested_at < ?
                         )
                     )
                   ORDER BY id DESC LIMIT 1
               )""",
            (reviewer_id, now.isoformat(), number, stale_before),
        )
        if cursor.rowcount > 0:
            await db.commit()
            return "claimed"
        rows = await db.execute_fetchall(
            """SELECT type, status, nickname_status FROM requests
               WHERE number = ? ORDER BY id DESC LIMIT 1""",
            (number,),
        )
        await db.commit()
    if not rows or rows[0][0] != "family_invite":
        return "missing"
    if rows[0][1] != "pending":
        return "finalized"
    return str(rows[0][2] or "idle")


async def complete_nickname_change_delivery(
    number: str,
    dm_message_id: int,
) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE requests
               SET nickname_status = 'requested', nickname_dm_message_id = ?
               WHERE id = (
                   SELECT id FROM requests WHERE number = ?
                   ORDER BY id DESC LIMIT 1
               ) AND nickname_status = 'sending'""",
            (dm_message_id, number),
        )
        await db.commit()
    return cursor.rowcount > 0


async def release_nickname_change_request(number: str) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE requests
               SET nickname_status = 'idle', nickname_requested_by = NULL,
                   nickname_requested_at = NULL, nickname_dm_message_id = NULL
               WHERE id = (
                   SELECT id FROM requests WHERE number = ?
                   ORDER BY id DESC LIMIT 1
               ) AND nickname_status = 'sending'""",
            (number,),
        )
        await db.commit()


async def confirm_nickname_change(
    dm_message_id: int,
    applicant_id: int,
) -> RequestDecision | None:
    async with _connect() as db:
        await begin_write(db, "request-nickname-dm", dm_message_id)
        rows = await db.execute_fetchall(
            """SELECT number FROM requests
               WHERE nickname_dm_message_id = ? AND user_id = ?
                 AND type = 'family_invite'
               ORDER BY id DESC LIMIT 1""",
            (dm_message_id, applicant_id),
        )
        if not rows:
            await db.commit()
            return None
        number = str(rows[0][0])
        cursor = await db.execute(
            """UPDATE requests
               SET nickname_status = 'confirmed', nickname_confirmed_at = ?
               WHERE id = (
                   SELECT id FROM requests WHERE number = ?
                   ORDER BY id DESC LIMIT 1
               ) AND nickname_status = 'requested'""",
            (_utc_now(), number),
        )
        await db.commit()
    if cursor.rowcount == 0:
        return None
    return await get_request_decision(number)


async def mark_request_presentation_complete(number: str) -> bool:
    async with _connect() as db:
        await begin_write(db, "request-presentation", number)
        rows = await db.execute_fetchall(
            """SELECT status, guild_id, reviewer_id, presentation_state
               FROM requests WHERE number = ? ORDER BY id DESC LIMIT 1""",
            (number,),
        )
        if not rows or rows[0][3] != "pending":
            await db.commit()
            return False
        status, guild_id, reviewer_id, _ = rows[0]
        cursor = await db.execute(
            """UPDATE requests SET presentation_state = 'completed'
               WHERE id = (SELECT id FROM requests WHERE number = ?
                            ORDER BY id DESC LIMIT 1)
                 AND status IN ('approved', 'rejected')
                 AND presentation_state = 'pending'""",
            (number,),
        )
        if cursor.rowcount > 0 and guild_id is not None:
            await db.execute(
                """INSERT INTO stats_processed_actions
                   (guild_id, action_kind, outcome, actor_id, processed_at)
                   VALUES (?, 'application', ?, ?, ?)""",
                (int(guild_id), str(status), reviewer_id, _utc_now()),
            )
        await db.commit()
    return cursor.rowcount > 0


async def pending_request_presentation_count() -> int:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT COUNT(*) FROM requests
               WHERE status IN ('approved', 'rejected')
                 AND presentation_state = 'pending'"""
        )
    return int(rows[0][0])
