"""Shared utility functions for SMC Signal Bot.

Common calculations reused across SMC modules to avoid duplication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from connectors.oanda_client import Candle


def calculate_atr_scalar(candles: list[Candle], period: int) -> float:
    """Calculate a single ATR value (simple average of True Ranges).

    Uses period-1 true ranges: candle[0] is the reference point,
    candles[1..period-1] provide the TR values. This matches
    SwingDetector's usage where a slice of ATR_PERIOD candles is passed.

    Semantic difference vs calculate_atr_series:
    - scalar: period-1 TRs from period candles (candle[0] = reference)
    - series: period TRs from period+1 candles (full Wilder's)
    Both are correct for their respective use cases.

    Args:
        candles: List of Candle objects (minimum `period` candles)
        period: ATR period (default 14)

    Returns:
        Single float ATR value, or 0.0 if insufficient data.
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
    """Calculate ATR series using Wilder's smoothing (one value per candle).

    Uses `period` true ranges for initial ATR (indices 0..period-1 from
    true_ranges where true_ranges[i] = TR between candles[i] and candles[i+1]).
    Subsequent values use Wilder's formula: ATR = (prev_ATR * (period-1) + TR) / period.

    Semantic difference vs calculate_atr_scalar:
    - scalar: period-1 TRs from period candles (candle[0] = reference)
    - series: period TRs from period+1 candles (full Wilder's)
    Both are correct for their respective use cases.

    Args:
        candles: List of Candle objects
        period: ATR period (default 14)

    Returns:
        List of (float | None), same length as candles. None for first `period` entries.
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
