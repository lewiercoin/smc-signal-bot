"""Tests for StructureAgent — prompt building, LLM parsing, deterministic fallback."""

from unittest.mock import MagicMock

import pytest

from agents.base_agent import AgentConfig, AgentResult, AgentTier, MarketBias
from agents.structure_agent import StructureAgent


def make_agent(api_client=None) -> StructureAgent:
    config = AgentConfig(
        model="claude-haiku-4-5-20251001",
        temperature=0.2,
        max_tokens=1024,
        cache_ttl_seconds=86400,
        timeout_seconds=5.0,
        max_retries=2,
    )
    return StructureAgent(config=config, api_client=api_client)


def make_llm_response(text: str) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    return response


SAMPLE_INPUT = {
    "instrument": "EUR_USD",
    "timeframe": "H4",
    "structure_breaks": [
        {"type": "BOS", "direction": "bullish", "index": 42},
    ],
    "swing_points": [
        {"type": "HL", "price": 1.1000, "index": 35},
        {"type": "HH", "price": 1.1050, "index": 40},
    ],
    "current_price": 1.1040,
    "atr": 0.00120,
}


class TestAgentName:
    def test_structure_agent_name(self):
        agent = make_agent()
        assert agent.agent_name == "structure_analyst"


class TestBuildPrompt:
    def test_build_prompt_contains_instrument(self):
        """Prompt contains instrument and timeframe."""
        agent = make_agent()
        system, user = agent._build_prompt(SAMPLE_INPUT)

        assert "EUR_USD" in user
        assert "H4" in user

    def test_build_prompt_contains_structure_breaks(self):
        """Prompt contains the structure breaks passed in input_data."""
        agent = make_agent()
        system, user = agent._build_prompt(SAMPLE_INPUT)

        assert "BOS" in user
        assert "bullish" in user

    def test_build_prompt_do_not_clause(self):
        """System prompt must contain the 'Do NOT analyze fibonacci' line."""
        agent = make_agent()
        system, user = agent._build_prompt(SAMPLE_INPUT)

        assert "Do NOT analyze fibonacci, RSI, MACD, volume divergence" in system


class TestParseLLMResponse:
    def test_parse_llm_response_valid_json(self):
        """Valid JSON response → correct AgentResult."""
        agent = make_agent()
        response_text = '{"confidence": 0.85, "bias": "bullish", "reasoning": "Strong BOS up"}'

        result = agent._parse_llm_response(response_text, SAMPLE_INPUT)

        assert isinstance(result, AgentResult)
        assert result.tier_used == AgentTier.LLM
        assert result.bias == MarketBias.BULLISH
        assert result.confidence == 0.85
        assert result.reasoning == "Strong BOS up"
        assert result.agent_name == "structure_analyst"

    def test_parse_llm_response_invalid_json(self):
        """Invalid JSON → ValueError."""
        agent = make_agent()

        with pytest.raises(ValueError, match="Invalid JSON"):
            agent._parse_llm_response("not valid json at all", SAMPLE_INPUT)

    def test_parse_llm_response_invalid_bias(self):
        """Bias not in allowed enum values → ValueError."""
        agent = make_agent()
        response_text = '{"confidence": 0.7, "bias": "sideways", "reasoning": "..."}'

        with pytest.raises(ValueError, match="Invalid bias"):
            agent._parse_llm_response(response_text, SAMPLE_INPUT)

    def test_parse_llm_response_confidence_out_of_range(self):
        """Confidence > 1.0 → ValueError."""
        agent = make_agent()
        response_text = '{"confidence": 1.5, "bias": "bullish", "reasoning": "..."}'

        with pytest.raises(ValueError, match="Confidence out of range"):
            agent._parse_llm_response(response_text, SAMPLE_INPUT)


class TestDeterministicFallback:
    def test_deterministic_fallback_empty_breaks(self):
        """No structure breaks → NEUTRAL, confidence 0.3."""
        agent = make_agent()
        input_data = {
            "instrument": "EUR_USD",
            "structure_breaks": [],
            "swing_points": [],
        }

        result = agent._deterministic_fallback(input_data)

        assert result.tier_used == AgentTier.DETERMINISTIC
        assert result.bias == MarketBias.NEUTRAL
        assert result.confidence == 0.3

    def test_deterministic_fallback_bos_bullish(self):
        """Last break is BOS bullish → BULLISH, confidence 0.5."""
        agent = make_agent()
        input_data = {
            "instrument": "EUR_USD",
            "structure_breaks": [
                {"type": "CHoCH", "direction": "bearish", "index": 10},
                {"type": "BOS", "direction": "bullish", "index": 20},
            ],
            "swing_points": [],
        }

        result = agent._deterministic_fallback(input_data)

        assert result.bias == MarketBias.BULLISH
        assert result.confidence == 0.5
        assert result.tier_used == AgentTier.DETERMINISTIC

    def test_deterministic_fallback_choch_bearish(self):
        """Last break is CHoCH bearish → BEARISH, confidence 0.4."""
        agent = make_agent()
        input_data = {
            "instrument": "EUR_USD",
            "structure_breaks": [
                {"type": "BOS", "direction": "bullish", "index": 5},
                {"type": "CHoCH", "direction": "bearish", "index": 15},
            ],
            "swing_points": [],
        }

        result = agent._deterministic_fallback(input_data)

        assert result.bias == MarketBias.BEARISH
        assert result.confidence == 0.4
        assert result.tier_used == AgentTier.DETERMINISTIC

    def test_deterministic_fallback_swing_confirmation_bullish(self):
        """BOS bullish + last 3 swings contain HH+HL → confidence boosted to 0.6."""
        agent = make_agent()
        input_data = {
            "instrument": "EUR_USD",
            "structure_breaks": [{"type": "BOS", "direction": "bullish", "index": 20}],
            "swing_points": [
                {"type": "HL", "price": 1.0900},
                {"type": "HH", "price": 1.1000},
                {"type": "HL", "price": 1.0950},
            ],
        }

        result = agent._deterministic_fallback(input_data)

        assert result.bias == MarketBias.BULLISH
        assert result.confidence == pytest.approx(0.6)

    def test_deterministic_fallback_never_raises(self):
        """Broken input_data (missing keys) → always returns AgentResult, no exception."""
        agent = make_agent()

        result = agent._deterministic_fallback({})

        assert isinstance(result, AgentResult)
        assert result.tier_used == AgentTier.DETERMINISTIC
