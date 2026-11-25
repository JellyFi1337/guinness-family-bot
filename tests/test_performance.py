"""Guards for the throughput work: no global write lock, no busy-work timers.

These are structural and unit tests. They do not prove the bot is fast; they
prove the specific things that made it slow cannot come back unnoticed.
"""

import ast
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from cogs.music import _CircuitBreaker
from cogs.weekly_stats import (
    _MAX_BACKFILL_WEEKS,
    _STATS_TIMESTAMP_QUANTUM_MINUTES,
)
from infrastructure.transactions import _lock_id
from repositories.recruiting import RecruiterTotals, RecruitingSummary


ROOT = Path(__file__).resolve().parents[1]

# The only two places a process-wide write lock is legitimate: a full data
# reset, and the migration runner. Everywhere else it serialises unrelated
# guilds, users and workflows behind one advisory lock.
GLOBAL_LOCK_ALLOWED = {
    ("utils/database.py", "reset_database_data"),
    # SQLite-only branch: the PostgreSQL path returns before reaching it.
    ("repositories/operations.py", "claim_operations"),
    ("infrastructure/migrations.py", None),
    ("infrastructure/postgresql.py", None),
    ("infrastructure/transactions.py", None),
}

# ``"BEGIN" if postgresql else "BEGIN IMMEDIATE"`` takes no advisory lock on
# PostgreSQL, so it is not a global-lock offender. Only the bare literal is.
BARE_GLOBAL_LOCK = 'await db.execute("BEGIN IMMEDIATE")'


class GlobalWriteLockTests(unittest.TestCase):
    def test_no_new_global_write_locks(self):
        """``BEGIN IMMEDIATE`` maps to one shared PostgreSQL advisory lock."""

        offenders: list[str] = []
        for path in sorted(ROOT.rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative.startswith((".venv", "tests")) or "__pycache__" in relative:
                continue
            source = path.read_text(encoding="utf-8")
            if BARE_GLOBAL_LOCK not in source:
                continue
            if (relative, None) in GLOBAL_LOCK_ALLOWED:
                continue
            tree = ast.parse(source, filename=relative)
            lines = source.split("\n")
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                body = "\n".join(lines[node.lineno - 1 : (node.end_lineno or node.lineno)])
                if BARE_GLOBAL_LOCK not in body:
                    continue
                if (relative, node.name) in GLOBAL_LOCK_ALLOWED:
                    continue
                offenders.append(f"{relative}:{node.lineno} {node.name}")
        self.assertEqual(
            offenders,
            [],
            "Используйте begin_write(db, namespace, key) вместо глобальной "
            "блокировки:\n" + "\n".join(offenders),
        )

    def test_lock_ids_are_stable_and_separate_aggregates(self):
        self.assertEqual(_lock_id("coins", 7), _lock_id("coins", 7))
        self.assertNotEqual(_lock_id("coins", 7), _lock_id("coins", 8))
        self.assertNotEqual(_lock_id("coins", 7), _lock_id("summit", 7))
        # PostgreSQL advisory locks take a signed 64-bit key.
        self.assertTrue(-(2**63) <= _lock_id("coins", 7) < 2**63)

    def test_lock_ids_spread_across_the_keyspace(self):
        """A weak hash would collide and silently re-serialise aggregates."""

        ids = {_lock_id("coins", user) for user in range(2_000)}
        self.assertEqual(len(ids), 2_000)


class MaintenanceTimerTests(unittest.TestCase):
    def test_analyze_is_not_on_the_five_minute_path(self):
        source = (ROOT / "infrastructure" / "postgresql.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        maintain = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "maintain"
        )
        # Skip the docstring: it explains what the method deliberately stopped
        # doing, and matching that text is not the same as matching a call.
        statements = [
            node
            for node in maintain.body
            if not (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            )
        ]
        body = "\n".join(ast.unparse(node) for node in statements)
        self.assertNotIn("ANALYZE", body.upper())

    def test_retention_is_a_separate_task_from_the_fast_tick(self):
        source = (ROOT / "cogs" / "weekly_stats.py").read_text(encoding="utf-8")
        self.assertIn('"stats-retention"', source)
        tree = ast.parse(source)
        tick = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "stats_maintenance"
        )
        body = "\n".join(source.split("\n")[tick.lineno - 1 : (tick.end_lineno or 0)])
        for expensive in ("aggregate_and_prune_stats", "prune_economy_activity", "checkpoint_db"):
            self.assertNotIn(expensive, body)


class TimestampQuantisationTests(unittest.TestCase):
    @staticmethod
    def _quantise(moment: datetime) -> datetime:
        return moment.replace(
            minute=(moment.minute // _STATS_TIMESTAMP_QUANTUM_MINUTES)
            * _STATS_TIMESTAMP_QUANTUM_MINUTES,
            second=0,
            microsecond=0,
        )

    def test_two_builds_inside_one_interval_are_identical(self):
        """Otherwise the weekly message is edited every five minutes forever."""

        first = datetime(2026, 7, 28, 14, 6, 1, tzinfo=UTC)
        second = datetime(2026, 7, 28, 14, 9, 59, tzinfo=UTC)
        self.assertEqual(self._quantise(first), self._quantise(second))

    def test_the_next_interval_differs(self):
        first = datetime(2026, 7, 28, 14, 9, 59, tzinfo=UTC)
        second = datetime(2026, 7, 28, 14, 10, 0, tzinfo=UTC)
        self.assertNotEqual(self._quantise(first), self._quantise(second))


class WeekBackfillTests(unittest.TestCase):
    @staticmethod
    def _missed(stored: date, current: date) -> list[date]:
        missed: list[date] = []
        cursor = stored
        while cursor < current and len(missed) < _MAX_BACKFILL_WEEKS:
            missed.append(cursor)
            cursor = cursor + timedelta(days=7)
        return missed

    def test_every_missed_week_is_finalised_not_just_the_stored_one(self):
        stored = date(2026, 6, 1)
        current = stored + timedelta(days=28)
        self.assertEqual(
            self._missed(stored, current),
            [stored, stored + timedelta(days=7), stored + timedelta(days=14),
             stored + timedelta(days=21)],
        )

    def test_a_single_missed_week_still_works(self):
        stored = date(2026, 6, 1)
        self.assertEqual(self._missed(stored, stored + timedelta(days=7)), [stored])

    def test_backfill_is_capped(self):
        stored = date(2020, 1, 6)
        current = date(2026, 7, 27)
        self.assertEqual(len(self._missed(stored, current)), _MAX_BACKFILL_WEEKS)


class CircuitBreakerTests(unittest.TestCase):
    def test_opens_after_the_threshold_and_is_per_host(self):
        breaker = _CircuitBreaker(threshold=3, cooldown=60.0)
        for _ in range(3):
            breaker.record_failure("youtube.com")
        self.assertTrue(breaker.is_open("youtube.com"))
        self.assertFalse(breaker.is_open("soundcloud.com"))

    def test_success_closes_it(self):
        breaker = _CircuitBreaker(threshold=2, cooldown=60.0)
        breaker.record_failure("youtube.com")
        breaker.record_success("youtube.com")
        breaker.record_failure("youtube.com")
        self.assertFalse(breaker.is_open("youtube.com"))

    def test_half_opens_after_the_cooldown(self):
        # Deterministic clock: avoids full-suite flake of real sleep without
        # softening the open / half-open boundaries. Base at 0.0 so the
        # cooldown comparison is exact in IEEE floats (1000.05 - 1000.0 != 0.05).
        now = 0.0

        def clock() -> float:
            return now

        breaker = _CircuitBreaker(threshold=2, cooldown=0.05, clock=clock)
        breaker.record_failure("youtube.com")
        breaker.record_failure("youtube.com")
        self.assertTrue(breaker.is_open("youtube.com"))
        # Still within cooldown: remains open.
        now = 0.049
        self.assertTrue(breaker.is_open("youtube.com"))
        # At/after cooldown: half-open (probe allowed).
        now = 0.05
        self.assertFalse(breaker.is_open("youtube.com"))
        # A failed probe must reopen immediately rather than spend the whole
        # threshold again.
        breaker.record_failure("youtube.com")
        self.assertTrue(breaker.is_open("youtube.com"))


class RecruitingDigestTests(unittest.TestCase):
    @staticmethod
    def _summary(accepted: int = 3) -> RecruitingSummary:
        return RecruitingSummary(
            joined=10,
            applied=6,
            accepted=accepted,
            rejected=1,
            pending=2,
            per_recruiter=(RecruiterTotals(1, 10, 6, accepted),),
            accepted_recent=((1, accepted),),
            accepted_total=((1, accepted),),
        )

    def test_identical_data_produces_an_identical_digest(self):
        self.assertEqual(self._summary().digest(), self._summary().digest())

    def test_changed_data_produces_a_different_digest(self):
        self.assertNotEqual(self._summary(3).digest(), self._summary(4).digest())

    def test_conversion_handles_an_empty_funnel(self):
        empty = RecruitingSummary(0, 0, 0, 0, 0, (), (), ())
        self.assertEqual(empty.conversion, 0.0)


if __name__ == "__main__":
    unittest.main()
