# CONTEXT.md — SMC Signal Bot
# Źródło prawdy dla Cascade. Czytaj ten plik na początku każdej sesji.
# Ostatnia aktualizacja: marzec 2026
# Wersja specyfikacji: v2.2 + ITS v1.0

---

## 1. Co to jest i po co powstało

Bot sygnałowy Telegram publikujący sygnały tradingowe oparte o strategię
**Smart Money Concepts** (makuchaku/ICT). Trzy instrumenty: EUR/USD, XAU/USD, BTC/USD.

Projekt przeszedł przez 5 audytów LLM (DeepSeek ×2, ChatGPT/Gemini, Grok, ITS v1.0).
Każdy audit wniósł coś innego. Finalna specyfikacja to synteza wszystkich — v2.2.

Użytkownik jest mechanikiem przemysłowym z doświadczeniem w automatyce,
uczy się programowania, buduje bota jako projekt poboczny. **Zaczyna od zera kodu.**

---

## 2. Architektura — 8 warstw (ITS v1.0)

```
Warstwa 0: Connectors      (OANDA, CCXT/Binance, Finnhub, Alpha Vantage)
Warstwa 1: Data Quality    (validators.py — ZAWSZE przed SMC)
Warstwa 2: Feature Store   (SMC + volatility + flow proxy)
Warstwa 3: Setup Engine    (generuje kandydatów setupów)
Warstwa 4: Confluence Gate (scoring 0–110 pkt, progi 60/70)
Warstwa 5: AI Agenci       (4 agentów + Optimizer tygodniowy)
Warstwa 6: Risk Engine     (sizing, SL/TP, circuit breakers)
Warstwa 7: Telegram + DB   (publikacja + SQLite journal)
```

Pipeline: dane → DQ → features → setups → scoring → agenci → risk → Telegram

---

## 3. Stack techniczny (zamrożony — nie proponuj alternatyw)

| Co | Czym | Powód |
|----|------|-------|
| Język | Python 3.11 | stabilna wersja, wszystkie biblioteki wspierają |
| Baza | SQLite → PostgreSQL | SQLite wystarczy do ~500 sig/mc |
| Broker | OANDA REST API v20 (oandapyV20) | darmowe demo, dobra dokumentacja |
| Crypto | CCXT → Binance | prawdziwy wolumen dla BTC |
| AI | Claude Haiku (anthropic SDK) | ~$3–8/mc, najlepszy stosunek cena/jakość |
| Fallback AI | Ollama llama3:8b → template | gdy API niedostępne |
| Scheduler | APScheduler | prosty, sprawdzony |
| Telegram | python-telegram-bot | webhook na prod, polling na dev |
| Linting | ruff + mypy | szybkie, nowoczesne |
| Testy | pytest + pytest-asyncio | standard |
| Logging | structlog | strukturowane logi, nie print() |
| Config | YAML per instrument | parametry NIGDY hardkodowane |

---

## 4. Trzy kluczowe mechanizmy dodane z audytu Groka

### [GROK-1] Absorption Detection (`smc/absorption.py`)
Identyfikuje świece gdzie instytucja absorbuje podaż/popyt.
Warunek: `body_ratio ≤ threshold AND volume_ratio ≥ threshold`
`body_ratio = abs(close - open) / (high - low)`

Progi per instrument:
- EUR/USD, XAU/USD: body_max=0.30, vol_min=1.5
- BTC/USD: body_max=0.25, vol_min=2.0 (bardziej restrykcyjny)

Wpływ na scoring:
- Absorption zgodna STRONG: +5 pkt (BTC) / +3 pkt (Forex)
- Absorption zgodna MODERATE: +3 pkt (BTC) / +2 pkt (Forex)
- Absorption SPRZECZNA: **-8 pkt** (override ostrzegawczy)

⚠️ WAŻNE: Dla EUR/USD i XAU/USD OANDA daje tick volume (proxy).
Dla BTC/USD używaj prawdziwego wolumenu z Binance przez CCXT.

### [GROK-2] Dynamic swing_length (`smc/swing_detector.py`)
ATR-adaptive. Trzy wartości: 7 (low vol), 10 (base), 14 (high vol).

Progi ATR:
- HIGH volatility (ATR ratio > 1.5× avg): swing_length = 14
- NORMAL (0.7×–1.5×): swing_length = 10 (baza)
- LOW volatility (ATR ratio < 0.7× avg): swing_length = 7

Stabilność: zmiana następuje dopiero gdy nowa wartość utrzymuje się przez **3 kolejne świece**.
ATR ratio = bieżący ATR(14) / średnia historyczna ATR z ostatnich 50 świec.
Loguj `swing_length_used` i `volatility_regime` do tabeli `signals`.

### [GROK-3] Weekly Optimizer Agent (`agents/optimizer_agent.py`)
Uruchamia się co niedzielę o 20:00 UTC przez APScheduler.
Analizuje ostatnie 4 tygodnie z SQLite, wysyła do Claude Haiku,
otrzymuje JSON z propozycjami zmian parametrów.
**NIGDY nie wdraża automatycznie** — wysyła raport na prywatny kanał TG operatora.
Operator akceptuje lub odrzuca. Historia w tabeli `optimizer_log`.
Minimum 10 zamkniętych sygnałów żeby analiza miała sens.

---

## 5. Confluence Scoring — pełna tabela (maks. 110 pkt)

```
PPDD OB in discount/premium         +25 pkt   (wszystkie instrumenty)
FVG overlap na OB (stacked)         +20 pkt
HTF BoS Daily potwierdzony          +15 pkt
Liquidity sweep przed setupem       +15 pkt
Sesja (London/NY, z mnożnikiem)     +10 pkt
DXY alignment                       +10 (EUR) / +5 (XAU) / +0-5 (BTC)
Brak news <2h HIGH impact           +5 (EUR) / +10 (XAU) / +10 (BTC)
FVG Bias licznik HTF                +5 pkt
Absorption STRONG zgodna            +5 (BTC) / +3 (Forex)    [GROK-1]
Absorption MODERATE zgodna          +3 (BTC) / +2 (Forex)    [GROK-1]
Absorption SPRZECZNA                -8 pkt (override)         [GROK-1]
Multiple Taps ≥3 bez reakcji        -10 do -25 pkt
IPDA 40-60%                         → hard reject (nie score, blokada)
```

**Progi decyzyjne:**
- score < 60 → odrzuć, nie wywołuj agentów
- score ≥ 60 → wywołaj AI Agentów 1–4
- score ≥ 65 → opublikuj na kanale Telegram

---

## 6. IPDA — strefy i reguły

```python
# Wzór:
ipda_percent = (close - period_low) / (period_high - period_low) * 100

# Decyzja:
if ipda_percent <= 40:   direction_allowed = "LONG"   # Discount
elif ipda_percent >= 60: direction_allowed = "SHORT"  # Premium
else:                    return "SKIP"                 # 40-60% = blokada
```

Period = ostatnia kompletna świeca Weekly (lub Daily z configu `bias` timeframe).

---

## 7. Sesje tradingowe i mnożniki

| Sesja | UTC | Mnożnik | Efektywny próg |
|-------|-----|---------|----------------|
| London open | 08:00–10:00 | 1.0× | 65 |
| NY open | 13:00–15:00 | 1.0× | 65 |
| London-NY overlap | 13:00–17:00 | 0.9× | 59 (obniżony!) |
| Azja | 00:00–07:00 | 1.25× | 88 (blokuj) |
| Weekend | — | ∞ | blokuj całkowicie |

---

## 8. OB Quality Filters (z audytu ChatGPT/Gemini)

**Body-ATR Ratio (BAR):**
```python
BAR = abs(impulse_close - impulse_open) / ATR(14)
# Progi: EUR=1.2, XAU=1.5, BTC=1.8
# Jeśli BAR < próg → OB oznacz jako "NOISE OB", score=0
```

**Relative Volume Ratio (RVR):**
```python
RVR = candle_volume / SMA(Volume, 20)
# Progi: EUR/XAU=1.5, BTC=2.0
```

**Multiple Taps Rule:**
Jeśli OB testowany ≥3 razy bez silnego odrzutu → status = EXHAUSTED, score = 0.
Śledź `ob_tap_count` i `ob_status` w tabeli `signals`.

---

## 9. Risk Management

**Stop Loss (ATR-buffered):**
```python
k = {"EUR_USD": 0.5, "XAU_USD": 0.75, "BTC_USD": 1.0}
SL_LONG  = ob_low  - (k[instrument] × ATR(14))
SL_SHORT = ob_high + (k[instrument] × ATR(14))
```

**Take Profit (R-based):**

| Instrument | TP1 | TP2 | TP3 |
|-----------|-----|-----|-----|
| EUR/USD | 1.5R (50%) | 2.5R (25%) | 4.0R (25%) |
| XAU/USD | 1.8R (50%) | 3.5R (25%) | 6.0R (25%) |
| BTC/USD | 1.2R (50%) | 2.5R (25%) | **4.5R** (25%) |

⚠️ BTC TP3 = 4.5R (NIE 8R — 8R odrzucone jako nierealistyczne na 4H)

**Breakeven:**
```python
estimated_cost = rolling_median_spread_7d[current_hour] × 1.5
BE_LONG  = entry + estimated_cost
BE_SHORT = entry - estimated_cost
```

**Circuit breakers:**
- Max dzienna strata: 3–5% → stop na 24h
- Max równoległe pozycje: 3 (z correlation filter <0.75)
- Blackout: ±120 min od HIGH impact news

---

## 10. H1Monitor — trigger entry

```python
# Aktywuje się po selekcji setupu na 4H
# Co 15 minut sprawdza CHoCH_1H lub BrokenFractal_1H
# Timeout: 8h od selekcji → setup wygasa
# swing_length dla 1H = stały 15 (nie dynamiczny)
# Zwraca: trigger_type + entry_zone
```

Typy triggerów: `CHoCH_1H`, `BF_1H`, `4H_direct`
Zapisuj `trigger_tf` do tabeli `signals`.

---

## 11. AI Agenci — 4 + Optimizer

### Agent 1 — Structure Analyst (Claude Haiku, temp=0.2)
Ocenia jakość setupu: OB displacement, trapped traders, absorption context.
Wejście: JSON z setup object + absorption fields.
Wyjście: `quality_score` (1–10), `recommendation` (PUBLISH/SKIP/WATCH), `rejection_reason`.

Auto-reject gdy:
- bar_ratio < instrument threshold
- ob_tap_count ≥ 3
- ipda_percent 40–60
- impulse_close_position < 0.6 dla LONG
- session_bucket == "ASIA" bez PPDD_STRONG + HTF_BOS
- OB formowany podczas news HIGH

### Agent 2 — Fundamental Analyst (Claude Haiku, temp=0.3)
Pobiera do 5 nagłówków z Finnhub `/news?category=forex` (lub crypto dla BTC).
Klasyfikuje przez LLM: BULLISH/BEARISH/NEUTRAL per instrument.
3+ BULLISH = "POZYTYWNY", 3+ BEARISH = "NEGATYWNY", else "MIESZANY".
⚠️ NIE używa Finnhub sentiment API — nie istnieje dla FX.

### Agent 3 — Risk Verifier (deterministyczny, BEZ LLM)
Sprawdza: spread z-score, korelacja portfela, circuit breakers, sizing.
Wyjście: `risk_approved` (bool), `position_size`, `risk_notes`.

### Agent 4 — Telegram Editor (Claude Haiku, temp=0.3)
Formatuje sygnał do publikacji w stylu makuchaku.
Wyjście: gotowa wiadomość Markdown dla Telegrama.

### Agent 5 — Optimizer (Claude Haiku, temp=0.4) [GROK-3]
Uruchamiany co niedzielę. Analizuje journal, proponuje zmiany.
NIE wdraża automatycznie. Historia w `optimizer_log`.

**3-tier fallback:**
1. Claude Haiku API (default)
2. Ollama llama3:8b (API timeout >5s lub niedostępne)
3. Template deterministyczny (Ollama niedostępne)

Loguj `llm_provider` do tabeli `signals` — śledź WR per provider.

---

## 12. Dane i źródła — co z czego

| Dane | Źródło | Uwagi |
|------|--------|-------|
| OHLCV EUR/USD, XAU/USD | OANDA REST API v20 | tick volume proxy |
| OHLCV BTC/USD | CCXT → Binance | prawdziwy wolumen |
| Kalendarz ekonomiczny | Finnhub `/calendar/economic` | NIE JBlanked! |
| Backup kalendarza | ForexFactory RSS | fallback |
| DXY | Alpha Vantage | 25 req/day, cache 4h |
| Nagłówki FX/Crypto | Finnhub `/news` | klasyfikacja przez LLM |
| COT (pozycjonowanie) | CFTC API | FAZA 2, nie MVP |
| Fear & Greed BTC | alternative.me | FAZA 2, nie MVP |

**JBlanked API = ODRZUCONY.** Limit 1 request/dzień od 2026.
**Finnhub Sentiment API = NIE ISTNIEJE dla FX.** Tylko spółki US.

---

## 13. Schemat SQLite — kluczowe tabele

Pełny schemat w `db/schema.sql`. Najważniejsze pola tabeli `signals`:

```sql
-- Podstawowe
instrument, direction, setup_type, confluence_score, ipda_percent

-- OB Quality [v2.1]
bar_ratio, rvr_ratio, ob_tap_count, ob_status

-- Absorption [GROK-1 v2.2]
absorption_detected, absorption_type, absorption_strength

-- Dynamic Swing [GROK-2 v2.2]
swing_length_used, volatility_regime

-- AI i trigger
agent1_score, agent1_recommendation, agent2_sentiment
llm_provider, trigger_tf

-- Wyniki
status (OPEN/TP1/TP2/TP3/SL/BE/EXPIRED), result_r, closed_at
```

Dodatkowe tabele: `economic_events`, `ob_quality_log`, `optimizer_log`, `candles`.

---

## 14. Kluczowe odrzucone propozycje (nie wracaj do nich)

| Propozycja | Kto proponował | Dlaczego odrzucona |
|-----------|---------------|-------------------|
| XGBoost ML Gate od razu | ITS/ChatGPT, Grok | <200 sygnałów = overfitting. Wdrożyć po 6 mies. |
| PostgreSQL od razu | ChatGPT, ITS | SQLite wystarczy do ~500 sig/mc |
| CrewAI / LangGraph | Grok | Zbędny overhead dla 4–5 wywołań LLM |
| Monte Carlo w risk verifier | Grok | Latencja sekund przy każdym sygnale |
| TP3 = 8R dla BTC | DeepSeek v2.0, ITS | Nierealistyczne na 4H, wicki invalidują BE |
| MT5 API | ChatGPT | Używamy OANDA REST, MT5 niepotrzebny |
| Finnhub Sentiment API | spec v1.0 | Nie istnieje dla FX — tylko spółki US |
| JBlanked ekonomiczny | spec v1.0 | 1 req/dzień od 2026, bezużyteczny |
| swing_length=50 globalnie | spec v1.0 | Za duże opóźnienie. Per-instrument + ATR-adaptive |
| Spoofing Detection MVP | Grok | Zbyt złożone, dużo false positives |
| "Supervisor Crew" Grok | Grok | @agent triggers nie istnieją w Windsurf |

---

## 15. Stan implementacji (bieżący)

### Status tygodni

- **Tydzień 1:** ✅ UKOŃCZONY (`connectors`, `db`, `dq`)
- **Tydzień 2:** ✅ UKOŃCZONY (SMC Engine — swing ✅, structure ✅, ob ✅, fvg ✅, utils ✅, liquidity ✅)
- **Tydzień 3:** ⬜ NIE ROZPOCZĘTY (Confluence Engine + News API)

### Ukończone moduły (Tydzień 2 ✅ UKOŃCZONY)

| Moduł | Status | Testy | Commit |
|-------|--------|-------|--------|
| `connectors/oanda_client.py` | ✅ | — | — |
| `db/schema.sql` | ✅ | — | — |
| `dq/validators.py` | ✅ | — | — |
| `smc/swing_detector.py` | ✅ | 9 | — |
| `smc/structure_analyzer.py` | ✅ | 10 | `11357ee` |
| `smc/ob_detector.py` | ✅ | 11 | `640f568` |
| `smc/utils.py` | ✅ | 13 | `f2707a7` |
| `smc/fvg_detector.py` | ✅ | 10 | — |
| `smc/liquidity_detector.py` | ✅ | 12 | bieżący |

**Łączna liczba testów: 126**

### Tydzień 2 — SMC Engine UKOŃCZONY

```
- swing_detector.py     (9 testów)  — dynamic swing_length GROK-2
- structure_analyzer.py (10 testów) — BOS/CHoCH/trend
- ob_detector.py        (11 testów) — Order Blocks + quality
- fvg_detector.py       (10 testów) — Fair Value Gaps + fill %
- liquidity_detector.py (12 testów) — Liquidity Sweeps
- utils.py              (13 testów) — shared ATR (scalar + series)
```

### Następny krok: Tydzień 3 — Confluence Engine + News API

```
Moduły:
- engine/confluence_scorer.py  (scoring 110 pkt)
- connectors/news_client.py    (prawdziwe API zamiast mock calendar)
```

---

## 15b. Kluczowe decyzje techniczne (zapis audytów)

### ATR — implementacja
- `smc/utils.py` zawiera dwie funkcje ATR (bez pandas-ta, czysta implementacja):
  - `calculate_atr_scalar(candles, period)` → single float (prosta średnia TR, używana przez SwingDetector)
  - `calculate_atr_series(candles, period)` → list[float | None] (Wilder's smoothing per-candle, używana przez OrderBlockDetector)
- Obie funkcje importowane przez `smc/swing_detector.py` i `smc/ob_detector.py` — zero duplikacji ATR.

### FVG Detector — decyzje logiczne
- **FVG definition**: candle3.low > candle1.high (bullish), candle3.high < candle1.low (bearish) — ICT 3-candle imbalance
- **Gap zone**: bullish = candle1.high → candle3.low; bearish = candle3.high → candle1.low
- **fill_percentage semantics**: 0.0 = untouched, 1.0 = fully filled; wartości 0.0–0.99 = nadal valid (ICT: cena wraca wypełnić FVG)
- **Age limit**: 30 bars (nie 50 jak OB) — FVG szybciej tracą relevance
- **Size threshold**: 0.2 ATR (nie 0.3 jak OB) — FVG bywają mniejsze niż OB
- **is_valid=False** tylko przy: fill >= 1.0 (fully filled) LUB age > 30 bars
- **ATR source**: `smc.utils.calculate_atr_series` — wspólna implementacja, zero duplikacji

### OB Detector — decyzje logiczne
- **Impulse measurement**: close-to-close (body movement), NIE wick-to-wick (ICT displacement definition)
- **Touch definition**: wick wchodzi w strefę OB, close NIE przebija granicy — OB pozostaje aktywny
- **Graceful degradation**: < 14 świec → empty list + structlog warning, bez wyjątku
- **Spójność**: `is_valid=False` gwarantuje `quality.passed=False` (wymuszane w `detect()`)

### Structure Analyzer — decyzje logiczne (po audycie Claude, 2026-03-07)
- `_determine_trend()`: używa `>` (nie `>=`) — eliminuje false bullish przy równych HH/LH
- `_determine_trend()`: analizuje `swings[-4:-1]` — wyklucza ostatni swing (potencjalny CHoCH) z oceny trendu
- **Tech Debt Faza 2**: CHoCH wg ICT powinien wymagać przebicia poprzedniego SH/SL; obecna implementacja uproszczona

### Liquidity Detector — decyzje logiczne
- **Sweep definition**: wick beyond level + close back inside (ICT standard)
- **Penetration filter**: 0.05–2.0 ATR — `< 0.05` = noise, `> 2.0` = breakout (odfiltrowany przed quality)
- **Age limit**: 20 bars (najkrótszy z SMC modułów — sweepy tracą relevance szybciej niż OB=50, FVG=30)
- **Next candle confirmation**: jeśli next candle close przekracza level → `is_valid=False` (breakout potwierdzony)
- **Rejection strength**: `abs(close - level) / ATR` — próg > 0.3 (im dalej close od poziomu, tym silniejszy sweep)
- **Level tested count**: wick ±0.2 ATR od poziomu, liczone świece przed sweep (mierzą nagromadzenie liquidity)
- **Buyside sweep = bearish**: SM sprzedaje po zebraniu buy stops (powyżej swing high)
- **Sellside sweep = bullish**: SM kupuje po zebraniu sell stops (poniżej swing low)
- **Spójność**: `is_valid=False` → `quality.passed` MUSI być `False` (wymuszane w `_find_buyside/sellside_sweeps`)
- **ATR source**: `smc.utils.calculate_atr_series` — wspólna implementacja, zero duplikacji

---

## 15c. Harmonogram implementacji (9 tygodni)

| Tydzień | Cel | Definition of Done |
|---------|-----|--------------------|
| 1–2 | Fundament | OANDA API działa, SQLite schema v2.2, config YAML, DQ validators |
| 3 | SMC Engine | OB+FVG+BoS+PPDD, BAR, RVR, absorption, dynamic swing |
| 4 | IPDA + Scoring | Confluence Scorer 110 pkt, progi 60/70 |
| 5 | Filtry | H1Monitor, news blackout, filtry sesyjne, spread |
| 6 | AI Agenci | Agenci 1–4 + 3-tier fallback |
| 7 | Telegram | Webhook, EU VPS (Hetzner CX22), formaty sygnałów |
| 8 | Paper Trading | Min. 20 zamkniętych sygnałów, analiza WR |
| 9 | Optimizer + Launch | Optimizer Agent aktywny, kanał publiczny |

Faza 2 (po 3+ mies.): COT module, CVD prawdziwy dla BTC, Fear&Greed, XGBoost.
Faza 2 Tech Debt SMC: CHoCH z wymaganym przebiciem SH/SL, sprzeczne wyniki _detect_choch/_detect_bos dla ranging.

---

## 16. Compliance (przed publicznym launchem)

- Disclaimer w opisie kanału: "Sygnały mają charakter edukacyjny. Nie są rekomendacjami inwestycyjnymi."
- Każdy sygnał: stopka "To nie jest porada finansowa. Trading wiąże się z ryzykiem utraty kapitału."
- Opis metodologii: przypięta wiadomość z opisem SMC (OB, FVG, PPDD)
- Track record: publikuj WERYFIKOWALNE wyniki (screenshoty OANDA)
- Start: kanał darmowy → niższe ryzyko regulacyjne niż płatny
- Przed monetyzacją: konsultacja prawna (MAR/UKNF Polska)

---

## 17. Zasada której Cascade ma przestrzegać

> Jeden tydzień = jeden działający efekt który można sprawdzić w terminalu.
> Nie przechodź dalej dopóki bieżący tygodniowy milestone nie jest gotowy.
> Jeśli coś nie działa — napraw to zanim zaczniesz cokolwiek nowego.
