"""Tests for OandaClient connector.

Uses pytest and unittest.mock to test OANDA API interactions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from connectors.oanda_client import Candle, OandaClient


@pytest.fixture
def mock_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set mock environment variables for testing."""
    monkeypatch.setenv("OANDA_API_KEY", "test_api_key_12345")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "test_account_001")
    monkeypatch.setenv("OANDA_ENVIRONMENT", "practice")


@pytest.fixture
def oanda_client(mock_env_vars: None) -> OandaClient:
    """Create OandaClient instance with mocked environment."""
    with patch("connectors.oanda_client.API") as mock_api:
        mock_api.return_value = MagicMock()
        return OandaClient()


class TestOandaClientInit:
    """Test OandaClient initialization."""

    def test_init_missing_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that ValueError is raised when OANDA_API_KEY is missing."""
        monkeypatch.delenv("OANDA_API_KEY", raising=False)
        monkeypatch.setenv("OANDA_ACCOUNT_ID", "test_account")

        with patch("connectors.oanda_client.API"):
            with pytest.raises(ValueError, match="OANDA_API_KEY not set"):
                OandaClient()

    def test_init_missing_account_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that ValueError is raised when OANDA_ACCOUNT_ID is missing."""
        monkeypatch.setenv("OANDA_API_KEY", "test_api_key")
        monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

        with patch("connectors.oanda_client.API"):
            with pytest.raises(ValueError, match="OANDA_ACCOUNT_ID not set"):
                OandaClient()

    def test_init_practice_environment(self, mock_env_vars: None) -> None:
        """Test initialization with practice environment."""
        with patch("connectors.oanda_client.API") as mock_api:
            client = OandaClient()
            assert client.api_key == "test_api_key_12345"
            assert client.account_id == "test_account_001"
            mock_api.assert_called_once_with(
                access_token="test_api_key_12345",
                environment="practice",
            )

    def test_init_live_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test initialization with live environment."""
        monkeypatch.setenv("OANDA_API_KEY", "live_api_key")
        monkeypatch.setenv("OANDA_ACCOUNT_ID", "live_account")
        monkeypatch.setenv("OANDA_ENVIRONMENT", "live")

        with patch("connectors.oanda_client.API") as mock_api:
            client = OandaClient()
            assert client.base_url == "https://api-fxtrade.oanda.com"
            mock_api.assert_called_once_with(
                access_token="live_api_key",
                environment="live",
            )


class TestGetCandles:
    """Test get_candles method."""

    def test_get_candles_success(self, oanda_client: OandaClient) -> None:
        """Test successful candle retrieval."""
        mock_response = {
            "candles": [
                {
                    "time": "2024-01-01T00:00:00.000000000Z",
                    "mid": {"o": "1.1000", "h": "1.1100", "l": "1.0900", "c": "1.1050"},
                    "volume": 1000,
                    "complete": True,
                },
                {
                    "time": "2024-01-01T01:00:00.000000000Z",
                    "mid": {"o": "1.1050", "h": "1.1150", "l": "1.1000", "c": "1.1100"},
                    "volume": 1500,
                    "complete": True,
                },
            ]
        }

        oanda_client.client.request = MagicMock(return_value=mock_response)

        candles = oanda_client.get_candles("EUR_USD", "H1", 2)

        assert len(candles) == 2
        assert isinstance(candles[0], Candle)
        assert candles[0].instrument == "EUR_USD"
        assert candles[0].open == 1.1000
        assert candles[0].high == 1.1100
        assert candles[0].low == 1.0900
        assert candles[0].close == 1.1050
        assert candles[0].volume == 1000

    def test_get_candles_skip_incomplete(self, oanda_client: OandaClient) -> None:
        """Test that incomplete candles are skipped."""
        mock_response = {
            "candles": [
                {
                    "time": "2024-01-01T00:00:00.000000000Z",
                    "mid": {"o": "1.1000", "h": "1.1100", "l": "1.0900", "c": "1.1050"},
                    "volume": 1000,
                    "complete": True,
                },
                {
                    "time": "2024-01-01T01:00:00.000000000Z",
                    "mid": {"o": "1.1050", "h": "1.1150", "l": "1.1000", "c": "1.1100"},
                    "volume": 500,
                    "complete": False,  # Incomplete
                },
            ]
        }

        oanda_client.client.request = MagicMock(return_value=mock_response)

        candles = oanda_client.get_candles("EUR_USD", "H1", 2)

        assert len(candles) == 1  # Only 1 complete candle

    def test_get_candles_count_exceeds_limit(self, oanda_client: OandaClient) -> None:
        """Test that ValueError is raised when count exceeds 5000."""
        with pytest.raises(ValueError, match="exceeds OANDA limit"):
            oanda_client.get_candles("EUR_USD", "H1", 5001)

    def test_get_candles_api_error(self, oanda_client: OandaClient) -> None:
        """Test handling of API errors."""
        oanda_client.client.request = MagicMock(
            side_effect=Exception("API connection failed")
        )

        with pytest.raises(Exception, match="API connection failed"):
            oanda_client.get_candles("EUR_USD", "H1", 10)


class TestGetAccountSummary:
    """Test get_account_summary method."""

    def test_get_account_summary_success(self, oanda_client: OandaClient) -> None:
        """Test successful account summary retrieval."""
        mock_response = {
            "account": {
                "id": "test_account",
                "balance": "10000.00",
                "currency": "USD",
                "marginRate": "0.02",
            }
        }

        oanda_client.client.request = MagicMock(return_value=mock_response)

        summary = oanda_client.get_account_summary()

        assert summary["balance"] == "10000.00"
        assert summary["currency"] == "USD"

    def test_get_account_summary_api_error(self, oanda_client: OandaClient) -> None:
        """Test handling of API errors."""
        oanda_client.client.request = MagicMock(
            side_effect=Exception("Account fetch failed")
        )

        with pytest.raises(Exception, match="Account fetch failed"):
            oanda_client.get_account_summary()


class TestGetCurrentSpread:
    """Test get_current_spread method."""

    def test_get_spread_success(self, oanda_client: OandaClient) -> None:
        """Test successful spread retrieval."""
        mock_response = {
            "prices": [
                {
                    "instrument": "EUR_USD",
                    "bid": "1.1000",
                    "ask": "1.1002",
                    "tradeable": True,
                }
            ]
        }

        oanda_client.client.request = MagicMock(return_value=mock_response)

        spread = oanda_client.get_current_spread("EUR_USD")

        assert spread == pytest.approx(0.0002)  # 1.1002 - 1.1000

    def test_get_spread_no_prices(self, oanda_client: OandaClient) -> None:
        """Test handling when no prices returned."""
        mock_response = {"prices": []}

        oanda_client.client.request = MagicMock(return_value=mock_response)

        with pytest.raises(ValueError, match="No price data"):
            oanda_client.get_current_spread("EUR_USD")

    def test_get_spread_api_error(self, oanda_client: OandaClient) -> None:
        """Test handling of API errors."""
        oanda_client.client.request = MagicMock(
            side_effect=Exception("Pricing fetch failed")
        )

        with pytest.raises(Exception, match="Pricing fetch failed"):
            oanda_client.get_current_spread("EUR_USD")


class TestIsMarketOpen:
    """Test is_market_open method."""

    def test_market_open(self, oanda_client: OandaClient) -> None:
        """Test when market is open."""
        mock_response = {
            "prices": [
                {
                    "instrument": "EUR_USD",
                    "bid": "1.1000",
                    "ask": "1.1002",
                    "tradeable": True,
                }
            ]
        }

        oanda_client.client.request = MagicMock(return_value=mock_response)

        is_open = oanda_client.is_market_open("EUR_USD")

        assert is_open is True

    def test_market_closed(self, oanda_client: OandaClient) -> None:
        """Test when market is closed."""
        mock_response = {
            "prices": [
                {
                    "instrument": "EUR_USD",
                    "bid": "1.1000",
                    "ask": "1.1002",
                    "tradeable": False,
                }
            ]
        }

        oanda_client.client.request = MagicMock(return_value=mock_response)

        is_open = oanda_client.is_market_open("EUR_USD")

        assert is_open is False

    def test_market_status_no_prices(self, oanda_client: OandaClient) -> None:
        """Test when no prices returned (market closed/failure)."""
        mock_response = {"prices": []}

        oanda_client.client.request = MagicMock(return_value=mock_response)

        is_open = oanda_client.is_market_open("EUR_USD")

        assert is_open is False

    def test_market_status_api_error(self, oanda_client: OandaClient) -> None:
        """Test graceful handling of API errors."""
        oanda_client.client.request = MagicMock(
            side_effect=Exception("API unavailable")
        )

        is_open = oanda_client.is_market_open("EUR_USD")

        assert is_open is False


class TestCandleDataclass:
    """Test Candle dataclass."""

    def test_candle_creation(self) -> None:
        """Test Candle dataclass creation and immutability."""
        candle = Candle(
            instrument="XAU_USD",
            timestamp=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            open=2000.0,
            high=2010.0,
            low=1995.0,
            close=2005.0,
            volume=5000,
        )

        assert candle.instrument == "XAU_USD"
        assert candle.open == 2000.0
        assert candle.volume == 5000

    def test_candle_frozen(self) -> None:
        """Test that Candle dataclass is frozen (immutable)."""
        candle = Candle(
            instrument="EUR_USD",
            timestamp=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            open=1.0,
            high=1.1,
            low=0.9,
            close=1.05,
            volume=100,
        )

        with pytest.raises(AttributeError):
            candle.close = 1.20  # type: ignore[misc]
