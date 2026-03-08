"""Confluence Scorer — core scoring engine for SMC Signal Bot.

Orchestrates all SMC detectors and scores a setup on a 0-110 scale.
Score >= 65 → signal published to Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

from smc.fvg_detector import FairValueGapDetector
from smc.liquidity_detector import LiquidityDetector
from smc.ob_detector import OrderBlockDetector
from smc.structure_analyzer import StructureAnalyzer
from smc.swing_detector import SwingDetector

if TYPE_CHECKING:
    from connectors.oanda_client import Candle
    from smc.fvg_detector import FVG
    from smc.liquidity_detector import LiquiditySweep
    from smc.ob_detector import OrderBlock
    from smc.structure_analyzer import StructureResult
    from smc.swing_detector import SwingResult

logger = structlog.get_logger(__name__)

_SIGNAL_THRESHOLD = 65
_MAX_SCORE = 110
_MIN_CANDLES = 14
_IPDA_RANGE_BARS = 20
_AVG_VOLUME_BARS = 20
_ABSORPTION_LOOKBACK = 3

# GROK-1: absorption = small body (indecision/absorption candle) + high volume
# body_ratio = abs(close - open) / (high - low) — SMALL ratio = absorption
_ABSORPTION_BODY_RATIO_MAX: dict[str, float] = {
    "EUR_USD": 0.30,
    "XAU_USD": 0.30,
    "BTC_USD": 0.25,  # stricter for BTC
}
_ABSORPTION_VOLUME_SPIKE_MIN: dict[str, float] = {
    "EUR_USD": 1.5,
    "XAU_USD": 1.5,
    "BTC_USD": 2.0,  # stricter for BTC
}
_ABSORPTION_BODY_RATIO_MAX_DEFAULT = 0.30
_ABSORPTION_VOLUME_SPIKE_MIN_DEFAULT = 1.5

SESSIONS: dict[str, tuple[int, int]] = {
    "overlap": (12, 16),
    "london": (7, 16),
    "new_york": (12, 21),
    "asian": (0, 7),
}


@dataclass(frozen=True)
class ScoreComponent:
    """Score contribution from a single confluence factor.

    Attributes:
        name: Human-readable name of the component
        score: Points awarded (0 to max_score)
        max_score: Maximum possible for this component
        reason: Short description of why this score was given
        details: Optional extra data (do NOT mutate after construction)
    """

    name: str
    score: int
    max_score: int
    reason: str
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConfluenceResult:
    """Result of a full confluence scoring pass.

    Attributes:
        total_score: Sum of all components (0-110)
        max_possible: Always 110
        threshold: Publication threshold (65)
        is_signal: True when total_score >= threshold
        setup_direction: "bullish" | "bearish" | "neutral"
        components: Breakdown list of all 8 ScoreComponent objects
        timestamp: UTC time of scoring
        pair: Instrument name e.g. "EUR_USD"
        reason: Non-empty only when score=0 (explains why)
    """

    total_score: int
    max_possible: int
    threshold: int
    is_signal: bool
    setup_direction: str
    components: list[ScoreComponent]
    timestamp: datetime
    pair: str
    reason: str = ""


class ConfluenceScorer:
    """Orchestrates all SMC detectors and produces a confluence score.

    Score breakdown (max 110 pts):
        IPDA Zone           25 pts
        OB Quality          20 pts
        FVG Present         15 pts
        Liquidity Sweep     15 pts
        Session Timing      10 pts
        Structure (BOS/CHoCH) 10 pts
        Absorption [GROK-1] 10 pts
        HTF Bias             5 pts
    """

    def __init__(self) -> None:
        """Initialise with one instance of each detector."""
        self.swing_detector = SwingDetector()
        self.structure_analyzer = StructureAnalyzer()
        self.ob_detector = OrderBlockDetector()
        self.fvg_detector = FairValueGapDetector()
        self.liquidity_detector = LiquidityDetector()
        self.logger = logger.bind(module="confluence_scorer")

    def score(
        self,
        candles: list[Candle],
        htf_candles: list[Candle] | None = None,
        pair: str = "EUR_USD",
    ) -> ConfluenceResult:
        """Run a full confluence scoring pass on the provided candles.

        Args:
            candles: LTF candle list (chronological, most recent last)
            htf_candles: Optional HTF candles for bias alignment
            pair: Instrument identifier, e.g. "EUR_USD"

        Returns:
            ConfluenceResult with total score and component breakdown.
        """
        now = datetime.now(tz=timezone.utc)

        if len(candles) < _MIN_CANDLES:
            self.logger.warning(
                "confluence_scorer.insufficient_candles",
                count=len(candles),
                required=_MIN_CANDLES,
            )
            return ConfluenceResult(
                total_score=0,
                max_possible=_MAX_SCORE,
                threshold=_SIGNAL_THRESHOLD,
                is_signal=False,
                setup_direction="neutral",
                components=[],
                timestamp=now,
                pair=pair,
                reason="Insufficient data",
            )

        swings: SwingResult = self.swing_detector.detect(candles)

        if not swings.highs and not swings.lows:
            self.logger.warning("confluence_scorer.no_swings", pair=pair)
            return ConfluenceResult(
                total_score=0,
                max_possible=_MAX_SCORE,
                threshold=_SIGNAL_THRESHOLD,
                is_signal=False,
                setup_direction="neutral",
                components=[],
                timestamp=now,
                pair=pair,
                reason="No swing points detected",
            )

        structure: StructureResult = self.structure_analyzer.analyze(swings)
        order_blocks: list[OrderBlock] = self.ob_detector.detect(candles, swings)
        fvgs: list[FVG] = self.fvg_detector.detect(candles, structure)
        sweeps: list[LiquiditySweep] = self.liquidity_detector.detect(candles, swings)

        htf_structure: StructureResult | None = None
        if htf_candles and len(htf_candles) >= _MIN_CANDLES:
            htf_swings = self.swing_detector.detect(htf_candles)
            if htf_swings.highs or htf_swings.lows:
                htf_structure = self.structure_analyzer.analyze(htf_swings)

        setup_direction = self._determine_setup_direction(structure, sweeps)

        if setup_direction == "neutral":
            return ConfluenceResult(
                total_score=0,
                max_possible=_MAX_SCORE,
                threshold=_SIGNAL_THRESHOLD,
                is_signal=False,
                setup_direction="neutral",
                components=[],
                timestamp=now,
                pair=pair,
                reason="No clear setup direction",
            )

        components: list[ScoreComponent] = [
            self._score_ipda_zone(candles, pair),
            self._score_ob_quality(order_blocks, setup_direction),
            self._score_fvg(fvgs, setup_direction),
            self._score_liquidity_sweep(sweeps, setup_direction),
            self._score_session(pair),
            self._score_structure(structure),
            self._score_absorption(candles, pair),
            self._score_htf_bias(htf_structure, setup_direction),
        ]

        total = sum(c.score for c in components)

        self.logger.info(
            "confluence_scoring_complete",
            pair=pair,
            total_score=total,
            setup_direction=setup_direction,
            is_signal=total >= _SIGNAL_THRESHOLD,
        )

        return ConfluenceResult(
            total_score=total,
            max_possible=_MAX_SCORE,
            threshold=_SIGNAL_THRESHOLD,
            is_signal=total >= _SIGNAL_THRESHOLD,
            setup_direction=setup_direction,
            components=components,
            timestamp=now,
            pair=pair,
        )

    def _score_ipda_zone(
        self,
        candles: list[Candle],
        pair: str,
    ) -> ScoreComponent:
        """Score IPDA dealing range position — max 25 pts.

        Uses the last 20 candles as the dealing range.
        Deep discount (≤20%) or deep premium (≥80%) → 25 pts.
        Equilibrium (40-60%) → 0 pts (no edge).
        """
        name = "IPDA Zone"
        range_candles = candles[-_IPDA_RANGE_BARS:]
        range_high = max(c.high for c in range_candles)
        range_low = min(c.low for c in range_candles)
        current_price = candles[-1].close

        span = range_high - range_low
        if span <= 0:
            return ScoreComponent(
                name=name,
                score=0,
                max_score=25,
                reason="Flat dealing range — no edge",
                details={"position": 0.5},
            )

        position = (current_price - range_low) / span

        if position <= 0.20:
            pts, reason = 25, "Deep discount zone (≤20%)"
        elif position <= 0.40:
            pts, reason = 20, "Discount zone (20-40%)"
        elif position >= 0.80:
            pts, reason = 25, "Deep premium zone (≥80%)"
        elif position >= 0.60:
            pts, reason = 20, "Premium zone (60-80%)"
        else:
            pts, reason = 0, "Equilibrium (40-60%) — no edge"

        return ScoreComponent(
            name=name,
            score=pts,
            max_score=25,
            reason=reason,
            details={"position": round(position, 4)},
        )

    def _score_ob_quality(
        self,
        order_blocks: list[OrderBlock],
        setup_direction: str,
    ) -> ScoreComponent:
        """Score Order Block quality — max 20 pts.

        Looks for the best valid OB aligned with setup_direction.
        """
        name = "OB Quality"

        ob_type = setup_direction
        matching = [
            ob for ob in order_blocks
            if ob.ob_type == ob_type and ob.is_valid
        ]

        if not matching:
            return ScoreComponent(
                name=name,
                score=0,
                max_score=20,
                reason=f"No {ob_type} OB found",
            )

        best = matching[0]

        if best.quality and best.quality.passed:
            return ScoreComponent(
                name=name,
                score=20,
                max_score=20,
                reason="Valid OB — quality passed",
                details={
                    "ob_age_bars": best.quality.ob_age_bars,
                    "ob_touches": best.quality.ob_touches,
                },
            )

        return ScoreComponent(
            name=name,
            score=10,
            max_score=20,
            reason="OB present but quality check failed",
        )

    def _score_fvg(
        self,
        fvgs: list[FVG],
        setup_direction: str,
    ) -> ScoreComponent:
        """Score Fair Value Gap — max 15 pts.

        Looks for a valid FVG aligned with setup_direction.
        Fresh (fill < 0.3) → 15 pts, partially filled (0.3-0.5) → 10 pts,
        quality failed → 5 pts, absent → 0 pts.
        """
        name = "FVG Present"

        matching = [
            f for f in fvgs
            if f.fvg_type == setup_direction and f.is_valid
        ]

        if not matching:
            return ScoreComponent(
                name=name,
                score=0,
                max_score=15,
                reason=f"No {setup_direction} FVG found",
            )

        best = matching[0]

        if best.quality is None or not best.quality.passed:
            return ScoreComponent(
                name=name,
                score=5,
                max_score=15,
                reason="FVG present but quality check failed",
            )

        fill = best.quality.fill_percentage

        if fill < 0.3:
            return ScoreComponent(
                name=name,
                score=15,
                max_score=15,
                reason="Fresh FVG (fill < 30%)",
                details={"fill_percentage": round(fill, 4)},
            )

        if fill <= 0.5:
            return ScoreComponent(
                name=name,
                score=10,
                max_score=15,
                reason="Partially filled FVG (30-50%)",
                details={"fill_percentage": round(fill, 4)},
            )

        return ScoreComponent(
            name=name,
            score=5,
            max_score=15,
            reason="FVG mostly filled (>50%) — weak",
            details={"fill_percentage": round(fill, 4)},
        )

    def _score_liquidity_sweep(
        self,
        sweeps: list[LiquiditySweep],
        setup_direction: str,
    ) -> ScoreComponent:
        """Score Liquidity Sweep — max 15 pts.

        Bullish setup → sellside sweep; bearish setup → buyside sweep.
        """
        name = "Liquidity Sweep"

        expected_sweep_type = "sellside" if setup_direction == "bullish" else "buyside"
        matching = [
            s for s in sweeps
            if s.sweep_type == expected_sweep_type and s.is_valid
        ]

        if not matching:
            return ScoreComponent(
                name=name,
                score=0,
                max_score=15,
                reason=f"No {expected_sweep_type} sweep found",
            )

        best = matching[0]

        if best.quality and best.quality.passed:
            return ScoreComponent(
                name=name,
                score=15,
                max_score=15,
                reason="Fresh liquidity sweep — quality passed",
                details={"sweep_age_bars": best.quality.sweep_age_bars},
            )

        return ScoreComponent(
            name=name,
            score=7,
            max_score=15,
            reason="Sweep present but quality check failed",
        )

    def _score_session(self, pair: str) -> ScoreComponent:
        """Score session timing — max 10 pts.

        London/NY overlap (12-16 UTC) → 10 pts.
        London (07-12) or NY (16-21) → 8 pts.
        Asian (00-07) → 3 pts (5 pts for BTC_USD).
        Per-pair adjustments: XAU_USD NY session = 10 pts.
        """
        name = "Session Timing"
        now_utc = datetime.now(tz=timezone.utc)
        hour = now_utc.hour

        overlap_start, overlap_end = SESSIONS["overlap"]
        london_start, london_end = SESSIONS["london"]
        ny_start, ny_end = SESSIONS["new_york"]
        asian_start, asian_end = SESSIONS["asian"]

        in_overlap = overlap_start <= hour < overlap_end
        in_london_only = london_start <= hour < overlap_start
        in_ny_only = overlap_end <= hour < ny_end
        in_asian = asian_start <= hour < asian_end

        if in_overlap:
            pts, reason = 10, "London/NY overlap (12-16 UTC)"
        elif in_london_only:
            pts, reason = 8, "London session (07-12 UTC)"
        elif in_ny_only:
            pts, reason = 8, "NY session (16-21 UTC)"
            if pair == "XAU_USD":
                pts, reason = 10, "NY session — gold most active (XAU_USD)"
        elif in_asian:
            pts = 5 if pair == "BTC_USD" else 3
            reason = "Asian session — reduced liquidity"
        else:
            pts, reason = 3, "Off-hours session"

        return ScoreComponent(
            name=name,
            score=pts,
            max_score=10,
            reason=reason,
            details={"utc_hour": hour},
        )

    def _score_structure(self, structure: StructureResult) -> ScoreComponent:
        """Score market structure confirmation — max 10 pts.

        BOS confirmed in setup direction → 10 pts.
        CHoCH detected → 7 pts.
        Neither → 0 pts.
        """
        name = "BOS/CHoCH Confirmed"

        if structure.last_bos is not None:
            return ScoreComponent(
                name=name,
                score=10,
                max_score=10,
                reason=f"BOS confirmed ({structure.last_bos.direction})",
            )

        if structure.last_choch is not None:
            return ScoreComponent(
                name=name,
                score=7,
                max_score=10,
                reason=f"CHoCH detected ({structure.last_choch.to_trend})",
            )

        return ScoreComponent(
            name=name,
            score=0,
            max_score=10,
            reason="No BOS or CHoCH detected",
        )

    def _score_absorption(
        self, candles: list[Candle], pair: str = "EUR_USD"
    ) -> ScoreComponent:
        """Score absorption detection [GROK-1] — max 10 pts.

        Checks last 3 candles:
        - Each candle: body_ratio = abs(close - open) / (high - low) ≤ 0.30
          (small body = institution absorbing supply/demand)
        - Last candle: volume_spike = volume / avg_volume(20) > 1.5 (Forex)
          or > 2.0 (BTC)

        All conditions met → 10 pts; otherwise → 0 pts.
        Per-instrument thresholds per GROK-1 spec.
        """
        name = "Absorption [GROK-1]"

        if len(candles) < _ABSORPTION_LOOKBACK + _AVG_VOLUME_BARS:
            return ScoreComponent(
                name=name,
                score=0,
                max_score=10,
                reason="Insufficient candles for absorption check",
            )

        body_max = _ABSORPTION_BODY_RATIO_MAX.get(pair, _ABSORPTION_BODY_RATIO_MAX_DEFAULT)
        vol_min = _ABSORPTION_VOLUME_SPIKE_MIN.get(pair, _ABSORPTION_VOLUME_SPIKE_MIN_DEFAULT)

        last_three = candles[-_ABSORPTION_LOOKBACK:]

        for candle in last_three:
            hl_range = candle.high - candle.low
            if hl_range <= 0:
                return ScoreComponent(
                    name=name,
                    score=0,
                    max_score=10,
                    reason="Zero-range candle — absorption not detected",
                )
            body_ratio = abs(candle.close - candle.open) / hl_range
            if body_ratio > body_max:
                return ScoreComponent(
                    name=name,
                    score=0,
                    max_score=10,
                    reason=f"Body ratio {body_ratio:.2f} > {body_max} threshold (not absorption)",
                )

        avg_volume_candles = candles[-(
            _ABSORPTION_LOOKBACK + _AVG_VOLUME_BARS
        ):-_ABSORPTION_LOOKBACK]
        total_vol = sum(c.volume for c in avg_volume_candles)
        avg_volume = total_vol / len(avg_volume_candles) if avg_volume_candles else 0

        last_candle = candles[-1]
        if avg_volume <= 0:
            self.logger.info(
                "confluence_scorer.absorption_zero_avg_volume",
                note="tick_volume_proxy_unreliable",
            )
            return ScoreComponent(
                name=name,
                score=0,
                max_score=10,
                reason="Average volume is zero — cannot compute volume spike",
            )

        volume_spike = last_candle.volume / avg_volume

        if volume_spike > vol_min:
            return ScoreComponent(
                name=name,
                score=10,
                max_score=10,
                reason=f"Absorption detected — body_ratio OK, vol_spike={volume_spike:.2f}x",
                details={"volume_spike": round(volume_spike, 3)},
            )

        return ScoreComponent(
            name=name,
            score=0,
            max_score=10,
            reason=f"Volume spike {volume_spike:.2f}x < {vol_min}x threshold",
        )

    def _score_htf_bias(
        self,
        htf_structure: StructureResult | None,
        setup_direction: str,
    ) -> ScoreComponent:
        """Score HTF bias alignment — max 5 pts.

        HTF trend same direction as setup → 5 pts.
        Opposite or absent → 0 pts.
        """
        name = "HTF Bias Aligned"

        if htf_structure is None:
            return ScoreComponent(
                name=name,
                score=0,
                max_score=5,
                reason="No HTF data provided",
            )

        htf_direction = htf_structure.trend

        htf_matches = (
            (setup_direction == "bullish" and htf_direction == "bullish")
            or (setup_direction == "bearish" and htf_direction == "bearish")
        )

        if htf_matches:
            return ScoreComponent(
                name=name,
                score=5,
                max_score=5,
                reason=f"HTF trend ({htf_direction}) aligned with setup ({setup_direction})",
            )

        return ScoreComponent(
            name=name,
            score=0,
            max_score=5,
            reason=f"HTF trend ({htf_direction}) not aligned with setup ({setup_direction})",
        )

    def _determine_setup_direction(
        self,
        structure: StructureResult,
        sweeps: list[LiquiditySweep],
    ) -> str:
        """Determine setup direction from structure and sweep context.

        Priority (strongest signal wins):
        1. Fresh sellside sweep → bullish (SM buys after sell stops collected)
        2. Fresh buyside sweep  → bearish (SM sells after buy stops collected)
        3. CHoCH detected       → opposite of previous trend
        4. BOS confirmed        → follow trend direction
        5. Fallback             → structure.trend

        Returns "bullish", "bearish", or "neutral".
        """
        valid_sweeps = [s for s in sweeps if s.is_valid]

        fresh_sellside = next(
            (s for s in valid_sweeps if s.sweep_type == "sellside"), None
        )
        if fresh_sellside:
            return "bullish"

        fresh_buyside = next(
            (s for s in valid_sweeps if s.sweep_type == "buyside"), None
        )
        if fresh_buyside:
            return "bearish"

        if structure.last_choch is not None:
            return structure.last_choch.to_trend

        if structure.last_bos is not None:
            return structure.last_bos.direction

        if structure.trend == "bullish":
            return "bullish"
        if structure.trend == "bearish":
            return "bearish"

        return "neutral"
