from __future__ import annotations

import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass

from config import COMMAND_COOLDOWN_DEFAULTS
from repositories.cooldowns import (
    get_command_cooldown,
    set_command_cooldown,
)

_COOLDOWN_ALIASES = {
    "event_create": "summit_vzp",
    "contract_start": "summit_contract",
}


@dataclass(frozen=True, slots=True)
class CooldownResult:
    allowed: bool
    retry_after: float = 0.0


class CommandCooldowns:
    """Guild-configurable, process-local rate limits.

    Durations are durable in ``guild_settings``. Hits deliberately remain
    in-memory: they are short-lived operational state and must not turn every
    button press into a database write.
    """

    def __init__(self, *, max_hits: int = 50_000) -> None:
        self._durations: dict[tuple[int, str], int] = {}
        self._hits: OrderedDict[tuple[int, str, int], float] = OrderedDict()
        self._bypass_once: set[tuple[int, str, int]] = set()
        self._bypass_until: dict[tuple[int, str, int], float] = {}
        self._max_hits = max(1_000, int(max_hits))

    async def duration(self, guild_id: int, command_name: str) -> int:
        key = (guild_id, command_name)
        cached = self._durations.get(key)
        if cached is not None:
            return cached
        if command_name not in COMMAND_COOLDOWN_DEFAULTS:
            raise ValueError(f"Unsupported cooldown: {command_name}")
        stored = await get_command_cooldown(guild_id, command_name)
        if stored is None:
            alias = _COOLDOWN_ALIASES.get(command_name)
            if alias is not None:
                stored = await get_command_cooldown(guild_id, alias)
        duration = (
            COMMAND_COOLDOWN_DEFAULTS[command_name]
            if stored is None
            else stored
        )
        self._durations[key] = duration
        return duration

    async def acquire(
        self,
        guild_id: int,
        command_name: str,
        user_id: int,
        *,
        bypass: bool = False,
    ) -> CooldownResult:
        if bypass:
            return CooldownResult(True)
        now = time.monotonic()
        key = (guild_id, command_name, user_id)
        bypass_until = self._bypass_until.get(key)
        if bypass_until is not None:
            if bypass_until > now:
                return CooldownResult(True)
            self._bypass_until.pop(key, None)
        if key in self._bypass_once:
            self._bypass_once.discard(key)
            self._hits[key] = now
            self._hits.move_to_end(key)
            return CooldownResult(True)
        duration = await self.duration(guild_id, command_name)
        if duration <= 0:
            return CooldownResult(True)

        previous = self._hits.get(key)
        if previous is not None:
            retry_after = duration - (now - previous)
            if retry_after > 0:
                self._hits.move_to_end(key)
                return CooldownResult(False, retry_after)
        self._hits[key] = now
        self._hits.move_to_end(key)
        while len(self._hits) > self._max_hits:
            self._hits.popitem(last=False)
        return CooldownResult(True)

    async def configure(
        self,
        guild_id: int,
        command_name: str,
        seconds: int,
    ) -> None:
        if command_name not in COMMAND_COOLDOWN_DEFAULTS:
            raise ValueError(f"Unsupported cooldown: {command_name}")
        seconds = max(0, int(seconds))
        await set_command_cooldown(guild_id, command_name, seconds)
        self._durations[(guild_id, command_name)] = seconds
        for key in tuple(self._hits):
            if key[0] == guild_id and key[1] == command_name:
                self._hits.pop(key, None)

    def release(self, guild_id: int, command_name: str, user_id: int) -> None:
        """Release a claim when the protected operation did not complete."""
        self._hits.pop((guild_id, command_name, user_id), None)

    def grant_once(self, guild_id: int, command_name: str, user_id: int) -> None:
        if command_name not in COMMAND_COOLDOWN_DEFAULTS:
            raise ValueError(f"Unsupported cooldown: {command_name}")
        key = (guild_id, command_name, user_id)
        self._bypass_until.pop(key, None)
        self._bypass_once.add(key)
        self._hits.pop(key, None)

    def grant_for(
        self,
        guild_id: int,
        command_name: str,
        user_id: int,
        seconds: int,
    ) -> None:
        if command_name not in COMMAND_COOLDOWN_DEFAULTS:
            raise ValueError(f"Unsupported cooldown: {command_name}")
        key = (guild_id, command_name, user_id)
        self._bypass_once.discard(key)
        self._bypass_until[key] = time.monotonic() + max(1, int(seconds))
        self._hits.pop(key, None)

    def restore(self, guild_id: int, command_name: str, user_id: int) -> bool:
        key = (guild_id, command_name, user_id)
        removed = key in self._bypass_once or key in self._bypass_until
        self._bypass_once.discard(key)
        self._bypass_until.pop(key, None)
        self._hits.pop(key, None)
        return removed

    def reset_for_tests(self) -> None:
        self._durations.clear()
        self._hits.clear()
        self._bypass_once.clear()
        self._bypass_until.clear()


command_cooldowns = CommandCooldowns()


class CooldownClaim:
    """A held cooldown slot that is returned when the operation does not run."""

    __slots__ = ("allowed", "retry_after", "_key", "_released")

    def __init__(self, result: CooldownResult, key: tuple[int, str, int]) -> None:
        self.allowed = result.allowed
        self.retry_after = result.retry_after
        self._key = key
        self._released = False

    def release(self) -> None:
        """Give the slot back — the protected operation never happened."""

        if self._released or not self.allowed:
            return
        self._released = True
        command_cooldowns.release(*self._key)

    def keep(self) -> None:
        """Consume the slot explicitly; the operation ran."""

        self._released = True


@asynccontextmanager
async def cooldown_claim(
    guild_id: int,
    command_name: str,
    user_id: int,
    *,
    bypass: bool = False,
):
    """Hold a cooldown slot only for as long as the operation might still run.

    ``acquire`` alone burns the slot the moment it is called. Commands that then
    returned early — a missing channel, a Discord error — charged the user 30 or
    300 seconds for the bot's own misconfiguration. The slot is released
    automatically unless the body calls :meth:`CooldownClaim.keep`, which every
    successful path should do once the durable change is committed.

    Usage::

        async with cooldown_claim(guild_id, "event_create", user_id) as claim:
            if not claim.allowed:
                ...  # tell the user how long is left
                return
            ...  # do the work
            claim.keep()
    """

    result = await command_cooldowns.acquire(
        guild_id, command_name, user_id, bypass=bypass
    )
    claim = CooldownClaim(result, (guild_id, command_name, user_id))
    if bypass:
        # A bypassed claim never occupied a slot, so there is nothing to return.
        claim.keep()
    try:
        yield claim
    except BaseException:
        claim.release()
        raise
    else:
        claim.release()


def cooldown_retry_text(retry_after: float) -> str:
    seconds = max(1, int(retry_after + 0.999))
    minutes, remaining = divmod(seconds, 60)
    if minutes and remaining:
        duration = f"{minutes} мин. {remaining} сек."
    elif minutes:
        duration = f"{minutes} мин."
    else:
        duration = f"{remaining} сек."
    return f"Повторите действие через **{duration}**"
