from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from infrastructure.postgresql import PostgresConnection


class _FakeInfo:
    def __init__(self, status: str) -> None:
        self.transaction_status = SimpleNamespace(name=status)


class _FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, sql: str, params=None) -> None:
        self.statements.append(sql.strip().split()[0].upper())

    async def fetchone(self):
        return (1,)

    async def close(self) -> None:
        return None


class _FakePsycopgConnection:
    def __init__(self, status: str = "IDLE") -> None:
        self.info = _FakeInfo(status)
        self.cursors: list[_FakeCursor] = []

    def cursor(self, row_factory=None) -> _FakeCursor:
        cursor = _FakeCursor()
        self.cursors.append(cursor)
        return cursor


class BeginIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_begin_when_idle_issues_begin(self) -> None:
        raw = _FakePsycopgConnection("IDLE")
        db = PostgresConnection(raw)  # type: ignore[arg-type]
        await db._execute("BEGIN")
        self.assertEqual([c.statements for c in raw.cursors], [["BEGIN"]])

    async def test_begin_when_already_in_transaction_skips(self) -> None:
        raw = _FakePsycopgConnection("INTRANS")
        db = PostgresConnection(raw)  # type: ignore[arg-type]
        await db._execute("BEGIN")
        self.assertEqual(raw.cursors, [])

    async def test_begin_immediate_when_in_transaction_still_locks(self) -> None:
        raw = _FakePsycopgConnection("INTRANS")
        db = PostgresConnection(raw)  # type: ignore[arg-type]
        await db._execute("BEGIN IMMEDIATE")
        self.assertEqual(len(raw.cursors), 1)
        self.assertEqual(raw.cursors[0].statements[0], "SELECT")

    async def test_begin_immediate_when_idle_begins_then_locks(self) -> None:
        raw = _FakePsycopgConnection("IDLE")
        db = PostgresConnection(raw)  # type: ignore[arg-type]
        await db._execute("BEGIN IMMEDIATE")
        self.assertEqual(len(raw.cursors), 2)
        self.assertEqual(raw.cursors[0].statements, ["BEGIN"])
        self.assertEqual(raw.cursors[1].statements[0], "SELECT")


class BeginWriteIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_begin_write_skips_begin_when_already_in_transaction(self) -> None:
        from infrastructure import transactions

        db = mock.AsyncMock()
        db.in_transaction = True
        lock_cursor = mock.AsyncMock()
        lock_cursor.fetchone = mock.AsyncMock(return_value=(True,))
        db.execute = mock.AsyncMock(return_value=lock_cursor)

        with mock.patch.object(transactions, "database_backend", return_value="postgresql"):
            await transactions.begin_write(db, "unit", 1)

        # Only the advisory lock SELECT — no explicit BEGIN.
        self.assertEqual(db.execute.await_count, 1)
        sql = db.execute.await_args.args[0]
        self.assertIn("pg_advisory_xact_lock", sql)


if __name__ == "__main__":
    unittest.main()
