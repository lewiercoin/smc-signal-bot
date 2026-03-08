"""Tests for connectors/news_client.py.

10 tests covering:
- FCS response parsing (3)
- News blocking logic (4)
- Pair mapping (2)
- Cache behaviour (1)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests

from connectors.news_client import (
    PAIR_COUNTRY_MAP,
    NewsClient,
    NewsEvent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FCS_SAMPLE_RESPONSE: dict = {
    "response": [
        {
            "title": "Non-Farm Payrolls",
            "country": "US",
            "date": "03-07-2026",
            "time": "08:30am",
            "impact": "High",
            "actual": "",
            "forecast": "180K",
            "previous": "175K",
        },
        {
            "title": "ECB Interest Rate Decision",
            "country": "EU",
            "date": "03-07-2026",
            "time": "01:45pm",
            "impact": "High",
            "actual": "",
            "forecast": "4.00%",
            "previous": "4.00%",
        },
        {
            "title": "EU Retail Sales m/m",
            "country": "EU",
            "date": "03-07-2026",
            "time": "10:00am",
            "impact": "medium",
            "actual": "",
            "forecast": "0.2%",
            "previous": "0.1%",
        },
        {
            "title": "EU Consumer Confidence",
            "country": "EU",
            "date": "03-07-2026",
            "time": "03:00pm",
            "impact": "Low",
            "actual": "",
            "forecast": "-10",
            "previous": "-11",
        },
    ]
}


def _make_client(api_key: str = "test-key") -> NewsClient:
    """Return a NewsClient with a fixed API key (no .env lookup)."""
    return NewsClient(api_key=api_key)


def _utc(hour: int, minute: int = 0, days_offset: int = 0) -> datetime:
    """Return a timezone-aware UTC datetime for today."""
    base = datetime.now(tz=timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return base + timedelta(days=days_offset)


# ---------------------------------------------------------------------------
# 1. Parse FCS response — high-impact events returned
# ---------------------------------------------------------------------------

def test_parse_fcs_response_high_impact() -> None:
    client = _make_client()
    events = client._parse_fcs_response(FCS_SAMPLE_RESPONSE)

    titles = [e.title for e in events]
    assert "Non-Farm Payrolls" in titles
    assert "ECB Interest Rate Decision" in titles

    nfp = next(e for e in events if e.title == "Non-Farm Payrolls")
    assert nfp.impact == "high"
    assert nfp.country == "united-states"
    assert nfp.forecast == "180K"
    assert nfp.previous == "175K"
    assert isinstance(nfp.time, datetime)
    assert nfp.time.tzinfo is not None


# ---------------------------------------------------------------------------
# 2. Parse FCS response — low-impact events filtered out
# ---------------------------------------------------------------------------

def test_parse_fcs_response_filters_low_impact() -> None:
    client = _make_client()
    events = client._parse_fcs_response(FCS_SAMPLE_RESPONSE)

    titles = [e.title for e in events]
    assert "EU Consumer Confidence" not in titles
    assert len(events) == 3  # High×2 + medium×1


# ---------------------------------------------------------------------------
# 3. Parse FCS response — empty response list → empty result
# ---------------------------------------------------------------------------

def test_parse_fcs_response_empty() -> None:
    client = _make_client()
    events = client._parse_fcs_response({"response": []})
    assert events == []


# ---------------------------------------------------------------------------
# 4. News blocked — event within window (60 min from now)
# ---------------------------------------------------------------------------

def test_news_blocked_within_window() -> None:
    client = _make_client()
    now = datetime.now(tz=timezone.utc)
    event_time = now + timedelta(minutes=60)

    event = NewsEvent(
        title="CPI m/m",
        country="united-states",
        time=event_time,
        impact="high",
    )

    with patch.object(client, "get_events_for_pairs", return_value=[event]):
        result = client.is_news_blocked("EUR_USD", window_minutes=120)

    assert result.is_blocked is True
    assert result.blocking_event is event
    assert result.minutes_to_event is not None
    assert 55 <= result.minutes_to_event <= 65


# ---------------------------------------------------------------------------
# 5. News NOT blocked — event outside window (180 min > 120 min window)
# ---------------------------------------------------------------------------

def test_news_not_blocked_outside_window() -> None:
    client = _make_client()
    now = datetime.now(tz=timezone.utc)
    event_time = now + timedelta(minutes=180)

    event = NewsEvent(
        title="GDP q/q",
        country="united-states",
        time=event_time,
        impact="high",
    )

    with patch.object(client, "get_events_for_pairs", return_value=[event]):
        result = client.is_news_blocked("EUR_USD", window_minutes=120)

    assert result.is_blocked is False
    assert result.blocking_event is None


# ---------------------------------------------------------------------------
# 6. Fail-safe — API timeout → is_blocked=True
# ---------------------------------------------------------------------------

def test_news_blocked_fail_safe_on_timeout() -> None:
    client = _make_client()

    with patch.object(
        client,
        "get_events_for_pairs",
        side_effect=requests.Timeout("connect timeout"),
    ):
        result = client.is_news_blocked("EUR_USD")

    assert result.is_blocked is True
    assert "timeout" in result.reason.lower()


# ---------------------------------------------------------------------------
# 7. Fail-safe — no API key → is_blocked=True
# ---------------------------------------------------------------------------

def test_news_blocked_fail_safe_no_api_key() -> None:
    client = NewsClient(api_key=None)
    # Ensure env var is not set in test environment
    with patch.dict("os.environ", {}, clear=False):
        client._api_key = None  # force no key
        result = client.is_news_blocked("XAU_USD")

    assert result.is_blocked is True
    assert "FCS_API_KEY" in result.reason or "key" in result.reason.lower()


# ---------------------------------------------------------------------------
# 8. Pair mapping — EUR_USD includes both eurozone and united-states
# ---------------------------------------------------------------------------

def test_events_for_eur_usd_includes_both_countries() -> None:
    assert "eurozone" in PAIR_COUNTRY_MAP["EUR_USD"]
    assert "united-states" in PAIR_COUNTRY_MAP["EUR_USD"]

    client = _make_client()
    now = datetime.now(tz=timezone.utc)

    us_event = NewsEvent(
        title="NFP", country="united-states", time=now, impact="high"
    )
    eu_event = NewsEvent(
        title="ECB Rate", country="eurozone", time=now, impact="high"
    )
    jp_event = NewsEvent(
        title="BOJ Meeting", country="japan", time=now, impact="high"
    )

    today = now.strftime("%Y-%m-%d")
    cache_key = f"{today}|united-states,eurozone,united-kingdom,switzerland,australia,japan"
    client._cache[cache_key] = (now, [us_event, eu_event, jp_event])

    result = client.get_events_for_pairs(["EUR_USD"])
    titles = [e.title for e in result]

    assert "NFP" in titles
    assert "ECB Rate" in titles
    assert "BOJ Meeting" not in titles


# ---------------------------------------------------------------------------
# 9. Pair mapping — XAU_USD returns only united-states events
# ---------------------------------------------------------------------------

def test_events_for_xau_usd_only_us() -> None:
    assert PAIR_COUNTRY_MAP["XAU_USD"] == ["united-states"]

    client = _make_client()
    now = datetime.now(tz=timezone.utc)

    us_event = NewsEvent(
        title="CPI", country="united-states", time=now, impact="high"
    )
    eu_event = NewsEvent(
        title="ECB Rate", country="eurozone", time=now, impact="high"
    )

    today = now.strftime("%Y-%m-%d")
    cache_key = f"{today}|united-states,eurozone,united-kingdom,switzerland,australia,japan"
    client._cache[cache_key] = (now, [us_event, eu_event])

    result = client.get_events_for_pairs(["XAU_USD"])
    titles = [e.title for e in result]

    assert "CPI" in titles
    assert "ECB Rate" not in titles


# ---------------------------------------------------------------------------
# 10. Cache — second call within TTL does NOT make an HTTP request
# ---------------------------------------------------------------------------

def test_cache_prevents_duplicate_api_calls() -> None:
    client = _make_client()

    mock_response = MagicMock()
    mock_response.json.return_value = FCS_SAMPLE_RESPONSE
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._session, "get", return_value=mock_response) as mock_get:
        # First call — should hit the network
        events_first = client.get_upcoming_events(hours_ahead=24)
        assert mock_get.call_count == 1

        # Second call within TTL — must NOT hit network again
        events_second = client.get_upcoming_events(hours_ahead=24)
        assert mock_get.call_count == 1  # still 1, not 2

    assert events_first == events_second
