from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from core.log_context import (
    current_reference,
    notify_critical_error,
    sanitize_text,
    task_context,
)
from infrastructure.database import DATABASE_OPERATIONAL_ERRORS
from repositories.operations import update_task_health


logger = logging.getLogger(__name__)
Worker = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class _Spec:
    name: str
    worker: Worker
    interval: float
    timeout: float
    jitter: float
    wait_until_ready: bool


class TaskSupervisor:
    """Owns restartable background jobs with backoff and persisted health."""

    def __init__(self, bot, *, health_interval: float = 60.0) -> None:
        self.bot = bot
        self.health_interval = max(15.0, health_interval)
        self._tasks: dict[str, asyncio.Task] = {}
        self._specs: dict[str, _Spec] = {}
        self._enabled = asyncio.Event()
        self._enabled.set()
        self._active = 0
        self._active_condition = asyncio.Condition()
        self._closed = False

    def start_periodic(
        self,
        name: str,
        worker: Worker,
        *,
        interval: float,
        timeout: float | None = None,
        jitter: float = 0.1,
        wait_until_ready: bool = True,
    ) -> None:
        if self._closed:
            raise RuntimeError("Task supervisor is closed")
        if name in self._tasks and not self._tasks[name].done():
            raise ValueError(f"Background task {name!r} is already running")
        spec = _Spec(
            name=name,
            worker=worker,
            interval=max(0.05, float(interval)),
            timeout=max(1.0, float(timeout or max(interval, 30.0))),
            jitter=max(0.0, min(float(jitter), 0.5)),
            wait_until_ready=wait_until_ready,
        )
        self._specs[name] = spec
        self._tasks[name] = asyncio.create_task(
            self._run(spec), name=f"guinness:{name}"
        )

    async def stop_task(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        self._specs.pop(name, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _write_health(self, name: str, state: str, **kwargs) -> None:
        try:
            await update_task_health(name, state, **kwargs)
        except Exception:
            # Health persistence must never become the reason a worker dies.
            logger.warning("Could not persist health for task %s", name, exc_info=True)

    async def _run(self, spec: _Spec) -> None:
        if spec.wait_until_ready:
            await self.bot.wait_until_ready()
        failures = 0
        last_health_write = 0.0
        while not self._closed and not self.bot.is_closed():
            # Admission and maintenance use the same condition. This closes a
            # race where a worker could pass Event.wait(), then begin after a
            # destructive maintenance operation had already observed zero
            # active jobs.
            async with self._active_condition:
                await self._active_condition.wait_for(
                    lambda: self._enabled.is_set() or self._closed
                )
                if self._closed:
                    return
                self._active += 1
            started = datetime.now(UTC).isoformat()
            started_clock = time.monotonic()
            try:
                with task_context(spec.name):
                    async with asyncio.timeout(spec.timeout):
                        await spec.worker()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                duration = int((time.monotonic() - started_clock) * 1_000)
                await self._write_health(
                    spec.name,
                    "degraded",
                    last_started=started,
                    last_failure=datetime.now(UTC).isoformat(),
                    consecutive_failures=failures,
                    last_error=f"{type(exc).__name__}: {sanitize_text(exc)}",
                    duration_ms=duration,
                )
                with task_context(spec.name):
                    logger.exception(
                        "Background task %s failed (%d consecutive failures) reference=%s",
                        spec.name,
                        failures,
                        current_reference(),
                    )
                    alert_key = (
                        "db:failure"
                        if isinstance(exc, DATABASE_OPERATIONAL_ERRORS)
                        else f"task:{spec.name}"
                    )
                    await notify_critical_error(
                        self.bot,
                        key=alert_key,
                        title="Фоновая задача завершилась ошибкой",
                        summary=f"Задача `{spec.name}` упала.",
                        detail=(
                            f"type={type(exc).__name__} task={spec.name} "
                            f"failures={failures}"
                        ),
                        next_step="Проверьте журнал и /dev health.",
                    )
                delay = min(300.0, max(spec.interval, 2 ** min(failures, 8)))
            else:
                duration = int((time.monotonic() - started_clock) * 1_000)
                now_clock = time.monotonic()
                should_write = failures > 0 or now_clock - last_health_write >= self.health_interval
                failures = 0
                if should_write:
                    await self._write_health(
                        spec.name,
                        "healthy",
                        last_started=started,
                        last_success=datetime.now(UTC).isoformat(),
                        consecutive_failures=0,
                        duration_ms=duration,
                    )
                    last_health_write = now_clock
                delay = spec.interval
            finally:
                async with self._active_condition:
                    self._active -= 1
                    self._active_condition.notify_all()
            spread = delay * spec.jitter
            await asyncio.sleep(max(0.05, delay + random.uniform(-spread, spread)))

    @asynccontextmanager
    async def maintenance(self):
        """Pause new jobs and wait for active jobs before destructive maintenance."""

        async with self._active_condition:
            self._enabled.clear()
            await self._active_condition.wait_for(lambda: self._active == 0)
        try:
            yield
        finally:
            async with self._active_condition:
                self._enabled.set()
                self._active_condition.notify_all()

    def snapshot(self) -> dict[str, object]:
        return {
            "paused": self.paused,
            "active": self._active,
            "jobs": {
                name: "stopped" if task.done() else "running"
                for name, task in self._tasks.items()
            },
        }

    @property
    def paused(self) -> bool:
        return not self._enabled.is_set()

    async def close(self) -> None:
        self._closed = True
        async with self._active_condition:
            self._enabled.set()
            self._active_condition.notify_all()
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        self._specs.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
