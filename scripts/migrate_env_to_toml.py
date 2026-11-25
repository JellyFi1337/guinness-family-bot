from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from configuration import (
    is_allowed_env_name,
    is_secret_env_name,
    load_public_settings,
)


ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=).*$"
)


def _parse_bool(name: str, raw_value: str) -> bool:
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _toml_value(name: str, raw_value: str, current: Any) -> str:
    if isinstance(current, bool):
        return "true" if _parse_bool(name, raw_value) else "false"
    if isinstance(current, int):
        try:
            return str(int(raw_value))
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer") from exc
    if isinstance(current, float):
        try:
            return repr(float(raw_value))
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a number") from exc
    if isinstance(current, list):
        try:
            parsed = ast.literal_eval(raw_value)
        except (SyntaxError, ValueError) as exc:
            raise RuntimeError(f"{name} must be a TOML/Python-style list") from exc
        if not isinstance(parsed, (list, tuple)):
            raise RuntimeError(f"{name} must be a list")
        return json.dumps(list(parsed), ensure_ascii=False)
    return json.dumps(raw_value, ensure_ascii=False)


def _replace_toml_values(
    config_text: str,
    settings: dict[str, Any],
    legacy_values: dict[str, str | None],
) -> tuple[str, list[str], list[str]]:
    if not legacy_values.get("BOT_OWNER_ID") and legacy_values.get("OWNER_ID"):
        legacy_values["BOT_OWNER_ID"] = legacy_values["OWNER_ID"]

    moved: list[str] = []
    ignored: list[str] = []
    lines = config_text.splitlines(keepends=True)
    line_indexes: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = ASSIGNMENT_RE.match(line)
        if match:
            line_indexes[match.group("key")] = index

    for name, raw_value in legacy_values.items():
        if is_secret_env_name(name) or is_allowed_env_name(name) or name == "OWNER_ID":
            # Identity and secrets stay in .env — never write them into TOML.
            continue
        if name not in settings or name not in line_indexes:
            ignored.append(name)
            continue
        if raw_value is None:
            continue
        index = line_indexes[name]
        match = ASSIGNMENT_RE.match(lines[index])
        if match is None:
            raise RuntimeError(f"Cannot locate {name} in bot_config.toml")
        newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
        lines[index] = (
            f"{match.group('prefix')} "
            f"{_toml_value(name, raw_value, settings[name])}{newline}"
        )
        moved.append(name)
    return "".join(lines), moved, ignored


def _secret_lines(env_text: str) -> list[str]:
    found: dict[str, str] = {}
    for line in env_text.splitlines():
        match = ASSIGNMENT_RE.match(line)
        if not match:
            continue
        name = match.group("key")
        if not is_secret_env_name(name):
            continue
        if name in found:
            raise RuntimeError(f"Duplicate {name} in .env")
        found[name] = line
    for required in ("TOKEN", "DATABASE_URL"):
        found.setdefault(required, f"{required}=")
    return [found[name] for name in sorted(found)]


def migrate(env_path: Path, config_path: Path) -> tuple[int, list[str]]:
    if not env_path.is_file():
        raise RuntimeError(f"File not found: {env_path}")
    settings = dict(load_public_settings(config_path))
    env_text = env_path.read_text(encoding="utf-8-sig")
    legacy_values = dict(dotenv_values(env_path))
    config_text = config_path.read_text(encoding="utf-8")
    new_config, moved, ignored = _replace_toml_values(
        config_text, settings, legacy_values
    )

    # Validate the generated TOML before replacing either source file.
    config_tmp = config_path.with_suffix(config_path.suffix + ".migrating")
    env_tmp = env_path.with_suffix(env_path.suffix + ".migrating")
    try:
        config_tmp.write_text(new_config, encoding="utf-8", newline="\n")
        load_public_settings(config_tmp)
        env_tmp.write_text(
            "# GUINNESS BOT — only secrets; chmod 600 .env on Linux.\n"
            + "\n".join(_secret_lines(env_text))
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if os.name == "posix":
            env_tmp.chmod(0o600)
        config_tmp.replace(config_path)
        env_tmp.replace(env_path)
    finally:
        config_tmp.unlink(missing_ok=True)
        env_tmp.unlink(missing_ok=True)
    return len(moved), sorted(ignored)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move non-secret values from legacy .env to bot_config.toml"
    )
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--config", type=Path, default=Path("bot_config.toml"))
    args = parser.parse_args()
    moved_count, ignored = migrate(args.env.resolve(), args.config.resolve())
    print(f"Moved {moved_count} non-secret settings; .env now contains secrets only.")
    if ignored:
        print("Ignored obsolete/unknown settings: " + ", ".join(ignored))


if __name__ == "__main__":
    main()
