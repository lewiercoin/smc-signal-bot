"""Tests for bot/scheduler.py — 8 testów."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.scheduler import SignalScheduler
from engine.signal_generator import Signal


# ── Fixtures ──────────────────────────────────────────────────────────────────


def mock_signal(pair: str = "EUR_USD") -> Signal:
    return Signal(
        id=str(uuid.uuid4()),
        pair=pair,
        timeframe="H1",
        direction="bullish",
        timestamp=datetime.now(timezone.utc),
        entry=1.08500,
        stop_loss=1.08200,
        take_profit_1=1.08950,
        take_profit_2=1.09250,
        take_profit_3=1.09700,
        position_size=0.5,
        risk_reward_ratio=1.5,
        confluence_score=75,
        confluence_components=(),
        risk_amount=50.0,
        risk_pct=0.02,
        sl_distance=0.003,
        atr_at_entry=0.0012,
        status="pending",
        notes="",
    )


def make_scheduler(active_sessions_only: bool = False) -> tuple[SignalScheduler, MagicMock]:
    """Tworzy SignalScheduler z zamockowanym TelegramBot."""
    mock_bot = MagicMock()
    mock_bot.signal_generator = MagicMock()
    mock_bot.send_signal = AsyncMock(return_value=True)
    mock_bot.notify_admin = AsyncMock()

    scheduler = SignalScheduler(
        telegram_bot=mock_bot,
        scan_interval_minutes=15,
        active_sessions_only=active_sessions_only,
    )
    return scheduler, mock_bot


# ── Lifecycle ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_creates_scheduler():
    """scheduler.start() → _is_running=True, scheduler object istnieje."""
    scheduler, _ = make_scheduler()

    scheduler.start()

    try:
        assert scheduler._is_running is True
        assert scheduler.scheduler is not None
    finally:
        scheduler.stop()


def test_stop_shuts_down():
    """scheduler.stop() → _is_running=False, scheduler.shutdown() wywołany."""
    scheduler, _ = make_scheduler()

    mock_aps_instance = MagicMock()
    scheduler.scheduler = mock_aps_instance
    scheduler._is_running = True

    scheduler.stop()

    assert scheduler._is_running is False
    mock_aps_instance.shutdown.assert_called_once_with(wait=False)


# ── Scan job ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_job_sends_signals():
    """_scan_job z 2 sygnałami → send_signal wywołany 2x."""
    scheduler, mock_bot = make_scheduler(active_sessions_only=False)

    signals = [mock_signal("EUR_USD"), mock_signal("XAU_USD")]
    mock_bot.signal_generator.scan_all_pairs.return_value = signals

    await scheduler._scan_job()

    assert mock_bot.send_signal.call_count == 2
    mock_bot.signal_generator.scan_all_pairs.assert_called_once()


@pytest.mark.asyncio
async def test_scan_job_no_signals():
    """_scan_job bez sygnałów → send_signal NIE wywołany."""
    scheduler, mock_bot = make_scheduler(active_sessions_only=False)
    mock_bot.signal_generator.scan_all_pairs.return_value = []

    await scheduler._scan_job()

    mock_bot.send_signal.assert_not_called()


@pytest.mark.asyncio
async def test_scan_job_error_notifies_admin():
    """_scan_job rzuca wyjątek → notify_admin wywołany."""
    scheduler, mock_bot = make_scheduler(active_sessions_only=False)
    mock_bot.signal_generator.scan_all_pairs.side_effect = RuntimeError("OANDA down")

    await scheduler._scan_job()

    mock_bot.notify_admin.assert_called_once()
    notify_msg = mock_bot.notify_admin.call_args[0][0]
    assert "Scan failed" in notify_msg
    assert "OANDA down" in notify_msg


# ── Session checks ─────────────────────────────────────────────────────────────


def test_active_session_weekday_london():
    """Wtorek 10:00 UTC → True (London session)."""
    scheduler, _ = make_scheduler()
    # 2026-03-10 = Tuesday
    now = datetime(2026, 3, 10, 10, 0, 0, tzinfo=timezone.utc)
    assert scheduler._is_active_session(_now=now) is True


def test_active_session_weekend():
    """Sobota → False."""
    scheduler, _ = make_scheduler()
    # 2026-03-14 = Saturday
    now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert scheduler._is_active_session(_now=now) is False


def test_active_session_outside_hours():
    """Wtorek 03:00 UTC → False (poza aktywnymi sesjami)."""
    scheduler, _ = make_scheduler()
    # 2026-03-10 = Tuesday
    now = datetime(2026, 3, 10, 3, 0, 0, tzinfo=timezone.utc)
    assert scheduler._is_active_session(_now=now) is False
