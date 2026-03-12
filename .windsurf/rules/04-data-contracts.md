# Data Contracts — Schemat Bazy i Źródła Danych
*Windsurf Rule: Always On*

## Źródła danych — co z czego (nie negocjowalny podział)

| Dane | Źródło | Uwagi |
|------|--------|-------|
| OHLCV EUR/USD, XAU/USD | OANDA REST API v20 | tick volume, nie prawdziwy |
| OHLCV BTC/USD | CCXT → Binance | prawdziwy wolumen |
| Kalendarz ekonomiczny | Finnhub `/calendar/economic` | NIE JBlanked (1 req/day limit) |
| Backup kalendarza | ForexFactory RSS | fallback gdy Finnhub niedostępny |
| DXY | Alpha Vantage | 25 req/day, cache agresywnie |
| Nagłówki newsów FX | Finnhub `/news?category=forex` | klasyfikacja przez LLM, NIE sentiment API |
| Nagłówki newsów Crypto | Finnhub `/news?category=crypto` | j.w. |
| Absorption wolumen BTC | Binance WebSocket aggTrades | przez CCXT |
| COT (futures positioning) | CFTC API | FAZA 2 — nie MVP |
| Fear & Greed BTC | alternative.me | FAZA 2 — nie MVP |

## Schemat SQLite — tabele (plik: db/schema.sql)

### signals (główna tabela) — stan po migracji Tydzień 7
```sql
CREATE TABLE signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_uuid         TEXT,                   -- UUID z Signal.id (Strategy A fix T7)
    instrument          TEXT NOT NULL,          -- EUR_USD / XAU_USD / BTC_USD
    direction           TEXT NOT NULL,          -- bullish / bearish (lowercase!)
    entry_price         REAL,
    sl_price            REAL,
    tp1_price           REAL,
    tp2_price           REAL,
    tp3_price           REAL,
    confluence_score    REAL,
    risk_reward         REAL,
    lot_size            REAL,
    atr_at_entry        REAL,
    status              TEXT DEFAULT 'OPEN',    -- OPEN / sent / closed
    closed_price        REAL,
    pnl_r               REAL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
**KRYTYCZNE**: `Signal.id` to UUID string (np. `"a1b2c3d4-..."`), NIE integer.
Do szukania po UUID: `db.get_signal_by_uuid(uuid)` i `db.update_signal_status_by_uuid(uuid, ...)`.
NIGDY nie łącz `Signal.id` z `signals.id` (INTEGER PK) — to inny typ.

### economic_events
```sql
CREATE TABLE economic_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date  TIMESTAMP NOT NULL,
    currency    TEXT NOT NULL,              -- USD / EUR / XAU / BTC
    event_name  TEXT NOT NULL,
    impact      TEXT NOT NULL,             -- HIGH / MEDIUM / LOW
    source      TEXT,                      -- finnhub / forexfactory_rss
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_economic_events_date ON economic_events(event_date);
CREATE INDEX idx_economic_events_currency ON economic_events(currency, impact);
```

### ob_quality_log
```sql
CREATE TABLE ob_quality_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER REFERENCES signals(id),
    bar_ratio   REAL,
    rvr_ratio   REAL,
    bar_passed  BOOLEAN,
    rvr_passed  BOOLEAN,
    final_result TEXT   -- WIN / LOSS / BE (uzupełniaj po zamknięciu)
);
-- Cel: kalibracja progów BAR/RVR na podstawie danych historycznych
```

### optimizer_log [GROK-3]
```sql
CREATE TABLE optimizer_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_date       DATE NOT NULL,
    signals_analyzed    INTEGER,
    win_rate_period     REAL,
    avg_r_period        REAL,
    llm_response        TEXT,      -- pełny JSON od Agenta 5
    change_1_param      TEXT,
    change_1_current    TEXT,
    change_1_proposed   TEXT,
    change_1_accepted   BOOLEAN,
    change_2_param      TEXT,
    change_2_current    TEXT,
    change_2_proposed   TEXT,
    change_2_accepted   BOOLEAN,
    operator_notes      TEXT,
    applied_at          TIMESTAMP
);
```

### candles (cache świec)
```sql
CREATE TABLE candles (
    ts_utc          TIMESTAMP NOT NULL,
    instrument      TEXT NOT NULL,
    tf              TEXT NOT NULL,     -- 1H / 4H / 1D / 1W
    open            REAL, high REAL, low REAL, close REAL,
    volume          REAL,
    source          TEXT,
    quality_flags   TEXT,              -- JSON
    PRIMARY KEY (ts_utc, instrument, tf)
);
```

## DQ — walidacja danych (dq/validators.py)
Każda świeca musi przejść walidację zanim trafi do detektorów SMC:
```python
def validate_candle(c: Candle) -> tuple[bool, str]:
    if c.high < c.low:         return False, "high < low"
    if c.close <= 0:           return False, "non_positive_close"
    if c.open <= 0:            return False, "non_positive_open"
    if c.high < c.close:       return False, "high < close"
    if c.low > c.close:        return False, "low > close"
    if c.high < c.open:        return False, "high < open"
    if c.low > c.open:         return False, "low > open"
    return True, "ok"
```
Odrzucone świece: loguj do pliku, nie rzucaj wyjątku, pomijaj w obliczeniach.

## Rate limits i cache
- OANDA: max 120 req/min → cache OHLCV w tabeli `candles`, odświeżaj tylko po nowej świecy
- Alpha Vantage: 25 req/day → cache DXY na 4h, nie odpytuj częściej
- Finnhub: 60 req/min (free) → bezpieczne, ale cache kalendarza na 4h
- Anthropic API: bez twardego limitu, ale każde wywołanie kosztuje → gate na score ≥60
