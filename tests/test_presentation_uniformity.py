"""Fail the suite if production code bypasses the design system."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    "cogs",
    "services",
    "utils",
    "core",
    "repositories",
    "infrastructure",
    "ui",
    "main.py",
)
ALLOWED_EMBED_FILES = {
    # Personal ephemeral pager may keep a local embed.
    "ui/pagination.py",
}
ALLOWED_SEND_FILES = {
    "ui/messages.py",
    "ui/pagination.py",
}
ALLOWED_ADMIN_FOLLOWUP_SEND = "cogs/admin.py"
SEND_ATTRS = {"send_message", "edit_message"}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _iter_production_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        target = ROOT / root
        if target.is_file():
            files.append(target)
            continue
        if not target.is_dir():
            continue
        files.extend(sorted(target.rglob("*.py")))
    return [path for path in files if "__pycache__" not in path.parts]


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_discord_embed(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "Embed":
        return False
    owner = func.value
    return isinstance(owner, ast.Name) and owner.id == "discord"


def _is_message_send(node: ast.Call) -> bool:
    name = _call_name(node)
    if name in SEND_ATTRS:
        return True
    if name != "send":
        return False
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    # destination.send / followup.send / owner.send — not send_v2()
    return True


def _has_file_kwarg(node: ast.Call) -> bool:
    return any(keyword.arg in {"file", "files"} for keyword in node.keywords)


def _line(path: Path, node: ast.AST) -> str:
    return f"{_rel(path)}:{node.lineno}"


class PresentationUniformityTests(unittest.TestCase):
    def test_production_code_does_not_construct_embeds(self) -> None:
        hits: list[str] = []
        for path in _iter_production_files():
            relative = _rel(path)
            if relative in ALLOWED_EMBED_FILES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _is_discord_embed(node):
                    hits.append(_line(path, node))
        self.assertEqual(
            hits,
            [],
            "discord.Embed(...) outside the design system:\n" + "\n".join(hits),
        )

    def test_production_code_does_not_bypass_message_transport(self) -> None:
        hits: list[str] = []
        for path in _iter_production_files():
            relative = _rel(path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_message_send(node):
                    continue
                if relative in ALLOWED_SEND_FILES:
                    continue
                if relative == ALLOWED_ADMIN_FOLLOWUP_SEND and _has_file_kwarg(node):
                    continue
                hits.append(_line(path, node))
        self.assertEqual(
            hits,
            [],
            "direct send/send_message/edit_message bypassing ui/messages.py:\n"
            + "\n".join(hits),
        )

    def test_exception_list_stays_explicit_and_small(self) -> None:
        self.assertEqual(
            ALLOWED_SEND_FILES,
            {"ui/messages.py", "ui/pagination.py"},
        )
        self.assertEqual(ALLOWED_ADMIN_FOLLOWUP_SEND, "cogs/admin.py")


if __name__ == "__main__":
    unittest.main()
