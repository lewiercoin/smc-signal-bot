"""Fair Value Gap (FVG) detector module for SMC Signal Bot.

Implements ICT/SMC 3-candle imbalance detection with quality scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from smc.utils import calculate_atr_series

if TYPE_CHECKING:
    from connectors.oanda_client import Candle
    from smc.structure_analyzer import StructureResult

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FVGQuality:
    """Quality metrics for a Fair Value Gap.

    Attributes:
        fvg_age_bars: Number of candles since FVG formation (counted from end of candles list)
        fvg_size_atr: FVG size (gap_high - gap_low) expressed in ATR(14) units
        fill_percentage: How much of the FVG has been filled (0.0 = untouched, 1.0 = fully filled)
        passed: True if age < 30 AND fill_percentage < 0.5 AND fvg_size_atr > 0.2
    """

    fvg_age_bars: int
    fvg_size_atr: float
    fill_percentage: float
    passed: bool


@dataclass(frozen=True)
class FVG:
    """A detected Fair Value Gap (3-candle imbalance zone).

    Attributes:
        index: Index of the middle candle (candle2) in the candles list
        time: Timestamp of candle2
        fvg_type: "bullish" or "bearish"
        gap_high: Upper boundary of the FVG zone
        gap_low: Lower boundary of the FVG zone
        candle1_index: Index of candle1 in the candles list
        candle3_index: Index of candle3 in the candles list
        atr_at_formation: ATR(14) value at the time of formation
        quality: FVGQuality metrics (None if not yet evaluated)
        is_valid: False if fully filled or older than 30 bars
    """

    index: int
    time: datetime
    fvg_type: str
    gap_high: float
    gap_low: float
    candle1_index: int
    candle3_index: int
    atr_at_formation: float
    quality: FVGQuality | None = field(default=None)
    is_valid: bool = field(default=True)


class FairValueGapDetector:
    """Detects Fair Value Gaps using ICT/SMC 3-candle imbalance methodology.

    A Fair Value Gap forms when the middle candle of a 3-candle sequence
    moves so strongly that a price gap (imbalance) is left between candle1
    and candle3. Price tends to return to fill this gap.

    Bullish FVG: candle3.low > candle1.high (gap above)
    Bearish FVG: candle3.high < candle1.low (gap below)
    """

    ATR_PERIOD: int = 14
    MAX_AGE_BARS: int = 30
    MIN_SIZE_ATR: float = 0.2
    MAX_FILL_PASSED: float = 0.5

    def detect(
        self,
        candles: list[Candle],
        structure: StructureResult | None = None,
    ) -> list[FVG]:
        """Detect all Fair Value Gaps in the candle series.

        Args:
            candles: List of OHLCV candles in chronological order
            structure: Optional structure context (reserved for future use)

        Returns:
            List of FVG objects sorted newest first (highest index first).
            Returns empty list if fewer than 3 candles.
        """
        if len(candles) < 3:
            logger.warning(
                "fvg_detector.insufficient_candles",
                required=3,
                got=len(candles),
            )
            return []

        atr_series = calculate_atr_series(candles, period=self.ATR_PERIOD)
        fvgs: list[FVG] = []

        for i in range(len(candles) - 2):
            candle1 = candles[i]
            candle2 = candles[i + 1]
            candle3 = candles[i + 2]

            atr_at_formation = atr_series[i + 2] or 0.0

            if self._find_bullish_fvg(candle1, candle2, candle3):
                fvg = FVG(
                    index=i + 1,
                    time=candle2.timestamp,
                    fvg_type="bullish",
                    gap_high=candle3.low,
                    gap_low=candle1.high,
                    candle1_index=i,
                    candle3_index=i + 2,
                    atr_at_formation=atr_at_formation,
                )
                quality = self._calculate_fvg_quality(fvg, candles)
                is_valid = self._is_fvg_valid(fvg, candles)
                fvg = FVG(
                    index=fvg.index,
                    time=fvg.time,
                    fvg_type=fvg.fvg_type,
                    gap_high=fvg.gap_high,
                    gap_low=fvg.gap_low,
                    candle1_index=fvg.candle1_index,
                    candle3_index=fvg.candle3_index,
                    atr_at_formation=fvg.atr_at_formation,
                    quality=quality,
                    is_valid=is_valid,
                )
                fvgs.append(fvg)

            elif self._find_bearish_fvg(candle1, candle2, candle3):
                fvg = FVG(
                    index=i + 1,
                    time=candle2.timestamp,
                    fvg_type="bearish",
                    gap_high=candle1.low,
                    gap_low=candle3.high,
                    candle1_index=i,
                    candle3_index=i + 2,
                    atr_at_formation=atr_at_formation,
                )
                quality = self._calculate_fvg_quality(fvg, candles)
                is_valid = self._is_fvg_valid(fvg, candles)
                fvg = FVG(
                    index=fvg.index,
                    time=fvg.time,
                    fvg_type=fvg.fvg_type,
                    gap_high=fvg.gap_high,
                    gap_low=fvg.gap_low,
                    candle1_index=fvg.candle1_index,
                    candle3_index=fvg.candle3_index,
                    atr_at_formation=fvg.atr_at_formation,
                    quality=quality,
                    is_valid=is_valid,
                )
                fvgs.append(fvg)

        fvgs.sort(key=lambda f: f.index, reverse=True)
        return fvgs

    def _find_bullish_fvg(
        self,
        candle1: Candle,
        candle2: Candle,
        candle3: Candle,
    ) -> bool:
        """Check if 3 candles form a bullish Fair Value Gap.

        Bullish FVG: candle3.low > candle1.high
        The gap zone runs from candle1.high (bottom) to candle3.low (top).

        Args:
            candle1: First candle (left reference)
            candle2: Middle candle (impulse candle — unused in condition)
            candle3: Third candle (right reference)

        Returns:
            True if bullish FVG condition is met.
        """
        return candle3.low > candle1.high

    def _find_bearish_fvg(
        self,
        candle1: Candle,
        candle2: Candle,
        candle3: Candle,
    ) -> bool:
        """Check if 3 candles form a bearish Fair Value Gap.

        Bearish FVG: candle3.high < candle1.low
        The gap zone runs from candle3.high (bottom) to candle1.low (top).

        Args:
            candle1: First candle (left reference)
            candle2: Middle candle (impulse candle — unused in condition)
            candle3: Third candle (right reference)

        Returns:
            True if bearish FVG condition is met.
        """
        return candle3.high < candle1.low

    def _calculate_fvg_quality(self, fvg: FVG, candles: list[Candle]) -> FVGQuality:
        """Calculate quality metrics for a detected FVG.

        Args:
            fvg: The FVG to evaluate
            candles: Full candle list used during detection

        Returns:
            FVGQuality with age, size, fill percentage, and passed flag.
        """
        last_idx = len(candles) - 1
        fvg_age_bars = last_idx - fvg.index

        gap_size = fvg.gap_high - fvg.gap_low
        fvg_size_atr = (gap_size / fvg.atr_at_formation) if fvg.atr_at_formation > 0 else 0.0

        fill_percentage = self._calculate_fill_percentage(fvg, candles)

        passed = (
            fvg_age_bars < self.MAX_AGE_BARS
            and fill_percentage < self.MAX_FILL_PASSED
            and fvg_size_atr > self.MIN_SIZE_ATR
        )

        return FVGQuality(
            fvg_age_bars=fvg_age_bars,
            fvg_size_atr=fvg_size_atr,
            fill_percentage=fill_percentage,
            passed=passed,
        )

    def _calculate_fill_percentage(self, fvg: FVG, candles: list[Candle]) -> float:
        """Calculate how much of the FVG zone has been filled by subsequent price action.

        For bullish FVG: tracks the lowest low of candles that enter the zone.
        For bearish FVG: tracks the highest high of candles that enter the zone.

        Args:
            fvg: The FVG to evaluate
            candles: Full candle list

        Returns:
            Fill percentage clamped to [0.0, 1.0].
        """
        gap_size = fvg.gap_high - fvg.gap_low
        if gap_size <= 0:
            return 1.0

        post_formation = candles[fvg.candle3_index + 1 :]
        if not post_formation:
            return 0.0

        if fvg.fvg_type == "bullish":
            lows_in_zone = [
                c.low for c in post_formation if c.low < fvg.gap_high
            ]
            if not lows_in_zone:
                return 0.0
            lowest_low = min(lows_in_zone)
            fill = (fvg.gap_high - lowest_low) / gap_size
            return max(0.0, min(1.0, fill))

        highs_in_zone = [
            c.high for c in post_formation if c.high > fvg.gap_low
        ]
        if not highs_in_zone:
            return 0.0
        highest_high = max(highs_in_zone)
        fill = (highest_high - fvg.gap_low) / gap_size
        return max(0.0, min(1.0, fill))

    def _is_fvg_valid(self, fvg: FVG, candles: list[Candle]) -> bool:
        """Determine if an FVG is still valid (not fully filled, not too old).

        An FVG becomes invalid if:
        - fill_percentage >= 1.0 (imbalance fully resolved)
        - fvg_age_bars > 30 (no longer relevant)

        Partially filled FVGs (0.0–0.99) remain valid — per ICT methodology,
        price tends to return to fill them completely.

        Args:
            fvg: The FVG to evaluate
            candles: Full candle list

        Returns:
            True if FVG is still valid, False otherwise.
        """
        last_idx = len(candles) - 1
        age = last_idx - fvg.index

        if age > self.MAX_AGE_BARS:
            return False

        fill = self._calculate_fill_percentage(fvg, candles)
        if fill >= 1.0:
            return False

        return True
