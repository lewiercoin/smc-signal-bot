"""Integration tests for the full SMC pipeline.

Tests the entire chain from candle data through SMC detectors,
confluence scoring, risk engine to signal output.
No mocks on internal components — only external APIs are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from connectors.oanda_client import Candle
from db.database import Database
from engine.signal_generator import Signal, SignalGenerator
from smc.fvg_detector import FVG, FairValueGapDetector
from smc.liquidity_detector import LiquiditySweep, LiquidityDetector
from smc.ob_detector import OrderBlock, OrderBlockDetector
from smc.swing_detector import SwingDetector, SwingResult


# ── Helpers ───────────────────────────────────────────────────────────────────


# Realistic spreads in price units (ask - bid), matching get_current_spread() output.
# EUR_USD pip=0.0001: 1.2 pip = 0.00012; XAU pip=0.01: 30 cent = 0.30; BTC pip=1.0: $20 = 20.0
REALISTIC_SPREADS: dict[str, float] = {
    "EUR_USD": 0.00012,
    "XAU_USD": 0.30,
    "BTC_USD": 20.0,
}


def _make_sg(
    candles: list[Candle],
    mock_news: MagicMock,
    db: Database,
    pair: str = "EUR_USD",
    spread: float | None = None,
) -> SignalGenerator:
    """Build a SignalGenerator with mocked external APIs, real internals.

    spread defaults to REALISTIC_SPREADS[pair] if not provided.
    """
    resolved_spread = spread if spread is not None else REALISTIC_SPREADS.get(pair, 0.00012)
    oanda = MagicMock()
    # Both LTF (count=100) and HTF (count=50) calls return the same candles
    oanda.get_candles.return_value = candles
    oanda.get_current_spread.return_value = resolved_spread

    return SignalGenerator(
        oanda_client=oanda,
        news_client=mock_news,
        db=db,
        confluence_threshold=65,
    )


# ── Pipeline end-to-end ───────────────────────────────────────────────────────


class TestFullPipelineEndToEnd:
    def test_full_pipeline_eur_usd_produces_result(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """EUR/USD full pipeline — returns Signal or None, never raises."""
        sg = _make_sg(realistic_eur_usd_candles, mock_news_client, in_memory_db, "EUR_USD")

        result = sg.generate("EUR_USD", "H1", account_balance=10000.0)  # spread=0.00012

        assert result is None or isinstance(result, Signal)
        if result is not None:
            assert result.pair == "EUR_USD"
            assert result.direction in ("bullish", "bearish")
            assert result.entry > 0
            assert result.stop_loss > 0
            assert result.confluence_score >= 65

    def test_full_pipeline_xau_usd_produces_result(
        self,
        realistic_xau_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """XAU/USD full pipeline — returns Signal or None, never raises."""
        sg = _make_sg(realistic_xau_usd_candles, mock_news_client, in_memory_db, "XAU_USD")

        result = sg.generate("XAU_USD", "H1", account_balance=10000.0)

        assert result is None or isinstance(result, Signal)
        if result is not None:
            assert result.pair == "XAU_USD"
            assert result.direction in ("bullish", "bearish")
            assert result.entry > 0
            assert result.stop_loss > 0

    def test_full_pipeline_btc_usd_produces_result(
        self,
        realistic_btc_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """BTC/USD full pipeline — returns Signal or None, never raises."""
        sg = _make_sg(realistic_btc_usd_candles, mock_news_client, in_memory_db, "BTC_USD")

        result = sg.generate("BTC_USD", "H1", account_balance=10000.0)

        assert result is None or isinstance(result, Signal)
        if result is not None:
            assert result.pair == "BTC_USD"
            assert result.direction in ("bullish", "bearish")
            assert result.entry > 0

    def test_scan_all_pairs_no_crash(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """scan_all_pairs() with 3 mocked pairs — list returned, no exceptions."""
        oanda = MagicMock()
        oanda.get_candles.return_value = realistic_eur_usd_candles
        oanda.get_current_spread.return_value = REALISTIC_SPREADS["EUR_USD"]

        sg = SignalGenerator(
            oanda_client=oanda,
            news_client=mock_news_client,
            db=in_memory_db,
            confluence_threshold=65,
        )

        results = sg.scan_all_pairs("H1")

        assert isinstance(results, list)
        assert len(results) <= 3
        for sig in results:
            assert isinstance(sig, Signal)


# ── DQ gate integration ───────────────────────────────────────────────────────


class TestDQGateIntegration:
    def test_pipeline_blocked_by_news(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client_blocked: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """News blackout → generate() returns None."""
        sg = _make_sg(realistic_eur_usd_candles, mock_news_client_blocked, in_memory_db, "EUR_USD")

        result = sg.generate("EUR_USD", "H1")

        assert result is None

    def test_pipeline_blocked_by_spread(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Spread=50 pips for EUR/USD in price units → DQ rejects → None.

        EUR/USD limit = 2 pips = 0.0002 price units.
        We pass 0.0050 = 50 pips → clearly above limit → DQ blocks.
        """
        sg = _make_sg(
            realistic_eur_usd_candles,
            mock_news_client,
            in_memory_db,
            "EUR_USD",
            spread=0.0050,  # 50 pips × 0.0001 — 25× above 2-pip EUR limit
        )

        result = sg.generate("EUR_USD", "H1")

        assert result is None

    def test_pipeline_passes_dq_with_clean_data(
        self,
        realistic_eur_usd_candles: list[Candle],
        mock_news_client: MagicMock,
        in_memory_db: Database,
    ) -> None:
        """Good candles + no news + normal spread → DQ does not block (pipeline proceeds)."""
        oanda = MagicMock()
        oanda.get_candles.return_value = realistic_eur_usd_candles
        oanda.get_current_spread.return_value = 0.00010  # 1 pip — well within EUR 2-pip limit

        sg = SignalGenerator(
            oanda_client=oanda,
            news_client=mock_news_client,
            db=in_memory_db,
            confluence_threshold=65,
        )

        # Should not raise; may return Signal or None (confluence decides)
        result = sg.generate("EUR_USD", "H1", account_balance=10000.0)
        assert result is None or isinstance(result, Signal)


# ── SMC → Confluence integration ─────────────────────────────────────────────


class TestSMCDetectorsConsistentTypes:
    def test_smc_detectors_produce_consistent_types(
        self,
        realistic_eur_usd_candles: list[Candle],
    ) -> None:
        """All SMC detectors run on realistic candles — output types are valid."""
        candles = realistic_eur_usd_candles

        # SwingDetector
        swing_det = SwingDetector()
        swing_result: SwingResult = swing_det.detect(candles)
        assert hasattr(swing_result, "highs")
        assert hasattr(swing_result, "lows")
        assert isinstance(swing_result.highs, list)
        assert isinstance(swing_result.lows, list)

        # OrderBlockDetector
        ob_det = OrderBlockDetector()
        obs: list[OrderBlock] = ob_det.detect(candles, swing_result)
        assert isinstance(obs, list)
        for ob in obs:
            assert ob.high > ob.low, "OB high must be above low"
            assert ob.ob_type in ("bullish", "bearish")
            assert ob.atr_at_formation > 0

        # FairValueGapDetector
        fvg_det = FairValueGapDetector()
        fvgs: list[FVG] = fvg_det.detect(candles)
        assert isinstance(fvgs, list)
        for fvg in fvgs:
            assert fvg.gap_high > fvg.gap_low, "FVG gap_high must be above gap_low"
            assert fvg.fvg_type in ("bullish", "bearish")

        # LiquidityDetector
        liq_det = LiquidityDetector()
        sweeps: list[LiquiditySweep] = liq_det.detect(candles, swing_result)
        assert isinstance(sweeps, list)
        for sweep in sweeps:
            assert sweep.penetration > 0
            assert sweep.sweep_type in ("buyside", "sellside")

    def test_confluence_scorer_receives_valid_detector_output(
        self,
        realistic_eur_usd_candles: list[Candle],
    ) -> None:
        """Detectors → ConfluenceScorer.score() → valid ConfluenceResult."""
        from engine.confluence_scorer import ConfluenceResult, ConfluenceScorer

        scorer = ConfluenceScorer()
        result: ConfluenceResult = scorer.score(
            candles=realistic_eur_usd_candles,
            htf_candles=realistic_eur_usd_candles,
            pair="EUR_USD",
        )

        assert 0 <= result.total_score <= 110
        assert isinstance(result.is_signal, bool)
        assert result.setup_direction in ("bullish", "bearish", "neutral")
        assert result.pair == "EUR_USD"
        assert isinstance(result.components, list)


# ── Risk integration ──────────────────────────────────────────────────────────


class TestRiskEngineIntegration:
    def test_risk_engine_with_real_swings(
        self,
        realistic_eur_usd_candles: list[Candle],
    ) -> None:
        """SwingDetector → RiskEngine.calculate_trade() — output within bounds."""
        from engine.risk_engine import RiskEngine

        risk = RiskEngine(max_risk_pct=0.02)

        from smc.utils import calculate_atr_scalar
        atr = calculate_atr_scalar(realistic_eur_usd_candles, 14)

        for direction in ("bullish", "bearish"):
            trade = risk.calculate_trade(
                candles=realistic_eur_usd_candles,
                setup_direction=direction,
                pair="EUR_USD",
                account_balance=10000.0,
                current_spread=0.0001,
            )
            if trade is not None and trade.is_valid:
                sl_dist = abs(trade.entry - trade.stop_loss)
                assert sl_dist <= 5 * atr, "SL must not be further than 5× ATR"
                tp_dist = abs(trade.take_profits.tp3 - trade.entry)
                assert tp_dist <= 20 * atr, "TP3 must not be further than 20× ATR"

    def test_position_size_with_real_balance(
        self,
        realistic_eur_usd_candles: list[Candle],
    ) -> None:
        """Position sizing respects 2% risk rule with 10k balance."""
        from engine.risk_engine import RiskEngine

        risk = RiskEngine(max_risk_pct=0.02)

        trade = risk.calculate_trade(
            candles=realistic_eur_usd_candles,
            setup_direction="bullish",
            pair="EUR_USD",
            account_balance=10000.0,
            current_spread=0.0001,
        )

        if trade is not None and trade.is_valid:
            assert 0.01 <= trade.position_size.lots <= 10.0
            assert trade.position_size.risk_pct <= 0.02


# ── Agent integration ─────────────────────────────────────────────────────────


class TestAgentsTier3Integration:
    def test_agents_tier3_with_real_data(
        self,
        realistic_eur_usd_candles: list[Candle],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without API key all agents fall back to Tier 3 (deterministic).

        Verifies no crash and tier_used is CACHE or DETERMINISTIC (not LLM).
        """
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from agents.base_agent import AgentConfig, AgentTier
        from agents.structure_agent import StructureAgent
        from agents.fundamental_agent import FundamentalAgent

        config = AgentConfig(
            model="claude-haiku-4-5-20251001",
            temperature=0.2,
            max_tokens=1024,
            cache_ttl_seconds=86400,
            timeout_seconds=5.0,
            max_retries=1,
        )

        # Pass api_client=None → LLM call will fail → falls to deterministic
        structure_agent = StructureAgent(config=config, api_client=None)
        fundamental_agent = FundamentalAgent(config=config, api_client=None)

        structure_input = {
            "instrument": "EUR_USD",
            "timeframe": "H1",
            "structure_breaks": [{"type": "BOS", "direction": "bullish", "index": 50}],
            "swing_points": [
                {"type": "HL", "price": 1.0840, "index": 45},
                {"type": "HH", "price": 1.0870, "index": 48},
            ],
            "current_price": 1.0860,
            "atr": 0.0012,
        }

        fundamental_input = {
            "instrument": "EUR_USD",
            "direction": "bullish",
            "news_events": [],
            "session": "london",
            "current_hour_utc": 9,
        }

        s_result = structure_agent.analyze(structure_input)
        f_result = fundamental_agent.analyze(fundamental_input)

        assert s_result is not None
        assert f_result is not None
        # tier_used is AgentTier enum — CACHE or DETERMINISTIC (no live API key)
        assert s_result.tier_used in (AgentTier.CACHE, AgentTier.DETERMINISTIC)
        assert f_result.tier_used in (AgentTier.CACHE, AgentTier.DETERMINISTIC)
