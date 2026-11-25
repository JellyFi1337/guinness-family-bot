from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import discord

from repositories.operations import (
    OutboxOperation,
    claim_operations,
    complete_operation,
    consume_operation_wake,
    retry_operation,
    wake_operation_worker,
)
from services.metrics import metrics


logger = logging.getLogger(__name__)
Handler = Callable[[OutboxOperation], Awaitable[str | int | None]]


class OperationWorker:
    """Forward-recovery worker for Discord side effects recorded in SQLite."""

    def __init__(
        self,
        *,
        max_attempts: int = 10,
        concurrency: int = 6,
        fallback_probe_seconds: float = 3.0,
    ) -> None:
        self.max_attempts = max(1, max_attempts)
        self.concurrency = max(1, min(int(concurrency), 16))
        self.fallback_probe_seconds = max(0.5, float(fallback_probe_seconds))
        self._handlers: dict[str, Handler] = {}
        self._last_probe = 0.0

    def register(self, kind: str, handler: Handler) -> None:
        if kind in self._handlers:
            raise ValueError(f"Outbox handler {kind!r} is already registered")
        self._handlers[kind] = handler

    def unregister(self, kind: str) -> None:
        self._handlers.pop(kind, None)

    async def _run_claimed(self, operation: OutboxOperation) -> None:
        handler = self._handlers.get(operation.kind)
        if handler is None:
            await retry_operation(
                operation.id,
                RuntimeError(f"No handler for {operation.kind}"),
                delay_seconds=300,
                dead=True,
            )
            return
        try:
            external_id = await handler(operation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, discord.HTTPException):
                metrics.increment("discord.http_errors")
            dead = operation.attempts >= self.max_attempts
            delay = min(3_600.0, (2 ** min(operation.attempts, 10)) + random.random() * 3)
            await retry_operation(
                operation.id,
                exc,
                delay_seconds=delay,
                dead=dead,
            )
            logger.exception(
                "Outbox operation failed id=%s kind=%s attempt=%s dead=%s",
                operation.id,
                operation.kind,
                operation.attempts,
                dead,
            )
        else:
            await complete_operation(operation.id, external_id)

    async def run_batch(
        self, *, max_items: int = 25, max_seconds: float = 2.0
    ) -> int:
        """Drain a bounded burst without monopolising the event loop."""

        now = time.monotonic()
        if (
            not consume_operation_wake()
            and now - self._last_probe < self.fallback_probe_seconds
        ):
            return 0
        self._last_probe = now
        claim_limit = max(1, min(int(max_items), 100))
        operations = await claim_operations(
            limit=claim_limit,
            kinds=tuple(self._handlers),
        )
        if not operations:
            return 0
        metrics.increment("outbox.claimed", len(operations))
        for operation in operations:
            try:
                created_at = datetime.fromisoformat(operation.created_at)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                metrics.observe(
                    "outbox.age_ms",
                    max(
                        0.0,
                        (datetime.now(UTC) - created_at).total_seconds() * 1_000,
                    ),
                )
            except ValueError:
                metrics.increment("outbox.invalid_timestamp")

        # Operations for one aggregate retain strict ordering. Independent
        # aggregates may progress concurrently while Discord applies its own
        # route/global rate limits.
        groups: dict[tuple[int, str, str], list[OutboxOperation]] = defaultdict(list)
        for operation in operations:
            aggregate = operation.aggregate_id or f"operation:{operation.id}"
            groups[(operation.guild_id, operation.kind, aggregate)].append(operation)

        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_group(items: list[OutboxOperation]) -> None:
            async with semaphore:
                for operation in items:
                    await self._run_claimed(operation)

        # The supervisor owns the hard timeout. Cancelling a Discord side
        # effect after the old two-second soft budget would leave it leased
        # until the reaper and could duplicate an externally completed call.
        del max_seconds
        await asyncio.gather(*(run_group(items) for items in groups.values()))
        metrics.increment("outbox.processed", len(operations))
        if len(operations) >= claim_limit:
            # A full claim strongly suggests more ready work. Keep the next
            # supervisor tick hot instead of waiting for the fallback probe.
            wake_operation_worker()
        return len(operations)
