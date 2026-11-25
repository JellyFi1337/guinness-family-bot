import asyncio
import logging
import os
import selectors

from config import LOG_DIR, LOG_FORMAT, LOG_LEVEL, LOG_LOGGER_LEVELS, TOKEN
from core.bot import GuinnessBot
from core.logging_setup import configure_logging

configure_logging(
    level=LOG_LEVEL,
    log_format=LOG_FORMAT,
    log_dir=LOG_DIR,
    logger_levels=LOG_LOGGER_LEVELS,
)
logger = logging.getLogger(__name__)


def _new_event_loop() -> asyncio.AbstractEventLoop:
    """Use the socket-capable loop required by psycopg on Windows."""

    if os.name == "nt":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("TOKEN is not configured. Add it to the .env file.")

    async with GuinnessBot() as bot:
        await bot.start(TOKEN)


def run() -> None:
    """Run the bot and treat an operator interrupt as a normal shutdown."""

    try:
        with asyncio.Runner(loop_factory=_new_event_loop) as runner:
            runner.run(main())
    except KeyboardInterrupt:
        # asyncio.Runner first cancels the Discord gateway task and then raises
        # KeyboardInterrupt to the synchronous caller. The cancellation is an
        # expected part of Ctrl+C/systemd SIGINT shutdown, not an application
        # failure, so keep the journal clean while preserving real exceptions.
        logger.info("Получен сигнал остановки. Бот завершил работу штатно.")


if __name__ == "__main__":
    run()
