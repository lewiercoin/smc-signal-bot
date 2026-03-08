"""Fundamental Analyst Agent — Agent 2 w pipeline."""

import json
from datetime import datetime, timezone

from agents.base_agent import AgentResult, AgentTier, BaseAgent, MarketBias


class FundamentalAgent(BaseAgent):
    """Analizuje wpływ wiadomości/danych makro na instrument."""

    @property
    def agent_name(self) -> str:
        return "fundamental_analyst"

    def _get_session_info(self, _now: datetime | None = None) -> tuple[str, str]:
        """Mapuje godzinę UTC na nazwę sesji i zakres godzin."""
        now = _now if _now is not None else datetime.now(timezone.utc)
        hour = now.hour

        if 0 <= hour <= 7:
            return ("Asian", "00:00-08:00")
        elif 8 <= hour <= 11:
            return ("London", "08:00-12:00")
        elif 12 <= hour <= 16:
            return ("London/NY Overlap", "12:00-17:00")
        elif 17 <= hour <= 20:
            return ("New York", "17:00-21:00")
        else:
            return ("Off-hours", "21:00-00:00")

    def _parse_instrument(self, instrument: str) -> tuple[str, str]:
        """Mapuje instrument string na (base_currency, quote_currency)."""
        mapping = {
            "EUR_USD": ("EUR", "USD"),
            "XAU_USD": ("XAU", "USD"),
            "BTC_USD": ("BTC", "USD"),
        }
        return mapping.get(instrument, ("", ""))

    def _build_prompt(self, input_data: dict, _now: datetime | None = None) -> tuple[str, str]:
        """Buduje system prompt i user prompt. Zwraca (system, user)."""
        instrument = input_data.get("instrument", "")
        news_events = input_data.get("news_events", [])
        current_bias = input_data.get("current_bias", "neutral")

        session_name, session_hours = self._get_session_info(_now)
        now = _now if _now is not None else datetime.now(timezone.utc)
        day_of_week = now.strftime("%A")

        system_prompt = (
            "You are a fundamental/macro analyst for forex and commodities.\n"
            "Evaluate the fundamental context for the given trade setup:\n"
            "1. Your confidence level (0.0-1.0)\n"
            "2. Your bias: \"bullish\", \"bearish\", or \"neutral\"\n"
            "3. Impact level: \"high\", \"medium\", \"low\", or \"none\"\n"
            "4. Brief reasoning (1-3 sentences)\n"
            "\n"
            "Consider: upcoming economic events, current session liquidity,\n"
            "day of week effects, known correlations (DXY for EUR, yields for XAU).\n"
            "\n"
            "Do NOT analyze chart patterns, technical indicators, or price action — that is\n"
            "the StructureAgent's job. Do NOT add analysis categories beyond what is listed above.\n"
            "\n"
            "RULES:\n"
            "- High-impact news for the BASE currency going positive = bullish for pair\n"
            "- High-impact news for the QUOTE currency going positive = bearish for pair\n"
            "- For XAU_USD: risk-off events = bullish gold, strong USD data = bearish gold\n"
            "- For BTC_USD: regulatory fear = bearish, institutional adoption = bullish\n"
            "- If no relevant news or all low-impact = neutral with low confidence\n"
            "- \"actual vs forecast\" matters more than \"actual vs previous\"\n"
            "\n"
            "If fundamentals CONTRADICT the current structure bias, lower confidence.\n"
            "If fundamentals CONFIRM the current structure bias, raise confidence.\n"
            "\n"
            "Respond ONLY with valid JSON, no markdown, no backticks, no other text:\n"
            "{\"confidence\": 0.7, \"bias\": \"neutral\", \"impact_level\": \"high\", \"reasoning\": \"...\"}"
        )

        news_events_formatted = json.dumps(news_events, indent=2)

        user_prompt = (
            f"Instrument: {instrument} | Current bias from StructureAgent: {current_bias}\n"
            f"Current session: {session_name} ({session_hours} UTC)\n"
            f"Day: {day_of_week}\n"
            "\n"
            "Upcoming news events:\n"
            f"{news_events_formatted}\n"
            "\n"
            "What is your fundamental assessment?"
        )

        return system_prompt, user_prompt

    def _parse_llm_response(self, response_text: str, input_data: dict) -> AgentResult:
        """Parsuje odpowiedź LLM na AgentResult. Rzuca ValueError przy błędzie parsowania."""
        try:
            parsed = json.loads(response_text.strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in LLM response: {exc}") from exc

        bias_str = parsed.get("bias", "")
        if bias_str not in ("bullish", "bearish", "neutral"):
            raise ValueError(f"Invalid bias value: {bias_str!r}")

        confidence = parsed.get("confidence")
        if confidence is None or not isinstance(confidence, (int, float)):
            raise ValueError(f"Missing or invalid confidence: {confidence!r}")
        if not (0.0 <= float(confidence) <= 1.0):
            raise ValueError(f"Confidence out of range [0.0, 1.0]: {confidence}")

        impact_level = parsed.get("impact_level")
        if impact_level not in ("high", "medium", "low", "none"):
            raise ValueError(f"Missing or invalid impact_level: {impact_level!r}")

        reasoning = parsed.get("reasoning", "")

        return AgentResult(
            agent_name=self.agent_name,
            tier_used=AgentTier.LLM,
            bias=MarketBias(bias_str),
            confidence=float(confidence),
            reasoning=reasoning,
            timestamp=datetime.now(timezone.utc),
            raw_data={"impact_level": impact_level},
        )

    def _deterministic_fallback(
        self, input_data: dict, _now: datetime | None = None
    ) -> AgentResult:
        """Tier 3: reguły sesyjno-newsowe. Zawsze zwraca AgentResult, nigdy nie rzuca wyjątku."""
        try:
            now = _now if _now is not None else datetime.now(timezone.utc)
            hour = now.hour
            weekday = now.weekday()  # 0=Monday, 4=Friday

            instrument = input_data.get("instrument", "")
            news_events = input_data.get("news_events", [])

            if weekday == 4 and hour >= 16:
                return AgentResult(
                    agent_name=self.agent_name,
                    tier_used=AgentTier.DETERMINISTIC,
                    bias=MarketBias.NEUTRAL,
                    confidence=0.2,
                    reasoning="Weekend gap risk — Friday late session",
                    timestamp=now,
                    raw_data={"impact_level": "low"},
                )

            if weekday == 0 and hour < 8:
                return AgentResult(
                    agent_name=self.agent_name,
                    tier_used=AgentTier.DETERMINISTIC,
                    bias=MarketBias.NEUTRAL,
                    confidence=0.2,
                    reasoning="Monday early — low liquidity",
                    timestamp=now,
                    raw_data={"impact_level": "low"},
                )

            high_impact_events = [
                e for e in news_events if str(e.get("impact", "")).lower() == "high"
            ]

            if high_impact_events:
                event = high_impact_events[0]
                actual = event.get("actual")
                forecast = event.get("forecast")
                event_title = event.get("title", "Unknown event")

                if actual is None:
                    return AgentResult(
                        agent_name=self.agent_name,
                        tier_used=AgentTier.DETERMINISTIC,
                        bias=MarketBias.NEUTRAL,
                        confidence=0.3,
                        reasoning=f"High-impact event pending — {event_title}",
                        timestamp=now,
                        raw_data={"impact_level": "high"},
                    )

                try:
                    actual_val = float(actual)
                    forecast_val = float(forecast)
                    base_currency, _ = self._parse_instrument(instrument)

                    threshold = abs(forecast_val) * 0.1 if forecast_val != 0 else 0.0
                    if abs(actual_val - forecast_val) < threshold:
                        bias = MarketBias.NEUTRAL
                        confidence = 0.3
                        reasoning = f"Actual ≈ forecast for {event_title}"
                    elif actual_val > forecast_val:
                        bias = MarketBias.BULLISH
                        confidence = 0.4
                        reasoning = f"Actual > forecast for {event_title} ({base_currency} base)"
                    else:
                        bias = MarketBias.BEARISH
                        confidence = 0.4
                        reasoning = f"Actual < forecast for {event_title} ({base_currency} base)"

                    session_name, _ = self._get_session_info(now)
                    if session_name == "London/NY Overlap":
                        confidence = min(0.9, confidence + 0.1)
                    elif session_name == "Asian" and instrument == "EUR_USD":
                        confidence = max(0.1, confidence - 0.1)

                    confidence = max(0.1, min(0.9, confidence))

                    return AgentResult(
                        agent_name=self.agent_name,
                        tier_used=AgentTier.DETERMINISTIC,
                        bias=bias,
                        confidence=confidence,
                        reasoning=reasoning,
                        timestamp=now,
                        raw_data={"impact_level": "high"},
                    )
                except (TypeError, ValueError):
                    return AgentResult(
                        agent_name=self.agent_name,
                        tier_used=AgentTier.DETERMINISTIC,
                        bias=MarketBias.NEUTRAL,
                        confidence=0.3,
                        reasoning=f"High-impact event pending — {event_title}",
                        timestamp=now,
                        raw_data={"impact_level": "high"},
                    )

            return AgentResult(
                agent_name=self.agent_name,
                tier_used=AgentTier.DETERMINISTIC,
                bias=MarketBias.NEUTRAL,
                confidence=0.3,
                reasoning="No significant fundamental factors",
                timestamp=now,
                raw_data={"impact_level": "none"},
            )

        except Exception as exc:
            return AgentResult(
                agent_name=self.agent_name,
                tier_used=AgentTier.DETERMINISTIC,
                bias=MarketBias.NEUTRAL,
                confidence=0.3,
                reasoning="Deterministic fallback: error in fallback logic",
                timestamp=datetime.now(timezone.utc),
                raw_data={"impact_level": "none"},
                error_message=str(exc),
            )
