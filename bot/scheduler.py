"""Signal Scheduler — harmonogram automatycznego skanowania par.

Uruchamia scan_all_pairs() co N minut w aktywnych sesjach tradingowych.
Co niedzielę 20:00 UTC uruchamia Optimizer Agent (GROK-3).
Używa APScheduler AsyncIOScheduler — kompatybilny z python-telegram-bot v20.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from agents.optimizer import Optimizer
    from bot.telegram_bot import TelegramBot
    from db.database import Database

log = structlog.get_logger(__name__)

_OPTIMIZER_LOOKBACK_DAYS: int = 28
_OPTIMIZER_MIN_TRADES: int = 10


class SignalScheduler:
    """Harmonogram skanowania sygnałów oparty na APScheduler AsyncIOScheduler.

    Skanuje wszystkie pary co scan_interval minut w aktywnych sesjach
    (London + NY: 07:00–21:00 UTC, dni robocze).
    Co niedzielę 20:00 UTC uruchamia Optimizer Agent (GROK-3, READ-ONLY).
    """

    def __init__(
        self,
        telegram_bot: TelegramBot,
        scan_interval_minutes: int = 15,
        active_sessions_only: bool = True,
        optimizer: Optimizer | None = None,
        db: Database | None = None,
    ) -> None:
        self.bot = telegram_bot
        self.scan_interval = scan_interval_minutes
        self.active_sessions_only = active_sessions_only
        self.optimizer = optimizer
        self.db = db
        self.scheduler: object | None = None
        self._is_running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Uruchamia harmonogram. Dodaje job skanowania i tygodniowy Optimizer."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: PLC0415
        from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415
        from apscheduler.triggers.interval import IntervalTrigger  # noqa: PLC0415

        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self._scan_job,
            trigger=IntervalTrigger(minutes=self.scan_interval),
            id="signal_scan",
            name="Scan all pairs for signals",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._optimizer_job,
            trigger=CronTrigger(day_of_week="sun", hour=20, minute=0, timezone="UTC"),
            id="weekly_optimizer",
            name="Weekly Optimizer Agent (GROK-3)",
            replace_existing=True,
        )
        self.scheduler.start()
        self._is_running = True
        log.info("scheduler_started", interval_min=self.scan_interval, optimizer_wired=self.optimizer is not None)

    def stop(self) -> None:
        """Zatrzymuje harmonogram."""
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            self._is_running = False
            log.info("scheduler_stopped")

    # ── Optimizer job ─────────────────────────────────────────────────────────

    async def _optimizer_job(self) -> None:
        """Co niedzielę 20:00 UTC — uruchamia Optimizer Agent (GROK-3, READ-ONLY).

        Pobiera zamknięte sygnały z DB, uruchamia Optimizer,
        wysyła raport na prywatny kanał admina.
        NIE wdraża zmian automatycznie.
        """
        log.info("optimizer_job_started")
        try:
            if self.optimizer is None or self.db is None:
                log.warning("optimizer_job_skipped", reason="optimizer or db not configured")
                await self.bot.notify_admin(
                    "⚠️ Optimizer: nie skonfigurowany (brak optimizer= lub db= w SignalScheduler)."
                )
                return

            trade_history = self.db.get_closed_signals(days=_OPTIMIZER_LOOKBACK_DAYS)
            log.info("optimizer_trades_loaded", count=len(trade_history))

            if len(trade_history) < _OPTIMIZER_MIN_TRADES:
                msg = (
                    f"📊 Optimizer: za mało danych ({len(trade_history)}/{_OPTIMIZER_MIN_TRADES} "
                    f"zamkniętych sygnałów z ostatnich {_OPTIMIZER_LOOKBACK_DAYS} dni). "
                    f"Uruchom ponownie po zebraniu większej próbki."
                )
                log.info("optimizer_job_skipped", reason="insufficient_trades", count=len(trade_history))
                await self.bot.notify_admin(msg)
                return

            result = self.optimizer.optimize(trade_history)

            report = self._format_optimizer_report(result, len(trade_history))
            await self.bot.notify_admin(report)
            log.info("optimizer_job_completed", tier=result.tier_used.name if result.tier_used else "unknown")

        except Exception as exc:
            log.error("optimizer_job_failed", error=str(exc))
            await self.bot.notify_admin(f"❌ Optimizer błąd: {exc}")

    def _format_optimizer_report(self, result: object, trade_count: int) -> str:
        """Formatuje wynik Optimizer Agent do wiadomości Telegram.

        Args:
            result: AgentResult z Optimizer.optimize().
            trade_count: Liczba analizowanych tradów.

        Returns:
            Sformatowany tekst raportu.
        """
        from agents.base_agent import AgentResult  # noqa: PLC0415

        if not isinstance(result, AgentResult):
            return f"Optimizer: nieoczekiwany wynik ({type(result).__name__})"

        tier_name = result.tier_used.name if result.tier_used else "N/A"
        suggestions: list[dict] = (result.raw_data or {}).get("suggestions", [])

        lines = [
            "*Tygodniowy Raport Optymalizatora*",
            f"Przeanalizowane sygnaly: {trade_count} (ostatnie {_OPTIMIZER_LOOKBACK_DAYS} dni)",
            f"Tier: {tier_name} | Confidence: {result.confidence:.0%}",
            "",
        ]

        if result.reasoning:
            lines.append("*Analiza:*")
            lines.append(result.reasoning[:600])
            lines.append("")

        if suggestions:
            lines.append("*Proponowane zmiany parametrow (READ-ONLY — akceptuj recznie):*")
            for s in suggestions:
                param = s.get("parameter", "?")
                current = s.get("current_value", "?")
                suggested = s.get("suggested_value", "?")
                lines.append(f"  - `{param}`: {current} -> {suggested}")
            lines.append("")
        else:
            lines.append("Brak proponowanych zmian parametrow.")
            lines.append("")

        lines.append("UWAGA: Zadne zmiany NIE zostaly wdrozone automatycznie.")
        return "\n".join(lines)

    # ── Scan job ──────────────────────────────────────────────────────────────

    async def _scan_job(self) -> None:
        """Wykonywany co scan_interval minut. Skanuje pary i wysyła sygnały."""
        try:
            if self.active_sessions_only and not self._is_active_session():
                log.debug("scan_skipped", reason="outside active session")
                return

            log.info("scan_started")
            signals = self.bot.signal_generator.scan_all_pairs()

            for signal in signals:
                success = await self.bot.send_signal(signal)
                if not success:
                    log.warning("signal_delivery_failed", pair=signal.pair)

            log.info("scan_completed", signals_found=len(signals))

        except Exception as exc:
            log.error("scan_job_failed", error=str(exc))
            await self.bot.notify_admin(f"⚠️ Scan failed: {exc}")

    # ── Session check ──────────────────────────────────────────────────────────

    def _is_active_session(self, _now: datetime | None = None) -> bool:
        """Sprawdza czy jesteśmy w aktywnej sesji tradingowej.

        Aktywne: London (07:00–16:00) + NY (12:00–21:00) = 07:00–21:00 UTC
        Nieaktywne: sesja azjatycka (00:00–07:00), weekendy.

        Args:
            _now: Opcjonalny datetime do testów (zamiast datetime.now).

        Returns:
            True jeśli w aktywnej sesji, False w przeciwnym wypadku.
        """
        now = _now or datetime.now(timezone.utc)
        hour = now.hour
        day = now.weekday()  # 0=Mon, 6=Sun

        if day >= 5:  # Saturday, Sunday
            return False

        if 7 <= hour < 21:
            return True

        return False
