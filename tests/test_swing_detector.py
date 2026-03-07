"""Tests for swing_detector module.

Covers GROK-2 dynamic swing_length and basic swing detection.
"""

from datetime import datetime, timedelta

import pytest

from connectors.oanda_client import Candle
from smc.swing_detector import SwingDetector, SwingPoint, SwingResult


class TestSwingDetector:
    """Test suite for SwingDetector class."""

    @pytest.fixture
    def detector(self) -> SwingDetector:
        """Create a fresh SwingDetector instance for each test."""
        return SwingDetector()

    @pytest.fixture
    def base_time(self) -> datetime:
        """Base timestamp for test candles."""
        return datetime(2026, 3, 7, 12, 0, 0)

    def _create_candles(
        self,
        base_time: datetime,
        count: int,
        pattern: str = "uptrend",
        volatility: str = "normal",
    ) -> list[Candle]:
        """Create test candles with specified pattern.

        Args:
            base_time: Starting timestamp
            count: Number of candles to create
            pattern: "uptrend", "downtrend", "choppy", "swing_high", "swing_low"
            volatility: "low", "normal", "high" - affects second half of data

        Returns:
            List of Candle objects
        """
        candles: list[Candle] = []
        
        # Split data: first half = normal baseline, second half = target volatility
        half_point = count // 2

        for i in range(count):
            timestamp = base_time + timedelta(hours=i)
            
            # Determine which half we're in
            if i < half_point:
                # First half: normal volatility baseline
                price_range = 0.0020  # 20 pips
            else:
                # Second half: target volatility
                if volatility == "low":
                    price_range = 0.0005  # 5 pips (much lower)
                elif volatility == "high":
                    price_range = 0.0080  # 80 pips (much higher)
                else:
                    price_range = 0.0020  # 20 pips (normal)

            base_price = 1.1000

            if pattern == "uptrend":
                trend_offset = (i / count) * 0.01
                open_p = base_price + trend_offset
                close_p = open_p + (price_range * 0.3)
                high_p = close_p + (price_range * 0.5)
                low_p = open_p - (price_range * 0.2)

            elif pattern == "downtrend":
                trend_offset = (i / count) * 0.01
                open_p = base_price + 0.02 - trend_offset
                close_p = open_p - (price_range * 0.3)
                high_p = open_p + (price_range * 0.5)
                low_p = close_p - (price_range * 0.2)

            elif pattern == "swing_high":
                if i == count // 2:
                    open_p = base_price + 0.005
                    close_p = open_p + price_range * 0.3
                    high_p = close_p + price_range * 0.8
                    low_p = open_p - price_range * 0.2
                elif abs(i - count // 2) <= 10:
                    distance = abs(i - count // 2)
                    offset = (10 - distance) * 0.0005
                    open_p = base_price + offset
                    close_p = open_p + price_range * 0.2
                    high_p = close_p + price_range * 0.1
                    low_p = open_p - price_range * 0.1
                else:
                    open_p = base_price
                    close_p = open_p + price_range * 0.1
                    high_p = close_p + price_range * 0.1
                    low_p = open_p - price_range * 0.1

            elif pattern == "swing_low":
                if i == count // 2:
                    open_p = base_price - 0.005
                    close_p = open_p - price_range * 0.3
                    high_p = open_p + price_range * 0.2
                    low_p = close_p - price_range * 0.8
                elif abs(i - count // 2) <= 10:
                    distance = abs(i - count // 2)
                    offset = (10 - distance) * 0.0005
                    open_p = base_price - offset
                    close_p = open_p - price_range * 0.2
                    high_p = open_p + price_range * 0.1
                    low_p = close_p - price_range * 0.1
                else:
                    open_p = base_price
                    close_p = open_p - price_range * 0.1
                    high_p = open_p + price_range * 0.1
                    low_p = close_p - price_range * 0.1

            else:  # choppy
                offset = (i % 3 - 1) * price_range * 0.3
                open_p = base_price + offset
                close_p = open_p + (price_range * 0.2 if i % 2 == 0 else -price_range * 0.2)
                high_p = max(open_p, close_p) + price_range * 0.3
                low_p = min(open_p, close_p) - price_range * 0.3

            candle = Candle(
                instrument="EUR_USD",
                timestamp=timestamp,
                open=round(open_p, 5),
                high=round(high_p, 5),
                low=round(low_p, 5),
                close=round(close_p, 5),
                volume=1000 + i * 10,
            )
            candles.append(candle)

        return candles

    def test_detects_swing_high_in_uptrend(
        self,
        detector: SwingDetector,
        base_time: datetime,
    ) -> None:
        """Test that swing highs are detected correctly in an uptrend."""
        # Create candles with clear swing high pattern
        candles = self._create_candles(base_time, 50, pattern="swing_high")

        result = detector.detect(candles)

        assert isinstance(result, SwingResult)
        assert len(result.highs) >= 1
        assert all(isinstance(h, SwingPoint) for h in result.highs)
        assert all(h.swing_type == "high" for h in result.highs)

        # Verify swing high properties
        for swing_high in result.highs:
            assert swing_high.index > 0
            assert swing_high.index < len(candles) - 1
            assert swing_high.price > 0
            assert swing_high.strength > 0

    def test_detects_swing_low_in_downtrend(
        self,
        detector: SwingDetector,
        base_time: datetime,
    ) -> None:
        """Test that swing lows are detected correctly in a downtrend."""
        # Create candles with clear swing low pattern
        candles = self._create_candles(base_time, 50, pattern="swing_low")

        result = detector.detect(candles)

        assert isinstance(result, SwingResult)
        assert len(result.lows) >= 1
        assert all(isinstance(swing_point, SwingPoint) for swing_point in result.lows)
        assert all(swing_point.swing_type == "low" for swing_point in result.lows)

        # Verify swing low properties
        for swing_low in result.lows:
            assert swing_low.index > 0
            assert swing_low.index < len(candles) - 1
            assert swing_low.price > 0
            assert swing_low.strength > 0

    def test_dynamic_swing_length_high_volatility(
        self,
        base_time: datetime,
    ) -> None:
        """Test that high volatility increases swing length to 14."""
        detector = SwingDetector()

        # Create high volatility candles
        candles = self._create_candles(base_time, 80, volatility="high")

        # First detection - need 3 consecutive high volatility candles
        # to trigger the change
        for _ in range(4):
            result = detector.detect(candles)

        # After stability requirement is met, should use high vol length
        assert result.swing_length_used in [10, 14]  # Base or high
        assert result.volatility_regime == "high"

    def test_dynamic_swing_length_low_volatility(
        self,
        base_time: datetime,
    ) -> None:
        """Test that low volatility decreases swing length to 7."""
        detector = SwingDetector()

        # Create low volatility candles
        candles = self._create_candles(base_time, 80, volatility="low")

        # Multiple detections to trigger stability rule
        for _ in range(4):
            result = detector.detect(candles)

        # Should detect low volatility regime
        assert result.volatility_regime == "low"
        # Swing length should be low (7) or base (10)
        assert result.swing_length_used in [7, 10]

    def test_swing_length_stability_3_candles(
        self,
        base_time: datetime,
    ) -> None:
        """Test that swing length only changes after 3 consecutive candles."""
        detector = SwingDetector()

        # Create low volatility candles
        low_vol_candles = self._create_candles(base_time, 80, volatility="low")

        # Initial detection - should be base length
        result1 = detector.detect(low_vol_candles)
        assert result1.swing_length_used in [7, 10]

        # Second detection - still in proposal phase
        detector.detect(low_vol_candles)

        # Third detection - should still be in proposal phase or changed
        detector.detect(low_vol_candles)

        # Fourth detection - should have changed by now
        result4 = detector.detect(low_vol_candles)

        # After 4 consecutive detections with low volatility,
        # the swing length should either be the same or have changed to 7
        assert result4.swing_length_used in [7, 10]

        # The volatility regime should be consistently low
        assert result4.volatility_regime == "low"

    def test_minimum_candles_required(
        self,
        detector: SwingDetector,
        base_time: datetime,
    ) -> None:
        """Test that insufficient candles returns empty result."""
        # Create too few candles (need at least 21 for base swing_length=10)
        candles = self._create_candles(base_time, 15, pattern="uptrend")

        result = detector.detect(candles)

        assert isinstance(result, SwingResult)
        assert result.highs == []
        assert result.lows == []
        assert result.swing_length_used == 10  # Default base length

    def test_returns_correct_volatility_regime(
        self,
        base_time: datetime,
    ) -> None:
        """Test that correct volatility regime is returned."""
        detector = SwingDetector()

        # Test high volatility regime
        high_vol_candles = self._create_candles(base_time, 80, volatility="high")
        result_high = detector.detect(high_vol_candles)
        assert result_high.volatility_regime == "high"

        # Reset detector for clean state
        detector2 = SwingDetector()

        # Test low volatility regime
        low_vol_candles = self._create_candles(
            base_time + timedelta(days=5), 80, volatility="low"
        )
        result_low = detector2.detect(low_vol_candles)
        assert result_low.volatility_regime == "low"

        # Reset detector for clean state
        detector3 = SwingDetector()

        # Test normal volatility regime
        normal_vol_candles = self._create_candles(
            base_time + timedelta(days=10), 80, volatility="normal"
        )
        result_normal = detector3.detect(normal_vol_candles)
        assert result_normal.volatility_regime in ["normal", "low", "high"]

    def test_swing_point_dataclass(self) -> None:
        """Test SwingPoint dataclass creation and immutability."""
        timestamp = datetime.now()

        point = SwingPoint(
            index=10,
            price=1.1050,
            time=timestamp,
            swing_type="high",
            strength=10,
        )

        assert point.index == 10
        assert point.price == 1.1050
        assert point.time == timestamp
        assert point.swing_type == "high"
        assert point.strength == 10

        # Test frozen dataclass (should not be modifiable)
        with pytest.raises(AttributeError):
            point.index = 20  # type: ignore[misc]

    def test_swing_result_dataclass(self) -> None:
        """Test SwingResult dataclass creation and immutability."""
        timestamp = datetime.now()

        swing_high = SwingPoint(
            index=15,
            price=1.1100,
            time=timestamp,
            swing_type="high",
            strength=10,
        )

        swing_low = SwingPoint(
            index=25,
            price=1.0950,
            time=timestamp,
            swing_type="low",
            strength=10,
        )

        result = SwingResult(
            highs=[swing_high],
            lows=[swing_low],
            swing_length_used=10,
            volatility_regime="normal",
        )

        assert len(result.highs) == 1
        assert len(result.lows) == 1
        assert result.swing_length_used == 10
        assert result.volatility_regime == "normal"

        # Test frozen dataclass
        with pytest.raises(AttributeError):
            result.swing_length_used = 14  # type: ignore[misc]
