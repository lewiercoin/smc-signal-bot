"""Tests for smc/liquidity_detector.py — Liquidity Sweep detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from smc.liquidity_detector import LiquidityDetector
from smc.swing_detector import SwingPoint, SwingResult


@dataclass(frozen=True)
class _Candle:
    instrument: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


_BASE = datetime(2026, 1, 1)


def _c(
    i: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int = 1000,
) -> _Candle:
    return _Candle(
        instrument="EUR_USD",
        timestamp=_BASE + timedelta(hours=i),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _flat_candles(n: int, price: float = 1.1000, spread: float = 0.0010, offset: int = 0) -> list[_Candle]:
    """Generate n flat candles with consistent OHLC around `price`."""
    return [
        _c(offset + i, price, price + spread, price - spread, price)
        for i in range(n)
    ]


def _make_swing_high(index: int, price: float) -> SwingPoint:
    return SwingPoint(
        index=index,
        price=price,
        time=_BASE + timedelta(hours=index),
        swing_type="high",
        strength=5,
    )


def _make_swing_low(index: int, price: float) -> SwingPoint:
    return SwingPoint(
        index=index,
        price=price,
        time=_BASE + timedelta(hours=index),
        swing_type="low",
        strength=5,
    )


def _make_swing_result(
    highs: list[SwingPoint] | None = None,
    lows: list[SwingPoint] | None = None,
) -> SwingResult:
    return SwingResult(
        highs=highs or [],
        lows=lows or [],
        swing_length_used=10,
        volatility_regime="normal",
    )


def _candles_with_buyside_sweep() -> tuple[list[_Candle], SwingResult]:
    """
    Build 20 padding candles + swing high + sweep candle.
    Swing high at index 18, price=1.1020.
    Sweep candle at index 19: high=1.1030 (above swing), close=1.1010 (back inside).
    ATR(14) will be non-None from index 14 onward.
    """
    padding = _flat_candles(19, price=1.1000, spread=0.0010)
    sweep_candle = _c(19, 1.1010, 1.1030, 1.0990, 1.1010)
    candles = padding + [sweep_candle]

    swing_high = _make_swing_high(index=18, price=1.1020)
    swings = _make_swing_result(highs=[swing_high])
    return candles, swings


def _candles_with_sellside_sweep() -> tuple[list[_Candle], SwingResult]:
    """
    Build 20 padding candles + sweep candle.
    Swing low at index 18, price=1.0980.
    Sweep candle at index 19: low=1.0970 (below swing), close=1.0985 (back inside).
    """
    padding = _flat_candles(19, price=1.1000, spread=0.0010)
    sweep_candle = _c(19, 1.0990, 1.1005, 1.0970, 1.0990)
    candles = padding + [sweep_candle]

    swing_low = _make_swing_low(index=18, price=1.0980)
    swings = _make_swing_result(lows=[swing_low])
    return candles, swings


class TestLiquidityDetection:
    def test_detects_buyside_sweep(self) -> None:
        """wick above swing high + close at/below → buyside sweep detected."""
        detector = LiquidityDetector()
        candles, swings = _candles_with_buyside_sweep()
        sweeps = detector.detect(candles, swings)

        buyside = [s for s in sweeps if s.sweep_type == "buyside"]
        assert len(buyside) >= 1

        s = buyside[0]
        assert s.sweep_type == "buyside"
        assert s.level_price == 1.1020
        assert s.sweep_high > s.level_price
        assert s.penetration > 0

    def test_detects_sellside_sweep(self) -> None:
        """wick below swing low + close at/above → sellside sweep detected."""
        detector = LiquidityDetector()
        candles, swings = _candles_with_sellside_sweep()
        sweeps = detector.detect(candles, swings)

        sellside = [s for s in sweeps if s.sweep_type == "sellside"]
        assert len(sellside) >= 1

        s = sellside[0]
        assert s.sweep_type == "sellside"
        assert s.level_price == 1.0980
        assert s.sweep_low < s.level_price
        assert s.penetration > 0

    def test_no_sweep_when_close_beyond_level(self) -> None:
        """Close breaks through swing high → not a sweep (breakout), no detection."""
        detector = LiquidityDetector()
        padding = _flat_candles(19, price=1.1000, spread=0.0010)
        breakout_candle = _c(19, 1.1010, 1.1040, 1.0995, 1.1035)
        candles = padding + [breakout_candle]

        swing_high = _make_swing_high(index=18, price=1.1020)
        swings = _make_swing_result(highs=[swing_high])

        sweeps = detector.detect(candles, swings)
        buyside = [s for s in sweeps if s.sweep_type == "buyside"]
        assert buyside == []

    def test_no_sweep_without_swings(self) -> None:
        """Empty swing highs and lows → empty list, no exception."""
        detector = LiquidityDetector()
        candles = _flat_candles(20)
        swings = _make_swing_result(highs=[], lows=[])

        sweeps = detector.detect(candles, swings)
        assert sweeps == []

    def test_multiple_sweeps_sorted_newest_first(self) -> None:
        """Multiple sweeps in series → sorted by index descending."""
        detector = LiquidityDetector()

        padding = _flat_candles(16, price=1.1000, spread=0.0010)

        sweep1 = _c(16, 1.1010, 1.1030, 1.0990, 1.1015)
        bridge = _flat_candles(3, price=1.1015, spread=0.0005, offset=17)
        sweep2 = _c(20, 1.1020, 1.1045, 1.1010, 1.1022)

        candles = padding + [sweep1] + bridge + [sweep2]

        swing_high1 = _make_swing_high(index=15, price=1.1020)
        swing_high2 = _make_swing_high(index=19, price=1.1035)
        swings = _make_swing_result(highs=[swing_high1, swing_high2])

        sweeps = detector.detect(candles, swings)
        assert len(sweeps) >= 2
        indices = [s.index for s in sweeps]
        assert indices == sorted(indices, reverse=True)

    def test_breakout_filtered_by_penetration_depth(self) -> None:
        """Penetration > 2.0 ATR → filtered out as breakout, not sweep."""
        detector = LiquidityDetector()

        padding = _flat_candles(19, price=1.1000, spread=0.0010)
        swing_high = _make_swing_high(index=18, price=1.1020)

        atr_approx = 0.0020
        deep_wick_high = 1.1020 + atr_approx * 3.0
        deep_candle = _c(19, 1.1010, deep_wick_high, 1.0990, 1.1015)
        candles = padding + [deep_candle]

        swings = _make_swing_result(highs=[swing_high])
        sweeps = detector.detect(candles, swings)

        buyside = [s for s in sweeps if s.sweep_type == "buyside"]
        assert buyside == []


class TestSweepQuality:
    def test_sweep_quality_passes(self) -> None:
        """Fresh sweep, good penetration, strong rejection → passed=True."""
        detector = LiquidityDetector()
        candles, swings = _candles_with_buyside_sweep()
        sweeps = detector.detect(candles, swings)

        buyside = [s for s in sweeps if s.sweep_type == "buyside"]
        assert buyside
        s = buyside[0]

        assert s.quality is not None
        assert s.quality.sweep_age_bars < 20
        assert s.quality.penetration_atr > 0.05
        assert s.quality.penetration_atr < 2.0
        assert s.quality.rejection_strength > 0.0
        assert s.quality.passed is True

    def test_sweep_quality_fails_too_old(self) -> None:
        """sweep_age_bars >= 20 → passed=False."""
        detector = LiquidityDetector()

        padding = _flat_candles(19, price=1.1000, spread=0.0010)
        sweep_candle = _c(19, 1.1010, 1.1030, 1.0990, 1.1015)
        tail = _flat_candles(25, price=1.1015, spread=0.0005, offset=20)
        candles = padding + [sweep_candle] + tail

        swing_high = _make_swing_high(index=18, price=1.1020)
        swings = _make_swing_result(highs=[swing_high])

        sweeps = detector.detect(candles, swings)
        buyside = [s for s in sweeps if s.sweep_type == "buyside"]
        assert buyside

        old_sweep = min(buyside, key=lambda s: s.index)
        assert old_sweep.quality is not None
        assert old_sweep.quality.sweep_age_bars >= 20
        assert old_sweep.quality.passed is False

    def test_sweep_quality_fails_too_deep(self) -> None:
        """penetration > 2.0 ATR → filtered before quality check (no sweep in result)."""
        detector = LiquidityDetector()

        padding = _flat_candles(19, price=1.1000, spread=0.0010)
        atr_approx = 0.0020
        deep_high = 1.1020 + atr_approx * 2.5
        deep_candle = _c(19, 1.1010, deep_high, 1.0990, 1.1015)
        candles = padding + [deep_candle]

        swing_high = _make_swing_high(index=18, price=1.1020)
        swings = _make_swing_result(highs=[swing_high])

        sweeps = detector.detect(candles, swings)
        buyside = [s for s in sweeps if s.sweep_type == "buyside"]
        assert buyside == []

    def test_sweep_quality_fails_noise(self) -> None:
        """penetration < 0.05 ATR → passed=False (noise, not a real sweep)."""
        detector = LiquidityDetector()

        padding = _flat_candles(19, price=1.1000, spread=0.0010)
        atr_approx = 0.0020
        tiny_wick_high = 1.1020 + atr_approx * 0.02
        noise_candle = _c(19, 1.1010, tiny_wick_high, 1.0990, 1.1015)
        candles = padding + [noise_candle]

        swing_high = _make_swing_high(index=18, price=1.1020)
        swings = _make_swing_result(highs=[swing_high])

        sweeps = detector.detect(candles, swings)
        buyside = [s for s in sweeps if s.sweep_type == "buyside"]

        if buyside:
            assert buyside[0].quality is not None
            assert buyside[0].quality.passed is False
        else:
            pass


class TestSweepValidity:
    def test_sweep_invalidated_by_next_candle_breakout(self) -> None:
        """Next candle closes beyond level → sweep is invalidated (breakout confirmed)."""
        detector = LiquidityDetector()

        padding = _flat_candles(19, price=1.1000, spread=0.0010)
        sweep_candle = _c(19, 1.1010, 1.1030, 1.0990, 1.1015)
        breakout_confirm = _c(20, 1.1015, 1.1050, 1.1010, 1.1045)
        candles = padding + [sweep_candle, breakout_confirm]

        swing_high = _make_swing_high(index=18, price=1.1020)
        swings = _make_swing_result(highs=[swing_high])

        sweeps = detector.detect(candles, swings)
        buyside = [s for s in sweeps if s.sweep_type == "buyside"]
        assert buyside

        invalidated = [s for s in buyside if s.index == 19]
        assert invalidated
        assert invalidated[0].is_valid is False

    def test_invalid_sweep_fails_quality_check(self) -> None:
        """is_valid=False → quality.passed must be False (logical consistency)."""
        detector = LiquidityDetector()

        padding = _flat_candles(19, price=1.1000, spread=0.0010)
        sweep_candle = _c(19, 1.1010, 1.1030, 1.0990, 1.1015)
        breakout_confirm = _c(20, 1.1015, 1.1050, 1.1010, 1.1045)
        candles = padding + [sweep_candle, breakout_confirm]

        swing_high = _make_swing_high(index=18, price=1.1020)
        swings = _make_swing_result(highs=[swing_high])

        sweeps = detector.detect(candles, swings)
        for sweep in sweeps:
            if not sweep.is_valid:
                assert sweep.quality is not None
                assert sweep.quality.passed is False
