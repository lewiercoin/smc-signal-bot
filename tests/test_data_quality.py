"""Tests for Data Quality module.

Tests DQResult, NewsCheckResult dataclasses and DataQualityChecker class.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from connectors.oanda_client import Candle
from dq.data_quality import (
    DQResult,
    DataQualityChecker,
    NewsCheckResult,
)


@pytest.fixture
def checker() -> DataQualityChecker:
    """Create DataQualityChecker instance."""
    return DataQualityChecker()


@pytest.fixture
def valid_candles_100() -> list[Candle]:
    """Create 100 valid candles for testing."""
    candles = []
    base_time = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

    for i in range(100):
        ts = base_time + timedelta(hours=i)
        candles.append(
            Candle(
                instrument="EUR_USD",
                timestamp=ts,
                open=1.1000 + (i * 0.0001),
                high=1.1010 + (i * 0.0001),
                low=1.0990 + (i * 0.0001),
                close=1.1005 + (i * 0.0001),
                volume=1000 + i,
            )
        )
    return candles


class TestDQResult:
    """Test DQResult dataclass."""

    def test_dqresult_creation(self) -> None:
        """Test DQResult creation."""
        result = DQResult(
            passed=True,
            issues=[],
            candles_valid=100,
            candles_total=100,
        )
        assert result.passed is True
        assert result.candles_valid == 100

    def test_dqresult_frozen(self) -> None:
        """Test DQResult is immutable."""
        result = DQResult(
            passed=True,
            issues=[],
            candles_valid=100,
            candles_total=100,
        )
        with pytest.raises(AttributeError):
            result.passed = False  # type: ignore[misc]


class TestNewsCheckResult:
    """Test NewsCheckResult dataclass."""

    def test_newscheck_clear(self) -> None:
        """Test clear news result."""
        result = NewsCheckResult(blocked=False)
        assert result.blocked is False
        assert result.reason is None
        assert result.next_clear is None

    def test_newscheck_blocked(self) -> None:
        """Test blocked news result."""
        clear_time = datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc)
        result = NewsCheckResult(
            blocked=True,
            reason="HIGH impact USD news",
            next_clear=clear_time,
        )
        assert result.blocked is True
        assert result.reason == "HIGH impact USD news"
        assert result.next_clear == clear_time


class TestCheckCandles:
    """Test check_candles method."""

    def test_valid_candles_pass(self, checker: DataQualityChecker, valid_candles_100: list[Candle]) -> None:
        """Test 100 valid candles pass all checks."""
        result = checker.check_candles(valid_candles_100, granularity="H1")

        assert result.passed is True
        assert len(result.issues) == 0
        assert result.candles_valid == 100
        assert result.candles_total == 100

    def test_insufficient_candles(self, checker: DataQualityChecker, valid_candles_100: list[Candle]) -> None:
        """Test fails with less than 100 candles."""
        candles = valid_candles_100[:50]
        result = checker.check_candles(candles, granularity="H1")

        assert result.passed is False
        assert any("Insufficient candles" in issue for issue in result.issues)

    def test_empty_candles(self, checker: DataQualityChecker) -> None:
        """Test empty candles list."""
        result = checker.check_candles([], granularity="H1")

        assert result.passed is False
        assert result.candles_valid == 0
        assert result.candles_total == 0

    def test_gap_detection(self, checker: DataQualityChecker) -> None:
        """Test detection of gaps > 2x granularity."""
        candles = []
        base_time = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

        # Create 50 valid candles
        for i in range(50):
            ts = base_time + timedelta(hours=i)
            candles.append(
                Candle(
                    instrument="EUR_USD",
                    timestamp=ts,
                    open=1.1000,
                    high=1.1010,
                    low=1.0990,
                    close=1.1005,
                    volume=1000,
                )
            )

        # Add gap: skip 10 hours (should be every 1 hour)
        for i in range(50, 100):
            ts = base_time + timedelta(hours=i + 10)  # +10 hour gap
            candles.append(
                Candle(
                    instrument="EUR_USD",
                    timestamp=ts,
                    open=1.1000,
                    high=1.1010,
                    low=1.0990,
                    close=1.1005,
                    volume=1000,
                )
            )

        result = checker.check_candles(candles, granularity="H1")

        assert result.passed is False
        assert any("Gap detected" in issue for issue in result.issues)

    def test_ohlc_integrity_high_violation(self, checker: DataQualityChecker, valid_candles_100: list[Candle]) -> None:
        """Test OHLC integrity: high < max(open, close)."""
        candles = valid_candles_100.copy()
        # Violate: high < close
        candles[50] = Candle(
            instrument="EUR_USD",
            timestamp=candles[50].timestamp,
            open=1.1000,
            high=1.1005,  # Should be >= close of 1.1010
            low=1.0990,
            close=1.1010,
            volume=1000,
        )

        result = checker.check_candles(candles, granularity="H1")

        assert result.passed is False
        assert any("High" in issue for issue in result.issues)

    def test_ohlc_integrity_low_violation(self, checker: DataQualityChecker, valid_candles_100: list[Candle]) -> None:
        """Test OHLC integrity: low > min(open, close)."""
        candles = valid_candles_100.copy()
        # Violate: low > open
        candles[50] = Candle(
            instrument="EUR_USD",
            timestamp=candles[50].timestamp,
            open=1.1000,
            high=1.1010,
            low=1.1005,  # Should be <= open of 1.1000
            close=1.0990,
            volume=1000,
        )

        result = checker.check_candles(candles, granularity="H1")

        assert result.passed is False
        assert any("Low" in issue for issue in result.issues)

    def test_ohlc_integrity_high_low_cross(self, checker: DataQualityChecker, valid_candles_100: list[Candle]) -> None:
        """Test OHLC integrity: high < low."""
        candles = valid_candles_100.copy()
        candles[50] = Candle(
            instrument="EUR_USD",
            timestamp=candles[50].timestamp,
            open=1.1000,
            high=1.0990,  # Invalid: high < low
            low=1.1010,
            close=1.1005,
            volume=1000,
        )

        result = checker.check_candles(candles, granularity="H1")

        assert result.passed is False
        assert any("High" in issue and "Low" in issue for issue in result.issues)

    def test_negative_volume(self, checker: DataQualityChecker, valid_candles_100: list[Candle]) -> None:
        """Test detection of negative volume."""
        candles = valid_candles_100.copy()
        candles[50] = Candle(
            instrument="EUR_USD",
            timestamp=candles[50].timestamp,
            open=1.1000,
            high=1.1010,
            low=1.0990,
            close=1.1005,
            volume=-100,  # Invalid
        )

        result = checker.check_candles(candles, granularity="H1")

        assert result.passed is False
        assert any("Negative volume" in issue for issue in result.issues)


class TestCheckSpread:
    """Test check_spread method."""

    def test_eur_usd_acceptable(self, checker: DataQualityChecker) -> None:
        """Test EUR/USD spread within limit."""
        # 1.5 pips = 0.00015 price
        result = checker.check_spread(0.00015, "EUR_USD")
        assert result is True

    def test_eur_usd_at_limit(self, checker: DataQualityChecker) -> None:
        """Test EUR/USD spread at exact limit."""
        # 2.0 pips = 0.0002 price
        result = checker.check_spread(0.0002, "EUR_USD")
        assert result is True

    def test_eur_usd_exceeds_limit(self, checker: DataQualityChecker) -> None:
        """Test EUR/USD spread exceeds limit."""
        # 2.5 pips = 0.00025 price
        result = checker.check_spread(0.00025, "EUR_USD")
        assert result is False

    def test_xau_usd_acceptable(self, checker: DataQualityChecker) -> None:
        """Test XAU/USD spread within limit."""
        # 25 pips = 0.25 price
        result = checker.check_spread(0.25, "XAU_USD")
        assert result is True

    def test_xau_usd_exceeds_limit(self, checker: DataQualityChecker) -> None:
        """Test XAU/USD spread exceeds limit."""
        # 35 pips = 0.35 price
        result = checker.check_spread(0.35, "XAU_USD")
        assert result is False

    def test_btc_usd_acceptable(self, checker: DataQualityChecker) -> None:
        """Test BTC/USD spread within limit."""
        # 40 pips = 40 price
        result = checker.check_spread(40.0, "BTC_USD")
        assert result is True

    def test_btc_usd_exceeds_limit(self, checker: DataQualityChecker) -> None:
        """Test BTC/USD spread exceeds limit."""
        # 60 pips = 60 price
        result = checker.check_spread(60.0, "BTC_USD")
        assert result is False


class TestCheckNewsWindow:
    """Test check_news_window method."""

    def test_no_news_clear(self, checker: DataQualityChecker) -> None:
        """Test clear when no news events."""
        current_time = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

        result = checker.check_news_window("EUR_USD", current_time)

        assert result.blocked is False
        assert result.reason is None
        assert result.next_clear is None

    def test_high_impact_usd_blocked(self, checker: DataQualityChecker) -> None:
        """Test blocked during HIGH impact USD news window."""
        news_time = datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc)
        current_time = datetime(2024, 1, 1, 14, 30, tzinfo=timezone.utc)  # +30 min

        checker.set_mock_news_calendar([
            {
                "time": news_time,
                "currency": "USD",
                "impact": "HIGH",
                "name": "NFP",
            }
        ])

        result = checker.check_news_window("EUR_USD", current_time)

        assert result.blocked is True
        assert result.reason is not None
        assert "NFP" in result.reason or "USD" in result.reason
        assert result.next_clear is not None

    def test_high_impact_eur_blocked(self, checker: DataQualityChecker) -> None:
        """Test blocked during HIGH impact EUR news window."""
        news_time = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        current_time = datetime(2024, 1, 1, 10, 30, tzinfo=timezone.utc)

        checker.set_mock_news_calendar([
            {
                "time": news_time,
                "currency": "EUR",
                "impact": "HIGH",
                "name": "ECB Rate",
            }
        ])

        result = checker.check_news_window("EUR_USD", current_time)

        assert result.blocked is True

    def test_medium_impact_not_blocked(self, checker: DataQualityChecker) -> None:
        """Test not blocked for MEDIUM impact news."""
        news_time = datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc)
        current_time = datetime(2024, 1, 1, 14, 30, tzinfo=timezone.utc)

        checker.set_mock_news_calendar([
            {
                "time": news_time,
                "currency": "USD",
                "impact": "MEDIUM",
                "name": "Some Event",
            }
        ])

        result = checker.check_news_window("EUR_USD", current_time)

        assert result.blocked is False

    def test_outside_window_not_blocked(self, checker: DataQualityChecker) -> None:
        """Test not blocked when outside ±120min window."""
        news_time = datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc)
        current_time = datetime(2024, 1, 1, 16, 30, tzinfo=timezone.utc)  # +2.5 hours

        checker.set_mock_news_calendar([
            {
                "time": news_time,
                "currency": "USD",
                "impact": "HIGH",
                "name": "NFP",
            }
        ])

        result = checker.check_news_window("EUR_USD", current_time)

        assert result.blocked is False

    def test_xau_ignores_eur_news(self, checker: DataQualityChecker) -> None:
        """Test XAU/USD ignores EUR-specific news."""
        news_time = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        current_time = datetime(2024, 1, 1, 10, 30, tzinfo=timezone.utc)

        checker.set_mock_news_calendar([
            {
                "time": news_time,
                "currency": "EUR",  # EUR news
                "impact": "HIGH",
                "name": "ECB Rate",
            }
        ])

        # XAU/USD only checks USD, so EUR news should not block
        result = checker.check_news_window("XAU_USD", current_time)

        assert result.blocked is False
