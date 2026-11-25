from __future__ import annotations

import argparse
import asyncio
import os
import selectors
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Destructive integration check for an isolated PostgreSQL database."
    )
    parser.add_argument(
        "--url",
        default=os.getenv("POSTGRES_TEST_URL", ""),
        help="Dedicated test database URL (or set POSTGRES_TEST_URL).",
    )
    return parser.parse_args()


def _validate_test_url(url: str) -> None:
    if not url:
        raise SystemExit("--url or POSTGRES_TEST_URL is required")
    parsed = urlsplit(url.replace("postgresql+psycopg://", "postgresql://", 1))
    database = parsed.path.strip("/").casefold()
    if "test" not in database:
        raise SystemExit(
            "Refusing destructive checks: the database name must contain 'test'."
        )


def _event_loop() -> asyncio.AbstractEventLoop:
    if os.name == "nt":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


async def _check() -> None:
    from infrastructure.database import (
        DATABASE_INTEGRITY_ERRORS,
        database_backend,
    )
    from infrastructure.migrations import MIGRATIONS
    from infrastructure.postgresql import assert_identity_registry_matches_schema
    from utils import database

    await database.init_db(persistent=True)
    assert database_backend() == "postgresql"
    await database.reset_database_data()

    async with database._connect() as connection:
        row = await connection.execute_fetchall(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        )
        await assert_identity_registry_matches_schema(connection)
    assert int(row[0][0]) == MIGRATIONS[-1].version

    numbers = await asyncio.gather(
        *(database.get_next_number("PG") for _ in range(25))
    )
    assert len(set(numbers)) == 25

    first = await database.add_treasury_transaction(
        42,
        "integration",
        500,
        "add",
        "postgresql check",
        guild_id=100,
        interaction_id=9_001,
    )
    duplicate = await database.add_treasury_transaction(
        42,
        "integration",
        500,
        "add",
        "postgresql check",
        guild_id=100,
        interaction_id=9_001,
    )
    assert (first, duplicate) == (1, 1)
    assert await database.get_treasury_balance() == 500

    await database.log_request("PG-0001", "role", 42, "request")
    claims = await asyncio.gather(
        *(database.claim_request_review("PG-0001", reviewer) for reviewer in range(20))
    )
    assert sum(claims) == 1

    await database.adjust_coins(7, 1_000, "postgresql_check")
    request_id = await database.purchase_role_request(
        7,
        "PostgreSQL",
        0x123456,
        None,
        None,
        400,
    )
    assert request_id is not None
    assert (await database.get_coin_account(7))[0] == 600

    car_id = await database.add_car("Test", "PG001", "Integration", 7)
    assert car_id > 0
    try:
        await database.add_car("Duplicate", "pg001", "Integration", 7)
    except DATABASE_INTEGRITY_ERRORS:
        pass
    else:
        raise AssertionError("case-normalised car plate uniqueness was not enforced")

    await database.record_stats_messages_batch(
        [
            (
                10_000 + index,
                100,
                200,
                "postgresql",
                300 + index % 5,
                f"2026-07-27T12:{index % 60:02d}:00+00:00",
            )
            for index in range(100)
        ],
        [10_001, 10_003],
    )
    stats = await database.get_weekly_stats_data(
        100,
        "2026-07-27T00:00:00+00:00",
        "2026-07-28T00:00:00+00:00",
    )
    assert stats["message_summary"] == (100, 5, 2)

    await database.reset_database_data()
    assert await database.get_treasury_balance() == 0
    assert await database.get_next_number("AB") == "AB-0001"

    # Reuse the established backend-independent repository scenarios against
    # PostgreSQL. The one excluded case constructs a deliberately old SQLite
    # file and therefore cannot describe PostgreSQL behaviour.
    from test_database import DatabaseTests
    from test_idempotency import IdempotencyTests
    from test_memphis_workflows import MemphisWorkflowTests

    excluded = {"test_legacy_summit_schema_is_migrated_before_index_creation"}
    scenarios = sorted(
        name
        for name in dir(DatabaseTests)
        if name.startswith("test_") and name not in excluded
    )
    for name in scenarios:
        await database.reset_database_data()
        case = DatabaseTests(methodName=name)
        await getattr(case, name)()

    for cls in (MemphisWorkflowTests, IdempotencyTests):
        methods = sorted(
            name for name in dir(cls) if name.startswith("test_")
        )
        for name in methods:
            case = cls(methodName=name)
            await case.asyncSetUp()
            try:
                await getattr(case, name)()
            finally:
                await case.asyncTearDown()

    await _check_upgrade_from_version_32(database)
    await _check_repeated_init_preserves_checksums(database)

    await database.close_db()


async def _wipe_public_schema(database: Any) -> None:
    async with database._connect() as connection:
        await connection.execute("DROP SCHEMA public CASCADE")
        await connection.execute("CREATE SCHEMA public")
        await connection.commit()
    database._INITIALIZED_PATH = None
    await database.close_db()


async def _check_upgrade_from_version_32(database: Any) -> None:
    """Fresh schema stopped at migration 32, then upgrade to the current head."""

    import infrastructure.migrations as migrations_mod
    from infrastructure.postgresql import assert_identity_registry_matches_schema

    original = migrations_mod.MIGRATIONS
    head = original[-1].version
    if head < 32:
        raise AssertionError("expected at least 52 migrations in the tree")

    await _wipe_public_schema(database)
    migrations_mod.MIGRATIONS = original[:32]
    try:
        await database.init_db(persistent=True)
        async with database._connect() as connection:
            row = await connection.execute_fetchall(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            )
            assert int(row[0][0]) == 32
            # Synthetic historical row that must survive 33–52.
            await connection.execute(
                """INSERT INTO guild_settings (guild_id, key, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at""",
                (
                    9001,
                    "r04.upgrade_probe",
                    "from-v32",
                    "1970-01-01T00:00:00+00:00",
                ),
            )
            await connection.commit()
    finally:
        migrations_mod.MIGRATIONS = original

    database._INITIALIZED_PATH = None
    await database.close_db()
    await database.init_db(persistent=True)
    async with database._connect() as connection:
        row = await connection.execute_fetchall(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        )
        assert int(row[0][0]) == head
        probe = await connection.execute_fetchall(
            """SELECT value FROM guild_settings
                WHERE guild_id = 9001 AND key = 'r04.upgrade_probe'"""
        )
        assert probe and probe[0][0] == "from-v32"
        await assert_identity_registry_matches_schema(connection)


async def _check_repeated_init_preserves_checksums(database: Any) -> None:
    async with database._connect() as connection:
        before = await connection.execute_fetchall(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        )
    database._INITIALIZED_PATH = None
    await database.close_db()
    await database.init_db(persistent=True)
    async with database._connect() as connection:
        after = await connection.execute_fetchall(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        )
    assert before == after
    assert before, "schema_migrations must not be empty after init"


def main() -> None:
    args = _arguments()
    _validate_test_url(args.url)
    # Set this before importing config/database modules.
    os.environ["DATABASE_URL"] = args.url
    os.environ["DATABASE_REQUIRE_POSTGRES"] = "true"
    with asyncio.Runner(loop_factory=_event_loop) as runner:
        runner.run(_check())
    print("PostgreSQL integration check: OK")


if __name__ == "__main__":
    main()
