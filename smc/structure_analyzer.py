"""Structure analyzer module for SMC Signal Bot.

Analyzes market structure based on swing points:
- Trend determination (bullish/bearish/ranging)
- Break of Structure (BoS) detection
- Change of Character (CHoCH) detection
- HTF bias determination
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from smc.swing_detector import SwingPoint, SwingResult

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class BOS:
    """Break of Structure event.

    Attributes:
        price: Price level where BoS occurred
        time: Timestamp of the BoS
        direction: "bullish" or "bearish"
        swing_point: The SwingPoint that triggered the BoS
    """

    price: float
    time: datetime
    direction: str  # "bullish" / "bearish"
    swing_point: "SwingPoint"


@dataclass(frozen=True)
class CHoCH:
    """Change of Character event.

    Attributes:
        price: Price level where CHoCH occurred
        time: Timestamp of the CHoCH
        from_trend: Previous trend direction
        to_trend: New trend direction
        swing_point: The SwingPoint that triggered the CHoCH
    """

    price: float
    time: datetime
    from_trend: str  # "bullish" / "bearish"
    to_trend: str  # "bullish" / "bearish"
    swing_point: "SwingPoint"


@dataclass(frozen=True)
class StructureResult:
    """Result of market structure analysis.

    Attributes:
        trend: Current market trend ("bullish"/"bearish"/"ranging")
        last_bos: Most recent Break of Structure or None
        last_choch: Most recent Change of Character or None
        htf_bias: HTF trading bias ("long"/"short"/"neutral")
        key_highs: Significant swing highs
        key_lows: Significant swing lows
    """

    trend: str  # "bullish" / "bearish" / "ranging"
    last_bos: BOS | None
    last_choch: CHoCH | None
    htf_bias: str  # "long" / "short" / "neutral"
    key_highs: list["SwingPoint"]
    key_lows: list["SwingPoint"]


class StructureAnalyzer:
    """Analyzes market structure from swing points.

    Detects:
    - Market trend (bullish/bearish/ranging)
    - Break of Structure (BoS)
    - Change of Character (CHoCH)
    - HTF bias for trading decisions
    """

    def __init__(self) -> None:
        """Initialize the structure analyzer."""
        self.logger = logger.bind(module="structure_analyzer")

    def analyze(self, swings: SwingResult) -> StructureResult:
        """Analyze market structure from swing points.

        Args:
            swings: SwingResult from SwingDetector

        Returns:
            StructureResult with trend, BoS, CHoCH, and HTF bias
        """
        self.logger.debug(
            "structure_analysis_started",
            highs_count=len(swings.highs),
            lows_count=len(swings.lows),
        )

        # Determine trend from swing sequence
        trend = self._determine_trend(swings)

        # Detect Break of Structure
        last_bos = self._detect_bos(swings)

        # Detect Change of Character
        last_choch = self._detect_choch(swings, trend)

        # Determine HTF bias
        htf_bias = self._determine_htf_bias(trend, last_bos, last_choch)

        # Extract key highs and lows (last 3 of each)
        key_highs = swings.highs[-3:] if len(swings.highs) >= 3 else swings.highs
        key_lows = swings.lows[-3:] if len(swings.lows) >= 3 else swings.lows

        self.logger.info(
            "structure_analysis_complete",
            trend=trend,
            has_bos=last_bos is not None,
            has_choch=last_choch is not None,
            htf_bias=htf_bias,
        )

        return StructureResult(
            trend=trend,
            last_bos=last_bos,
            last_choch=last_choch,
            htf_bias=htf_bias,
            key_highs=key_highs,
            key_lows=key_lows,
        )

    def _detect_bos(self, swings: SwingResult) -> BOS | None:
        """Detect Break of Structure from swing points.

        Bullish BoS: Price breaks above previous swing high
        Bearish BoS: Price breaks below previous swing low

        Args:
            swings: SwingResult containing swing highs and lows

        Returns:
            BOS object or None if no BoS detected
        """
        if len(swings.highs) < 2 or len(swings.lows) < 2:
            return None

        # Get last two swing highs and lows
        last_high = swings.highs[-1]
        prev_high = swings.highs[-2]
        last_low = swings.lows[-1]
        prev_low = swings.lows[-2]

        # Bullish BoS: new high breaks above previous high
        if last_high.price > prev_high.price and last_high.index > prev_high.index:
            return BOS(
                price=last_high.price,
                time=last_high.time,
                direction="bullish",
                swing_point=last_high,
            )

        # Bearish BoS: new low breaks below previous low
        if last_low.price < prev_low.price and last_low.index > prev_low.index:
            return BOS(
                price=last_low.price,
                time=last_low.time,
                direction="bearish",
                swing_point=last_low,
            )

        return None

    def _detect_choch(self, swings: SwingResult, current_trend: str) -> CHoCH | None:
        """Detect Change of Character (trend reversal signal).

        CHoCH is a BoS that goes against the current trend,
        indicating a potential trend change.

        Args:
            swings: SwingResult containing swing highs and lows
            current_trend: Currently determined trend

        Returns:
            CHoCH object or None if no CHoCH detected
        """
        if len(swings.highs) < 2 or len(swings.lows) < 2:
            return None

        # Get last two swing highs and lows
        last_high = swings.highs[-1]
        prev_high = swings.highs[-2]
        last_low = swings.lows[-1]
        prev_low = swings.lows[-2]

        # Bullish CHoCH in bearish trend
        # Price makes higher low after lower lows (breaking bearish structure)
        if current_trend == "bearish":
            if last_low.price > prev_low.price and last_low.index > prev_low.index:
                return CHoCH(
                    price=last_low.price,
                    time=last_low.time,
                    from_trend="bearish",
                    to_trend="bullish",
                    swing_point=last_low,
                )

        # Bearish CHoCH in bullish trend
        # Price makes lower high after higher highs (breaking bullish structure)
        if current_trend == "bullish":
            if last_high.price < prev_high.price and last_high.index > prev_high.index:
                return CHoCH(
                    price=last_high.price,
                    time=last_high.time,
                    from_trend="bullish",
                    to_trend="bearish",
                    swing_point=last_high,
                )

        return None

    def _determine_trend(self, swings: SwingResult) -> str:
        """Determine market trend from swing point sequence.

        Bullish: Higher Highs (HH) + Higher Lows (HL)
        Bearish: Lower Highs (LH) + Lower Lows (LL)
        Ranging: No clear sequence

        Args:
            swings: SwingResult containing swing highs and lows

        Returns:
            Trend string: "bullish", "bearish", or "ranging"
        """
        if len(swings.highs) < 2 or len(swings.lows) < 2:
            return "ranging"

        # Analyze last 3 swing highs for direction
        recent_highs = swings.highs[-3:]
        recent_lows = swings.lows[-3:]

        # Check for Higher Highs (bullish) or Lower Highs (bearish)
        hh_count = 0
        lh_count = 0

        for i in range(1, len(recent_highs)):
            if recent_highs[i].price > recent_highs[i - 1].price:
                hh_count += 1
            elif recent_highs[i].price < recent_highs[i - 1].price:
                lh_count += 1

        # Check for Higher Lows (bullish) or Lower Lows (bearish)
        hl_count = 0
        ll_count = 0

        for i in range(1, len(recent_lows)):
            if recent_lows[i].price > recent_lows[i - 1].price:
                hl_count += 1
            elif recent_lows[i].price < recent_lows[i - 1].price:
                ll_count += 1

        # Determine trend based on combinations
        # Bullish: majority HH and HL
        if hh_count >= lh_count and hl_count >= ll_count:
            if hh_count > 0 or hl_count > 0:
                return "bullish"

        # Bearish: majority LH and LL
        if lh_count >= hh_count and ll_count >= hl_count:
            if lh_count > 0 or ll_count > 0:
                return "bearish"

        return "ranging"

    def _determine_htf_bias(
        self,
        trend: str,
        bos: BOS | None,
        choch: CHoCH | None,
    ) -> str:
        """Determine HTF trading bias from structure analysis.

        Logic per CONTEXT.md:
        - CHoCH bullish → bias "long"
        - CHoCH bearish → bias "short"
        - No CHoCH → bias follows trend direction

        Args:
            trend: Current market trend
            bos: Last Break of Structure or None
            choch: Last Change of Character or None

        Returns:
            HTF bias: "long", "short", or "neutral"
        """
        # CHoCH takes precedence - it's a stronger signal
        if choch is not None:
            if choch.to_trend == "bullish":
                return "long"
            elif choch.to_trend == "bearish":
                return "short"

        # No CHoCH - follow trend direction
        if trend == "bullish":
            return "long"
        elif trend == "bearish":
            return "short"

        return "neutral"
