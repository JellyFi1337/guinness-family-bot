"""Stable Discord ``custom_id`` scheme for persistent Components V2 panels.

Format: ``g:<version>:<domain>:<action>:<object_id>:<params>``.
State belongs in the id, not in a process-local dictionary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Mapping

DISCORD_CUSTOM_ID_LIMIT: Final[int] = 100
CURRENT_VERSION: Final[int] = 1
PREFIX: Final[str] = "g"

_TOKEN = r"[a-z0-9_]+"
_OBJECT = r"[A-Za-z0-9_-]+"
_PARAM_KEY = r"[a-z][a-z0-9_]*"
_PARAM_VALUE = r"[A-Za-z0-9_.-]*"


class CustomIdError(ValueError):
    """The id is malformed, too long, or uses illegal characters."""


class StaleCustomId(CustomIdError):
    """The id is syntactically valid but the version is no longer served."""


@dataclass(frozen=True, slots=True)
class CustomId:
    version: int
    domain: str
    action: str
    object_id: str
    params: tuple[tuple[str, str], ...] = ()

    def param(self, key: str, default: str = "") -> str:
        for name, value in self.params:
            if name == key:
                return value
        return default

    def page(self) -> int:
        raw = self.param("p", "1")
        try:
            value = int(raw)
        except ValueError:
            return 1
        return value if value > 0 else 1

    def owner_id(self) -> int | None:
        raw = self.param("o")
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def used(self) -> bool:
        return self.param("u") == "1"

    def with_action(self, action: str) -> CustomId:
        return CustomId(
            version=self.version,
            domain=self.domain,
            action=action,
            object_id=self.object_id,
            params=self.params,
        )

    def with_params(self, **updates: str | int | None) -> CustomId:
        merged = dict(self.params)
        for key, value in updates.items():
            if value is None or value == "":
                merged.pop(key, None)
            else:
                merged[key] = str(value)
        return CustomId(
            version=self.version,
            domain=self.domain,
            action=self.action,
            object_id=self.object_id,
            params=tuple(sorted(merged.items())),
        )

    def render(self) -> str:
        return build_custom_id(
            domain=self.domain,
            action=self.action,
            object_id=self.object_id,
            params=dict(self.params),
            version=self.version,
        )


def build_custom_id(
    *,
    domain: str,
    action: str,
    object_id: str = "-",
    params: Mapping[str, str | int] | None = None,
    version: int = CURRENT_VERSION,
) -> str:
    """Assemble a ``custom_id``. Raises ``CustomIdError`` if it cannot be sent."""

    _check_token("domain", domain)
    _check_token("action", action)
    _check_object_id(object_id)
    if version < 1:
        raise CustomIdError("Версия custom_id должна быть положительным числом.")
    encoded = _encode_params(params or {})
    rendered = f"{PREFIX}:{version}:{domain}:{action}:{object_id}:{encoded}"
    if len(rendered) > DISCORD_CUSTOM_ID_LIMIT:
        raise CustomIdError(
            f"custom_id длиннее {DISCORD_CUSTOM_ID_LIMIT} символов Discord "
            f"({len(rendered)}): {rendered}"
        )
    return rendered


def parse_custom_id(raw: str, *, allow_stale: bool = False) -> CustomId:
    """Parse and validate a ``custom_id``. Unknown versions raise ``StaleCustomId``."""

    value = (raw or "").strip()
    parts = value.split(":", 5)
    if len(parts) != 6 or parts[0] != PREFIX:
        raise CustomIdError("custom_id не соответствует схеме g:<версия>:<домен>:<действие>:<id>:<параметры>.")
    try:
        version = int(parts[1])
    except ValueError as exc:
        raise CustomIdError("Версия custom_id должна быть числом.") from exc
    if version < 1:
        raise CustomIdError("Версия custom_id должна быть положительным числом.")
    domain, action, object_id, encoded = parts[2], parts[3], parts[4], parts[5]
    _check_token("domain", domain)
    _check_token("action", action)
    _check_object_id(object_id)
    params = _decode_params(encoded)
    parsed = CustomId(
        version=version,
        domain=domain,
        action=action,
        object_id=object_id,
        params=params,
    )
    if version != CURRENT_VERSION and not allow_stale:
        raise StaleCustomId(
            f"Версия custom_id {version} больше не обслуживается "
            f"(текущая {CURRENT_VERSION})."
        )
    return parsed


def _check_token(field: str, value: str) -> None:
    if not value or not _matches(_TOKEN, value) or len(value) > 20:
        raise CustomIdError(f"Поле {field} должно быть коротким токеном [a-z0-9_].")


def _check_object_id(value: str) -> None:
    if not value or not _matches(_OBJECT, value) or len(value) > 32:
        raise CustomIdError("id объекта должен быть [A-Za-z0-9_-], до 32 символов.")


def _encode_params(params: Mapping[str, str | int]) -> str:
    items: list[str] = []
    for key in sorted(params):
        if not _matches(_PARAM_KEY, key) or len(key) > 12:
            raise CustomIdError(f"Ключ параметра {key!r} недопустим.")
        value = str(params[key])
        if not _matches(_PARAM_VALUE, value) or len(value) > 40:
            raise CustomIdError(f"Значение параметра {key!r} недопустимо.")
        if value == "":
            continue
        items.append(f"{key}={value}")
    return "&".join(items)


def _decode_params(encoded: str) -> tuple[tuple[str, str], ...]:
    if not encoded:
        return ()
    parsed: list[tuple[str, str]] = []
    for chunk in encoded.split("&"):
        if "=" not in chunk:
            raise CustomIdError("Параметры custom_id должны быть парами key=value.")
        key, value = chunk.split("=", 1)
        if not _matches(_PARAM_KEY, key) or not _matches(_PARAM_VALUE, value):
            raise CustomIdError("Параметры custom_id содержат недопустимые символы.")
        parsed.append((key, value))
    return tuple(parsed)


def _matches(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern, value) is not None
