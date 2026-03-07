# Python Conventions
*Windsurf Rule: Always On*

## Wersja i środowisko
```
Python 3.11 (dokładnie — nie 3.12, nie 3.10)
Środowisko: venv w katalogu .venv/
Plik zależności: pyproject.toml (nie requirements.txt)
```

## Formatowanie i linting
```bash
ruff check .          # linting
ruff format .         # formatowanie (zastępuje black)
mypy src/             # type checking
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

@dataclass
class Candle:
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    source: str
    tf: str
    instrument: str
    quality_flags: dict

def validate_candle(candle: Candle) -> bool:
    """Sprawdza czy świeca spełnia kontrakty danych.
    
    Args:
        candle: Obiekt świecy do walidacji.
        
    Returns:
        True jeśli świeca jest poprawna, False w przeciwnym razie.
    """
    if candle.high < candle.low:
        logger.warning("candle_invalid", reason="high < low", ts=candle.ts_utc)
        return False
    if candle.close <= 0 or candle.open <= 0:
        logger.warning("candle_invalid", reason="non_positive_price", ts=candle.ts_utc)
        return False
    return True
```

## Kontrakty danych — kluczowe obiekty
Zawsze używaj tych struktur, nie luźnych słowników:

```python
# Candle — kontrakt wejściowy
@dataclass
class Candle:
    ts_utc: datetime
    open: float; high: float; low: float; close: float
    volume: float | None
    source: str; tf: str; instrument: str
    quality_flags: dict

# Setup — obiekt przekazywany przez cały pipeline
@dataclass
class Setup:
    instrument: str
    direction: str            # "LONG" | "SHORT"
    htf_bias: str             # "bull" | "bear" | "neutral"
    ipda_percent: float       # 0.0–100.0
    smc_tags: list[str]       # ["PPDD", "OB", "FVG", "BOS", ...]
    ob_zone: dict             # {"top": float, "bottom": float, "ts": datetime}
    fvg_zone: dict | None
    confluence_score: int     # 0–110
    bar_ratio: float
    rvr_ratio: float
    absorption_detected: bool
    absorption_type: str      # "BULLISH" | "BEARISH" | "NEUTRAL" | "NONE"
    swing_length_used: int
    volatility_regime: str    # "NORMAL" | "HIGH" | "EXTREME" | "LOW" | "FLAT"
    detected_at_utc: datetime
    expires_at_utc: datetime
    news_risk: str            # "LOW" | "MEDIUM" | "HIGH"
```

## Testowanie
```
tests/
├── test_dq.py              # walidacja danych
├── test_smc_detector.py    # detekcja OB, FVG, BoS
├── test_confluence.py      # scoring
├── test_risk.py            # SL/TP, sizing
└── test_no_lookahead.py    # KRYTYCZNY: brak look-ahead w backteście
```
Każdy moduł w `smc/` i `utils/` ma odpowiadający plik testowy.
Test no-lookahead: wszystkie cechy liczone TYLKO z danych dostępnych przed `detected_at_utc`.

## Async i scheduler
- Telegram bot: `async` z `asyncio`
- APScheduler z `AsyncIOScheduler`
- Funkcje I/O (OANDA API, baza, Anthropic API): `async def`
- CPU-bound (obliczenia SMC): `def` (synchroniczne — nie blokują event loop)

## Zmienne środowiskowe — nigdy w kodzie
```python
# ✓ DOBRZE
import os
OANDA_API_KEY = os.environ["OANDA_API_KEY"]

# ✗ ŹLE — nigdy
OANDA_API_KEY = "abc123xyz"
```
Plik `.env` w korzeniu projektu (w `.gitignore`). `python-dotenv` do ładowania.
