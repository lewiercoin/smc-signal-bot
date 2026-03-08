"""Base agent class with 3-tier fallback: Cache → LLM → Deterministic."""

import abc
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    pass


class AgentTier(Enum):
    LLM = 1
    CACHE = 2
    DETERMINISTIC = 3


class MarketBias(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class AgentConfig:
    """Konfiguracja agenta."""

    model: str = "claude-haiku-4-5-20251001"
    temperature: float = 0.2
    max_tokens: int = 1024
    cache_ttl_seconds: int = 86400  # 24h
    timeout_seconds: float = 30.0
    max_retries: int = 2


@dataclass(frozen=True)
class AgentResult:
    """Wynik działania agenta."""

    agent_name: str
    tier_used: AgentTier
    bias: MarketBias
    confidence: float  # 0.0–1.0
    reasoning: str  # tekstowe uzasadnienie
    timestamp: datetime
    raw_data: dict = field(default_factory=dict)  # opcjonalne dane szczegółowe
    error_message: str = ""  # niepuste jeśli fallback z powodu błędu


@dataclass(frozen=True)
class CacheEntry:
    """Wpis w cache."""

    cache_key: str
    result: AgentResult
    created_at: datetime


class BaseAgent(abc.ABC):
    """Bazowa klasa agenta z 3-tier fallback (Cache → LLM → Deterministic)."""

    def __init__(self, config: AgentConfig, api_client: Any = None) -> None:
        self.config = config
        self._api_client = api_client
        self._cache: dict[str, CacheEntry] = {}
        self._log: structlog.BoundLogger = structlog.get_logger().bind(
            agent_name=self.agent_name
        )

    @property
    @abc.abstractmethod
    def agent_name(self) -> str:
        """Unikalna nazwa agenta."""

    @abc.abstractmethod
    def _build_prompt(self, input_data: dict) -> tuple[str, str]:
        """Buduje system prompt i user prompt z danych wejściowych. Zwraca (system, user)."""

    @abc.abstractmethod
    def _parse_llm_response(self, response_text: str, input_data: dict) -> "AgentResult":
        """Parsuje odpowiedź LLM na AgentResult."""

    @abc.abstractmethod
    def _deterministic_fallback(self, input_data: dict) -> "AgentResult":
        """Tier 3: zawsze zwraca wynik, nigdy nie rzuca wyjątku."""

    def analyze(self, input_data: dict) -> "AgentResult":
        """Implementuje 3-tier fallback: Cache → LLM → Deterministic."""
        start_time = time.monotonic()
        cache_key = self._make_cache_key(input_data)

        cached = self._check_cache(cache_key)
        if cached is not None:
            elapsed = time.monotonic() - start_time
            self._log.info(
                "cache_hit",
                cache_key=cache_key[:16],
                elapsed_ms=round(elapsed * 1000, 1),
            )
            return cached

        try:
            result = self._call_llm(input_data)
            self._update_cache(cache_key, result)
            elapsed = time.monotonic() - start_time
            self._log.info(
                "llm_success",
                tier=AgentTier.LLM.name,
                elapsed_ms=round(elapsed * 1000, 1),
            )
            return result
        except Exception as exc:
            elapsed = time.monotonic() - start_time
            self._log.warning(
                "llm_failed_using_deterministic",
                error=str(exc),
                elapsed_ms=round(elapsed * 1000, 1),
            )
            result = self._deterministic_fallback(input_data)
            self._log.info(
                "deterministic_fallback_used",
                tier=AgentTier.DETERMINISTIC.name,
                bias=result.bias.value,
                confidence=result.confidence,
            )
            return result

    def _make_cache_key(self, input_data: dict) -> str:
        """Generuje klucz cache: sha256(agent_name + json(input_data))."""
        payload = json.dumps(input_data, sort_keys=True, default=str)
        raw = f"{self.agent_name}:{payload}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _check_cache(self, cache_key: str) -> "AgentResult | None":
        """Sprawdza cache. Zwraca None jeśli brak lub TTL wygasł."""
        entry = self._cache.get(cache_key)
        if entry is None:
            return None

        now = datetime.now(timezone.utc)
        age_seconds = (now - entry.created_at).total_seconds()
        if age_seconds > self.config.cache_ttl_seconds:
            del self._cache[cache_key]
            self._log.debug("cache_expired", cache_key=cache_key[:16], age_seconds=age_seconds)
            return None

        cached_result = AgentResult(
            agent_name=entry.result.agent_name,
            tier_used=AgentTier.CACHE,
            bias=entry.result.bias,
            confidence=entry.result.confidence,
            reasoning=entry.result.reasoning,
            timestamp=datetime.now(timezone.utc),
            raw_data=entry.result.raw_data,
            error_message=entry.result.error_message,
        )
        return cached_result

    def _update_cache(self, cache_key: str, result: "AgentResult") -> None:
        """Zapisuje wynik do cache. Ewiktuje najstarszy wpis jeśli >1000 wpisów."""
        if len(self._cache) >= 1000:
            oldest_key = min(self._cache, key=lambda k: self._cache[k].created_at)
            del self._cache[oldest_key]
            self._log.debug("cache_eviction", evicted_key=oldest_key[:16])

        entry = CacheEntry(
            cache_key=cache_key,
            result=result,
            created_at=datetime.now(timezone.utc),
        )
        self._cache[cache_key] = entry

    def _call_llm(self, input_data: dict) -> "AgentResult":
        """Wywołuje LLM z retry + exponential backoff. Rzuca wyjątek po wyczerpaniu retries."""
        system_prompt, user_prompt = self._build_prompt(input_data)
        last_exc: Exception = RuntimeError("No retries attempted")

        for attempt in range(self.config.max_retries):
            try:
                response = self._api_client.messages.create(
                    model=self.config.model,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    timeout=self.config.timeout_seconds,
                )
                response_text = response.content[0].text
                return self._parse_llm_response(response_text, input_data)
            except Exception as exc:
                last_exc = exc
                wait = 2**attempt  # 1s, 2s
                self._log.warning(
                    "llm_retry",
                    attempt=attempt + 1,
                    max_retries=self.config.max_retries,
                    error=str(exc),
                    wait_seconds=wait,
                )
                if attempt < self.config.max_retries - 1:
                    time.sleep(wait)

        raise last_exc
