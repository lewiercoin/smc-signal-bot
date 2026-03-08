"""Tests for FundamentalAgent — prompt building, LLM parsing, deterministic fallback."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from agents.base_agent import AgentConfig, AgentResult, AgentTier, MarketBias
from agents.fundamental_agent import FundamentalAgent


def make_agent(api_client=None) -> FundamentalAgent:
    config = AgentConfig(
        model="claude-haiku-4-5-20251001",
        temperature=0.3,
        max_tokens=1024,
        cache_ttl_seconds=86400,
        timeout_seconds=5.0,
        max_retries=2,
    )
    return FundamentalAgent(config=config, api_client=api_client)


def make_llm_response(text: str) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    return response


def utc(year=2026, month=3, day=10, hour=10, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)


SAMPLE_NEWS = [
    {
        "title": "ECB raises rates",
        "impact": "high",
        "currency": "EUR",
        "actual": "4.5",
        "forecast": "4.25",
        "previous": "4.0",
    }
]

SAMPLE_INPUT = {
    "instrument": "EUR_USD",
    "news_events": SAMPLE_NEWS,
    "current_bias": "bullish",
}


class TestAgentName:
    def test_fundamental_agent_name(self):
        agent = make_agent()
        assert agent.agent_name == "fundamental_analyst"


class TestBuildPrompt:
    def test_build_prompt_contains_news(self):
        """Prompt contains news events."""
        agent = make_agent()
        system, user = agent._build_prompt(SAMPLE_INPUT)

        assert "ECB raises rates" in user

    def test_build_prompt_contains_current_bias(self):
        """Prompt contains current_bias from StructureAgent."""
        agent = make_agent()
        system, user = agent._build_prompt(SAMPLE_INPUT)

        assert "bullish" in user

    def test_build_prompt_contains_session_info(self):
        """Prompt contains session name and day of week."""
        agent = make_agent()
        now = utc(hour=10)  # London session
        system, user = agent._build_prompt(SAMPLE_INPUT, _now=now)

        assert "London" in user
        assert "Tuesday" in user or "Monday" in user or "Wednesday" in user or "Saturday" in user or "Sunday" in user or "Thursday" in user or "Friday" in user

    def test_build_prompt_do_not_clause(self):
        """System prompt must contain 'Do NOT analyze chart patterns, technical indicators'."""
        agent = make_agent()
        system, user = agent._build_prompt(SAMPLE_INPUT)

        assert "Do NOT analyze chart patterns, technical indicators" in system


class TestParseLLMResponse:
    def test_parse_llm_response_valid_json(self):
        """Valid JSON with impact_level → correct AgentResult."""
        agent = make_agent()
        response_text = (
            '{"confidence": 0.7, "bias": "bullish", "impact_level": "high", '
            '"reasoning": "ECB hawkish"}'
        )

        result = agent._parse_llm_response(response_text, SAMPLE_INPUT)

        assert isinstance(result, AgentResult)
        assert result.tier_used == AgentTier.LLM
        assert result.bias == MarketBias.BULLISH
        assert result.confidence == 0.7
        assert result.raw_data.get("impact_level") == "high"
        assert result.agent_name == "fundamental_analyst"

    def test_parse_llm_response_missing_impact(self):
        """Missing impact_level → ValueError."""
        agent = make_agent()
        response_text = '{"confidence": 0.7, "bias": "bullish", "reasoning": "..."}'

        with pytest.raises(ValueError, match="impact_level"):
            agent._parse_llm_response(response_text, SAMPLE_INPUT)


class TestDeterministicFallbackBasic:
    def test_deterministic_fallback_no_news(self):
        """No news events → NEUTRAL, confidence 0.3, impact 'none'."""
        agent = make_agent()
        input_data = {"instrument": "EUR_USD", "news_events": [], "current_bias": "neutral"}

        result = agent._deterministic_fallback(input_data)

        assert result.tier_used == AgentTier.DETERMINISTIC
        assert result.bias == MarketBias.NEUTRAL
        assert result.confidence == pytest.approx(0.3)
        assert result.raw_data.get("impact_level") == "none"

    def test_deterministic_fallback_high_impact_positive(self):
        """actual > forecast → BULLISH, confidence 0.4."""
        agent = make_agent()
        now = utc(hour=14)  # London/NY overlap for cleaner test (no session modifier)
        input_data = {
            "instrument": "EUR_USD",
            "news_events": [
                {
                    "title": "NFP",
                    "impact": "high",
                    "currency": "EUR",
                    "actual": "250",
                    "forecast": "200",
                }
            ],
            "current_bias": "neutral",
        }

        result = agent._deterministic_fallback(input_data, _now=now)

        assert result.bias == MarketBias.BULLISH
        assert result.confidence >= 0.4
        assert result.raw_data.get("impact_level") == "high"

    def test_deterministic_fallback_high_impact_negative(self):
        """actual < forecast → BEARISH, confidence 0.4."""
        agent = make_agent()
        now = utc(hour=9)  # London session — no overlap modifier
        input_data = {
            "instrument": "EUR_USD",
            "news_events": [
                {
                    "title": "GDP",
                    "impact": "high",
                    "currency": "EUR",
                    "actual": "1.5",
                    "forecast": "2.0",
                }
            ],
            "current_bias": "neutral",
        }

        result = agent._deterministic_fallback(input_data, _now=now)

        assert result.bias == MarketBias.BEARISH
        assert result.confidence == pytest.approx(0.4)
        assert result.raw_data.get("impact_level") == "high"


class TestDeterministicFallbackSessionRules:
    def test_deterministic_fallback_friday_late(self):
        """Friday 17:00 UTC → NEUTRAL, confidence 0.2."""
        agent = make_agent()
        friday_late = datetime(2026, 3, 6, 17, 0, 0, tzinfo=timezone.utc)  # Friday
        input_data = {"instrument": "EUR_USD", "news_events": [], "current_bias": "neutral"}

        result = agent._deterministic_fallback(input_data, _now=friday_late)

        assert result.bias == MarketBias.NEUTRAL
        assert result.confidence == pytest.approx(0.2)
        assert "Friday" in result.reasoning

    def test_deterministic_fallback_monday_early(self):
        """Monday 05:00 UTC → NEUTRAL, confidence 0.2."""
        agent = make_agent()
        monday_early = datetime(2026, 3, 9, 5, 0, 0, tzinfo=timezone.utc)  # Monday
        input_data = {"instrument": "EUR_USD", "news_events": [], "current_bias": "neutral"}

        result = agent._deterministic_fallback(input_data, _now=monday_early)

        assert result.bias == MarketBias.NEUTRAL
        assert result.confidence == pytest.approx(0.2)
        assert "Monday" in result.reasoning

    def test_deterministic_fallback_pending_event(self):
        """High-impact event without 'actual' (upcoming) → NEUTRAL, 0.3, 'pending'."""
        agent = make_agent()
        now = utc(hour=14)
        input_data = {
            "instrument": "EUR_USD",
            "news_events": [
                {"title": "FOMC Decision", "impact": "high", "currency": "USD"}
            ],
            "current_bias": "bullish",
        }

        result = agent._deterministic_fallback(input_data, _now=now)

        assert result.bias == MarketBias.NEUTRAL
        assert result.confidence == pytest.approx(0.3)
        assert "pending" in result.reasoning.lower() or "FOMC" in result.reasoning
        assert result.raw_data.get("impact_level") == "high"

    def test_deterministic_fallback_overlap_confidence_boost(self):
        """London/NY Overlap session → confidence +0.1."""
        agent = make_agent()
        overlap_time = utc(hour=14)  # 14:00 UTC = London/NY Overlap
        input_data = {
            "instrument": "EUR_USD",
            "news_events": [
                {
                    "title": "CPI",
                    "impact": "high",
                    "currency": "EUR",
                    "actual": "3.0",
                    "forecast": "2.5",
                }
            ],
            "current_bias": "neutral",
        }

        result = agent._deterministic_fallback(input_data, _now=overlap_time)

        assert result.bias == MarketBias.BULLISH
        assert result.confidence == pytest.approx(0.5)  # 0.4 base + 0.1 overlap

    def test_deterministic_fallback_asian_eur_confidence_drop(self):
        """Asian session + EUR_USD → confidence -0.1 (0.4 base → 0.3 after penalty)."""
        agent = make_agent()
        asian_time = utc(hour=3)  # 03:00 UTC = Asian session
        input_data = {
            "instrument": "EUR_USD",
            "news_events": [
                {
                    "title": "EUR PMI",
                    "impact": "high",
                    "currency": "EUR",
                    "actual": "60.0",
                    "forecast": "50.0",  # diff=10 > threshold(5) → clearly BULLISH
                }
            ],
            "current_bias": "neutral",
        }

        result = agent._deterministic_fallback(input_data, _now=asian_time)

        assert result.bias == MarketBias.BULLISH
        assert result.confidence == pytest.approx(0.3)  # 0.4 base - 0.1 asian penalty

    def test_deterministic_fallback_never_raises(self):
        """Broken input_data (missing keys) → always returns AgentResult, no exception."""
        agent = make_agent()

        result = agent._deterministic_fallback({})

        assert isinstance(result, AgentResult)
        assert result.tier_used == AgentTier.DETERMINISTIC
        assert result.bias == MarketBias.NEUTRAL


class TestHelperMethods:
    def test_parse_instrument_eur_usd(self):
        agent = make_agent()
        assert agent._parse_instrument("EUR_USD") == ("EUR", "USD")

    def test_parse_instrument_xau_usd(self):
        agent = make_agent()
        assert agent._parse_instrument("XAU_USD") == ("XAU", "USD")

    def test_parse_instrument_btc_usd(self):
        agent = make_agent()
        assert agent._parse_instrument("BTC_USD") == ("BTC", "USD")

    def test_get_session_info_london(self):
        """Hour 10 UTC → London session."""
        agent = make_agent()
        now = utc(hour=10)
        session_name, session_hours = agent._get_session_info(now)

        assert session_name == "London"
        assert session_hours == "08:00-12:00"

    def test_get_session_info_asian(self):
        """Hour 3 UTC → Asian session."""
        agent = make_agent()
        now = utc(hour=3)
        session_name, session_hours = agent._get_session_info(now)

        assert session_name == "Asian"
        assert session_hours == "00:00-08:00"

    def test_get_session_info_overlap(self):
        """Hour 14 UTC → London/NY Overlap."""
        agent = make_agent()
        now = utc(hour=14)
        session_name, session_hours = agent._get_session_info(now)

        assert session_name == "London/NY Overlap"
        assert session_hours == "12:00-17:00"

    def test_get_session_info_new_york(self):
        """Hour 18 UTC → New York."""
        agent = make_agent()
        now = utc(hour=18)
        session_name, session_hours = agent._get_session_info(now)

        assert session_name == "New York"
        assert session_hours == "17:00-21:00"

    def test_get_session_info_off_hours(self):
        """Hour 22 UTC → Off-hours."""
        agent = make_agent()
        now = utc(hour=22)
        session_name, session_hours = agent._get_session_info(now)

        assert session_name == "Off-hours"
        assert session_hours == "21:00-00:00"
