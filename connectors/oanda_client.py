"""OANDA REST API v20 Client for SMC Signal Bot.

Connector for EUR/USD and XAU/USD OHLCV data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from dotenv import load_dotenv
from oandapyV20 import API
from oandapyV20.endpoints import accounts, instruments, pricing

# Load environment variables
load_dotenv()

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Candle:
    """OHLCV candle data from OANDA.

    Matches CONTEXT.md spec for Feature Store compatibility.
    """

    instrument: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class OandaClient:
    """OANDA REST API v20 client for SMC Signal Bot.

    Handles EUR/USD, XAU/USD data retrieval and account operations.
    Configured via environment variables from .env file.
    """

    def __init__(self) -> None:
        """Initialize OANDA API client with environment configuration."""
        self.api_key = os.getenv("OANDA_API_KEY")
        self.account_id = os.getenv("OANDA_ACCOUNT_ID")
        env = os.getenv("OANDA_ENVIRONMENT", "practice")

        if not self.api_key:
            raise ValueError("OANDA_API_KEY not set in environment")
        if not self.account_id:
            raise ValueError("OANDA_ACCOUNT_ID not set in environment")

        # Determine API endpoint
        if env == "live":
            self.base_url = "https://api-fxtrade.oanda.com"
        else:
            self.base_url = "https://api-fxpractice.oanda.com"
            env = "practice"

        self.client = API(access_token=self.api_key, environment=env)
        self.logger = logger.bind(
            module="oanda_client",
            account_id=self.account_id[:8] + "...",
            environment=env,
        )
        self.logger.info("oanda_client_initialized")

    def get_candles(
        self,
        instrument: str,
        granularity: str,
        count: int,
    ) -> list[Candle]:
        """Retrieve OHLCV candles from OANDA.

        Args:
            instrument: OANDA instrument name (e.g., "EUR_USD", "XAU_USD")
            granularity: Candle timeframe (e.g., "H1", "H4", "D")
            count: Number of candles to retrieve (max 5000)

        Returns:
            List of Candle dataclass instances

        Raises:
            ValueError: If count exceeds 5000 or granularity invalid
            APIError: On OANDA API failure
        """
        if count > 5000:
            raise ValueError(f"Count {count} exceeds OANDA limit of 5000")

        self.logger.info(
            "fetching_candles",
            instrument=instrument,
            granularity=granularity,
            count=count,
        )

        params = {"granularity": granularity, "count": count, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=instrument, params=params)

        try:
            response = self.client.request(r)
            candles_data = response.get("candles", [])
        except Exception as e:
            self.logger.error(
                "candles_fetch_failed",
                instrument=instrument,
                error=str(e),
            )
            raise

        candles: list[Candle] = []
        for candle in candles_data:
            if not candle.get("complete"):
                continue  # Skip incomplete candles

            mid = candle.get("mid", {})
            ts = datetime.fromisoformat(
                candle["time"].replace("Z", "+00:00")
            )

            candles.append(
                Candle(
                    instrument=instrument,
                    timestamp=ts,
                    open=float(mid["o"]),
                    high=float(mid["h"]),
                    low=float(mid["l"]),
                    close=float(mid["c"]),
                    volume=int(candle.get("volume", 0)),
                )
            )

        self.logger.info(
            "candles_fetched",
            instrument=instrument,
            count=len(candles),
        )
        return candles

    def get_account_summary(self) -> dict[str, Any]:
        """Retrieve account summary from OANDA.

        Returns:
            Dictionary with account details (balance, margin, etc.)

        Raises:
            APIError: On OANDA API failure
        """
        self.logger.info("fetching_account_summary")

        r = accounts.AccountSummary(self.account_id)

        try:
            response = self.client.request(r)
            summary = response.get("account", {})
        except Exception as e:
            self.logger.error("account_summary_failed", error=str(e))
            raise

        self.logger.info(
            "account_summary_fetched",
            balance=summary.get("balance"),
            currency=summary.get("currency"),
        )
        return summary

    def get_current_spread(self, instrument: str) -> float:
        """Get current spread (bid/ask difference) for instrument.

        Args:
            instrument: OANDA instrument name (e.g., "EUR_USD")

        Returns:
            Spread as float (in price terms, not pips)

        Raises:
            APIError: On OANDA API failure
        """
        self.logger.info("fetching_spread", instrument=instrument)

        params = {"instruments": instrument}
        r = pricing.PricingInfo(self.account_id, params=params)

        try:
            response = self.client.request(r)
            prices = response.get("prices", [])
            if not prices:
                raise ValueError(f"No price data for {instrument}")

            price = prices[0]
            bid = float(price["bid"])
            ask = float(price["ask"])
            spread = ask - bid
        except Exception as e:
            self.logger.error(
                "spread_fetch_failed",
                instrument=instrument,
                error=str(e),
            )
            raise

        self.logger.info(
            "spread_fetched",
            instrument=instrument,
            spread=spread,
            bid=bid,
            ask=ask,
        )
        return spread

    def is_market_open(self, instrument: str) -> bool:
        """Check if market is currently open for trading.

        Args:
            instrument: OANDA instrument name (e.g., "EUR_USD")

        Returns:
            True if market is open, False otherwise
        """
        self.logger.info("checking_market_status", instrument=instrument)

        params = {"instruments": instrument}
        r = pricing.PricingInfo(self.account_id, params=params)

        try:
            response = self.client.request(r)
            prices = response.get("prices", [])
            if not prices:
                return False

            price = prices[0]
            # Tradeable flag indicates if market is open
            is_open = price.get("tradeable", False)
        except Exception as e:
            self.logger.warning(
                "market_status_check_failed",
                instrument=instrument,
                error=str(e),
            )
            return False

        self.logger.info(
            "market_status_checked",
            instrument=instrument,
            is_open=is_open,
        )
        return is_open
