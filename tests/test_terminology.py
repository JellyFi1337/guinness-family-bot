"""User-facing copy must not keep Milton terminology.

Walks string literals in cogs/, ui/ and services/ (except archival modules)
and README / FUNCTIONAL. Technical identifiers, logger messages, custom_id
values and historical docs stay on an explicit exception list.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests.test_maps import MILTON_MAP_NAMES

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("cogs", "ui", "services")
DOC_FILES = (
    ROOT / "README.md",
    ROOT / "docs" / "FUNCTIONAL.md",
)
EXEMPT_FILES = frozenset(
    {
        "services/external/rage_master.py",
    }
)
LOGGER_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log"}
)
CUSTOM_ID_KEYS = frozenset({"custom_id"})
TECHNICAL_WORD = re.compile(r"^[A-Za-z0-9_.:\-]+$")
VZP_RE = re.compile(r"(?<![A-Za-z0-9_])VZP(?![A-Za-z0-9_])", re.IGNORECASE)
GTA_RE = re.compile(r"GTA\s*5\s*RP|GTA5RP", re.IGNORECASE)
MILTON_RE = re.compile(r"(?<![A-Za-z])Milton(?![A-Za-z])")


def _python_files():
    for directory in SOURCE_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative in EXEMPT_FILES or "__pycache__" in relative:
                continue
            yield relative, path


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


def _skip_ids(tree: ast.AST) -> set[int]:
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        logger_call = isinstance(func, ast.Attribute) and func.attr in LOGGER_METHODS
        if logger_call:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    skipped.add(id(arg))
            for kw in node.keywords:
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    skipped.add(id(kw.value))
        for kw in node.keywords:
            if kw.arg in CUSTOM_ID_KEYS and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    skipped.add(id(kw.value))
    return skipped


def _hits(value: str) -> list[str]:
    found: list[str] = []
    if TECHNICAL_WORD.fullmatch(value):
        return found
    if VZP_RE.search(value):
        found.append("VZP")
    if "ВЗП" in value:
        found.append("ВЗП")
    if MILTON_RE.search(value):
        found.append("Milton")
    if GTA_RE.search(value):
        found.append("GTA 5 RP")
    for name in MILTON_MAP_NAMES:
        if name in value:
            found.append(name)
    return found


class TerminologyTests(unittest.TestCase):
    def test_user_facing_python_has_no_milton_terms(self) -> None:
        offenders: list[str] = []
        for relative, path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            skipped = _skip_ids(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in skipped or _is_docstring(tree, node):
                    continue
                for term in _hits(node.value):
                    offenders.append(f"{relative}:{node.lineno}: {term}")
        self.assertEqual(
            offenders,
            [],
            "Запрещённые термины Milton в пользовательском тексте:\n"
            + "\n".join(offenders),
        )

    def test_readme_and_functional_use_memphis_copy(self) -> None:
        offenders: list[str] = []
        for path in DOC_FILES:
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#") and "CHANNEL_" in stripped:
                    continue
                for term in _hits(line):
                    offenders.append(
                        f"{path.relative_to(ROOT).as_posix()}:{lineno}: {term}"
                    )
        self.assertEqual(
            offenders,
            [],
            "Запрещённые термины Milton в пользовательской документации:\n"
            + "\n".join(offenders),
        )
