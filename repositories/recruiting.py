"""Durable invite baselines and explicit non-attribution.

Invite use counters used to live in a plain dict. After a restart the baseline
was empty, so ``previous.get(code, 0)`` returned 0 for every managed invite and
the next join was credited to whichever link happened to have the highest
lifetime use count — permanently, and silently.

Two changes fix that. The baseline is persisted, so a restart resumes from the
last observed counts. And when the evidence is not conclusive — no baseline yet,
several invites moved between two polls, or a delta larger than one — the join
is recorded as *unresolved* instead of guessed. An unresolved join is visible
and fixable; a wrong guess is neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from infrastructure.database import connection as _connect, utc_now as _utc_now
from infrastructure.transactions import begin_write


REASON_NO_BASELINE = "no_baseline"
REASON_AMBIGUOUS = "ambiguous"
REASON_MULTIPLE_DELTA = "multiple_delta"
REASON_UNKNOWN_INVITE = "unknown_invite"

REASON_LABELS = {
    REASON_NO_BASELINE: "нет baseline после перезапуска",
    REASON_AMBIGUOUS: "изменилось несколько ссылок",
    REASON_MULTIPLE_DELTA: "счётчик вырос больше чем на 1",
    REASON_UNKNOWN_INVITE: "вход не по управляемой ссылке",
}


@dataclass(frozen=True, slots=True)
class Attribution:
    """What a single join can be credited to, if anything."""

    invite_code: str | None
    reason: str | None
    candidates: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.invite_code is not None

    @property
    def needs_review(self) -> bool:
        return self.invite_code is None and self.reason is not None


def decide_attribution(
    *,
    baseline: dict[str, int],
    observed: dict[str, int],
    managed_codes: frozenset[str] | set[str],
    baseline_ready: bool,
) -> Attribution:
    """Decide which managed invite a join belongs to.

    Pure by design so the rules can be tested without a gateway. Every branch
    that is not a single unambiguous +1 on exactly one managed invite refuses to
    guess: a wrong recruiter credit is permanent and invisible, while an
    unresolved join shows up in ``/recruiting unresolved`` and can be fixed.
    """

    if not baseline_ready and not baseline:
        return Attribution(None, REASON_NO_BASELINE)

    moved = [
        (code, observed[code] - baseline.get(code, 0))
        for code in observed
        if observed[code] > baseline.get(code, 0)
    ]
    managed_moved = [(code, delta) for code, delta in moved if code in managed_codes]

    if not managed_moved:
        if moved:
            return Attribution(
                None, REASON_UNKNOWN_INVITE, tuple(code for code, _ in moved)
            )
        # Nothing moved at all: a vanity URL, a widget, or an invite the bot
        # cannot see. Not something a recruiter should be credited for.
        return Attribution(None, None)
    if len(managed_moved) > 1:
        return Attribution(
            None, REASON_AMBIGUOUS, tuple(code for code, _ in managed_moved)
        )
    code, delta = managed_moved[0]
    if delta > 1:
        # Several members joined between two polls and only one of them is this
        # one; nothing in the counters says which.
        return Attribution(None, REASON_MULTIPLE_DELTA, (f"{code} (+{delta})",))
    return Attribution(code, None)


@dataclass(frozen=True, slots=True)
class UnresolvedJoin:
    id: int
    guild_id: int
    member_id: int
    joined_at: str
    reason: str
    candidates: str

    @property
    def reason_label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)


@dataclass(frozen=True, slots=True)
class RecruiterTotals:
    recruiter_id: int
    joined: int
    applied: int
    accepted: int


@dataclass(frozen=True, slots=True)
class RecruitingSummary:
    """Everything the recruiting panel needs, aggregated by the database."""

    joined: int
    applied: int
    accepted: int
    rejected: int
    pending: int
    per_recruiter: tuple[RecruiterTotals, ...]
    accepted_recent: tuple[tuple[int, int], ...]
    accepted_total: tuple[tuple[int, int], ...]

    @property
    def conversion(self) -> float:
        return self.accepted / self.joined * 100 if self.joined else 0.0

    def digest(self) -> tuple:
        """Stable identity of the rendered content.

        The panel used to be edited on every ten-minute pass whether or not
        anything had changed. Comparing this against the previous pass turns an
        unchanged report into zero Discord requests.
        """

        return (
            self.joined,
            self.applied,
            self.accepted,
            self.rejected,
            self.pending,
            tuple(
                (item.recruiter_id, item.joined, item.applied, item.accepted)
                for item in self.per_recruiter
            ),
            self.accepted_recent,
            self.accepted_total,
        )


async def get_recruiting_summary(
    guild_id: int, *, recent_since: str, limit: int = 10
) -> RecruitingSummary:
    """Aggregate the recruiting funnel in SQL rather than in Python.

    The panel used to load every referral row ever recorded and count them in a
    loop, every ten minutes. That cost grows with the history and never shrinks,
    so the report gets slower forever. Four grouped queries return the same
    numbers in constant-ish time and let indexes do the work.
    """

    async with _connect() as db:
        totals = await db.execute_fetchall(
            """SELECT COUNT(*),
                      SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN applied_at IS NOT NULL
                                AND status NOT IN ('accepted', 'rejected')
                               THEN 1 ELSE 0 END)
                 FROM recruiting_referrals WHERE guild_id = ?""",
            (guild_id,),
        )
        per_recruiter = await db.execute_fetchall(
            """SELECT recruiter_id,
                      COUNT(*) AS joined,
                      SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END)
                 FROM recruiting_referrals WHERE guild_id = ?
                 GROUP BY recruiter_id""",
            (guild_id,),
        )
        accepted_recent = await db.execute_fetchall(
            """SELECT recruiter_id, COUNT(*) AS total
                 FROM recruiting_referrals
                WHERE guild_id = ? AND status = 'accepted'
                  AND accepted_at IS NOT NULL AND accepted_at >= ?
                GROUP BY recruiter_id
                ORDER BY total DESC, recruiter_id
                LIMIT ?""",
            (guild_id, recent_since, max(1, int(limit))),
        )
        accepted_total = await db.execute_fetchall(
            """SELECT recruiter_id, COUNT(*) AS total
                 FROM recruiting_referrals
                WHERE guild_id = ? AND status = 'accepted'
                  AND accepted_at IS NOT NULL
                GROUP BY recruiter_id
                ORDER BY total DESC, recruiter_id
                LIMIT ?""",
            (guild_id, max(1, int(limit))),
        )

    row = totals[0] if totals else (0, 0, 0, 0, 0)
    return RecruitingSummary(
        joined=int(row[0] or 0),
        applied=int(row[1] or 0),
        accepted=int(row[2] or 0),
        rejected=int(row[3] or 0),
        pending=int(row[4] or 0),
        per_recruiter=tuple(
            RecruiterTotals(
                int(item[0]), int(item[1] or 0), int(item[2] or 0), int(item[3] or 0)
            )
            for item in per_recruiter
        ),
        accepted_recent=tuple((int(a), int(b)) for a, b in accepted_recent),
        accepted_total=tuple((int(a), int(b)) for a, b in accepted_total),
    )


async def get_referral_decision(
    guild_id: int, member_id: int, application_number: str
) -> tuple[str | None, int | None] | None:
    """Return ``(status, approved_by)`` for one referral.

    The caller used to load every referral row in the guild and scan for a
    single member — an O(history) read on a conflict path that runs on every
    duplicate decision click.
    """

    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT status, approved_by FROM recruiting_referrals
               WHERE guild_id = ? AND member_id = ? AND application_number = ?""",
            (guild_id, member_id, application_number),
        )
    if not rows:
        return None
    status, approved_by = rows[0]
    return (
        str(status) if status is not None else None,
        int(approved_by) if approved_by is not None else None,
    )


async def get_invite_baseline(guild_id: int) -> dict[str, int]:
    """Last observed use count per invite code, or an empty mapping."""

    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT invite_code, last_uses FROM recruiter_invite_uses WHERE guild_id = ?",
            (guild_id,),
        )
    return {str(code): int(uses) for code, uses in rows}


async def save_invite_baseline(guild_id: int, uses: dict[str, int]) -> None:
    """Replace the stored baseline with the counts just observed."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "recruiting-baseline", guild_id)
        try:
            for code, count in uses.items():
                await db.execute(
                    """INSERT INTO recruiter_invite_uses
                       (guild_id, invite_code, last_uses, observed_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(guild_id, invite_code) DO UPDATE SET
                           last_uses = excluded.last_uses,
                           observed_at = excluded.observed_at""",
                    (guild_id, code, int(count), now),
                )
            if uses:
                placeholders = ",".join("?" for _ in uses)
                await db.execute(
                    f"""DELETE FROM recruiter_invite_uses
                        WHERE guild_id = ? AND invite_code NOT IN ({placeholders})""",
                    (guild_id, *uses.keys()),
                )
            else:
                await db.execute(
                    "DELETE FROM recruiter_invite_uses WHERE guild_id = ?",
                    (guild_id,),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def forget_invite(guild_id: int, invite_code: str) -> None:
    async with _connect() as db:
        await begin_write(db, "recruiting-baseline", guild_id)
        try:
            await db.execute(
                "DELETE FROM recruiter_invite_uses WHERE guild_id = ? AND invite_code = ?",
                (guild_id, invite_code),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def has_baseline(guild_id: int) -> bool:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT 1 FROM recruiter_invite_uses WHERE guild_id = ? LIMIT 1",
            (guild_id,),
        )
    return bool(rows)


async def record_unresolved_join(
    guild_id: int,
    member_id: int,
    *,
    reason: str,
    candidates: str = "",
) -> None:
    """Record a join the bot refuses to attribute automatically."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "recruiting-unresolved", guild_id)
        try:
            await db.execute(
                """INSERT OR IGNORE INTO recruiting_unresolved_joins
                   (guild_id, member_id, joined_at, reason, candidates)
                   VALUES (?, ?, ?, ?, ?)""",
                (guild_id, member_id, now, reason, candidates[:500]),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def get_unresolved_joins(
    guild_id: int, *, limit: int = 25
) -> list[UnresolvedJoin]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT id, guild_id, member_id, joined_at, reason, candidates
               FROM recruiting_unresolved_joins
               WHERE guild_id = ? AND resolved_at IS NULL
               ORDER BY joined_at
               LIMIT ?""",
            (guild_id, max(1, int(limit))),
        )
    return [
        UnresolvedJoin(
            int(row[0]), int(row[1]), int(row[2]), str(row[3]), str(row[4]), str(row[5])
        )
        for row in rows
    ]


async def count_unresolved_joins(guild_id: int) -> int:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT COUNT(*) FROM recruiting_unresolved_joins
               WHERE guild_id = ? AND resolved_at IS NULL""",
            (guild_id,),
        )
    return int(rows[0][0]) if rows else 0


async def resolve_join(
    guild_id: int,
    member_id: int,
    *,
    recruiter_id: int | None,
    resolved_by: int,
) -> bool:
    """Close every pending record for a member. ``recruiter_id`` may be None
    when an administrator decides the join simply has no recruiter."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "recruiting-unresolved", guild_id)
        try:
            cursor = await db.execute(
                """UPDATE recruiting_unresolved_joins
                   SET resolved_at = ?, resolved_by = ?, recruiter_id = ?
                   WHERE guild_id = ? AND member_id = ? AND resolved_at IS NULL""",
                (now, resolved_by, recruiter_id, guild_id, member_id),
            )
            changed = max(0, int(cursor.rowcount))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return changed > 0


@dataclass(frozen=True, slots=True)
class ReferralFunnel:
    guild_id: int
    member_id: int
    status: str
    accepted_at: str | None
    verified_at: str | None
    retained_at: str | None


async def get_referral_funnel(
    guild_id: int, member_id: int
) -> ReferralFunnel | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT guild_id, member_id, status, accepted_at,
                      verified_at, retained_at
                 FROM recruiting_referrals
                WHERE guild_id = ? AND member_id = ?""",
            (guild_id, member_id),
        )
    if not rows:
        return None
    row = rows[0]
    return ReferralFunnel(
        int(row[0]),
        int(row[1]),
        str(row[2]),
        str(row[3]) if row[3] is not None else None,
        str(row[4]) if row[4] is not None else None,
        str(row[5]) if row[5] is not None else None,
    )


async def mark_referral_verified(guild_id: int, member_id: int) -> str | None:
    """Record in-game verify on the recruiting funnel. Status CHECK is unchanged."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "recruiting-referral", f"{guild_id}:{member_id}")
        cursor = await db.execute(
            """UPDATE recruiting_referrals
                  SET verified_at = COALESCE(verified_at, ?)
                WHERE guild_id = ? AND member_id = ?""",
            (now, guild_id, member_id),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            return None
        await db.commit()
    funnel = await get_referral_funnel(guild_id, member_id)
    return funnel.verified_at if funnel is not None else now


async def maybe_mark_referral_retained(
    guild_id: int, member_id: int, *, retention_days: int
) -> str | None:
    """Mark retention when accepted_at + days has elapsed. 0 days is unset."""

    if retention_days <= 0:
        return None
    funnel = await get_referral_funnel(guild_id, member_id)
    if funnel is None or funnel.accepted_at is None:
        return None
    if funnel.retained_at is not None:
        return funnel.retained_at
    accepted = datetime.fromisoformat(funnel.accepted_at)
    if accepted.tzinfo is None:
        accepted = accepted.replace(tzinfo=UTC)
    if datetime.now(UTC) < accepted + timedelta(days=retention_days):
        return None
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "recruiting-referral", f"{guild_id}:{member_id}")
        cursor = await db.execute(
            """UPDATE recruiting_referrals
                  SET retained_at = ?
                WHERE guild_id = ? AND member_id = ?
                  AND retained_at IS NULL
                  AND accepted_at IS NOT NULL""",
            (now, guild_id, member_id),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            return None
        await db.commit()
    return now
