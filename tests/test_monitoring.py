"""Tests for bot/monitoring.py — 8 testów."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.monitoring import BotMonitor, ComponentCheck, DailyStats, HealthStatus


# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_monitor() -> tuple[BotMonitor, MagicMock]:
    """Tworzy BotMonitor z zamockowanym DB."""
    mock_db = MagicMock()
    mock_db.get_signals.return_value = []
    monitor = BotMonitor(db=mock_db)
    return monitor, mock_db


# ── Health check ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_all_ok():
    """Wszystkie komponenty OK → overall_ok=True."""
    monitor, _ = make_monitor()

    monitor._check_oanda = AsyncMock(
        return_value=ComponentCheck(name="oanda_api", ok=True, latency_ms=12.5)
    )
    monitor._check_news = AsyncMock(
        return_value=ComponentCheck(name="news_api", ok=True)
    )
    monitor._check_db = MagicMock(
        return_value=ComponentCheck(name="database", ok=True)
    )
    monitor._check_telegram = AsyncMock(
        return_value=ComponentCheck(name="telegram", ok=True)
    )

    status = await monitor.health_check()

    assert isinstance(status, HealthStatus)
    assert status.overall_ok is True
    assert len(status.checks) == 4
    assert status.checks["oanda_api"].ok is True
    assert status.checks["news_api"].ok is True
    assert status.checks["database"].ok is True
    assert status.checks["telegram"].ok is True


@pytest.mark.asyncio
async def test_health_check_one_failure():
    """OANDA fails → overall_ok=False."""
    monitor, _ = make_monitor()

    monitor._check_oanda = AsyncMock(
        return_value=ComponentCheck(name="oanda_api", ok=False, error="timeout")
    )
    monitor._check_news = AsyncMock(
        return_value=ComponentCheck(name="news_api", ok=True)
    )
    monitor._check_db = MagicMock(
        return_value=ComponentCheck(name="database", ok=True)
    )
    monitor._check_telegram = AsyncMock(
        return_value=ComponentCheck(name="telegram", ok=True)
    )

    status = await monitor.health_check()

    assert status.overall_ok is False
    assert status.checks["oanda_api"].ok is False
    assert status.checks["oanda_api"].error == "timeout"


# ── Counters ──────────────────────────────────────────────────────────────────


def test_record_scan_increments():
    """record_scan() → scan_count i signal_count rosną."""
    monitor, _ = make_monitor()

    assert monitor._scan_count == 0
    assert monitor._signal_count == 0

    monitor.record_scan(signals_found=2)
    assert monitor._scan_count == 1
    assert monitor._signal_count == 2

    monitor.record_scan(signals_found=1)
    assert monitor._scan_count == 2
    assert monitor._signal_count == 3

    assert monitor._last_scan_time is not None


def test_record_error_tracks_last():
    """record_error() → error_count rośnie, last_error zaktualizowany."""
    monitor, _ = make_monitor()

    assert monitor._error_count == 0
    assert monitor._last_error is None

    monitor.record_error("OANDA timeout")
    assert monitor._error_count == 1
    assert monitor._last_error == "OANDA timeout"

    monitor.record_error("DB write failed")
    assert monitor._error_count == 2
    assert monitor._last_error == "DB write failed"


# ── Uptime ────────────────────────────────────────────────────────────────────


def test_uptime_calculation():
    """uptime > 0 po uruchomieniu."""
    monitor, _ = make_monitor()

    uptime = monitor._get_uptime()
    assert uptime >= 0.0

    # Uptime powinien być bardzo mały (ułamek sekundy od init)
    assert uptime < 5.0


# ── Daily stats ───────────────────────────────────────────────────────────────


def test_daily_stats_from_db():
    """get_daily_stats() → poprawne statystyki na podstawie danych DB."""
    monitor, mock_db = make_monitor()
    today = datetime.now(timezone.utc).date().isoformat()

    mock_db.get_signals.return_value = [
        {
            "instrument": "EUR_USD",
            "direction": "bullish",
            "confluence_score": 75,
            "status": "TP1",
            "created_at": f"{today}T10:00:00+00:00",
            "pnl_r": 1.5,
        },
        {
            "instrument": "XAU_USD",
            "direction": "bearish",
            "confluence_score": 80,
            "status": "SL",
            "created_at": f"{today}T11:00:00+00:00",
            "pnl_r": -1.0,
        },
        {
            "instrument": "BTC_USD",
            "direction": "bullish",
            "confluence_score": 70,
            "status": "OPEN",
            "created_at": f"{today}T12:00:00+00:00",
            "pnl_r": None,
        },
    ]

    stats = monitor.get_daily_stats()

    assert isinstance(stats, DailyStats)
    assert stats.date == today
    assert stats.signals_sent == 3
    assert stats.win_count == 1
    assert stats.loss_count == 1
    assert stats.win_rate == pytest.approx(0.5)
    assert stats.avg_confluence_score == pytest.approx(75.0)


# ── Component checks ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_oanda_timeout():
    """OANDA client rzuca wyjątek → ok=False z komunikatem błędu."""
    monitor, _ = make_monitor()

    mock_oanda = MagicMock()
    mock_oanda.get_account_summary.side_effect = TimeoutError("connection timeout")
    monitor._oanda_client = mock_oanda

    result = await monitor._check_oanda()

    assert result.ok is False
    assert "timeout" in result.error.lower()


@pytest.mark.asyncio
async def test_check_db_failure():
    """DB error → ok=False."""
    monitor, mock_db = make_monitor()
    mock_db.get_signals.side_effect = Exception("disk full")

    result = monitor._check_db()

    assert result.ok is False
    assert "disk full" in result.error
