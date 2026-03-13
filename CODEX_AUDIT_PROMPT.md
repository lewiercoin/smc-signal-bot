# CODEX DEEP AUDIT PROMPT — SMC Signal Bot v2.2
# Cel: Kompleksowy audyt kodu pod kątem maksymalizacji zysków, optymalizacji AI i niezawodności.
# Wykonawca: OpenAI Codex / o3 / GPT-4.1
# Kontekst: Przeczytaj CONTEXT.md PRZED audytem. To źródło prawdy projektu.

---

## TOŻSAMOŚĆ BOTA I KONTEKST

Bot sygnałowy Telegram publikujący sygnały tradingowe EUR/USD, XAU/USD, BTC/USD
oparte o strategię **Smart Money Concepts (ICT/makuchaku)**.

Stack: Python 3.12, OANDA REST API v20, Claude Haiku, APScheduler, SQLite, python-telegram-bot.
Serwer: Hetzner CX22, Ubuntu 24.04, polling mode, systemd service.
Pipeline: OANDA → DataQuality → SMC Engine → ConfluenceScorer → AIAgents → RiskEngine → Telegram.

Specyfikacja: CONTEXT.md (Master Spec v2.2 + ITS v1.0). ZAWSZE zgodna ze specyfikacją.

---

## ZADANIE AUDYTOWE — 6 OBSZARÓW

### OBSZAR 1: STRATEGIA HANDLOWA — MAKSYMALIZACJA ZYSKU

Przeanalizuj cały pipeline od `engine/signal_generator.py` przez `engine/confluence_scorer.py`,
`smc/` (swing, structure, ob, fvg, liquidity), `engine/risk_engine.py`.

**Pytania do zbadania:**

1. **Confluence Scoring (0–110 pkt)** — Czy wszystkie komponenty z CONTEXT.md sekcja 5 są
   zaimplementowane? Sprawdź:
   - `_score_ppdd_ob()` — OB w discount/premium (+25 pkt)
   - `_score_fvg_overlap()` — FVG stacked na OB (+20 pkt)
   - `_score_htf_bos()` — HTF BoS Daily (+15 pkt)
   - `_score_liquidity_sweep()` — sweep przed setupem (+15 pkt)
   - `_score_session()` — sesja z mnożnikiem (+10 pkt)
   - `_score_dxy()` — DXY alignment (+10/5/0-5 pkt) ← czy zaimplementowany?
   - `_score_news()` — brak news <2h (+5/10 pkt) ← czy zaimplementowany?
   - `_score_absorption()` — GROK-1 absorption detection
   - Czy łączny maks wynosi 110 pkt? Czy żaden komponent nie jest pominięty?

2. **Progi decyzyjne** — Czy `score < 65 → reject` jest optymalny?
   Zaproponuj analizę: czy obniżenie do 60 lub podwyższenie do 70 historycznie
   przyniosłoby lepszy profit factor (na podstawie logiki scoring).

3. **IPDA Filter** — Sprawdź implementację w `engine/signal_generator.py`.
   Czy `ipda_percent 40-60% = hard reject` działa prawidłowo?
   Czy period = ostatnia kompletna świeca Weekly (nie bieżąca)?

4. **Order Block Quality** — Sprawdź `smc/ob_detector.py`:
   - BAR threshold: EUR=1.2, XAU=1.5, BTC=1.8 (per CONTEXT.md sekcja 8)
   - RVR threshold: EUR/XAU=1.5, BTC=2.0
   - Multiple Taps Rule: ≥3 taps → EXHAUSTED, score=0
   - Czy `ob_status` jest aktualizowany przy każdym skanie?

5. **Risk Engine** — Sprawdź `engine/risk_engine.py`:
   - SL ATR-buffered: k={EUR:0.5, XAU:0.75, BTC:1.0}
   - TP ratios: EUR(1.5/2.5/4.0R), XAU(1.8/3.5/6.0R), BTC(1.2/2.5/4.5R)
   - Breakeven: `entry + spread_median_7d × 1.5`
   - Position sizing: stały 2% ryzyka per trade
   - Czy spread gate prawidłowo konwertuje price units → pip units?
     (Bug FIX 4 z CONTEXT.md — sprawdź czy naprawiony w `_check_spread()`)

6. **Session Filter** — Sprawdź `bot/scheduler.py`:
   - Aktywne: 07:00–21:00 UTC, Mon–Fri
   - Mnożniki: London-NY overlap 0.9× (próg efektywny 59)
   - Azja 1.25× (próg 88 = praktyczny block)
   - Weekend = całkowity block

7. **Dynamic Swing Length** — Sprawdź `smc/swing_detector.py`:
   - HIGH (ATR ratio > 1.5×): swing=14
   - NORMAL (0.7-1.5×): swing=10
   - LOW (< 0.7×): swing=7
   - Stabilność: zmiana po 3 kolejnych świecach (histereza)

**Zidentyfikuj i napraw:**
- Brakujące komponenty scoringu
- Błędy w progach/formułach względem specyfikacji
- Edge cases które mogą powodować missed signals lub false positives
- Miejsca gdzie bot może tracić potencjalne zyski przez zbyt restrykcyjne filtry


### OBSZAR 2: ROLA AI W OPTYMALIZACJI — MAKSYMALIZACJA INTELIGENCJI

Przeanalizuj `agents/` (structure_agent, fundamental_agent, risk_verifier, telegram_editor, optimizer).

**Pytania do zbadania:**

1. **Agent 1 — Structure Analyst** (`agents/structure_agent.py`):
   - Czy prompt systemowy zawiera wszystkie potrzebne informacje o setupie?
   - Czy model dostaje: OB displacement, trapped traders context, absorption data?
   - Czy `quality_score` (1-10) i `recommendation` (PUBLISH/SKIP/WATCH) wpływają
     rzeczywiście na decyzję publikacji? Prześledź flow od `AgentResult` do `send_signal`.
   - Czy deterministic fallback działa prawidłowo gdy API timeout?

2. **Agent 2 — Fundamental Analyst** (`agents/fundamental_agent.py`):
   - Czy nagłówki z Finnhub są faktycznie pobierane i klasyfikowane?
   - Jakość promptu: czy LLM dostaje kontekst sesji, instrumentu, godziny?
   - Czy 3+ BULLISH → "POZYTYWNY" wpływa na scoring lub tylko informacyjnie?
   - Sprawdź `_get_session_info()` i `_parse_instrument()`.

3. **Agent 3 — Risk Verifier** (`agents/risk_verifier.py`):
   - Deterministyczny (BEZ LLM) — czy wszystkie 5 sprawdzeń działa?
   - Kolejność: daily_loss → max_positions → correlation → spread_zscore → sizing
   - Czy correlation threshold 0.60 jest prawidłowy (identyczny instrument=1.0)?
   - Czy circuit breaker ≥5% blokuje prawidłowo?

4. **Agent 4 — Telegram Editor** (`agents/telegram_editor.py`):
   - Jakość formatowania sygnałów — czy wiadomości są czytelne i profesjonalne?
   - Czy deterministic fallback generuje poprawny format z emoji 🟢/🔴?
   - Czy disclaimer jest zawsze dołączany?

5. **Optimizer Agent** (`agents/optimizer.py` + `bot/scheduler.py`):
   - Sprawdź 5-warstwową ochronę: DENYLIST → WHITELIST → type parse → range → delta ±20%
   - Czy metryki (win_rate, avg_r, profit_factor, max_drawdown, expectancy) są prawidłowo
     liczone w `_calculate_metrics()`?
   - Czy adapter `_adapt_closed_signals_for_optimizer()` poprawnie mapuje DB → Optimizer?
   - MIN_SAMPLE: 30 tradów — czy sprawdzenie jest prawidłowe?

**Zaproponuj konkretne ulepszenia promptów** dla Agenta 1 i 2 które mogą:
- Zwiększyć jakość filtrowania setupów
- Lepiej wykorzystać kontekst rynkowy (sesja, volatility regime, HTF bias)
- Zmniejszyć liczbę false positives

**Zaproponuj rozszerzenie roli AI:**
- Czy Agent 1 mógłby dostać dostęp do historii ostatnich 5 sygnałów per instrument
  (win/loss context) — bez naruszania zasady READ-ONLY?
- Czy warto dodać Agent 6 — "Pattern Memory": LLM analizuje najlepsze setupy z ostatnich
  30 dni i wyciąga wspólne cechy → zwiększa wagę tych cech w scoring?
  (Sprawdź zgodność z zasadą "NIE wdraża automatycznie")


### OBSZAR 3: MONITORING I ALERTY — "CZY BOT DZIAŁA"

Przeanalizuj `bot/monitoring.py`, `bot/scheduler.py`, `bot/telegram_bot.py`, `main.py`.

**Aktualny stan monitoringu (in-memory, Faza 1):**
- Countery: `scan_count`, `signal_count`, `error_count`, `last_scan_time`, `last_error`
- Brak persistent metrics, brak alertów proaktywnych

**Zaimplementuj lub zaproponuj implementację:**

1. **Heartbeat Alert** — Bot wysyła do admin co 6h wiadomość:
   ```
   ✅ Bot ŻYJE | 12:00 UTC
   Skany: 24 | Sygnały: 2 | Błędy: 0
   Ostatni scan: 11:45 UTC | Następny: 12:00 UTC
   ```
   Trigger: `CronTrigger(hour='0,6,12,18')` w scheduler.
   Przy `error_count > 0` — zmień emoji na ⚠️ i dodaj opis ostatniego błędu.

2. **Dead Man's Switch** — Jeśli bot nie wykonał skanu przez >30 minut w godzinach
   aktywnych (07-21 UTC Mon-Fri):
   - Wyślij alert do admina: "⛔ BOT NIE SKANUJE od {X} minut!"
   - Sprawdzanie: co 5 minut przez osobny job schedulera.

3. **OANDA API Health Check** — Co 30 minut (poza skanami) sprawdź czy OANDA
   odpowiada (`GET /v3/accounts/{id}` z timeoutem 5s).
   - Sukces: log debug
   - Fail: alert admin + `error_count++`

4. **Signal Quality Alert** — Jeśli przez 24h aktywne sesje (07-21 UTC) nie pojawił
   się żaden sygnał → alert admin: "⚠️ 24h bez sygnałów — sprawdź logi"

5. **Daily Summary** — Każdego dnia o 21:00 UTC (koniec sesji NY):
   ```
   📊 Dzienny raport SMC Bot
   Data: 2026-03-13
   Skany: 56 | Sygnały: 3 | Błędy: 1
   
   Otwarte pozycje: EUR/USD LONG @ 1.0850
   Zamknięte dziś: XAU/USD SHORT → TP1 ✅ (+1.5R)
   
   Equity dziś: +2.3% | Drawdown: 0.8%
   ```

6. **Error Rate Alert** — Jeśli `error_count / scan_count > 0.2` (>20% błędów):
   - Alert: "⛔ Wysoki błąd rate: {X}% — bot może nie działać prawidłowo"

**Wymagania techniczne:**
- Wszystkie alerty przez `_notify_admin()` w `telegram_bot.py`
- Persistent countery w SQLite tabela `bot_health` (nie in-memory)
- Graceful: błąd w monitoringu NIE crashuje bota
- Logi przez structlog: `log.info("heartbeat_sent", ...)` itp.


### OBSZAR 4: DATA QUALITY I ODPORNOŚĆ NA BŁĘDY

Przeanalizuj `dq/data_quality.py`, `connectors/oanda_client.py`, `connectors/news_client.py`.

**Pytania:**

1. **OANDA resilience** — Czy `get_candles()` i `get_current_spread()` mają:
   - Retry logic (np. 3 próby z exponential backoff)?
   - Timeout (domyślny oandapyV20 = brak timeout)?
   - Graceful handling gdy OANDA zwraca pustą odpowiedź?

2. **MIN_CANDLES = 99** — Czy to wystarczy dla wszystkich detektorów SMC?
   Sprawdź minima: SwingDetector min 14, OBDetector min 14, ConfluenceScorer min 14.
   Czy 99 świec H1 daje wystarczający kontekst dla dynamic swing (ATR z 50 świec)?

3. **News Client resilience** — Sprawdź `connectors/news_client.py`:
   - ForexFactory RSS: czy jest timeout? Czy 403 error jest obsługiwany gracefully?
   - Finnhub `/calendar/economic`: czy brak klucza → `blocked=True` (fail-safe)?
   - Cache 5min: czy jest thread-safe?

4. **Spread DQ** — Aktualne limity:
   - EUR_USD: 2.0 pips ← czy nie za restrykcyjne w momentach wysokiej zmienności?
   - XAU_USD: 100.0 pips ← ustawione dla OANDA practice; na live ~30 pips
   - BTC_USD: 50.0 pips ← sprawdź czy realistyczne dla OANDA practice
   Zaproponuj dynamiczny spread limit: `base_limit × session_multiplier`
   (np. przed news × 1.5, sesja azjatycka × 2.0)

5. **Candle gaps** — Czy `check_candle_gaps()` prawidłowo obsługuje weekendy i święta?
   Granularity H1: max gap = 2h (2× granularity). Czy weekend gap nie powoduje false fail?


### OBSZAR 5: WYDAJNOŚĆ I SKALOWALNOŚĆ

Przeanalizuj całą bazę kodu pod kątem performance.

**Pytania:**

1. **Scan latency** — Ile zajmuje jeden pełny scan dla 3 par?
   Zidentyfikuj bottlenecki: API calls, LLM calls, DB operations.
   Czy 3 pary są skanowane sekwencyjnie czy równolegle?
   Zaproponuj: `asyncio.gather()` dla równoległego skanowania gdy pipeline jest async.

2. **LLM calls** — Ile wywołań Anthropic API na jeden sygnał?
   Sprawdź czy cache (TTL 24h) działa poprawnie dla `BaseAgent`.
   Estymuj koszt miesięczny przy założeniu 10 sygnałów/tydzień.

3. **SQLite performance** — Czy tabela `signals` ma indeksy na:
   - `instrument`, `status`, `created_at`, `signal_uuid`?
   Bez indeksów queries przy 500+ rekordach spowalniają.

4. **Memory leaks** — Czy `BotMonitor` in-memory countery mogą rosnąć nieogranicznie?
   Sprawdź `last_error` — czy przechowuje pełny traceback (może być duży)?

5. **APScheduler overlap** — Czy istnieje zabezpieczenie przed overlapping jobs?
   Jeśli scan trwa >15 minut, czy kolejny job jest blokowany czy uruchamiany równolegle?
   Użyj `max_instances=1` dla scan job.


### OBSZAR 6: BEZPIECZEŃSTWO I COMPLIANCE

**Pytania:**

1. **Credentials** — Czy wszystkie API keys są WYŁĄCZNIE w `.env` (nie hardkodowane)?
   Sprawdź wszystkie pliki `.py` na obecność kluczy API.

2. **Admin auth** — Czy `_is_admin()` w `telegram_bot.py` jest wystarczające?
   Czy jest zabezpieczenie przed flood commands (rate limiting)?

3. **SQLite backup** — Czy istnieje mechanizm backupu `signals.db`?
   Zaproponuj: cron job co 24h → kopia do `/root/backups/signals_YYYYMMDD.db`

4. **Error leakage** — Czy error messages wysyłane do admina przez Telegram
   nie zawierają wrażliwych danych (API keys w stack trace)?

5. **Disclaimer compliance** — Sprawdź `agents/telegram_editor.py`:
   Czy każda wiadomość sygnałowa zawiera disclaimer?
   Czy `/start` command wyświetla właściwy disclaimer?

---

## FORMAT RAPORTU AUDYTOWEGO

Dla każdego znaleziska użyj formatu:

```
### [OBSZAR X] Tytuł Znaleziska
**Severity:** BLOCKER | HIGH | MEDIUM | LOW | INFO
**Plik:** `ścieżka/do/pliku.py:linia`
**Problem:** Opis problemu
**Wpływ na zyski/działanie:** Konkretny wpływ
**Fix:** Konkretna implementacja (kod Python)
**Test:** Jak zweryfikować fix
```

---

## PRIORYTETY IMPLEMENTACJI

Po zakończeniu audytu utwórz listę priorytetów:

**P1 — BLOCKER** (napraw natychmiast, bot może tracić pieniądze lub nie działać):
- Brakujące komponenty scoring wpływające na miss rate
- Błędy w risk engine (SL/TP/sizing)
- Crash scenarios bez graceful handling

**P2 — HIGH** (napraw w ciągu tygodnia):
- Brakujące alerty monitoringu
- Retry logic dla OANDA API
- Ulepszenia promptów AI

**P3 — MEDIUM** (napraw przed go-live):
- Performance optimizations
- SQLite indeksy
- Daily summary report

**P4 — LOW / INFO** (tech debt, Faza 2):
- Rozszerzenia AI (Pattern Memory Agent)
- Dynamiczny spread limit
- PostgreSQL migration trigger

---

## ZASADY KTÓRYCH NIE WOLNO ŁAMAĆ

Czytaj CONTEXT.md sekcja 14 (odrzucone propozycje). W szczególności:

- **NIE** proponuj XGBoost przed 200+ sygnałami
- **NIE** proponuj CrewAI/LangGraph
- **NIE** proponuj JBlanked API
- **NIE** proponuj MT5
- **NIE** proponuj TP3 BTC = 8R (max 4.5R)
- **NIE** zmieniaj stosu technicznego (Python, SQLite, OANDA, Claude Haiku)
- **NIE** wdrażaj zmian Optimizer automatycznie — zawsze READ-ONLY + notify_admin
- **NIE** dodawaj Monte Carlo do risk verifier (latencja)
- Każda zmiana musi być pokryta testem pytest

---

## KONTEKST PRODUKCYJNY (aktualny stan)

Bot działa od 2026-03-13 na Hetzner CX22.
Tryb: polling (USE_POLLING=true).
Aktualne score'y przy normalnym rynku konsolidacyjnym:
- EUR/USD: score=0 (trend=ranging, brak BOS/CHoCH)
- XAU/USD: score=53 (BOS=True, 2 OB, brak FVG/liquidity alignment)
- BTC/USD: score=55 (CHoCH=True, 1 bullish OB, 7 liquidity sweeps)

Próg publikacji: 65 pkt. Żaden sygnał nie przekroczył progu podczas konsolidacji.
To NORMALNE zachowanie — audyt ma sprawdzić czy scoring jest optymalny,
nie czy bot "nie działa".

Bugi naprawione przed uruchomieniem (NIE naprawiaj ponownie):
- OANDA Account ID format
- Spread parsing: `bids[0]["price"]` zamiast `price["bid"]`
- MIN_CANDLES: 99 (nie 100)
- XAU_USD spread limit: 100 pips (nie 30)
- requirements.txt: six + tornado

---

## DODATKOWE PYTANIA STRATEGICZNE

Po technicznym audycie odpowiedz na pytania strategiczne:

1. **Czy bot jest zdolny do zarabiania pieniędzy** przy obecnej implementacji?
   Jakie są główne ryzyka miss rate vs false positive rate?

2. **Gdzie AI jest niedostatecznie wykorzystane?**
   Gdzie decyzje są deterministyczne a mogłyby skorzystać na kontekście LLM?

3. **Jaki jest szacowany win rate** przy obecnym scoring (zakładając sprawny rynek)?
   Bazuj na logice: OB+FVG+sweep+BOS alignment historycznie w SMC daje 55-65% WR.

4. **Co jest największą słabością** całego systemu pod kątem zysku?

5. **Gdybyś miał zaimplementować JEDNĄ rzecz** która maksymalnie zwiększyłaby
   profit factor bota — co by to było? (w ramach obecnego stosu technicznego)

---

*Prompt wygenerowany: 2026-03-13 | Wersja bota: v2.2 | Do użytku z OpenAI Codex / o3 / GPT-4.1*
*Przed audytem przeczytaj: CONTEXT.md, engine/confluence_scorer.py, engine/signal_generator.py*
