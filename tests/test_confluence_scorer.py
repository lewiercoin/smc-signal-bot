"""Tests for engine/confluence_scorer.py — 15 tests.

Covers:
- All 8 scoring components (unit, with mocked detectors)
- Integration / edge cases
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.oanda_client import Candle
from engine.confluence_scorer import (
    ConfluenceScorer,
    _SIGNAL_THRESHOLD,
)
from smc.fvg_detector import FVG, FVGQuality
from smc.liquidity_detector import LiquiditySweep, SweepQuality
from smc.ob_detector import OBQuality, OrderBlock
from smc.structure_analyzer import BOS, CHoCH, StructureResult
from smc.swing_detector import SwingPoint, SwingResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(i: int = 0) -> datetime:
    """Return a deterministic UTC datetime offset by `i` hours."""
    return datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc) + timedelta(hours=i)


def _make_candle(
    close: float,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: int = 100,
    i: int = 0,
) -> Candle:
    """Build a synthetic Candle with sensible defaults."""
    o = open_ if open_ is not None else close
    h = high if high is not None else close + 0.001
    low_ = low if low is not None else close - 0.001
    return Candle(
        instrument="EUR_USD",
        timestamp=_ts(i),
        open=o,
        high=h,
        low=low_,
        close=close,
        volume=volume,
    )


def _make_candles(n: int = 50, base: float = 1.1000) -> list[Candle]:
    """Generate n synthetic candles with slight upward drift."""
    candles = []
    for i in range(n):
        price = base + i * 0.0001
        candles.append(_make_candle(close=price, open_=price - 0.0001, i=i))
    return candles


def _make_ob(
    ob_type: str = "bullish",
    quality_passed: bool = True,
    is_valid: bool = True,
) -> OrderBlock:
    quality = OBQuality(
        ob_age_bars=5,
        ob_touches=0,
        ob_size_atr=0.5,
        passed=quality_passed,
    )
    return OrderBlock(
        index=10,
        time=_ts(),
        ob_type=ob_type,
        high=1.1010,
        low=1.1000,
        open=1.1005,
        close=1.1002,
        atr_at_formation=0.0010,
        quality=quality,
        is_valid=is_valid,
    )


def _make_fvg(
    fvg_type: str = "bullish",
    fill: float = 0.1,
    quality_passed: bool = True,
    is_valid: bool = True,
) -> FVG:
    quality = FVGQuality(
        fvg_age_bars=3,
        fvg_size_atr=0.3,
        fill_percentage=fill,
        passed=quality_passed,
    )
    return FVG(
        index=12,
        time=_ts(),
        fvg_type=fvg_type,
        gap_high=1.1020,
        gap_low=1.1010,
        candle1_index=11,
        candle3_index=13,
        atr_at_formation=0.0010,
        quality=quality,
        is_valid=is_valid,
    )


def _make_sweep(
    sweep_type: str = "sellside",
    quality_passed: bool = True,
    is_valid: bool = True,
) -> LiquiditySweep:
    quality = SweepQuality(
        sweep_age_bars=2,
        penetration_atr=0.5,
        rejection_strength=0.8,
        level_tested_count=1,
        passed=quality_passed,
    )
    return LiquiditySweep(
        index=15,
        time=_ts(),
        sweep_type=sweep_type,
        level_price=1.0990,
        sweep_high=1.1000,
        sweep_low=1.0985,
        penetration=0.0005,
        swing_index=10,
        atr_at_sweep=0.0010,
        quality=quality,
        is_valid=is_valid,
    )


def _make_swing_point(
    index: int = 5,
    price: float = 1.1000,
    swing_type: str = "high",
) -> SwingPoint:
    return SwingPoint(
        index=index,
        price=price,
        time=_ts(),
        swing_type=swing_type,
        strength=10,
    )


def _make_structure(
    trend: str = "bullish",
    has_bos: bool = True,
    has_choch: bool = False,
) -> StructureResult:
    bos = None
    if has_bos:
        sp = _make_swing_point()
        bos = BOS(price=1.1010, time=_ts(), direction=trend, swing_point=sp)

    choch = None
    if has_choch:
        sp = _make_swing_point()
        to_trend = "bullish" if trend == "bearish" else "bearish"
        choch = CHoCH(
            price=1.1005,
            time=_ts(),
            from_trend=trend,
            to_trend=to_trend,
            swing_point=sp,
        )

    return StructureResult(
        trend=trend,
        last_bos=bos,
        last_choch=choch,
        htf_bias="long" if trend == "bullish" else "short",
        key_highs=[_make_swing_point(swing_type="high")],
        key_lows=[_make_swing_point(swing_type="low")],
    )


# ---------------------------------------------------------------------------
# Component tests (unit)
# ---------------------------------------------------------------------------

class TestIPDAZone:
    def _scorer(self) -> ConfluenceScorer:
        return ConfluenceScorer()

    def test_ipda_deep_discount_25pts(self):
        """Position ≤ 0.20 in range → 25 pts."""
        # range 0.0 to 1.0 → current price = 0.10 → position = 0.10
        candles = [_make_candle(close=float(i) * 0.01, i=i) for i in range(20)]
        candles[-1] = _make_candle(close=0.01, high=0.011, low=0.009, i=19)
        # Force deterministic range: set explicit high/low extremes
        candles[0] = _make_candle(close=0.0, high=0.0001, low=0.0, i=0)
        candles[19] = _make_candle(close=0.0, high=0.0001, low=0.0, i=19)
        candles[10] = _make_candle(close=1.0, high=1.0, low=1.0, i=10)

        scorer2 = self._scorer()
        comp = scorer2._score_ipda_zone(candles, "EUR_USD")
        assert comp.score == 25
        assert comp.max_score == 25

    def test_ipda_equilibrium_0pts(self):
        """Position in 0.40-0.60 → 0 pts."""
        scorer = self._scorer()
        # Build 20 candles: range_low=1.0, range_high=2.0 → close=1.5 → position=0.5
        candles = []
        for i in range(20):
            candles.append(_make_candle(close=1.5, high=1.5, low=1.5, i=i))
        candles[0] = _make_candle(close=1.0, high=1.0, low=1.0, i=0)
        candles[10] = _make_candle(close=2.0, high=2.0, low=2.0, i=10)
        candles[19] = _make_candle(close=1.5, high=1.5, low=1.5, i=19)

        comp = scorer._score_ipda_zone(candles, "EUR_USD")
        assert comp.score == 0

    def _make_ipda_candles(
        self, range_low: float, range_high: float, current_price: float
    ) -> list[Candle]:
        """Helper: 20 candles where high=range_high, low=range_low, last close=current_price."""
        candles = [_make_candle(close=1.1000, high=1.1001, low=1.0999, i=i) for i in range(20)]
        candles[5] = _make_candle(close=range_high, high=range_high, low=range_high, i=5)
        candles[6] = _make_candle(close=range_low, high=range_low, low=range_low, i=6)
        candles[19] = _make_candle(
            close=current_price,
            high=current_price + 0.0001,
            low=current_price - 0.0001,
            i=19,
        )
        return candles

    def test_ipda_deep_discount_explicit(self):
        scorer = self._scorer()
        candles = self._make_ipda_candles(1.0000, 2.0000, 1.1000)
        comp = scorer._score_ipda_zone(candles, "EUR_USD")
        assert comp.score == 25
        assert comp.details["position"] <= 0.20

    def test_ipda_equilibrium_explicit(self):
        scorer = self._scorer()
        candles = self._make_ipda_candles(1.0000, 2.0000, 1.4500)
        comp = scorer._score_ipda_zone(candles, "EUR_USD")
        assert comp.score == 0
        assert 0.40 < comp.details["position"] < 0.60


class TestOBQuality:
    def test_ob_quality_passed_20pts(self):
        """Valid OB, quality.passed=True → 20 pts."""
        scorer = ConfluenceScorer()
        ob = _make_ob(ob_type="bullish", quality_passed=True)
        comp = scorer._score_ob_quality([ob], "bullish")
        assert comp.score == 20
        assert comp.max_score == 20

    def test_ob_absent_0pts(self):
        """No OB in setup direction → 0 pts."""
        scorer = ConfluenceScorer()
        ob = _make_ob(ob_type="bearish")
        comp = scorer._score_ob_quality([ob], "bullish")
        assert comp.score == 0

    def test_ob_quality_failed_10pts(self):
        """OB present but quality.passed=False → 10 pts."""
        scorer = ConfluenceScorer()
        ob = _make_ob(ob_type="bullish", quality_passed=False)
        comp = scorer._score_ob_quality([ob], "bullish")
        assert comp.score == 10


class TestFVGScoring:
    def test_fvg_fresh_15pts(self):
        """Valid FVG, quality.passed=True, fill < 0.3 → 15 pts."""
        scorer = ConfluenceScorer()
        fvg = _make_fvg(fvg_type="bullish", fill=0.1, quality_passed=True)
        comp = scorer._score_fvg([fvg], "bullish")
        assert comp.score == 15

    def test_fvg_partially_filled_10pts(self):
        """Valid FVG, quality.passed=True, fill 0.3-0.5 → 10 pts."""
        scorer = ConfluenceScorer()
        fvg = _make_fvg(fvg_type="bullish", fill=0.4, quality_passed=True)
        comp = scorer._score_fvg([fvg], "bullish")
        assert comp.score == 10


class TestLiquiditySweepScoring:
    def test_liquidity_sweep_passed_15pts(self):
        """Valid sellside sweep for bullish setup → 15 pts."""
        scorer = ConfluenceScorer()
        sweep = _make_sweep(sweep_type="sellside", quality_passed=True)
        comp = scorer._score_liquidity_sweep([sweep], "bullish")
        assert comp.score == 15

    def test_liquidity_sweep_absent_0pts(self):
        """No matching sweep → 0 pts."""
        scorer = ConfluenceScorer()
        comp = scorer._score_liquidity_sweep([], "bullish")
        assert comp.score == 0


class TestSessionScoring:
    def test_session_overlap_10pts(self):
        """14:00 UTC is London/NY overlap → 10 pts."""
        scorer = ConfluenceScorer()
        fixed_dt = datetime(2024, 1, 2, 14, 0, 0, tzinfo=timezone.utc)
        with patch("engine.confluence_scorer.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            comp = scorer._score_session("EUR_USD")
        assert comp.score == 10

    def test_session_asian_3pts(self):
        """03:00 UTC for EUR_USD → 3 pts."""
        scorer = ConfluenceScorer()
        fixed_dt = datetime(2024, 1, 2, 3, 0, 0, tzinfo=timezone.utc)
        with patch("engine.confluence_scorer.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            comp = scorer._score_session("EUR_USD")
        assert comp.score == 3


class TestStructureScoring:
    def test_structure_bos_10pts(self):
        """BOS confirmed → 10 pts."""
        scorer = ConfluenceScorer()
        structure = _make_structure(trend="bullish", has_bos=True, has_choch=False)
        comp = scorer._score_structure(structure)
        assert comp.score == 10

    def test_structure_no_bos_no_choch_0pts(self):
        """No BOS, no CHoCH → 0 pts."""
        scorer = ConfluenceScorer()
        structure = _make_structure(trend="ranging", has_bos=False, has_choch=False)
        comp = scorer._score_structure(structure)
        assert comp.score == 0


class TestAbsorptionScoring:
    def _make_absorption_candles(
        self,
        body_ratio: float = 0.85,
        volume_spike: float = 2.0,
        n_base: int = 30,
    ) -> list[Candle]:
        """Build candle list where last 3 candles have large body_ratio + volume spike.

        GROK-1: body_ratio = abs(close-open)/(high-low) > 0.70 = absorption candle.
        Large body + small wicks = strong directional pressure = institution absorbing.
        """
        avg_vol = 100
        spike_vol = int(avg_vol * volume_spike)

        base_candles = [
            _make_candle(close=1.1000 + i * 0.0001, volume=avg_vol, i=i)
            for i in range(n_base)
        ]

        hl_range = 0.0010
        body = hl_range * body_ratio
        mid = 1.1100
        absorption_candles = []
        for j in range(3):
            idx = n_base + j
            c = Candle(
                instrument="EUR_USD",
                timestamp=_ts(idx),
                open=mid - body / 2,
                high=mid + hl_range / 2,
                low=mid - hl_range / 2,
                close=mid + body / 2,
                volume=spike_vol if j == 2 else avg_vol,
            )
            absorption_candles.append(c)

        return base_candles + absorption_candles

    def test_absorption_detected_10pts(self):
        """3 candles with body_ratio > 0.70 (large body) + vol_spike > 1.5 → 10 pts."""
        scorer = ConfluenceScorer()
        candles = self._make_absorption_candles(body_ratio=0.85, volume_spike=2.0)
        comp = scorer._score_absorption(candles)
        assert comp.score == 10

    def test_absorption_small_body_0pts(self):
        """Body ratio ≤ 0.70 (small body = doji, NOT absorption) → 0 pts."""
        scorer = ConfluenceScorer()
        candles = self._make_absorption_candles(body_ratio=0.30, volume_spike=2.0)
        comp = scorer._score_absorption(candles)
        assert comp.score == 0


class TestHTFBiasScoring:
    def test_htf_bias_aligned_5pts(self):
        """HTF bullish + setup bullish → 5 pts."""
        scorer = ConfluenceScorer()
        htf_structure = _make_structure(trend="bullish", has_bos=True)
        comp = scorer._score_htf_bias(htf_structure, "bullish")
        assert comp.score == 5

    def test_htf_bias_no_data_0pts(self):
        """No HTF data → 0 pts."""
        scorer = ConfluenceScorer()
        comp = scorer._score_htf_bias(None, "bullish")
        assert comp.score == 0


# ---------------------------------------------------------------------------
# Integration / edge case tests
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_insufficient_candles_score_zero(self):
        """< 14 candles → score=0, reason contains 'Insufficient'."""
        scorer = ConfluenceScorer()
        candles = _make_candles(n=10)
        result = scorer.score(candles, pair="EUR_USD")
        assert result.total_score == 0
        assert not result.is_signal
        assert "Insufficient" in result.reason


class TestIntegration:
    """Integration tests using real detectors with controlled datasets."""

    def _make_trending_candles(self, n: int = 60) -> list[Candle]:
        """Create a clear bullish trending sequence for integration tests."""
        candles = []
        base = 1.1000
        for i in range(n):
            o = base + i * 0.0002
            h = o + 0.0015
            low_ = o - 0.0005
            c = o + 0.0010
            candles.append(
                Candle(
                    instrument="EUR_USD",
                    timestamp=_ts(i),
                    open=o,
                    high=h,
                    low=low_,
                    close=c,
                    volume=150,
                )
            )
        return candles

    def test_full_confluence_above_threshold(self):
        """All scoring components mocked at max → score > 65, is_signal=True."""
        scorer = ConfluenceScorer()
        candles = _make_candles(n=50)

        with (
            patch.object(scorer.swing_detector, "detect") as mock_swing,
            patch.object(scorer.structure_analyzer, "analyze") as mock_struct,
            patch.object(scorer.ob_detector, "detect") as mock_ob,
            patch.object(scorer.fvg_detector, "detect") as mock_fvg,
            patch.object(scorer.liquidity_detector, "detect") as mock_liq,
            patch("engine.confluence_scorer.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = datetime(2024, 1, 2, 14, 0, 0, tzinfo=timezone.utc)

            sp_high = _make_swing_point(index=20, swing_type="high")
            sp_low = _make_swing_point(index=25, swing_type="low", price=1.0990)
            mock_swing.return_value = SwingResult(
                highs=[sp_high],
                lows=[sp_low],
                swing_length_used=10,
                volatility_regime="normal",
            )

            mock_struct.return_value = _make_structure("bullish", has_bos=True)
            mock_ob.return_value = [_make_ob("bullish", quality_passed=True)]
            mock_fvg.return_value = [_make_fvg("bullish", fill=0.1, quality_passed=True)]

            sweep = _make_sweep("sellside", quality_passed=True)
            mock_liq.return_value = [sweep]

            result = scorer.score(candles, pair="EUR_USD")

        assert result.total_score > _SIGNAL_THRESHOLD
        assert result.is_signal is True
        assert result.setup_direction == "bullish"

    def test_weak_setup_below_threshold(self):
        """All scoring components mocked at near-zero → score < 65, is_signal=False."""
        scorer = ConfluenceScorer()
        candles = _make_candles(n=50)

        with (
            patch.object(scorer.swing_detector, "detect") as mock_swing,
            patch.object(scorer.structure_analyzer, "analyze") as mock_struct,
            patch.object(scorer.ob_detector, "detect") as mock_ob,
            patch.object(scorer.fvg_detector, "detect") as mock_fvg,
            patch.object(scorer.liquidity_detector, "detect") as mock_liq,
            patch("engine.confluence_scorer.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = datetime(2024, 1, 2, 3, 0, 0, tzinfo=timezone.utc)

            sp_high = _make_swing_point(index=20, swing_type="high")
            sp_low = _make_swing_point(index=25, swing_type="low", price=1.0990)
            mock_swing.return_value = SwingResult(
                highs=[sp_high],
                lows=[sp_low],
                swing_length_used=10,
                volatility_regime="normal",
            )

            structure = StructureResult(
                trend="bullish",
                last_bos=None,
                last_choch=None,
                htf_bias="long",
                key_highs=[sp_high],
                key_lows=[sp_low],
            )
            mock_struct.return_value = structure

            mock_ob.return_value = []
            mock_fvg.return_value = []

            sweep = _make_sweep("sellside", quality_passed=False)
            mock_liq.return_value = [sweep]

            result = scorer.score(candles, pair="EUR_USD")

        assert result.total_score < _SIGNAL_THRESHOLD
        assert result.is_signal is False
