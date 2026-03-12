"""Bot Monitoring — health checks i metryki runtime.

BotMonitor śledzi stan komponentów (OANDA, News, DB, Telegram),
countery skanów/sygnałów/błędów i statystyki dzienne z DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from db.database import Database

log = structlog.get_logger(__name__)


# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComponentCheck:
    """Wynik sprawdzenia pojedynczego komponentu."""

    name: str
    ok: bool
    latency_ms: float | None = None
    error: str = ""


@dataclass(frozen=True)
class HealthStatus:
    """Pełny status zdrowia bota."""

    overall_ok: bool
    checks: dict  # name → ComponentCheck
    uptime_seconds: float
    scan_count: int
    signal_count: int
    error_count: int
    last_scan: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class DailyStats:
    """Statystyki dzienne z DB."""

    date: str
    signals_sent: int
    signals_filled: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_confluence_score: float
    total_pnl: float | None = None


# ── BotMonitor ─────────────────────────────────────────────────────────────────


class BotMonitor:
    """Monitoruje zdrowie bota i zbiera metryki runtime.

    Countery są in-memory (Faza 1). Persistent metrics w Fazie 2.
    """

    def __init__(self, db: Database | None = None) -> None:
        if db is None:
            from db.database import Database as _DB  # noqa: PLC0415
            db = _DB()

        self.db = db
        self._start_time = datetime.now(timezone.utc)
        self._scan_count = 0
        self._signal_count = 0
        self._error_count = 0
        self._last_scan_time: datetime | None = None
        self._last_error: str | None = None

        self._oanda_client: object | None = None
        self._news_client: object | None = None
        self._telegram_bot: object | None = None

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_clients(
        self,
        oanda_client: object | None = None,
        news_client: object | None = None,
        telegram_bot: object | None = None,
    ) -> None:
        """Wstrzykuje zewnętrzne klienty do health checków."""
        self._oanda_client = oanda_client
        self._news_client = news_client
        self._telegram_bot = telegram_bot

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthStatus:
        """Sprawdza wszystkie komponenty i zwraca pełny HealthStatus."""
        checks: dict[str, ComponentCheck] = {
            "oanda_api": await self._check_oanda(),
            "news_api": await self._check_news(),
            "database": self._check_db(),
            "telegram": await self._check_telegram(),
        }

        overall = all(c.ok for c in checks.values())

        return HealthStatus(
            overall_ok=overall,
            checks=checks,
            uptime_seconds=self._get_uptime(),
            scan_count=self._scan_count,
            signal_count=self._signal_count,
            error_count=self._error_count,
            last_scan=self._last_scan_time,
            last_error=self._last_error,
        )

    async def _check_oanda(self) -> ComponentCheck:
        """Sprawdza OANDA API przez get_account_summary()."""
        import time  # noqa: PLC0415

        if self._oanda_client is None:
            try:
                from connectors.oanda_client import OandaClient  # noqa: PLC0415
                self._oanda_client = OandaClient()
            except Exception as exc:
                return ComponentCheck(name="oanda_api", ok=False, error=str(exc))

        start = time.monotonic()
        try:
            self._oanda_client.get_account_summary()
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ComponentCheck(name="oanda_api", ok=True, latency_ms=latency_ms)
        except Exception as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ComponentCheck(
                name="oanda_api", ok=False, latency_ms=latency_ms, error=str(exc)
            )

    async def _check_news(self) -> ComponentCheck:
        """Sprawdza News API przez get_upcoming_events()."""
        if self._news_client is None:
            try:
                from connectors.news_client import NewsClient  # noqa: PLC0415
                self._news_client = NewsClient()
            except Exception as exc:
                return ComponentCheck(name="news_api", ok=False, error=str(exc))

        try:
            self._news_client.get_upcoming_events(hours_ahead=1)
            return ComponentCheck(name="news_api", ok=True)
        except Exception as exc:
            return ComponentCheck(name="news_api", ok=False, error=str(exc))

    def _check_db(self) -> ComponentCheck:
        """Sprawdza SQLite przez get_signals(limit=1)."""
        try:
            self.db.get_signals(limit=1)
            return ComponentCheck(name="database", ok=True)
        except Exception as exc:
            return ComponentCheck(name="database", ok=False, error=str(exc))

    async def _check_telegram(self) -> ComponentCheck:
        """Sprawdza Telegram bot przez get_me()."""
        if self._telegram_bot is None:
            return ComponentCheck(
                name="telegram", ok=False, error="telegram_bot not configured"
            )

        try:
            bot = getattr(self._telegram_bot, "_app", None)
            if bot is None:
                return ComponentCheck(
                    name="telegram", ok=False, error="app not initialized"
                )
            await bot.bot.get_me()
            return ComponentCheck(name="telegram", ok=True)
        except Exception as exc:
            return ComponentCheck(name="telegram", ok=False, error=str(exc))

    # ── Counters ──────────────────────────────────────────────────────────────

    def record_scan(self, signals_found: int) -> None:
        """Inkrementuje scan counter po każdym scanie."""
        self._scan_count += 1
        self._signal_count += signals_found
        self._last_scan_time = datetime.now(timezone.utc)
        log.debug(
            "scan_recorded",
            scan_count=self._scan_count,
            signals_found=signals_found,
        )

    def record_error(self, error: str) -> None:
        """Inkrementuje error counter i zapisuje last_error."""
        self._error_count += 1
        self._last_error = error
        log.debug("error_recorded", error_count=self._error_count, error=error)

    # ── Stats ──────────────────────────────────────────────────────────────────

    def get_daily_stats(self) -> DailyStats:
        """Pobiera statystyki z dzisiaj z DB."""
        today = datetime.now(timezone.utc).date().isoformat()

        try:
            all_signals = self.db.get_signals(limit=500)
            today_signals = [
                s for s in all_signals
                if str(s.get("created_at", "")).startswith(today)
            ]

            signals_sent = len(today_signals)
            filled = [s for s in today_signals if s.get("status") not in ("OPEN", "PENDING")]
            signals_filled = len(filled)

            closed = [s for s in today_signals if s.get("pnl_r") is not None]
            wins = [s for s in closed if (s.get("pnl_r") or 0) > 0]
            losses = [s for s in closed if (s.get("pnl_r") or 0) <= 0]
            win_count = len(wins)
            loss_count = len(losses)
            win_rate = win_count / len(closed) if closed else 0.0

            scores = [
                s["confluence_score"]
                for s in today_signals
                if s.get("confluence_score") is not None
            ]
            avg_score = sum(scores) / len(scores) if scores else 0.0

            pnl_values = [s["pnl_r"] for s in closed]
            total_pnl = sum(pnl_values) if pnl_values else None

        except Exception as exc:
            log.error("daily_stats_error", error=str(exc))
            return DailyStats(
                date=today,
                signals_sent=0,
                signals_filled=0,
                win_count=0,
                loss_count=0,
                win_rate=0.0,
                avg_confluence_score=0.0,
                total_pnl=None,
            )

        return DailyStats(
            date=today,
            signals_sent=signals_sent,
            signals_filled=signals_filled,
            win_count=win_count,
            loss_count=loss_count,
            win_rate=win_rate,
            avg_confluence_score=avg_score,
            total_pnl=total_pnl,
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_uptime(self) -> float:
        """Zwraca uptime w sekundach od uruchomienia."""
        return (datetime.now(timezone.utc) - self._start_time).total_seconds()
