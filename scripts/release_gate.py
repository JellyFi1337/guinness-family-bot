"""Run every release check that can be automated, and name the ones that cannot.

The audit's release gate was a list in a document, which means it was a list
somebody had to remember. This runs the mechanical half and prints the manual
half, so "did we check that?" has an answer instead of a recollection.

    python scripts/release_gate.py            # everything runnable here
    python scripts/release_gate.py --quick    # skip the slow test run

Three summary lines are enough to notice a failure and never enough to fix
one, so the *complete* output of every check is also written to
``.release_gate/<check>.log``.

Exit code is non-zero if any automated check fails, so CI can gate on it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / ".release_gate"


def _mypy_targets() -> list[str]:
    """Typed-module list shared with CI via ``[tool.guinness].mypy_targets``."""
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = data["tool"]["guinness"]["mypy_targets"]
    if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
        raise TypeError("[tool.guinness].mypy_targets must be a list of strings")
    return targets


# Checked by a human on the live server; listed so they are not forgotten.
MANUAL_STEPS = (
    ("/admin preflight", "0 критических ошибок и 0 маршрутов «только по имени»"),
    ("Privileged intents", "message_content и members включены в Developer Portal"),
    ("PostgreSQL 18", "боевая БД и CI используют одну мажорную версию"),
    ("Нагрузочный тест", "50 параллельных тикетов + Coins + автопарк, db_acquire p95 < 50 мс"),
    ("Тестовый сервер", "полный проход сценариев на копии прав и ролей"),
    ("Backup", "дамп снят и восстановление проверено на пустой базе"),
    ("Smoke после systemd", "systemctl start, затем /dev health без degraded"),
)


@dataclass(slots=True)
class Result:
    name: str
    ok: bool
    detail: str
    skipped: bool = False
    log: Path | None = None

    @property
    def mark(self) -> str:
        if self.skipped:
            return "—"
        return "OK" if self.ok else "FAIL"


def _slug(name: str) -> str:
    # \w, not [a-z0-9]: the check names are Russian, and an ASCII-only class
    # collapses every one of them to the same empty string.
    return re.sub(r"\W+", "-", name.casefold(), flags=re.UNICODE).strip("-") or "check"


def _write_log(name: str, command: list[str], output: str) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{_slug(name)}.log"
    path.write_text(
        f"$ {' '.join(command)}\n\n{output}", encoding="utf-8", errors="replace"
    )
    return path


def _tool(name: str, *args: str) -> list[str] | None:
    """Prefer the installed console script, fall back to ``python -m``.

    Ruff and mypy ship as standalone executables; ``python -m ruff`` only works
    when they happen to be installed into the interpreter running this script,
    which is not the case for a pipx- or user-installed toolchain.
    """

    executable = shutil.which(name)
    if executable:
        return [executable, *args]
    probe = subprocess.run(
        [sys.executable, "-m", name, "--version"], capture_output=True, text=True
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", name, *args]
    return None


def _run(
    name: str,
    command: list[str] | None,
    *,
    cwd: Path = ROOT,
    skip_if: tuple[str, ...] = (),
) -> Result:
    if command is None:
        return Result(name, True, "инструмент не установлен", skipped=True)
    try:
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=1_800
        )
    except FileNotFoundError:
        return Result(name, True, f"{command[0]} не найден", skipped=True)
    except subprocess.TimeoutExpired:
        return Result(name, False, "превышен таймаут 30 мин")
    output = completed.stdout + completed.stderr
    log = _write_log(name, command, output)
    if completed.returncode == 0:
        return Result(name, True, "")
    for marker in skip_if:
        if marker in output:
            return Result(name, True, "неприменимо в этом окружении", skipped=True)
    tail = output.strip().split("\n")
    summary = " / ".join(line.strip() for line in tail[-3:] if line.strip())
    return Result(name, False, summary, log=log.relative_to(ROOT))


def check_migrations() -> Result:
    """Versions must be contiguous and every checksum unique and stable.

    A duplicated or reordered version is the one migration mistake that cannot
    be undone in production: the runner refuses to start, and the checksum
    guard turns an edited applied migration into a hard failure.
    """

    source = (ROOT / "infrastructure" / "migrations.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    versions: list[int] = []
    checksums: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "MIGRATIONS" for t in node.targets):
            continue
        for element in node.value.elts:  # type: ignore[attr-defined]
            version = element.args[0].value
            name = element.args[1].value
            statements: list[str] = []
            columns: list[str] = []
            for keyword in element.keywords:
                if keyword.arg == "statements":
                    statements = [item.value for item in keyword.value.elts]
                elif keyword.arg == "columns":
                    for table, mapping in zip(
                        keyword.value.keys, keyword.value.values, strict=True
                    ):
                        for column, definition in zip(
                            mapping.keys, mapping.values, strict=True
                        ):
                            columns.append(
                                f"{table.value}.{column.value}:{definition.value}"
                            )
            payload = "\n".join([name, *statements] + columns)
            checksums[version] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            versions.append(version)
    if not versions:
        return Result("Миграции", False, "MIGRATIONS не найдены")
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        return Result("Миграции", False, f"версии не непрерывны: {versions}")
    if len(set(checksums.values())) != len(checksums):
        return Result("Миграции", False, "совпадающие checksum у разных миграций")
    return Result("Миграции", True, f"1..{versions[-1]}, checksum уникальны")


def check_locked_dependencies() -> Result:
    """The deploy instructions must install the same set CI tests."""

    readme = (ROOT / "deploy" / "README-ubuntu.md").read_text(encoding="utf-8")
    if "requirements.txt" in readme and "pip install" in readme:
        for line in readme.split("\n"):
            if "requirements.txt" in line and "install" in line:
                return Result(
                    "Закреплённые зависимости", False,
                    "деплой ставит requirements.txt вместо requirements.lock",
                )
    if "--require-hashes" not in readme:
        return Result(
            "Закреплённые зависимости", False, "в деплое нет --require-hashes"
        )
    return Result("Закреплённые зависимости", True, "деплой ставит requirements.lock")


def check_lock_interpreter() -> Result:
    """A lock compiled on one Python is not installable on an older one.

    ``pip-compile`` resolves against the interpreter that runs it and emits no
    environment markers, so a lock built on 3.14 pulls in packages that only
    exist there — ``audioop-lts`` is the standing example — and the whole
    ``--require-hashes`` install aborts on a 3.12 venv. Compile where you
    deploy, and this stays quiet.
    """

    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    header = re.compile(r"autogenerated by pip-compile with Python (\d+\.\d+)")
    for name in ("requirements.lock", "requirements-dev.lock"):
        path = ROOT / name
        if not path.exists():
            return Result("Питон локов", False, f"{name} отсутствует")
        found = header.search(path.read_text(encoding="utf-8"))
        if not found:
            continue
        compiled = found.group(1)
        if compiled == running:
            continue
        return Result(
            "Питон локов", False,
            f"{name} собран на Python {compiled}, деплой на {running} — "
            f"пересоберите: pip-compile --generate-hashes "
            f"--output-file={name} "
            f"{'requirements-dev.in' if 'dev' in name else 'requirements.txt'}",
        )
    return Result("Питон локов", True, f"локи собраны на Python {running}")


def check_configured_ids() -> Result:
    """Empty *_ID settings mean the bot routes by name and breaks on a rename."""

    config = (ROOT / "bot_config.toml").read_text(encoding="utf-8")
    empty = [
        line.split("=")[0].strip()
        for line in config.split("\n")
        if line.strip().endswith('= ""') and "_ID" in line.split("=")[0]
    ]
    if empty:
        return Result(
            "Discord ID заполнены", False,
            f"пусто: {len(empty)} (например {', '.join(empty[:3])}) — "
            "выгрузите значения через /admin config_ids",
        )
    return Result("Discord ID заполнены", True, "все *_ID заданы")


def _toolchain() -> str:
    """Name the versions doing the checking.

    A lint failure nobody can reproduce is almost always a version difference
    between the machine that wrote the code and the machine that gates it, so
    the report says which ruff and which mypy produced it.
    """

    parts = [f"python {sys.version.split()[0]}"]
    for name in ("ruff", "mypy"):
        command = _tool(name, "--version")
        if command is None:
            parts.append(f"{name} не установлен")
            continue
        probe = subprocess.run(command, capture_output=True, text=True)
        parts.append(probe.stdout.strip().splitlines()[0] if probe.stdout else name)
    return " • ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick", action="store_true", help="пропустить полный прогон тестов"
    )
    args = parser.parse_args()

    results = [
        _run("Ruff", _tool("ruff", "check", ".")),
        _run(
            "Mypy",
            _tool(
                "mypy", "--explicit-package-bases",
                *_mypy_targets(),
            ),
        ),
        _run(
            "Секреты",
            [sys.executable, "scripts/check_secrets.py", "--mode", "tracked"],
            # Running outside a checkout is not a failing secret scan.
            skip_if=("not a git repository", "fatal: not a git"),
        ),
        _run("pip check", [sys.executable, "-m", "pip", "check"]),
        check_migrations(),
        check_locked_dependencies(),
        check_lock_interpreter(),
        check_configured_ids(),
    ]
    if args.quick:
        results.append(Result("Тесты", True, "пропущены (--quick)", skipped=True))
    else:
        results.append(
            _run(
                "Тесты",
                # "-t ." matters: without an explicit top-level directory
                # unittest treats tests/ as the root and never imports
                # tests/__init__.py, which is what pins the suite to SQLite.
                [
                    sys.executable, "-m", "unittest", "discover",
                    "-s", "tests", "-t", ".",
                ],
                skip_if=("No module named 'discord'",),
            )
        )
        # An unconfigured integration database is a check that did not run, not
        # a check that failed — reporting it as FAIL trains people to ignore it.
        if os.getenv("POSTGRES_TEST_URL"):
            results.append(
                _run(
                    "PostgreSQL",
                    [sys.executable, "scripts/postgresql_integration_check.py"],
                )
            )
        else:
            results.append(
                Result(
                    "PostgreSQL", True,
                    "POSTGRES_TEST_URL не задан — интеграционный прогон пропущен",
                    skipped=True,
                )
            )

    width = max(len(item.name) for item in results)
    print("\nАвтоматические проверки")
    print(_toolchain())
    print("-" * (width + 40))
    for item in results:
        print(f"  {item.mark:<4} {item.name:<{width}}  {item.detail}")
        if item.log is not None:
            print(f"  {'':<4} {'':<{width}}  полный вывод: {item.log}")

    print("\nПроверить вручную на боевом сервере")
    print("-" * (width + 40))
    for name, detail in MANUAL_STEPS:
        print(f"  [ ]  {name:<{width}}  {detail}")

    failed = [item for item in results if not item.ok and not item.skipped]
    print()
    if failed:
        print(f"Не пройдено: {len(failed)} — релиз не готов.")
        return 1
    print("Автоматическая часть пройдена. Остаётся ручной список выше.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
