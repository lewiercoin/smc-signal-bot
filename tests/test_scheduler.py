"""Tests for bot/scheduler.py — 14 testów (8 original + 6 T9 optimizer)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

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


# ── T9: Optimizer job ──────────────────────────────────────────────────────────


def _make_agent_result(
    reasoning: str = "test analysis",
    suggestions: list[dict] | None = None,
    confidence: float = 0.7,
) -> object:
    """Helper: tworzy AgentResult dla Optimizer."""
    from agents.base_agent import AgentResult, AgentTier, MarketBias
    return AgentResult(
        agent_name="optimizer",
        tier_used=AgentTier.DETERMINISTIC,
        bias=MarketBias.NEUTRAL,
        confidence=confidence,
        reasoning=reasoning,
        timestamp=datetime.now(timezone.utc),
        raw_data={"suggestions": suggestions or [], "period_days": 28},
    )


def _make_closed_trade(pair: str = "EUR_USD", pnl_r: float = 1.5) -> dict:
    return {
        "instrument": pair,
        "direction": "bullish",
        "status": "TP1",
        "pnl_r": pnl_r,
        "confluence_score": 72,
        "session": "H1",
        "closed_at": "2026-03-10T12:00:00",
    }


@pytest.mark.asyncio
async def test_optimizer_job_skipped_when_not_configured():
    """_optimizer_job bez optimizer= → notify_admin z informacją, brak crashu."""
    scheduler, mock_bot = make_scheduler()
    scheduler.optimizer = None
    scheduler.db = None

    await scheduler._optimizer_job()

    mock_bot.notify_admin.assert_called_once()
    msg = mock_bot.notify_admin.call_args[0][0]
    assert "nie skonfigurowany" in msg


@pytest.mark.asyncio
async def test_optimizer_job_skipped_insufficient_trades():
    """_optimizer_job z <10 sygnałów → notify_admin z info o za małej próbce."""
    scheduler, mock_bot = make_scheduler()

    mock_optimizer = MagicMock()
    mock_db = MagicMock()
    mock_db.get_closed_signals.return_value = [_make_closed_trade()] * 5
    scheduler.optimizer = mock_optimizer
    scheduler.db = mock_db

    await scheduler._optimizer_job()

    mock_optimizer.optimize.assert_not_called()
    mock_bot.notify_admin.assert_called_once()
    msg = mock_bot.notify_admin.call_args[0][0]
    assert "za mało danych" in msg
    assert "5/10" in msg


@pytest.mark.asyncio
async def test_optimizer_job_runs_and_sends_report():
    """_optimizer_job z >=10 sygnałów → optimize() wywołany, raport wysłany."""
    scheduler, mock_bot = make_scheduler()

    mock_optimizer = MagicMock()
    mock_optimizer.optimize.return_value = _make_agent_result(
        reasoning="WR 45%, profit factor 1.2. Sugeruj obniżenie progu.",
        suggestions=[{"parameter": "confluence_threshold", "current_value": 65, "suggested_value": 60}],
    )
    mock_db = MagicMock()
    mock_db.get_closed_signals.return_value = [_make_closed_trade()] * 12
    scheduler.optimizer = mock_optimizer
    scheduler.db = mock_db

    await scheduler._optimizer_job()

    mock_optimizer.optimize.assert_called_once()
    mock_bot.notify_admin.assert_called_once()
    report = mock_bot.notify_admin.call_args[0][0]
    assert "Raport Optymalizatora" in report
    assert "confluence_threshold" in report
    assert "UWAGA" in report


@pytest.mark.asyncio
async def test_optimizer_job_exception_notifies_admin():
    """_optimizer_job rzuca wyjątek → notify_admin z błędem, brak propagacji."""
    scheduler, mock_bot = make_scheduler()

    mock_optimizer = MagicMock()
    mock_optimizer.optimize.side_effect = RuntimeError("API timeout")
    mock_db = MagicMock()
    mock_db.get_closed_signals.return_value = [_make_closed_trade()] * 15
    scheduler.optimizer = mock_optimizer
    scheduler.db = mock_db

    await scheduler._optimizer_job()

    mock_bot.notify_admin.assert_called_once()
    msg = mock_bot.notify_admin.call_args[0][0]
    assert "blad" in msg.lower() or "API timeout" in msg


def test_format_optimizer_report_no_suggestions():
    """_format_optimizer_report bez sugestii → zawiera 'Brak proponowanych zmian'."""
    scheduler, _ = make_scheduler()
    result = _make_agent_result(reasoning="All params optimal.", suggestions=[])

    report = scheduler._format_optimizer_report(result, trade_count=15)

    assert "Brak proponowanych zmian" in report
    assert "UWAGA" in report
    assert "15" in report


def test_format_optimizer_report_with_suggestions():
    """_format_optimizer_report z sugestiami → zawiera parametr i wartości."""
    scheduler, _ = make_scheduler()
    result = _make_agent_result(
        suggestions=[{"parameter": "tp1_ratio", "current_value": 1.5, "suggested_value": 1.8}]
    )

    report = scheduler._format_optimizer_report(result, trade_count=20)

    assert "tp1_ratio" in report
    assert "1.5" in report
    assert "1.8" in report
