"""Tests for agents/optimizer.py — Optimizer Agent (GROK-3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.base_agent import AgentResult, AgentTier, MarketBias
from agents.optimizer import Optimizer


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_trade(
    instrument: str = "EUR_USD",
    direction: str = "bullish",
    result: str = "tp1_hit",
    r_achieved: float = 1.5,
    session: str = "London",
    setup_type: str = "OB_FVG",
    opened_at: str = "2026-03-01T10:00:00Z",
) -> dict:
    """Factory dla pojedynczego trade."""
    return {
        "instrument": instrument,
        "direction": direction,
        "entry": 1.0850,
        "stop_loss": 1.0800,
        "take_profits": {"tp1": 1.0925, "tp2": 1.0975, "tp3": 1.1050},
        "confluence_score": 72,
        "setup_type": setup_type,
        "session": session,
        "result": result,
        "r_achieved": r_achieved,
        "opened_at": opened_at,
        "closed_at": "2026-03-01T14:15:00Z",
    }


def _make_trade_history(
    n: int,
    win_rate: float = 0.5,
    avg_r: float = 1.5,
    session: str = "London",
    instrument: str = "EUR_USD",
) -> list[dict]:
    """Factory do generowania testowych trade_history z kontrolowanymi parametrami.

    Args:
        n: liczba tradów
        win_rate: odsetek wygranych (0.0–1.0)
        avg_r: przybliżony średni R (wpływa na wartości r_achieved)
        session: sesja dla wszystkich tradów
        instrument: instrument dla wszystkich tradów
    """
    trades = []
    wins = int(n * win_rate)
    for i in range(n):
        if i < wins:
            r = avg_r if avg_r > 0 else 1.5
            result = "tp1_hit"
        else:
            r = -1.0
            result = "sl_hit"
        trades.append(_make_trade(
            instrument=instrument,
            session=session,
            result=result,
            r_achieved=r,
            opened_at=f"2026-03-{(i % 28) + 1:02d}T10:00:00Z",
        ))
    return trades


# ── Agent properties ───────────────────────────────────────────────────────────


def test_optimizer_agent_name() -> None:
    """agent_name zwraca 'optimizer'."""
    agent = Optimizer(api_client=MagicMock())
    assert agent.agent_name == "optimizer"


def test_optimizer_temperature() -> None:
    """config.temperature == 0.4."""
    agent = Optimizer(api_client=MagicMock())
    assert agent.config.temperature == 0.4


def test_optimizer_cache_ttl() -> None:
    """config.cache_ttl_seconds == 604800 (7 dni)."""
    agent = Optimizer(api_client=MagicMock())
    assert agent.config.cache_ttl_seconds == 604800


# ── _build_prompt ──────────────────────────────────────────────────────────────


def test_build_prompt_do_not_clause() -> None:
    """System prompt zawiera oba 'Do NOT' klauzule — kluczowe dla zapobiegania halucynacjom."""
    agent = Optimizer(api_client=MagicMock())
    trade_history = _make_trade_history(10, win_rate=0.5, avg_r=1.5)
    metrics = agent._calculate_metrics(trade_history)
    input_data = {"trade_history": trade_history, "metrics": metrics}
    system_prompt, _ = agent._build_prompt(input_data)
    assert "Do NOT suggest changes to SMC detection logic" in system_prompt
    assert "Do NOT suggest adding new indicators" in system_prompt


def test_build_prompt_contains_metrics() -> None:
    """User prompt zawiera win_rate, avg_r, profit_factor."""
    agent = Optimizer(api_client=MagicMock())
    trade_history = _make_trade_history(10, win_rate=0.6, avg_r=1.8)
    metrics = agent._calculate_metrics(trade_history)
    input_data = {"trade_history": trade_history, "metrics": metrics}
    _, user_prompt = agent._build_prompt(input_data)
    assert "Win rate" in user_prompt
    assert "Average R" in user_prompt
    assert "Profit factor" in user_prompt


def test_build_prompt_contains_tp_distribution() -> None:
    """User prompt zawiera tp1_hit i sl_hit."""
    agent = Optimizer(api_client=MagicMock())
    trade_history = _make_trade_history(10, win_rate=0.5, avg_r=1.5)
    metrics = agent._calculate_metrics(trade_history)
    input_data = {"trade_history": trade_history, "metrics": metrics}
    _, user_prompt = agent._build_prompt(input_data)
    assert "TP1 hit" in user_prompt
    assert "SL hit" in user_prompt


# ── _parse_llm_response ────────────────────────────────────────────────────────


def test_parse_llm_response_valid() -> None:
    """Poprawny JSON → AgentResult z suggestions w raw_data."""
    agent = Optimizer(api_client=MagicMock())
    trade_history = _make_trade_history(10, win_rate=0.5, avg_r=1.5)
    metrics = agent._calculate_metrics(trade_history)
    input_data = {"trade_history": trade_history, "metrics": metrics}

    llm_json = json.dumps({
        "overall_assessment": "needs_tuning",
        "confidence": 0.75,
        "suggestions": [
            {
                "parameter": "confluence_threshold",
                "current_value": "65",
                "suggested_value": "67",
                "reasoning": "Modest improvement in selectivity",
            }
        ],
        "summary": "Strategy needs minor adjustments.",
    })

    result = agent._parse_llm_response(llm_json, input_data)

    assert isinstance(result, AgentResult)
    assert result.agent_name == "optimizer"
    assert result.tier_used == AgentTier.LLM
    assert result.bias == MarketBias.NEUTRAL
    assert result.confidence == 0.75
    assert len(result.raw_data["suggestions"]) == 1
    assert result.raw_data["suggestions"][0]["parameter"] == "confluence_threshold"


def test_parse_llm_response_invalid_json() -> None:
    """Zły JSON → ValueError (Tier 3 fallback wywoływany przez BaseAgent)."""
    agent = Optimizer(api_client=MagicMock())
    input_data = {"trade_history": [], "metrics": {}}

    with pytest.raises(ValueError, match="Invalid JSON"):
        agent._parse_llm_response("this is not json {{{", input_data)


def test_parse_llm_response_safety_guard_risk() -> None:
    """Sugestia max_risk_per_trade > 3% → odrzucona (safety guard)."""
    agent = Optimizer(api_client=MagicMock())
    trade_history = _make_trade_history(5)
    metrics = agent._calculate_metrics(trade_history)
    input_data = {"trade_history": trade_history, "metrics": metrics}

    llm_json = json.dumps({
        "overall_assessment": "needs_tuning",
        "confidence": 0.5,
        "suggestions": [
            {
                "parameter": "max_risk_per_trade",
                "current_value": "0.02",
                "suggested_value": "0.05",  # > 3% — powinno być odrzucone
                "reasoning": "Higher risk for more profit",
            }
        ],
        "summary": "Increase risk to improve returns.",
    })

    result = agent._parse_llm_response(llm_json, input_data)

    assert len(result.raw_data["suggestions"]) == 1
    suggestion = result.raw_data["suggestions"][0]
    assert "note" in suggestion
    assert "Rejected" in suggestion["note"]
    assert "3%" in suggestion["note"]


def test_parse_llm_response_safety_guard_threshold() -> None:
    """Sugestia confluence_threshold < 50 → odrzucona (safety guard)."""
    agent = Optimizer(api_client=MagicMock())
    trade_history = _make_trade_history(5)
    metrics = agent._calculate_metrics(trade_history)
    input_data = {"trade_history": trade_history, "metrics": metrics}

    llm_json = json.dumps({
        "overall_assessment": "needs_tuning",
        "confidence": 0.5,
        "suggestions": [
            {
                "parameter": "confluence_threshold",
                "current_value": "65",
                "suggested_value": "40",  # < 50 — powinno być odrzucone
                "reasoning": "Lower threshold for more signals",
            }
        ],
        "summary": "Lower threshold for more signals.",
    })

    result = agent._parse_llm_response(llm_json, input_data)

    assert len(result.raw_data["suggestions"]) == 1
    suggestion = result.raw_data["suggestions"][0]
    assert "note" in suggestion
    assert "Rejected" in suggestion["note"]
    assert "50" in suggestion["note"]


# ── _calculate_metrics ─────────────────────────────────────────────────────────


def test_calculate_metrics_basic() -> None:
    """10 tradów → poprawne win_rate, avg_r, profit_factor."""
    agent = Optimizer(api_client=MagicMock())
    # 6 wygranych (r=2.0), 4 przegrane (r=-1.0) → WR=60%, avg_r=0.8
    trades = (
        [_make_trade(result="tp1_hit", r_achieved=2.0)] * 6
        + [_make_trade(result="sl_hit", r_achieved=-1.0)] * 4
    )
    metrics = agent._calculate_metrics(trades)

    assert metrics["total_trades"] == 10
    assert metrics["win_rate"] == 60.0
    assert metrics["avg_r"] == pytest.approx(0.8, abs=0.01)
    assert metrics["best_r"] == pytest.approx(2.0, abs=0.01)
    assert metrics["worst_r"] == pytest.approx(-1.0, abs=0.01)
    # PF = (6*2.0) / (4*1.0) = 12/4 = 3.0
    assert metrics["profit_factor"] == pytest.approx(3.0, abs=0.01)


def test_calculate_metrics_empty_history() -> None:
    """Pusta lista → total_trades=0, zerowe metryki, bez wyjątku."""
    agent = Optimizer(api_client=MagicMock())
    metrics = agent._calculate_metrics([])

    assert metrics["total_trades"] == 0
    assert metrics["win_rate"] == 0.0
    assert metrics["avg_r"] == 0.0
    assert metrics["profit_factor"] == 0.0
    assert metrics["by_instrument"] == {}
    assert metrics["by_session"] == {}
    assert metrics["by_setup"] == {}


def test_calculate_metrics_no_losses() -> None:
    """Same wygrane (brak strat) → profit_factor=999.0 (cap, nie infinity)."""
    agent = Optimizer(api_client=MagicMock())
    trades = [_make_trade(result="tp1_hit", r_achieved=2.0)] * 5
    metrics = agent._calculate_metrics(trades)

    assert metrics["profit_factor"] == 999.0
    assert metrics["win_rate"] == 100.0


def test_calculate_metrics_by_instrument() -> None:
    """Breakdown per instrument poprawny."""
    agent = Optimizer(api_client=MagicMock())
    trades = (
        [_make_trade(instrument="EUR_USD", r_achieved=1.5)] * 5
        + [_make_trade(instrument="XAU_USD", r_achieved=2.0)] * 3
    )
    metrics = agent._calculate_metrics(trades)

    assert "EUR_USD" in metrics["by_instrument"]
    assert "XAU_USD" in metrics["by_instrument"]
    assert metrics["by_instrument"]["EUR_USD"]["trades"] == 5
    assert metrics["by_instrument"]["XAU_USD"]["trades"] == 3


def test_calculate_metrics_by_session() -> None:
    """Breakdown per session poprawny."""
    agent = Optimizer(api_client=MagicMock())
    trades = (
        [_make_trade(session="London", r_achieved=1.5)] * 6
        + [_make_trade(session="New York", r_achieved=0.8)] * 4
    )
    metrics = agent._calculate_metrics(trades)

    assert "London" in metrics["by_session"]
    assert "New York" in metrics["by_session"]
    assert metrics["by_session"]["London"]["trades"] == 6
    assert metrics["by_session"]["New York"]["trades"] == 4


# ── _deterministic_fallback ────────────────────────────────────────────────────


def test_deterministic_insufficient_data() -> None:
    """<5 tradów → 'needs_tuning', puste suggestions, 'Insufficient data'."""
    agent = Optimizer(api_client=MagicMock())
    trade_history = _make_trade_history(3)
    metrics = agent._calculate_metrics(trade_history)
    input_data = {"trade_history": trade_history, "metrics": metrics}

    result = agent._deterministic_fallback(input_data)

    assert result.raw_data["suggestions"] == []
    assert "Insufficient data" in result.reasoning
    assert result.confidence == pytest.approx(0.2, abs=0.01)
    assert result.bias == MarketBias.NEUTRAL


def test_deterministic_performing_well() -> None:
    """win_rate>60% + PF>1.5 → 'performing_well', puste suggestions."""
    agent = Optimizer(api_client=MagicMock())
    # 8 wygranych (r=3.0), 2 przegrane (r=-1.0) → WR=80%, PF=12/2=6.0
    trades = (
        [_make_trade(result="tp2_hit", r_achieved=3.0)] * 8
        + [_make_trade(result="sl_hit", r_achieved=-1.0)] * 2
    )
    metrics = agent._calculate_metrics(trades)
    input_data = {"trade_history": trades, "metrics": metrics}

    result = agent._deterministic_fallback(input_data)

    assert result.raw_data["suggestions"] == []
    assert "performing well" in result.reasoning.lower()
    assert result.confidence == pytest.approx(0.6, abs=0.01)


def test_deterministic_underperforming() -> None:
    """win_rate<40% → sugestia podniesienia confluence_threshold."""
    agent = Optimizer(api_client=MagicMock())
    # 3 wygrane (r=1.5), 7 przegranych (r=-1.0) → WR=30%
    trades = (
        [_make_trade(result="tp1_hit", r_achieved=1.5)] * 3
        + [_make_trade(result="sl_hit", r_achieved=-1.0)] * 7
    )
    metrics = agent._calculate_metrics(trades)
    input_data = {"trade_history": trades, "metrics": metrics}

    result = agent._deterministic_fallback(input_data)

    params = [s["parameter"] for s in result.raw_data["suggestions"]]
    assert "confluence_threshold" in params

    # Safety guard: nigdy < 50
    for suggestion in result.raw_data["suggestions"]:
        if suggestion["parameter"] == "confluence_threshold":
            assert int(suggestion["suggested_value"]) >= 50


def test_deterministic_low_avg_r() -> None:
    """avg_r<1.0 + win_rate>=40% → sugestia TP1 adjustment."""
    agent = Optimizer(api_client=MagicMock())
    # 5 wygranych (r=0.5), 5 przegranych (r=-0.4) → WR=50%, avg_r=0.05
    trades = (
        [_make_trade(result="tp1_hit", r_achieved=0.5)] * 5
        + [_make_trade(result="sl_hit", r_achieved=-0.4)] * 5
    )
    metrics = agent._calculate_metrics(trades)
    input_data = {"trade_history": trades, "metrics": metrics}

    result = agent._deterministic_fallback(input_data)

    params = [s["parameter"] for s in result.raw_data["suggestions"]]
    assert "tp1_ratio" in params


def test_deterministic_session_underperforming() -> None:
    """Sesja <30% win rate + >=5 tradów → sugestia review dla tej sesji."""
    agent = Optimizer(api_client=MagicMock())
    # London: 7 wygranych → WR dobre
    # New York: 1 wygrana, 6 przegranych → WR=14%
    trades = (
        [_make_trade(session="London", result="tp1_hit", r_achieved=2.0)] * 7
        + [_make_trade(session="New York", result="tp1_hit", r_achieved=2.0)] * 1
        + [_make_trade(session="New York", result="sl_hit", r_achieved=-1.0)] * 6
    )
    metrics = agent._calculate_metrics(trades)
    # Ręcznie ustawiamy by_session z wymaganymi wartościami
    metrics["by_session"]["New York"]["win_rate"] = 14.3
    metrics["by_session"]["New York"]["trades"] = 7
    input_data = {"trade_history": trades, "metrics": metrics}

    result = agent._deterministic_fallback(input_data)

    params = [s["parameter"] for s in result.raw_data["suggestions"]]
    assert "session_filter" in params

    session_suggestion = next(s for s in result.raw_data["suggestions"] if s["parameter"] == "session_filter")
    assert "New York" in session_suggestion["reasoning"]


def test_deterministic_max_3_suggestions() -> None:
    """Nawet jeśli reguły dają 4+, output ma max 3 sugestie."""
    agent = Optimizer(api_client=MagicMock())
    # Scenariusz który może generować wiele reguł:
    # WR=30% (reguła underperforming), avg_r=0.5 (reguła TP)
    # Ale underperforming ma WR<40%, więc tylko ta reguła działa
    # Dodajemy 2 złe sesje żeby wymusić wiele sugestii
    metrics = {
        "total_trades": 10,
        "win_rate": 30.0,  # underperforming → 1 sugestia
        "avg_r": 0.5,
        "best_r": 1.5,
        "worst_r": -1.0,
        "profit_factor": 0.6,
        "by_instrument": {},
        "by_session": {
            # Te sesje powinny dodać po 1 sugestii każda
            "London": {"trades": 5, "win_rate": 20.0, "avg_r": -0.5},
            "New York": {"trades": 5, "win_rate": 20.0, "avg_r": -0.5},
            "Asia": {"trades": 5, "win_rate": 10.0, "avg_r": -1.0},
        },
        "by_setup": {},
        "tp_distribution": {"tp1_hit": 3, "tp2_hit": 0, "tp3_hit": 0, "sl_hit": 7, "breakeven": 0},
        "period_days": 7,
    }
    input_data = {"trade_history": [], "metrics": metrics}

    result = agent._deterministic_fallback(input_data)

    assert len(result.raw_data["suggestions"]) <= 3


def test_deterministic_never_raises() -> None:
    """Zepsuty input → zwraca default AgentResult, NIE rzuca wyjątku."""
    agent = Optimizer(api_client=MagicMock())

    # Kompletnie zepsuty input
    result = agent._deterministic_fallback({"metrics": None, "trade_history": "not_a_list"})  # type: ignore[arg-type]

    assert isinstance(result, AgentResult)
    assert result.bias == MarketBias.NEUTRAL
    assert result.confidence >= 0.0


# ── optimize() wrapper ────────────────────────────────────────────────────────


def test_optimize_wrapper() -> None:
    """optimize(trade_history) wywołuje analyze() z poprawnymi danymi."""
    agent = Optimizer(api_client=MagicMock())

    with patch.object(agent, "analyze") as mock_analyze:
        mock_analyze.return_value = AgentResult(
            agent_name="optimizer",
            tier_used=AgentTier.DETERMINISTIC,
            bias=MarketBias.NEUTRAL,
            confidence=0.5,
            reasoning="test",
            timestamp=datetime.now(timezone.utc),
            raw_data={"metrics": {}, "suggestions": [], "period_days": 0},
        )
        trade_history = _make_trade_history(5)
        agent.optimize(trade_history)

        mock_analyze.assert_called_once()
        call_args = mock_analyze.call_args[0][0]
        assert "trade_history" in call_args
        assert "metrics" in call_args
        assert call_args["trade_history"] == trade_history


def test_bias_always_neutral() -> None:
    """Optimizer zawsze zwraca MarketBias.NEUTRAL — nie daje bias rynkowego."""
    agent = Optimizer(api_client=MagicMock())
    trade_history = _make_trade_history(5)
    metrics = agent._calculate_metrics(trade_history)
    input_data = {"trade_history": trade_history, "metrics": metrics}

    result = agent._deterministic_fallback(input_data)
    assert result.bias == MarketBias.NEUTRAL


def test_bias_neutral_from_parse_llm_response() -> None:
    """_parse_llm_response zawsze zwraca bias=NEUTRAL."""
    agent = Optimizer(api_client=MagicMock())
    trade_history = _make_trade_history(5)
    metrics = agent._calculate_metrics(trade_history)
    input_data = {"trade_history": trade_history, "metrics": metrics}

    llm_json = json.dumps({
        "overall_assessment": "performing_well",
        "confidence": 0.8,
        "suggestions": [],
        "summary": "All good.",
    })

    result = agent._parse_llm_response(llm_json, input_data)
    assert result.bias == MarketBias.NEUTRAL


def test_optimize_uses_deterministic_when_llm_fails() -> None:
    """Gdy LLM failuje → optimize() zwraca wynik z Tier DETERMINISTIC."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("API timeout")

    agent = Optimizer(api_client=mock_client)
    trade_history = _make_trade_history(10, win_rate=0.5, avg_r=1.5)

    result = agent.optimize(trade_history)

    assert result.tier_used == AgentTier.DETERMINISTIC
    assert result.bias == MarketBias.NEUTRAL
    assert isinstance(result.raw_data.get("suggestions"), list)


def test_calculate_metrics_tp_distribution() -> None:
    """tp_distribution liczy poprawnie każdy typ wyniku."""
    agent = Optimizer(api_client=MagicMock())
    trades = [
        _make_trade(result="tp1_hit", r_achieved=1.5),
        _make_trade(result="tp1_hit", r_achieved=1.5),
        _make_trade(result="tp2_hit", r_achieved=2.5),
        _make_trade(result="tp3_hit", r_achieved=3.5),
        _make_trade(result="sl_hit", r_achieved=-1.0),
        _make_trade(result="sl_hit", r_achieved=-1.0),
        _make_trade(result="breakeven", r_achieved=0.0),
    ]
    metrics = agent._calculate_metrics(trades)
    tp = metrics["tp_distribution"]

    assert tp["tp1_hit"] == 2
    assert tp["tp2_hit"] == 1
    assert tp["tp3_hit"] == 1
    assert tp["sl_hit"] == 2
    assert tp["breakeven"] == 1
