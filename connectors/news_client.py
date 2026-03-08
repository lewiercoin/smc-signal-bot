"""News client for SMC Signal Bot.

Fetches economic calendar events from FCS API with Forex Factory fallback.
Used by confluence_scorer to check news blackout windows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import structlog
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logger = structlog.get_logger(__name__)

# FCS API country codes for economic calendar
PAIR_COUNTRY_MAP: dict[str, list[str]] = {
    "EUR_USD": ["eurozone", "united-states"],
    "XAU_USD": ["united-states"],
    "BTC_USD": ["united-states"],
}

# FCS API country code → normalised name mapping
FCS_COUNTRY_CODE_MAP: dict[str, str] = {
    "US": "united-states",
    "EU": "eurozone",
    "GB": "united-kingdom",
    "CH": "switzerland",
    "AU": "australia",
    "JP": "japan",
}

_HIGH_IMPACT_LABELS = {"high", "hight"}  # FCS sometimes typos "hight"
_MEDIUM_IMPACT_LABELS = {"medium", "moderate", "med"}
_ALLOWED_IMPACT_LABELS = _HIGH_IMPACT_LABELS | _MEDIUM_IMPACT_LABELS

_FCS_BASE_URL = "https://fcsapi.com/api-v3/forex/economy_cal"
_FOREX_FACTORY_URL = "https://www.forexfactory.com/calendar"

_DEFAULT_COUNTRIES = (
    "united-states,eurozone,united-kingdom,switzerland,australia,japan"
)


@dataclass(frozen=True)
class NewsEvent:
    """A single economic calendar event.

    Attributes:
        title: Event name (e.g. "Non-Farm Payrolls")
        country: Normalised country name (e.g. "united-states")
        time: Event UTC datetime
        impact: "high" or "medium"
        forecast: Analyst forecast string (may be empty)
        previous: Previous reading string (may be empty)
        actual: Actual reading string (may be empty)
    """

    title: str
    country: str
    time: datetime
    impact: str
    forecast: str = ""
    previous: str = ""
    actual: str = ""


@dataclass(frozen=True)
class NewsCheckResult:
    """Result of a news blackout window check.

    Attributes:
        is_blocked: True if trading should be blocked
        blocking_event: The event causing the block (if any)
        minutes_to_event: Minutes until / since the blocking event
        reason: Human-readable explanation
    """

    is_blocked: bool
    blocking_event: NewsEvent | None = None
    minutes_to_event: int | None = None
    reason: str = ""


class NewsClient:
    """Fetches and caches economic calendar events.

    Primary source: FCS API (https://fcsapi.com)
    Fallback: Forex Factory HTML scraping
    Fail-safe: block trading on any API error / missing key
    """

    _cache_ttl: int = 300  # seconds

    def __init__(self, api_key: str | None = None) -> None:
        """Initialise the client.

        Args:
            api_key: FCS API key. If None, reads FCS_API_KEY from environment.
        """
        self._api_key: str | None = api_key or os.getenv("FCS_API_KEY")
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "SMCSignalBot/1.0"})
        self._cache: dict[str, tuple[datetime, list[NewsEvent]]] = {}
        self.logger = logger.bind(module="news_client")

        if not self._api_key:
            self.logger.error(
                "fcs_api_key_missing",
                hint="Set FCS_API_KEY in .env",
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_upcoming_events(self, hours_ahead: int = 4) -> list[NewsEvent]:
        """Fetch economic events for the next ``hours_ahead`` hours.

        Queries FCS API for today's events, filters high/medium impact,
        and returns those scheduled within the requested window.

        Args:
            hours_ahead: How many hours into the future to look.

        Returns:
            Sorted list of NewsEvent objects (chronological).
        """
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        cache_key = f"{today}|{_DEFAULT_COUNTRIES}"

        events = self._get_cached_or_fetch(cache_key, today)
        cutoff = datetime.now(tz=timezone.utc) + timedelta(hours=hours_ahead)

        return sorted(
            [e for e in events if e.time <= cutoff],
            key=lambda e: e.time,
        )

    def get_events_for_pairs(self, pairs: list[str]) -> list[NewsEvent]:
        """Fetch events relevant to the given instrument pairs.

        Args:
            pairs: List of instrument codes, e.g. ["EUR_USD", "XAU_USD"].

        Returns:
            Deduplicated, chronologically sorted list of NewsEvent objects.
        """
        relevant_countries: set[str] = set()
        for pair in pairs:
            relevant_countries.update(PAIR_COUNTRY_MAP.get(pair, []))

        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        cache_key = f"{today}|{_DEFAULT_COUNTRIES}"
        all_events = self._get_cached_or_fetch(cache_key, today)

        seen: set[tuple[str, str, datetime]] = set()
        result: list[NewsEvent] = []
        for event in all_events:
            if event.country not in relevant_countries:
                continue
            dedup_key = (event.title, event.country, event.time)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            result.append(event)

        return sorted(result, key=lambda e: e.time)

    def is_news_blocked(
        self,
        pair: str,
        window_minutes: int = 120,
    ) -> NewsCheckResult:
        """Check whether trading is blocked due to an upcoming/recent event.

        Fail-safe: returns ``is_blocked=True`` on any error or missing key.

        Args:
            pair: Instrument code, e.g. "EUR_USD".
            window_minutes: Blackout radius in minutes (±window around event).

        Returns:
            NewsCheckResult describing block status.
        """
        if not self._api_key:
            self.logger.error(
                "news_blocked_no_api_key",
                pair=pair,
            )
            return NewsCheckResult(
                is_blocked=True,
                reason="No FCS_API_KEY configured — fail-safe block",
            )

        try:
            events = self.get_events_for_pairs([pair])
        except requests.Timeout:
            self.logger.warning(
                "news_api_timeout",
                pair=pair,
            )
            return NewsCheckResult(
                is_blocked=True,
                reason="FCS API timeout — fail-safe block",
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "news_api_error",
                pair=pair,
                error=str(exc),
            )
            return NewsCheckResult(
                is_blocked=True,
                reason=f"FCS API error — fail-safe block: {exc}",
            )

        now = datetime.now(tz=timezone.utc)
        for event in events:
            event_time = event.time
            if not event_time.tzinfo:
                event_time = event_time.replace(tzinfo=timezone.utc)

            diff = (event_time - now).total_seconds() / 60.0
            if -window_minutes <= diff <= window_minutes:
                minutes_to = int(diff)
                self.logger.warning(
                    "news_window_blocked",
                    pair=pair,
                    event_title=event.title,
                    minutes_to_event=minutes_to,
                )
                return NewsCheckResult(
                    is_blocked=True,
                    blocking_event=event,
                    minutes_to_event=minutes_to,
                    reason=(
                        f"{event.impact.upper()} impact event '{event.title}'"
                        f" in {minutes_to} min"
                    ),
                )

        self.logger.info(
            "news_window_clear",
            pair=pair,
            events_checked=len(events),
        )
        return NewsCheckResult(is_blocked=False)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_cached_or_fetch(
        self,
        cache_key: str,
        today: str,
    ) -> list[NewsEvent]:
        """Return cached events or fetch fresh ones.

        Falls back to Forex Factory scraping when the FCS API is unavailable.

        Args:
            cache_key: Cache dictionary key.
            today: Date string YYYY-MM-DD for API query.

        Returns:
            List of NewsEvent objects (may be empty on total failure).
        """
        now = datetime.now(tz=timezone.utc)

        cached = self._cache.get(cache_key)
        if cached is not None:
            cached_at, events = cached
            age = (now - cached_at).total_seconds()
            if age < self._cache_ttl:
                self.logger.debug(
                    "news_cache_hit",
                    cache_key=cache_key,
                    age_seconds=int(age),
                )
                return events

        events = self._fetch_from_fcs(today)
        if not events:
            self.logger.warning(
                "fcs_fetch_empty_trying_fallback",
                today=today,
            )
            events = self._fallback_forex_factory()

        self._cache[cache_key] = (now, events)
        return events

    def _fetch_from_fcs(self, today: str) -> list[NewsEvent]:
        """Fetch events from FCS API.

        Args:
            today: Date string YYYY-MM-DD.

        Returns:
            Parsed list of NewsEvent objects; empty list on error.
        """
        if not self._api_key:
            return []

        params: dict[str, str] = {
            "country": _DEFAULT_COUNTRIES,
            "from": today,
            "to": today,
            "access_key": self._api_key,
        }

        try:
            resp = self._session.get(
                _FCS_BASE_URL,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            events = self._parse_fcs_response(data)
            self.logger.info(
                "fcs_fetch_success",
                today=today,
                events_count=len(events),
            )
            return events

        except requests.Timeout:
            self.logger.warning("fcs_api_timeout", today=today)
            raise
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            self.logger.error(
                "fcs_http_error",
                status=status,
                today=today,
            )
            return []
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "fcs_unexpected_error",
                error=str(exc),
                today=today,
            )
            return []

    def _parse_fcs_response(self, data: dict[str, Any]) -> list[NewsEvent]:
        """Parse FCS API JSON response into NewsEvent objects.

        Filters out low-impact events. Skips malformed entries with a warning.

        Args:
            data: Decoded JSON dict from FCS API.

        Returns:
            List of NewsEvent objects (high/medium impact only).
        """
        raw_events: list[dict[str, Any]] = data.get("response", [])
        if not isinstance(raw_events, list):
            self.logger.warning(
                "fcs_unexpected_response_format",
                type=type(raw_events).__name__,
            )
            return []

        results: list[NewsEvent] = []
        for raw in raw_events:
            try:
                impact_raw: str = str(raw.get("impact", "")).strip().lower()
                if impact_raw not in _ALLOWED_IMPACT_LABELS:
                    continue

                impact_normalised = (
                    "high" if impact_raw in _HIGH_IMPACT_LABELS else "medium"
                )

                country_code: str = str(raw.get("country", "")).strip().upper()
                country = FCS_COUNTRY_CODE_MAP.get(country_code, country_code.lower())

                date_str: str = str(raw.get("date", "")).strip()
                time_str: str = str(raw.get("time", "")).strip()
                event_dt = _parse_fcs_datetime(date_str, time_str)
                if event_dt is None:
                    self.logger.warning(
                        "fcs_datetime_parse_failed",
                        date=date_str,
                        time=time_str,
                        title=raw.get("title"),
                    )
                    continue

                results.append(
                    NewsEvent(
                        title=str(raw.get("title", "")).strip(),
                        country=country,
                        time=event_dt,
                        impact=impact_normalised,
                        forecast=str(raw.get("forecast", "") or "").strip(),
                        previous=str(raw.get("previous", "") or "").strip(),
                        actual=str(raw.get("actual", "") or "").strip(),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "fcs_event_parse_error",
                    error=str(exc),
                    raw=raw,
                )
                continue

        return results

    def _fallback_forex_factory(self) -> list[NewsEvent]:
        """Scrape Forex Factory calendar as fallback.

        Called when FCS API returns no events or fails entirely.
        Parses the HTML table and returns high/medium impact events for today.

        Returns:
            List of NewsEvent objects; empty list on scraping failure.
        """
        self.logger.warning(
            "news_fallback_forex_factory",
            url=_FOREX_FACTORY_URL,
        )
        try:
            resp = self._session.get(
                _FOREX_FACTORY_URL,
                timeout=10,
                headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            resp.raise_for_status()
            return self._parse_forex_factory_html(resp.text)

        except requests.Timeout:
            self.logger.warning("forex_factory_timeout")
            return []
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "forex_factory_scrape_error",
                error=str(exc),
            )
            return []

    def _parse_forex_factory_html(self, html: str) -> list[NewsEvent]:
        """Parse Forex Factory calendar HTML into NewsEvent objects.

        Args:
            html: Raw HTML response body.

        Returns:
            List of high/medium impact NewsEvent objects for today.
        """
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="calendar__table")
        if table is None:
            self.logger.warning("forex_factory_table_not_found")
            return []

        results: list[NewsEvent] = []
        today = datetime.now(tz=timezone.utc).date()
        current_time: datetime | None = None

        rows = table.find_all("tr", class_=lambda c: c and "calendar__row" in c)
        for row in rows:
            try:
                time_cell = row.find("td", class_="calendar__time")
                if time_cell and time_cell.get_text(strip=True):
                    time_text = time_cell.get_text(strip=True)
                    current_time = _parse_ff_time(time_text, today)

                impact_cell = row.find("td", class_="calendar__impact")
                if impact_cell is None:
                    continue
                impact_icon = impact_cell.find("span")
                if impact_icon is None:
                    continue
                impact_class = " ".join(impact_icon.get("class", []))

                if "red" in impact_class:
                    impact_normalised = "high"
                elif "orange" in impact_class:
                    impact_normalised = "medium"
                else:
                    continue

                currency_cell = row.find("td", class_="calendar__currency")
                if currency_cell is None:
                    continue
                currency = currency_cell.get_text(strip=True).upper()
                country = _ff_currency_to_country(currency)

                title_cell = row.find("td", class_="calendar__event")
                title = title_cell.get_text(strip=True) if title_cell else ""

                forecast_cell = row.find("td", class_="calendar__forecast")
                forecast = forecast_cell.get_text(strip=True) if forecast_cell else ""

                previous_cell = row.find("td", class_="calendar__previous")
                previous = previous_cell.get_text(strip=True) if previous_cell else ""

                actual_cell = row.find("td", class_="calendar__actual")
                actual = actual_cell.get_text(strip=True) if actual_cell else ""

                if current_time is None:
                    continue

                results.append(
                    NewsEvent(
                        title=title,
                        country=country,
                        time=current_time,
                        impact=impact_normalised,
                        forecast=forecast,
                        previous=previous,
                        actual=actual,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "forex_factory_row_parse_error",
                    error=str(exc),
                )
                continue

        self.logger.info(
            "forex_factory_parsed",
            events_count=len(results),
        )
        return results


# ------------------------------------------------------------------
# Module-level parsing helpers
# ------------------------------------------------------------------


def _parse_fcs_datetime(date_str: str, time_str: str) -> datetime | None:
    """Parse FCS API date + time strings into a UTC datetime.

    FCS uses date format ``MM-DD-YYYY`` and time ``HH:MMam/pm``.

    Args:
        date_str: e.g. "03-07-2026"
        time_str: e.g. "08:30am"

    Returns:
        Timezone-aware UTC datetime, or None if parsing fails.
    """
    if not date_str or not time_str:
        return None
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %H:%M")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_ff_time(time_text: str, date: Any) -> datetime | None:
    """Parse Forex Factory time string for a given date.

    Args:
        time_text: e.g. "8:30am" or "All Day"
        date: datetime.date for today.

    Returns:
        Timezone-aware UTC datetime, or None for unparseable strings.
    """
    time_text = time_text.strip().lower()
    if not time_text or time_text in ("all day", "tentative"):
        return None
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            t = datetime.strptime(time_text, fmt)
            return datetime(
                date.year, date.month, date.day,
                t.hour, t.minute, tzinfo=timezone.utc,
            )
        except ValueError:
            continue
    return None


def _ff_currency_to_country(currency: str) -> str:
    """Map Forex Factory currency code to normalised country name.

    Args:
        currency: e.g. "USD", "EUR"

    Returns:
        Normalised country name used in PAIR_COUNTRY_MAP.
    """
    mapping = {
        "USD": "united-states",
        "EUR": "eurozone",
        "GBP": "united-kingdom",
        "CHF": "switzerland",
        "AUD": "australia",
        "JPY": "japan",
        "CAD": "canada",
        "NZD": "new-zealand",
    }
    return mapping.get(currency.upper(), currency.lower())
