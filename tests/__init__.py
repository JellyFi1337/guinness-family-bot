"""Keep the test suite off every database that is not its own.

Each test case builds a throwaway schema in a temporary SQLite file and patches
``utils.database.DB_PATH`` to point at it. Nothing patches ``DATABASE_URL``,
though — and ``configuration`` loads ``.env`` at import time. On a machine where
that file holds the production connection string, which is precisely the boxed
case of the deploy server, all 300 tests silently ran against the live
PostgreSQL instance: they created tickets, credited Coins and dropped rows in
production, and then failed on each other's leftovers. The failures read as
logic bugs (``1600 != 600``, ``'active' != 'provisioning'``) when the real
message was "this ran somewhere it should never have run".

This module is imported before any test module, so clearing the variable here
is enough to pin the whole suite to SQLite. PostgreSQL is covered separately and
deliberately by ``scripts/postgresql_integration_check.py``, which requires an
explicit ``POSTGRES_TEST_URL``.
"""

from __future__ import annotations

import os


# Present-but-empty stops production DATABASE_URL; empty `.env` values do not
# clear non-empty process stubs (see configuration.load_secret_env).
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_REQUIRE_POSTGRES"] = "false"

# Stub Discord identity before config import so BOT_PROFILE=test with an empty
# GUILD_ID_TEST in a developer `.env` cannot abort the unit suite.
os.environ["BOT_PROFILE"] = "test"
os.environ["TOKEN"] = "unit-test-token"
os.environ["TOKEN_TEST"] = "unit-test-token"
os.environ["TOKEN_MAIN"] = "unit-test-token"
os.environ["GUILD_ID"] = "1"
os.environ["GUILD_ID_TEST"] = "1"
os.environ["GUILD_ID_MAIN"] = "1"
os.environ["BOT_OWNER_ID"] = "1"

# Reading it back through config closes the loop: if anything ever imports the
# settings before this module runs, the suite stops instead of quietly writing
# to whatever that import resolved to.
from config import DATABASE_URL as _CONFIGURED_DATABASE_URL  # noqa: E402


if _CONFIGURED_DATABASE_URL:
    raise RuntimeError(
        "Тесты работают только на временной SQLite-базе, но DATABASE_URL "
        f"указывает на {_CONFIGURED_DATABASE_URL.rsplit('@', 1)[-1]}. "
        "Прогон остановлен, чтобы не трогать боевые данные. "
        "Для проверки PostgreSQL используйте "
        "scripts/postgresql_integration_check.py с POSTGRES_TEST_URL."
    )
