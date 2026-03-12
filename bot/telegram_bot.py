"""Telegram Bot — warstwa prezentacji pipeline'u SMC Signal Bot.

Odbiera sygnały z SignalGenerator, formatuje przez TelegramEditor
i publikuje na kanał Telegram. Admin commands przez prywatny chat.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, ContextTypes

    from agents.telegram_editor import TelegramEditor
    from db.database import Database
    from engine.signal_generator import Signal, SignalGenerator

log = structlog.get_logger(__name__)


class TelegramBot:
    """Główny bot Telegram — publikuje sygnały, obsługuje admin commands.

    Read-only channel bot. Admin (Greg) steruje przez prywatny chat.
    Webhook primary, polling fallback gdy brak WEBHOOK_URL.
    """

    def __init__(
        self,
        signal_generator: SignalGenerator | None = None,
        telegram_editor: TelegramEditor | None = None,
        db: Database | None = None,
        channel_id: str | None = None,
        admin_chat_id: str | None = None,
    ) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.channel_id = channel_id or os.getenv("TELEGRAM_CHANNEL_ID")
        self.admin_chat_id = admin_chat_id or os.getenv("TELEGRAM_ADMIN_CHAT_ID")

        if signal_generator is None:
            from engine.signal_generator import SignalGenerator as _SG  # noqa: PLC0415
            signal_generator = _SG()
        if telegram_editor is None:
            from agents.telegram_editor import TelegramEditor as _TE  # noqa: PLC0415
            telegram_editor = _TE()
        if db is None:
            from db.database import Database as _DB  # noqa: PLC0415
            db = _DB()

        self.signal_generator = signal_generator
        self.telegram_editor = telegram_editor
        self.db = db
        self._app: Application | None = None

    # ── Setup ─────────────────────────────────────────────────────────────────

    def setup(self) -> Application:
        """Inicjalizuje Application z handlerami i error handlerem."""
        from telegram.ext import Application, CommandHandler  # noqa: PLC0415

        app = Application.builder().token(self.token).build()

        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("scan", self._cmd_scan))
        app.add_handler(CommandHandler("last", self._cmd_last))
        app.add_handler(CommandHandler("pairs", self._cmd_pairs))
        app.add_handler(CommandHandler("health", self._cmd_health))
        app.add_handler(CommandHandler("help", self._cmd_help))

        app.add_error_handler(self._error_handler)

        self._app = app
        log.info("bot_setup_complete", handlers=6)
        return app

    # ── Webhook / polling ─────────────────────────────────────────────────────

    async def run_webhook(
        self,
        host: str = "0.0.0.0",
        port: int = 8443,
        webhook_url: str | None = None,
    ) -> None:
        """Uruchamia bota. Webhook jeśli webhook_url dostępny, polling jako fallback."""
        resolved_url = webhook_url or os.getenv("WEBHOOK_URL")

        if not resolved_url:
            log.warning("no_webhook_url", msg="Falling back to polling mode")
            await self._app.run_polling()
            return

        log.info("running_webhook", url=resolved_url, host=host, port=port)
        await self._app.run_webhook(
            listen=host,
            port=port,
            url_path="webhook",
            webhook_url=f"{resolved_url}/webhook",
            allowed_updates=["message"],
        )

    # ── Signal sending ─────────────────────────────────────────────────────────

    async def send_signal(self, signal: Signal) -> bool:
        """Formatuje sygnał przez TelegramEditor i wysyła na kanał.

        Returns:
            True jeśli wysłano pomyślnie, False przy błędzie.
        """
        try:
            context = self._build_editor_context(signal)
            editor_result = self.telegram_editor.analyze(context)
            message_text = editor_result.raw_data.get(
                "telegram_message", editor_result.reasoning
            )

            msg = await self._app.bot.send_message(
                chat_id=self.channel_id,
                text=message_text,
                parse_mode="HTML",
            )

            self.db.update_signal_status(
                signal_id=signal.id,
                status="sent",
                closed_price=signal.entry,
                pnl_r=0.0,
            )

            await self._notify_admin(
                f"✅ Signal sent: {signal.pair} {signal.direction}"
            )

            log.info(
                "signal_sent",
                pair=signal.pair,
                direction=signal.direction,
                msg_id=msg.message_id,
            )
            return True

        except Exception as exc:
            log.error("signal_send_failed", pair=signal.pair, error=str(exc))
            await self._notify_admin(f"❌ Signal FAILED: {signal.pair} — {exc}")
            return False

    def _build_editor_context(self, signal: Signal) -> dict[str, Any]:
        """Mapuje Signal na input_data format TelegramEditor."""
        return {
            "instrument": signal.pair,
            "direction": signal.direction,
            "entry": signal.entry,
            "stop_loss": signal.stop_loss,
            "take_profits": {
                "tp1": signal.take_profit_1,
                "tp2": signal.take_profit_2,
                "tp3": signal.take_profit_3,
            },
            "confluence_score": signal.confluence_score,
            "risk_reward": signal.risk_reward_ratio,
            "atr": signal.atr_at_entry,
            "session": "",
            "setup_type": "",
            "structure_bias": "",
            "fundamental_bias": "",
            "risk_notes": [],
        }

    # ── Admin commands ─────────────────────────────────────────────────────────

    def _is_admin(self, update: Update) -> bool:
        """Sprawdza czy wiadomość pochodzi od admina."""
        if not self.admin_chat_id:
            return False
        try:
            return update.effective_chat.id == int(self.admin_chat_id)
        except (ValueError, AttributeError):
            return False

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Podsumowanie: sygnały dziś, aktywne pozycje."""
        if not self._is_admin(update):
            return

        try:
            signals = self.db.get_signals(limit=100)
            today = datetime.now(timezone.utc).date().isoformat()
            today_signals = [s for s in signals if str(s.get("created_at", "")).startswith(today)]
            active = [s for s in signals if s.get("status") == "OPEN"]
            closed = [s for s in signals if s.get("pnl_r") is not None]
            wins = [s for s in closed if (s.get("pnl_r") or 0) > 0]
            win_rate = len(wins) / len(closed) if closed else 0.0

            text = (
                "<b>SMC Signal Bot — Status</b>\n"
                f"Date: {today}\n"
                f"Signals today: {len(today_signals)}\n"
                f"Active: {len(active)}\n"
                f"Win rate (all-time): {win_rate:.0%}\n"
                f"Total signals: {len(signals)}"
            )
        except Exception as exc:
            text = f"❌ Status error: {exc}"

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ręczny scan — uruchamia SignalGenerator.scan_all_pairs() natychmiast."""
        if not self._is_admin(update):
            return

        await update.message.reply_text("🔍 Scanning all pairs...")

        try:
            signals = self.signal_generator.scan_all_pairs()
            if not signals:
                await update.message.reply_text("No signals found.")
                return

            for signal in signals:
                await self.send_signal(signal)

            await update.message.reply_text(f"✅ Scan done — {len(signals)} signal(s) sent.")
        except Exception as exc:
            await update.message.reply_text(f"❌ Scan failed: {exc}")

    async def _cmd_last(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Pokaż ostatni sygnał z DB."""
        if not self._is_admin(update):
            return

        try:
            signals = self.db.get_signals(limit=1)
            if not signals:
                await update.message.reply_text("No signals in DB.")
                return

            s = signals[0]
            text = (
                "<b>Last Signal</b>\n"
                f"Instrument: {s.get('instrument')}\n"
                f"Direction: {s.get('direction')}\n"
                f"Entry: {s.get('entry_price')}\n"
                f"SL: {s.get('sl_price')}\n"
                f"Score: {s.get('confluence_score')}\n"
                f"Status: {s.get('status')}\n"
                f"Created: {s.get('created_at')}"
            )
        except Exception as exc:
            text = f"❌ DB error: {exc}"

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_pairs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Status per para: ostatni signal, status."""
        if not self._is_admin(update):
            return

        try:
            pairs = ["EUR_USD", "XAU_USD", "BTC_USD"]
            lines = ["<b>Pairs Status</b>"]
            for pair in pairs:
                signals = self.db.get_signals(instrument=pair, limit=1)
                if signals:
                    s = signals[0]
                    lines.append(
                        f"{pair}: last={s.get('created_at', 'n/a')[:10]} "
                        f"status={s.get('status', 'n/a')}"
                    )
                else:
                    lines.append(f"{pair}: no signals yet")
            text = "\n".join(lines)
        except Exception as exc:
            text = f"❌ Error: {exc}"

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Health check: DB, uptime."""
        if not self._is_admin(update):
            return

        try:
            self.db.get_signals(limit=1)
            db_ok = True
        except Exception:
            db_ok = False

        db_status = "✅" if db_ok else "❌"
        text = (
            "<b>Health Check</b>\n"
            f"Database: {db_status}\n"
            f"Token configured: {'✅' if self.token else '❌'}\n"
            f"Channel configured: {'✅' if self.channel_id else '❌'}"
        )

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Lista komend z opisami."""
        if not self._is_admin(update):
            return

        text = (
            "<b>SMC Signal Bot — Admin Commands</b>\n\n"
            "/status — sygnały dziś, win rate\n"
            "/scan — ręczny scan wszystkich par\n"
            "/last — ostatni sygnał z DB\n"
            "/pairs — status per para\n"
            "/health — health check komponentów\n"
            "/help — ta wiadomość"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    # ── Notifications ──────────────────────────────────────────────────────────

    async def _notify_admin(self, message: str) -> None:
        """Wysyła powiadomienie do admin chatu. Błąd NIE blokuje pipeline'u."""
        if not self.admin_chat_id or not self._app:
            return
        try:
            await self._app.bot.send_message(
                chat_id=self.admin_chat_id,
                text=message,
            )
        except Exception as exc:
            log.warning("admin_notify_failed", error=str(exc))

    async def notify_admin(self, message: str) -> None:
        """Publiczny alias dla _notify_admin (używany przez scheduler)."""
        await self._notify_admin(message)

    # ── Error handler ──────────────────────────────────────────────────────────

    async def _error_handler(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Loguje błąd przez structlog i powiadamia admina."""
        error = context.error
        log.error("telegram_error", error=str(error), update=str(update))
        await self._notify_admin(f"⚠️ Bot error: {error}")
