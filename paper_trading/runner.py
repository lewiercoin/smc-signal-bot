"""Paper Trading Runner for SMC Signal Bot.

Simulates live trading without real money. Runs the full pipeline,
tracks virtual positions, computes PnL and max drawdown.

Usage: python main.py paper
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

from db.database import Database
from engine.signal_generator import SignalGenerator

if TYPE_CHECKING:
    from connectors.oanda_client import OandaClient
    from engine.signal_generator import Signal

log = structlog.get_logger(__name__)

PIP_VALUES: dict[str, dict[str, float]] = {
    "EUR_USD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "XAU_USD": {"pip_size": 0.01, "pip_value_per_lot": 1.0},
    "BTC_USD": {"pip_size": 1.0, "pip_value_per_lot": 1.0},
}


@dataclass
class PaperTrade:
    """A single paper trade. NOT frozen — status and pnl update during lifecycle."""

    id: str
    pair: str
    direction: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    lots: float
    confluence_score: int
    opened_at: datetime
    status: str = "open"
    closed_at: datetime | None = None
    close_price: float | None = None
    close_reason: str = ""
    pnl: float = 0.0


class PaperTradingRunner:
    """Runs the signal pipeline in paper trading mode.

    Dependency injection: pass mock clients for testing or leave None for
    live OANDA defaults — consistent with the rest of the project.
    """

    def __init__(
        self,
        signal_generator: SignalGenerator | None = None,
        oanda_client: OandaClient | None = None,
        db: Database | None = None,
        pairs: list[str] | None = None,
        initial_balance: float = 10000.0,
        scan_interval_minutes: int = 15,
        log_dir: str = "paper_trading/logs",
    ) -> None:
        if oanda_client is None:
            from connectors.oanda_client import OandaClient as _OandaClient  # noqa: PLC0415
            oanda_client = _OandaClient()

        if db is None:
            db = Database()

        if signal_generator is None:
            signal_generator = SignalGenerator(oanda_client=oanda_client, db=db)

        self.signal_generator = signal_generator
        self.oanda = oanda_client
        self.db = db
        self.pairs = pairs or ["EUR_USD", "XAU_USD", "BTC_USD"]
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.trades: list[PaperTrade] = []
        self.scan_interval = scan_interval_minutes
        self.log_dir = log_dir

        self.db.initialize()
        os.makedirs(log_dir, exist_ok=True)

        log.info(
            "paper_trading_runner_initialized",
            pairs=self.pairs,
            initial_balance=self.balance,
            scan_interval_minutes=scan_interval_minutes,
        )

    # ── Public interface ──────────────────────────────────────────────────────

    async def run(self, duration_hours: int = 24) -> None:
        """Run paper trading loop for the given duration.

        Args:
            duration_hours: How long to run (default 24h).
        """
        end_time = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        log.info("paper_trading_started", duration_hours=duration_hours, balance=self.balance)

        while datetime.now(timezone.utc) < end_time:
            try:
                await self._scan_cycle()
                await self._check_open_trades()
                await asyncio.sleep(self.scan_interval * 60)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error("paper_cycle_error", error=str(e))
                await asyncio.sleep(60)

        self._print_summary()

    # ── Private: scan cycle ───────────────────────────────────────────────────

    async def _scan_cycle(self) -> None:
        """Scan all pairs for new signals and open paper trades."""
        for pair in self.pairs:
            try:
                signal = self.signal_generator.generate(pair)
                if signal:
                    trade = self._open_paper_trade(signal)
                    self.trades.append(trade)
                    log.info(
                        "paper_trade_opened",
                        pair=signal.pair,
                        direction=signal.direction,
                        entry=signal.entry,
                        sl=signal.stop_loss,
                        tp1=signal.take_profit_1,
                        confluence=signal.confluence_score,
                    )
            except Exception as e:
                log.error("paper_scan_error", pair=pair, error=str(e))

    async def _check_open_trades(self) -> None:
        """Check open trades against current prices; close on SL or TP1."""
        for trade in self.trades:
            if trade.status != "open":
                continue

            current_price = self._get_current_price(trade.pair)
            if current_price is None:
                continue

            if trade.direction == "bullish":
                if current_price <= trade.stop_loss:
                    self._close_trade(trade, current_price, "sl_hit")
                elif current_price >= trade.tp1:
                    self._close_trade(trade, current_price, "tp1_hit")
            else:  # bearish
                if current_price >= trade.stop_loss:
                    self._close_trade(trade, current_price, "sl_hit")
                elif current_price <= trade.tp1:
                    self._close_trade(trade, current_price, "tp1_hit")

    # ── Private: price fetch ──────────────────────────────────────────────────

    def _get_current_price(self, pair: str) -> float | None:
        """Fetch latest close price for a pair.

        Args:
            pair: Instrument key.

        Returns:
            Latest close price or None on error.
        """
        try:
            candles = self.oanda.get_candles(pair, "M1", count=1)
            if candles:
                return candles[-1].close
        except Exception as e:
            log.warning("price_fetch_failed", pair=pair, error=str(e))
        return None

    # ── Private: trade lifecycle ──────────────────────────────────────────────

    def _open_paper_trade(self, signal: Signal) -> PaperTrade:
        """Create a PaperTrade from a Signal.

        Args:
            signal: Fully validated Signal from the pipeline.

        Returns:
            New PaperTrade with status="open".
        """
        return PaperTrade(
            id=str(uuid.uuid4()),
            pair=signal.pair,
            direction=signal.direction,
            entry=signal.entry,
            stop_loss=signal.stop_loss,
            tp1=signal.take_profit_1,
            tp2=signal.take_profit_2,
            tp3=signal.take_profit_3,
            lots=signal.position_size,
            confluence_score=signal.confluence_score,
            opened_at=datetime.now(timezone.utc),
        )

    def _close_trade(self, trade: PaperTrade, close_price: float, reason: str) -> None:
        """Close a paper trade and update balance.

        Args:
            trade: The trade to close.
            close_price: Price at which trade is closed.
            reason: Close reason (e.g. "sl_hit", "tp1_hit").
        """
        pnl = self._calculate_pnl(trade, close_price)
        self.balance += pnl
        trade.status = "closed"
        trade.closed_at = datetime.now(timezone.utc)
        trade.close_price = close_price
        trade.close_reason = reason
        trade.pnl = pnl

        log.info(
            "paper_trade_closed",
            pair=trade.pair,
            reason=reason,
            pnl=round(pnl, 2),
            balance=round(self.balance, 2),
        )

    def _calculate_pnl(self, trade: PaperTrade, close_price: float) -> float:
        """Calculate PnL in account currency.

        Args:
            trade: The trade being closed.
            close_price: Exit price.

        Returns:
            PnL in USD.
        """
        price_diff = close_price - trade.entry
        if trade.direction == "bearish":
            price_diff = trade.entry - close_price

        cfg = PIP_VALUES.get(trade.pair, {"pip_size": 0.0001, "pip_value_per_lot": 10.0})
        pips = price_diff / cfg["pip_size"]
        return pips * cfg["pip_value_per_lot"] * trade.lots

    # ── Private: summary ──────────────────────────────────────────────────────

    def _print_summary(self) -> None:
        """Log and save a JSON summary of all paper trades."""
        closed = [t for t in self.trades if t.status == "closed"]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]

        summary = {
            "total_trades": len(self.trades),
            "closed": len(closed),
            "open": len(self.trades) - len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "total_pnl": round(sum(t.pnl for t in closed), 2),
            "final_balance": round(self.balance, 2),
            "return_pct": round(
                (self.balance - self.initial_balance) / self.initial_balance * 100, 2
            ),
            "max_drawdown": self._calculate_max_drawdown(),
        }

        log.info("paper_trading_summary", **summary)

        filepath = (
            f"{self.log_dir}/summary_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json"
        )
        with open(filepath, "w") as f:
            json.dump(summary, f, indent=2, default=str)

        log.info("paper_trading_summary_saved", filepath=filepath)

    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum equity drawdown as a percentage.

        Returns:
            Max drawdown percentage (e.g. 5.2 = 5.2% drawdown).
        """
        if not self.trades:
            return 0.0

        equity = self.initial_balance
        peak = equity
        max_dd = 0.0

        for trade in sorted(self.trades, key=lambda t: t.opened_at):
            if trade.status == "closed":
                equity += trade.pnl
                peak = max(peak, equity)
                dd = (peak - equity) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)

        return round(max_dd * 100, 2)
