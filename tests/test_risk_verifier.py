"""Tests for agents/risk_verifier.py — 14 tests."""

from __future__ import annotations

from agents.risk_verifier import RiskVerifier, RiskVerifierResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_input(**overrides) -> dict:
    """Base valid input_data for RiskVerifier."""
    data = {
        "instrument": "EUR_USD",
        "direction": "bullish",
        "entry": 1.0850,
        "stop_loss": 1.0800,
        "take_profits": {"tp1": 1.0925, "tp2": 1.0975, "tp3": 1.1050},
        "position_size_lots": 0.05,
        "account_balance": 10000.0,
        "current_spread": 1.2,
        "spread_history": None,
        "open_positions": [],
        "daily_loss_pct": 0.01,
        "confluence_score": 72,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRiskVerifierApproved:
    def test_approved_basic(self):
        verifier = RiskVerifier()
        result = verifier.verify(_base_input())
        assert isinstance(result, RiskVerifierResult)
        assert result.risk_approved is True
        assert result.rejection_reason == ""
        assert result.circuit_breaker_hit == ""
        assert result.portfolio_corr_blocked is False

    def test_rejection_reason_empty_when_approved(self):
        verifier = RiskVerifier()
        result = verifier.verify(_base_input())
        assert result.rejection_reason == ""


class TestCircuitBreakerDailyLoss:
    def test_daily_loss_hard_block(self):
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(daily_loss_pct=0.05))
        assert result.risk_approved is False
        assert result.circuit_breaker_hit == "daily_loss"
        assert "5%" in result.rejection_reason or "≥5%" in result.rejection_reason

    def test_daily_loss_exactly_hard_threshold(self):
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(daily_loss_pct=0.05))
        assert result.risk_approved is False
        assert result.circuit_breaker_hit == "daily_loss"

    def test_daily_loss_soft_warning(self):
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(daily_loss_pct=0.04))
        assert result.risk_approved is True
        assert result.circuit_breaker_hit == ""
        assert any("WARNING" in note for note in result.risk_notes)

    def test_daily_loss_below_threshold_no_warning(self):
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(daily_loss_pct=0.02))
        assert result.risk_approved is True
        assert not any("WARNING" in note and "Daily" in note for note in result.risk_notes)


class TestCircuitBreakerMaxPositions:
    def test_max_positions_block(self):
        positions = [
            {"instrument": "XAU_USD", "direction": "bullish", "lots": 0.02},
            {"instrument": "BTC_USD", "direction": "bullish", "lots": 0.01},
            {"instrument": "XAU_USD", "direction": "bearish", "lots": 0.01},
        ]
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(open_positions=positions))
        assert result.risk_approved is False
        assert result.circuit_breaker_hit == "max_positions"

    def test_two_positions_allowed(self):
        positions = [
            {"instrument": "XAU_USD", "direction": "bullish", "lots": 0.02},
            {"instrument": "BTC_USD", "direction": "bearish", "lots": 0.01},
        ]
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(open_positions=positions))
        assert result.risk_approved is True
        assert result.circuit_breaker_hit == ""


class TestPortfolioCorrelation:
    def test_correlation_same_instrument_same_direction_blocked(self):
        positions = [{"instrument": "EUR_USD", "direction": "bullish", "lots": 0.05}]
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(
            instrument="EUR_USD", direction="bullish", open_positions=positions
        ))
        assert result.risk_approved is False
        assert result.portfolio_corr_blocked is True

    def test_correlation_same_instrument_opposite_direction_allowed(self):
        positions = [{"instrument": "EUR_USD", "direction": "bearish", "lots": 0.05}]
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(
            instrument="EUR_USD", direction="bullish", open_positions=positions
        ))
        assert result.risk_approved is True
        assert result.portfolio_corr_blocked is False

    def test_correlation_different_instruments_below_threshold(self):
        # EUR_USD ↔ XAU_USD = 0.40, below 0.60 threshold
        positions = [{"instrument": "XAU_USD", "direction": "bullish", "lots": 0.02}]
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(
            instrument="EUR_USD", direction="bullish", open_positions=positions
        ))
        assert result.risk_approved is True
        assert result.portfolio_corr_blocked is False


class TestSpreadZScore:
    def test_spread_zscore_computed(self):
        history = [1.0, 1.1, 1.0, 1.2, 1.0, 1.1, 1.0, 1.2, 1.0, 1.1,
                   1.0, 1.1, 1.0, 1.2, 1.0, 1.1, 1.0, 1.2, 1.0, 1.1]
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(spread_history=history, current_spread=1.1))
        assert result.spread_z_score is not None

    def test_spread_zscore_none_without_history(self):
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(spread_history=None))
        assert result.spread_z_score is None

    def test_spread_high_zscore_warning(self):
        # History with variance so std > 0; current_spread far above mean → z > 2.0
        history = [1.0, 1.1, 1.0, 1.2, 1.0, 1.1, 1.0, 1.2, 1.0, 1.1,
                   1.0, 1.1, 1.0, 1.2, 1.0, 1.1, 1.0, 1.2, 1.0, 1.1]
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(spread_history=history, current_spread=10.0))
        assert result.spread_z_score is not None
        assert result.spread_z_score > 2.0
        assert any("WARNING" in note and "z-score" in note for note in result.risk_notes)
        # Spread z-score should NOT block approval
        assert result.risk_approved is True


class TestSizingValidation:
    def test_sizing_scaled_down(self):
        # lots=5.0, entry=1.085, sl=1.080 → sl_pips=50 → risk=5*10*50=2500 → 25% of 10000
        # Exceeds 2% → should be scaled down
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(position_size_lots=5.0))
        assert result.position_size < 5.0
        assert any("ALERT" in note for note in result.risk_notes)

    def test_sizing_within_limit(self):
        # 0.05 lots, sl_pips=50 → risk=0.05*10*50=25 → 0.25% of 10000 — well within 2%
        verifier = RiskVerifier()
        result = verifier.verify(_base_input(position_size_lots=0.05))
        assert result.position_size == 0.05
        assert not any("ALERT" in note for note in result.risk_notes)


class TestRobustness:
    def test_verify_never_raises_on_bad_input(self):
        verifier = RiskVerifier()
        result = verifier.verify({})  # wszystkie klucze brakują
        assert isinstance(result, RiskVerifierResult)
        # Should not raise — returns approved or rejected, never exception

    def test_verify_never_raises_on_none_values(self):
        verifier = RiskVerifier()
        result = verifier.verify({"instrument": None, "entry": None, "stop_loss": None})
        assert isinstance(result, RiskVerifierResult)
