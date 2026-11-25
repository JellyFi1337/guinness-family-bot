"""Concurrent mixed-workload load test with the audit's latency thresholds.

The audit's release gate asks for "50 параллельных тикетов + Coins + автопарк,
db_acquire p95 < 50 мс". That number is the whole point of removing the global
write lock: before, every unrelated write queued behind one advisory lock, so
concurrency made latency worse rather than throughput better.

This drives the three busiest write paths at once and fails if the measured
percentiles miss the targets, so the claim is checked rather than asserted.

    python scripts/load_test.py --postgres-url postgresql://…/guinness_test
    python scripts/load_test.py --sqlite            # local smoke run

Exit code is non-zero when a threshold is missed.
"""

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

# Release criteria from the audit.
DB_ACQUIRE_P95_MS = 50.0
TRANSACTION_P95_MS = 100.0

GUILD_ID = 1
_CAR_COUNT = 25


def _event_loop() -> asyncio.AbstractEventLoop:
    if os.name == "nt":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def _validate_test_url(url: str) -> None:
    parsed = urlsplit(url.replace("postgresql+psycopg://", "postgresql://", 1))
    if not url or "test" not in parsed.path.strip("/").casefold():
        raise SystemExit("A dedicated database whose name contains 'test' is required.")


async def _ticket_flow(index: int) -> None:
    """Provision and close a ticket, the way the cog does."""

    from repositories.tickets import (
        activate_ticket,
        attach_ticket_channel,
        begin_ticket,
        begin_ticket_close,
        complete_ticket_close,
    )

    interaction_id = 10_000_000 + index
    channel_id = 20_000_000 + index
    await begin_ticket(
        guild_id=GUILD_ID,
        interaction_id=interaction_id,
        ticket_type="жалоба",
        number=f"load-{index:05d}",
        author_id=1_000 + index,
    )
    await attach_ticket_channel(interaction_id, channel_id)
    await activate_ticket(interaction_id, channel_id + 1)
    await begin_ticket_close(
        guild_id=GUILD_ID,
        channel_id=channel_id,
        number=f"load-{index:05d}",
        author_id=1_000 + index,
        ticket_type="жалоба",
        closed_by=1,
    )
    await complete_ticket_close(channel_id)


async def _coins_flow(index: int) -> None:
    """Credit and spend, which is a read-modify-write on one account."""

    from utils import database

    user_id = 500_000 + index
    await database.adjust_coins(user_id, 1_000, "load_test")
    await database.spend_coins(user_id, 250, "load_test")
    await database.transfer_coins(user_id, user_id + 1, 100)


async def _car_flow(index: int) -> None:
    """Book and release a car — contention on one row per car."""

    from utils import database

    car_id = (index % _CAR_COUNT) + 1
    now = "2026-07-28T12:00:00+00:00"
    later = "2026-07-28T18:00:00+00:00"
    result, booking = await database.create_car_booking(
        car_id, 700_000 + index, f"IC {index}", now, later
    )
    if result == "booked" and booking:
        await database.force_cancel_car_booking(int(booking[0]), 1)


async def _prepare() -> None:
    from utils import database

    await database.reset_database_data()
    for index in range(1, _CAR_COUNT + 1):
        await database.add_car(f"Car {index}", f"PLATE{index:03d}", "Family", 1)


def _percentiles(name: str) -> dict[str, float]:
    from services.metrics import metrics

    histograms = metrics.snapshot()["histograms"]
    assert isinstance(histograms, dict)
    histogram = histograms.get(name) or {}
    return {
        "count": float(histogram.get("count", 0)),
        "p50": float(histogram.get("p50", 0.0)),
        "p95": float(histogram.get("p95", 0.0)),
        "p99": float(histogram.get("p99", 0.0)),
    }


async def _run(concurrency: int, rounds: int) -> dict[str, object]:
    from infrastructure.database import database_backend
    from utils import database

    await _prepare()
    backend = database_backend()

    started = time.perf_counter()
    for round_index in range(rounds):
        offset = round_index * concurrency
        # All three workloads run at once on purpose: the failure mode being
        # tested is unrelated writes blocking each other, which a single-flow
        # benchmark cannot see.
        await asyncio.gather(
            *(_ticket_flow(offset + i) for i in range(concurrency)),
            *(_coins_flow(offset + i) for i in range(concurrency)),
            *(_car_flow(offset + i) for i in range(concurrency)),
        )
    elapsed = time.perf_counter() - started

    operations = rounds * concurrency * 3
    pool = database.get_database_pool_status()
    return {
        "backend": backend,
        "concurrency": concurrency,
        "rounds": rounds,
        "operations": operations,
        "wall_seconds": round(elapsed, 3),
        "operations_per_second": round(operations / elapsed, 1) if elapsed else 0.0,
        "pool": pool,
        "db_acquire_ms": _percentiles(f"database.{backend}.acquire_ms"),
        "db_transaction_ms": _percentiles(f"database.{backend}.connection_ms"),
    }


def _evaluate(report: dict[str, object]) -> list[str]:
    failures: list[str] = []
    acquire = report["db_acquire_ms"]
    transaction = report["db_transaction_ms"]
    assert isinstance(acquire, dict) and isinstance(transaction, dict)
    if acquire["p95"] > DB_ACQUIRE_P95_MS:
        failures.append(
            f"db_acquire p95 = {acquire['p95']:.1f} ms > {DB_ACQUIRE_P95_MS:.0f} ms"
        )
    if transaction["p95"] > TRANSACTION_P95_MS:
        failures.append(
            f"transaction p95 = {transaction['p95']:.1f} ms > {TRANSACTION_P95_MS:.0f} ms"
        )
    return failures


async def _main(url: str, *, use_sqlite: bool, concurrency: int, rounds: int) -> int:
    from utils import database

    if use_sqlite:
        with tempfile.TemporaryDirectory() as directory:
            database.DATABASE_URL = ""
            database.DB_PATH = Path(directory) / "load.db"
            try:
                await database.init_db(persistent=True)
                report = await _run(concurrency, rounds)
            finally:
                await database.close_db()
    else:
        database.DATABASE_URL = url
        try:
            await database.init_db(persistent=True)
            report = await _run(concurrency, rounds)
        finally:
            await database.close_db()

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    failures = _evaluate(report)
    if failures:
        print("\nПороги не выдержаны:")
        for failure in failures:
            print(f"  - {failure}")
        if report["backend"] == "sqlite":
            print(
                "  (SQLite сериализует запись по своей природе — "
                "критерии релиза измеряются на PostgreSQL)"
            )
            return 0
        return 1
    print("\nПороги выдержаны.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-url", default=os.getenv("POSTGRES_TEST_URL", ""))
    parser.add_argument(
        "--sqlite", action="store_true", help="локальный прогон без PostgreSQL"
    )
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=4)
    args = parser.parse_args()

    if not args.sqlite:
        _validate_test_url(args.postgres_url)
    os.environ["DATABASE_URL"] = ""
    os.environ["DATABASE_REQUIRE_POSTGRES"] = "false"
    with asyncio.Runner(loop_factory=_event_loop) as runner:
        return runner.run(
            _main(
                args.postgres_url,
                use_sqlite=args.sqlite,
                concurrency=max(1, args.concurrency),
                rounds=max(1, args.rounds),
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
