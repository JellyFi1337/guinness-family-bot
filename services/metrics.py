from __future__ import annotations

import math
import threading
from collections import defaultdict, deque


class MetricsRegistry:
    def __init__(self, *, sample_size: int = 2_000) -> None:
        self._sample_size = max(100, int(sample_size))
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._sample_size)
        )

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += int(value)

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._samples[name].append(float(value))

    def restore_counters(self, counters: dict[str, int]) -> None:
        """Seed cumulative counters recorded before the last restart.

        Only counters are restored. Gauges describe the present moment and
        histograms describe a rolling window; carrying either across a restart
        would report state that no longer exists.
        """

        with self._lock:
            for name, value in counters.items():
                self._counters[name] = max(self._counters[name], int(value))

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float:
        if not values:
            return 0.0
        values.sort()
        index = max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))
        return values[index]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            samples = {name: list(values) for name, values in self._samples.items()}
        histograms = {
            name: {
                "count": len(values),
                "p50": self._percentile(values.copy(), 0.50),
                "p95": self._percentile(values.copy(), 0.95),
                "p99": self._percentile(values.copy(), 0.99),
            }
            for name, values in samples.items()
        }
        return {
            "counters": counters,
            "gauges": gauges,
            "histograms": histograms,
        }


metrics = MetricsRegistry()
