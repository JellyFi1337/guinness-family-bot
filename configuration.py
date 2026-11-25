from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from dotenv import dotenv_values


# Secrets and Discord identity that belong in .env (never in bot_config.toml).
SECRET_ENV_KEYS = frozenset(
    {
        "TOKEN",
        "TOKEN_TEST",
        "TOKEN_MAIN",
        "DATABASE_URL",
        "MAJESTIC_API_KEY",
    }
)
# Identity / process switches — .env only, forbidden in bot_config.toml.
PROFILE_ENV_KEYS = frozenset(
    {
        "BOT_PROFILE",
        "GUILD_ID",
        "GUILD_ID_TEST",
        "GUILD_ID_MAIN",
        "BOT_OWNER_ID",
        "COMMAND_SYNC_FORCE",
        "COMMAND_CLEAR_GLOBAL",
    }
)
ALLOWED_ENV_KEYS = SECRET_ENV_KEYS | PROFILE_ENV_KEYS
# Keys that must not appear in public TOML (identity lives in .env).
TOML_FORBIDDEN_ENV_KEYS = frozenset(PROFILE_ENV_KEYS | SECRET_ENV_KEYS)

_SECRET_NAME_PARTS = (
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "SECRET",
    "PRIVATE_KEY",
    "API_KEY",
    "DATABASE_URL",
)

_PROFILE_ALIASES = {
    "test": "test",
    "dev": "test",
    "development": "test",
    "main": "main",
    "family": "main",
    "prod": "main",
    "production": "main",
}


def _flatten_toml(
    value: Mapping[str, Any],
    *,
    prefix: str = "",
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    flattened = {} if result is None else result
    for key, item in value.items():
        location = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping):
            _flatten_toml(item, prefix=location, result=flattened)
            continue
        if key in flattened:
            raise RuntimeError(
                f"Duplicate setting {key!r} in bot_config.toml ({location})"
            )
        flattened[key] = item
    return flattened


def _looks_secret(name: str) -> bool:
    normalized = name.upper()
    return any(part in normalized for part in _SECRET_NAME_PARTS)


def is_secret_env_name(name: str) -> bool:
    normalized = name.strip().upper()
    return normalized in SECRET_ENV_KEYS or _looks_secret(normalized)


def is_allowed_env_name(name: str) -> bool:
    normalized = name.strip().upper()
    return normalized in ALLOWED_ENV_KEYS or is_secret_env_name(normalized)


def load_public_settings(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise RuntimeError(
            f"Configuration file not found: {path}. "
            "Restore the tracked bot_config.toml file from Git."
        )
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"Invalid TOML in {path}: {exc}") from exc

    settings = _flatten_toml(document)
    secret_names = sorted(name for name in settings if _looks_secret(name))
    if secret_names:
        joined = ", ".join(secret_names)
        raise RuntimeError(
            f"Secrets are forbidden in {path.name}: {joined}. "
            "Store them in .env instead."
        )
    forbidden = sorted(
        name for name in settings if name.upper() in TOML_FORBIDDEN_ENV_KEYS
    )
    if forbidden:
        joined = ", ".join(forbidden)
        raise RuntimeError(
            f"Identity settings are forbidden in {path.name}: {joined}. "
            "Set BOT_PROFILE, GUILD_ID_TEST / GUILD_ID_MAIN, BOT_OWNER_ID, "
            "COMMAND_SYNC_FORCE and COMMAND_CLEAR_GLOBAL in .env."
        )
    return MappingProxyType(settings)


def _validate_env_permissions(path: Path) -> None:
    if os.name != "posix" or not path.exists():
        return
    # Docker Desktop bind-mounts from Windows often appear as 0777 and cannot
    # be chmod'd inside the container. Host Ubuntu installs still enforce 600.
    if Path("/.dockerenv").is_file():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError(
            f"{path} permissions are too open ({mode:#o}); "
            f"run: chmod 600 {path}"
        )


def load_secret_env(path: Path) -> None:
    """Load `.env` with priority over process env for non-empty allowed keys.

    Allowed: Discord tokens (TOKEN / TOKEN_TEST / TOKEN_MAIN), DATABASE_URL,
    MAJESTIC_API_KEY, BOT_PROFILE, GUILD_ID(_TEST|_MAIN), BOT_OWNER_ID.
    Channel/role settings stay in bot_config.toml.

    Empty `.env` entries do not clear a non-empty process value (Compose DSN
    parts, unit-test stubs). Non-empty `.env` values always win.
    """

    if not path.exists():
        return
    _validate_env_permissions(path)
    parsed = dotenv_values(path)
    unexpected = sorted(
        key for key in parsed if key and not is_allowed_env_name(key)
    )
    if unexpected:
        joined = ", ".join(unexpected)
        raise RuntimeError(
            f"{path.name} may contain only Discord tokens, BOT_PROFILE, "
            f"GUILD_ID(_TEST|_MAIN), BOT_OWNER_ID, COMMAND_SYNC_FORCE, "
            f"COMMAND_CLEAR_GLOBAL, DATABASE_URL, MAJESTIC_API_KEY and "
            f"similar secrets; move channel/role settings to "
            f"bot_config.toml: {joined}"
        )
    for key, raw in parsed.items():
        if not key:
            continue
        value = "" if raw is None else str(raw).strip()
        existing = os.getenv(key, "").strip()
        if not value and existing:
            continue
        os.environ[key] = value


def _read_secret_file(path_env: str) -> str | None:
    """Read a Docker-style secret file. Never logs the file contents.

    Empty or missing path → None (caller may fall back to .env). An empty
    mounted secret must not abort startup when TOKEN already comes from .env.
    """

    raw = os.getenv(path_env, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise RuntimeError(
            f"{path_env} is set but the file does not exist: {path}"
        )
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"{path_env} could not be read: {path}") from exc
    if not value:
        return None
    return value


def _apply_named_secret_file(env_name: str) -> None:
    """Load ``{env_name}_FILE`` only when ``env_name`` is not already set.

    `.env` / resolved profile tokens have priority over Docker secret files.
    """

    if os.getenv(env_name, "").strip():
        return
    file_env = f"{env_name}_FILE"
    value = _read_secret_file(file_env)
    if value is not None:
        os.environ[env_name] = value


def _apply_database_url_from_files() -> None:
    """Resolve DATABASE_URL from a full URL file or password + DSN parts.

    Non-secret DSN parts (user/host/port/name) come from process environment —
    compose ``environment:``, not ``/app/.env``.
    The password is always URL-encoded.

    When Compose sets ``DATABASE_HOST`` + ``DATABASE_PASSWORD_FILE``, that DSN
    wins over a host-only ``DATABASE_URL`` from the mounted `.env` (127.0.0.1
    is unreachable inside the bot container).
    """

    compose_host = os.getenv("DATABASE_HOST", "").strip()
    compose_password_file = os.getenv("DATABASE_PASSWORD_FILE", "").strip()
    force_compose_dsn = bool(compose_host and compose_password_file)

    if not force_compose_dsn and os.getenv("DATABASE_URL", "").strip():
        return

    url = _read_secret_file("DATABASE_URL_FILE")
    if url is not None and not force_compose_dsn:
        os.environ["DATABASE_URL"] = url
        return

    password = _read_secret_file("DATABASE_PASSWORD_FILE")
    if password is None:
        return

    user = os.getenv("DATABASE_USER", "").strip()
    host = compose_host
    name = os.getenv("DATABASE_NAME", "").strip()
    port = os.getenv("DATABASE_PORT", "5432").strip() or "5432"
    missing = [
        label
        for label, value in (
            ("DATABASE_USER", user),
            ("DATABASE_HOST", host),
            ("DATABASE_NAME", name),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "DATABASE_PASSWORD_FILE is set but DSN parts are incomplete: "
            + ", ".join(missing)
        )
    os.environ["DATABASE_URL"] = (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{name}"
    )


def apply_file_secrets() -> None:
    """Load container secret files into the process environment.

    Preferred in Docker: ``TOKEN_FILE``, ``MAJESTIC_API_KEY_FILE``, and either
    ``DATABASE_URL_FILE`` or ``DATABASE_PASSWORD_FILE`` plus
    ``DATABASE_USER`` / ``DATABASE_HOST`` / ``DATABASE_NAME`` / ``DATABASE_PORT``.

    Values already present from `.env` are not overwritten. Empty secret files
    are ignored so a mounted `.env` can supply TOKEN_TEST / TOKEN_MAIN.
    """

    _apply_named_secret_file("TOKEN")
    _apply_named_secret_file("TOKEN_TEST")
    _apply_named_secret_file("TOKEN_MAIN")
    _apply_named_secret_file("MAJESTIC_API_KEY")
    _apply_database_url_from_files()


def normalize_bot_profile(raw: str | None) -> str:
    value = (raw or "test").strip().lower() or "test"
    profile = _PROFILE_ALIASES.get(value)
    if profile is None:
        raise RuntimeError(
            f"BOT_PROFILE must be 'test' or 'main' "
            f"(aliases: family, prod, dev), got {raw!r}"
        )
    return profile


def resolve_bot_profile() -> str:
    """Select active TOKEN and GUILD_ID from BOT_PROFILE.

    ``BOT_PROFILE=test`` → TOKEN_TEST + GUILD_ID_TEST
    ``BOT_PROFILE=main`` → TOKEN_MAIN + GUILD_ID_MAIN (family production)

    Token fallback: plain TOKEN if the profile-specific key is empty.
    Guild ID must be set for the active profile (or legacy GUILD_ID) so a test
    bot cannot silently attach to the family guild from bot_config.toml.
    """

    profile = normalize_bot_profile(os.getenv("BOT_PROFILE"))
    token_key = "TOKEN_TEST" if profile == "test" else "TOKEN_MAIN"
    guild_key = "GUILD_ID_TEST" if profile == "test" else "GUILD_ID_MAIN"

    token = os.getenv(token_key, "").strip() or os.getenv("TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            f"No Discord token for BOT_PROFILE={profile}. "
            f"Set {token_key}=... in .env (or legacy TOKEN=)."
        )
    os.environ["TOKEN"] = token
    os.environ["BOT_PROFILE"] = profile

    guild = os.getenv(guild_key, "").strip() or os.getenv("GUILD_ID", "").strip()
    if not guild:
        raise RuntimeError(
            f"No guild for BOT_PROFILE={profile}. "
            f"Set {guild_key}=... in .env (Discord Server ID)."
        )
    os.environ["GUILD_ID"] = guild

    return profile
