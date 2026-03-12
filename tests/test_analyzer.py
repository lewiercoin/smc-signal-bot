"""Tests for paper_trading/analyzer.py — 8 tests."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from paper_trading.analyzer import AnalysisReport, PaperAnalyzer, PairStats


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_trade(
    pair: str = "EUR_USD",
    pnl_r: float = 1.5,
    exit_reason: str = "TP1",
    risk_reward: float = 1.5,
    confluence_score: int = 72,
    entry_time: str = "2025-01-15T09:00:00",
    status: str = "closed",
) -> dict:
    return {
        "pair": pair,
        "pnl_r": pnl_r,
        "exit_reason": exit_reason,
        "risk_reward": risk_reward,
        "confluence_score": confluence_score,
        "entry_time": entry_time,
        "status": status,
    }


def _make_summary(
    trades: list[dict],
    total_pnl: float = 0.0,
    max_drawdown_pct: float = 0.0,
    initial_balance: float = 10000.0,
) -> dict:
    return {
        "trades": trades,
        "total_pnl": total_pnl,
        "max_drawdown_pct": max_drawdown_pct,
        "initial_balance": initial_balance,
    }


def _write_summaries(tmp_dir: str, summaries: list[dict]) -> None:
    for i, summary in enumerate(summaries):
        path = os.path.join(tmp_dir, f"summary_{i:04d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f)


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestLoadSummaries:
    def test_load_summaries_from_dir(self) -> None:
        """Mock directory with 3 JSON files → load_summaries returns 3 dicts."""
        with tempfile.TemporaryDirectory() as tmp:
            summaries = [
                _make_summary([_make_trade()]),
                _make_summary([_make_trade(pair="XAU_USD")]),
                _make_summary([_make_trade(pair="BTC_USD")]),
            ]
            _write_summaries(tmp, summaries)

            analyzer = PaperAnalyzer(log_dir=tmp)
            result = analyzer.load_summaries()

            assert len(result) == 3

    def test_load_summaries_empty_dir(self) -> None:
        """Empty log dir → returns empty list without error."""
        with tempfile.TemporaryDirectory() as tmp:
            analyzer = PaperAnalyzer(log_dir=tmp)
            result = analyzer.load_summaries()
            assert result == []


class TestAnalyzeStats:
    def test_analyze_computes_overall_stats(self) -> None:
        """Known trade data → correct win_rate, pnl, drawdown."""
        trades = (
            [_make_trade(pnl_r=1.5, exit_reason="TP1")] * 6   # 6 wins
            + [_make_trade(pnl_r=-1.0, exit_reason="SL")] * 4  # 4 losses
        )
        summary = _make_summary(
            trades=trades,
            total_pnl=5.0,
            max_drawdown_pct=3.5,
            initial_balance=10000.0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            _write_summaries(tmp, [summary])
            analyzer = PaperAnalyzer(log_dir=tmp)
            report = analyzer.analyze()

        assert report.total_sessions == 1
        assert report.total_trades == 10
        assert report.total_closed == 10
        assert report.overall_win_rate == pytest.approx(0.60)
        assert report.overall_pnl == pytest.approx(5.0)
        assert report.max_drawdown == pytest.approx(3.5)

    def test_analyze_by_pair_breakdown(self) -> None:
        """3 pairs → correct per-pair stats in by_pair dict."""
        trades = [
            _make_trade(pair="EUR_USD", pnl_r=1.5, exit_reason="TP1"),
            _make_trade(pair="EUR_USD", pnl_r=1.5, exit_reason="TP1"),
            _make_trade(pair="EUR_USD", pnl_r=-1.0, exit_reason="SL"),
            _make_trade(pair="XAU_USD", pnl_r=2.0, exit_reason="TP2"),
            _make_trade(pair="XAU_USD", pnl_r=-1.0, exit_reason="SL"),
            _make_trade(pair="BTC_USD", pnl_r=1.5, exit_reason="TP1"),
        ]
        summary = _make_summary(trades=trades, total_pnl=5.5)

        with tempfile.TemporaryDirectory() as tmp:
            _write_summaries(tmp, [summary])
            analyzer = PaperAnalyzer(log_dir=tmp)
            report = analyzer.analyze()

        assert "EUR_USD" in report.by_pair
        assert "XAU_USD" in report.by_pair
        assert "BTC_USD" in report.by_pair

        eur = report.by_pair["EUR_USD"]
        assert eur.trades == 3
        assert eur.win_rate == pytest.approx(2 / 3)

        xau = report.by_pair["XAU_USD"]
        assert xau.trades == 2
        assert xau.win_rate == pytest.approx(0.5)

        btc = report.by_pair["BTC_USD"]
        assert btc.trades == 1
        assert btc.win_rate == pytest.approx(1.0)


class TestReadiness:
    def _report_with(
        self,
        closed: int = 25,
        win_rate: float = 0.55,
        max_dd: float = 5.0,
        sl_hit_rate: float = 0.30,
        avg_rr: float = 1.8,
        by_pair: dict | None = None,
    ) -> tuple[bool, list[str], list[str]]:
        analyzer = PaperAnalyzer()
        return analyzer._evaluate_readiness_raw(
            total_closed=closed,
            win_rate=win_rate,
            max_dd=max_dd,
            sl_hit_rate=sl_hit_rate,
            avg_rr=avg_rr,
            by_pair=by_pair or {},
        )

    def test_readiness_passes_good_results(self) -> None:
        """WR>50%, DD<15%, 20+ trades → ready=True, no issues."""
        ready, issues, _ = self._report_with(
            closed=25, win_rate=0.55, max_dd=5.0, sl_hit_rate=0.35, avg_rr=1.8
        )
        assert ready is True
        assert issues == []

    def test_readiness_fails_insufficient_trades(self) -> None:
        """10 closed trades → ready=False, issue mentions 10/20."""
        ready, issues, _ = self._report_with(closed=10, win_rate=0.55)
        assert ready is False
        assert any("10" in i and "20" in i for i in issues)

    def test_readiness_fails_high_drawdown(self) -> None:
        """Max drawdown 20% → ready=False, issue mentions drawdown."""
        ready, issues, _ = self._report_with(closed=25, win_rate=0.55, max_dd=20.0)
        assert ready is False
        assert any("drawdown" in i.lower() or "20" in i for i in issues)

    def test_readiness_warns_low_winrate(self) -> None:
        """WR=45% (above 40% blocker but below 50% warn) → recommendation present."""
        ready, issues, recs = self._report_with(
            closed=25, win_rate=0.45, max_dd=5.0, sl_hit_rate=0.35, avg_rr=1.8
        )
        assert ready is True
        assert issues == []
        assert any("win rate" in r.lower() or "50%" in r for r in recs)


class TestPrintReport:
    def test_print_report_no_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        """print_report() with a valid AnalysisReport → no exception, output has key strings."""
        report = AnalysisReport(
            total_sessions=3,
            total_trades=25,
            total_closed=22,
            overall_win_rate=0.583,
            overall_pnl=8.5,
            overall_return_pct=3.4,
            max_drawdown=4.2,
            by_pair={
                "EUR_USD": PairStats("EUR_USD", 12, 0.667, 1.2, 1.6),
                "XAU_USD": PairStats("XAU_USD", 6, 0.5, 0.8, 1.5),
                "BTC_USD": PairStats("BTC_USD", 4, 0.5, 0.9, 1.5),
            },
            best_session_time="09:00–10:00 UTC",
            worst_session_time="00:00–01:00 UTC",
            avg_confluence_score=71.5,
            min_confluence_score=65,
            max_confluence_score=88,
            avg_risk_reward=1.6,
            sl_hit_rate=0.27,
            tp1_hit_rate=0.45,
            ready_for_live=True,
            issues=[],
            recommendations=["Consider raising TP1 for XAU_USD"],
        )

        analyzer = PaperAnalyzer()
        analyzer.print_report(report)

        captured = capsys.readouterr()
        out = captured.out
        assert "READY FOR LIVE" in out
        assert "EUR_USD" in out
        assert "58.3%" in out or "58.4%" in out or "0.583" in out or "58" in out
