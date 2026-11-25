from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from psycopg import AsyncConnection
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool


# BEGIN IMMEDIATE serialized SQLite writers.  A transaction-scoped PostgreSQL
# advisory lock preserves that correctness model for existing read/modify/write
# workflows without blocking ordinary readers.
_WRITE_LOCK_ID = 7_559_018_321

_IDENTITY_TABLES = {
    "account_reconciliations",
    "accounts",
    "anonymous_conversations",
    "birthday_fundraisers",
    "bonus_periods",
    "bonus_shares",
    "car_bookings",
    "cars",
    "clearances",
    "decision_log",
    "contribution_bonus_marks",
    "contribution_entries",
    "contribution_rule_versions",
    "contract_catalog",
    "contract_report_evidence",
    "contract_report_expenses",
    "contract_report_item_payouts",
    "contract_report_purchases",
    "contract_report_reversals",
    "contract_reports",
    "contract_runs",
    "report_payout_obligations",
    "coin_activity_ledger",
    "coin_transactions",
    "event_attendance_log",
    "event_roster",
    "external_snapshots",
    "family_context",
    "family_events",
    "family_maps",
    "family_properties",
    "family_seasons",
    "fundraiser_contributions",
    "giveaways",
    "inventory_balances",
    "inventory_items",
    "inventory_kit_lines",
    "inventory_kits",
    "inventory_movements",
    "inventory_reconciliations",
    "inventory_reservations",
    "inventory_tax_duties",
    "ledger_entries",
    "obligations",
    "music_tracks",
    "onboarding_tracks",
    "org_development",
    "org_lore_reviews",
    "org_upgrade_progress",
    "org_upgrades",
    "promotion_criteria",
    "promotion_proposals",
    "operation_outbox",
    "member_events",
    "members",
    "metric_snapshots",
    "polls",
    "queue_claims",
    "queue_transfers",
    "ranks",
    "rules_versions",
    "season_play_hours",
    "staff_appeals",
    "training_assignments",
    "purchased_roles",
    "recruiting_unresolved_joins",
    "requests",
    "role_purchase_requests",
    "shop_purchase_intents",
    "stats_channel_events",
    "stats_member_events",
    "stats_processed_actions",
    "stats_voice_sessions",
    "storage_locations",
    "summit_lists",
    "summit_vzp_lists",
    "tickets",
    "treasury_transactions",
}


def diff_identity_registry(
    registry: set[str], tables_with_id: set[str]
) -> tuple[list[str], list[str]]:
    """Return (registry-but-no-id, has-id-but-missing-from-registry)."""

    return (
        sorted(registry - tables_with_id),
        sorted(tables_with_id - registry),
    )


async def fetch_identity_table_names(connection: Any) -> set[str]:
    """Tables in the live schema whose ``id`` is identity/serial.

    Matches the audit's AUTOINCREMENT criterion after SQLite→PostgreSQL
    translation (``GENERATED … AS IDENTITY`` / ``serial`` sequences).
    Singletons with a plain ``id`` CHECK and no sequence are excluded.
    """

    rows = await connection.execute_fetchall(
        """
        SELECT c.table_name
          FROM information_schema.columns AS c
          JOIN information_schema.tables AS t
            ON t.table_schema = c.table_schema
           AND t.table_name = c.table_name
         WHERE c.table_schema = current_schema()
           AND t.table_type = 'BASE TABLE'
           AND c.column_name = 'id'
           AND (
                c.is_identity = 'YES'
                OR pg_get_serial_sequence(
                    format('%I.%I', c.table_schema, c.table_name),
                    'id'
                ) IS NOT NULL
           )
        """
    )
    return {str(name) for (name,) in rows}


async def assert_identity_registry_matches_schema(connection: Any) -> None:
    """Fail if ``_IDENTITY_TABLES`` disagrees with the live schema either way."""

    with_id = await fetch_identity_table_names(connection)
    missing_id, missing_registry = diff_identity_registry(
        set(_IDENTITY_TABLES), with_id
    )
    if missing_id or missing_registry:
        raise AssertionError(
            "identity registry mismatch: "
            f"registry but no id column={missing_id!r}; "
            f"has id but missing from registry={missing_registry!r}"
        )

_PRAGMA_TABLE_INFO = re.compile(
    r"^\s*PRAGMA\s+table_info\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;?\s*$",
    re.IGNORECASE,
)
_INSERT_TABLE = re.compile(
    r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_SQLITE_SEQUENCE_NAMES = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)'")


def normalise_postgresql_url(url: str) -> str:
    """Return a libpq-compatible URL from an application/SQLAlchemy URL."""

    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _convert_placeholders(sql: str) -> str:
    """Convert SQLite qmark parameters to psycopg parameters.

    SQL in this project does not use PostgreSQL's JSON ``?`` operator, but the
    scanner still avoids replacing question marks inside quoted literals.
    """

    converted: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            converted.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    converted.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            converted.append(char)
        elif char == "?":
            converted.append("%s")
        else:
            converted.append(char)
        index += 1
    return "".join(converted)


def _append_on_conflict_do_nothing(sql: str) -> str:
    stripped = sql.rstrip()
    suffix = ";" if stripped.endswith(";") else ""
    if suffix:
        stripped = stripped[:-1].rstrip()
    return f"{stripped} ON CONFLICT DO NOTHING{suffix}"


def _convert_scalar_extrema(sql: str) -> str:
    """Translate SQLite scalar MAX/MIN calls while preserving aggregates."""

    replacements: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\b(MAX|MIN)\s*\(", sql, re.IGNORECASE):
        opening = match.end() - 1
        depth = 0
        quote: str | None = None
        has_top_level_comma = False
        index = opening
        while index < len(sql):
            char = sql[index]
            if quote is not None:
                if char == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            elif char == "," and depth == 1:
                has_top_level_comma = True
            index += 1
        if has_top_level_comma and depth == 0:
            replacement = (
                "GREATEST" if match.group(1).upper() == "MAX" else "LEAST"
            )
            replacements.append((match.start(1), match.end(1), replacement))

    translated = sql
    for start, end, replacement in reversed(replacements):
        translated = translated[:start] + replacement + translated[end:]
    return translated


def translate_sql(sql: str, *, include_returning: bool = True) -> str:
    """Translate the deliberately small SQLite dialect used by the bot."""

    translated = sql
    insert_or_ignore = bool(
        re.match(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", translated, re.IGNORECASE)
    )
    if insert_or_ignore:
        translated = re.sub(
            r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\b",
            r"\1INSERT INTO",
            translated,
            count=1,
            flags=re.IGNORECASE,
        )

    if re.match(r"^\s*(CREATE|ALTER)\b", translated, re.IGNORECASE):
        translated = re.sub(
            r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
            "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
            translated,
            flags=re.IGNORECASE,
        )
        translated = re.sub(
            r"\bINTEGER\b",
            "BIGINT",
            translated,
            flags=re.IGNORECASE,
        )
        translated = re.sub(
            r"\s+AUTOINCREMENT\b",
            "",
            translated,
            flags=re.IGNORECASE,
        )

    # PostgreSQL's MAX/MIN are aggregate-only; GREATEST/LEAST are scalar.
    translated = _convert_scalar_extrema(translated)
    translated = re.sub(
        r"(\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*)"
        r"\s+COLLATE\s+NOCASE\b",
        r"LOWER(\1)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = _convert_placeholders(translated)

    if insert_or_ignore:
        translated = _append_on_conflict_do_nothing(translated)

    match = _INSERT_TABLE.match(translated)
    if (
        include_returning
        and match is not None
        and match.group(1).lower() in _IDENTITY_TABLES
        and " RETURNING " not in translated.upper()
        and not re.search(r"\bSELECT\b", translated, re.IGNORECASE)
    ):
        stripped = translated.rstrip()
        suffix = ";" if stripped.endswith(";") else ""
        if suffix:
            stripped = stripped[:-1].rstrip()
        translated = f"{stripped} RETURNING id{suffix}"
    return translated


def _split_script(script: str) -> list[str]:
    """Split the project's migration scripts without breaking quoted text."""

    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(script):
        char = script[index]
        current.append(char)
        if quote is not None:
            if char == quote:
                if index + 1 < len(script) and script[index + 1] == quote:
                    current.append(script[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


class PostgresCursor:
    def __init__(
        self,
        cursor: Any | None = None,
        *,
        rows: Sequence[tuple[Any, ...]] = (),
        rowcount: int | None = None,
        lastrowid: int | None = None,
    ) -> None:
        self._cursor = cursor
        self._rows = list(rows)
        self._row_index = 0
        self.rowcount = (
            int(rowcount)
            if rowcount is not None
            else int(getattr(cursor, "rowcount", -1))
        )
        self.lastrowid = lastrowid

    async def fetchone(self) -> tuple[Any, ...] | None:
        if self._cursor is not None:
            row = await self._cursor.fetchone()
            return tuple(row) if row is not None else None
        if self._row_index >= len(self._rows):
            return None
        row = self._rows[self._row_index]
        self._row_index += 1
        return row

    async def fetchall(self) -> list[tuple[Any, ...]]:
        if self._cursor is not None:
            return [tuple(row) for row in await self._cursor.fetchall()]
        remaining = self._rows[self._row_index :]
        self._row_index = len(self._rows)
        return remaining

    async def close(self) -> None:
        if self._cursor is not None:
            await self._cursor.close()
            self._cursor = None

    async def __aenter__(self) -> PostgresCursor:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()


class _ExecuteOperation:
    def __init__(
        self,
        connection: PostgresConnection,
        sql: str,
        params: Sequence[Any],
    ) -> None:
        self._connection = connection
        self._sql = sql
        self._params = params
        self._result: PostgresCursor | None = None

    async def _run(self) -> PostgresCursor:
        if self._result is None:
            self._result = await self._connection._execute(self._sql, self._params)
        return self._result

    def __await__(self):
        return self._run().__await__()

    async def __aenter__(self) -> PostgresCursor:
        return await self._run()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._result is not None:
            await self._result.close()


class PostgresConnection:
    """aiosqlite-shaped wrapper over a psycopg 3 async connection."""

    def __init__(self, connection: AsyncConnection[Any]) -> None:
        self._connection = connection

    @property
    def in_transaction(self) -> bool:
        return self._connection.info.transaction_status.name != "IDLE"

    def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> _ExecuteOperation:
        return _ExecuteOperation(self, sql, tuple(params))

    async def _execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        include_returning: bool = True,
    ) -> PostgresCursor:
        table_info = _PRAGMA_TABLE_INFO.match(sql)
        if table_info:
            cursor = self._connection.cursor(row_factory=tuple_row)
            await cursor.execute(
                """SELECT ordinal_position, column_name, data_type,
                          is_nullable, column_default
                     FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = %s
                    ORDER BY ordinal_position""",
                (table_info.group(1),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            # SQLite PRAGMA table_info exposes the column name at index 1.
            compatible = [
                (int(position) - 1, name, data_type, nullable, default, 0)
                for position, name, data_type, nullable, default in rows
            ]
            return PostgresCursor(rows=compatible, rowcount=len(compatible))

        normalised = sql.strip().rstrip(";").strip()
        upper = normalised.upper()
        if upper.startswith("PRAGMA "):
            return PostgresCursor(rowcount=0)

        if upper == "BEGIN" or upper == "BEGIN IMMEDIATE":
            # psycopg with autocommit=False auto-starts a transaction on the
            # first statement. Callers still issue explicit BEGIN / BEGIN
            # IMMEDIATE (SQLite habit); a second BEGIN only warns:
            # "there is already a transaction in progress".
            if not self.in_transaction:
                begin = self._connection.cursor(row_factory=tuple_row)
                await begin.execute("BEGIN")
                await begin.close()
            if upper == "BEGIN IMMEDIATE":
                lock = self._connection.cursor(row_factory=tuple_row)
                await lock.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_WRITE_LOCK_ID,),
                )
                await lock.fetchone()
                await lock.close()
            return PostgresCursor(rowcount=0)

        if upper.startswith("DELETE FROM SQLITE_SEQUENCE"):
            names = [
                name
                for name in _SQLITE_SEQUENCE_NAMES.findall(sql)
                if name in _IDENTITY_TABLES
            ]
            for name in names:
                cursor = self._connection.cursor(row_factory=tuple_row)
                await cursor.execute(
                    """SELECT setval(sequence_name, 1, false)
                         FROM (
                             SELECT pg_get_serial_sequence(%s, 'id') AS sequence_name
                         ) AS sequence_lookup
                        WHERE sequence_name IS NOT NULL""",
                    (name,),
                )
                await cursor.fetchall()
                await cursor.close()
            return PostgresCursor(rowcount=len(names))

        translated = translate_sql(sql, include_returning=include_returning)
        cursor = self._connection.cursor(row_factory=tuple_row)
        # ``None``, not ``()``: psycopg only scans for placeholders when it is
        # given parameters, and an empty tuple still counts. A statement with a
        # literal percent sign — ``LIKE 'magi%'``, ``to_char(…, '99%')`` — then
        # dies with "only '%s', '%b', '%t' are allowed as placeholders" on
        # PostgreSQL while working fine on SQLite.
        await cursor.execute(translated, tuple(params) if params else None)
        lastrowid: int | None = None
        if re.search(r"\bRETURNING\s+id\b", translated, re.IGNORECASE):
            row = await cursor.fetchone()
            if row is not None:
                lastrowid = int(row[0])
        return PostgresCursor(cursor, lastrowid=lastrowid)

    async def execute_fetchall(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[tuple[Any, ...]]:
        cursor = await self._execute(sql, params)
        try:
            return await cursor.fetchall()
        finally:
            await cursor.close()

    async def executemany(
        self,
        sql: str,
        params: Iterable[Sequence[Any]],
    ) -> PostgresCursor:
        translated = translate_sql(sql, include_returning=False)
        cursor = self._connection.cursor(row_factory=tuple_row)
        await cursor.executemany(translated, [tuple(values) for values in params])
        return PostgresCursor(cursor)

    async def executescript(self, script: str) -> None:
        for statement in _split_script(script):
            cursor = await self._execute(
                statement,
                include_returning=False,
            )
            await cursor.close()

    async def commit(self) -> None:
        await self._connection.commit()

    async def rollback(self) -> None:
        await self._connection.rollback()


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class PostgresPool:
    def __init__(self, *, size: int = 4, timeout: float = 30.0) -> None:
        self.size = max(1, min(int(size), 32))
        self.timeout = max(1.0, float(timeout))
        self._url = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pool: AsyncConnectionPool[Any] | None = None

    async def _ensure_open(self, url: str) -> None:
        loop = asyncio.get_running_loop()
        normalised_url = normalise_postgresql_url(url)
        if (
            self._pool is not None
            and self._loop is loop
            and self._url == normalised_url
        ):
            return
        await self.close()
        self._url = normalised_url
        self._loop = loop
        self._pool = AsyncConnectionPool(
            conninfo=normalised_url,
            min_size=1,
            max_size=self.size,
            timeout=self.timeout,
            open=False,
            kwargs={
                "autocommit": False,
                "row_factory": tuple_row,
                "connect_timeout": int(self.timeout),
                "application_name": "guinness_bot",
            },
        )
        await self._pool.open(wait=True)

    @asynccontextmanager
    async def connection(self, url: str):
        await self._ensure_open(url)
        assert self._pool is not None
        async with self._pool.connection(timeout=self.timeout) as connection:
            yield PostgresConnection(connection)

    async def maintain(self, url: str) -> None:
        """Liveness probe, not a statistics rebuild.

        This used to run a full ``ANALYZE`` over the whole schema, and the
        caller invoked it every five minutes — 288 planner-statistics rebuilds a
        day on a database where autovacuum already does this on its own
        schedule, driven by actual table churn. A cheap round-trip keeps the
        original purpose (surface a dead pool early) without the I/O.

        Use :meth:`analyze` for a deliberate, rare, manual rebuild.
        """

        await self._ensure_open(url)
        assert self._pool is not None
        async with self._pool.connection(timeout=self.timeout) as connection:
            await connection.execute("SELECT 1")

    async def analyze(self, url: str) -> None:
        """Rebuild planner statistics on demand (administrative operation)."""

        await self._ensure_open(url)
        assert self._pool is not None
        async with self._pool.connection(timeout=self.timeout) as connection:
            await connection.execute("ANALYZE")
            await connection.commit()

    async def close(self) -> None:
        pool = self._pool
        owner = self._loop
        self._pool = None
        self._url = ""
        self._loop = None
        if pool is None:
            return
        if owner is not None and owner is not _running_loop():
            # The pool's maintenance workers are tasks on a loop that is already
            # closed — every test case gets a fresh loop, and _ensure_open()
            # drops the stale pool on the next call. Awaiting close() here would
            # wait on tasks that can never run again and surface their
            # cancellation as a CancelledError in the caller, which is what made
            # 23 unrelated tests report failures they had nothing to do with.
            return
        await pool.close()

    def status(self) -> dict[str, int | bool | str]:
        if self._pool is None:
            return {
                "enabled": False,
                "size": self.size,
                "opened": 0,
                "available": 0,
                "in_use": 0,
            }
        stats = self._pool.get_stats()
        opened = int(stats.get("pool_size", 0))
        available = int(stats.get("pool_available", 0))
        return {
            "enabled": True,
            "size": self.size,
            "opened": opened,
            "available": available,
            "in_use": max(0, opened - available),
        }


def safe_database_target(url: str) -> str:
    parsed = urlsplit(normalise_postgresql_url(url))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    database = parsed.path.lstrip("/") or "postgres"
    return f"{host}:{port}/{database}"
