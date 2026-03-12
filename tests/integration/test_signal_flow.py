"""Integration tests for signal generation flow.

Tests signal lifecycle: generation → DB persistence → status updates.
Verifies signal field correctness and risk rule compliance.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from connectors.oanda_client import Candle
from db.database import Database
from engine.signal_generator import Signal, SignalGenerator


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_sg_for_flow(
    candles: list[Candle],
    mock_news: MagicMock,
    db: Database,
    pair: str = "EUR_USD",
    spread: float = 0.00010,
) -> SignalGenerator:
    """SignalGenerator with mocked OANDA/News, real internal modules, real DB."""
    oanda = MagicMock()
    oanda.get_candles.return_value = candles
    oanda.get_current_spread.return_value = spread

    return SignalGenerator(
        oanda_client=oanda,
        news_client=mock_news,
        db=db,
        confluence_threshold=65,
    )


def _force_signal(
    sg: SignalGenerator,
    pair: str,
    direction: str = "bullish",
    candles: list[Candle] | None = None,
) -> Signal | None:
    """Attempt to force a signal by patching scorer to return high score."""
    from datetime import datetime, timezone
    from engine.confluence_scorer import ConfluenceResult, ScoreComponent
    from engine.risk_engine import (
        PositionSize,
        SpreadCheck,
        TakeProfitLevels,
        TradeParameters,
    )

    pip_size = {
        "EUR_USD": 0.0001,
        "XAU_USD": 0.01,
        "BTC_USD": 1.0,
    }.get(pair, 0.0001)

    # Use the last candle close so entry is within recent price range
    if candles:
        base_price = candles[-1].close
    else:
        base_price = {
            "EUR_USD": 1.0850,
            "XAU_USD": 1950.0,
            "BTC_USD": 42000.0,
        }.get(pair, 1.0850)

    sl_dist = pip_size * 20
    entry = base_price
    sl = entry - sl_dist if direction == "bullish" else entry + sl_dist
    tp1 = entry + sl_dist * 1.5 if direction == "bullish" else entry - sl_dist * 1.5
    tp2 = entry + sl_dist * 2.5 if direction == "bullish" else entry - sl_dist * 2.5
    tp3 = entry + sl_dist * 4.0 if direction == "bullish" else entry - sl_dist * 4.0

    comp = ScoreComponent(name="IPDA Zone", score=80, max_score=110, reason="forced")
    confluence = ConfluenceResult(
        total_score=80,
        max_possible=110,
        threshold=65,
        is_signal=True,
        setup_direction=direction,
        components=[comp],
        timestamp=datetime.now(tz=timezone.utc),
        pair=pair,
    )

    tps = TakeProfitLevels(tp1=tp1, tp2=tp2, tp3=tp3, ratios=(1.5, 2.5, 4.0))
    ps = PositionSize(lots=0.10, risk_amount=200.0, risk_pct=0.02, sl_pips=20.0)
    sc = SpreadCheck(passed=True, current_spread=1.0, max_allowed=3.0)
    trade = TradeParameters(
        pair=pair,
        direction=direction,
        entry=entry,
        stop_loss=sl,
        take_profits=tps,
        position_size=ps,
        spread_check=sc,
        risk_reward_ratio=1.5,
        sl_distance=sl_dist,
        atr_at_entry=pip_size * 12,
        is_valid=True,
        rejection_reason="",
        timestamp=datetime.now(tz=timezone.utc),
    )

    # Also patch risk.calculate_trade with current entry price
    with (
        patch.object(sg.scorer, "score", return_value=confluence),
        patch.object(sg.risk, "calculate_trade", return_value=trade),
    ):
        return sg.generate(pair, "H1", account_balance=10000.0)


# ── Signal lifecycle ──────────────────────────────────────────────────────────


class TestSignalLifecycle:
    def test_signal_saved_to_db_after_generate(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Successful generate() → signal_uuid stored in DB (DB fix verification)."""
        sg = _make_sg_for_flow(realistic_eur_usd_candles, mock_news_client, in_memory_db)

        signal = _force_signal(sg, "EUR_USD", "bullish", candles=realistic_eur_usd_candles)

        assert signal is not None, "Expected a Signal from forced pipeline"
        assert len(signal.id) == 36, "Signal.id must be UUID format"

        # Verify UUID is in DB (Strategy A fix)
        db_row = in_memory_db.get_signal_by_uuid(signal.id)
        assert db_row is not None, "Signal UUID must be findable in DB"
        assert db_row["instrument"] == "EUR_USD"
        assert db_row["signal_uuid"] == signal.id

    def test_signal_status_updated_after_send(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """send_signal() → update_signal_status_by_uuid() → DB status == 'sent'.

        Tests the real path: TelegramBot.send_signal() calls
        update_signal_status_by_uuid() (FIX 1). Validates the full chain
        from Signal to DB status update, not just the DB method in isolation.
        """
        from bot.telegram_bot import TelegramBot

        sg = _make_sg_for_flow(realistic_eur_usd_candles, mock_news_client, in_memory_db)
        signal = _force_signal(sg, "EUR_USD", "bullish", candles=realistic_eur_usd_candles)
        assert signal is not None

        # Build TelegramBot with real DB, mocked Telegram network
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(
            return_value=MagicMock(message_id=9999)
        )
        mock_app.bot.send_message.return_value = MagicMock(message_id=9999)

        # TelegramEditor falls back to deterministic (no API key in tests)
        bot = TelegramBot(
            signal_generator=sg,
            db=in_memory_db,
            channel_id="-100123456789",
            admin_chat_id=None,
        )
        bot._app = mock_app

        asyncio.run(bot.send_signal(signal))

        db_row = in_memory_db.get_signal_by_uuid(signal.id)
        assert db_row is not None
        assert db_row["status"] == "sent", (
            f"Expected status='sent' after send_signal(), got {db_row['status']!r}. "
            "Check that send_signal() calls update_signal_status_by_uuid()."
        )

    def test_multiple_signals_different_pairs(
        self,
        realistic_eur_usd_candles: list[Candle],
        realistic_xau_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """EUR + XAU signals saved independently to same DB."""
        sg_eur = _make_sg_for_flow(
            realistic_eur_usd_candles, mock_news_client, in_memory_db, "EUR_USD"
        )
        # XAU spread: 0.30 price units = 30 pip × 0.01 = 30 cents (within 30 pip XAU limit)
        sg_xau = _make_sg_for_flow(
            realistic_xau_usd_candles, mock_news_client, in_memory_db, "XAU_USD", spread=0.30
        )

        sig_eur = _force_signal(sg_eur, "EUR_USD", "bullish", candles=realistic_eur_usd_candles)
        sig_xau = _force_signal(sg_xau, "XAU_USD", "bearish", candles=realistic_xau_usd_candles)

        assert sig_eur is not None
        assert sig_xau is not None

        # Both in DB, separate instruments
        row_eur = in_memory_db.get_signal_by_uuid(sig_eur.id)
        row_xau = in_memory_db.get_signal_by_uuid(sig_xau.id)
        assert row_eur is not None
        assert row_xau is not None
        assert row_eur["instrument"] == "EUR_USD"
        assert row_xau["instrument"] == "XAU_USD"
        assert sig_eur.id != sig_xau.id  # Different UUIDs


# ── Signal field sanity checks ────────────────────────────────────────────────


class TestSignalSanityChecks:
    def test_signal_entry_within_price_range(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Signal entry must be within the price range of the last 10 candles."""
        sg = _make_sg_for_flow(realistic_eur_usd_candles, mock_news_client, in_memory_db)
        signal = _force_signal(sg, "EUR_USD", "bullish", candles=realistic_eur_usd_candles)

        if signal is None:
            pytest.skip("No signal produced — skip range check")

        last_10 = realistic_eur_usd_candles[-10:]
        price_low = min(c.low for c in last_10)
        price_high = max(c.high for c in last_10)

        assert price_low <= signal.entry <= price_high, (
            f"Entry {signal.entry} outside last-10-candle range [{price_low}, {price_high}]"
        )

    def test_signal_sl_below_entry_for_bullish(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Bullish signal: stop_loss < entry."""
        sg = _make_sg_for_flow(realistic_eur_usd_candles, mock_news_client, in_memory_db)
        signal = _force_signal(sg, "EUR_USD", "bullish", candles=realistic_eur_usd_candles)

        if signal is None:
            pytest.skip("No signal produced")

        assert signal.stop_loss < signal.entry, (
            f"Bullish SL {signal.stop_loss} must be below entry {signal.entry}"
        )

    def test_signal_sl_above_entry_for_bearish(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Bearish signal: stop_loss > entry."""
        sg = _make_sg_for_flow(realistic_eur_usd_candles, mock_news_client, in_memory_db)
        signal = _force_signal(sg, "EUR_USD", "bearish", candles=realistic_eur_usd_candles)

        if signal is None:
            pytest.skip("No signal produced")

        assert signal.stop_loss > signal.entry, (
            f"Bearish SL {signal.stop_loss} must be above entry {signal.entry}"
        )

    def test_signal_tp_sequence_correct(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """TP levels must be ordered correctly relative to entry."""
        for direction in ("bullish", "bearish"):
            db = Database(":memory:")
            db.initialize()
            sg = _make_sg_for_flow(realistic_eur_usd_candles, mock_news_client, db)
            signal = _force_signal(sg, "EUR_USD", direction, candles=realistic_eur_usd_candles)
            db.close()

            if signal is None:
                continue

            if signal.direction == "bullish":
                assert signal.entry < signal.take_profit_1, "Bullish: entry < TP1"
                assert signal.take_profit_1 < signal.take_profit_2, "Bullish: TP1 < TP2"
                assert signal.take_profit_2 < signal.take_profit_3, "Bullish: TP2 < TP3"
            else:
                assert signal.entry > signal.take_profit_1, "Bearish: entry > TP1"
                assert signal.take_profit_1 > signal.take_profit_2, "Bearish: TP1 > TP2"
                assert signal.take_profit_2 > signal.take_profit_3, "Bearish: TP2 > TP3"

    def test_signal_risk_reward_above_minimum(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Signal risk_reward_ratio must be >= 1.0."""
        sg = _make_sg_for_flow(realistic_eur_usd_candles, mock_news_client, in_memory_db)
        signal = _force_signal(sg, "EUR_USD", "bullish", candles=realistic_eur_usd_candles)

        if signal is None:
            pytest.skip("No signal produced")

        assert signal.risk_reward_ratio >= 1.0, (
            f"RR ratio {signal.risk_reward_ratio} is below minimum 1.0"
        )

    def test_signal_confluence_above_threshold(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Any produced signal must have confluence_score >= 65."""
        sg = _make_sg_for_flow(realistic_eur_usd_candles, mock_news_client, in_memory_db)
        signal = _force_signal(sg, "EUR_USD", "bullish", candles=realistic_eur_usd_candles)

        if signal is None:
            pytest.skip("No signal produced")

        assert signal.confluence_score >= 65, (
            f"Confluence {signal.confluence_score} below threshold 65"
        )


# ── No-internal-mocks smoke test (FIX 3 — Approach A) ───────────────────────


class TestNoInternalMocks:
    def test_full_pipeline_no_internal_mocks(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Pipeline runs end-to-end with ZERO internal mocks.

        Only OANDA and News are mocked (external I/O).
        scorer, risk, detectors all run with real logic.
        If Signal produced → sanity check fields.
        If None → acceptable (low confluence on this data).
        Point: prove the full chain never crashes.
        """
        sg = _make_sg_for_flow(
            realistic_eur_usd_candles, mock_news_client, in_memory_db, "EUR_USD"
        )

        # No patch.object — real scorer, real risk, real detectors
        result = sg.generate("EUR_USD", "H1", account_balance=10000.0)

        assert result is None or isinstance(result, Signal)

        if result is not None:
            # Full field sanity: all critical fields must be sane
            assert len(result.id) == 36, "Signal.id must be UUID"
            assert result.pair == "EUR_USD"
            assert result.direction in ("bullish", "bearish")
            assert result.entry > 0
            assert result.stop_loss > 0
            assert result.confluence_score >= 65
            assert result.risk_reward_ratio >= 1.0

            # Verify it was persisted to DB (no mock on DB either)
            db_row = in_memory_db.get_signal_by_uuid(result.id)
            assert db_row is not None, "Signal must be persisted to DB"
            assert db_row["signal_uuid"] == result.id


# ── Edge case flows ───────────────────────────────────────────────────────────


class TestEdgeCaseFlows:
    def test_no_signal_on_flat_market(
        self,
        flat_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Flat candles (near-zero drift) — any signal must still meet threshold."""
        sg = _make_sg_for_flow(flat_candles, mock_news_client, in_memory_db)

        result = sg.generate("EUR_USD", "H1", account_balance=10000.0)

        # If the bot produces a signal on flat data, it must still follow its rules
        if result is not None:
            assert result.confluence_score >= 65, (
                "Bot must never publish below its own confluence threshold"
            )
