"""Alerting rules, presence economy and the widened lint gate.

The health data always existed; nobody was told about it. These tests cover the
rules that decide when the owner is worth waking, and the two hygiene gates that
keep the previous sprints from silently regressing.
"""

import tomllib
import unittest
from pathlib import Path

from services.alerts import (
    DB_ACQUIRE_P95_LIMIT_MS,
    GATEWAY_RECONNECT_LIMIT,
    MUSIC_EXTRACT_ERROR_LIMIT,
    TASK_FAILURE_THRESHOLD,
    Alert,
    OwnerAlerts,
)
from services.metrics import MetricsRegistry


ROOT = Path(__file__).resolve().parents[1]

HEALTHY = {
    "tasks": [("stats-ingest", "healthy", 0)],
    "outbox_dead": 0,
    "stats_dropped": 0,
    "db_acquire_p95_ms": 5.0,
    "gateway_reconnects": 0,
    "music_extract_errors": 0,
}


class _RecordingAlerts(OwnerAlerts):
    """Captures deliveries instead of touching Discord."""

    def __init__(self) -> None:
        super().__init__(bot=None)
        self.delivered: list[str] = []

    async def _deliver(self, spec) -> bool:
        self.delivered.append(spec.title)
        return True


class AlertRuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_healthy_snapshot_raises_nothing(self):
        alerts = await _RecordingAlerts().evaluate(dict(HEALTHY))
        self.assertEqual(alerts, [])

    async def test_one_failure_is_not_an_incident(self):
        snapshot = dict(HEALTHY, tasks=[("stats-ingest", "degraded", 1)])
        self.assertEqual(await _RecordingAlerts().evaluate(snapshot), [])

    async def test_repeated_task_failure_alerts(self):
        snapshot = dict(
            HEALTHY, tasks=[("stats-ingest", "degraded", TASK_FAILURE_THRESHOLD)]
        )
        alerts = await _RecordingAlerts().evaluate(snapshot)
        self.assertEqual([alert.key for alert in alerts], ["task:stats-ingest"])

    async def test_dead_letter_alerts_immediately(self):
        alerts = await _RecordingAlerts().evaluate(dict(HEALTHY, outbox_dead=1))
        self.assertEqual([alert.key for alert in alerts], ["outbox:dead"])

    async def test_dropped_statistics_alert(self):
        alerts = await _RecordingAlerts().evaluate(dict(HEALTHY, stats_dropped=3))
        self.assertEqual([alert.key for alert in alerts], ["stats:dropped"])

    async def test_slow_database_alerts_only_past_the_threshold(self):
        under = dict(HEALTHY, db_acquire_p95_ms=DB_ACQUIRE_P95_LIMIT_MS)
        over = dict(HEALTHY, db_acquire_p95_ms=DB_ACQUIRE_P95_LIMIT_MS + 1)
        self.assertEqual(await _RecordingAlerts().evaluate(under), [])
        self.assertEqual(
            [alert.key for alert in await _RecordingAlerts().evaluate(over)],
            ["db:acquire"],
        )

    async def test_gateway_and_music_thresholds(self):
        alerts = await _RecordingAlerts().evaluate(
            dict(
                HEALTHY,
                gateway_reconnects=GATEWAY_RECONNECT_LIMIT,
                music_extract_errors=MUSIC_EXTRACT_ERROR_LIMIT,
            )
        )
        self.assertEqual(
            {alert.key for alert in alerts}, {"gateway:reconnects", "music:extract"}
        )

    async def test_a_persistent_fault_is_not_repeated_every_pass(self):
        alerts = _RecordingAlerts()
        alert = Alert("outbox:dead", "t", "s", "d", "n")
        await alerts.raise_alert(alert)
        await alerts.raise_alert(alert)
        await alerts.raise_alert(alert)
        self.assertEqual(len(alerts.delivered), 1)

    async def test_recovery_is_announced_once(self):
        alerts = _RecordingAlerts()
        await alerts.raise_alert(Alert("outbox:dead", "t", "s", "d", "n"))
        await alerts.clear_alert("outbox:dead")
        await alerts.clear_alert("outbox:dead")
        self.assertEqual(len(alerts.delivered), 2)

    async def test_clearing_an_inactive_alert_says_nothing(self):
        alerts = _RecordingAlerts()
        await alerts.clear_alert("outbox:dead")
        self.assertEqual(alerts.delivered, [])


class MetricsRestoreTests(unittest.TestCase):
    def test_counters_are_restored_without_going_backwards(self):
        registry = MetricsRegistry()
        registry.increment("outbox.processed", 5)
        registry.restore_counters({"outbox.processed": 100, "alerts.sent": 2})
        snapshot = registry.snapshot()
        self.assertEqual(snapshot["counters"]["outbox.processed"], 100)
        self.assertEqual(snapshot["counters"]["alerts.sent"], 2)

    def test_restore_never_lowers_a_live_counter(self):
        """A restart mid-flight must not erase work done since startup."""

        registry = MetricsRegistry()
        registry.increment("outbox.processed", 500)
        registry.restore_counters({"outbox.processed": 10})
        self.assertEqual(registry.snapshot()["counters"]["outbox.processed"], 500)

    def test_gauges_and_histograms_are_not_restored(self):
        registry = MetricsRegistry()
        registry.restore_counters({"x": 1})
        snapshot = registry.snapshot()
        self.assertEqual(snapshot["gauges"], {})
        self.assertEqual(snapshot["histograms"], {})


class LintGateTests(unittest.TestCase):
    def test_the_ruff_gate_covers_more_than_syntax_errors(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        selected = set(config["tool"]["ruff"]["lint"]["select"])
        for family in ("F", "B", "ASYNC", "C4"):
            self.assertIn(family, selected)

    def test_presence_runs_under_the_task_supervisor(self):
        source = (ROOT / "core" / "bot.py").read_text(encoding="utf-8")
        self.assertIn('"presence-refresh"', source)
        self.assertIn("self.presence_manager.tick", source)

    def test_alerts_and_metric_persistence_are_scheduled(self):
        source = (ROOT / "core" / "bot.py").read_text(encoding="utf-8")
        self.assertIn('"owner-alerts"', source)
        self.assertIn('"metrics-persist"', source)

    def test_acknowledgement_latency_is_measured_separately(self):
        source = (ROOT / "ui" / "messages.py").read_text(encoding="utf-8")
        self.assertIn("interaction_ack_ms", source)
        self.assertIn("interaction_complete_ms", source)


if __name__ == "__main__":
    unittest.main()
