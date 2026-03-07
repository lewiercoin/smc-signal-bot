"""Liquidity Sweep detector module for SMC Signal Bot.

Implements ICT/SMC liquidity sweep detection: stop hunts beyond swing highs/lows
where price wicks past a level but closes back inside.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from smc.utils import calculate_atr_series

if TYPE_CHECKING:
    from connectors.oanda_client import Candle
    from smc.swing_detector import SwingPoint, SwingResult

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SweepQuality:
    """Quality metrics for a Liquidity Sweep.

    Attributes:
        sweep_age_bars: Number of candles since sweep (counted from end of candles list)
        penetration_atr: Wick penetration depth expressed in ATR(14) units
        rejection_strength: abs(candle.close - level) / ATR — how far close is from level
        level_tested_count: Times the swing level was tested (wick ±0.2 ATR) before sweep
        passed: True if age < 20 AND 0.05 < penetration_atr < 2.0 AND rejection_strength > 0.3
    """

    sweep_age_bars: int
    penetration_atr: float
    rejection_strength: float
    level_tested_count: int
    passed: bool


@dataclass(frozen=True)
class LiquiditySweep:
    """A detected Liquidity Sweep (stop hunt).

    Attributes:
        index: Index of the sweeping candle in the candles list
        time: Timestamp of the sweeping candle
        sweep_type: "buyside" (swept above swing high, bearish signal) or
                    "sellside" (swept below swing low, bullish signal)
        level_price: Price of the swing high/low (liquidity level swept)
        sweep_high: High of the sweeping candle
        sweep_low: Low of the sweeping candle
        penetration: How far the wick went beyond the level
        swing_index: Index of the original SwingPoint in the candles list
        atr_at_sweep: ATR(14) at the time of the sweep
        quality: SweepQuality metrics (None if not yet evaluated)
        is_valid: False if age > 20 bars, next candle confirms breakout, or penetration > 2.0 ATR
    """

    index: int
    time: datetime
    sweep_type: str
    level_price: float
    sweep_high: float
    sweep_low: float
    penetration: float
    swing_index: int
    atr_at_sweep: float
    quality: SweepQuality | None = field(default=None)
    is_valid: bool = field(default=True)


class LiquidityDetector:
    """Detects Liquidity Sweeps using ICT/SMC stop hunt methodology.

    Liquidity sweeps occur when Smart Money drives price beyond swing highs/lows
    to collect stop orders, then reverses. The signature is a wick that breaches
    the level with the candle body closing back inside.

    Buy-side sweep (above swing high): bearish signal — SM sells after collecting buy stops.
    Sell-side sweep (below swing low): bullish signal — SM buys after collecting sell stops.
    """

    ATR_PERIOD: int = 14
    MAX_AGE_BARS: int = 20
    MIN_PENETRATION_ATR: float = 0.05
    MAX_PENETRATION_ATR: float = 2.0
    MIN_REJECTION_STRENGTH: float = 0.3
    LEVEL_TEST_PROXIMITY_ATR: float = 0.2

    def detect(
        self,
        candles: list[Candle],
        swings: SwingResult,
    ) -> list[LiquiditySweep]:
        """Detect all liquidity sweeps in the candle series.

        Args:
            candles: List of OHLCV candles in chronological order
            swings: SwingResult containing swing highs and lows

        Returns:
            List of LiquiditySweep objects sorted newest first (highest index first).
            Returns empty list if no swing points exist.
        """
        if not swings.highs and not swings.lows:
            logger.warning(
                "liquidity_detector.no_swings",
                candle_count=len(candles),
            )
            return []

        buyside_sweeps = self._find_buyside_sweeps(candles, swings.highs)
        sellside_sweeps = self._find_sellside_sweeps(candles, swings.lows)

        all_sweeps = buyside_sweeps + sellside_sweeps
        all_sweeps.sort(key=lambda s: s.index, reverse=True)

        logger.info(
            "liquidity_detector.detection_complete",
            buyside_count=len(buyside_sweeps),
            sellside_count=len(sellside_sweeps),
            total=len(all_sweeps),
        )

        return all_sweeps

    def _find_buyside_sweeps(
        self,
        candles: list[Candle],
        swing_highs: list[SwingPoint],
    ) -> list[LiquiditySweep]:
        """Find buy-side liquidity sweeps (wicks above swing highs, bearish signal).

        Buy-side liquidity sits above swing highs (buy stop orders).
        A sweep occurs when a candle's high exceeds the swing high but closes at or below it.
        Penetration > 2.0 ATR is filtered as a probable breakout, not a sweep.

        Args:
            candles: Full candle list
            swing_highs: List of swing high SwingPoints

        Returns:
            List of detected buy-side LiquiditySweep objects.
        """
        atr_series = calculate_atr_series(candles, period=self.ATR_PERIOD)
        sweeps: list[LiquiditySweep] = []

        for swing in swing_highs:
            for i in range(swing.index + 1, len(candles)):
                candle = candles[i]
                atr = atr_series[i]
                if atr is None or atr <= 0:
                    continue

                if candle.high > swing.price and candle.close <= swing.price:
                    penetration = candle.high - swing.price

                    if penetration > self.MAX_PENETRATION_ATR * atr:
                        continue

                    sweep = LiquiditySweep(
                        index=i,
                        time=candle.timestamp,
                        sweep_type="buyside",
                        level_price=swing.price,
                        sweep_high=candle.high,
                        sweep_low=candle.low,
                        penetration=penetration,
                        swing_index=swing.index,
                        atr_at_sweep=atr,
                    )
                    quality = self._calculate_sweep_quality(sweep, candles)
                    is_valid = self._is_sweep_valid(sweep, candles)

                    if not is_valid:
                        quality = SweepQuality(
                            sweep_age_bars=quality.sweep_age_bars,
                            penetration_atr=quality.penetration_atr,
                            rejection_strength=quality.rejection_strength,
                            level_tested_count=quality.level_tested_count,
                            passed=False,
                        )

                    sweep = LiquiditySweep(
                        index=sweep.index,
                        time=sweep.time,
                        sweep_type=sweep.sweep_type,
                        level_price=sweep.level_price,
                        sweep_high=sweep.sweep_high,
                        sweep_low=sweep.sweep_low,
                        penetration=sweep.penetration,
                        swing_index=sweep.swing_index,
                        atr_at_sweep=sweep.atr_at_sweep,
                        quality=quality,
                        is_valid=is_valid,
                    )
                    sweeps.append(sweep)

        return sweeps

    def _find_sellside_sweeps(
        self,
        candles: list[Candle],
        swing_lows: list[SwingPoint],
    ) -> list[LiquiditySweep]:
        """Find sell-side liquidity sweeps (wicks below swing lows, bullish signal).

        Sell-side liquidity sits below swing lows (sell stop orders).
        A sweep occurs when a candle's low drops below the swing low but closes at or above it.
        Penetration > 2.0 ATR is filtered as a probable breakout, not a sweep.

        Args:
            candles: Full candle list
            swing_lows: List of swing low SwingPoints

        Returns:
            List of detected sell-side LiquiditySweep objects.
        """
        atr_series = calculate_atr_series(candles, period=self.ATR_PERIOD)
        sweeps: list[LiquiditySweep] = []

        for swing in swing_lows:
            for i in range(swing.index + 1, len(candles)):
                candle = candles[i]
                atr = atr_series[i]
                if atr is None or atr <= 0:
                    continue

                if candle.low < swing.price and candle.close >= swing.price:
                    penetration = swing.price - candle.low

                    if penetration > self.MAX_PENETRATION_ATR * atr:
                        continue

                    sweep = LiquiditySweep(
                        index=i,
                        time=candle.timestamp,
                        sweep_type="sellside",
                        level_price=swing.price,
                        sweep_high=candle.high,
                        sweep_low=candle.low,
                        penetration=penetration,
                        swing_index=swing.index,
                        atr_at_sweep=atr,
                    )
                    quality = self._calculate_sweep_quality(sweep, candles)
                    is_valid = self._is_sweep_valid(sweep, candles)

                    if not is_valid:
                        quality = SweepQuality(
                            sweep_age_bars=quality.sweep_age_bars,
                            penetration_atr=quality.penetration_atr,
                            rejection_strength=quality.rejection_strength,
                            level_tested_count=quality.level_tested_count,
                            passed=False,
                        )

                    sweep = LiquiditySweep(
                        index=sweep.index,
                        time=sweep.time,
                        sweep_type=sweep.sweep_type,
                        level_price=sweep.level_price,
                        sweep_high=sweep.sweep_high,
                        sweep_low=sweep.sweep_low,
                        penetration=sweep.penetration,
                        swing_index=sweep.swing_index,
                        atr_at_sweep=sweep.atr_at_sweep,
                        quality=quality,
                        is_valid=is_valid,
                    )
                    sweeps.append(sweep)

        return sweeps

    def _calculate_sweep_quality(
        self,
        sweep: LiquiditySweep,
        candles: list[Candle],
    ) -> SweepQuality:
        """Calculate quality metrics for a detected sweep.

        Args:
            sweep: The LiquiditySweep to evaluate
            candles: Full candle list used during detection

        Returns:
            SweepQuality with age, penetration, rejection strength, test count, and passed flag.
        """
        last_idx = len(candles) - 1
        sweep_age_bars = last_idx - sweep.index

        atr = sweep.atr_at_sweep
        penetration_atr = (sweep.penetration / atr) if atr > 0 else 0.0

        sweep_candle = candles[sweep.index]
        rejection_strength = (
            abs(sweep_candle.close - sweep.level_price) / atr if atr > 0 else 0.0
        )

        level_tested_count = self._count_level_tests(sweep, candles)

        passed = (
            sweep_age_bars < self.MAX_AGE_BARS
            and penetration_atr > self.MIN_PENETRATION_ATR
            and penetration_atr < self.MAX_PENETRATION_ATR
            and rejection_strength > self.MIN_REJECTION_STRENGTH
        )

        return SweepQuality(
            sweep_age_bars=sweep_age_bars,
            penetration_atr=penetration_atr,
            rejection_strength=rejection_strength,
            level_tested_count=level_tested_count,
            passed=passed,
        )

    def _count_level_tests(
        self,
        sweep: LiquiditySweep,
        candles: list[Candle],
    ) -> int:
        """Count how many times the swing level was tested (wick within ±0.2 ATR) before sweep.

        Args:
            sweep: The LiquiditySweep to evaluate
            candles: Full candle list

        Returns:
            Number of candles with wick within ±0.2 ATR of the level, prior to sweep.
        """
        atr = sweep.atr_at_sweep
        proximity = self.LEVEL_TEST_PROXIMITY_ATR * atr
        level = sweep.level_price
        count = 0

        for i in range(sweep.swing_index + 1, sweep.index):
            candle = candles[i]
            if sweep.sweep_type == "buyside":
                if abs(candle.high - level) <= proximity:
                    count += 1
            else:
                if abs(candle.low - level) <= proximity:
                    count += 1

        return count

    def _is_sweep_valid(
        self,
        sweep: LiquiditySweep,
        candles: list[Candle],
    ) -> bool:
        """Determine if a sweep is still valid.

        A sweep becomes invalid if ANY of the following:
        - sweep_age_bars > 20 (too old)
        - penetration_atr > 2.0 (breakout, not sweep)
        - Next candle after sweep closes beyond the level (confirms breakout)

        Args:
            sweep: The LiquiditySweep to evaluate
            candles: Full candle list

        Returns:
            True if sweep is still valid, False otherwise.
        """
        last_idx = len(candles) - 1
        age = last_idx - sweep.index

        if age > self.MAX_AGE_BARS:
            return False

        atr = sweep.atr_at_sweep
        if atr > 0 and (sweep.penetration / atr) > self.MAX_PENETRATION_ATR:
            return False

        next_idx = sweep.index + 1
        if next_idx <= last_idx:
            next_candle = candles[next_idx]
            if sweep.sweep_type == "buyside":
                if next_candle.close > sweep.level_price:
                    return False
            else:
                if next_candle.close < sweep.level_price:
                    return False

        return True
