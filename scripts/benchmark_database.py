from __future__ import annotations

import argparse
import asyncio
import json
import os
import selectors
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _event_loop() -> asyncio.AbstractEventLoop:
    if os.name == "nt":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare representative Guinness Bot SQLite/PostgreSQL workloads."
    )
    parser.add_argument(
        "--postgres-url",
        default=os.getenv("POSTGRES_TEST_URL", ""),
        help="Dedicated PostgreSQL test database URL.",
    )
    parser.add_argument("--iterations", type=int, default=100)
    return parser.parse_args()


def _validate_test_url(url: str) -> None:
    parsed = urlsplit(url.replace("postgresql+psycopg://", "postgresql://", 1))
    if not url or "test" not in parsed.path.strip("/").casefold():
        raise SystemExit("A dedicated database whose name contains 'test' is required.")


async def _timed(coroutine) -> float:
    started = time.perf_counter()
    await coroutine
    return time.perf_counter() - started


async def _run_workload(iterations: int) -> dict[str, float]:
    from utils import database

    await database.reset_database_data()
    counter_operations = min(iterations, 25)

    async def settings_roundtrip() -> None:
        for index in range(iterations):
            await database.set_setting("benchmark", str(index))
            assert await database.get_setting("benchmark") == str(index)

    async def concurrent_counters() -> None:
        values = await asyncio.gather(
            *(
                database.get_next_number("BENCH")
                for _ in range(counter_operations)
            )
        )
        assert len(values) == len(set(values))

    rows = [
        (
            1_000_000 + index,
            1,
            2,
            "benchmark",
            10 + index % 20,
            f"2026-07-27T12:{index % 60:02d}:{index % 60:02d}+00:00",
        )
        for index in range(iterations)
    ]

    async def stats_batch() -> None:
        await database.record_stats_messages_batch(
            rows,
            [row[0] for row in rows[::20]],
        )

    return {
        "counter_operations": float(counter_operations),
        "settings_roundtrip_s": await _timed(settings_roundtrip()),
        "concurrent_counters_s": await _timed(concurrent_counters()),
        "stats_batch_s": await _timed(stats_batch()),
    }


async def _benchmark(postgres_url: str, iterations: int) -> dict[str, object]:
    from utils import database

    results: dict[str, object] = {"iterations": iterations}
    with tempfile.TemporaryDirectory() as directory:
        database.DATABASE_URL = ""
        database.DB_PATH = Path(directory) / "benchmark.db"
        try:
            await database.init_db(persistent=True)
            results["sqlite"] = await _run_workload(iterations)
        finally:
            await database.close_db()

    database.DATABASE_URL = postgres_url
    try:
        await database.init_db(persistent=True)
        results["postgresql"] = await _run_workload(iterations)
    finally:
        await database.close_db()

    sqlite = results["sqlite"]
    postgres = results["postgresql"]
    assert isinstance(sqlite, dict) and isinstance(postgres, dict)
    results["postgresql_vs_sqlite_ratio"] = {
        key.removesuffix("_s"): float(postgres[key]) / float(sqlite[key])
        for key in sqlite
        if key.endswith("_s")
    }
    return results


def main() -> None:
    args = _arguments()
    _validate_test_url(args.postgres_url)
    # Prevent the user's regular .env database from being selected on import.
    os.environ["DATABASE_URL"] = ""
    os.environ["DATABASE_REQUIRE_POSTGRES"] = "false"
    with asyncio.Runner(loop_factory=_event_loop) as runner:
        result = runner.run(
            _benchmark(args.postgres_url, max(10, args.iterations))
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
