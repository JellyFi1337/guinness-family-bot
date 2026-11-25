from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord import app_commands

from core.command_tree import GuinnessCommandTree
from core.log_context import (
    ContextFilter,
    bind_interaction,
    clear_context,
    collect_interaction_options,
    current_context,
    current_reference,
    sanitize_text,
    task_context,
    use_context,
)
from core.logging_setup import JsonFormatter, configure_logging, parse_logger_levels


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _interaction(**overrides: object) -> SimpleNamespace:
    user = SimpleNamespace(id=42, name="cadet")
    channel = SimpleNamespace(id=99, name="help-room")
    command = SimpleNamespace(qualified_name="stats economy", binding=None)
    values = {
        "id": 777,
        "extras": {},
        "guild_id": 15,
        "channel_id": 99,
        "user": user,
        "channel": channel,
        "command": command,
        "data": {
            "options": [
                {"name": "amount", "value": 100},
                {"name": "token", "value": "super-secret-token"},
            ]
        },
        "response": SimpleNamespace(is_done=lambda: False),
        "client": SimpleNamespace(owner_alerts=None),
        "type": SimpleNamespace(name="application_command"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LogContextTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_context()

    def tearDown(self) -> None:
        clear_context()

    def test_filter_puts_context_on_the_record(self) -> None:
        logger = logging.getLogger("tests.logging_context.filter")
        handler = _ListHandler()
        handler.addFilter(ContextFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            with use_context(
                correlation_id="abc-1",
                command="help",
                user_id="42",
                user_name="cadet",
                guild_id="15",
                channel_id="99",
                channel_name="help-room",
                source="command",
            ):
                logger.info("hello")
        finally:
            logger.removeHandler(handler)

        self.assertEqual(len(handler.records), 1)
        record = handler.records[0]
        self.assertEqual(record.correlation_id, "abc-1")
        self.assertEqual(record.command, "help")
        self.assertEqual(record.user_id, "42")
        self.assertEqual(record.guild_id, "15")
        self.assertEqual(record.channel_id, "99")
        self.assertEqual(record.source, "command")

    def test_missing_fields_are_dashes(self) -> None:
        record = logging.LogRecord(
            "tests.logging_context.missing",
            logging.INFO,
            __file__,
            0,
            "plain",
            (),
            None,
        )
        ContextFilter().filter(record)
        self.assertEqual(record.correlation_id, "-")
        self.assertEqual(record.command, "-")
        self.assertEqual(record.source, "-")

    def test_secrets_are_masked_in_options_and_text(self) -> None:
        interaction = _interaction()
        bind_interaction(interaction, source="command")
        options = collect_interaction_options(interaction)
        self.assertEqual(options["amount"], "100")
        self.assertEqual(options["token"], "***")
        self.assertNotIn("super-secret-token", str(options))

        with patch.dict(os.environ, {"TOKEN": "live-token-value"}, clear=False):
            self.assertEqual(
                sanitize_text("Bearer live-token-value"),
                "Bearer ***",
            )
        self.assertIn(
            "***",
            sanitize_text("postgresql://guinnes:example-secret@127.0.0.1:5432/db"),
        )

    def test_anonymous_payload_is_masked(self) -> None:
        interaction = _interaction(
            command=SimpleNamespace(qualified_name="anonymous reply", binding=None),
            data={
                "custom_id": "anonymous:user_reply",
                "components": [
                    {
                        "components": [
                            {"custom_id": "response_text", "value": "личный текст"}
                        ]
                    }
                ],
            },
        )
        bind_interaction(interaction, source="modal")
        options = collect_interaction_options(interaction)
        self.assertEqual(options["response_text"], "***")
        self.assertNotIn("личный текст", str(options))

    def test_json_formatter_includes_context_and_traceback(self) -> None:
        record = logging.LogRecord(
            "tests.logging_context.json",
            logging.ERROR,
            __file__,
            0,
            "failed",
            (),
            (RuntimeError, RuntimeError("boom"), None),
        )
        with use_context(correlation_id="ref-9", command="help", source="command"):
            ContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(payload["correlation_id"], "ref-9")
        self.assertEqual(payload["command"], "help")
        self.assertEqual(payload["msg"], "failed")
        self.assertIn("boom", payload["traceback"])

    def test_gateway_and_client_cannot_go_below_warning(self) -> None:
        levels = parse_logger_levels("discord.gateway=DEBUG,discord.client=INFO")
        self.assertEqual(levels["discord.gateway"], "WARNING")
        self.assertEqual(levels["discord.client"], "WARNING")


class LoggingSetupTests(unittest.TestCase):
    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
        configure_logging(level="INFO", log_dir="", log_format="text")
        clear_context()

    def test_unwritable_log_dir_keeps_stdout_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            resolved = configure_logging(log_dir=str(blocker), level="INFO")
            self.assertIsNone(resolved)

    def test_writable_log_dir_creates_error_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolved = configure_logging(log_dir=directory, level="INFO")
            self.assertEqual(resolved, Path(directory))
            logging.getLogger("tests.logging_context.files").error("disk-boom")
            for handler in logging.getLogger().handlers:
                handler.flush()
            error_log = Path(directory) / "bot.error.log"
            self.assertTrue(error_log.is_file())
            self.assertIn("disk-boom", error_log.read_text(encoding="utf-8"))
            root = logging.getLogger()
            for handler in list(root.handlers):
                handler.close()
                root.removeHandler(handler)


class CommandErrorLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_error_log_has_actor_place_and_reference(self) -> None:
        tree = GuinnessCommandTree.__new__(GuinnessCommandTree)
        tree.client = SimpleNamespace(owner_alerts=None)
        interaction = _interaction()
        error = app_commands.AppCommandError("boom")
        error.original = RuntimeError("boom")  # type: ignore[attr-defined]

        with (
            patch("core.command_tree.respond_spec", new=AsyncMock()) as respond,
            self.assertLogs("core.command_tree", level="ERROR") as captured,
        ):
            await tree.on_error(interaction, error)

        text = "\n".join(captured.output)
        self.assertIn("stats economy", text)
        self.assertIn("42", text)
        self.assertIn("cadet", text)
        self.assertIn("15", text)
        self.assertIn("99", text)
        self.assertIn("help-room", text)
        self.assertIn("777", text)
        self.assertIn("reference=777", text)
        self.assertIn("***", text)
        self.assertNotIn("super-secret-token", text)
        spec = respond.await_args.args[1]
        self.assertEqual(spec.reference, "777")
        self.assertEqual(current_reference(), "777")
        clear_context()


class TaskIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_does_not_leak_between_tasks(self) -> None:
        seen: list[str] = []

        async def left() -> None:
            with use_context(correlation_id="left", source="task"):
                await asyncio.sleep(0.02)
                seen.append(current_context().correlation_id)

        async def right() -> None:
            with use_context(correlation_id="right", source="task"):
                await asyncio.sleep(0)
                seen.append(current_context().correlation_id)

        await asyncio.gather(left(), right())
        self.assertEqual(set(seen), {"left", "right"})

    async def test_task_context_uses_fresh_correlation_id(self) -> None:
        async def worker() -> None:
            with task_context("operation-outbox"):
                first = current_reference()
            with task_context("operation-outbox"):
                second = current_reference()
            self.assertNotEqual(first, "-")
            self.assertNotEqual(first, second)
            self.assertEqual(current_context().command, "")

        await worker()


if __name__ == "__main__":
    unittest.main()
