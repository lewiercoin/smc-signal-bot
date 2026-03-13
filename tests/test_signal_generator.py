"""Tests for engine/signal_generator.py — 12 tests covering full pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from connectors.oanda_client import Candle
from engine.confluence_scorer import ConfluenceResult, ScoreComponent
from engine.risk_engine import (
    PositionSize,
    SpreadCheck,
    TakeProfitLevels,
    TradeParameters,
)
from engine.signal_generator import Signal, SignalGenerator


# ── Fixtures & helpers ────────────────────────────────────────────────────────


def _make_candle(
    close: float = 1.1000,
    instrument: str = "EUR_USD",
    idx: int = 0,
) -> Candle:
    from datetime import timedelta
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=idx)
    c = close
    return Candle(
        instrument=instrument,
        timestamp=ts,
        open=c - 0.0002,
        high=c + 0.0005,
        low=c - 0.0005,
        close=c,
        volume=1000,
    )


def _make_candles(n: int = 100, base: float = 1.1000, instrument: str = "EUR_USD") -> list[Candle]:
    return [_make_candle(base + i * 0.0001, instrument, i) for i in range(n)]


def _make_score_component(name: str = "IPDA Zone", score: int = 25) -> ScoreComponent:
    return ScoreComponent(name=name, score=score, max_score=25, reason="test")


def _make_confluence_result(
    total_score: int = 80,
    direction: str = "bullish",
    pair: str = "EUR_USD",
) -> ConfluenceResult:
    comp = _make_score_component(score=total_score)
    return ConfluenceResult(
        total_score=total_score,
        max_possible=110,
        threshold=65,
        is_signal=total_score >= 65,
        setup_direction=direction,
        components=[comp],
        timestamp=datetime.now(tz=timezone.utc),
        pair=pair,
    )


def _make_trade_params(
    entry: float = 1.1000,
    pair: str = "EUR_USD",
    is_valid: bool = True,
    rejection_reason: str = "",
    rr: float = 1.5,
) -> TradeParameters:
    sl = entry - 0.0020
    tps = TakeProfitLevels(
        tp1=entry + 0.0030,
        tp2=entry + 0.0050,
        tp3=entry + 0.0070,
        ratios=(1.5, 2.5, 3.5),
    )
    ps = PositionSize(lots=1.00, risk_amount=200.0, risk_pct=0.02, sl_pips=20.0)
    sc = SpreadCheck(passed=True, current_spread=1.0, max_allowed=2.0)
    return TradeParameters(
        pair=pair,
        direction="bullish",
        entry=entry,
        stop_loss=sl,
        take_profits=tps,
        position_size=ps,
        spread_check=sc,
        risk_reward_ratio=rr,
        sl_distance=abs(entry - sl),
        atr_at_entry=0.0010,
        is_valid=is_valid,
        rejection_reason=rejection_reason,
        timestamp=datetime.now(tz=timezone.utc),
    )


def _make_news_result(is_blocked: bool = False, reason: str = "") -> MagicMock:
    r = MagicMock()
    r.is_blocked = is_blocked
    r.reason = reason
    return r


@pytest.fixture()
def sg() -> SignalGenerator:
    """SignalGenerator with all dependencies mocked."""
    oanda = MagicMock()
    news = MagicMock()
    db = MagicMock()

    candles = _make_candles(100)
    htf_candles = _make_candles(50)
    oanda.get_candles.side_effect = lambda pair, tf, count: (
        candles if count == 100 else htf_candles
    )
    oanda.get_current_spread.return_value = 0.0001  # 1 pip in price terms

    news.is_news_blocked.return_value = _make_news_result(is_blocked=False)

    gen = SignalGenerator(
        oanda_client=oanda,
        news_client=news,
        db=db,
        confluence_threshold=65,
    )
    return gen


# ── Full pipeline tests ───────────────────────────────────────────────────────


class TestGeneratePipeline:
    def test_generate_produces_signal_on_strong_setup(self, sg: SignalGenerator) -> None:
        """All gates pass → returns a fully-populated Signal."""
        confluence = _make_confluence_result(total_score=80, direction="bullish")
        trade = _make_trade_params()

        with (
            patch.object(sg.scorer, "score", return_value=confluence),
            patch.object(sg.risk, "calculate_trade", return_value=trade),
        ):
            result = sg.generate("EUR_USD", "H1")

        assert result is not None
        assert isinstance(result, Signal)
        assert result.pair == "EUR_USD"
        assert result.direction == "bullish"
        assert result.confluence_score == 80
        assert result.entry == pytest.approx(trade.entry)
        assert result.stop_loss == pytest.approx(trade.stop_loss)
        assert result.take_profit_1 == pytest.approx(trade.take_profits.tp1)
        assert result.take_profit_2 == pytest.approx(trade.take_profits.tp2)
        assert result.take_profit_3 == pytest.approx(trade.take_profits.tp3)
        assert result.position_size == pytest.approx(1.00)
        assert result.risk_reward_ratio == pytest.approx(1.5)
        assert result.risk_amount == pytest.approx(200.0)
        assert result.risk_pct == pytest.approx(0.02)
        assert result.status == "pending"
        assert len(result.id) == 36  # UUID format

    def test_generate_returns_none_on_low_confluence(self, sg: SignalGenerator) -> None:
        """Score below threshold → None."""
        confluence = _make_confluence_result(total_score=50)

        with patch.object(sg.scorer, "score", return_value=confluence):
            result = sg.generate("EUR_USD", "H1")

        assert result is None

    def test_generate_returns_none_on_news_block(self, sg: SignalGenerator) -> None:
        """News blackout active → None."""
        sg.news.is_news_blocked.return_value = _make_news_result(
            is_blocked=True, reason="NFP in 30 min"
        )
        confluence = _make_confluence_result(total_score=80)

        with patch.object(sg.scorer, "score", return_value=confluence):
            result = sg.generate("EUR_USD", "H1")

        assert result is None

    def test_generate_returns_none_on_bad_spread(self, sg: SignalGenerator) -> None:
        """Spread DQ fail → None.

        DQ checks spread in price terms. EUR_USD limit = 2.0 pips = 0.0002.
        We inject spread > limit to trigger DQ rejection.
        """
        sg.oanda.get_current_spread.return_value = 0.0003  # 3 pips > 2 pip limit
        confluence = _make_confluence_result(total_score=80)

        with patch.object(sg.scorer, "score", return_value=confluence):
            result = sg.generate("EUR_USD", "H1")

        assert result is None

    def test_generate_returns_none_on_invalid_trade(self, sg: SignalGenerator) -> None:
        """RiskEngine returns is_valid=False → None."""
        confluence = _make_confluence_result(total_score=80)
        trade = _make_trade_params(is_valid=False, rejection_reason="Risk:reward below minimum")

        with (
            patch.object(sg.scorer, "score", return_value=confluence),
            patch.object(sg.risk, "calculate_trade", return_value=trade),
        ):
            result = sg.generate("EUR_USD", "H1")

        assert result is None

    def test_generate_neutral_direction_returns_none(self, sg: SignalGenerator) -> None:
        """setup_direction='neutral' → None even with high score."""
        confluence = _make_confluence_result(total_score=80, direction="neutral")

        with patch.object(sg.scorer, "score", return_value=confluence):
            result = sg.generate("EUR_USD", "H1")

        assert result is None


# ── Data Quality gate tests ───────────────────────────────────────────────────


class TestDQGate:
    def test_dq_candles_fail_blocks_signal(self, sg: SignalGenerator) -> None:
        """Candle DQ failure (too few candles) → None."""
        # Return only 5 candles — well below 100 minimum
        sg.oanda.get_candles.side_effect = lambda pair, tf, count: (
            _make_candles(5) if count == 100 else _make_candles(50)
        )
        result = sg.generate("EUR_USD", "H1")
        assert result is None

    def test_dq_spread_fail_blocks_signal(self, sg: SignalGenerator) -> None:
        """Spread above DQ limit blocks signal before confluence step."""
        # 0.0005 = 5 pips for EUR_USD, limit is 2 pips (0.0002)
        sg.oanda.get_current_spread.return_value = 0.0005

        # Even with perfect confluence the spread gate fires first
        confluence = _make_confluence_result(total_score=90)
        with patch.object(sg.scorer, "score", return_value=confluence):
            result = sg.generate("EUR_USD", "H1")

        assert result is None


# ── Scan all pairs tests ──────────────────────────────────────────────────────


class TestScanAllPairs:
    def test_scan_all_pairs_returns_multiple_signals(self, sg: SignalGenerator) -> None:
        """2 out of 3 pairs produce valid Signals → list of 2."""
        good_confluence = _make_confluence_result(total_score=80)
        low_confluence = _make_confluence_result(total_score=40)
        good_trade = _make_trade_params()

        call_count = {"n": 0}

        def _score_side_effect(candles, htf_candles=None, pair="EUR_USD"):
            call_count["n"] += 1
            # Third pair (BTC_USD) gets low score
            if pair == "BTC_USD":
                return low_confluence
            return good_confluence

        with (
            patch.object(sg.scorer, "score", side_effect=_score_side_effect),
            patch.object(sg.risk, "calculate_trade", return_value=good_trade),
        ):
            signals = sg.scan_all_pairs("H1")

        assert len(signals) == 2
        pairs = {s.pair for s in signals}
        assert "EUR_USD" in pairs
        assert "XAU_USD" in pairs
        assert "BTC_USD" not in pairs

    def test_scan_all_pairs_one_error_doesnt_block_others(
        self, sg: SignalGenerator
    ) -> None:
        """OANDA error on one pair → other pairs still scanned, no exception."""
        good_confluence = _make_confluence_result(total_score=80)
        good_trade = _make_trade_params()

        original_get_candles = sg.oanda.get_candles.side_effect

        def _candles_with_error(pair, tf, count):
            if pair == "XAU_USD":
                raise ConnectionError("OANDA timeout")
            return original_get_candles(pair, tf, count)

        sg.oanda.get_candles.side_effect = _candles_with_error

        with (
            patch.object(sg.scorer, "score", return_value=good_confluence),
            patch.object(sg.risk, "calculate_trade", return_value=good_trade),
        ):
            signals = sg.scan_all_pairs("H1")

        # EUR_USD and BTC_USD should still produce signals; XAU_USD failed silently
        assert len(signals) == 2
        pairs = {s.pair for s in signals}
        assert "XAU_USD" not in pairs


# ── AI gate tests ─────────────────────────────────────────────────────────────


class TestAIGate:
    def test_risk_verifier_blocks_when_not_approved(self, sg: SignalGenerator) -> None:
        """Signal with score >= 65 but risk_approved=False → None."""
        from agents.risk_verifier import RiskVerifierResult

        confluence = _make_confluence_result(total_score=80)
        trade = _make_trade_params()

        blocked_result = RiskVerifierResult(
            risk_approved=False,
            position_size=0.0,
            risk_notes=["Daily loss limit reached"],
            rejection_reason="Daily loss limit reached (≥5%)",
        )

        with (
            patch.object(sg.scorer, "score", return_value=confluence),
            patch.object(sg.risk, "calculate_trade", return_value=trade),
            patch.object(sg.risk_verifier, "verify", return_value=blocked_result),
        ):
            result = sg.generate("EUR_USD", "H1")

        assert result is None

    def test_agents_called_when_score_above_60(self, sg: SignalGenerator) -> None:
        """Score >= 60 → structure_agent.analyze and risk_verifier.verify are called."""
        from agents.base_agent import AgentResult, AgentTier, MarketBias
        from agents.risk_verifier import RiskVerifierResult

        confluence = _make_confluence_result(total_score=65)
        trade = _make_trade_params()

        dummy = AgentResult(
            agent_name="test",
            tier_used=AgentTier.DETERMINISTIC,
            bias=MarketBias.BULLISH,
            confidence=0.7,
            reasoning="test",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        approved_result = RiskVerifierResult(
            risk_approved=True,
            position_size=1.0,
        )

        with (
            patch.object(sg.scorer, "score", return_value=confluence),
            patch.object(sg.risk, "calculate_trade", return_value=trade),
            patch.object(sg.structure_agent, "analyze", return_value=dummy) as mock_structure,
            patch.object(sg.fundamental_agent, "analyze", return_value=dummy) as mock_fundamental,
            patch.object(sg.risk_verifier, "verify", return_value=approved_result) as mock_risk,
        ):
            sg.generate("EUR_USD", "H1")

        mock_structure.assert_called_once()
        mock_fundamental.assert_called_once()
        mock_risk.assert_called_once()

    def test_agents_not_called_when_score_below_60(self, sg: SignalGenerator) -> None:
        """Score < 60 → AI agents are NOT called (saves API costs)."""
        confluence = _make_confluence_result(total_score=50)

        with (
            patch.object(sg.scorer, "score", return_value=confluence),
            patch.object(sg.structure_agent, "analyze") as mock_structure,
            patch.object(sg.fundamental_agent, "analyze") as mock_fundamental,
            patch.object(sg.risk_verifier, "verify") as mock_risk,
        ):
            result = sg.generate("EUR_USD", "H1")

        assert result is None
        mock_structure.assert_not_called()
        mock_fundamental.assert_not_called()
        mock_risk.assert_not_called()


# ── Edge case tests ───────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_generate_handles_oanda_error_gracefully(self, sg: SignalGenerator) -> None:
        """OANDA get_candles raises → None, no exception propagates."""
        sg.oanda.get_candles.side_effect = ConnectionError("OANDA timeout")

        result = sg.generate("EUR_USD", "H1")

        assert result is None

    def test_signal_saved_to_db(self, sg: SignalGenerator) -> None:
        """After successful generate, db.save_signal is called once with correct data."""
        confluence = _make_confluence_result(total_score=80)
        trade = _make_trade_params()

        with (
            patch.object(sg.scorer, "score", return_value=confluence),
            patch.object(sg.risk, "calculate_trade", return_value=trade),
        ):
            result = sg.generate("EUR_USD", "H1")

        assert result is not None
        sg.db.save_signal.assert_called_once()

        call_kwargs = sg.db.save_signal.call_args[0][0]
        assert call_kwargs["instrument"] == "EUR_USD"
        assert call_kwargs["direction"] == "bullish"
        assert call_kwargs["entry_price"] == pytest.approx(trade.entry)
        assert call_kwargs["confluence_score"] == 80
