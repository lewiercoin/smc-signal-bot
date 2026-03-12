"""Signal Scheduler — harmonogram automatycznego skanowania par.

Uruchamia scan_all_pairs() co N minut w aktywnych sesjach tradingowych.
Używa APScheduler AsyncIOScheduler — kompatybilny z python-telegram-bot v20.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from bot.telegram_bot import TelegramBot

log = structlog.get_logger(__name__)


class SignalScheduler:
    """Harmonogram skanowania sygnałów oparty na APScheduler AsyncIOScheduler.

    Skanuje wszystkie pary co scan_interval minut w aktywnych sesjach
    (London + NY: 07:00–21:00 UTC, dni robocze).
    """

    def __init__(
        self,
        telegram_bot: TelegramBot,
        scan_interval_minutes: int = 15,
        active_sessions_only: bool = True,
    ) -> None:
        self.bot = telegram_bot
        self.scan_interval = scan_interval_minutes
        self.active_sessions_only = active_sessions_only
        self.scheduler: object | None = None
        self._is_running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Uruchamia harmonogram. Dodaje job skanowania co scan_interval minut."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: PLC0415
        from apscheduler.triggers.interval import IntervalTrigger  # noqa: PLC0415

        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self._scan_job,
            trigger=IntervalTrigger(minutes=self.scan_interval),
            id="signal_scan",
            name="Scan all pairs for signals",
            replace_existing=True,
        )
        self.scheduler.start()
        self._is_running = True
        log.info("scheduler_started", interval_min=self.scan_interval)

    def stop(self) -> None:
        """Zatrzymuje harmonogram."""
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            self._is_running = False
            log.info("scheduler_stopped")

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
