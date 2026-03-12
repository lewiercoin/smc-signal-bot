"""Shared fixtures for integration tests.

Provides realistic market data and mock external dependencies.
All data fixtures use random.seed(42) for reproducibility.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from connectors.oanda_client import Candle
from db.database import Database


# ── Realistic candle generators ───────────────────────────────────────────────


def _build_candles(
    pair: str,
    base_price: float,
    typical_range: float,
    drift_scale: float,
    count: int = 100,
) -> list[Candle]:
    """Build realistic OHLCV candles with trending phases.

    Phases:
      0–19   uptrend
      20–29  consolidation
      30–34  sharp impulse down (triggers OB + FVG)
      35–49  bounce with sweep potential
      50–99  continuation up
    """
    random.seed(42)
    candles: list[Candle] = []
    price = base_price
    base_time = datetime.now(timezone.utc) - timedelta(hours=count)
    noise_sigma = typical_range * 0.20

    for i in range(count):
        if i < 20:
            drift = drift_scale * 0.0003 / 0.0003 * (typical_range * 0.20)
        elif i < 30:
            drift = 0.0
        elif i < 35:
            # Sharp impulse down: > 1.5× ATR per candle
            drift = -(typical_range * 0.55)
        elif i < 50:
            drift = typical_range * 0.27
        else:
            drift = typical_range * 0.13

        open_price = price
        close_price = open_price + drift + random.gauss(0, noise_sigma)
        high = max(open_price, close_price) + abs(random.gauss(0, noise_sigma))
        low = min(open_price, close_price) - abs(random.gauss(0, noise_sigma))

        # FVG seed: candle 36 creates gap above candle 34 low for bullish FVG
        if i == 36:
            low = max(low, open_price)  # force low above open → gap potential

        volume = random.randint(100, 500)

        candles.append(
            Candle(
                instrument=pair,
                timestamp=base_time + timedelta(hours=i),
                open=round(open_price, 5),
                high=round(high, 5),
                low=round(low, 5),
                close=round(close_price, 5),
                volume=volume,
            )
        )
        price = close_price

    return candles


@pytest.fixture
def realistic_eur_usd_candles() -> list[Candle]:
    """100 H1 EUR/USD candles with realistic SMC structure."""
    return _build_candles(
        pair="EUR_USD",
        base_price=1.0850,
        typical_range=0.0015,
        drift_scale=0.0003,
        count=100,
    )


@pytest.fixture
def realistic_xau_usd_candles() -> list[Candle]:
    """100 H1 XAU/USD candles with realistic SMC structure."""
    return _build_candles(
        pair="XAU_USD",
        base_price=1950.0,
        typical_range=5.0,
        drift_scale=1.0,
        count=100,
    )


@pytest.fixture
def realistic_btc_usd_candles() -> list[Candle]:
    """100 H1 BTC/USD candles with realistic SMC structure."""
    return _build_candles(
        pair="BTC_USD",
        base_price=42000.0,
        typical_range=500.0,
        drift_scale=100.0,
        count=100,
    )


@pytest.fixture
def flat_candles() -> list[Candle]:
    """100 candles with near-zero drift — flat market, low confluence."""
    random.seed(42)
    candles: list[Candle] = []
    price = 1.0850
    base_time = datetime.now(timezone.utc) - timedelta(hours=100)

    for i in range(100):
        open_price = price
        close_price = open_price + random.gauss(0, 0.00005)
        high = max(open_price, close_price) + abs(random.gauss(0, 0.00003))
        low = min(open_price, close_price) - abs(random.gauss(0, 0.00003))

        candles.append(
            Candle(
                instrument="EUR_USD",
                timestamp=base_time + timedelta(hours=i),
                open=round(open_price, 5),
                high=round(high, 5),
                low=round(low, 5),
                close=round(close_price, 5),
                volume=random.randint(50, 150),
            )
        )
        price = close_price

    return candles


# ── Mock external dependencies ────────────────────────────────────────────────


@pytest.fixture
def mock_oanda_client(realistic_eur_usd_candles: list[Candle]) -> MagicMock:
    """OandaClient mock returning EUR/USD candles by default."""
    mock = MagicMock()
    mock.get_candles.return_value = realistic_eur_usd_candles
    mock.get_current_spread.return_value = 1.2
    mock.get_account_summary.return_value = {"balance": 10000.0}
    mock.is_market_open.return_value = True
    return mock


@pytest.fixture
def mock_news_client() -> MagicMock:
    """NewsClient mock — no news blackout by default."""
    mock = MagicMock()
    result = MagicMock()
    result.is_blocked = False
    result.reason = ""
    mock.is_news_blocked.return_value = result
    return mock


@pytest.fixture
def mock_news_client_blocked() -> MagicMock:
    """NewsClient mock — news IS blocked."""
    mock = MagicMock()
    result = MagicMock()
    result.is_blocked = True
    result.reason = "NFP release in 45 minutes"
    mock.is_news_blocked.return_value = result
    return mock


@pytest.fixture
def mock_telegram() -> AsyncMock:
    """Mock Telegram bot application."""
    mock = AsyncMock()
    mock.send_message = AsyncMock(return_value=MagicMock(message_id=12345))
    return mock


# ── Real in-memory DB ─────────────────────────────────────────────────────────


@pytest.fixture
def in_memory_db() -> Database:
    """Real Database instance backed by SQLite :memory:."""
    db = Database(":memory:")
    db.initialize()
    yield db
    db.close()
