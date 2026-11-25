import asyncio
import unittest

from utils.locks import KeyedLockPool


class KeyedLockPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_is_serialized_and_released(self):
        pool = KeyedLockPool[int]()
        active = 0
        peak = 0

        async def worker():
            nonlocal active, peak
            async with pool.lock(1):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0)
                active -= 1

        await asyncio.gather(*(worker() for _ in range(20)))
        self.assertEqual(peak, 1)
        self.assertEqual(len(pool), 0)

    async def test_different_keys_can_run_concurrently(self):
        pool = KeyedLockPool[int]()
        both_started = asyncio.Event()
        active = 0

        async def worker(key: int):
            nonlocal active
            async with pool.lock(key):
                active += 1
                if active == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=1)

        await asyncio.gather(worker(1), worker(2))
        self.assertEqual(len(pool), 0)
