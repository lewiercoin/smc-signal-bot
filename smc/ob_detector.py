"""Order Block detector module for SMC Signal Bot.

Detects bullish and bearish Order Blocks based on displacement impulses
using close-to-close ATR measurement (ICT/SMC body movement definition).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from smc.utils import calculate_atr_series

if TYPE_CHECKING:
    from connectors.oanda_client import Candle
    from smc.swing_detector import SwingResult

logger = structlog.get_logger(__name__)

_ATR_PERIOD = 14
_IMPULSE_ATR_MULTIPLIER = 1.5
_MAX_OB_AGE_BARS = 50
_MAX_OB_TOUCHES = 2
_MIN_OB_SIZE_ATR = 0.3


@dataclass(frozen=True)
class OBQuality:
    """Quality metrics for an Order Block.

    Attributes:
        ob_age_bars: How many candles ago the OB formed (from end of candle list)
        ob_touches: How many times price wicked into the OB zone without closing through
        ob_size_atr: OB size (high - low) in ATR units
        passed: True if age < 50 AND touches <= 2 AND size > 0.3 ATR
    """

    ob_age_bars: int
    ob_touches: int
    ob_size_atr: float
    passed: bool


@dataclass(frozen=True)
class OrderBlock:
    """A detected Order Block.

    Attributes:
        index: Candle index in the original candle list
        time: Timestamp of the OB candle
        ob_type: "bullish" or "bearish"
        high: High of the OB candle
        low: Low of the OB candle
        open: Open of the OB candle
        close: Close of the OB candle
        atr_at_formation: ATR value at the time of OB formation
        quality: OBQuality metrics (None if not yet calculated)
        is_valid: False if price has closed through the OB zone
    """

    index: int
    time: datetime
    ob_type: str  # "bullish" | "bearish"
    high: float
    low: float
    open: float
    close: float
    atr_at_formation: float
    quality: OBQuality | None = field(default=None)
    is_valid: bool = field(default=True)


class OrderBlockDetector:
    """Detects Order Blocks from candle data and swing context.

    An Order Block is the last directional candle before a displacement impulse.
    Impulse is measured close-to-close (body movement per ICT definition).
    Requires minimum 14 candles for ATR(14) calculation.
    """

    def detect(self, candles: list[Candle], swings: SwingResult) -> list[OrderBlock]:
        """Detect all valid Order Blocks in the candle series.

        Args:
            candles: List of OHLCV candles (chronological order)
            swings: SwingResult from SwingDetector (used for context)

        Returns:
            List of OrderBlock sorted newest first. Empty list if < 14 candles.
        """
        if len(candles) < _ATR_PERIOD:
            logger.warning(
                "Insufficient candles for ATR calculation",
                candles_count=len(candles),
                required=_ATR_PERIOD,
            )
            return []

        bullish_obs = self._find_bullish_ob(candles, swings)
        bearish_obs = self._find_bearish_ob(candles, swings)

        all_obs: list[OrderBlock] = []

        for ob in bullish_obs + bearish_obs:
            is_valid = self._is_ob_valid(ob, candles)
            quality = self._calculate_ob_quality(ob, candles)

            if not is_valid and quality.passed:
                quality = OBQuality(
                    ob_age_bars=quality.ob_age_bars,
                    ob_touches=quality.ob_touches,
                    ob_size_atr=quality.ob_size_atr,
                    passed=False,
                )

            updated_ob = OrderBlock(
                index=ob.index,
                time=ob.time,
                ob_type=ob.ob_type,
                high=ob.high,
                low=ob.low,
                open=ob.open,
                close=ob.close,
                atr_at_formation=ob.atr_at_formation,
                quality=quality,
                is_valid=is_valid,
            )
            all_obs.append(updated_ob)

        all_obs.sort(key=lambda x: x.index, reverse=True)

        logger.debug(
            "Order blocks detected",
            total=len(all_obs),
            bullish=len(bullish_obs),
            bearish=len(bearish_obs),
        )
        return all_obs

    def _find_bullish_ob(
        self, candles: list[Candle], swings: SwingResult
    ) -> list[OrderBlock]:
        """Find bullish Order Blocks (bearish candle before bullish impulse).

        Bullish impulse = close[i] - close[i-1] > 1.5 * ATR(14) (close-to-close).
        OB candle = candle[i-1], must be bearish (close < open).

        Args:
            candles: Full candle list
            swings: Swing context (unused in detection, kept for API consistency)

        Returns:
            List of bullish OrderBlock instances (without quality/validity set).
        """
        atr_values = calculate_atr_series(candles, _ATR_PERIOD)
        obs: list[OrderBlock] = []

        for i in range(1, len(candles)):
            atr_idx = i - 1
            if atr_idx >= len(atr_values) or atr_values[atr_idx] is None:
                continue
            atr = atr_values[atr_idx]
            if atr is None or atr <= 0:
                continue

            close_move = candles[i].close - candles[i - 1].close
            if close_move <= _IMPULSE_ATR_MULTIPLIER * atr:
                continue

            ob_candle = candles[i - 1]
            if ob_candle.close >= ob_candle.open:
                continue

            ob = OrderBlock(
                index=i - 1,
                time=ob_candle.timestamp,
                ob_type="bullish",
                high=ob_candle.high,
                low=ob_candle.low,
                open=ob_candle.open,
                close=ob_candle.close,
                atr_at_formation=atr,
            )
            obs.append(ob)

        return obs

    def _find_bearish_ob(
        self, candles: list[Candle], swings: SwingResult
    ) -> list[OrderBlock]:
        """Find bearish Order Blocks (bullish candle before bearish impulse).

        Bearish impulse = close[i-1] - close[i] > 1.5 * ATR(14) (close-to-close).
        OB candle = candle[i-1], must be bullish (close > open).

        Args:
            candles: Full candle list
            swings: Swing context (unused in detection, kept for API consistency)

        Returns:
            List of bearish OrderBlock instances (without quality/validity set).
        """
        atr_values = calculate_atr_series(candles, _ATR_PERIOD)
        obs: list[OrderBlock] = []

        for i in range(1, len(candles)):
            atr_idx = i - 1
            if atr_idx >= len(atr_values) or atr_values[atr_idx] is None:
                continue
            atr = atr_values[atr_idx]
            if atr is None or atr <= 0:
                continue

            close_move = candles[i - 1].close - candles[i].close
            if close_move <= _IMPULSE_ATR_MULTIPLIER * atr:
                continue

            ob_candle = candles[i - 1]
            if ob_candle.close <= ob_candle.open:
                continue

            ob = OrderBlock(
                index=i - 1,
                time=ob_candle.timestamp,
                ob_type="bearish",
                high=ob_candle.high,
                low=ob_candle.low,
                open=ob_candle.open,
                close=ob_candle.close,
                atr_at_formation=atr,
            )
            obs.append(ob)

        return obs

    def _calculate_ob_quality(
        self, ob: OrderBlock, candles: list[Candle]
    ) -> OBQuality:
        """Calculate quality metrics for an Order Block.

        Metrics:
        - ob_age_bars: candles since OB formation (len(candles) - 1 - ob.index)
        - ob_touches: wicks entering zone without close breaking through
        - ob_size_atr: (high - low) / atr_at_formation
        - passed: age < 50 AND touches <= 2 AND size > 0.3 ATR

        Touch definition:
        - Bullish OB: candle.low <= ob.high AND candle.close >= ob.low
        - Bearish OB: candle.high >= ob.low AND candle.close <= ob.high

        Args:
            ob: The OrderBlock to evaluate
            candles: Full candle list

        Returns:
            OBQuality with computed metrics.
        """
        ob_age_bars = len(candles) - 1 - ob.index
        ob_size_atr = (ob.high - ob.low) / ob.atr_at_formation if ob.atr_at_formation > 0 else 0.0

        ob_touches = 0
        for candle in candles[ob.index + 1 :]:
            if ob.ob_type == "bullish":
                if candle.low <= ob.high and candle.close >= ob.low:
                    ob_touches += 1
            else:
                if candle.high >= ob.low and candle.close <= ob.high:
                    ob_touches += 1

        passed = (
            ob_age_bars < _MAX_OB_AGE_BARS
            and ob_touches <= _MAX_OB_TOUCHES
            and ob_size_atr > _MIN_OB_SIZE_ATR
        )

        return OBQuality(
            ob_age_bars=ob_age_bars,
            ob_touches=ob_touches,
            ob_size_atr=ob_size_atr,
            passed=passed,
        )

    def _is_ob_valid(self, ob: OrderBlock, current_candles: list[Candle]) -> bool:
        """Check if an Order Block is still valid (not invalidated by price).

        An OB is invalid if ANY of:
        - Bullish OB: any candle closed below ob.low
        - Bearish OB: any candle closed above ob.high
        - ob_age_bars > 50
        - ob_touches > 2

        Args:
            ob: The OrderBlock to validate
            current_candles: Full candle list

        Returns:
            True if OB is still valid, False otherwise.
        """
        ob_age_bars = len(current_candles) - 1 - ob.index
        if ob_age_bars > _MAX_OB_AGE_BARS:
            return False

        ob_touches = 0
        for candle in current_candles[ob.index + 1 :]:
            if ob.ob_type == "bullish":
                if candle.close < ob.low:
                    return False
                if candle.low <= ob.high and candle.close >= ob.low:
                    ob_touches += 1
            else:
                if candle.close > ob.high:
                    return False
                if candle.high >= ob.low and candle.close <= ob.high:
                    ob_touches += 1

        return ob_touches <= _MAX_OB_TOUCHES

