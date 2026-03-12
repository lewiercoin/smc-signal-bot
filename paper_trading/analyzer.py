"""Paper Trading Analyzer — post-session analysis and live-readiness evaluation.

Reads JSON summary logs produced by PaperTradingRunner and produces a structured
AnalysisReport with per-pair breakdown and a go/no-go recommendation for live trading.

Usage:
    python main.py analyze
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── Readiness thresholds ──────────────────────────────────────────────────────

_MIN_CLOSED_TRADES = 20
_MIN_WIN_RATE = 0.40
_MAX_DRAWDOWN_PCT = 15.0
_MAX_SL_HIT_RATE = 0.60
_WARN_WIN_RATE = 0.50
_WARN_MIN_RR = 1.5
_WARN_PAIR_MIN_WR = 0.30
_WARN_PAIR_MIN_TRADES = 5


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class PairStats:
    """Per-instrument performance summary."""

    pair: str
    trades: int
    win_rate: float
    avg_pnl: float
    avg_rr: float


@dataclass
class AnalysisReport:
    """Full paper trading analysis report."""

    # Overview
    total_sessions: int
    total_trades: int
    total_closed: int

    # Performance
    overall_win_rate: float
    overall_pnl: float
    overall_return_pct: float
    max_drawdown: float

    # Per-pair
    by_pair: dict[str, PairStats]

    # Timing
    best_session_time: str
    worst_session_time: str

    # Confluence
    avg_confluence_score: float
    min_confluence_score: int
    max_confluence_score: int

    # Risk
    avg_risk_reward: float
    sl_hit_rate: float
    tp1_hit_rate: float

    # Readiness
    ready_for_live: bool
    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


# ── PaperAnalyzer ─────────────────────────────────────────────────────────────


class PaperAnalyzer:
    """Analyzes paper trading results from JSON session logs."""

    def __init__(self, log_dir: str = "paper_trading/logs") -> None:
        self.log_dir = log_dir
        self.logger = logger.bind(module="paper_analyzer", log_dir=log_dir)

    def load_summaries(self) -> list[dict[str, Any]]:
        """Load all summary_*.json files from log_dir.

        Returns:
            List of parsed summary dicts, sorted by filename.
        """
        pattern = os.path.join(self.log_dir, "summary_*.json")
        paths = sorted(glob.glob(pattern))

        summaries: list[dict[str, Any]] = []
        for path in paths:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                summaries.append(data)
                self.logger.debug("summary_loaded", path=path)
            except (OSError, json.JSONDecodeError) as exc:
                self.logger.warning("summary_load_failed", path=path, error=str(exc))

        self.logger.info("summaries_loaded", count=len(summaries))
        return summaries

    def analyze(self) -> AnalysisReport:
        """Produce a full AnalysisReport from all available session logs.

        Returns:
            AnalysisReport with performance stats and readiness evaluation.
        """
        summaries = self.load_summaries()

        if not summaries:
            self.logger.warning("no_summaries_found", log_dir=self.log_dir)
            ready, issues, recs = self._evaluate_readiness_raw(
                total_closed=0,
                win_rate=0.0,
                max_dd=0.0,
                sl_hit_rate=0.0,
                avg_rr=0.0,
                by_pair={},
            )
            return AnalysisReport(
                total_sessions=0,
                total_trades=0,
                total_closed=0,
                overall_win_rate=0.0,
                overall_pnl=0.0,
                overall_return_pct=0.0,
                max_drawdown=0.0,
                by_pair={},
                best_session_time="N/A",
                worst_session_time="N/A",
                avg_confluence_score=0.0,
                min_confluence_score=0,
                max_confluence_score=0,
                avg_risk_reward=0.0,
                sl_hit_rate=0.0,
                tp1_hit_rate=0.0,
                ready_for_live=False,
                issues=issues,
                recommendations=recs,
            )

        # ── Aggregate across all sessions ─────────────────────────────────────
        all_trades: list[dict[str, Any]] = []
        total_pnl = 0.0
        max_dd = 0.0
        initial_balance = 0.0

        for summary in summaries:
            trades = summary.get("trades", [])
            all_trades.extend(trades)
            total_pnl += summary.get("total_pnl", 0.0)
            dd = summary.get("max_drawdown_pct", summary.get("max_drawdown", 0.0))
            if dd > max_dd:
                max_dd = dd
            if initial_balance == 0.0:
                initial_balance = summary.get("initial_balance", 10000.0)

        closed_trades = [t for t in all_trades if t.get("status") not in ("open", "OPEN", None)]
        total_closed = len(closed_trades)
        total_trades = len(all_trades)

        # Win rate
        winners = [t for t in closed_trades if t.get("pnl_r", 0.0) > 0]
        win_rate = len(winners) / total_closed if total_closed > 0 else 0.0

        # Return %
        return_pct = (total_pnl / initial_balance * 100.0) if initial_balance > 0 else 0.0

        # Per-pair breakdown
        by_pair = self._compute_per_pair(closed_trades)

        # Confluence stats
        scores = [
            t.get("confluence_score", 0)
            for t in all_trades
            if t.get("confluence_score") is not None
        ]
        avg_confluence = sum(scores) / len(scores) if scores else 0.0
        min_confluence = int(min(scores)) if scores else 0
        max_confluence = int(max(scores)) if scores else 0

        # Risk stats
        rr_values = [
            t.get("risk_reward", t.get("rr", 0.0))
            for t in closed_trades
            if t.get("risk_reward", t.get("rr")) is not None
        ]
        avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0

        sl_hits = [t for t in closed_trades if t.get("exit_reason", "").upper() in ("SL", "STOP_LOSS")]
        sl_hit_rate = len(sl_hits) / total_closed if total_closed > 0 else 0.0

        tp1_hits = [t for t in closed_trades if t.get("exit_reason", "").upper() in ("TP1", "TP_1")]
        tp1_hit_rate = len(tp1_hits) / total_closed if total_closed > 0 else 0.0

        # Session timing
        best_time, worst_time = self._compute_session_timing(closed_trades)

        # Readiness
        ready, issues, recs = self._evaluate_readiness_raw(
            total_closed=total_closed,
            win_rate=win_rate,
            max_dd=max_dd,
            sl_hit_rate=sl_hit_rate,
            avg_rr=avg_rr,
            by_pair=by_pair,
        )

        return AnalysisReport(
            total_sessions=len(summaries),
            total_trades=total_trades,
            total_closed=total_closed,
            overall_win_rate=win_rate,
            overall_pnl=total_pnl,
            overall_return_pct=return_pct,
            max_drawdown=max_dd,
            by_pair=by_pair,
            best_session_time=best_time,
            worst_session_time=worst_time,
            avg_confluence_score=avg_confluence,
            min_confluence_score=min_confluence,
            max_confluence_score=max_confluence,
            avg_risk_reward=avg_rr,
            sl_hit_rate=sl_hit_rate,
            tp1_hit_rate=tp1_hit_rate,
            ready_for_live=ready,
            issues=issues,
            recommendations=recs,
        )

    def _compute_per_pair(self, closed_trades: list[dict[str, Any]]) -> dict[str, PairStats]:
        """Compute per-pair stats from closed trades."""
        pairs: dict[str, list[dict[str, Any]]] = {}
        for trade in closed_trades:
            pair = trade.get("pair", "UNKNOWN")
            pairs.setdefault(pair, []).append(trade)

        result: dict[str, PairStats] = {}
        for pair, trades in pairs.items():
            wins = [t for t in trades if t.get("pnl_r", 0.0) > 0]
            wr = len(wins) / len(trades) if trades else 0.0
            avg_pnl = sum(t.get("pnl_r", 0.0) for t in trades) / len(trades) if trades else 0.0
            rr_vals = [
                t.get("risk_reward", t.get("rr", 0.0))
                for t in trades
                if t.get("risk_reward", t.get("rr")) is not None
            ]
            avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0.0
            result[pair] = PairStats(
                pair=pair,
                trades=len(trades),
                win_rate=wr,
                avg_pnl=avg_pnl,
                avg_rr=avg_rr,
            )

        return result

    def _compute_session_timing(
        self, closed_trades: list[dict[str, Any]]
    ) -> tuple[str, str]:
        """Return (best_hour_range, worst_hour_range) by win rate."""
        if not closed_trades:
            return "N/A", "N/A"

        hourly: dict[int, list[float]] = {}
        for trade in closed_trades:
            entry_time = trade.get("entry_time", "")
            if isinstance(entry_time, str) and "T" in entry_time:
                try:
                    hour = int(entry_time.split("T")[1][:2])
                    hourly.setdefault(hour, []).append(trade.get("pnl_r", 0.0))
                except (ValueError, IndexError):
                    pass

        if not hourly:
            return "N/A", "N/A"

        wr_by_hour = {
            h: len([r for r in results if r > 0]) / len(results)
            for h, results in hourly.items()
            if results
        }

        best_h = max(wr_by_hour, key=lambda h: wr_by_hour[h])
        worst_h = min(wr_by_hour, key=lambda h: wr_by_hour[h])

        def _fmt(h: int) -> str:
            return f"{h:02d}:00–{(h + 1) % 24:02d}:00 UTC"

        return _fmt(best_h), _fmt(worst_h)

    def _evaluate_readiness_raw(
        self,
        total_closed: int,
        win_rate: float,
        max_dd: float,
        sl_hit_rate: float,
        avg_rr: float,
        by_pair: dict[str, PairStats],
    ) -> tuple[bool, list[str], list[str]]:
        """Evaluate live-readiness from raw metrics.

        Returns:
            (ready, issues, recommendations)
        """
        issues: list[str] = []
        recommendations: list[str] = []

        # Hard blockers
        if total_closed < _MIN_CLOSED_TRADES:
            issues.append(
                f"Insufficient trades: {total_closed}/{_MIN_CLOSED_TRADES} minimum"
            )
        if win_rate < _MIN_WIN_RATE:
            issues.append(
                f"Win rate {win_rate:.0%} below {_MIN_WIN_RATE:.0%} minimum"
            )
        if max_dd > _MAX_DRAWDOWN_PCT:
            issues.append(
                f"Max drawdown {max_dd:.1f}% exceeds {_MAX_DRAWDOWN_PCT:.1f}% limit"
            )
        if sl_hit_rate > _MAX_SL_HIT_RATE:
            issues.append(
                f"SL hit rate {sl_hit_rate:.0%} too high (>{_MAX_SL_HIT_RATE:.0%})"
            )

        # Warnings
        if win_rate < _WARN_WIN_RATE:
            recommendations.append(
                "Win rate below 50% — consider raising confluence threshold"
            )
        if avg_rr < _WARN_MIN_RR:
            recommendations.append(
                "Avg R:R below 1.5 — review TP placement"
            )

        # Per-pair warnings
        for pair, stats in by_pair.items():
            if stats.trades >= _WARN_PAIR_MIN_TRADES and stats.win_rate < _WARN_PAIR_MIN_WR:
                recommendations.append(
                    f"{pair} underperforming ({stats.win_rate:.0%}) — consider disabling"
                )

        ready = len(issues) == 0
        return ready, issues, recommendations

    def _evaluate_readiness(self, report: AnalysisReport) -> tuple[bool, list[str], list[str]]:
        """Evaluate live-readiness from an AnalysisReport (convenience wrapper)."""
        return self._evaluate_readiness_raw(
            total_closed=report.total_closed,
            win_rate=report.overall_win_rate,
            max_dd=report.max_drawdown,
            sl_hit_rate=report.sl_hit_rate,
            avg_rr=report.avg_risk_reward,
            by_pair=report.by_pair,
        )

    def print_report(self, report: AnalysisReport) -> None:
        """Print a formatted analysis report to stdout."""
        w = 46
        sep = "-" * w

        def line(text: str = "") -> None:
            print(f"| {text:<{w - 3}}|")

        print(f"+{sep}+")
        print(f"|{'SMC Signal Bot -- Paper Trading':^{w}}|")
        print(f"|{'Analysis Report':^{w}}|")
        print(f"+{sep}+")

        line(
            f"Sessions: {report.total_sessions}"
            f"  |  Trades: {report.total_trades}"
            f"  |  Closed: {report.total_closed}"
        )
        pnl_sign = "+" if report.overall_pnl >= 0 else ""
        line(
            f"Win Rate: {report.overall_win_rate:.1%}"
            f"  |  PnL: {pnl_sign}{report.overall_pnl:.2f}R"
        )
        ret_sign = "+" if report.overall_return_pct >= 0 else ""
        line(
            f"Max Drawdown: {report.max_drawdown:.1f}%"
            f"  |  Return: {ret_sign}{report.overall_return_pct:.1f}%"
        )
        print(f"+{sep}+")

        if report.by_pair:
            for pair, stats in sorted(report.by_pair.items()):
                pnl_sign = "+" if stats.avg_pnl >= 0 else ""
                line(
                    f"{pair}: {stats.trades} trades,"
                    f" {stats.win_rate:.1%} WR,"
                    f" {pnl_sign}{stats.avg_pnl:.2f}R avg"
                )
        else:
            line("No per-pair data available")

        print(f"+{sep}+")

        line(f"Confluence: avg={report.avg_confluence_score:.0f}"
             f"  min={report.min_confluence_score}"
             f"  max={report.max_confluence_score}")
        line(f"Avg R:R: {report.avg_risk_reward:.2f}"
             f"  |  SL rate: {report.sl_hit_rate:.0%}"
             f"  |  TP1: {report.tp1_hit_rate:.0%}")
        line(f"Best time: {report.best_session_time}")
        line(f"Worst time: {report.worst_session_time}")

        print(f"+{sep}+")

        ready_str = "YES" if report.ready_for_live else "NO"
        line(f"READY FOR LIVE: {ready_str}")

        if report.issues:
            line("Issues:")
            for issue in report.issues:
                line(f"  [BLOCK] {issue}")

        if report.recommendations:
            line("Recommendations:")
            for rec in report.recommendations:
                line(f"  - {rec}")

        print(f"+{sep}+")
