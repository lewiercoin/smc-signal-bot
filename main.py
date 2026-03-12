"""SMC Signal Bot — Entry Point."""

import asyncio
import os

import structlog

from bot.scheduler import SignalScheduler
from bot.telegram_bot import TelegramBot

log = structlog.get_logger()


async def main() -> None:
    log.info("bot_starting")

    bot = TelegramBot()
    app = bot.setup()
    scheduler = SignalScheduler(telegram_bot=bot)

    scheduler.start()

    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        log.info("running_webhook", url=webhook_url)
        await app.run_webhook(
            listen="0.0.0.0",
            port=8443,
            url_path="webhook",
            webhook_url=f"{webhook_url}/webhook",
        )
    else:
        log.info("running_polling")
        await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
