"""Optimizer Agent — Agent 5 w pipeline (LLM, dziedziczy BaseAgent, GROK-3, weekly).

Weekly optimization: analizuje historię tradów i sugeruje tuning parametrów.
NIE wdraża zmian automatycznie — READ-ONLY (Faza 1).
Operator akceptuje lub odrzuca sugestie ręcznie.

GROK-3: weekly optimization, READ-ONLY (suggests, nie auto-applies).
Scheduling NIE jest w tym module — będzie w Tygodniu 6 (APScheduler).
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any

import structlog

from agents.base_agent import AgentConfig, AgentResult, AgentTier, BaseAgent, MarketBias

logger = structlog.get_logger(__name__)

# ── Parameter safety constants ────────────────────────────────────────────────

PARAMETER_DENYLIST: frozenset[str] = frozenset({
    "max_risk_per_trade",
    "circuit_breaker_daily_loss",
    "circuit_breaker_weekly_loss",
    "max_positions",
    "max_risk",
})

PARAMETER_WHITELIST: dict[str, dict] = {
    "confluence_threshold": {"min": 55, "max": 80, "type": int},
    "tp1_ratio":            {"min": 1.0, "max": 2.5, "type": float},
    "tp2_ratio":            {"min": 2.0, "max": 4.0, "type": float},
    "tp3_ratio":            {"min": 3.0, "max": 6.0, "type": float},
    "session_filter":       {"type": str, "allowed": ["London", "New York", "review"]},
}

CURRENT_VALUES: dict[str, float | int | str] = {
    "confluence_threshold": 65,
    "tp1_ratio": 1.5,
    "tp2_ratio": 2.5,
    "tp3_ratio": 3.5,
    "session_filter": "London,New York",
}

MAX_DELTA_RATIO: float = 0.20


class Optimizer(BaseAgent):
    """Weekly optimization agent — Agent 5 (GROK-3).

    Analizuje historię tradów i sugeruje tuning parametrów strategii.
    NIE wdraża zmian automatycznie (Faza 1 = READ-ONLY).
    3-tier fallback: Cache → LLM (Claude Haiku, temp=0.1) → Deterministic.
    """

    def __init__(self, api_client: object = None) -> None:
        config = AgentConfig(
            temperature=0.1,
            max_tokens=2048,
            cache_ttl_seconds=604800,  # 7 dni — optymalizacja raz na tydzień
        )
        super().__init__(config=config, api_client=api_client)

    @property
    def agent_name(self) -> str:
        return "optimizer"

    # ── Public interface ──────────────────────────────────────────────────────

    def optimize(self, trade_history: list[dict]) -> AgentResult:
        """Weekly optimization — analizuje historię tradów i sugeruje tuning."""
        metrics = self._calculate_metrics(trade_history)
        input_data = {
            "trade_history": trade_history,
            "metrics": metrics,
        }
        if metrics["total_trades"] < 30:
            return self._deterministic_fallback(input_data)
        return self.analyze(input_data)

    # ── Metrics ───────────────────────────────────────────────────────────────

    def _calculate_metrics(self, trade_history: list[dict]) -> dict:
        """Oblicza metryki deterministycznie z trade_history."""
        try:
            return self._calculate_metrics_internal(trade_history)
        except Exception as exc:
            logger.warning("calculate_metrics_error", error=str(exc))
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_r": 0.0,
                "best_r": 0.0,
                "worst_r": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "expectancy": 0.0,
                "max_consecutive_losses": 0,
                "by_instrument": {},
                "by_session": {},
                "by_setup": {},
                "tp_distribution": {
                    "tp1_hit": 0,
                    "tp2_hit": 0,
                    "tp3_hit": 0,
                    "sl_hit": 0,
                    "breakeven": 0,
                },
                "period_days": 0,
            }

    def _calculate_metrics_internal(self, trade_history: list[dict]) -> dict:
        """Wewnętrzna logika obliczania metryk (może rzucać wyjątki)."""
        if not trade_history:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_r": 0.0,
                "best_r": 0.0,
                "worst_r": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "expectancy": 0.0,
                "max_consecutive_losses": 0,
                "by_instrument": {},
                "by_session": {},
                "by_setup": {},
                "tp_distribution": {
                    "tp1_hit": 0,
                    "tp2_hit": 0,
                    "tp3_hit": 0,
                    "sl_hit": 0,
                    "breakeven": 0,
                },
                "period_days": 0,
            }

        total_trades = len(trade_history)
        r_values = [float(t.get("r_achieved", 0.0)) for t in trade_history]

        wins = [r for r in r_values if r > 0.0]
        losses = [r for r in r_values if r < 0.0]

        win_rate = round(len(wins) / total_trades * 100, 1) if total_trades > 0 else 0.0
        avg_r = round(sum(r_values) / len(r_values), 3) if r_values else 0.0
        best_r = round(max(r_values), 3) if r_values else 0.0
        worst_r = round(min(r_values), 3) if r_values else 0.0

        total_wins = sum(wins)
        total_losses = abs(sum(losses))
        if total_losses == 0.0:
            profit_factor = 999.0  # cap — nie infinity
        else:
            profit_factor = round(total_wins / total_losses, 3)

        # TP distribution
        tp_keys = ["tp1_hit", "tp2_hit", "tp3_hit", "sl_hit", "breakeven"]
        tp_distribution: dict[str, int] = {k: 0 for k in tp_keys}
        for trade in trade_history:
            result = trade.get("result", "")
            if result in tp_distribution:
                tp_distribution[result] += 1

        # By instrument
        by_instrument: dict[str, dict[str, Any]] = {}
        instr_groups: dict[str, list[float]] = {}
        for trade in trade_history:
            instr = str(trade.get("instrument", ""))
            if not instr:
                continue
            if instr not in instr_groups:
                instr_groups[instr] = []
            instr_groups[instr].append(float(trade.get("r_achieved", 0.0)))

        for instr, rs in instr_groups.items():
            instr_wins = [r for r in rs if r > 0.0]
            instr_wr = round(len(instr_wins) / len(rs) * 100, 1) if rs else 0.0
            instr_avg_r = round(sum(rs) / len(rs), 3) if rs else 0.0
            by_instrument[instr] = {
                "trades": len(rs),
                "win_rate": instr_wr,
                "avg_r": instr_avg_r,
            }

        # By session
        by_session: dict[str, dict[str, Any]] = {}
        session_groups: dict[str, list[float]] = {}
        for trade in trade_history:
            sess = str(trade.get("session", ""))
            if not sess:
                continue
            if sess not in session_groups:
                session_groups[sess] = []
            session_groups[sess].append(float(trade.get("r_achieved", 0.0)))

        for sess, rs in session_groups.items():
            sess_wins = [r for r in rs if r > 0.0]
            sess_wr = round(len(sess_wins) / len(rs) * 100, 1) if rs else 0.0
            sess_avg_r = round(sum(rs) / len(rs), 3) if rs else 0.0
            by_session[sess] = {
                "trades": len(rs),
                "win_rate": sess_wr,
                "avg_r": sess_avg_r,
            }

        # By setup
        by_setup: dict[str, dict[str, Any]] = {}
        setup_groups: dict[str, list[float]] = {}
        for trade in trade_history:
            setup = str(trade.get("setup_type", ""))
            if not setup:
                continue
            if setup not in setup_groups:
                setup_groups[setup] = []
            setup_groups[setup].append(float(trade.get("r_achieved", 0.0)))

        for setup, rs in setup_groups.items():
            setup_wins = [r for r in rs if r > 0.0]
            setup_wr = round(len(setup_wins) / len(rs) * 100, 1) if rs else 0.0
            setup_avg_r = round(sum(rs) / len(rs), 3) if rs else 0.0
            by_setup[setup] = {
                "trades": len(rs),
                "win_rate": setup_wr,
                "avg_r": setup_avg_r,
            }

        # Period days
        period_days = 0
        dates = []
        for trade in trade_history:
            opened_at = trade.get("opened_at")
            if opened_at:
                try:
                    if isinstance(opened_at, str):
                        dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                    else:
                        dt = opened_at
                    dates.append(dt)
                except (ValueError, TypeError):
                    pass

        if len(dates) >= 2:
            period_days = (max(dates) - min(dates)).days

        # Max drawdown (peak-to-trough na equity curve z R-multiples)
        if r_values:
            equity = list(itertools.accumulate(r_values))
            peak = equity[0]
            max_drawdown = 0.0
            for val in equity:
                peak = max(peak, val)
                max_drawdown = min(max_drawdown, val - peak)
            max_drawdown = round(max_drawdown, 3)
        else:
            max_drawdown = 0.0

        # Expectancy = WR * avg_win_r - LR * avg_loss_r
        avg_win_r = round(sum(wins) / len(wins), 3) if wins else 0.0
        avg_loss_r = round(abs(sum(losses) / len(losses)), 3) if losses else 0.0
        loss_rate = len(losses) / total_trades
        expectancy = round((len(wins) / total_trades) * avg_win_r - loss_rate * avg_loss_r, 3)

        # Max consecutive losses
        max_consecutive_losses = 0
        streak = 0
        for r in r_values:
            if r < 0.0:
                streak += 1
                max_consecutive_losses = max(max_consecutive_losses, streak)
            else:
                streak = 0

        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "avg_r": avg_r,
            "best_r": best_r,
            "worst_r": worst_r,
            "profit_factor": profit_factor,
            "max_drawdown": max_drawdown,
            "expectancy": expectancy,
            "max_consecutive_losses": max_consecutive_losses,
            "by_instrument": by_instrument,
            "by_session": by_session,
            "by_setup": by_setup,
            "tp_distribution": tp_distribution,
            "period_days": period_days,
        }

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _build_prompt(self, input_data: dict) -> tuple[str, str]:
        """Buduje system prompt i user prompt. Zwraca (system, user)."""
        system_prompt = (
            "You are a trading strategy optimizer for an ICT/SMC signal bot.\n"
            "\n"
            "Analyze the performance metrics below and suggest parameter adjustments.\n"
            "\n"
            "TUNABLE PARAMETERS (current values):\n"
            "- confluence_threshold: 65 (min score to publish signal, max possible 110)\n"
            "- tp1_ratio: 1.5R, tp2_ratio: 2.5R, tp3_ratio: 3.5R (BTC: tp3=5.5R)\n"
            "- session_filter: London, New York (currently both enabled)\n"
            "\n"
            "FORBIDDEN parameters (never suggest changes to these):\n"
            "- max_risk_per_trade, circuit_breaker_daily_loss, circuit_breaker_weekly_loss,\n"
            "  max_positions, max_risk (these are hard risk limits, not tunable)\n"
            "\n"
            "RULES:\n"
            "- Suggest SMALL incremental changes only (e.g., threshold 65→67, not 65→80)\n"
            "- Maximum change per parameter: ±20% of current value\n"
            "- Never suggest threshold <55 or >80\n"
            "- If win_rate > 60% and profit_factor > 1.5: suggest NO changes (\"Strategy performing well\")\n"
            "- If total_trades < 30: output performing_well with empty suggestions (insufficient data)\n"
            "- If win_rate < 40%: focus on confluence_threshold increase (more selective)\n"
            "- If avg_r < 1.0: focus on TP ratio adjustments\n"
            "- If one session significantly underperforms: suggest disabling it\n"
            "- Maximum 3 suggestions per optimization run\n"
            "\n"
            "Do NOT suggest changes to SMC detection logic (OB, FVG, swing detection internals).\n"
            "Do NOT suggest adding new indicators (RSI, MACD, fibonacci, volume divergence).\n"
            "Do NOT suggest changes to the AI agent architecture.\n"
            "These are parameter tuning suggestions only.\n"
            "\n"
            "Respond ONLY with valid JSON, no markdown, no backticks, no other text:\n"
            "{\n"
            "    \"overall_assessment\": \"performing_well\" | \"needs_tuning\" | \"underperforming\",\n"
            "    \"confidence\": 0.0-1.0,\n"
            "    \"suggestions\": [\n"
            "        {\n"
            "            \"parameter\": \"confluence_threshold\",\n"
            "            \"current_value\": \"65\",\n"
            "            \"suggested_value\": \"68\",\n"
            "            \"reasoning\": \"Win rate 42% suggests being more selective\"\n"
            "        }\n"
            "    ],\n"
            "    \"summary\": \"1-3 sentence overall summary\"\n"
            "}"
        )

        metrics: dict = input_data.get("metrics", {})
        tp_dist: dict = metrics.get("tp_distribution", {})
        by_instrument: dict = metrics.get("by_instrument", {})
        by_session: dict = metrics.get("by_session", {})
        by_setup: dict = metrics.get("by_setup", {})

        total_trades = metrics.get("total_trades", 0)
        win_rate = metrics.get("win_rate", 0.0)
        avg_r = metrics.get("avg_r", 0.0)
        profit_factor = metrics.get("profit_factor", 0.0)
        best_r = metrics.get("best_r", 0.0)
        worst_r = metrics.get("worst_r", 0.0)
        max_drawdown = metrics.get("max_drawdown", 0.0)
        expectancy = metrics.get("expectancy", 0.0)
        max_consecutive_losses = metrics.get("max_consecutive_losses", 0)
        period_days = metrics.get("period_days", 0)

        tp1_hit = tp_dist.get("tp1_hit", 0)
        tp2_hit = tp_dist.get("tp2_hit", 0)
        tp3_hit = tp_dist.get("tp3_hit", 0)
        sl_hit = tp_dist.get("sl_hit", 0)
        breakeven = tp_dist.get("breakeven", 0)

        by_instrument_lines = []
        for instr, data in by_instrument.items():
            by_instrument_lines.append(
                f"{instr}: {data['trades']} trades, {data['win_rate']}% win rate, avg {data['avg_r']}R"
            )
        by_instrument_formatted = "\n".join(by_instrument_lines) if by_instrument_lines else "No data"

        by_session_lines = []
        for sess, data in by_session.items():
            by_session_lines.append(
                f"{sess}: {data['trades']} trades, {data['win_rate']}% win rate, avg {data['avg_r']}R"
            )
        by_session_formatted = "\n".join(by_session_lines) if by_session_lines else "No data"

        by_setup_lines = []
        for setup, data in by_setup.items():
            by_setup_lines.append(
                f"{setup}: {data['trades']} trades, {data['win_rate']}% win rate, avg {data['avg_r']}R"
            )
        by_setup_formatted = "\n".join(by_setup_lines) if by_setup_lines else "No data"

        user_prompt = (
            f"Analyze this week's trading performance:\n"
            f"\n"
            f"Period: {period_days} days | Total trades: {total_trades}\n"
            f"Win rate: {win_rate}% | Average R: {avg_r} | Profit factor: {profit_factor}\n"
            f"Best trade: {best_r}R | Worst trade: {worst_r}R\n"
            f"Max drawdown: {max_drawdown}R | Expectancy: {expectancy}R | Max consecutive losses: {max_consecutive_losses}\n"
            f"\n"
            f"TP Distribution:\n"
            f"- TP1 hit: {tp1_hit} | TP2 hit: {tp2_hit} | TP3 hit: {tp3_hit}\n"
            f"- SL hit: {sl_hit} | Breakeven: {breakeven}\n"
            f"\n"
            f"Performance by instrument:\n"
            f"{by_instrument_formatted}\n"
            f"\n"
            f"Performance by session:\n"
            f"{by_session_formatted}\n"
            f"\n"
            f"Performance by setup type:\n"
            f"{by_setup_formatted}\n"
            f"\n"
            f"What parameter adjustments do you suggest?"
        )

        return system_prompt, user_prompt

    # ── Parse LLM response ────────────────────────────────────────────────────

    def _parse_llm_response(self, response_text: str, input_data: dict) -> AgentResult:
        """Parsuje odpowiedź LLM (JSON) na AgentResult. Rzuca ValueError przy błędzie parsowania."""
        try:
            data = json.loads(response_text.strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON from LLM: {exc}") from exc

        overall_assessment = data.get("overall_assessment", "")
        if overall_assessment not in ("performing_well", "needs_tuning", "underperforming"):
            raise ValueError(f"Invalid overall_assessment: {overall_assessment!r}")

        raw_confidence = data.get("confidence", 0.5)
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid confidence: {raw_confidence!r}") from exc
        confidence = max(0.0, min(1.0, confidence))

        raw_suggestions = data.get("suggestions", [])
        if not isinstance(raw_suggestions, list):
            raise ValueError("suggestions must be a list")

        validated_suggestions = []
        for suggestion in raw_suggestions:
            if not isinstance(suggestion, dict):
                continue
            parameter = str(suggestion.get("parameter", ""))
            current_value = str(suggestion.get("current_value", ""))
            suggested_value = str(suggestion.get("suggested_value", ""))
            reasoning = str(suggestion.get("reasoning", ""))

            if not parameter:
                continue

            # Layer 1: Denylist — hard risk parameters, always rejected
            if parameter in PARAMETER_DENYLIST:
                logger.warning(
                    "denylist_rejected",
                    parameter=parameter,
                    suggested_value=suggested_value,
                )
                validated_suggestions.append({
                    "parameter": parameter,
                    "current_value": current_value,
                    "suggested_value": suggested_value,
                    "reasoning": reasoning,
                    "note": "Rejected: forbidden risk parameter",
                })
                continue

            # Layer 2: Whitelist — only known tunable parameters allowed
            if parameter not in PARAMETER_WHITELIST:
                logger.warning(
                    "whitelist_rejected",
                    parameter=parameter,
                    suggested_value=suggested_value,
                )
                validated_suggestions.append({
                    "parameter": parameter,
                    "current_value": current_value,
                    "suggested_value": suggested_value,
                    "reasoning": reasoning,
                    "note": "Rejected: unknown parameter not in whitelist",
                })
                continue

            spec = PARAMETER_WHITELIST[parameter]
            param_type = spec["type"]

            # Layer 3: Type parsing
            if param_type in (int, float):
                try:
                    parsed: int | float | str = int(float(suggested_value)) if param_type is int else float(suggested_value)
                except (ValueError, TypeError):
                    logger.warning(
                        "type_parse_rejected",
                        parameter=parameter,
                        suggested_value=suggested_value,
                    )
                    validated_suggestions.append({
                        "parameter": parameter,
                        "current_value": current_value,
                        "suggested_value": suggested_value,
                        "reasoning": reasoning,
                        "note": "Rejected: invalid numeric value",
                    })
                    continue

                # Layer 4: Range check
                param_min = spec.get("min")
                param_max = spec.get("max")
                if (param_min is not None and parsed < param_min) or (param_max is not None and parsed > param_max):
                    logger.warning(
                        "range_rejected",
                        parameter=parameter,
                        parsed=parsed,
                        min=param_min,
                        max=param_max,
                    )
                    validated_suggestions.append({
                        "parameter": parameter,
                        "current_value": current_value,
                        "suggested_value": suggested_value,
                        "reasoning": reasoning,
                        "note": f"Rejected: value {parsed} outside allowed range [{param_min}, {param_max}]",
                    })
                    continue

                # Layer 5: Delta check vs source-of-truth (not LLM's current_value)
                source_value = CURRENT_VALUES.get(parameter)
                if source_value is not None and isinstance(source_value, (int, float)) and source_value != 0:
                    delta_ratio = abs(float(parsed) - float(source_value)) / abs(float(source_value))
                    if delta_ratio > MAX_DELTA_RATIO:
                        logger.warning(
                            "delta_rejected",
                            parameter=parameter,
                            parsed=parsed,
                            source_value=source_value,
                            delta_pct=round(delta_ratio * 100, 1),
                        )
                        validated_suggestions.append({
                            "parameter": parameter,
                            "current_value": current_value,
                            "suggested_value": suggested_value,
                            "reasoning": reasoning,
                            "note": f"Rejected: change {round(delta_ratio * 100, 1)}% exceeds ±{int(MAX_DELTA_RATIO * 100)}% limit",
                        })
                        continue

            elif param_type is str:
                # Layer 4 (str): allowed values check
                allowed = spec.get("allowed", [])
                if allowed and suggested_value not in allowed:
                    logger.warning(
                        "allowed_values_rejected",
                        parameter=parameter,
                        suggested_value=suggested_value,
                        allowed=allowed,
                    )
                    validated_suggestions.append({
                        "parameter": parameter,
                        "current_value": current_value,
                        "suggested_value": suggested_value,
                        "reasoning": reasoning,
                        "note": f"Rejected: value '{suggested_value}' not in allowed {allowed}",
                    })
                    continue

            validated_suggestions.append({
                "parameter": parameter,
                "current_value": current_value,
                "suggested_value": suggested_value,
                "reasoning": reasoning,
            })

        summary = str(data.get("summary", ""))
        metrics: dict = input_data.get("metrics", {})
        period_days: int = metrics.get("period_days", 0)

        return AgentResult(
            agent_name=self.agent_name,
            tier_used=AgentTier.LLM,
            bias=MarketBias.NEUTRAL,
            confidence=confidence,
            reasoning=summary,
            timestamp=datetime.now(timezone.utc),
            raw_data={
                "metrics": metrics,
                "suggestions": validated_suggestions,
                "period_days": period_days,
            },
        )

    # ── Deterministic fallback ────────────────────────────────────────────────

    def _deterministic_fallback(self, input_data: dict) -> AgentResult:
        """Tier 3: rule-based assessment z _calculate_metrics. Nigdy nie rzuca wyjątku."""
        try:
            return self._deterministic_fallback_internal(input_data)
        except Exception as exc:
            logger.warning("deterministic_fallback_error", error=str(exc))
            return AgentResult(
                agent_name="optimizer",
                tier_used=AgentTier.DETERMINISTIC,
                bias=MarketBias.NEUTRAL,
                confidence=0.1,
                reasoning="Deterministic fallback error — no suggestions generated",
                timestamp=datetime.now(timezone.utc),
                raw_data={"metrics": {}, "suggestions": [], "period_days": 0},
            )

    def _deterministic_fallback_internal(self, input_data: dict) -> AgentResult:
        """Wewnętrzna logika deterministyczna (może rzucać wyjątki — opakowane przez _deterministic_fallback)."""
        metrics: dict = input_data.get("metrics", {})
        total_trades: int = int(metrics.get("total_trades", 0))
        win_rate: float = float(metrics.get("win_rate", 0.0))
        avg_r: float = float(metrics.get("avg_r", 0.0))
        profit_factor: float = float(metrics.get("profit_factor", 0.0))
        by_session: dict = metrics.get("by_session", {})
        period_days: int = int(metrics.get("period_days", 0))

        suggestions: list[dict] = []
        summary: str
        confidence: float

        # Reguła 1: za mało danych
        if total_trades < 30:
            suggestions = []
            summary = (
                f"Insufficient data: only {total_trades} trades in period. "
                f"Minimum 30 needed for optimization."
            )
            confidence = 0.1

        # Reguła 2: strategia działa dobrze
        elif win_rate > 60.0 and profit_factor > 1.5:
            suggestions = []
            pf_str = f"{profit_factor:.2f}" if profit_factor < 999.0 else "999.0"
            summary = (
                f"Strategy performing well. Win rate {win_rate}%, PF {pf_str}. "
                f"No changes suggested."
            )
            confidence = 0.6

        # Reguła 3: underperforming
        elif win_rate < 40.0:
            new_threshold = min(75, 65 + 3)  # mała zmiana — +3, cap 75
            suggestions = [{
                "parameter": "confluence_threshold",
                "current_value": "65",
                "suggested_value": str(new_threshold),
                "reasoning": "Low win rate suggests being more selective",
            }]
            summary = (
                f"Win rate {win_rate}% is below 40%. Suggest increasing confluence threshold "
                f"to {new_threshold} to filter weaker setups."
            )
            confidence = 0.4

        # Reguła 4: niski avg_r ale acceptable win_rate
        elif avg_r < 1.0 and win_rate >= 40.0:
            suggestions = [{
                "parameter": "tp1_ratio",
                "current_value": "1.5",
                "suggested_value": "1.3",
                "reasoning": "Low avg R — consider tighter TP1 for more consistent wins",
            }]
            summary = (
                f"Average R {avg_r:.2f} below 1.0. Consider tighter TP1 ratio "
                f"to capture profits more consistently."
            )
            confidence = 0.4

        # Default
        else:
            suggestions = []
            summary = (
                f"Mixed results. Win rate {win_rate}%, avg R {avg_r:.2f}. "
                f"Manual review recommended."
            )
            confidence = 0.3

        # Reguła 5: słaba sesja (może dokładać do istniejącej listy)
        for sess, data in by_session.items():
            sess_wr = float(data.get("win_rate", 100.0))
            sess_trades = int(data.get("trades", 0))
            if sess_wr < 30.0 and sess_trades >= 5:
                session_suggestion = {
                    "parameter": "session_filter",
                    "current_value": "enabled",
                    "suggested_value": "review",
                    "reasoning": (
                        f"{sess} underperforming ({sess_wr}% win rate over {sess_trades} trades)"
                    ),
                }
                suggestions.append(session_suggestion)

        # Max 3 sugestie
        suggestions = suggestions[:3]

        return AgentResult(
            agent_name=self.agent_name,
            tier_used=AgentTier.DETERMINISTIC,
            bias=MarketBias.NEUTRAL,
            confidence=confidence,
            reasoning=summary,
            timestamp=datetime.now(timezone.utc),
            raw_data={
                "metrics": metrics,
                "suggestions": suggestions,
                "period_days": period_days,
            },
        )
