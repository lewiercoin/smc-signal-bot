# SMC Conventions — Parametry i Logika
*Windsurf Rule: Always On*

## Instrumenty i profile
Trzy instrumenty, każdy ma osobny plik YAML w `config/profiles/`.
Nigdy nie używaj twardych wartości w kodzie — zawsze czytaj z configu.

| Parametr | EUR/USD | XAU/USD | BTC/USD |
|----------|---------|---------|---------|
| swing_length (base) | 28 | 24 | 18 |
| displacement_atr_min | 1.2 | 1.5 | 1.8 |
| bar_ratio_min (BAR) | 1.2 | 1.5 | 1.8 |
| rvr_ratio_min | 1.5 | 1.5 | 2.0 |
| absorption body_max | 0.30 | 0.30 | 0.25 |
| absorption vol_min | 1.5 | 1.5 | 2.0 |

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

## Progi decyzyjne (nie zmieniaj bez wyraźnej decyzji)
```
score < 60   → odrzuć, nie wywołuj agentów
score ≥ 60   → wywołaj AI Agentów 1–4
score ≥ 70   → opublikuj na kanale Telegram
```

## Dynamic swing_length [GROK-2]
Zakres: 16–40. Obliczany w `utils/dynamic_swing.py`.
Reżimy zmienności (na podstawie bieżącego ATR vs. mediana ATR z ostatnich 50 świec):
```
EXTREME (ATR ratio ≥ 2.0)  → swing = base × 0.55, clamp do 16
HIGH    (ratio 1.4–2.0)    → swing = base × 0.72
NORMAL  (ratio 0.8–1.4)    → swing = base × 1.00
LOW     (ratio 0.5–0.8)    → swing = base × 1.30
FLAT    (ratio < 0.5)      → swing = base × 1.55, clamp do 40
```

## Absorption Detection [GROK-1]
Zaimplementowane w `smc/absorption.py`.
Absorption = `body_ratio ≤ threshold AND volume_ratio ≥ threshold`.
`body_ratio = abs(close - open) / (high - low)`
Kierunek: długi dolny wick → BULLISH absorption, długi górny wick → BEARISH absorption.
WAŻNE: Dla EUR/USD i XAU/USD OANDA daje tick volume (proxy), nie prawdziwy wolumen.
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
