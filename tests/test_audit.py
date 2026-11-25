from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.settings import get_registry
from infrastructure.database import connection
from infrastructure.transactions import begin_write
from repositories import audit as audit_repo
from repositories.settings import get_runtime_setting, upsert_runtime_setting
from services.audit import (
    list_decisions,
    prune_old_decisions,
    record_decision,
)
from utils import access, database


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "audit.db",
        )
        self.path_patch.start()
        await database.init_db()
        get_registry().clear_cache()

    async def asyncTearDown(self) -> None:
        get_registry().clear_cache()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_decision_rolls_back_with_the_business_change(self) -> None:
        async with connection() as db:
            await begin_write(db, "runtime-setting", "1:COINS_DAILY")
            await upsert_runtime_setting(
                db, 1, "COINS_DAILY", "11", updated_by="tester"
            )
            await record_decision(
                guild_id=1,
                action="SETTINGS.SET",
                subject_type="setting",
                subject_id="COINS_DAILY",
                actor_id=0,
                actor_name="tester",
                before=None,
                after="11",
                source="command",
                db=db,
            )
            await db.rollback()

        self.assertIsNone(await get_runtime_setting(1, "COINS_DAILY"))
        self.assertEqual(await list_decisions(1), [])

    async def test_decision_commits_with_the_business_change(self) -> None:
        async with connection() as db:
            await begin_write(db, "runtime-setting", "1:COINS_DAILY")
            await upsert_runtime_setting(
                db, 1, "COINS_DAILY", "22", updated_by="tester"
            )
            decision_id = await record_decision(
                guild_id=1,
                action="SETTINGS.SET",
                subject_type="setting",
                subject_id="COINS_DAILY",
                actor_id=0,
                actor_name="tester",
                before="50",
                after="22",
                source="command",
                db=db,
            )
            await db.commit()

        self.assertEqual(await get_runtime_setting(1, "COINS_DAILY"), "22")
        rows = await list_decisions(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, decision_id)
        self.assertEqual(rows[0].action, "SETTINGS.SET")
        self.assertEqual(rows[0].after, "22")

    async def test_settings_registry_write_shares_transaction_with_audit(self) -> None:
        with patch(
            "services.audit.record_decision",
            AsyncMock(side_effect=RuntimeError("audit fail")),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit fail"):
                await get_registry().set(4, "COINS_DAILY", 7, updated_by="tester")

        self.assertIsNone(await get_runtime_setting(4, "COINS_DAILY"))
        self.assertEqual(await list_decisions(4), [])

        await get_registry().set(4, "COINS_DAILY", 8, updated_by="tester")
        self.assertEqual(await get_runtime_setting(4, "COINS_DAILY"), "8")
        rows = await list_decisions(4, action="SETTINGS.SET")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].subject_id, "COINS_DAILY")
        self.assertEqual(rows[0].after, "8")
        self.assertEqual(rows[0].source, "command")

    async def test_owner_bypass_is_recorded_only_when_check_would_fail(self) -> None:
        async def denied(_interaction: object) -> bool:
            return False

        async def allowed(_interaction: object) -> bool:
            return True

        denied_command = SimpleNamespace(
            checks=[denied], qualified_name="admin setup"
        )
        allowed_command = SimpleNamespace(
            checks=[allowed], qualified_name="admin preflight"
        )
        tree = SimpleNamespace(walk_commands=lambda: [denied_command, allowed_command])
        owner = SimpleNamespace(id=777, name="owner", display_name="owner")
        other = SimpleNamespace(id=888, name="staff")

        with patch.object(access, "BOT_OWNER_ID", 777):
            self.assertEqual(access.install_bot_owner_command_bypass(tree), 2)
            owner_denied = SimpleNamespace(
                user=owner,
                guild_id=9,
                command=denied_command,
                id=1001,
            )
            owner_allowed = SimpleNamespace(
                user=owner,
                guild_id=9,
                command=allowed_command,
                id=1002,
            )
            stranger = SimpleNamespace(
                user=other,
                guild_id=9,
                command=denied_command,
                id=1003,
            )
            self.assertTrue(await denied_command.checks[0](owner_denied))
            self.assertTrue(await allowed_command.checks[0](owner_allowed))
            self.assertFalse(await denied_command.checks[0](stranger))

        rows = await list_decisions(9)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, "owner_bypass")
        self.assertEqual(rows[0].action, "ACCESS.OWNER_BYPASS")
        self.assertEqual(rows[0].subject_type, "command")
        self.assertEqual(rows[0].subject_id, "admin setup")
        self.assertEqual(rows[0].actor_id, 777)

    async def test_list_filters_actor_action_subject_and_period(self) -> None:
        now = datetime.now(UTC)
        older = (now - timedelta(days=10)).isoformat()
        newer = now.isoformat()
        await record_decision(
            guild_id=2,
            action="SETTINGS.SET",
            subject_type="setting",
            subject_id="COINS_DAILY",
            actor_id=10,
            actor_name="ann",
            source="command",
            correlation_id="a",
        )
        await record_decision(
            guild_id=2,
            action="TREASURY.PAYOUT_APPROVED",
            subject_type="payout",
            subject_id="55",
            actor_id=11,
            actor_name="bob",
            source="component",
            correlation_id="b",
        )
        async with connection() as db:
            await db.execute(
                "UPDATE decision_log SET created_at = ? WHERE action = ?",
                (older, "SETTINGS.SET"),
            )
            await db.execute(
                "UPDATE decision_log SET created_at = ? WHERE action = ?",
                (newer, "TREASURY.PAYOUT_APPROVED"),
            )
            await db.commit()

        by_actor = await list_decisions(2, actor_id=10)
        self.assertEqual([row.action for row in by_actor], ["SETTINGS.SET"])
        by_action = await list_decisions(2, action="TREASURY.PAYOUT_APPROVED")
        self.assertEqual([row.subject_id for row in by_action], ["55"])
        by_subject = await list_decisions(
            2, subject_type="setting", subject_id="COINS_DAILY"
        )
        self.assertEqual(len(by_subject), 1)
        since_recent = await list_decisions(2, since=(now - timedelta(days=1)).date().isoformat())
        self.assertEqual([row.action for row in since_recent], ["TREASURY.PAYOUT_APPROVED"])
        until_old = await list_decisions(2, until=(now - timedelta(days=5)).date().isoformat())
        self.assertEqual([row.action for row in until_old], ["SETTINGS.SET"])

    async def test_repository_has_no_public_row_mutate_api(self) -> None:
        names = {
            name
            for name, value in inspect.getmembers(audit_repo)
            if inspect.isfunction(value) and not name.startswith("_")
        }
        self.assertIn("insert_decision", names)
        self.assertIn("get_decision", names)
        self.assertIn("list_decisions", names)
        self.assertIn("prune_expired_decisions", names)
        self.assertNotIn("update_decision", names)
        self.assertNotIn("delete_decision", names)
        self.assertFalse(any("update" in name for name in names))
        self.assertFalse(any(name.startswith("delete") for name in names))

    async def test_retention_zero_skips_and_positive_prunes(self) -> None:
        await record_decision(
            guild_id=3,
            action="SETTINGS.SET",
            subject_type="setting",
            subject_id="family_name",
            actor_id=1,
            actor_name="tester",
            source="system",
        )
        async with connection() as db:
            await db.execute(
                "UPDATE decision_log SET created_at = ?",
                ((datetime.now(UTC) - timedelta(days=400)).isoformat(),),
            )
            await db.commit()

        self.assertEqual(await prune_old_decisions(0), 0)
        self.assertEqual(len(await list_decisions(3)), 1)
        self.assertEqual(await prune_old_decisions(365), 1)
        self.assertEqual(await list_decisions(3), [])

    async def test_secret_payloads_are_redacted(self) -> None:
        with patch.dict("os.environ", {"TOKEN": "super-secret-token"}):
            await record_decision(
                guild_id=5,
                action="SETTINGS.SET",
                subject_type="setting",
                subject_id="TOKEN",
                actor_id=1,
                actor_name="tester",
                before="super-secret-token",
                after="postgresql://user:pass@localhost/db",
                source="command",
            )
        row = (await list_decisions(5))[0]
        self.assertEqual(row.before, "***")
        self.assertEqual(row.after, "***")
        self.assertNotIn("super-secret-token", row.after or "")


if __name__ == "__main__":
    unittest.main()
