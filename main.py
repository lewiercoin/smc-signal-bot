"""SMC Signal Bot — Entry Point."""

import asyncio
import os
import sys

import structlog
from dotenv import load_dotenv

from agents.optimizer import Optimizer
from bot.scheduler import SignalScheduler
from bot.telegram_bot import TelegramBot
from db.database import Database

load_dotenv()

log = structlog.get_logger()


async def main() -> None:
    log.info("bot_starting")

    db = Database()
    db.initialize()

    bot = TelegramBot(db=db)
    app = bot.setup()

    optimizer = Optimizer()
    scheduler = SignalScheduler(telegram_bot=bot, optimizer=optimizer, db=db)

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
    if len(sys.argv) > 1 and sys.argv[1] == "paper":
        from paper_trading.runner import PaperTradingRunner
        runner = PaperTradingRunner()
        asyncio.run(runner.run(duration_hours=24))
    elif len(sys.argv) > 1 and sys.argv[1] == "analyze":
        from paper_trading.analyzer import PaperAnalyzer
        analyzer = PaperAnalyzer()
        report = analyzer.analyze()
        analyzer.print_report(report)
    else:
        asyncio.run(main())
