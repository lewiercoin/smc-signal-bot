"""Risk Engine — position sizing, SL/TP calculation, spread filter.

Last layer before signal generation. Answers: where to put SL/TP,
how large the position, and whether spread allows entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

from smc.utils import calculate_atr_series

if TYPE_CHECKING:
    from connectors.oanda_client import Candle
    from smc.swing_detector import SwingResult

logger = structlog.get_logger(__name__)

# ── Per-pair configuration ────────────────────────────────────────────────────

MIN_SL_DISTANCE: dict[str, float] = {
    "EUR_USD": 0.0010,  # 10 pips minimum
    "XAU_USD": 1.0,     # $1 minimum
    "BTC_USD": 50.0,    # $50 minimum
}

MAX_SPREADS: dict[str, float] = {
    "EUR_USD": 2.0,   # 2.0 pips max
    "XAU_USD": 30.0,  # 30 pips (cents) max
    "BTC_USD": 50.0,  # $50 max
}

# TP reward ratios per instrument: (TP1_R, TP2_R, TP3_R)
REWARD_RATIOS: dict[str, tuple[float, float, float]] = {
    "EUR_USD": (1.5, 2.5, 3.5),
    "XAU_USD": (1.5, 2.5, 3.5),
    "BTC_USD": (1.5, 2.5, 5.5),  # BTC: TP3 higher (greater volatility)
}

PIP_VALUES: dict[str, dict[str, float]] = {
    "EUR_USD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "XAU_USD": {"pip_size": 0.01,   "pip_value_per_lot": 1.0},
    "BTC_USD": {"pip_size": 1.0,    "pip_value_per_lot": 1.0},
}

_ATR_PERIOD = 14
_ATR_FALLBACK_MULTIPLIER = 2.0
_ATR_BUFFER_MULTIPLIER = 0.5
_MIN_LOT_SIZE = 0.01
_MAX_LOT_SIZE = 10.0


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TakeProfitLevels:
    """Three TP levels based on risk:reward ratios."""

    tp1: float
    tp2: float
    tp3: float
    ratios: tuple[float, float, float]  # (1.5, 2.5, 3.5) or BTC variant


@dataclass(frozen=True)
class PositionSize:
    """Position sizing result."""

    lots: float
    risk_amount: float  # $ risked
    risk_pct: float     # % of account risked
    sl_pips: float      # SL distance in pips


@dataclass(frozen=True)
class SpreadCheck:
    """Spread filter result."""

    passed: bool
    current_spread: float | None
    max_allowed: float
    reason: str = ""


@dataclass(frozen=True)
class TradeParameters:
    """Complete trade parameters returned by RiskEngine."""

    pair: str
    direction: str              # "bullish" | "bearish"
    entry: float
    stop_loss: float
    take_profits: TakeProfitLevels
    position_size: PositionSize
    spread_check: SpreadCheck
    risk_reward_ratio: float    # TP1 / risk distance
    sl_distance: float          # absolute |entry - SL|
    atr_at_entry: float
    is_valid: bool
    rejection_reason: str = ""
    timestamp: datetime | None = field(default=None)


# ── RiskEngine ────────────────────────────────────────────────────────────────


class RiskEngine:
    """Calculates SL, TP levels, position size and validates trade viability.

    Implements ATR-buffered SL placement (no fixed pips), R-based TP levels,
    2% max risk position sizing, and per-pair spread filtering.
    """

    def __init__(
        self,
        max_risk_pct: float = 0.02,
        default_balance: float = 10000.0,
    ) -> None:
        """Initialise RiskEngine.

        Args:
            max_risk_pct: Maximum fraction of account to risk per trade (default 2%).
            default_balance: Fallback account balance when none is provided.
        """
        self.max_risk_pct = max_risk_pct
        self.default_balance = default_balance
        self.logger = logger.bind(module="risk_engine")

    # ── Public interface ──────────────────────────────────────────────────────

    def calculate_trade(
        self,
        candles: list[Candle],
        setup_direction: str,
        pair: str,
        account_balance: float | None = None,
        swings: SwingResult | None = None,
        current_spread: float | None = None,
    ) -> TradeParameters | None:
        """Orchestrate full trade parameter calculation.

        Args:
            candles: OHLCV candle list (minimum 14 candles).
            setup_direction: "bullish" or "bearish".
            pair: Instrument key e.g. "EUR_USD".
            account_balance: Account equity; uses default_balance if None.
            swings: Pre-computed SwingResult; calculates SL fallback if None.
            current_spread: Current spread in pip units; skips spread check if None.

        Returns:
            TradeParameters if trade is viable, None if a critical error occurs.
        """
        if len(candles) < _ATR_PERIOD:
            self.logger.warning(
                "insufficient_candles_for_risk_engine",
                count=len(candles),
                required=_ATR_PERIOD,
            )
            return None

        balance = account_balance if account_balance is not None else self.default_balance

        # 1. Entry price — last candle close (signal-time price)
        entry = candles[-1].close

        # 2. ATR at entry (last valid value in series)
        atr_at_entry = self._get_last_atr(candles)
        if atr_at_entry == 0.0:
            self.logger.warning("atr_is_zero", pair=pair, candle_count=len(candles))
            return None

        # 3. Stop Loss
        stop_loss = self._calculate_stop_loss(candles, setup_direction, swings)

        # 4. Enforce minimum SL distance
        sl_distance = abs(entry - stop_loss)
        min_sl = MIN_SL_DISTANCE.get(pair, 0.0)
        if sl_distance < min_sl:
            self.logger.info(
                "sl_distance_below_minimum_enforced",
                pair=pair,
                calculated_sl_distance=sl_distance,
                minimum=min_sl,
            )
            sl_distance = min_sl
            if setup_direction == "bullish":
                stop_loss = entry - min_sl
            else:
                stop_loss = entry + min_sl

        # 5. Take Profit levels
        take_profits = self._calculate_take_profits(entry, stop_loss, setup_direction, pair)

        # 6. Spread filter
        spread_check = self._check_spread(pair, current_spread)

        # 7. Position size
        position_size = self._calculate_position_size(balance, entry, stop_loss, pair)

        # 8. Risk:reward ratio (TP1 / risk distance)
        risk_distance = abs(entry - stop_loss)
        tp1_distance = abs(take_profits.tp1 - entry)
        risk_reward_ratio = tp1_distance / risk_distance if risk_distance > 0 else 0.0

        # 9. Validate
        is_valid, rejection_reason = self._validate_trade(
            entry, stop_loss, take_profits, position_size, spread_check, pair
        )

        params = TradeParameters(
            pair=pair,
            direction=setup_direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profits=take_profits,
            position_size=position_size,
            spread_check=spread_check,
            risk_reward_ratio=risk_reward_ratio,
            sl_distance=sl_distance,
            atr_at_entry=atr_at_entry,
            is_valid=is_valid,
            rejection_reason=rejection_reason,
            timestamp=datetime.now(tz=timezone.utc),
        )

        self.logger.info(
            "trade_calculated",
            pair=pair,
            direction=setup_direction,
            entry=entry,
            stop_loss=stop_loss,
            sl_distance=sl_distance,
            risk_reward_ratio=risk_reward_ratio,
            lots=position_size.lots,
            is_valid=is_valid,
            rejection_reason=rejection_reason or None,
        )

        return params

    # ── SL calculation ────────────────────────────────────────────────────────

    def _calculate_stop_loss(
        self,
        candles: list[Candle],
        setup_direction: str,
        swings: SwingResult | None = None,
    ) -> float:
        """Calculate SL based on swing structure + ATR buffer.

        Bullish: nearest swing low below entry - 0.5 ATR
        Bearish: nearest swing high above entry + 0.5 ATR
        Fallback (no swings): entry ± 2.0 ATR

        Args:
            candles: OHLCV candle list.
            setup_direction: "bullish" or "bearish".
            swings: Pre-computed SwingResult from SwingDetector.

        Returns:
            Stop loss price level.
        """
        entry = candles[-1].close
        atr = self._get_last_atr(candles)
        buffer = _ATR_BUFFER_MULTIPLIER * atr

        if swings is None or (not swings.lows and not swings.highs):
            self.logger.warning(
                "no_swings_available_using_atr_fallback",
                direction=setup_direction,
                atr=atr,
            )
            if setup_direction == "bullish":
                return entry - (_ATR_FALLBACK_MULTIPLIER * atr)
            return entry + (_ATR_FALLBACK_MULTIPLIER * atr)

        if setup_direction == "bullish":
            # Find nearest swing low below entry
            lows_below = [sp for sp in swings.lows if sp.price < entry]
            if not lows_below:
                self.logger.warning(
                    "no_swing_low_below_entry_using_fallback",
                    entry=entry,
                    swing_lows=[sp.price for sp in swings.lows],
                )
                return entry - (_ATR_FALLBACK_MULTIPLIER * atr)
            nearest_low = max(lows_below, key=lambda sp: sp.price)
            return nearest_low.price - buffer

        # Bearish: find nearest swing high above entry
        highs_above = [sp for sp in swings.highs if sp.price > entry]
        if not highs_above:
            self.logger.warning(
                "no_swing_high_above_entry_using_fallback",
                entry=entry,
                swing_highs=[sp.price for sp in swings.highs],
            )
            return entry + (_ATR_FALLBACK_MULTIPLIER * atr)
        nearest_high = min(highs_above, key=lambda sp: sp.price)
        return nearest_high.price + buffer

    # ── TP calculation ────────────────────────────────────────────────────────

    def _calculate_take_profits(
        self,
        entry: float,
        sl: float,
        setup_direction: str,
        pair: str,
    ) -> TakeProfitLevels:
        """Calculate three TP levels based on R multiples.

        Args:
            entry: Entry price.
            sl: Stop loss price.
            setup_direction: "bullish" or "bearish".
            pair: Instrument key for reward ratio lookup.

        Returns:
            TakeProfitLevels with tp1, tp2, tp3, ratios.
        """
        ratios = REWARD_RATIOS.get(pair, (1.5, 2.5, 3.5))
        r1, r2, r3 = ratios

        if setup_direction == "bullish":
            risk = entry - sl
            tp1 = entry + risk * r1
            tp2 = entry + risk * r2
            tp3 = entry + risk * r3
        else:
            risk = sl - entry
            tp1 = entry - risk * r1
            tp2 = entry - risk * r2
            tp3 = entry - risk * r3

        return TakeProfitLevels(tp1=tp1, tp2=tp2, tp3=tp3, ratios=ratios)

    # ── Position sizing ───────────────────────────────────────────────────────

    def _calculate_position_size(
        self,
        account_balance: float,
        entry: float,
        sl: float,
        pair: str,
    ) -> PositionSize:
        """Calculate position size respecting 2% max risk.

        Args:
            account_balance: Account equity in USD.
            entry: Entry price.
            sl: Stop loss price.
            pair: Instrument key for pip value lookup.

        Returns:
            PositionSize with lots, risk_amount, risk_pct, sl_pips.
        """
        risk_amount = account_balance * self.max_risk_pct
        sl_distance = abs(entry - sl)

        pip_config = PIP_VALUES.get(pair, {"pip_size": 0.0001, "pip_value_per_lot": 10.0})
        pip_size = pip_config["pip_size"]
        pip_value_per_lot = pip_config["pip_value_per_lot"]

        sl_pips = sl_distance / pip_size

        if sl_pips == 0:
            self.logger.warning("sl_pips_is_zero_clamping_to_min", pair=pair)
            lots = _MIN_LOT_SIZE
        else:
            risk_per_lot = sl_pips * pip_value_per_lot
            lots = risk_amount / risk_per_lot

        # Clamp to broker limits
        lots = max(_MIN_LOT_SIZE, min(_MAX_LOT_SIZE, lots))
        lots = round(lots, 2)

        risk_pct = self.max_risk_pct

        return PositionSize(
            lots=lots,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            sl_pips=sl_pips,
        )

    # ── Spread filter ─────────────────────────────────────────────────────────

    def _check_spread(
        self,
        pair: str,
        current_spread: float | None = None,
    ) -> SpreadCheck:
        """Check whether current spread is within acceptable limits.

        Args:
            pair: Instrument key.
            current_spread: Current spread in price units (ask - bid), as returned
                by OandaClient.get_current_spread(). None = no data available.

        Returns:
            SpreadCheck with passed flag, spread data (in original price units),
            and reason string.
        """
        max_allowed = MAX_SPREADS.get(pair, 5.0)

        if current_spread is None:
            self.logger.warning("spread_data_unavailable_assuming_ok", pair=pair)
            return SpreadCheck(
                passed=True,
                current_spread=None,
                max_allowed=max_allowed,
                reason="No spread data available — assuming acceptable",
            )

        # Convert price units → pips before comparing against MAX_SPREADS (pip limits).
        # OandaClient.get_current_spread() returns ask - bid in price terms.
        # Example: EUR/USD 0.00012 price = 0.00012 / 0.0001 = 1.2 pips.
        pip_size = PIP_VALUES.get(pair, {"pip_size": 0.0001})["pip_size"]
        spread_in_pips = current_spread / pip_size

        if spread_in_pips > max_allowed:
            reason = (
                f"Spread {spread_in_pips:.1f} pips (raw: {current_spread}) "
                f"exceeds max {max_allowed:.1f} for {pair}"
            )
            self.logger.info(
                "spread_rejected",
                pair=pair,
                spread_pips=spread_in_pips,
                spread_price=current_spread,
                max_pips=max_allowed,
            )
            return SpreadCheck(
                passed=False,
                current_spread=current_spread,
                max_allowed=max_allowed,
                reason=reason,
            )

        return SpreadCheck(
            passed=True,
            current_spread=current_spread,
            max_allowed=max_allowed,
            reason="",
        )

    # ── Final validation ──────────────────────────────────────────────────────

    def _validate_trade(
        self,
        entry: float,
        sl: float,
        tp_levels: TakeProfitLevels,
        position_size: PositionSize,
        spread_check: SpreadCheck,
        pair: str,
    ) -> tuple[bool, str]:
        """Final validation gate — reject if any hard criterion fails.

        Args:
            entry: Entry price.
            sl: Stop loss price.
            tp_levels: Calculated TP levels.
            position_size: Calculated position size.
            spread_check: Spread filter result.
            pair: Instrument key for minimum distance lookup.

        Returns:
            Tuple of (is_valid, rejection_reason).
        """
        sl_distance = abs(entry - sl)

        if not spread_check.passed:
            return False, "Spread too wide"

        risk_distance = abs(entry - sl)
        tp1_distance = abs(tp_levels.tp1 - entry)
        rr = tp1_distance / risk_distance if risk_distance > 0 else 0.0
        if rr < 1.0:
            return False, "Risk:reward below minimum"

        if position_size.lots < _MIN_LOT_SIZE:
            return False, "Position too small"

        if position_size.lots > _MAX_LOT_SIZE:
            return False, "Position too large"

        min_sl = MIN_SL_DISTANCE.get(pair, 0.0)
        if sl_distance < min_sl:
            return False, "SL too close"

        return True, ""

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_last_atr(self, candles: list[Candle]) -> float:
        """Return the most recent valid ATR value from the candle series.

        Args:
            candles: OHLCV candle list.

        Returns:
            Last non-None ATR value, or 0.0 if series has no valid values.
        """
        atr_series = calculate_atr_series(candles, _ATR_PERIOD)
        for value in reversed(atr_series):
            if value is not None:
                return value
        return 0.0
