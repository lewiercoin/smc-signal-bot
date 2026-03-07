"""Tests for smc/utils.py — shared ATR calculation utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from smc.utils import calculate_atr_scalar, calculate_atr_series


@dataclass(frozen=True)
class _Candle:
    instrument: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def _make_candle(
    i: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    base: datetime | None = None,
) -> _Candle:
    base = base or datetime(2026, 1, 1)
    return _Candle(
        instrument="EUR_USD",
        timestamp=base + timedelta(hours=i),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


class TestCalculateAtrScalar:
    def test_atr_basic_calculation(self) -> None:
        """ATR scalar over known candles returns correct mean TR."""
        base = datetime(2026, 1, 1)
        # 5 candles: each has TR = high - low = 0.0020, prev_close gaps are smaller
        candles = [
            _make_candle(0, 1.1000, 1.1010, 1.0990, 1.1000, base),
            _make_candle(1, 1.1000, 1.1010, 1.0990, 1.1000, base),
            _make_candle(2, 1.1000, 1.1010, 1.0990, 1.1000, base),
            _make_candle(3, 1.1000, 1.1010, 1.0990, 1.1000, base),
            _make_candle(4, 1.1000, 1.1010, 1.0990, 1.1000, base),
        ]
        # TR for each: max(0.0020, |1.1010-1.1000|=0.0010, |1.0990-1.1000|=0.0010) = 0.0020
        result = calculate_atr_scalar(candles, period=5)
        assert abs(result - 0.0020) < 1e-9

    def test_atr_scalar_returns_zero_for_single_candle(self) -> None:
        """Single candle → returns 0.0 (no TR possible)."""
        base = datetime(2026, 1, 1)
        candles = [_make_candle(0, 1.1000, 1.1010, 1.0990, 1.1000, base)]
        result = calculate_atr_scalar(candles, period=5)
        assert result == 0.0

    def test_atr_scalar_period_clamped_to_len(self) -> None:
        """Period larger than candles → clamped, no crash."""
        base = datetime(2026, 1, 1)
        candles = [
            _make_candle(0, 1.1000, 1.1020, 1.0980, 1.1000, base),
            _make_candle(1, 1.1000, 1.1020, 1.0980, 1.1000, base),
            _make_candle(2, 1.1000, 1.1020, 1.0980, 1.1000, base),
        ]
        result = calculate_atr_scalar(candles, period=100)
        assert result > 0.0

    def test_atr_scalar_gap_candle_increases_tr(self) -> None:
        """A gap candle (open >> prev_close) increases ATR due to larger TR."""
        base = datetime(2026, 1, 1)
        # First two candles: tight range
        candles_tight = [
            _make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000, base),
            _make_candle(1, 1.1000, 1.1005, 1.0995, 1.1000, base),
        ]
        # Same but second candle gaps up: prev_close=1.1000, high=1.1050 → TR bigger
        candles_gap = [
            _make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000, base),
            _make_candle(1, 1.1040, 1.1050, 1.1038, 1.1045, base),
        ]
        atr_tight = calculate_atr_scalar(candles_tight, period=2)
        atr_gap = calculate_atr_scalar(candles_gap, period=2)
        assert atr_gap > atr_tight


class TestCalculateAtrSeries:
    def test_atr_series_length_matches_candles(self) -> None:
        """Output list has same length as input candles."""
        base = datetime(2026, 1, 1)
        candles = [
            _make_candle(i, 1.1000, 1.1010, 1.0990, 1.1000, base)
            for i in range(20)
        ]
        result = calculate_atr_series(candles, period=14)
        assert len(result) == 20

    def test_atr_series_first_values_are_none(self) -> None:
        """First `period` values are None (insufficient data)."""
        base = datetime(2026, 1, 1)
        candles = [
            _make_candle(i, 1.1000, 1.1010, 1.0990, 1.1000, base)
            for i in range(20)
        ]
        result = calculate_atr_series(candles, period=14)
        # indices 0..13 should be None
        for i in range(14):
            assert result[i] is None, f"Expected None at index {i}, got {result[i]}"

    def test_atr_series_values_after_period_are_floats(self) -> None:
        """Values at index >= period are float (not None)."""
        base = datetime(2026, 1, 1)
        candles = [
            _make_candle(i, 1.1000, 1.1010, 1.0990, 1.1000, base)
            for i in range(20)
        ]
        result = calculate_atr_series(candles, period=14)
        for i in range(14, 20):
            assert result[i] is not None
            assert isinstance(result[i], float)

    def test_atr_series_insufficient_candles_all_none(self) -> None:
        """Fewer candles than period → all values None."""
        base = datetime(2026, 1, 1)
        candles = [
            _make_candle(i, 1.1000, 1.1010, 1.0990, 1.1000, base)
            for i in range(10)
        ]
        result = calculate_atr_series(candles, period=14)
        assert all(v is None for v in result)

    def test_atr_series_wilder_smoothing_decreases_slowly(self) -> None:
        """After initial ATR, Wilder's smoothing produces a gradual series."""
        base = datetime(2026, 1, 1)
        # 20 identical candles then one calm one — ATR should be smooth
        candles = [
            _make_candle(i, 1.1000, 1.1020, 1.0980, 1.1000, base)
            for i in range(20)
        ]
        result = calculate_atr_series(candles, period=14)
        non_none = [v for v in result if v is not None]
        # All values should be equal for identical TRs (Wilder's preserves constant TR)
        assert len(non_none) >= 2
        for v in non_none:
            assert abs(v - non_none[0]) < 1e-6

    def test_atr_series_single_candle_all_none(self) -> None:
        """Single candle → all None (no TR computable)."""
        base = datetime(2026, 1, 1)
        candles = [_make_candle(0, 1.1000, 1.1010, 1.0990, 1.1000, base)]
        result = calculate_atr_series(candles, period=14)
        assert result == [None]


class TestAtrConsistency:
    def test_scalar_and_series_identical_tr_agree(self) -> None:
        """Both functions return the same ATR when all TRs are identical.

        calculate_atr_scalar uses period-1 TRs (range 1..period),
        calculate_atr_series uses period TRs (indices 0..period-1 of true_ranges).
        When TR is constant, mean is the same regardless of count.
        """
        base = datetime(2026, 1, 1)
        period = 5
        # All candles identical → TR always = 0.0020
        candles = [
            _make_candle(i, 1.1000, 1.1010, 1.0990, 1.1000, base)
            for i in range(period + 2)
        ]
        scalar = calculate_atr_scalar(candles[:period], period=period)
        series = calculate_atr_series(candles, period=period)
        first_value = series[period]
        assert first_value is not None
        assert abs(scalar - first_value) < 1e-9

    @pytest.mark.parametrize("n_candles", [0, 1])
    def test_atr_scalar_edge_cases(self, n_candles: int) -> None:
        """Zero or one candle returns 0.0 without raising."""
        base = datetime(2026, 1, 1)
        candles = [
            _make_candle(i, 1.1000, 1.1010, 1.0990, 1.1000, base)
            for i in range(n_candles)
        ]
        result = calculate_atr_scalar(candles, period=14)
        assert result == 0.0
