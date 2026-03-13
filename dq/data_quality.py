"""Data Quality module for SMC Signal Bot.

Validates candles, spreads, and news windows before SMC processing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from connectors.oanda_client import Candle

if TYPE_CHECKING:
    from connectors.news_client import NewsClient

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DQResult:
    """Data Quality check result for candles.

    Attributes:
        passed: True if all checks passed
        issues: List of issues found
        candles_valid: Number of valid candles
        candles_total: Total candles checked
    """

    passed: bool
    issues: list[str]
    candles_valid: int
    candles_total: int


@dataclass(frozen=True)
class NewsCheckResult:
    """News window check result.

    Attributes:
        blocked: True if trading should be blocked
        reason: Explanation if blocked
        next_clear: When the news window clears (if blocked)
    """

    blocked: bool
    reason: str | None = None
    next_clear: datetime | None = None


class DataQualityChecker:
    """Data Quality checker for SMC Signal Bot.

    Validates market data before SMC engine processing.
    Implements checks from CONTEXT.md section 5 (H1Monitor) and 9 (Risk).
    """

    # Spread limits in pips (from CONTEXT.md)
    SPREAD_LIMITS = {
        "EUR_USD": 2.0,
        "XAU_USD": 100.0,
        "BTC_USD": 50.0,
    }

    # Warning threshold (80% of limit)
    SPREAD_WARNING_PCT = 0.8

    # Minimum candles required
    MIN_CANDLES = 99

    # Max gap multiplier (2x granularity)
    MAX_GAP_MULTIPLIER = 2

    # News blackout window (±120 min from HIGH impact)
    NEWS_BLACKOUT_MINUTES = 120

    def __init__(self, news_client: NewsClient | None = None) -> None:
        """Initialize Data Quality checker.

        Args:
            news_client: Optional NewsClient for news window checks.
                If None, a new NewsClient is created on first use.
                Pass a mock in tests to avoid real API calls.
        """
        self.logger = logger.bind(module="data_quality")
        self._news_client: NewsClient | None = news_client
        self._mock_news_calendar: list[dict[str, Any]] = []
        self.logger.info("data_quality_checker_initialized")

    def _get_granularity_minutes(self, granularity: str) -> int:
        """Convert granularity string to minutes."""
        mapping = {
            "M1": 1,
            "M5": 5,
            "M15": 15,
            "M30": 30,
            "H1": 60,
            "H2": 120,
            "H4": 240,
            "H8": 480,
            "D": 1440,
            "W": 10080,
        }
        return mapping.get(granularity, 60)  # Default to H1

    def check_candles(
        self,
        candles: list[Candle],
        granularity: str = "H1",
    ) -> DQResult:
        """Validate candle data quality.

        Checks:
        - Minimum 100 candles
        - No gaps > 2x granularity
        - OHLC integrity (high >= max(o,c), low <= min(o,c))
        - Volume >= 0

        Args:
            candles: List of Candle objects to validate
            granularity: Candle timeframe (default H1)

        Returns:
            DQResult with validation status and issues
        """
        issues: list[str] = []
        valid_count = 0
        total_count = len(candles)

        self.logger.info(
            "checking_candles",
            total=total_count,
            granularity=granularity,
        )

        # Check minimum count
        if total_count < self.MIN_CANDLES:
            issues.append(
                f"Insufficient candles: {total_count} < {self.MIN_CANDLES}"
            )

        if total_count == 0:
            return DQResult(
                passed=False,
                issues=issues,
                candles_valid=0,
                candles_total=0,
            )

        # Sort candles by timestamp
        sorted_candles = sorted(candles, key=lambda c: c.timestamp)

        # Check gaps
        granularity_minutes = self._get_granularity_minutes(granularity)
        max_gap = timedelta(minutes=granularity_minutes * self.MAX_GAP_MULTIPLIER)

        for i in range(1, len(sorted_candles)):
            prev = sorted_candles[i - 1]
            curr = sorted_candles[i]
            gap = curr.timestamp - prev.timestamp

            if gap > max_gap:
                issues.append(
                    f"Gap detected: {gap} > {max_gap} at {curr.timestamp}"
                )

        # Check individual candles
        for candle in sorted_candles:
            candle_issues = []

            # OHLC integrity
            oc_max = max(candle.open, candle.close)
            oc_min = min(candle.open, candle.close)

            if candle.high < oc_max:
                candle_issues.append(
                    f"High {candle.high} < max(O,C) {oc_max}"
                )
            if candle.low > oc_min:
                candle_issues.append(
                    f"Low {candle.low} > min(O,C) {oc_min}"
                )
            if candle.high < candle.low:
                candle_issues.append(
                    f"High {candle.high} < Low {candle.low}"
                )

            # Volume check
            if candle.volume < 0:
                candle_issues.append(f"Negative volume {candle.volume}")

            if candle_issues:
                issues.extend(
                    [f"{candle.timestamp}: {issue}" for issue in candle_issues]
                )
            else:
                valid_count += 1

        passed = len(issues) == 0 and total_count >= self.MIN_CANDLES

        self.logger.info(
            "candles_checked",
            total=total_count,
            valid=valid_count,
            issues=len(issues),
            passed=passed,
        )

        return DQResult(
            passed=passed,
            issues=issues,
            candles_valid=valid_count,
            candles_total=total_count,
        )

    def check_spread(self, spread: float, instrument: str) -> bool:
        """Check if spread is within acceptable limits.

        Args:
            spread: Current spread in price terms (not pips)
            instrument: Instrument name (e.g., "EUR_USD")

        Returns:
            True if spread is acceptable

        Logs warning if spread > 80% of limit.
        """
        limit_pips = self.SPREAD_LIMITS.get(instrument, 2.0)

        # Convert pips to price for comparison
        # Standard pip sizes: EUR/USD=0.0001, XAU/USD=0.01, BTC/USD=1.0
        pip_sizes = {
            "EUR_USD": 0.0001,
            "XAU_USD": 0.01,
            "BTC_USD": 1.0,
        }
        pip_size = pip_sizes.get(instrument, 0.0001)
        limit_price = limit_pips * pip_size

        # Calculate spread in pips for logging
        spread_pips = spread / pip_size

        warning_threshold = limit_price * self.SPREAD_WARNING_PCT

        if spread > warning_threshold:
            self.logger.warning(
                "spread_high",
                instrument=instrument,
                spread_pips=round(spread_pips, 2),
                limit_pips=limit_pips,
                pct_of_limit=round((spread / limit_price) * 100, 1),
            )

        is_acceptable = spread <= limit_price

        self.logger.info(
            "spread_checked",
            instrument=instrument,
            spread_pips=round(spread_pips, 2),
            limit_pips=limit_pips,
            acceptable=is_acceptable,
        )

        return is_acceptable

    def check_news_window(
        self,
        instrument: str,
        current_time: datetime,
    ) -> NewsCheckResult:
        """Check if trading should be blocked due to news.

        If a mock calendar has been set via set_mock_news_calendar(), uses it
        (backward-compatible path for existing tests).
        Otherwise delegates to NewsClient.is_news_blocked().

        Fail-safe: any API error → blocked=True.

        Args:
            instrument: Target instrument (determines currency check)
            current_time: Time to check (used only in mock-calendar path)

        Returns:
            NewsCheckResult with block status and details
        """
        # ── Mock-calendar path (used by legacy tests) ──────────────────────
        if self._mock_news_calendar:
            currency_map = {
                "EUR_USD": ["USD", "EUR"],
                "XAU_USD": ["USD"],
                "BTC_USD": ["USD"],
            }
            relevant_currencies = currency_map.get(instrument, ["USD"])

            for event in self._mock_news_calendar:
                if event.get("impact") != "HIGH":
                    continue
                if event.get("currency") not in relevant_currencies:
                    continue

                event_time = event.get("time")
                if not isinstance(event_time, datetime):
                    continue

                window_start = event_time - timedelta(minutes=self.NEWS_BLACKOUT_MINUTES)
                window_end = event_time + timedelta(minutes=self.NEWS_BLACKOUT_MINUTES)

                if window_start <= current_time <= window_end:
                    next_clear = window_end
                    reason = (
                        f"HIGH impact {event.get('currency')} news at {event_time}"
                    )
                    self.logger.warning(
                        "news_window_blocked",
                        instrument=instrument,
                        news_event=event.get("name"),
                        reason=reason,
                    )
                    return NewsCheckResult(
                        blocked=True,
                        reason=reason,
                        next_clear=next_clear,
                    )

            self.logger.info(
                "news_window_clear",
                instrument=instrument,
                current_time=current_time,
            )
            return NewsCheckResult(blocked=False)

        # ── Real NewsClient path ───────────────────────────────────────────
        try:
            from connectors.news_client import NewsClient as _NewsClient  # noqa: PLC0415

            client = self._news_client
            if client is None:
                client = _NewsClient()
                self._news_client = client

            nc_result = client.is_news_blocked(
                pair=instrument,
                window_minutes=self.NEWS_BLACKOUT_MINUTES,
            )
            if nc_result.is_blocked:
                self.logger.warning(
                    "news_window_blocked",
                    instrument=instrument,
                    reason=nc_result.reason,
                )
                return NewsCheckResult(
                    blocked=True,
                    reason=nc_result.reason,
                )
            self.logger.info(
                "news_window_clear",
                instrument=instrument,
            )
            return NewsCheckResult(blocked=False)

        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "news_window_check_failed",
                instrument=instrument,
                error=str(exc),
            )
            return NewsCheckResult(
                blocked=True,
                reason=f"News check error — fail-safe block: {exc}",
            )

    def set_mock_news_calendar(self, events: list[dict[str, Any]]) -> None:
        """Set mock news calendar for testing.

        Args:
            events: List of mock events with keys:
                time (datetime), currency (str), impact (str), name (str)

        Note: This is temporary for Week 1. Will be replaced with real API.
        """
        self._mock_news_calendar = events
        self.logger.info(
            "mock_calendar_set",
            event_count=len(events),
        )
