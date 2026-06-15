"""
Finch Telegram bridge — long-polling receiver that calls the local Finch API.

Telegram message -> Finch Telegram service -> Finch local API -> Telegram reply.
No grocery logic lives here; cart guardrails remain in finch/api.py.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

from finch.env_util import load_env
from finch_telegram import __version__, config, handler, telegram_client

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run_polling(*, once: bool = False) -> None:
    load_env(force=True)
    config.validate_startup()
    logger.info("Finch Telegram bridge v%s starting (long polling)", __version__)

    stop = False

    def _handle_stop(signum: int, _frame: object) -> None:
        nonlocal stop
        logger.info("Received signal %s; stopping Telegram polling", signum)
        stop = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    next_offset: int | None = None
    while not stop:
        try:
            updates = telegram_client.get_updates(next_offset)
        except telegram_client.TelegramApiError as exc:
            logger.error("Telegram getUpdates failed (status=%s)", exc.status_code)
            if once:
                raise
            time.sleep(5)
            continue

        for message in telegram_client.extract_text_messages(updates):
            next_offset = message.update_id + 1
            logger.info("Processing Telegram command for user_id=%s", message.user_id)
            handler.process_inbound(message)

        if updates:
            last_update_id = updates[-1].get("update_id")
            if isinstance(last_update_id, int):
                next_offset = last_update_id + 1

        if once:
            break

    logger.info("Finch Telegram bridge stopped")


def main() -> None:
    _configure_logging()
    try:
        run_polling()
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
