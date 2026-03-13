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


def main() -> None:
    log.info("bot_starting")

    db = Database()
    db.initialize()

    bot = TelegramBot(db=db)
    app = bot.setup()

    optimizer = Optimizer()
    scheduler = SignalScheduler(telegram_bot=bot, optimizer=optimizer, db=db)

    async def post_init(application: object) -> None:
        scheduler.start()

    async def post_shutdown(application: object) -> None:
        scheduler.stop()

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    webhook_url = os.getenv("WEBHOOK_URL")
    use_polling = os.getenv("USE_POLLING", "false").lower() == "true"
    if webhook_url and not use_polling:
        import pathlib  # noqa: PLC0415
        base = pathlib.Path(__file__).parent / "deploy" / "ssl"
        cert_path = base / "cert.pem"
        key_path = base / "private.key"
        log.info("running_webhook", url=webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=8443,
            url_path="webhook",
            webhook_url=f"{webhook_url}/webhook",
            cert=str(cert_path),
            key=str(key_path),
        )
    else:
        log.info("running_polling")
        app.run_polling(drop_pending_updates=True)


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
        main()
