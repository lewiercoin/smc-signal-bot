"""Signal Generator — top-level pipeline orchestrator for SMC Signal Bot.

Connects the full chain:
  OANDA (candles) → Data Quality → SMC Engine → Confluence Scorer
  → Risk Engine → Signal → DB

One call to generate() = complete cycle from raw data to a tradeable Signal
(or None when any gate fails).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

from db.database import Database
from dq.data_quality import DataQualityChecker
from engine.confluence_scorer import ConfluenceScorer
from engine.risk_engine import RiskEngine

if TYPE_CHECKING:
    from connectors.news_client import NewsClient
    from connectors.oanda_client import Candle, OandaClient
    from engine.confluence_scorer import ConfluenceResult
    from engine.risk_engine import TradeParameters

logger = structlog.get_logger(__name__)

PAIRS: list[str] = ["EUR_USD", "XAU_USD", "BTC_USD"]

_HTF_TIMEFRAME = "H4"
_LTF_COUNT = 100
_HTF_COUNT = 50


# ── Signal dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Signal:
    """Fully computed trade signal ready for Telegram publication.

    All monetary values are in the instrument's native quote currency.
    """

    # Identification
    id: str
    pair: str
    timeframe: str
    direction: str          # "bullish" | "bearish"
    timestamp: datetime

    # Trade parameters
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    position_size: float    # lots
    risk_reward_ratio: float

    # Confluence breakdown
    confluence_score: int   # 0–110
    confluence_components: tuple[Any, ...]  # tuple of ScoreComponent

    # Risk info
    risk_amount: float      # $ risked
    risk_pct: float         # fraction risked (0.02 = 2%)
    sl_distance: float
    atr_at_entry: float

    # Lifecycle
    status: str = "pending"   # pending → sent → filled → closed
    notes: str = ""


# ── SignalGenerator ───────────────────────────────────────────────────────────


class SignalGenerator:
    """Top-level pipeline orchestrator — produces Signals from raw market data.

    Each public method is fully exception-safe: external failures (OANDA
    timeout, news API error, DB write failure) are caught and logged; None
    or an empty list is returned instead of propagating.
    """

    def __init__(
        self,
        oanda_client: OandaClient | None = None,
        news_client: NewsClient | None = None,
        db: Database | None = None,
        confluence_threshold: int = 65,
        max_risk_pct: float = 0.02,
    ) -> None:
        """Initialise with injected or default dependencies.

        Args:
            oanda_client: OANDA REST client; creates OandaClient() if None.
            news_client: News API client; creates NewsClient() if None.
            db: Database handle; creates Database() if None.
            confluence_threshold: Minimum score to produce a Signal (default 65).
            max_risk_pct: Maximum account fraction risked per trade (default 2%).
        """
        if oanda_client is None:
            from connectors.oanda_client import OandaClient as _OandaClient  # noqa: PLC0415
            oanda_client = _OandaClient()
        if news_client is None:
            from connectors.news_client import NewsClient as _NewsClient  # noqa: PLC0415
            news_client = _NewsClient()
        if db is None:
            db = Database()

        self.oanda = oanda_client
        self.news = news_client
        self.db = db
        self.dq = DataQualityChecker()
        self.scorer = ConfluenceScorer()
        self.risk = RiskEngine(max_risk_pct=max_risk_pct)
        self.confluence_threshold = confluence_threshold
        self.logger = logger.bind(module="signal_generator")

    # ── Public interface ──────────────────────────────────────────────────────

    def generate(
        self,
        pair: str,
        timeframe: str = "H1",
        account_balance: float | None = None,
    ) -> Signal | None:
        """Run the full pipeline for one instrument.

        Steps:
          1. Fetch OANDA candles + spread
          2. Data Quality gate
          3. Confluence scoring gate
          4. Risk calculation gate
          5. Build Signal, persist to DB

        Args:
            pair: Instrument key e.g. "EUR_USD".
            timeframe: Entry timeframe (default "H1").
            account_balance: Account equity; RiskEngine default used if None.

        Returns:
            Signal if all gates pass, None otherwise.
        """
        log = self.logger.bind(pair=pair, timeframe=timeframe)
        log.info("pipeline_start")

        # ── STEP 1: Fetch data ────────────────────────────────────────────────
        fetch_result = self._fetch_data(pair, timeframe)
        if fetch_result is None:
            log.info("pipeline_abort", step="fetch", reason="data fetch failed")
            return None
        candles, htf_candles, spread = fetch_result

        # ── STEP 2: Data Quality gate ─────────────────────────────────────────
        dq_passed, dq_reason = self._run_dq_checks(pair, candles, spread)
        if not dq_passed:
            log.info("pipeline_abort", step="data_quality", reason=dq_reason)
            return None

        # ── STEP 3: Confluence scoring gate ───────────────────────────────────
        try:
            confluence = self.scorer.score(candles, htf_candles, pair)
        except Exception as exc:
            log.error("confluence_scoring_error", error=str(exc))
            return None

        if confluence.total_score < self.confluence_threshold:
            log.info(
                "pipeline_abort",
                step="confluence",
                reason=f"score {confluence.total_score} < threshold {self.confluence_threshold}",
            )
            return None

        if confluence.setup_direction == "neutral":
            log.info("pipeline_abort", step="confluence", reason="neutral direction")
            return None

        log.info(
            "confluence_gate_passed",
            score=confluence.total_score,
            direction=confluence.setup_direction,
        )

        # ── STEP 4: Risk calculation gate ─────────────────────────────────────
        try:
            trade = self.risk.calculate_trade(
                candles=candles,
                setup_direction=confluence.setup_direction,
                pair=pair,
                account_balance=account_balance,
                current_spread=spread,
            )
        except Exception as exc:
            log.error("risk_calculation_error", error=str(exc))
            return None

        if trade is None:
            log.info("pipeline_abort", step="risk", reason="calculate_trade returned None")
            return None

        if not trade.is_valid:
            log.info(
                "pipeline_abort",
                step="risk",
                reason=trade.rejection_reason or "trade invalid",
            )
            return None

        log.info(
            "risk_gate_passed",
            lots=trade.position_size.lots,
            rr=trade.risk_reward_ratio,
            sl=trade.stop_loss,
        )

        # ── STEP 5: Build Signal and persist ─────────────────────────────────
        signal = self._build_signal(pair, timeframe, confluence, trade)
        self._save_signal(signal)

        log.info(
            "pipeline_complete",
            signal_id=signal.id,
            entry=signal.entry,
            sl=signal.stop_loss,
            score=signal.confluence_score,
        )

        return signal

    def scan_all_pairs(self, timeframe: str = "H1") -> list[Signal]:
        """Scan all three instruments and collect valid Signals.

        One pair failure never blocks others.

        Args:
            timeframe: Entry timeframe forwarded to generate().

        Returns:
            List of 0–3 Signal objects.
        """
        signals: list[Signal] = []
        for pair in PAIRS:
            try:
                signal = self.generate(pair, timeframe)
                if signal is not None:
                    signals.append(signal)
            except Exception as exc:
                self.logger.error("scan_pair_failed", pair=pair, error=str(exc))
        return signals

    # ── Private helpers ───────────────────────────────────────────────────────

    def _fetch_data(
        self,
        pair: str,
        timeframe: str,
    ) -> tuple[list[Candle], list[Candle], float | None] | None:
        """Fetch LTF candles, HTF candles and current spread.

        Args:
            pair: Instrument key.
            timeframe: LTF timeframe string.

        Returns:
            (candles, htf_candles, spread) or None on any OANDA error.
        """
        try:
            candles: list[Candle] = self.oanda.get_candles(pair, timeframe, count=_LTF_COUNT)
            htf_candles: list[Candle] = self.oanda.get_candles(
                pair, _HTF_TIMEFRAME, count=_HTF_COUNT
            )
            spread: float | None
            try:
                spread = self.oanda.get_current_spread(pair)
            except Exception as spread_exc:
                self.logger.warning(
                    "spread_fetch_failed_continuing",
                    pair=pair,
                    error=str(spread_exc),
                )
                spread = None
            return candles, htf_candles, spread
        except Exception as exc:
            self.logger.error("fetch_failed", pair=pair, error=str(exc))
            return None

    def _run_dq_checks(
        self,
        pair: str,
        candles: list[Candle],
        spread: float | None,
    ) -> tuple[bool, str]:
        """Run all Data Quality checks.

        Checks:
        - Candle integrity (count, OHLC, gaps)
        - Spread within limits (when data available)
        - News blackout window

        Args:
            pair: Instrument key.
            candles: LTF candles to validate.
            spread: Current spread in price units; None skips spread DQ.

        Returns:
            (passed, reason) — reason is empty string when passed=True.
        """
        # Candle quality
        dq_result = self.dq.check_candles(candles)
        if not dq_result.passed:
            issues_str = "; ".join(dq_result.issues[:3])  # first 3 to keep log brief
            return False, f"Candle DQ failed: {issues_str}"

        # Spread check
        if spread is not None:
            spread_ok = self.dq.check_spread(spread, pair)
            if not spread_ok:
                return False, f"Spread DQ failed for {pair}: {spread}"

        # News blackout
        try:
            news_result = self.news.is_news_blocked(
                pair=pair,
                window_minutes=120,
            )
            if news_result.is_blocked:
                return False, f"News blackout: {news_result.reason}"
        except Exception as exc:
            self.logger.warning(
                "news_check_error_fail_safe_block",
                pair=pair,
                error=str(exc),
            )
            return False, f"News check error (fail-safe block): {exc}"

        return True, ""

    def _build_signal(
        self,
        pair: str,
        timeframe: str,
        confluence: ConfluenceResult,
        trade: TradeParameters,
    ) -> Signal:
        """Assemble the final Signal dataclass from pipeline results.

        Args:
            pair: Instrument key.
            timeframe: Entry timeframe.
            confluence: Scored ConfluenceResult.
            trade: Validated TradeParameters.

        Returns:
            Immutable Signal ready for DB storage and Telegram publication.
        """
        return Signal(
            id=str(uuid.uuid4()),
            pair=pair,
            timeframe=timeframe,
            direction=confluence.setup_direction,
            timestamp=datetime.now(tz=timezone.utc),
            entry=trade.entry,
            stop_loss=trade.stop_loss,
            take_profit_1=trade.take_profits.tp1,
            take_profit_2=trade.take_profits.tp2,
            take_profit_3=trade.take_profits.tp3,
            position_size=trade.position_size.lots,
            risk_reward_ratio=trade.risk_reward_ratio,
            confluence_score=confluence.total_score,
            confluence_components=tuple(confluence.components),
            risk_amount=trade.position_size.risk_amount,
            risk_pct=trade.position_size.risk_pct,
            sl_distance=trade.sl_distance,
            atr_at_entry=trade.atr_at_entry,
            status="pending",
            notes="",
        )

    def _save_signal(self, signal: Signal) -> None:
        """Persist Signal to the database.

        Failure is logged but never re-raised — a DB error must not
        discard a valid signal from being returned to the caller.

        Args:
            signal: The Signal to persist.
        """
        try:
            payload: dict[str, Any] = {
                "instrument": signal.pair,
                "direction": signal.direction,
                "entry_price": signal.entry,
                "sl_price": signal.stop_loss,
                "tp1_price": signal.take_profit_1,
                "tp2_price": signal.take_profit_2,
                "tp3_price": signal.take_profit_3,
                "confluence_score": signal.confluence_score,
                "session": signal.timeframe,
                "status": signal.status.upper(),
                "created_at": signal.timestamp.isoformat(),
            }
            self.db.save_signal(payload)
        except Exception as exc:
            self.logger.error(
                "db_save_failed",
                signal_id=signal.id,
                pair=signal.pair,
                error=str(exc),
            )
