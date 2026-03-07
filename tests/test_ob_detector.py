"""Tests for OrderBlockDetector.

Tests cover:
- Bullish/bearish OB detection from close-to-close impulse
- No OB without sufficient impulse
- OB invalidation by close through zone
- OB quality metrics (age, touches, size)
- Edge cases: insufficient candles, invalid OB fails quality check
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from smc.ob_detector import OBQuality, OrderBlock, OrderBlockDetector


def _make_candle(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    base_time: datetime,
    instrument: str = "EUR_USD",
    volume: int = 1000,
) -> object:
    """Create a mock Candle-like object without importing the real Candle class."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Candle:
        instrument: str
        timestamp: datetime
        open: float
        high: float
        low: float
        close: float
        volume: int

    return _Candle(
        instrument=instrument,
        timestamp=base_time + timedelta(hours=index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _make_swing_result() -> object:
    """Create a minimal SwingResult mock."""
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class _SwingResult:
        highs: list = field(default_factory=list)
        lows: list = field(default_factory=list)
        swing_length_used: int = 10
        volatility_regime: str = "normal"

    return _SwingResult()


def _flat_candles(n: int, base_time: datetime, price: float = 1.1000) -> list:
    """Generate n flat candles with small range (quiet market, consistent ATR)."""
    candles = []
    for i in range(n):
        candles.append(
            _make_candle(
                index=i,
                open_=price,
                high=price + 0.0010,
                low=price - 0.0010,
                close=price + 0.0002,
                base_time=base_time,
            )
        )
    return candles


@pytest.fixture
def base_time() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture
def detector() -> OrderBlockDetector:
    return OrderBlockDetector()


@pytest.fixture
def swings() -> object:
    return _make_swing_result()


class TestOrderBlockDetector:
    def test_detects_bullish_ob_before_impulse(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """Bearish candle before close-to-close bullish impulse > 1.5x ATR → bullish OB."""
        # 14 flat candles establish ATR ~ 0.0020 (high-low range)
        candles = _flat_candles(14, base_time)
        # OB candle: bearish (close < open)
        candles.append(
            _make_candle(14, open_=1.1010, high=1.1015, low=1.0995, close=1.0998, base_time=base_time)
        )
        # Impulse candle: close rises >> 1.5 * ATR above previous close (1.0998)
        # ATR ~ 0.0020, need close > 1.0998 + 1.5*0.0020 = 1.1028; use 1.1060
        candles.append(
            _make_candle(15, open_=1.1000, high=1.1065, low=1.0999, close=1.1060, base_time=base_time)
        )

        result = detector.detect(candles, swings)

        bullish_obs = [ob for ob in result if ob.ob_type == "bullish"]
        assert len(bullish_obs) >= 1
        ob = bullish_obs[0]
        assert ob.ob_type == "bullish"
        assert ob.close < ob.open  # OB candle is bearish

    def test_detects_bearish_ob_before_impulse(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """Bullish candle before close-to-close bearish impulse > 1.5x ATR → bearish OB."""
        candles = _flat_candles(14, base_time)
        # OB candle: bullish (close > open)
        candles.append(
            _make_candle(14, open_=1.0995, high=1.1015, low=1.0990, close=1.1008, base_time=base_time)
        )
        # Impulse candle: close drops >> 1.5 * ATR below previous close (1.1008)
        # ATR ~ 0.0020, need prev_close - close > 0.0030; use close=1.0970
        candles.append(
            _make_candle(15, open_=1.1005, high=1.1006, low=1.0968, close=1.0970, base_time=base_time)
        )

        result = detector.detect(candles, swings)

        bearish_obs = [ob for ob in result if ob.ob_type == "bearish"]
        assert len(bearish_obs) >= 1
        ob = bearish_obs[0]
        assert ob.ob_type == "bearish"
        assert ob.close > ob.open  # OB candle is bullish

    def test_no_ob_without_impulse(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """No OB when close-to-close move < 1.5x ATR."""
        # All candles flat, no displacement
        candles = _flat_candles(20, base_time)

        result = detector.detect(candles, swings)

        assert result == []

    def test_ob_invalidated_by_close_below(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """Bullish OB is invalid when a subsequent candle closes below ob.low."""
        candles = _flat_candles(14, base_time)
        # OB candle at index 14: bearish, low=1.0995
        candles.append(
            _make_candle(14, open_=1.1010, high=1.1015, low=1.0995, close=1.0998, base_time=base_time)
        )
        # Impulse candle at index 15: creates bullish OB
        candles.append(
            _make_candle(15, open_=1.1000, high=1.1065, low=1.0999, close=1.1060, base_time=base_time)
        )
        # Candle that closes below ob.low (1.0995) → invalidates bullish OB
        candles.append(
            _make_candle(16, open_=1.0998, high=1.1000, low=1.0980, close=1.0985, base_time=base_time)
        )

        result = detector.detect(candles, swings)

        bullish_obs = [ob for ob in result if ob.ob_type == "bullish"]
        assert len(bullish_obs) >= 1
        invalidated = [ob for ob in bullish_obs if ob.index == 14]
        assert len(invalidated) == 1
        assert invalidated[0].is_valid is False

    def test_ob_quality_passes(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """OB with age < 50, touches <= 2, size > 0.3 ATR → quality.passed=True."""
        candles = _flat_candles(14, base_time)
        candles.append(
            _make_candle(14, open_=1.1010, high=1.1020, low=1.0990, close=1.0998, base_time=base_time)
        )
        # Impulse: big bullish move
        candles.append(
            _make_candle(15, open_=1.1000, high=1.1070, low=1.0999, close=1.1065, base_time=base_time)
        )
        # 1 touching candle: wick enters OB zone but close stays above ob.low
        candles.append(
            _make_candle(16, open_=1.1050, high=1.1055, low=1.0995, close=1.1040, base_time=base_time)
        )

        result = detector.detect(candles, swings)

        bullish_obs = [ob for ob in result if ob.ob_type == "bullish" and ob.index == 14]
        assert len(bullish_obs) == 1
        quality = bullish_obs[0].quality
        assert quality is not None
        assert quality.ob_age_bars < 50
        assert quality.ob_touches <= 2
        assert quality.ob_size_atr > 0.3
        assert quality.passed is True

    def test_ob_quality_fails_too_old(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """OB with age >= 50 → quality.passed=False."""
        candles = _flat_candles(14, base_time)
        # OB at index 14
        candles.append(
            _make_candle(14, open_=1.1010, high=1.1020, low=1.0990, close=1.0998, base_time=base_time)
        )
        # Impulse at index 15
        candles.append(
            _make_candle(15, open_=1.1000, high=1.1070, low=1.0999, close=1.1065, base_time=base_time)
        )
        # Add 50 more flat candles so ob_age_bars = 50 + 1 = 51 >= 50
        for i in range(16, 67):
            candles.append(
                _make_candle(i, open_=1.1060, high=1.1070, low=1.1050, close=1.1065, base_time=base_time)
            )

        result = detector.detect(candles, swings)

        bullish_obs = [ob for ob in result if ob.ob_type == "bullish" and ob.index == 14]
        assert len(bullish_obs) == 1
        quality = bullish_obs[0].quality
        assert quality is not None
        assert quality.ob_age_bars >= 50
        assert quality.passed is False

    def test_ob_quality_fails_too_many_touches(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """OB with more than 2 touches → quality.passed=False."""
        candles = _flat_candles(14, base_time)
        # OB at index 14: low=1.0990, high=1.1020
        candles.append(
            _make_candle(14, open_=1.1010, high=1.1020, low=1.0990, close=1.0998, base_time=base_time)
        )
        # Impulse
        candles.append(
            _make_candle(15, open_=1.1000, high=1.1070, low=1.0999, close=1.1065, base_time=base_time)
        )
        # 3 touching candles: wick enters zone (low <= ob.high=1.1020) but close >= ob.low=1.0990
        for i in range(16, 19):
            candles.append(
                _make_candle(i, open_=1.1050, high=1.1055, low=1.1010, close=1.1040, base_time=base_time)
            )

        result = detector.detect(candles, swings)

        bullish_obs = [ob for ob in result if ob.ob_type == "bullish" and ob.index == 14]
        assert len(bullish_obs) == 1
        quality = bullish_obs[0].quality
        assert quality is not None
        assert quality.ob_touches > 2
        assert quality.passed is False

    def test_ob_size_in_atr_units(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """ob_size_atr = (ob.high - ob.low) / atr_at_formation."""
        candles = _flat_candles(14, base_time)
        # OB candle with known high/low spread
        candles.append(
            _make_candle(14, open_=1.1010, high=1.1025, low=1.0985, close=1.0998, base_time=base_time)
        )
        candles.append(
            _make_candle(15, open_=1.1000, high=1.1070, low=1.0999, close=1.1065, base_time=base_time)
        )

        result = detector.detect(candles, swings)

        bullish_obs = [ob for ob in result if ob.ob_type == "bullish" and ob.index == 14]
        assert len(bullish_obs) == 1
        ob = bullish_obs[0]
        quality = ob.quality
        assert quality is not None
        expected_size_atr = (ob.high - ob.low) / ob.atr_at_formation
        assert abs(quality.ob_size_atr - expected_size_atr) < 1e-9

    def test_insufficient_candles_returns_empty(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """len(candles) < 14 → returns empty list without raising exception."""
        candles = _flat_candles(10, base_time)

        result = detector.detect(candles, swings)

        assert result == []

    def test_invalid_ob_fails_quality_check(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """If is_valid=False, then quality.passed must also be False."""
        candles = _flat_candles(14, base_time)
        # OB at index 14: low=1.0995
        candles.append(
            _make_candle(14, open_=1.1010, high=1.1020, low=1.0995, close=1.0998, base_time=base_time)
        )
        # Impulse
        candles.append(
            _make_candle(15, open_=1.1000, high=1.1070, low=1.0999, close=1.1065, base_time=base_time)
        )
        # Candle that closes below ob.low=1.0995 → is_valid=False
        candles.append(
            _make_candle(16, open_=1.1000, high=1.1005, low=1.0980, close=1.0988, base_time=base_time)
        )

        result = detector.detect(candles, swings)

        bullish_obs = [ob for ob in result if ob.ob_type == "bullish" and ob.index == 14]
        assert len(bullish_obs) == 1
        ob = bullish_obs[0]
        assert ob.is_valid is False
        assert ob.quality is not None
        assert ob.quality.passed is False

    def test_ob_dataclass_immutability(
        self, detector: OrderBlockDetector, base_time: datetime, swings: object
    ) -> None:
        """OrderBlock and OBQuality are frozen dataclasses."""
        quality = OBQuality(ob_age_bars=5, ob_touches=1, ob_size_atr=0.5, passed=True)
        ob = OrderBlock(
            index=5,
            time=base_time,
            ob_type="bullish",
            high=1.1020,
            low=1.0990,
            open=1.1010,
            close=1.0998,
            atr_at_formation=0.0020,
            quality=quality,
            is_valid=True,
        )

        with pytest.raises(AttributeError):
            ob.ob_type = "bearish"  # type: ignore[misc]

        with pytest.raises(AttributeError):
            quality.passed = False  # type: ignore[misc]
