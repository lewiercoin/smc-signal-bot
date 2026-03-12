# SMC Conventions — Parametry i Logika
*Windsurf Rule: Always On*

## Instrumenty i profile
Parametry hardcoded w modułach — brak pliku YAML config. Źródło prawdy: `engine/risk_engine.py`.

| Parametr | EUR/USD | XAU/USD | BTC/USD |
|----------|---------|---------|---------|
| pip_size | 0.0001 | 0.01 | 1.0 |
| max_spread (pips) | 2.0 | 30.0 | 50.0 |
| TP1 / TP2 / TP3 (R) | 1.5 / 2.5 / 3.5 | 1.5 / 2.5 / 3.5 | 1.5 / 2.5 / 5.5 |
| max_risk_pct | 2% | 2% | 2% |
| absorption body_ratio | > 0.70 | > 0.70 | > 0.70 |
| absorption vol_ratio | ≥ 1.5 | ≥ 1.5 | ≥ 2.0 |

## IPDA — strefy i reguły
```
LONG  → tylko gdy ipda_percent w zakresie  0–40%   (Discount)
SHORT → tylko gdy ipda_percent w zakresie 60–100%  (Premium)
SKIP  → gdy ipda_percent w zakresie 40–60%          (Neutralna — hard reject)
```
Zakres IPDA liczymy na podstawie ostatniej kompletnej świecy Weekly lub Daily (z configu `bias` timeframe).
Wzór: `ipda_percent = (close - period_low) / (period_high - period_low) * 100`

## Confluence Scorer — wagi (maks. 110 pkt)
```
PPDD OB in discount/premium     +25 pkt
FVG overlap na OB (stacked)     +20 pkt
HTF BoS Daily potwierdzony      +15 pkt
Liquidity sweep przed setupem   +15 pkt
Sesja (London/NY, mnożnik)      +10 pkt
DXY alignment (EUR: +10, XAU: +5, BTC: 0–5)
Brak news <2h HIGH impact       +5–10 pkt (per instrument)
FVG Bias licznik HTF            +5 pkt
Absorption zgodna STRONG        +3–5 pkt (BTC: +5, Forex: +3)
Absorption zgodna MODERATE      +2–3 pkt
Absorption SPRZECZNA            -8 pkt (override ostrzegawczy)
Multiple Taps ≥3 bez reakcji    -10 do -25 pkt
```

## Progi decyzyjne ⚠️ ZAKTUALIZOWANE (nie zmieniaj bez wyraźnej decyzji)
```
score < 65   → odrzuć, generuj None (jeden próg, nie dwa)
score ≥ 65   → generuj Signal, zapisz do DB, wyślij na Telegram
```
**UWAGA**: Stare rules miały progi 60/70 (dwa progi). Faktyczny kod używa JEDNEGO progu 65 (`confluence_threshold=65` w `SignalGenerator`). Nie zmieniaj na 60/70.

## Dynamic swing_length [GROK-2]
Zaimplementowany w `smc/swing_detector.py` (nie w osobnym `utils/dynamic_swing.py`).
Trzy reżimy zmienności (ATR-adaptive):
```
HIGH    (ATR ratio ≥ 1.5)   → swing_length = 14  (długi — większe swingsy w zmiennym rynku)
NORMAL  (ratio 0.7–1.5)    → swing_length = 10  (domyślny, BASE_SWING_LENGTH)
LOW     (ratio < 0.7)      → swing_length = 7   (krótki — mała zmienność)
```
**UWAGA**: Stare rules miały 5 reżimów (EXTREME/HIGH/NORMAL/LOW/FLAT) i zakres 16–40. Faktyczny kod używa 3 reżimów (HIGH/NORMAL/LOW), wartości 14/10/7, próg HIGH_VOL_THRESHOLD=1.5 (nie 1.4).

## Absorption Detection [GROK-1]
Zaimplementowane w `engine/confluence_scorer.py` (NIE w osobnym `smc/absorption.py`).
Absorption = duże ciało świecy (pochodzi impuls) + wysoki wolumen.
`body_ratio = abs(close - open) / (high - low)`
```
FORMAT: body_ratio > 0.70 AND volume_ratio ≥ 1.5 (Forex) / 2.0 (BTC)
```
⚠️ KRYTYCZNE: `body_ratio > 0.70` (duże body = absorpcja) — NIE `≤ 0.30`.
Stare rules miały odwrotną logikę (`body_max ≤ 0.30` — małe body). To było BŁĘDEM.
Kierunek: długi dolny wick + duże body up → BULLISH, długi górny wick + duże body down → BEARISH.
WAżNE: OANDA daje tick volume (proxy) dla EUR/USD i XAU/USD, nie prawdziwy wolumen.
Dla BTC/USD używaj prawdziwego wolumenu z Binance (CCXT).

## OB Quality Filters
Body-ATR Ratio (BAR): `BAR = abs(impulse_close - impulse_open) / ATR(14)`
Relative Volume Ratio (RVR): `RVR = candle_volume / SMA(Volume, 20)`
Multiple Taps Rule: jeśli OB testowany ≥3 razy bez silnego odrzutu → degraduj do 0 pkt, status = EXHAUSTED.

## Sesje tradingowe i filtry
```
London open     (08:00–10:00 UTC)  → multiplier 1.0×, próg 70
NY open         (13:00–15:00 UTC)  → multiplier 1.0×, próg 70
London-NY OVL   (13:00–17:00 UTC)  → multiplier 0.9×, próg 63 (obniżony!)
Azja            (00:00–07:00 UTC)  → multiplier 1.25×, próg 88 (blokuj prawie wszystko)
Weekend                             → blokuj całkowicie
```

## Filtr newsów — kalendarz ekonomiczny
Źródło: Finnhub `/calendar/economic` (nie JBlanked — 1 req/day limit, odrzucone).
Backup: ForexFactory RSS.
Blackout: ±120 minut od HIGH impact event dla danej waluty.
Kesh: zapisuj do tabeli `economic_events` w SQLite, odświeżaj co 4h.

## H1Monitor — trigger entry
Aktywuje się po selekcji setupu na 4H.
Sprawdza CHoCH_1H lub BrokenFractal_1H co 15 minut.
Timeout: 8h od momentu selekcji — jeśli nie ma triggera, setup wygasa.
Stały swing_length=15 dla H1 (nie dynamiczny — H1 ma za mało historii).

## Sentinel — czego NIE wolno
- Nigdy nie implementuj backtestowania jako cechę MVP (to jest tygodniowy bot sygnałowy).
- Nigdy nie dodawaj XGBoost ML Gate dopóki nie ma 200+ sygnałów w bazie.
- Nigdy nie zamieniaj SQLite na PostgreSQL zanim nie ma 500+ sygnałów/miesiąc.
- Nigdy nie uruchamiaj CrewAI ani LangGraph — zbędny overhead.
- Nigdy nie używaj MT5 API — broker to OANDA REST.
