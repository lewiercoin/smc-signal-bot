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

### signals (główna tabela)
```sql
CREATE TABLE signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument          TEXT NOT NULL,          -- EUR_USD / XAU_USD / BTC_USD
    direction           TEXT NOT NULL,          -- LONG / SHORT
    setup_type          TEXT NOT NULL,          -- PPDD_OB / FVG_STACKED / BF_1H
    
    -- Strefy cenowe
    ob_top              REAL, ob_bottom REAL,
    fvg_top             REAL, fvg_bottom REAL,
    entry_price         REAL,
    sl_price            REAL,
    tp1_price           REAL, tp2_price REAL, tp3_price REAL,
    
    -- Scoring
    confluence_score    INTEGER,
    ipda_percent        REAL,
    session_bucket      TEXT,                   -- LONDON / NY / OVERLAP / ASIA / OTHER
    
    -- OB Quality [v2.1]
    bar_ratio           REAL,
    rvr_ratio           REAL,
    ob_tap_count        INTEGER DEFAULT 0,
    ob_status           TEXT DEFAULT 'ACTIVE',  -- ACTIVE / EXHAUSTED
    
    -- Absorption [GROK-1 v2.2]
    absorption_detected BOOLEAN DEFAULT FALSE,
    absorption_type     TEXT DEFAULT 'NONE',    -- BULLISH / BEARISH / NEUTRAL / NONE
    absorption_strength TEXT DEFAULT 'NONE',    -- STRONG / MODERATE / NONE
    
    -- Dynamic Swing [GROK-2 v2.2]
    swing_length_used   INTEGER,
    volatility_regime   TEXT,                   -- NORMAL / HIGH / EXTREME / LOW / FLAT
    
    -- AI Agenci
    agent1_score        INTEGER,
    agent1_recommendation TEXT,
    agent2_sentiment    TEXT,                   -- POZYTYWNY / NEGATYWNY / MIESZANY
    agent4_message      TEXT,
    llm_provider        TEXT DEFAULT 'claude_haiku',  -- claude_haiku / ollama_llama3 / template_fallback
    
    -- Trigger
    trigger_tf          TEXT DEFAULT '1H',      -- CHoCH_1H / BF_1H / 4H_direct
    
    -- Wyniki
    status              TEXT DEFAULT 'OPEN',    -- OPEN / TP1 / TP2 / TP3 / SL / BE / EXPIRED
    result_r            REAL,                   -- wynik w R (np. 1.5, -1.0, 0.0)
    
    -- Timestamps
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at           TIMESTAMP,
    expires_at          TIMESTAMP,
    
    -- Compliance
    news_risk           TEXT DEFAULT 'LOW',
    published_to_tg     BOOLEAN DEFAULT FALSE
);
```

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
