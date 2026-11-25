import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from services.task_supervisor import TaskSupervisor


class _Bot:
    def __init__(self):
        self.closed = False

    async def wait_until_ready(self):
        return None

    def is_closed(self):
        return self.closed


class TaskSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_worker_recovers_after_failure_and_reports_health(self):
        bot = _Bot()
        supervisor = TaskSupervisor(bot)
        recovered = asyncio.Event()
        attempts = 0
        real_sleep = asyncio.sleep

        async def worker():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary failure")
            recovered.set()
            bot.closed = True

        async def fast_sleep(_delay):
            await real_sleep(0)

        health = AsyncMock()
        try:
            with (
                patch("services.task_supervisor.update_task_health", health),
                patch("services.task_supervisor.asyncio.sleep", side_effect=fast_sleep),
                patch("services.task_supervisor.random.uniform", return_value=0.0),
            ):
                supervisor.start_periodic(
                    "recoverable",
                    worker,
                    interval=0.05,
                    jitter=0,
                    wait_until_ready=False,
                )
                await asyncio.wait_for(recovered.wait(), timeout=1)
                await real_sleep(0)
        finally:
            await supervisor.close()

        self.assertEqual(attempts, 2)
        states = [call.args[1] for call in health.await_args_list]
        self.assertEqual(states, ["degraded", "healthy"])
        degraded = health.await_args_list[0]
        self.assertEqual(degraded.kwargs["consecutive_failures"], 1)
        self.assertIn("RuntimeError: temporary failure", degraded.kwargs["last_error"])
        self.assertEqual(
            health.await_args_list[1].kwargs["consecutive_failures"], 0
        )

    async def test_maintenance_waits_for_active_worker_and_pauses_next_run(self):
        bot = _Bot()
        supervisor = TaskSupervisor(bot)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def worker():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        with patch.object(supervisor, "_write_health", AsyncMock()):
            supervisor.start_periodic(
                "maintenance-aware",
                worker,
                interval=0.05,
                jitter=0,
                wait_until_ready=False,
            )
            await asyncio.wait_for(started.wait(), timeout=1)

            entered = asyncio.Event()

            async def maintenance():
                async with supervisor.maintenance():
                    entered.set()
                    await asyncio.sleep(0.1)

            maintenance_task = asyncio.create_task(maintenance())
            await asyncio.sleep(0)
            self.assertFalse(entered.is_set())
            release.set()
            await asyncio.wait_for(entered.wait(), timeout=1)
            paused_calls = calls
            await asyncio.sleep(0.07)
            self.assertEqual(calls, paused_calls)
            await maintenance_task
            await supervisor.close()


if __name__ == "__main__":
    unittest.main()
