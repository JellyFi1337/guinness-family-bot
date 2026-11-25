"""Guard against user-facing text referencing commands that do not exist.

`/daily`, `/pay`, `/role_order` and `/skip` all outlived their move into command
groups and kept telling users to run commands Discord no longer knew. These
tests fail on the next such rename instead of shipping a dead hint.
"""

import ast
import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discord
from discord.ext import commands as discord_commands

from core.bot import EXTENSIONS
from ui.command_registry import (
    REFERENCEABLE,
    UnknownCommand,
    cmd,
    code,
    diff_against_tree,
    diff_groups_against_tree,
    format_diff,
)
from utils import database


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("cogs", "ui", "utils", "services", "repositories", "core")

# A backticked slash reference in a Russian sentence, e.g. `/coins daily`.
INLINE_COMMAND_RE = re.compile(r"`/([a-z][a-z0-9_]*(?: [a-z][a-z0-9_]*)?)`")

# Literals that legitimately mention a slash-prefixed token which is not an
# application command (documentation of the registry itself lives here).
EXEMPT_FILES = {"ui/command_registry.py"}


def _python_files():
    for directory in SOURCE_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative in EXEMPT_FILES or "__pycache__" in relative:
                continue
            yield relative, path


class CommandRegistryTests(unittest.TestCase):
    def test_cmd_renders_qualified_name(self):
        self.assertEqual(cmd("coins daily"), "/coins daily")
        self.assertEqual(code("music skip"), "`/music skip`")

    def test_cmd_rejects_unknown_command(self):
        with self.assertRaises(UnknownCommand):
            cmd("daily")
        with self.assertRaises(UnknownCommand):
            cmd("skip")

    def test_no_source_literal_references_an_unregistered_command(self):
        """Every `/xxx` written inside a string literal must be a real command.

        Only string literals are inspected: comments and docstrings describe
        history and may legitimately quote a retired name.
        """

        offenders: list[str] = []
        for relative, path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if _is_docstring(tree, node):
                    continue
                for match in INLINE_COMMAND_RE.finditer(node.value):
                    name = match.group(1)
                    if name not in REFERENCEABLE:
                        offenders.append(f"{relative}:{node.lineno}: /{name}")
        self.assertEqual(
            offenders,
            [],
            "Устаревшие подсказки команд в тексте:\n" + "\n".join(offenders),
        )


class CommandTreeDriftTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_matches_the_live_command_tree(self):
        """The registry and the real command tree must not drift apart."""

        bot = discord_commands.Bot(
            command_prefix="!", intents=discord.Intents.default()
        )
        bot._ready = asyncio.Event()
        bot._ready.set()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            database, "DB_PATH", Path(temp_dir) / "registry.db"
        ):
            await database.init_db()
            try:
                for extension in EXTENSIONS:
                    await bot.load_extension(extension)
                extra, stale = diff_against_tree(bot.tree)
                self.assertEqual(
                    (extra, stale),
                    (set(), set()),
                    "Реестр команд разошёлся с деревом: "
                    + format_diff(extra, stale),
                )
                extra_groups, stale_groups = diff_groups_against_tree(bot.tree)
                self.assertEqual(
                    (extra_groups, stale_groups),
                    (set(), set()),
                    "Реестр групп разошёлся с деревом: "
                    + format_diff(extra_groups, stale_groups),
                )
            finally:
                await bot.close()


def _is_docstring(tree: ast.AST, node: ast.Constant) -> bool:
    for parent in ast.walk(tree):
        if not isinstance(
            parent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(parent, "body", [])
        if body and isinstance(body[0], ast.Expr) and body[0].value is node:
            return True
    return False


if __name__ == "__main__":
    unittest.main()
