"""Tests for BaseAgent — 3-tier fallback logic, cache, retry."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.base_agent import (
    AgentConfig,
    AgentResult,
    AgentTier,
    BaseAgent,
    CacheEntry,
    MarketBias,
)


class ConcreteTestAgent(BaseAgent):
    """Minimal concrete implementation of BaseAgent for testing."""

    @property
    def agent_name(self) -> str:
        return "test_agent"

    def _build_prompt(self, input_data: dict) -> tuple[str, str]:
        return "system prompt", "user prompt"

    def _parse_llm_response(self, response_text: str, input_data: dict) -> AgentResult:
        if response_text == "FAIL":
            raise ValueError("Intentional parse failure")
        return AgentResult(
            agent_name=self.agent_name,
            tier_used=AgentTier.LLM,
            bias=MarketBias.BULLISH,
            confidence=0.8,
            reasoning="LLM result",
            timestamp=datetime.now(timezone.utc),
        )

    def _deterministic_fallback(self, input_data: dict) -> AgentResult:
        return AgentResult(
            agent_name=self.agent_name,
            tier_used=AgentTier.DETERMINISTIC,
            bias=MarketBias.NEUTRAL,
            confidence=0.3,
            reasoning="Deterministic fallback",
            timestamp=datetime.now(timezone.utc),
        )


def make_agent(api_client=None) -> ConcreteTestAgent:
    config = AgentConfig(
        model="claude-haiku-4-5-20251001",
        temperature=0.2,
        max_tokens=1024,
        cache_ttl_seconds=86400,
        timeout_seconds=5.0,
        max_retries=2,
    )
    return ConcreteTestAgent(config=config, api_client=api_client)


def make_llm_response(text: str) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    return response


def _make_result(tier: AgentTier = AgentTier.LLM) -> AgentResult:
    return AgentResult(
        agent_name="test_agent",
        tier_used=tier,
        bias=MarketBias.BULLISH,
        confidence=0.8,
        reasoning="cached result",
        timestamp=datetime.now(timezone.utc),
    )


class TestAnalyzeCacheHit:
    def test_analyze_cache_hit(self):
        """Cache hit — returns cached result with tier=CACHE, does NOT call LLM."""
        api_client = MagicMock()
        agent = make_agent(api_client)
        input_data = {"instrument": "EUR_USD"}

        cache_key = agent._make_cache_key(input_data)
        cached_result = _make_result(AgentTier.LLM)
        entry = CacheEntry(
            cache_key=cache_key,
            result=cached_result,
            created_at=datetime.now(timezone.utc),
        )
        agent._cache[cache_key] = entry

        result = agent.analyze(input_data)

        assert result.tier_used == AgentTier.CACHE
        assert result.bias == MarketBias.BULLISH
        api_client.messages.create.assert_not_called()


class TestAnalyzeCacheExpired:
    def test_analyze_cache_expired(self):
        """Expired cache entry — falls through to LLM."""
        api_client = MagicMock()
        api_client.messages.create.return_value = make_llm_response("valid")
        agent = make_agent(api_client)
        input_data = {"instrument": "EUR_USD"}

        cache_key = agent._make_cache_key(input_data)
        old_time = datetime.now(timezone.utc) - timedelta(seconds=90000)
        cached_result = _make_result(AgentTier.LLM)
        entry = CacheEntry(
            cache_key=cache_key,
            result=cached_result,
            created_at=old_time,
        )
        agent._cache[cache_key] = entry

        result = agent.analyze(input_data)

        assert result.tier_used == AgentTier.LLM
        api_client.messages.create.assert_called_once()


class TestAnalyzeCacheMissLLMSuccess:
    def test_analyze_cache_miss_llm_success(self):
        """Cache miss → LLM succeeds → result cached → tier=LLM."""
        api_client = MagicMock()
        api_client.messages.create.return_value = make_llm_response("valid")
        agent = make_agent(api_client)
        input_data = {"instrument": "XAU_USD"}

        result = agent.analyze(input_data)

        assert result.tier_used == AgentTier.LLM
        api_client.messages.create.assert_called_once()

        cache_key = agent._make_cache_key(input_data)
        assert cache_key in agent._cache


class TestAnalyzeLLMFailDeterministicFallback:
    def test_analyze_llm_fail_deterministic_fallback(self):
        """LLM raises exception → tier=DETERMINISTIC."""
        api_client = MagicMock()
        api_client.messages.create.side_effect = RuntimeError("API unavailable")
        agent = make_agent(api_client)

        with patch.object(agent, "_call_llm", side_effect=RuntimeError("API unavailable")):
            result = agent.analyze({"instrument": "BTC_USD"})

        assert result.tier_used == AgentTier.DETERMINISTIC


class TestAnalyzeNeverRaises:
    def test_analyze_never_raises(self):
        """Even when LLM and cache both fail, analyze() always returns AgentResult."""
        api_client = MagicMock()
        api_client.messages.create.side_effect = Exception("total failure")
        agent = make_agent(api_client)

        result = agent.analyze({"some": "data"})

        assert isinstance(result, AgentResult)
        assert result.tier_used == AgentTier.DETERMINISTIC


class TestMakeCacheKey:
    def test_make_cache_key_deterministic(self):
        """Same input always produces same key."""
        agent = make_agent()
        input_data = {"instrument": "EUR_USD", "price": 1.1050}

        key1 = agent._make_cache_key(input_data)
        key2 = agent._make_cache_key(input_data)

        assert key1 == key2
        assert len(key1) == 64  # sha256 hex

    def test_make_cache_key_different_agents(self):
        """Same data but different agent names → different keys."""

        class OtherAgent(ConcreteTestAgent):
            @property
            def agent_name(self) -> str:
                return "other_agent"

        config = AgentConfig()
        agent1 = ConcreteTestAgent(config=config)
        agent2 = OtherAgent(config=config)
        input_data = {"instrument": "EUR_USD"}

        assert agent1._make_cache_key(input_data) != agent2._make_cache_key(input_data)


class TestCacheEviction:
    def test_cache_eviction(self):
        """When cache exceeds 1000 entries, oldest entry is evicted."""
        agent = make_agent()
        base_time = datetime.now(timezone.utc) - timedelta(hours=2)

        for i in range(1000):
            key = f"key_{i:04d}"
            result = _make_result()
            entry = CacheEntry(
                cache_key=key,
                result=result,
                created_at=base_time + timedelta(seconds=i),
            )
            agent._cache[key] = entry

        assert len(agent._cache) == 1000
        oldest_key = "key_0000"
        assert oldest_key in agent._cache

        new_result = _make_result()
        agent._update_cache("new_key", new_result)

        assert len(agent._cache) == 1000
        assert oldest_key not in agent._cache
        assert "new_key" in agent._cache


class TestCallLLMRetry:
    def test_call_llm_retry_on_failure(self):
        """First call fails, second succeeds → returns result."""
        api_client = MagicMock()
        api_client.messages.create.side_effect = [
            RuntimeError("transient error"),
            make_llm_response("valid"),
        ]
        agent = make_agent(api_client)

        with patch("time.sleep"):
            result = agent._call_llm({"instrument": "EUR_USD"})

        assert result.tier_used == AgentTier.LLM
        assert api_client.messages.create.call_count == 2

    def test_call_llm_all_retries_exhausted(self):
        """All retries fail → raises exception (caught by analyze())."""
        api_client = MagicMock()
        api_client.messages.create.side_effect = RuntimeError("persistent error")
        agent = make_agent(api_client)

        with patch("time.sleep"):
            with pytest.raises(RuntimeError, match="persistent error"):
                agent._call_llm({"instrument": "EUR_USD"})

        assert api_client.messages.create.call_count == agent.config.max_retries
