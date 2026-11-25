import unittest
from unittest.mock import AsyncMock, patch

import main


class MainShutdownTests(unittest.TestCase):
    def test_keyboard_interrupt_is_logged_without_traceback(self):
        interrupted = AsyncMock(side_effect=KeyboardInterrupt)

        with (
            patch.object(main, "main", interrupted),
            patch.object(main.logger, "info") as log,
        ):
            main.run()

        interrupted.assert_awaited_once()
        log.assert_called_once()

    def test_real_startup_error_is_not_suppressed(self):
        failed = AsyncMock(side_effect=RuntimeError("startup failed"))

        with patch.object(main, "main", failed):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                main.run()


if __name__ == "__main__":
    unittest.main()
