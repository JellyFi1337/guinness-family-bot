import asyncio
from dataclasses import dataclass
from typing import Generic, Hashable, TypeVar


KeyT = TypeVar("KeyT", bound=Hashable)


@dataclass
class _Entry:
    lock: asyncio.Lock
    users: int = 0


class _Lease(Generic[KeyT]):
    def __init__(self, pool: "KeyedLockPool[KeyT]", key: KeyT) -> None:
        self._pool = pool
        self._key = key
        self._entry: _Entry | None = None

    async def acquire(self) -> bool:
        if self._entry is not None:
            raise RuntimeError("Lock lease is already acquired")
        entry = self._pool._entries.get(self._key)
        if entry is None:
            entry = _Entry(asyncio.Lock())
            self._pool._entries[self._key] = entry
        entry.users += 1
        self._entry = entry
        try:
            await entry.lock.acquire()
        except BaseException:
            self._entry = None
            self._pool._release_entry(self._key, entry)
            raise
        return True

    def release(self) -> None:
        entry = self._entry
        if entry is None or not entry.lock.locked():
            raise RuntimeError("Lock lease is not acquired")
        self._entry = None
        entry.lock.release()
        self._pool._release_entry(self._key, entry)

    async def __aenter__(self) -> "_Lease[KeyT]":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.release()


class KeyedLockPool(Generic[KeyT]):
    """Serialize operations per key without retaining every historical key."""

    def __init__(self) -> None:
        self._entries: dict[KeyT, _Entry] = {}

    def lock(self, key: KeyT) -> _Lease[KeyT]:
        return _Lease(self, key)

    def hold(self, key: KeyT) -> _Lease[KeyT]:
        """Readable alias used by service-level critical sections."""

        return self.lock(key)

    def _release_entry(self, key: KeyT, entry: _Entry) -> None:
        entry.users -= 1
        if entry.users == 0 and self._entries.get(key) is entry:
            self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)
