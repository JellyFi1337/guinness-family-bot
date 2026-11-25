import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs import car_park


class CarParkMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_uses_the_shared_static_message_service(self):
        channel = SimpleNamespace(id=500)
        guild = SimpleNamespace(id=1, me=SimpleNamespace(id=77))
        canonical = SimpleNamespace(id=10)
        upsert = AsyncMock(return_value=canonical)

        with (
            patch.object(car_park, "get_text_channel", return_value=channel),
            patch.object(car_park, "upsert_static_message", upsert),
            patch.object(car_park, "set_setting", AsyncMock()) as set_setting,
        ):
            result = await car_park.update_car_list_message(guild)

        self.assertIs(result, canonical)
        self.assertEqual(upsert.await_args.kwargs["panel_key"], "car-park")
        self.assertEqual(
            upsert.await_args.kwargs["legacy_setting_key"],
            car_park.CAR_MESSAGE_KEY,
        )
        set_setting.assert_awaited_once_with(car_park.CAR_MESSAGE_KEY, "10")


if __name__ == "__main__":
    unittest.main()
