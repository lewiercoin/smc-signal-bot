"""Tests for engine/risk_engine.py — 14 tests covering SL, TP, sizing, spread, validation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from connectors.oanda_client import Candle
from engine.risk_engine import (
    MAX_SPREADS,
    MIN_SL_DISTANCE,
    REWARD_RATIOS,
    PositionSize,
    RiskEngine,
    SpreadCheck,
    TakeProfitLevels,
    TradeParameters,
)
from smc.swing_detector import SwingPoint, SwingResult


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_candle(
    close: float,
    high: float | None = None,
    low: float | None = None,
    open_: float | None = None,
    instrument: str = "EUR_USD",
    idx: int = 0,
) -> Candle:
    """Build a Candle with sensible defaults."""
    c = close
    return Candle(
        instrument=instrument,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=open_ if open_ is not None else c,
        high=high if high is not None else c + 0.0005,
        low=low if low is not None else c - 0.0005,
        close=c,
        volume=1000,
    )


def _make_candles(
    n: int = 30,
    base_price: float = 1.1000,
    instrument: str = "EUR_USD",
    atr_size: float = 0.0010,
) -> list[Candle]:
    """Generate `n` candles with controlled, uniform ATR (~atr_size)."""
    candles = []
    for i in range(n):
        close = base_price + i * 0.0001
        candles.append(
            Candle(
                instrument=instrument,
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                open=close - atr_size * 0.3,
                high=close + atr_size * 0.5,
                low=close - atr_size * 0.5,
                close=close,
                volume=1000,
            )
        )
    return candles


def _swing_point(price: float, swing_type: str, index: int = 5) -> SwingPoint:
    return SwingPoint(
        index=index,
        price=price,
        time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        swing_type=swing_type,
        strength=5,
    )


def _make_swing_result(
    lows: list[float] | None = None,
    highs: list[float] | None = None,
) -> SwingResult:
    return SwingResult(
        highs=[_swing_point(p, "high") for p in (highs or [])],
        lows=[_swing_point(p, "low") for p in (lows or [])],
        swing_length_used=10,
        volatility_regime="normal",
    )


# ── Stop Loss tests ───────────────────────────────────────────────────────────


class TestStopLoss:
    def test_sl_bullish_below_swing_low_with_buffer(self) -> None:
        """SL = nearest swing low below entry − 0.5 × ATR."""
        engine = RiskEngine()
        candles = _make_candles(30, base_price=1.1000)
        entry = candles[-1].close  # ~1.1029

        swing_low_price = entry - 0.0030  # below entry
        swings = _make_swing_result(lows=[swing_low_price])

        atr = engine._get_last_atr(candles)
        expected_sl = swing_low_price - 0.5 * atr

        sl = engine._calculate_stop_loss(candles, "bullish", swings)

        assert abs(sl - expected_sl) < 1e-9

    def test_sl_bearish_above_swing_high_with_buffer(self) -> None:
        """SL = nearest swing high above entry + 0.5 × ATR."""
        engine = RiskEngine()
        candles = _make_candles(30, base_price=1.1000)
        entry = candles[-1].close

        swing_high_price = entry + 0.0030  # above entry
        swings = _make_swing_result(highs=[swing_high_price])

        atr = engine._get_last_atr(candles)
        expected_sl = swing_high_price + 0.5 * atr

        sl = engine._calculate_stop_loss(candles, "bearish", swings)

        assert abs(sl - expected_sl) < 1e-9

    def test_sl_fallback_without_swings(self) -> None:
        """When swings=None, SL = entry ± 2.0 × ATR."""
        engine = RiskEngine()
        candles = _make_candles(30, base_price=1.1000)
        entry = candles[-1].close
        atr = engine._get_last_atr(candles)

        sl_bull = engine._calculate_stop_loss(candles, "bullish", None)
        sl_bear = engine._calculate_stop_loss(candles, "bearish", None)

        assert abs(sl_bull - (entry - 2.0 * atr)) < 1e-9
        assert abs(sl_bear - (entry + 2.0 * atr)) < 1e-9

    def test_sl_minimum_distance_enforced(self) -> None:
        """If calculated SL distance < minimum, minimum is applied."""
        engine = RiskEngine()
        # Use tiny ATR so swing-based SL would be < 10 pips minimum
        candles = _make_candles(30, base_price=1.1000, atr_size=0.00001)
        entry = candles[-1].close

        # Place swing low just 1 pip below entry (< 10-pip minimum)
        swing_low = entry - 0.0001
        swings = _make_swing_result(lows=[swing_low])

        result = engine.calculate_trade(
            candles, "bullish", "EUR_USD", swings=swings
        )

        assert result is not None
        assert result.sl_distance >= MIN_SL_DISTANCE["EUR_USD"]


# ── Take Profit tests ─────────────────────────────────────────────────────────


class TestTakeProfits:
    def test_tp_bullish_three_levels(self) -> None:
        """Bullish TP1=1.5R, TP2=2.5R, TP3=3.5R for EUR_USD."""
        engine = RiskEngine()
        entry = 1.1000
        sl = 1.0980  # 20-pip SL → risk = 0.0020

        tps = engine._calculate_take_profits(entry, sl, "bullish", "EUR_USD")

        risk = entry - sl
        assert abs(tps.tp1 - (entry + risk * 1.5)) < 1e-9
        assert abs(tps.tp2 - (entry + risk * 2.5)) < 1e-9
        assert abs(tps.tp3 - (entry + risk * 3.5)) < 1e-9
        assert tps.ratios == (1.5, 2.5, 3.5)

    def test_tp_bearish_three_levels(self) -> None:
        """Bearish TPs are below entry."""
        engine = RiskEngine()
        entry = 1.1000
        sl = 1.1020  # SL above entry (short trade)

        tps = engine._calculate_take_profits(entry, sl, "bearish", "EUR_USD")

        risk = sl - entry
        assert abs(tps.tp1 - (entry - risk * 1.5)) < 1e-9
        assert abs(tps.tp2 - (entry - risk * 2.5)) < 1e-9
        assert abs(tps.tp3 - (entry - risk * 3.5)) < 1e-9
        assert tps.tp1 < entry
        assert tps.tp2 < tps.tp1
        assert tps.tp3 < tps.tp2

    def test_tp_btc_higher_tp3(self) -> None:
        """BTC TP3 ratio = 5.5R (not 3.5R)."""
        engine = RiskEngine()
        entry = 50000.0
        sl = 49500.0  # $500 SL

        tps = engine._calculate_take_profits(entry, sl, "bullish", "BTC_USD")

        risk = entry - sl
        assert abs(tps.tp3 - (entry + risk * 5.5)) < 1e-6
        assert tps.ratios == REWARD_RATIOS["BTC_USD"]
        assert tps.ratios[2] == 5.5


# ── Position sizing tests ─────────────────────────────────────────────────────


class TestPositionSizing:
    def test_position_size_2pct_risk(self) -> None:
        """$10000 account, 2% risk, 20-pip SL → $200 risk, correct lots.

        Manual: risk=$200, SL=20 pips, pip_value=$10/lot
        → risk_per_lot = 20 * 10 = $200 → lots = 200/200 = 1.00
        """
        engine = RiskEngine(max_risk_pct=0.02, default_balance=10000.0)
        entry = 1.1000
        sl = 1.0980  # 20 pips @ 0.0001 pip_size

        ps = engine._calculate_position_size(10000.0, entry, sl, "EUR_USD")

        assert ps.risk_amount == pytest.approx(200.0)
        assert ps.lots == pytest.approx(1.00)
        assert ps.sl_pips == pytest.approx(20.0)
        assert ps.risk_pct == pytest.approx(0.02)

    def test_position_size_min_lots(self) -> None:
        """Very large SL would give < 0.01 lots — should clamp to 0.01."""
        engine = RiskEngine(max_risk_pct=0.02, default_balance=10000.0)
        # For EUR_USD: lots = 200 / (sl_pips * 10)
        # To get lots < 0.01: sl_pips > 200/(0.01*10) = 2000 pips → SL = 0.2000
        entry = 1.1000
        sl = 0.9000  # 2000 pips SL

        ps = engine._calculate_position_size(10000.0, entry, sl, "EUR_USD")

        assert ps.lots == pytest.approx(_min_lot())

    def test_position_size_max_lots(self) -> None:
        """Very small SL would give > 10.0 lots — should clamp to 10.0."""
        engine = RiskEngine(max_risk_pct=0.02, default_balance=10000.0)
        # For EUR_USD: lots = 200 / (sl_pips * 10)
        # To get lots > 10: sl_pips < 200/(10*10) = 2 pips → SL = 0.0002
        entry = 1.1000
        # 1-pip SL → lots = 200/(1*10) = 20 → clamp to 10
        sl_one_pip = entry - 0.0001

        ps = engine._calculate_position_size(10000.0, entry, sl_one_pip, "EUR_USD")

        assert ps.lots == pytest.approx(_max_lot())


def _min_lot() -> float:
    from engine.risk_engine import _MIN_LOT_SIZE
    return _MIN_LOT_SIZE


def _max_lot() -> float:
    from engine.risk_engine import _MAX_LOT_SIZE
    return _MAX_LOT_SIZE


# ── Spread filter tests ───────────────────────────────────────────────────────


class TestSpreadFilter:
    def test_spread_passed(self) -> None:
        """Spread below max → passed=True."""
        engine = RiskEngine()
        # 1.5 pips EUR/USD = 1.5 × 0.0001 = 0.00015 price units
        result = engine._check_spread("EUR_USD", current_spread=0.00015)

        assert result.passed is True
        assert result.current_spread == 0.00015
        assert result.max_allowed == MAX_SPREADS["EUR_USD"]
        assert result.reason == ""

    def test_spread_rejected(self) -> None:
        """Spread above max → passed=False with reason."""
        engine = RiskEngine()
        # 3.0 pips EUR/USD = 3.0 × 0.0001 = 0.00030 price units (above 2-pip limit)
        result = engine._check_spread("EUR_USD", current_spread=0.00030)

        assert result.passed is False
        assert result.current_spread == 0.00030
        # Reason shows pips: "3.0 pips ... exceeds max 2.0"
        assert "3.0" in result.reason
        assert "2.0" in result.reason


# ── Validation / Integration tests ───────────────────────────────────────────


class TestValidationAndIntegration:
    def test_trade_rejected_bad_rr(self) -> None:
        """When TP1 is closer than SL distance (R:R < 1.0), trade is rejected."""
        engine = RiskEngine()
        # Manufacture a case: entry=1.1000, SL=1.0950 (50 pip risk),
        # TP1 will be entry + 1.5R = 1.1000 + 0.0075 = 1.1075 → R:R=1.5 normally.
        # Force R:R < 1.0 by overriding _validate_trade directly with crafted values.
        entry = 1.1000
        sl = 1.0950
        # Fake TPs where tp1 is only 0.5R away (bad)
        bad_tps = TakeProfitLevels(
            tp1=entry + 0.0025,  # 0.5R → R:R = 0.5 < 1.0
            tp2=entry + 0.0075,
            tp3=entry + 0.0175,
            ratios=(1.5, 2.5, 3.5),
        )
        ps = PositionSize(lots=1.0, risk_amount=200.0, risk_pct=0.02, sl_pips=50.0)
        spread_ok = SpreadCheck(passed=True, current_spread=1.0, max_allowed=2.0)

        valid, reason = engine._validate_trade(entry, sl, bad_tps, ps, spread_ok, "EUR_USD")

        assert valid is False
        assert "reward" in reason.lower()

    def test_full_trade_calculation(self) -> None:
        """Full flow: candles → TradeParameters with all fields correctly populated."""
        engine = RiskEngine(max_risk_pct=0.02, default_balance=10000.0)

        candles = _make_candles(40, base_price=1.1000, atr_size=0.0010)
        entry = candles[-1].close  # ~1.1039

        # Swing low 30 pips below entry, swing high 30 pips above
        swing_low = entry - 0.0030
        swing_high = entry + 0.0050
        swings = _make_swing_result(lows=[swing_low], highs=[swing_high])

        result = engine.calculate_trade(
            candles=candles,
            setup_direction="bullish",
            pair="EUR_USD",
            account_balance=10000.0,
            swings=swings,
            current_spread=0.00010,  # 1.0 pip × 0.0001 price units
        )

        assert result is not None
        assert isinstance(result, TradeParameters)

        # Direction and pair
        assert result.pair == "EUR_USD"
        assert result.direction == "bullish"
        assert result.entry == pytest.approx(entry)

        # SL below entry for bullish
        assert result.stop_loss < result.entry

        # TPs above entry for bullish
        assert result.take_profits.tp1 > result.entry
        assert result.take_profits.tp2 > result.take_profits.tp1
        assert result.take_profits.tp3 > result.take_profits.tp2

        # Position size in valid range
        assert 0.01 <= result.position_size.lots <= 10.0
        assert result.position_size.risk_amount == pytest.approx(200.0)

        # Risk:reward at least 1.5 (ratio for TP1)
        assert result.risk_reward_ratio >= 1.0

        # Spread passed
        assert result.spread_check.passed is True

        # Valid trade
        assert result.is_valid is True
        assert result.rejection_reason == ""

        # ATR populated
        assert result.atr_at_entry > 0.0

        # Timestamp set
        assert result.timestamp is not None
