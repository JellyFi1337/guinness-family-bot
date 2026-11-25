"""Reliability rules introduced to stop half-completed operations.

Three failure modes are covered here: a cooldown burned by an early return, a
join credited to the wrong recruiter after a restart, and the assumption that
"no exception escaped" means "the operation completed".
"""

import unittest

from repositories.recruiting import (
    REASON_AMBIGUOUS,
    REASON_MULTIPLE_DELTA,
    REASON_NO_BASELINE,
    REASON_UNKNOWN_INVITE,
    decide_attribution,
)
from services.cooldowns import CooldownResult, command_cooldowns, cooldown_claim
from ui.results import ResultKind, completed, failed, queued, unchanged


class AttributionTests(unittest.TestCase):
    """The rules that decide whether a join may be credited at all."""

    def test_single_managed_invite_incremented_by_one_is_attributed(self):
        decision = decide_attribution(
            baseline={"aaa": 4, "bbb": 9},
            observed={"aaa": 5, "bbb": 9},
            managed_codes={"aaa", "bbb"},
            baseline_ready=True,
        )
        self.assertTrue(decision.resolved)
        self.assertEqual(decision.invite_code, "aaa")

    def test_empty_baseline_after_restart_is_never_attributed(self):
        """The old code credited the invite with the highest lifetime count."""

        decision = decide_attribution(
            baseline={},
            observed={"aaa": 1, "bbb": 400},
            managed_codes={"aaa", "bbb"},
            baseline_ready=False,
        )
        self.assertFalse(decision.resolved)
        self.assertTrue(decision.needs_review)
        self.assertEqual(decision.reason, REASON_NO_BASELINE)

    def test_two_managed_invites_moving_together_is_ambiguous(self):
        decision = decide_attribution(
            baseline={"aaa": 1, "bbb": 1},
            observed={"aaa": 2, "bbb": 2},
            managed_codes={"aaa", "bbb"},
            baseline_ready=True,
        )
        self.assertEqual(decision.reason, REASON_AMBIGUOUS)
        self.assertEqual(set(decision.candidates), {"aaa", "bbb"})

    def test_delta_greater_than_one_is_not_attributed(self):
        decision = decide_attribution(
            baseline={"aaa": 1},
            observed={"aaa": 4},
            managed_codes={"aaa"},
            baseline_ready=True,
        )
        self.assertEqual(decision.reason, REASON_MULTIPLE_DELTA)
        self.assertIn("aaa (+3)", decision.candidates)

    def test_unmanaged_invite_is_flagged_not_credited(self):
        decision = decide_attribution(
            baseline={"aaa": 1, "public": 10},
            observed={"aaa": 1, "public": 11},
            managed_codes={"aaa"},
            baseline_ready=True,
        )
        self.assertEqual(decision.reason, REASON_UNKNOWN_INVITE)
        self.assertEqual(decision.candidates, ("public",))

    def test_nothing_moved_produces_neither_credit_nor_noise(self):
        """A vanity URL or widget join is not an unresolved case to triage."""

        decision = decide_attribution(
            baseline={"aaa": 3},
            observed={"aaa": 3},
            managed_codes={"aaa"},
            baseline_ready=True,
        )
        self.assertFalse(decision.resolved)
        self.assertFalse(decision.needs_review)

    def test_a_brand_new_invite_counts_as_movement(self):
        decision = decide_attribution(
            baseline={"aaa": 3},
            observed={"aaa": 3, "new": 1},
            managed_codes={"aaa", "new"},
            baseline_ready=True,
        )
        self.assertEqual(decision.invite_code, "new")


class CooldownClaimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        command_cooldowns.reset_for_tests()
        command_cooldowns._durations[(1, "summit_vzp")] = 30

    async def test_early_return_releases_the_slot(self):
        """A missing channel must not cost the user 30 seconds."""

        async with cooldown_claim(1, "summit_vzp", 42) as claim:
            self.assertTrue(claim.allowed)
            # Simulates `if not channel: return` — keep() is never reached.
        second = await command_cooldowns.acquire(1, "summit_vzp", 42)
        self.assertTrue(second.allowed)

    async def test_kept_claim_consumes_the_slot(self):
        async with cooldown_claim(1, "summit_vzp", 43) as claim:
            self.assertTrue(claim.allowed)
            claim.keep()
        second = await command_cooldowns.acquire(1, "summit_vzp", 43)
        self.assertFalse(second.allowed)
        self.assertGreater(second.retry_after, 0)

    async def test_exception_releases_the_slot(self):
        with self.assertRaises(RuntimeError):
            async with cooldown_claim(1, "summit_vzp", 44) as claim:
                self.assertTrue(claim.allowed)
                raise RuntimeError("Discord failed")
        second = await command_cooldowns.acquire(1, "summit_vzp", 44)
        self.assertTrue(second.allowed)

    async def test_exception_after_keep_does_not_return_the_slot(self):
        with self.assertRaises(RuntimeError):
            async with cooldown_claim(1, "summit_vzp", 45) as claim:
                claim.keep()
                raise RuntimeError("failed after the durable write")
        second = await command_cooldowns.acquire(1, "summit_vzp", 45)
        self.assertFalse(second.allowed)

    async def test_blocked_claim_reports_retry_after(self):
        await command_cooldowns.acquire(1, "summit_vzp", 46)
        async with cooldown_claim(1, "summit_vzp", 46) as claim:
            self.assertFalse(claim.allowed)
            self.assertGreater(claim.retry_after, 0)

    async def test_bypass_never_holds_a_slot(self):
        async with cooldown_claim(1, "summit_vzp", 47, bypass=True) as claim:
            self.assertTrue(claim.allowed)
        result = await command_cooldowns.acquire(1, "summit_vzp", 47)
        self.assertIsInstance(result, CooldownResult)
        self.assertTrue(result.allowed)


class ResultKindTests(unittest.TestCase):
    def test_queued_is_distinct_from_completed_and_failed(self):
        """"Сохранено, панель обновляется" is not "не выполнено"."""

        self.assertEqual(
            queued(code="X.Q", title="t", summary="s").tone.name, "INFO"
        )
        self.assertEqual(
            completed(code="X.C", title="t", summary="s").tone.name, "SUCCESS"
        )
        self.assertEqual(
            failed(code="X.F", title="t", summary="s").tone.name, "ERROR"
        )
        self.assertEqual(
            unchanged(code="X.U", title="t", summary="s").tone.name, "WARNING"
        )

    def test_every_result_carries_a_code_and_a_next_step(self):
        for kind in ResultKind:
            spec = completed(code="X.Y", title="t", summary="s")
            self.assertTrue(spec.code)
            self.assertTrue(spec.next_step)
            self.assertTrue(kind.default_next_step)

    def test_explicit_next_step_overrides_the_default(self):
        spec = queued(
            code="CAR.BOOKED",
            title="Забронировано",
            summary="s",
            next_step="Панель обновится через минуту.",
        )
        self.assertEqual(spec.next_step, "Панель обновится через минуту.")


if __name__ == "__main__":
    unittest.main()
