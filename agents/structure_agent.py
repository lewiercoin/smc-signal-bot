"""Structure Analyst Agent — Agent 1 w pipeline."""

import json
from datetime import datetime, timezone

from agents.base_agent import AgentResult, AgentTier, BaseAgent, MarketBias


class StructureAgent(BaseAgent):
    """Analizuje strukturę rynku (BOS/CHoCH, trendy, swing structure) i zwraca bias."""

    @property
    def agent_name(self) -> str:
        return "structure_analyst"

    def _build_prompt(self, input_data: dict) -> tuple[str, str]:
        """Buduje system prompt i user prompt. Zwraca (system, user)."""
        instrument = input_data.get("instrument", "")
        timeframe = input_data.get("timeframe", "")
        structure_breaks = input_data.get("structure_breaks", [])
        swing_points = input_data.get("swing_points", [])
        current_price = input_data.get("current_price", 0.0)
        atr = input_data.get("atr", 0.0)

        system_prompt = (
            "You are an ICT/SMC market structure analyst. Analyze the given setup and provide:\n"
            "1. Your confidence level (0.0-1.0)\n"
            "2. Your bias: \"bullish\", \"bearish\", or \"neutral\"\n"
            "3. Brief reasoning (1-3 sentences)\n"
            "\n"
            "Focus ONLY on: BOS/CHoCH alignment, swing point sequences (HH/HL/LH/LL),\n"
            "premium/discount positioning relative to swing structure.\n"
            "\n"
            "Do NOT analyze fibonacci, RSI, MACD, volume divergence, or any indicators\n"
            "outside ICT/SMC methodology. Do NOT add analysis categories beyond what is\n"
            "listed above.\n"
            "\n"
            "Respond ONLY with valid JSON, no markdown, no backticks, no other text:\n"
            "{\"confidence\": 0.85, \"bias\": \"bullish\", \"reasoning\": \"...\"}"
        )

        structure_breaks_formatted = json.dumps(structure_breaks, indent=2)
        swing_points_formatted = json.dumps(swing_points, indent=2)

        user_prompt = (
            f"Instrument: {instrument} | Timeframe: {timeframe}\n"
            f"Current price: {current_price} | ATR(14): {atr}\n"
            "\n"
            "Structure breaks (recent):\n"
            f"{structure_breaks_formatted}\n"
            "\n"
            "Swing points (recent):\n"
            f"{swing_points_formatted}\n"
            "\n"
            "What is your structural bias?"
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

        reasoning = parsed.get("reasoning", "")

        return AgentResult(
            agent_name=self.agent_name,
            tier_used=AgentTier.LLM,
            bias=MarketBias(bias_str),
            confidence=float(confidence),
            reasoning=reasoning,
            timestamp=datetime.now(timezone.utc),
        )

    def _deterministic_fallback(self, input_data: dict) -> AgentResult:
        """Tier 3: reguły na bazie ostatniego structure break + swing points."""
        try:
            structure_breaks = input_data.get("structure_breaks", [])
            swing_points = input_data.get("swing_points", [])

            if not structure_breaks:
                return AgentResult(
                    agent_name=self.agent_name,
                    tier_used=AgentTier.DETERMINISTIC,
                    bias=MarketBias.NEUTRAL,
                    confidence=0.3,
                    reasoning="Deterministic fallback: no structure breaks available",
                    timestamp=datetime.now(timezone.utc),
                    error_message="No structure breaks in input_data",
                )

            last_break = structure_breaks[-1]
            break_type = str(last_break.get("type", "")).upper()
            direction = str(last_break.get("direction", "")).lower()

            if break_type == "BOS":
                if direction == "bullish":
                    bias = MarketBias.BULLISH
                    confidence = 0.5
                elif direction == "bearish":
                    bias = MarketBias.BEARISH
                    confidence = 0.5
                else:
                    bias = MarketBias.NEUTRAL
                    confidence = 0.3
            elif break_type == "CHOCH":
                if direction == "bullish":
                    bias = MarketBias.BULLISH
                    confidence = 0.4
                elif direction == "bearish":
                    bias = MarketBias.BEARISH
                    confidence = 0.4
                else:
                    bias = MarketBias.NEUTRAL
                    confidence = 0.3
            else:
                bias = MarketBias.NEUTRAL
                confidence = 0.3

            recent_swings = swing_points[-3:] if len(swing_points) >= 3 else swing_points
            swing_types = [str(s.get("type", "")).upper() for s in recent_swings]
            if bias == MarketBias.BULLISH and set(swing_types) >= {"HH", "HL"}:
                confidence = min(1.0, confidence + 0.1)
            elif bias == MarketBias.BEARISH and set(swing_types) >= {"LH", "LL"}:
                confidence = min(1.0, confidence + 0.1)

            reasoning = f"Deterministic fallback: last break was {break_type} {direction}"

            return AgentResult(
                agent_name=self.agent_name,
                tier_used=AgentTier.DETERMINISTIC,
                bias=bias,
                confidence=confidence,
                reasoning=reasoning,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:
            return AgentResult(
                agent_name=self.agent_name,
                tier_used=AgentTier.DETERMINISTIC,
                bias=MarketBias.NEUTRAL,
                confidence=0.3,
                reasoning="Deterministic fallback: error in fallback logic",
                timestamp=datetime.now(timezone.utc),
                error_message=str(exc),
            )
