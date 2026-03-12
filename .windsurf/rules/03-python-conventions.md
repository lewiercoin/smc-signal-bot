# Python Conventions
*Windsurf Rule: Always On*

## Wersja i środowisko
```
Python 3.11+ (projekt działa na 3.13.1)
Środowisko: venv w katalogu .venv/
Plik zależności: requirements.txt (brak pyproject.toml)
```

## Formatowanie i linting
```bash
ruff check .          # linting
ruff format .         # formatowanie (zastępuje black)
mypy .                # type checking (NIE mypy src/ — brak katalogu src/)
pytest tests/ -x      # testy, stop na pierwszym błędzie
```
Uruchom te cztery komendy przed każdym commitem. Jeśli cokolwiek failuje — napraw zanim commit.

## Styl kodu
- Type hints na WSZYSTKICH sygnaturach funkcji (parametry + return type)
- Google-style docstringi dla funkcji publicznych
- Early returns zamiast zagnieżdżonych if-else (max 2 poziomy zagłębienia)
- Brak `print()` do debugowania — używaj `structlog`
- Brak globalnego mutable state
- Dataclasses lub Pydantic dla obiektów danych (Setup, Candle, Signal)

## Przykład poprawnego stylu
```python
from dataclasses import dataclass
from datetime import datetime
import structlog

logger = structlog.get_logger()

@dataclass(frozen=True)
class Candle:
    """Faktyczna definicja: connectors/oanda_client.py"""
    instrument: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

def validate_candle(candle: Candle) -> bool:
    """Sprawdza czy świeca spełnia kontrakty danych.
    
    Args:
        candle: Obiekt świecy do walidacji.
        
    Returns:
        True jeśli świeca jest poprawna, False w przeciwnym razie.
    """
    if candle.high < candle.low:
        logger.warning("candle_invalid", reason="high < low", ts=candle.timestamp)
        return False
    if candle.close <= 0 or candle.open <= 0:
        logger.warning("candle_invalid", reason="non_positive_price", ts=candle.timestamp)
        return False
    return True
```

## Kontrakty danych — kluczowe obiekty
Zawsze używaj tych struktur, nie luźnych słowników:

```python
# Candle — kontrakt wejściowy (connectors/oanda_client.py)
@dataclass(frozen=True)
class Candle:
    instrument: str          # "EUR_USD" / "XAU_USD" / "BTC_USD"
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

# Signal — wyjście pipeline (engine/signal_generator.py)
# Nie używaj klasy Setup — została zastąpiona przez Signal
@dataclass
class Signal:
    id: str                  # UUID string, np. str(uuid.uuid4())
    pair: str
    direction: str           # "bullish" / "bearish" (lowercase!)
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    position_size: float    # lots
    confluence_score: int   # 0-110
    risk_reward_ratio: float
    created_at: datetime
```

## Testowanie
```
tests/
├── test_data_quality.py   # DQ validators (spread, news, candle count)
├── test_swing_detector.py # swing detection + ATR-adaptive length
├── test_ob_detector.py    # order block detection
├── test_fvg_detector.py   # fair value gap detection
├── test_confluence_scorer.py  # scoring + absorption
├── test_risk_engine.py    # SL/TP, sizing, spread gate
├── test_signal_generator.py  # end-to-end pipeline
├── test_telegram_bot.py   # bot send/recv
├── test_analyzer.py       # paper trading analyzer
└── integration/           # full pipeline integration tests (22+)
    ├── conftest.py
    ├── test_pipeline.py
    └── test_signal_flow.py
```
Łączna liczba testów: 381 (po Tygodniu 8).

## Async i scheduler
- Telegram bot: `async` z `asyncio`
- APScheduler z `AsyncIOScheduler`
- Funkcje I/O (OANDA API, baza, Anthropic API): `def` (synchroniczne — connectors i db nie są async)
- CPU-bound (obliczenia SMC): `def` (synchroniczne)
- **UWAGA**: `oanda_client.py`, `news_client.py`, `database.py` — wszystkie synchroniczne. Nie refaktoruj na `async def` bez decyzji architektonicznej.

## Zmienne środowiskowe — nigdy w kodzie
```python
# ✓ DOBRZE
import os
OANDA_API_KEY = os.environ["OANDA_API_KEY"]

# ✗ ŹLE — nigdy
OANDA_API_KEY = "abc123xyz"
```
Plik `.env` w korzeniu projektu (w `.gitignore`). `python-dotenv` do ładowania.
