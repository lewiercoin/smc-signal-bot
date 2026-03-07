"""Swing detector module for SMC Signal Bot.

Implements GROK-2 dynamic swing_length adaptation based on ATR volatility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from smc.utils import calculate_atr_scalar

if TYPE_CHECKING:
    from connectors.oanda_client import Candle

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SwingPoint:
    """A single swing high or low point.

    Attributes:
        index: Position in the candle list
        price: Price level of the swing point
        time: Timestamp of the candle
        swing_type: "high" for swing high, "low" for swing low
        strength: Number of candles on each side confirming the swing
    """

    index: int
    price: float
    time: datetime
    swing_type: str  # "high" / "low"
    strength: int  # ile świec po każdej stronie


@dataclass(frozen=True)
class SwingResult:
    """Result of swing detection containing highs, lows and metadata.

    Attributes:
        highs: List of detected swing highs
        lows: List of detected swing lows
        swing_length_used: The swing length value used for detection
        volatility_regime: Current volatility regime ("high"/"normal"/"low")
    """

    highs: list[SwingPoint]
    lows: list[SwingPoint]
    swing_length_used: int
    volatility_regime: str  # "high" / "normal" / "low"


class SwingDetector:
    """Detects swing highs and lows with ATR-adaptive swing length.

    Implements GROK-2 dynamic swing_length mechanism:
    - Base swing_length = 10
    - High volatility (ATR > 1.5x average): swing_length = 14
    - Low volatility (ATR < 0.7x average): swing_length = 7
    - Change only when new value persists for 3 consecutive candles
    """

    # Default swing length parameters
    BASE_SWING_LENGTH: int = 10
    HIGH_VOL_SWING_LENGTH: int = 14
    LOW_VOL_SWING_LENGTH: int = 7

    # ATR threshold multipliers
    HIGH_VOL_THRESHOLD: float = 1.5
    LOW_VOL_THRESHOLD: float = 0.7

    # Stability requirement for swing length change
    STABILITY_CANDLES: int = 3

    # ATR calculation period
    ATR_PERIOD: int = 14

    def __init__(self) -> None:
        """Initialize the swing detector."""
        self.logger = logger.bind(module="swing_detector")
        self._current_swing_length: int = self.BASE_SWING_LENGTH
        self._proposed_length: int | None = None
        self._proposed_count: int = 0

    def detect(self, candles: list[Candle]) -> SwingResult:
        """Detect swing highs and lows in the provided candles.

        Args:
            candles: List of OHLCV candles to analyze

        Returns:
            SwingResult containing detected highs, lows, and metadata
        """
        if len(candles) < self.BASE_SWING_LENGTH * 2 + 1:
            self.logger.warning(
                "insufficient_candles",
                count=len(candles),
                required=self.BASE_SWING_LENGTH * 2 + 1,
            )
            return SwingResult(
                highs=[],
                lows=[],
                swing_length_used=self._current_swing_length,
                volatility_regime="normal",
            )

        # Calculate dynamic swing length based on volatility
        swing_length = self._calculate_dynamic_swing_length(candles)
        volatility_regime = self._determine_volatility_regime(candles)

        self.logger.debug(
            "swing_detection_started",
            candle_count=len(candles),
            swing_length=swing_length,
            volatility_regime=volatility_regime,
        )

        # Find swing highs and lows
        highs = self._find_swing_highs(candles, swing_length)
        lows = self._find_swing_lows(candles, swing_length)

        self.logger.info(
            "swing_detection_complete",
            swing_highs_count=len(highs),
            swing_lows_count=len(lows),
            swing_length_used=swing_length,
            volatility_regime=volatility_regime,
        )

        return SwingResult(
            highs=highs,
            lows=lows,
            swing_length_used=swing_length,
            volatility_regime=volatility_regime,
        )

    def _calculate_dynamic_swing_length(self, candles: list[Candle]) -> int:
        """Calculate ATR-adaptive swing length with 3-candle stability.

        Args:
            candles: List of OHLCV candles

        Returns:
            Dynamic swing length (7, 10, or 14)
        """
        if len(candles) < self.ATR_PERIOD * 2 + 1:
            return self._current_swing_length

        # Calculate recent ATR (last period candles)
        recent_candles = candles[-self.ATR_PERIOD :]
        recent_atr = calculate_atr_scalar(recent_candles, self.ATR_PERIOD)

        # Calculate historical ATR (candles before the recent ones)
        historical_candles = candles[: -self.ATR_PERIOD]
        if len(historical_candles) < self.ATR_PERIOD:
            return self._current_swing_length

        lookback = min(50, len(historical_candles) - self.ATR_PERIOD)
        atr_values: list[float] = []

        for i in range(lookback):
            end_idx = len(historical_candles) - i
            start_idx = end_idx - self.ATR_PERIOD
            if start_idx >= 0:
                atr = calculate_atr_scalar(
                    historical_candles[start_idx:end_idx], self.ATR_PERIOD
                )
                atr_values.append(atr)

        if not atr_values:
            return self._current_swing_length

        avg_historical_atr = sum(atr_values) / len(atr_values)

        if avg_historical_atr == 0:
            return self._current_swing_length

        atr_ratio = recent_atr / avg_historical_atr

        # Determine target swing length based on volatility
        if atr_ratio > self.HIGH_VOL_THRESHOLD:
            target_length = self.HIGH_VOL_SWING_LENGTH
        elif atr_ratio < self.LOW_VOL_THRESHOLD:
            target_length = self.LOW_VOL_SWING_LENGTH
        else:
            target_length = self.BASE_SWING_LENGTH

        # Apply 3-candle stability rule
        new_length = self._apply_stability_rule(target_length)

        self.logger.debug(
            "dynamic_swing_length_calculated",
            recent_atr=recent_atr,
            avg_historical_atr=avg_historical_atr,
            atr_ratio=atr_ratio,
            target_length=target_length,
            new_length=new_length,
            previous_length=self._current_swing_length,
        )

        return new_length

    def _apply_stability_rule(self, target_length: int) -> int:
        """Apply 3-candle stability rule to swing length changes.

        Args:
            target_length: The proposed new swing length

        Returns:
            Confirmed swing length (may be current or target)
        """
        if target_length == self._current_swing_length:
            # Reset proposal tracking when target matches current
            self._proposed_length = None
            self._proposed_count = 0
            return self._current_swing_length

        if target_length == self._proposed_length:
            # Same proposal as before - increment counter
            self._proposed_count += 1
            if self._proposed_count >= self.STABILITY_CANDLES:
                # Stability requirement met - apply change
                self.logger.info(
                    "swing_length_changed",
                    old_length=self._current_swing_length,
                    new_length=target_length,
                    consecutive_candles=self._proposed_count,
                )
                self._current_swing_length = target_length
                self._proposed_length = None
                self._proposed_count = 0
                return self._current_swing_length
        else:
            # New proposal - reset counter
            self._proposed_length = target_length
            self._proposed_count = 1

        return self._current_swing_length

    def _determine_volatility_regime(self, candles: list[Candle]) -> str:
        """Determine current volatility regime for reporting.

        Args:
            candles: List of OHLCV candles

        Returns:
            Volatility regime string: "high", "normal", or "low"
        """
        if len(candles) < self.ATR_PERIOD * 2 + 1:
            return "normal"

        # Calculate recent ATR (last few candles)
        recent_candles = candles[-self.ATR_PERIOD :]
        recent_atr = calculate_atr_scalar(recent_candles, self.ATR_PERIOD)

        # Calculate historical ATR (candles before the recent ones)
        historical_candles = candles[: -self.ATR_PERIOD]
        if len(historical_candles) < self.ATR_PERIOD:
            return "normal"

        lookback = min(50, len(historical_candles) - self.ATR_PERIOD)
        atr_values: list[float] = []

        for i in range(lookback):
            end_idx = len(historical_candles) - i
            start_idx = end_idx - self.ATR_PERIOD
            if start_idx >= 0:
                atr = calculate_atr_scalar(
                    historical_candles[start_idx:end_idx], self.ATR_PERIOD
                )
                atr_values.append(atr)

        if not atr_values:
            return "normal"

        avg_historical_atr = sum(atr_values) / len(atr_values)

        if avg_historical_atr == 0:
            return "normal"

        atr_ratio = recent_atr / avg_historical_atr

        if atr_ratio > self.HIGH_VOL_THRESHOLD:
            return "high"
        elif atr_ratio < self.LOW_VOL_THRESHOLD:
            return "low"
        return "normal"

    def _find_swing_highs(
        self,
        candles: list[Candle],
        swing_length: int,
    ) -> list[SwingPoint]:
        """Find swing highs in the candle data.

        A swing high is a candle whose high is greater than all highs
        of 'swing_length' candles on both sides.

        Args:
            candles: List of OHLCV candles
            swing_length: Number of candles to check on each side

        Returns:
            List of SwingPoint objects for swing highs
        """
        swing_highs: list[SwingPoint] = []

        # Need swing_length candles on each side
        for i in range(swing_length, len(candles) - swing_length):
            current_high = candles[i].high
            is_swing_high = True

            # Check left side
            for j in range(1, swing_length + 1):
                if candles[i - j].high >= current_high:
                    is_swing_high = False
                    break

            if not is_swing_high:
                continue

            # Check right side
            for j in range(1, swing_length + 1):
                if candles[i + j].high >= current_high:
                    is_swing_high = False
                    break

            if is_swing_high:
                swing_highs.append(
                    SwingPoint(
                        index=i,
                        price=current_high,
                        time=candles[i].timestamp,
                        swing_type="high",
                        strength=swing_length,
                    )
                )

        return swing_highs

    def _find_swing_lows(
        self,
        candles: list[Candle],
        swing_length: int,
    ) -> list[SwingPoint]:
        """Find swing lows in the candle data.

        A swing low is a candle whose low is lower than all lows
        of 'swing_length' candles on both sides.

        Args:
            candles: List of OHLCV candles
            swing_length: Number of candles to check on each side

        Returns:
            List of SwingPoint objects for swing lows
        """
        swing_lows: list[SwingPoint] = []

        # Need swing_length candles on each side
        for i in range(swing_length, len(candles) - swing_length):
            current_low = candles[i].low
            is_swing_low = True

            # Check left side
            for j in range(1, swing_length + 1):
                if candles[i - j].low <= current_low:
                    is_swing_low = False
                    break

            if not is_swing_low:
                continue

            # Check right side
            for j in range(1, swing_length + 1):
                if candles[i + j].low <= current_low:
                    is_swing_low = False
                    break

            if is_swing_low:
                swing_lows.append(
                    SwingPoint(
                        index=i,
                        price=current_low,
                        time=candles[i].timestamp,
                        swing_type="low",
                        strength=swing_length,
                    )
                )

        return swing_lows
