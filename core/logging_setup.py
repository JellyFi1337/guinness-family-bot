"""Process-wide logging: stdout first, optional files, never fail on LOG_DIR."""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from core.log_context import CONTEXT_FIELD_NAMES, ContextFilter

_TEXT_FORMAT: Final = (
    "%(asctime)s level=%(levelname)s logger=%(name)s "
    "correlation_id=%(correlation_id)s command=%(command)s "
    "component_id=%(component_id)s user_id=%(user_id)s user_name=%(user_name)s "
    "guild_id=%(guild_id)s channel_id=%(channel_id)s channel_name=%(channel_name)s "
    "cog=%(cog)s source=%(source)s msg=%(message)s"
)
_FORCED_WARNING_LOGGERS: Final[tuple[str, ...]] = (
    "discord.gateway",
    "discord.client",
)
_VALID_LEVELS: Final[frozenset[str]] = frozenset(
    {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
)


def _level_number(name: str) -> int:
    value = getattr(logging, name, None)
    return value if isinstance(value, int) else logging.INFO


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including context fields and traceback."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name in CONTEXT_FIELD_NAMES:
            payload[name] = getattr(record, name, "-")
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def parse_logger_levels(raw: str) -> dict[str, str]:
    levels: dict[str, str] = {
        "discord.gateway": "WARNING",
        "discord.client": "WARNING",
    }
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                f"LOG_LOGGER_LEVELS must be name=LEVEL pairs, got {item!r}"
            )
        name, level = item.split("=", 1)
        normalized = level.strip().upper()
        if normalized not in _VALID_LEVELS:
            raise ValueError(f"Unknown log level {level!r} for {name.strip()}")
        key = name.strip()
        if not key:
            raise ValueError("LOG_LOGGER_LEVELS contains an empty logger name")
        levels[key] = normalized
    for forced in _FORCED_WARNING_LOGGERS:
        current = levels.get(forced, "WARNING")
        if _level_number(current) < logging.WARNING:
            levels[forced] = "WARNING"
    return levels


def prepare_log_dir(raw: str) -> Path | None:
    """Return a writable directory or None. Never raises for a bad path."""

    text = raw.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        print(
            f"Каталог логов {path} недоступен для записи ({exc}). "
            "Бот продолжит работу через stdout. Код: LOG_DIR_UNWRITABLE.",
            file=sys.stderr,
        )
        return None
    return path


def build_logging_config(
    *,
    level: str = "INFO",
    log_format: str = "text",
    log_dir: Path | None = None,
    logger_levels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    formatter_name = "json" if log_format == "json" else "text"
    handlers: dict[str, Any] = {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": formatter_name,
            "filters": ["context"],
        }
    }
    root_handlers = ["console"]
    if log_dir is not None:
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_dir / "bot.log"),
            "maxBytes": 2_000_000,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": formatter_name,
            "filters": ["context"],
        }
        handlers["error_file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_dir / "bot.error.log"),
            "maxBytes": 2_000_000,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": formatter_name,
            "filters": ["context"],
            "level": "ERROR",
        }
        root_handlers.extend(("file", "error_file"))

    configured_levels = parse_logger_levels("")
    if logger_levels:
        configured_levels.update(dict(logger_levels))
        for forced in _FORCED_WARNING_LOGGERS:
            current = configured_levels.get(forced, "WARNING")
            if _level_number(current) < logging.WARNING:
                configured_levels[forced] = "WARNING"

    loggers = {
        name: {"level": value, "handlers": [], "propagate": True}
        for name, value in configured_levels.items()
    }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "context": {"()": ContextFilter},
        },
        "formatters": {
            "text": {"format": _TEXT_FORMAT},
            "json": {"()": JsonFormatter},
        },
        "handlers": handlers,
        "root": {"level": level, "handlers": root_handlers},
        "loggers": loggers,
    }


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "text",
    log_dir: str = "",
    logger_levels: str = "",
) -> Path | None:
    """Install dictConfig. File handlers are skipped when LOG_DIR is unusable."""

    normalized_format = log_format.strip().casefold()
    if normalized_format not in {"text", "json"}:
        raise ValueError("LOG_FORMAT must be 'text' or 'json'")
    normalized_level = level.strip().upper()
    if normalized_level not in _VALID_LEVELS:
        raise ValueError(f"Unknown LOG_LEVEL {level!r}")

    resolved_dir = prepare_log_dir(log_dir)
    levels = parse_logger_levels(logger_levels)
    logging.config.dictConfig(
        build_logging_config(
            level=normalized_level,
            log_format=normalized_format,
            log_dir=resolved_dir,
            logger_levels=levels,
        )
    )
    if log_dir.strip() and resolved_dir is None:
        logging.getLogger(__name__).warning(
            "Каталог логов недоступен, запись идёт только в stdout. "
            "Код: LOG_DIR_UNWRITABLE."
        )
    elif resolved_dir is not None:
        logging.getLogger(__name__).info("File logging enabled dir=%s", resolved_dir)
    return resolved_dir
