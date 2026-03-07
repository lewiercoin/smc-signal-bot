"""Tests for smc/fvg_detector.py — Fair Value Gap detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from smc.fvg_detector import FairValueGapDetector


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
) -> _Candle:
    return _Candle(
        instrument="EUR_USD",
        timestamp=_BASE + timedelta(hours=i),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


def _flat_candles(n: int, price: float = 1.1000, spread: float = 0.0010) -> list[_Candle]:
    """Generate n flat candles with consistent OHLC around `price`."""
    return [
        _c(i, price, price + spread, price - spread, price)
        for i in range(n)
    ]


def _bullish_fvg_candles() -> list[_Candle]:
    """14 warm-up candles + 3-candle bullish FVG formation at end.

    candle1.high = 1.1010, candle3.low = 1.1030 → gap = 1.1010..1.1030
    """
    warmup = _flat_candles(14, price=1.1000, spread=0.0010)
    candle1 = _c(14, 1.1000, 1.1010, 1.0990, 1.1005)
    candle2 = _c(15, 1.1010, 1.1060, 1.1005, 1.1055)
    candle3 = _c(16, 1.1055, 1.1065, 1.1030, 1.1060)
    return warmup + [candle1, candle2, candle3]


def _bearish_fvg_candles() -> list[_Candle]:
    """14 warm-up candles + 3-candle bearish FVG formation at end.

    candle1.low = 1.0990, candle3.high = 1.0970 → gap = 1.0970..1.0990
    """
    warmup = _flat_candles(14, price=1.1000, spread=0.0010)
    candle1 = _c(14, 1.1000, 1.1010, 1.0990, 1.0995)
    candle2 = _c(15, 1.0995, 1.0998, 1.0940, 1.0945)
    candle3 = _c(16, 1.0945, 1.0970, 1.0935, 1.0940)
    return warmup + [candle1, candle2, candle3]


class TestFVGDetection:
    def test_detects_bullish_fvg(self) -> None:
        """candle3.low > candle1.high → bullish FVG with correct gap boundaries."""
        detector = FairValueGapDetector()
        candles = _bullish_fvg_candles()
        fvgs = detector.detect(candles)

        bullish = [f for f in fvgs if f.fvg_type == "bullish"]
        assert len(bullish) >= 1

        fvg = bullish[0]
        assert fvg.fvg_type == "bullish"
        assert abs(fvg.gap_low - 1.1010) < 1e-9
        assert abs(fvg.gap_high - 1.1030) < 1e-9
        assert fvg.gap_high > fvg.gap_low

    def test_detects_bearish_fvg(self) -> None:
        """candle3.high < candle1.low → bearish FVG with correct gap boundaries."""
        detector = FairValueGapDetector()
        candles = _bearish_fvg_candles()
        fvgs = detector.detect(candles)

        bearish = [f for f in fvgs if f.fvg_type == "bearish"]
        assert len(bearish) >= 1

        fvg = bearish[0]
        assert fvg.fvg_type == "bearish"
        assert abs(fvg.gap_low - 1.0970) < 1e-9
        assert abs(fvg.gap_high - 1.0990) < 1e-9
        assert fvg.gap_high > fvg.gap_low

    def test_no_fvg_when_no_gap(self) -> None:
        """candle3.low <= candle1.high → no FVG detected."""
        detector = FairValueGapDetector()
        # All flat candles — no gap possible
        candles = _flat_candles(20)
        fvgs = detector.detect(candles)
        assert fvgs == []

    def test_insufficient_candles_returns_empty(self) -> None:
        """len(candles) < 3 → empty list, no exception."""
        detector = FairValueGapDetector()
        assert detector.detect([]) == []
        assert detector.detect(_flat_candles(1)) == []
        assert detector.detect(_flat_candles(2)) == []

    def test_multiple_fvgs_sorted_newest_first(self) -> None:
        """Multiple FVGs in a series → sorted by index descending."""
        detector = FairValueGapDetector()

        # Build two separate bullish FVG sequences
        warmup = _flat_candles(14, price=1.1000, spread=0.0010)

        # First FVG at index 15 (middle candle)
        fvg1 = [
            _c(14, 1.1000, 1.1010, 1.0990, 1.1005),
            _c(15, 1.1010, 1.1060, 1.1005, 1.1055),
            _c(16, 1.1055, 1.1065, 1.1030, 1.1060),
        ]
        # Flat bridge
        bridge = [_c(17 + i, 1.1060, 1.1070, 1.1050, 1.1060) for i in range(3)]
        # Second FVG at index 21 (middle candle)
        fvg2 = [
            _c(20, 1.1060, 1.1070, 1.1055, 1.1065),
            _c(21, 1.1065, 1.1120, 1.1060, 1.1115),
            _c(22, 1.1115, 1.1125, 1.1090, 1.1120),
        ]
        candles = warmup + fvg1 + bridge + fvg2

        fvgs = detector.detect(candles)
        bullish = [f for f in fvgs if f.fvg_type == "bullish"]
        assert len(bullish) >= 2

        indices = [f.index for f in bullish]
        assert indices == sorted(indices, reverse=True)


class TestFVGQuality:
    def test_fvg_quality_passes(self) -> None:
        """age < 30, fill < 0.5, size > 0.2 ATR → passed=True."""
        detector = FairValueGapDetector()
        # FVG formed just before last candle (age=0), no post-formation candles → fill=0
        candles = _bullish_fvg_candles()
        fvgs = detector.detect(candles)

        bullish = [f for f in fvgs if f.fvg_type == "bullish"]
        assert bullish
        fvg = bullish[0]

        assert fvg.quality is not None
        assert fvg.quality.fvg_age_bars < 30
        assert fvg.quality.fill_percentage < 0.5
        assert fvg.quality.fvg_size_atr > 0.0
        assert fvg.quality.passed is True

    def test_fvg_quality_fails_too_old(self) -> None:
        """age >= 30 → passed=False, is_valid=False."""
        detector = FairValueGapDetector()

        # Build FVG followed by 35 flat candles so age > 30
        warmup = _flat_candles(14, price=1.1000, spread=0.0010)
        candle1 = _c(14, 1.1000, 1.1010, 1.0990, 1.1005)
        candle2 = _c(15, 1.1010, 1.1060, 1.1005, 1.1055)
        candle3 = _c(16, 1.1055, 1.1065, 1.1030, 1.1060)
        # 35 candles after — price stays above FVG zone (no fill)
        tail = [_c(17 + i, 1.1060, 1.1070, 1.1050, 1.1060) for i in range(35)]
        candles = warmup + [candle1, candle2, candle3] + tail

        fvgs = detector.detect(candles)
        bullish = [f for f in fvgs if f.fvg_type == "bullish"]
        assert bullish

        old_fvg = min(bullish, key=lambda f: f.index)
        assert old_fvg.quality is not None
        assert old_fvg.quality.fvg_age_bars >= 30
        assert old_fvg.quality.passed is False
        assert old_fvg.is_valid is False

    def test_fvg_fully_filled_invalid(self) -> None:
        """fill_percentage >= 1.0 → is_valid=False."""
        detector = FairValueGapDetector()

        # Bullish FVG: gap_low=1.1010, gap_high=1.1030
        warmup = _flat_candles(14, price=1.1000, spread=0.0010)
        candle1 = _c(14, 1.1000, 1.1010, 1.0990, 1.1005)
        candle2 = _c(15, 1.1010, 1.1060, 1.1005, 1.1055)
        candle3 = _c(16, 1.1055, 1.1065, 1.1030, 1.1060)
        # Post-formation candle: low = 1.1000 < gap_low=1.1010 → fully fills zone
        fill_candle = _c(17, 1.1060, 1.1065, 1.1000, 1.1005)
        candles = warmup + [candle1, candle2, candle3, fill_candle]

        fvgs = detector.detect(candles)
        bullish = [f for f in fvgs if f.fvg_type == "bullish"]
        assert bullish

        fvg = bullish[0]
        assert fvg.quality is not None
        assert fvg.quality.fill_percentage >= 1.0
        assert fvg.is_valid is False

    def test_fvg_partially_filled_still_valid(self) -> None:
        """fill_percentage ~0.3 → is_valid=True (ICT: cena wraca do FVG)."""
        detector = FairValueGapDetector()

        # Bullish FVG: gap_low=1.1010, gap_high=1.1030 → gap_size=0.0020
        # Partial fill: low enters at 1.1024 → fill=(1.1030-1.1024)/0.0020=0.30
        warmup = _flat_candles(14, price=1.1000, spread=0.0010)
        candle1 = _c(14, 1.1000, 1.1010, 1.0990, 1.1005)
        candle2 = _c(15, 1.1010, 1.1060, 1.1005, 1.1055)
        candle3 = _c(16, 1.1055, 1.1065, 1.1030, 1.1060)
        partial_fill = _c(17, 1.1060, 1.1065, 1.1024, 1.1050)
        candles = warmup + [candle1, candle2, candle3, partial_fill]

        fvgs = detector.detect(candles)
        bullish = [f for f in fvgs if f.fvg_type == "bullish"]
        assert bullish

        fvg = bullish[0]
        assert fvg.quality is not None
        assert 0.0 < fvg.quality.fill_percentage < 1.0
        assert fvg.is_valid is True

    def test_fill_percentage_calculation(self) -> None:
        """Known candle values → verify fill percentage precisely."""
        detector = FairValueGapDetector()

        # Bullish FVG: gap_low=1.1010, gap_high=1.1030 → gap_size=0.0020
        # Post candle: low=1.1020 → enters zone (1.1020 < 1.1030)
        # fill = (1.1030 - 1.1020) / 0.0020 = 0.0010 / 0.0020 = 0.50
        warmup = _flat_candles(14, price=1.1000, spread=0.0010)
        candle1 = _c(14, 1.1000, 1.1010, 1.0990, 1.1005)
        candle2 = _c(15, 1.1010, 1.1060, 1.1005, 1.1055)
        candle3 = _c(16, 1.1055, 1.1065, 1.1030, 1.1060)
        fill_50pct = _c(17, 1.1060, 1.1065, 1.1020, 1.1050)
        candles = warmup + [candle1, candle2, candle3, fill_50pct]

        fvgs = detector.detect(candles)
        bullish = [f for f in fvgs if f.fvg_type == "bullish"]
        assert bullish

        fvg = bullish[0]
        assert fvg.quality is not None
        assert abs(fvg.quality.fill_percentage - 0.50) < 1e-9
