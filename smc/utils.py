"""Shared utility functions for SMC Signal Bot.

Common calculations reused across SMC modules to avoid duplication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from connectors.oanda_client import Candle


def calculate_atr_scalar(candles: list[Candle], period: int) -> float:
    """Calculate Average True Range as a single scalar value.

    Simple mean of True Ranges over the given period. Used by SwingDetector
    for volatility regime comparisons.

    Args:
        candles: List of OHLCV candles (must have at least 2)
        period: Number of candles to use (will be clamped to len(candles))

    Returns:
        ATR value as float. Returns 0.0 if insufficient candles.
    """
    if len(candles) < 2:
        return 0.0

    if len(candles) < period:
        period = len(candles)

    true_ranges: list[float] = []
    for i in range(1, period):
        current = candles[i]
        previous = candles[i - 1]
        tr = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        true_ranges.append(tr)

    if not true_ranges:
        return 0.0

    return sum(true_ranges) / len(true_ranges)


def calculate_atr_series(
    candles: list[Candle], period: int = 14
) -> list[float | None]:
    """Calculate ATR(period) for each candle using Wilder's smoothing.

    Returns a per-candle ATR series aligned with the candles list.
    The first `period` values are None (insufficient data to compute ATR).
    Used by OrderBlockDetector for impulse threshold and OB quality metrics.

    Args:
        candles: List of OHLCV candles in chronological order
        period: ATR period (default 14)

    Returns:
        List of ATR values (None where insufficient data). Same length as candles.
    """
    n = len(candles)
    atr_values: list[float | None] = [None] * n

    true_ranges: list[float] = []
    for i in range(1, n):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return atr_values

    initial_atr = sum(true_ranges[:period]) / period
    atr_values[period] = initial_atr

    current_atr = initial_atr
    for i in range(period + 1, n):
        tr_idx = i - 1
        current_atr = (current_atr * (period - 1) + true_ranges[tr_idx]) / period
        atr_values[i] = current_atr

    return atr_values
