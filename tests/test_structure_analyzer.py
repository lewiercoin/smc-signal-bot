"""Tests for structure_analyzer module.

Covers trend detection, BoS, CHoCH, and HTF bias logic.
"""

from datetime import datetime, timedelta

import pytest

from smc.structure_analyzer import BOS, CHoCH, StructureAnalyzer, StructureResult
from smc.swing_detector import SwingPoint


class TestStructureAnalyzer:
    """Test suite for StructureAnalyzer class."""

    @pytest.fixture
    def analyzer(self) -> StructureAnalyzer:
        """Create a fresh StructureAnalyzer instance."""
        return StructureAnalyzer()

    @pytest.fixture
    def base_time(self) -> datetime:
        """Base timestamp for test data."""
        return datetime(2026, 3, 7, 12, 0, 0)

    def _create_swing_point(
        self,
        index: int,
        price: float,
        timestamp: datetime,
        swing_type: str,
    ) -> SwingPoint:
        """Helper to create a SwingPoint."""
        return SwingPoint(
            index=index,
            price=price,
            time=timestamp,
            swing_type=swing_type,
            strength=10,
        )

    def test_bullish_trend_hh_hl(self, analyzer: StructureAnalyzer, base_time: datetime) -> None:
        """Test bullish trend detection with Higher Highs and Higher Lows."""
        # Create HH + HL pattern
        swings = [
            self._create_swing_point(10, 1.0900, base_time, "low"),
            self._create_swing_point(15, 1.1000, base_time + timedelta(hours=5), "high"),
            self._create_swing_point(20, 1.0950, base_time + timedelta(hours=10), "low"),
            self._create_swing_point(25, 1.1100, base_time + timedelta(hours=15), "high"),
            self._create_swing_point(30, 1.1050, base_time + timedelta(hours=20), "low"),
            self._create_swing_point(35, 1.1200, base_time + timedelta(hours=25), "high"),
        ]

        # Separate highs and lows
        highs = [s for s in swings if s.swing_type == "high"]
        lows = [s for s in swings if s.swing_type == "low"]

        from smc.swing_detector import SwingResult

        swing_result = SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=10,
            volatility_regime="normal",
        )

        result = analyzer.analyze(swing_result)

        assert isinstance(result, StructureResult)
        assert result.trend == "bullish"

    def test_bearish_trend_lh_ll(self, analyzer: StructureAnalyzer, base_time: datetime) -> None:
        """Test bearish trend detection with Lower Highs and Lower Lows."""
        # Create LH + LL pattern
        swings = [
            self._create_swing_point(10, 1.1200, base_time, "high"),
            self._create_swing_point(15, 1.1100, base_time + timedelta(hours=5), "low"),
            self._create_swing_point(20, 1.1150, base_time + timedelta(hours=10), "high"),
            self._create_swing_point(25, 1.1050, base_time + timedelta(hours=15), "low"),
            self._create_swing_point(30, 1.1100, base_time + timedelta(hours=20), "high"),
            self._create_swing_point(35, 1.1000, base_time + timedelta(hours=25), "low"),
        ]

        highs = [s for s in swings if s.swing_type == "high"]
        lows = [s for s in swings if s.swing_type == "low"]

        from smc.swing_detector import SwingResult

        swing_result = SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=10,
            volatility_regime="normal",
        )

        result = analyzer.analyze(swing_result)

        assert result.trend == "bearish"

    def test_ranging_market(self, analyzer: StructureAnalyzer, base_time: datetime) -> None:
        """Test ranging market detection with mixed signals."""
        # Create ranging pattern: dominujące LH + HL (sprzeczne sygnały)
        # Highs: 1.1020 → 1.1010 (LH) → 1.1005 (LH)  [0 HH, 2 LH]
        # Lows:  1.0990 → 1.0995 (HL) → 1.1000 (HL)  [2 HL, 0 LL]
        # Bullish: HH>=LH (0>=2)=False -> nie bullish
        # Bearish: LH>=HH (2>=0)=True, ale LL>=HL (0>=2)=False -> nie bearish
        # Więc: ranging
        swings = [
            self._create_swing_point(10, 1.1020, base_time, "high"),
            self._create_swing_point(15, 1.0990, base_time + timedelta(hours=5), "low"),
            self._create_swing_point(20, 1.1010, base_time + timedelta(hours=10), "high"),  # LH
            self._create_swing_point(25, 1.0995, base_time + timedelta(hours=15), "low"),   # HL
            self._create_swing_point(30, 1.1005, base_time + timedelta(hours=20), "high"),  # LH
            self._create_swing_point(35, 1.1000, base_time + timedelta(hours=25), "low"),   # HL
        ]

        highs = [s for s in swings if s.swing_type == "high"]
        lows = [s for s in swings if s.swing_type == "low"]

        from smc.swing_detector import SwingResult

        swing_result = SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=10,
            volatility_regime="normal",
        )

        result = analyzer.analyze(swing_result)

        assert result.trend == "ranging"

    def test_detects_bullish_bos(self, analyzer: StructureAnalyzer, base_time: datetime) -> None:
        """Test detection of bullish Break of Structure."""
        # Bullish BoS: new high breaks above previous high
        swings = [
            self._create_swing_point(10, 1.0900, base_time, "low"),
            self._create_swing_point(15, 1.1000, base_time + timedelta(hours=5), "high"),
            self._create_swing_point(20, 1.0950, base_time + timedelta(hours=10), "low"),
            self._create_swing_point(25, 1.1100, base_time + timedelta(hours=15), "high"),  # Higher high
        ]

        highs = [s for s in swings if s.swing_type == "high"]
        lows = [s for s in swings if s.swing_type == "low"]

        from smc.swing_detector import SwingResult

        swing_result = SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=10,
            volatility_regime="normal",
        )

        result = analyzer.analyze(swing_result)

        assert result.last_bos is not None
        assert result.last_bos.direction == "bullish"
        assert result.last_bos.price == 1.1100

    def test_detects_bearish_bos(self, analyzer: StructureAnalyzer, base_time: datetime) -> None:
        """Test detection of bearish Break of Structure."""
        # Bearish BoS: new low breaks below previous low
        swings = [
            self._create_swing_point(10, 1.1200, base_time, "high"),
            self._create_swing_point(15, 1.1100, base_time + timedelta(hours=5), "low"),
            self._create_swing_point(20, 1.1150, base_time + timedelta(hours=10), "high"),
            self._create_swing_point(25, 1.1050, base_time + timedelta(hours=15), "low"),  # Lower low
        ]

        highs = [s for s in swings if s.swing_type == "high"]
        lows = [s for s in swings if s.swing_type == "low"]

        from smc.swing_detector import SwingResult

        swing_result = SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=10,
            volatility_regime="normal",
        )

        result = analyzer.analyze(swing_result)

        assert result.last_bos is not None
        assert result.last_bos.direction == "bearish"
        assert result.last_bos.price == 1.1050

    def test_detects_choch_bullish(self, analyzer: StructureAnalyzer, base_time: datetime) -> None:
        """Test detection of bullish Change of Character."""
        # Bullish CHoCH: Higher low in bearish trend (trend reversal)
        # recent_highs[-3:] = [LH, LH, LH] → LH=2, HH=0
        # recent_lows[-3:] = [LL, LL, HL] → LL=2 > HL=1? No, need LL only before CHoCH
        # Use 4 lows so recent_lows[-3:] = [LL, LL, HL(CHoCH)] → LL=2 first two = bearish trend
        # Actually: recent_lows[-3:] includes CHoCH candidate, LL=1 HL=1 → ranging
        # Solution: have 5 lows, so the 3 BEFORE last = all LL
        swings = [
            self._create_swing_point(5, 1.1300, base_time, "high"),
            self._create_swing_point(10, 1.1200, base_time + timedelta(hours=5), "low"),
            self._create_swing_point(15, 1.1250, base_time + timedelta(hours=10), "high"),  # LH
            self._create_swing_point(20, 1.1150, base_time + timedelta(hours=15), "low"),  # LL
            self._create_swing_point(25, 1.1220, base_time + timedelta(hours=20), "high"),  # LH
            self._create_swing_point(30, 1.1100, base_time + timedelta(hours=25), "low"),  # LL
            self._create_swing_point(35, 1.1180, base_time + timedelta(hours=30), "high"),  # LH
            self._create_swing_point(40, 1.1050, base_time + timedelta(hours=35), "low"),  # LL
            self._create_swing_point(45, 1.1140, base_time + timedelta(hours=40), "high"),  # LH
            self._create_swing_point(50, 1.1090, base_time + timedelta(hours=45), "low"),  # Higher Low = CHoCH!
        ]

        highs = [s for s in swings if s.swing_type == "high"]
        lows = [s for s in swings if s.swing_type == "low"]

        from smc.swing_detector import SwingResult

        swing_result = SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=10,
            volatility_regime="normal",
        )

        result = analyzer.analyze(swing_result)

        assert result.trend == "bearish"
        assert result.last_choch is not None
        assert result.last_choch.from_trend == "bearish"
        assert result.last_choch.to_trend == "bullish"
        assert result.last_choch.price == 1.1090

    def test_detects_choch_bearish(self, analyzer: StructureAnalyzer, base_time: datetime) -> None:
        """Test detection of bearish Change of Character."""
        # Bearish CHoCH: Lower high in bullish trend (trend reversal)
        # Mirror of bullish CHoCH: 4 HH before CHoCH, 3 HL before CHoCH
        swings = [
            self._create_swing_point(5, 1.0850, base_time, "low"),
            self._create_swing_point(10, 1.0950, base_time + timedelta(hours=5), "high"),
            self._create_swing_point(15, 1.0880, base_time + timedelta(hours=10), "low"),  # HL
            self._create_swing_point(20, 1.1050, base_time + timedelta(hours=15), "high"),  # HH
            self._create_swing_point(25, 1.0950, base_time + timedelta(hours=20), "low"),  # HL
            self._create_swing_point(30, 1.1150, base_time + timedelta(hours=25), "high"),  # HH
            self._create_swing_point(35, 1.1050, base_time + timedelta(hours=30), "low"),  # HL
            self._create_swing_point(40, 1.1250, base_time + timedelta(hours=35), "high"),  # HH
            self._create_swing_point(45, 1.1150, base_time + timedelta(hours=40), "low"),  # HL
            self._create_swing_point(50, 1.1200, base_time + timedelta(hours=45), "high"),  # Lower High = CHoCH!
        ]

        highs = [s for s in swings if s.swing_type == "high"]
        lows = [s for s in swings if s.swing_type == "low"]

        from smc.swing_detector import SwingResult

        swing_result = SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=10,
            volatility_regime="normal",
        )

        result = analyzer.analyze(swing_result)

        assert result.trend == "bullish"
        assert result.last_choch is not None
        assert result.last_choch.from_trend == "bullish"
        assert result.last_choch.to_trend == "bearish"
        assert result.last_choch.price == 1.1200

    def test_htf_bias_follows_choch(self, analyzer: StructureAnalyzer, base_time: datetime) -> None:
        """Test that HTF bias follows CHoCH direction."""
        # CHoCH bullish → bias = "long" — identyczne dane co test_detects_choch_bullish
        swings = [
            self._create_swing_point(5, 1.1300, base_time, "high"),
            self._create_swing_point(10, 1.1200, base_time + timedelta(hours=5), "low"),
            self._create_swing_point(15, 1.1250, base_time + timedelta(hours=10), "high"),  # LH
            self._create_swing_point(20, 1.1150, base_time + timedelta(hours=15), "low"),  # LL
            self._create_swing_point(25, 1.1220, base_time + timedelta(hours=20), "high"),  # LH
            self._create_swing_point(30, 1.1100, base_time + timedelta(hours=25), "low"),  # LL
            self._create_swing_point(35, 1.1180, base_time + timedelta(hours=30), "high"),  # LH
            self._create_swing_point(40, 1.1050, base_time + timedelta(hours=35), "low"),  # LL
            self._create_swing_point(45, 1.1140, base_time + timedelta(hours=40), "high"),  # LH
            self._create_swing_point(50, 1.1090, base_time + timedelta(hours=45), "low"),  # Higher Low = CHoCH
        ]

        highs = [s for s in swings if s.swing_type == "high"]
        lows = [s for s in swings if s.swing_type == "low"]

        from smc.swing_detector import SwingResult

        swing_result = SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=10,
            volatility_regime="normal",
        )

        result = analyzer.analyze(swing_result)

        assert result.last_choch is not None
        assert result.last_choch.to_trend == "bullish"
        assert result.htf_bias == "long"

    def test_htf_bias_follows_trend_no_choch(self, analyzer: StructureAnalyzer, base_time: datetime) -> None:
        """Test that HTF bias follows trend when no CHoCH."""
        # No CHoCH - clear bullish trend (HH=2 > LH=0, HL=2 > LL=0) should give "long" bias
        swings = [
            self._create_swing_point(5,  1.0900, base_time, "low"),
            self._create_swing_point(10, 1.1000, base_time + timedelta(hours=5), "high"),
            self._create_swing_point(15, 1.0950, base_time + timedelta(hours=10), "low"),   # HL
            self._create_swing_point(20, 1.1100, base_time + timedelta(hours=15), "high"),  # HH
            self._create_swing_point(25, 1.1050, base_time + timedelta(hours=20), "low"),   # HL
            self._create_swing_point(30, 1.1200, base_time + timedelta(hours=25), "high"),  # HH
        ]

        highs = [s for s in swings if s.swing_type == "high"]
        lows = [s for s in swings if s.swing_type == "low"]

        from smc.swing_detector import SwingResult

        swing_result = SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=10,
            volatility_regime="normal",
        )

        result = analyzer.analyze(swing_result)

        assert result.last_choch is None
        assert result.trend == "bullish"
        assert result.htf_bias == "long"

    def test_structure_result_dataclass(self) -> None:
        """Test StructureResult dataclass immutability."""
        base_time = datetime(2026, 3, 7, 12, 0, 0)

        swing_point = SwingPoint(
            index=10,
            price=1.1000,
            time=base_time,
            swing_type="high",
            strength=10,
        )

        bos = BOS(
            price=1.1100,
            time=base_time,
            direction="bullish",
            swing_point=swing_point,
        )

        choch = CHoCH(
            price=1.0900,
            time=base_time,
            from_trend="bearish",
            to_trend="bullish",
            swing_point=swing_point,
        )

        result = StructureResult(
            trend="bullish",
            last_bos=bos,
            last_choch=choch,
            htf_bias="long",
            key_highs=[swing_point],
            key_lows=[],
        )

        assert result.trend == "bullish"
        assert result.htf_bias == "long"
        assert result.last_bos is not None
        assert result.last_choch is not None

        # Test frozen dataclass
        with pytest.raises(AttributeError):
            result.trend = "bearish"  # type: ignore[misc]
